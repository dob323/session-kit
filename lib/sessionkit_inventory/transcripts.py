"""Where a conversation's own record lives, whichever profile wrote it.

One resolver, used by everything that has to answer "can this machine still
read that conversation": the closed-session list (a conversation whose
transcript is gone is not restorable and stops being offered), the history
fallback, and the doctor's reachability check.

The measured defect this answers: a live session running under a rotated
account profile had its transcript on this disk, and the tool asked for it
reported "no transcript found", because one resolver knew a root the other did
not. Every root the kit may have written to is searched here, once.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Iterator, Mapping

from .common import valid_uuid

MAX_ROLLOUT_FILES = 20_000


def _home(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("HOME") or Path.home())


def _account_root(environ: Mapping[str, str]) -> Path:
    return Path(
        environ.get("SESSION_KIT_ACCOUNT_ROOT")
        or _home(environ) / ".local/share/session-kit/accounts"
    )


def claude_roots(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Every Claude profile root this machine may have written to."""
    env = environ if environ is not None else os.environ
    roots = [_home(env) / ".claude"]
    explicit = env.get("CLAUDE_CONFIG_DIR")
    if explicit:
        roots.append(Path(explicit))
    try:
        roots.extend(sorted((_account_root(env) / "claude").glob("*")))
    except OSError:
        pass
    return [root for root in roots if root.is_dir()]


def codex_roots(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Every Codex home this machine may have written rollouts to."""
    env = environ if environ is not None else os.environ
    roots = [_home(env) / ".codex"]
    for name in ("SESSION_KIT_CODEX_HOME", "CODEX_HOME"):
        explicit = env.get(name)
        if explicit:
            roots.append(Path(explicit))
    try:
        roots.extend(sorted((_account_root(env) / "codex").glob("*")))
    except OSError:
        pass
    return [root for root in roots if root.is_dir()]


def _newest(paths: Iterator[Path]) -> Path | None:
    best: Path | None = None
    best_mtime = -1.0
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def _claude_transcript(uuid: str, environ: Mapping[str, str]) -> Path | None:
    def candidates() -> Iterator[Path]:
        for root in claude_roots(environ):
            try:
                yield from (root / "projects").glob(f"*/{uuid}.jsonl")
            except OSError:
                continue

    return _newest(candidates())


def _codex_rollout(uuid: str, environ: Mapping[str, str]) -> Path | None:
    def candidates() -> Iterator[Path]:
        seen = 0
        for root in codex_roots(environ):
            sessions = root / "sessions"
            if not sessions.is_dir():
                continue
            try:
                for directory, names, files in os.walk(sessions):
                    names[:] = sorted(
                        name for name in names if not name.startswith(".")
                    )
                    for name in sorted(files):
                        seen += 1
                        if seen > MAX_ROLLOUT_FILES:
                            return
                        if (
                            uuid in name
                            and name.startswith("rollout-")
                            and name.endswith(".jsonl")
                        ):
                            yield Path(directory) / name
            except OSError:
                continue

    return _newest(candidates())


def locate_transcript(
    provider: str, uuid: str, *, environ: Mapping[str, str] | None = None
) -> Path | None:
    """The conversation's own record, or None when this machine cannot read it."""
    env = environ if environ is not None else os.environ
    exact = valid_uuid(uuid)
    if not exact:
        return None
    if provider == "claude":
        return _claude_transcript(exact, env)
    if provider == "codex":
        return _codex_rollout(exact, env)
    return None


def transcript_snapshot(
    provider: str, uuid: str, *, environ: Mapping[str, str] | None = None
) -> dict[str, object] | None:
    """The sweep's exact transcript-movement evidence for one conversation.

    Path discovery stays with the kit's existing provider-neutral resolver.  The
    selected path is then opened without following a symlink, read once to
    prove it is readable, and statted through that open descriptor.  This is
    the same size/mtime evidence family used by the shipped sub-agent sweep;
    missing, unreadable and irregular files are no evidence of idleness.
    """
    env = environ if environ is not None else os.environ
    path = locate_transcript(provider, uuid, environ=env)
    if path is None:
        return None
    return stable_transcript_snapshot(path)


def stable_transcript_snapshot(path: Path) -> dict[str, object] | None:
    """Snapshot one exact path with the shipped sweep's stable-open rules."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        os.read(descriptor, 1)
        info = os.fstat(descriptor)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return {
        "path": os.fspath(path),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }
