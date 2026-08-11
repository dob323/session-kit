"""Owner-private source events used to prove direct operator authority.

The provider hook, not an agent message, writes these records.  This machine
uses a cooperative same-UID trust model: owner/mode/symlink checks make stale
or ambiguous paths fail closed, but they do not provide cryptographic identity
against another process running as the same Unix user.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

try:
    from sessionkit_messages.envelope import valid_thread_key, valid_uuid
    from sessionkit_supervisor.source_ledger import LedgerError, SegmentedLedger
except ModuleNotFoundError:  # ``lib.sessionkit_supervisor`` test/import form
    # Same symbols by the package-relative path the test import form needs;
    # the rebinding is the point of the fallback, not an accidental shadow.
    from ..sessionkit_messages.envelope import (  # type: ignore[no-redef]
        valid_thread_key,
        valid_uuid,
    )
    from .source_ledger import LedgerError, SegmentedLedger


MAX_RAW_PROMPT_BYTES = 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
MAX_CODEX_HEAD_BYTES = 1024 * 1024
MAX_TRANSCRIPT_PATH_BYTES = 4096
MAX_TURN_BYTES = 200
MAX_DISPLAY = 500
ACCEPTANCE_PATH_ENV = "SESSION_KIT_SOURCE_ACCEPTANCE_PATH"
ACCEPTANCE_DIGEST_ENV = "SESSION_KIT_SOURCE_ACCEPTANCE_DIGEST"
INTAKE_COMMIT_PATH_ENV = "SESSION_KIT_INTAKE_COMMIT_PATH"
MANAGED_GENERATION_ENV = "SESSION_KIT_MANAGED_GENERATION"
EVENT_ID_RE = re.compile(r"\A[0-9a-f]{64}\Z")
AUTHORITY_TTL_MAX_MS = 15 * 60 * 1000
SIDECHAIN_MARKERS = (
    "parent_session_id",
    "parentSessionId",
    "parent_tool_use_id",
    "parentToolUseId",
    "parent_thread_id",
    "is_sidechain",
    "isSidechain",
    "subagent_id",
    "subagent_type",
)
# Harness envelopes reach `UserPromptSubmit` through the very channel a
# person's typing reaches: the provider wraps a machine turn in one of these
# tags and submits it as the prompt. Every entry is an opening XML-style tag
# the harness itself emits, observed at the start of machine user-turns in
# this machine's own transcript corpus. Matching is exact-prefix, so a prompt
# that quotes one of these later in its text is still the operator's own.
MACHINE_ENVELOPE_PREFIXES = (
    "<bash-input",
    "<bash-stderr",
    "<bash-stdout",
    "<command-args",
    "<command-message",
    "<command-name",
    "<cross-session-message",
    "<local-command-caveat",
    "<local-command-stderr",
    "<local-command-stdout",
    "<system-reminder",
    "<task-notification",
    "<teammate-message",
)
# Machine transport this kit or the provider itself submits as a prompt.
OPERATOR_ENVELOPE_PREFIXES = (
    "[session-kit operator message",
    "Continue from where you left off.",
    "You are a Session Kit delivery runner.",
)
# Codex names one rollout `rollout-<timestamp>-<session id>.jsonl` under a
# `CODEX_HOME/sessions/YYYY/MM/DD` directory. The depth and entry bounds keep
# a prompt hook from walking an unexpectedly large or looping tree.
# Kit-owned state — events, ledger, lock, markers — is verified under the
# strict owner-private mask: no group or other bit at all.
#
# A provider writes its own transcript under the operator's umask, not ours.
# Codex creates every rollout 0644 under the default 0022 umask and never
# chmods it, so the strict mask made Codex transcript evidence unreachable on
# any ordinary machine. A Codex rollout is verified under the no-foreign-write
# rule instead: owned by this uid, and no group or other WRITE bit. Forging
# authority evidence needs write access; a read bit Codex itself set adds no
# attack surface that does not already exist. The Claude path keeps the strict
# mask — Claude writes its transcripts 0600 and verifies under it today, and an
# invariant that demonstrably works is not weakened. Maintainer-approved scope,
# recorded on the change that introduced it.
PRIVATE_MODE_MASK = 0o077
PROVIDER_TRANSCRIPT_MODE_MASK = 0o022
CODEX_ROLLOUT_MAX_DEPTH = 4
CODEX_ROLLOUT_MAX_ENTRIES = 50_000
CODEX_ROLLOUT_TIMESTAMP = r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}"


class SourceAuthorityError(ValueError):
    """A source event or its evidence cannot be trusted."""


def _now_ms(clock: Callable[[], float]) -> int:
    return int(clock() * 1000)


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
    ):
        raise SourceAuthorityError(
            f"source-event state must be a mode-0700 current-owner directory: {path}"
        )


def _private_file_info(path: Path, *, maximum: int) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SourceAuthorityError(f"cannot inspect source-event evidence: {path}") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_uid != os.geteuid()
        or info.st_size > maximum
    ):
        raise SourceAuthorityError(
            f"source-event evidence must be an owner-private regular file: {path}"
        )
    return info


def _read_private(path: Path, *, maximum: int) -> bytes:
    before = _private_file_info(path, maximum=maximum)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SourceAuthorityError(f"cannot open source-event evidence: {path}") from exc
    try:
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise SourceAuthorityError("source-event evidence changed while it was opened")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            raise SourceAuthorityError("source-event evidence exceeds its size limit")
        return raw
    finally:
        os.close(descriptor)


def _read_private_region(
    path: Path, *, offset: int, maximum: int, mask: int = PRIVATE_MODE_MASK
) -> tuple[bytes, int, os.stat_result]:
    """Read one stable bounded transcript region without bounding file size."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise SourceAuthorityError(f"cannot inspect source-event evidence: {path}") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) & mask
        or before.st_uid != os.geteuid()
        or not isinstance(offset, int)
        or offset < 0
    ):
        raise SourceAuthorityError(
            f"source-event evidence must be an owner-private regular file: {path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            opened.st_dev, opened.st_ino, opened.st_size
        ):
            raise SourceAuthorityError("source-event evidence changed while it was opened")
        raw = os.pread(descriptor, maximum, min(offset, opened.st_size))
        after = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            after.st_dev, after.st_ino, after.st_size
        ):
            raise SourceAuthorityError("source-event evidence changed while it was read")
        return raw, opened.st_size, opened
    finally:
        os.close(descriptor)


def _read_private_tail(
    path: Path, *, maximum: int, mask: int = PRIVATE_MODE_MASK
) -> tuple[bytes, int, int, os.stat_result]:
    info = path.lstat()
    start = max(0, info.st_size - maximum)
    raw, size, opened = _read_private_region(
        path, offset=start, maximum=maximum, mask=mask
    )
    if size != info.st_size:
        raise SourceAuthorityError("source-event evidence changed before tail capture")
    return raw, start, size, opened


def _read_private_codex_windows(
    path: Path,
) -> tuple[bytes, bytes, int]:
    """Read stable bounded canonical head and tail windows from one inode.

    Codex only. The mode test is the provider-transcript rule — this uid owns
    it and nobody else can write it — while ownership stays exact, so a
    foreign-owned rollout is refused however unwritable it is.
    """
    try:
        before = path.lstat()
    except OSError as exc:
        raise SourceAuthorityError(f"cannot inspect source-event evidence: {path}") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) & PROVIDER_TRANSCRIPT_MODE_MASK
        or before.st_uid != os.geteuid()
    ):
        raise SourceAuthorityError(
            f"source-event evidence must be an unwritable regular file this uid owns: {path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SourceAuthorityError(f"cannot open source-event evidence: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino, opened.st_size)
        if (before.st_dev, before.st_ino, before.st_size) != identity:
            raise SourceAuthorityError("source-event evidence changed while it was opened")
        head = os.pread(descriptor, MAX_CODEX_HEAD_BYTES, 0)
        tail_start = max(0, opened.st_size - MAX_TRANSCRIPT_BYTES)
        tail = os.pread(descriptor, MAX_TRANSCRIPT_BYTES, tail_start)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size) != identity:
            raise SourceAuthorityError("source-event evidence changed while it was read")
        try:
            current = path.lstat()
        except OSError as exc:
            raise SourceAuthorityError(
                "source-event evidence was replaced while it was read"
            ) from exc
        if (
            path.is_symlink()
            or (current.st_dev, current.st_ino, current.st_size) != identity
        ):
            raise SourceAuthorityError("source-event evidence was replaced while it was read")
        return head, tail, tail_start
    finally:
        os.close(descriptor)


def _atomic_private_write(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def prompt_sha256(prompt: str) -> str:
    """SHA-256 of the exact UTF-8 bytes supplied by the hook."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def source_event_id(provider: str, session_id: str, turn_id: str, digest: str) -> str:
    material = "\0".join((provider, session_id, turn_id, digest)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _tuple_id(provider: str, session_id: str, turn_id: str) -> str:
    return hashlib.sha256(
        "\0".join((provider, session_id, turn_id)).encode("utf-8")
    ).hexdigest()


def _display(prompt: str) -> str:
    flattened = " ".join(
        "".join(character for character in prompt if character >= " ").split()
    )
    return flattened[:MAX_DISPLAY]


def _direct_source_prompt(prompt: str) -> bool:
    """Exclude machine/operator envelopes that reached the same hook.

    Only a prompt that BEGINS as an envelope is refused. Operator prose that
    quotes, pastes, or discusses one of these tags mid-message stays
    certifiable, which is what keeps this screen conservative: it can cost a
    person a certified turn only if they open their message with a literal
    harness tag.
    """
    stripped = prompt.lstrip()
    return not stripped.startswith(
        OPERATOR_ENVELOPE_PREFIXES + MACHINE_ENVELOPE_PREFIXES
    )


def _turn(value: object) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", False
    if not value or value != value.strip() or any(ord(char) < 32 for char in value):
        return "", False
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return "", False
    if len(encoded) > MAX_TURN_BYTES:
        return "", False
    return value, True


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _codex_sessions_root() -> Path:
    """`CODEX_HOME/sessions`, resolved exactly as `_transcript_path` does."""
    home = Path(os.environ.get("HOME", os.fspath(Path.home()))).resolve()
    return (
        Path(os.environ.get("CODEX_HOME", os.fspath(home / ".codex"))).resolve()
        / "sessions"
    )


def _codex_rollout_path(session_id: str) -> str:
    """The one rollout file Codex wrote for this session, or "".

    Codex's `UserPromptSubmit` payload carries no `transcript_path`, so an
    otherwise evidence-free capture derives it from the session id. The file
    name must be exactly `rollout-<timestamp>-<session id>.jsonl` — an anchored
    match with the id as a whole trailing component, never a prefix or a
    substring of a longer id — and it must be reached from `CODEX_HOME` without
    crossing a symlink at any level. Zero matches, more than one, or a tree
    larger than the bound all return "", which leaves exactly the unsafe result
    the empty payload produces today. Uniqueness counts distinct names, not
    inodes, so a planted decoy or a second hard link can only refuse authority;
    it can never redirect it at a real rollout's expense.
    """
    exact = valid_uuid(session_id)
    if not exact:
        return ""
    pattern = re.compile(
        r"\Arollout-" + CODEX_ROLLOUT_TIMESTAMP + "-" + re.escape(exact) + r"\.jsonl\Z"
    )
    root = _codex_sessions_root()
    if root.is_symlink():
        return ""
    found: list[str] = []
    budget = CODEX_ROLLOUT_MAX_ENTRIES
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    budget -= 1
                    if budget <= 0:
                        return ""
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < CODEX_ROLLOUT_MAX_DEPTH:
                            pending.append((Path(entry.path), depth + 1))
                    elif entry.is_file(follow_symlinks=False) and pattern.fullmatch(
                        entry.name
                    ):
                        found.append(entry.path)
                        if len(found) > 1:
                            return ""
        except OSError:
            continue
    return found[0] if len(found) == 1 else ""


def _transcript_path(
    provider: str, value: object, state_dir: Path
) -> tuple[str, bool, Mapping[str, Any]]:
    if value in (None, ""):
        return "", False, {}
    if not isinstance(value, str) or "\x00" in value:
        return "", False, {}
    try:
        if len(value.encode("utf-8")) > MAX_TRANSCRIPT_PATH_BYTES:
            return "", False, {}
    except UnicodeError:
        return "", False, {}
    path = Path(value)
    if not path.is_absolute() or any(ord(char) < 32 for char in value):
        return "", False, {}
    # Codex owns the file it writes and writes it 0644; Claude writes 0600 and
    # keeps the strict mask. Ownership is exact for both: a file this uid does
    # not own is foreign evidence no matter what its mode bits say.
    mask = PROVIDER_TRANSCRIPT_MODE_MASK if provider == "codex" else PRIVATE_MODE_MASK
    home = Path(os.environ.get("HOME", os.fspath(Path.home()))).resolve()
    test_root = (state_dir / "test-transcripts").resolve()
    roots = [test_root]
    if provider == "claude":
        roots.append((home / ".claude" / "projects").resolve())
    elif provider == "codex":
        roots.append(
            Path(os.environ.get("CODEX_HOME", os.fspath(home / ".codex"))).resolve()
            / "sessions"
        )
    try:
        canonical_parent = path.parent.resolve(strict=True)
    except OSError:
        return "", False, {}
    canonical = canonical_parent / path.name
    if not any(_inside(canonical, root) for root in roots):
        return "", False, {}
    try:
        info = canonical.lstat()
    except FileNotFoundError:
        return os.fspath(canonical), provider == "codex", {}
    except OSError:
        return "", False, {}
    if (
        canonical.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & mask
    ):
        return "", False, {}
    try:
        raw, tail_start, _size, opened = _read_private_tail(
            canonical, maximum=MAX_TRANSCRIPT_BYTES, mask=mask
        )
    except (OSError, SourceAuthorityError):
        return "", False, {}
    final_newline = raw.rfind(b"\n")
    if final_newline < 0:
        complete_size = 0
        last = b""
    else:
        complete_size = tail_start + final_newline + 1
        prior_newline = raw.rfind(b"\n", 0, final_newline)
        if prior_newline < 0 and tail_start:
            if provider == "claude":
                return os.fspath(canonical), False, {}
            last = b""
        else:
            last = raw[prior_newline + 1 : final_newline]
    anchor = {
        "canonical_path": os.fspath(canonical),
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "pre_submit_size": complete_size,
        "last_complete_sha256": hashlib.sha256(last).hexdigest(),
    }
    return os.fspath(canonical), True, anchor


def _submission_key(
    provider: str,
    turn_id: object,
    transcript_anchor: Mapping[str, Any],
    prompt_digest: str,
) -> tuple[str, bool]:
    if provider == "codex":
        exact, valid = _turn(turn_id)
        return (exact, True) if valid else ("untrusted-prompt:" + prompt_digest, False)
    if not transcript_anchor:
        # The event still needs a collision-free identity so separate untrusted
        # prompts can reach intake. This key never grants authority.
        return "untrusted-prompt:" + prompt_digest, False
    encoded = _canonical(transcript_anchor)
    return "claude-anchor:" + hashlib.sha256(encoded).hexdigest(), True


class SourceEventStore:
    """Atomic event objects plus an append-only, hash-chained audit ledger."""

    def __init__(
        self, state_dir: Path | str, *, segment_bytes: int = 256 * 1024
    ) -> None:
        self.state_dir = Path(state_dir)
        self.root = self.state_dir / "supervisor" / "source-events"
        self.entries = self.root / "entries"
        self.tuples = self.root / "tuples"
        self.verifications = self.root / "verifications"
        self.lock_path = self.root / "source-events.lock"
        self.ledger = SegmentedLedger(self.root, segment_bytes=segment_bytes)
        # Compatibility for diagnostics that name the first compact segment.
        self.ledger_path = self.root / "segments" / "00000001.jsonl"

    def ensure(self) -> None:
        _private_directory(self.root.parent)
        _private_directory(self.root)
        for directory in (self.entries, self.tuples, self.verifications):
            _private_directory(directory)
        self.ledger.ensure()

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.ensure()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise SourceAuthorityError("source-event lock is not owner-private")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def event_path(self, event_id: object) -> Path:
        if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
            raise SourceAuthorityError("a source event needs an exact lowercase SHA-256 id")
        return self.entries / f"{event_id}.json"

    def read(self, event_id: object) -> dict[str, Any]:
        path = self.event_path(event_id)
        try:
            raw = _read_private(path, maximum=MAX_EVENT_BYTES)
        except SourceAuthorityError:
            if not path.exists() and not path.is_symlink():
                raise SourceAuthorityError(f"no source event {event_id}") from None
            raise
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise SourceAuthorityError("source event is not valid JSON") from exc
        if not isinstance(value, Mapping):
            raise SourceAuthorityError("source event is not an object")
        event = dict(value)
        self._validate_event(event)
        return event

    def _validate_event(self, event: Mapping[str, Any]) -> None:
        provider = event.get("provider")
        session_id = valid_uuid(event.get("session_id"))
        submission_key = event.get("submission_key")
        digest = event.get("prompt_sha256")
        raw_prompt = event.get("raw_prompt")
        if event.get("schema") != 2 or provider not in ("claude", "codex") or not session_id:
            raise SourceAuthorityError("source event has no exact provider session")
        if not isinstance(submission_key, str) or not isinstance(raw_prompt, str):
            raise SourceAuthorityError("source event has malformed prompt identity")
        if not isinstance(digest, str) or prompt_sha256(raw_prompt) != digest:
            raise SourceAuthorityError("source event raw prompt digest mismatch")
        if provider == "claude" and not submission_key.startswith(
            ("claude-anchor:", "untrusted-prompt:")
        ):
            raise SourceAuthorityError("Claude source event has no stable submission identity")
        expected_id = source_event_id(provider, session_id, submission_key, digest)
        if event.get("event_id") != expected_id:
            raise SourceAuthorityError("source event id mismatch")
        unsigned = dict(event)
        event_hash = unsigned.pop("event_hash", None)
        if not isinstance(event_hash, str) or hashlib.sha256(_canonical(unsigned)).hexdigest() != event_hash:
            raise SourceAuthorityError("source event chain hash mismatch")

    def _pointer_path(self, tuple_id: str) -> Path:
        return self.tuples / f"{tuple_id}.json"

    def _read_pointer(self, tuple_id: str) -> Mapping[str, Any] | None:
        path = self._pointer_path(tuple_id)
        if not path.exists() and not path.is_symlink():
            return None
        raw = _read_private(path, maximum=1024)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise SourceAuthorityError("source-event tuple pointer is malformed") from exc
        if not isinstance(value, Mapping):
            raise SourceAuthorityError("source-event tuple pointer is malformed")
        return value

    def _complete_transaction(self, transaction: Mapping[str, Any]) -> dict[str, Any]:
        event = dict(transaction["event"])
        tuple_id = str(transaction["tuple_id"])
        path = self.event_path(event["event_id"])
        payload = (
            json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            + b"\n"
        )
        if path.exists() or path.is_symlink():
            existing = self.read(event["event_id"])
            if existing != event:
                raise SourceAuthorityError("source event WAL conflicts with event object")
        else:
            _atomic_private_write(path, payload)
        self.ledger.append(event)
        pointer = self._read_pointer(tuple_id)
        expected_pointer = {
            "event_id": event["event_id"],
            "prompt_sha256": event["prompt_sha256"],
        }
        if pointer is not None and dict(pointer) != expected_pointer:
            raise SourceAuthorityError("source event WAL conflicts with tuple pointer")
        if pointer is None:
            _atomic_private_write(
                self._pointer_path(tuple_id), _canonical(expected_pointer) + b"\n"
            )
        self.ledger.finish(str(event["event_id"]))
        return event

    def recover(self) -> None:
        for transaction in self.ledger.pending():
            self._complete_transaction(transaction)

    def capture(
        self,
        *,
        provider: str,
        session_id: str,
        turn_id: object,
        raw_prompt: object,
        transcript_path: object = None,
        clock: Callable[[], float] = time.time,
    ) -> dict[str, Any]:
        exact_session = valid_uuid(session_id)
        if provider not in ("claude", "codex") or not exact_session:
            raise SourceAuthorityError("source hook has no exact provider root session UUID")
        if not isinstance(raw_prompt, str):
            raise SourceAuthorityError("source hook prompt must be a string")
        raw_bytes = raw_prompt.encode("utf-8")
        if not raw_bytes or len(raw_bytes) > MAX_RAW_PROMPT_BYTES:
            raise SourceAuthorityError("source hook prompt is empty or exceeds its byte limit")
        if provider == "codex" and transcript_path in (None, ""):
            # Codex hands the hook no transcript_path at all. Without this the
            # provider could never reach transcript-verified authority, so the
            # rollout its own session id names stands in — and only when it is
            # unmistakable. `_transcript_path` still applies every ownership,
            # containment, and symlink check to whatever comes back.
            transcript_path = _codex_rollout_path(exact_session)
        safe_transcript, transcript_safe, transcript_anchor = _transcript_path(
            provider, transcript_path, self.state_dir
        )
        digest = hashlib.sha256(raw_bytes).hexdigest()
        submission_key, valid_submission = _submission_key(
            provider, turn_id, transcript_anchor, digest
        )
        exact_turn, _valid_turn = _turn(turn_id)
        event_id = source_event_id(provider, exact_session, submission_key, digest)
        tuple_id = _tuple_id(provider, exact_session, submission_key)
        with self.locked():
            try:
                self.recover()
            except LedgerError as exc:
                raise SourceAuthorityError(str(exc)) from exc
            pointer = self._read_pointer(tuple_id)
            if pointer is not None:
                if pointer.get("prompt_sha256") != digest:
                    raise SourceAuthorityError(
                        "source session/turn tuple was reused with different prompt bytes"
                    )
                existing = self.read(pointer.get("event_id"))
                self.ledger.verify_event(existing["event_id"], existing["event_hash"])
                return existing
            previous = str(self.ledger.scan()["head_event_hash"])
            direct_source = _direct_source_prompt(raw_prompt)
            capable = bool(valid_submission and transcript_safe and direct_source)
            event: dict[str, Any] = {
                "schema": 2,
                "event_id": event_id,
                "provider": provider,
                "session_id": exact_session,
                "turn_id": exact_turn,
                "submission_key": submission_key,
                "prompt_sha256": digest,
                "raw_prompt": raw_prompt,
                "display_summary": _display(raw_prompt),
                "transcript_path": safe_transcript,
                "transcript_anchor": transcript_anchor,
                "authority_capable": capable,
                "authority_limit_reason": (
                    "" if capable else
                    "missing or malformed provider submission key" if not valid_submission else
                    "unsafe transcript_path" if not transcript_safe else
                    "machine/operator envelope is not direct source authority"
                ),
                "recorded_unix_ms": _now_ms(clock),
                "previous_event_hash": previous,
            }
            event["event_hash"] = hashlib.sha256(_canonical(event)).hexdigest()
            payload = json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
            if len(payload) > MAX_EVENT_BYTES:
                raise SourceAuthorityError("source event exceeds its storage limit")
            try:
                self.ledger.begin(event, tuple_id)
                return self._complete_transaction(
                    {"schema_version": 2, "tuple_id": tuple_id, "event": event}
                )
            except LedgerError as exc:
                raise SourceAuthorityError(str(exc)) from exc

    def mark_verification(
        self, event_id: str, *, basis: str, clock: Callable[[], float]
    ) -> None:
        payload = {
            "event_id": event_id,
            "basis": basis,
            "non_cryptographic": basis == "hook-ledger",
            "verified_unix_ms": _now_ms(clock),
        }
        _atomic_private_write(
            self.verifications / f"{event_id}.json", _canonical(payload) + b"\n"
        )


class AuthorityRequestStore:
    """Immutable exact-action requests that a later source event may approve."""

    def __init__(self, state_dir: Path | str) -> None:
        self.root = (
            Path(state_dir) / "supervisor" / "source-events" / "authority-requests"
        )

    def ensure(self) -> None:
        _private_directory(self.root.parent.parent)
        _private_directory(self.root.parent)
        _private_directory(self.root)

    def path(self, request_id: object) -> Path:
        if not isinstance(request_id, str) or not EVENT_ID_RE.fullmatch(request_id):
            raise SourceAuthorityError("authority request needs an exact SHA-256 id")
        return self.root / f"{request_id}.json"

    def create(
        self,
        *,
        target: str,
        text: str,
        category: str,
        scope: str,
        source_thread_key: str,
        ttl_seconds: int = 600,
        clock: Callable[[], float] = time.time,
    ) -> dict[str, Any]:
        thread = valid_thread_key(source_thread_key)
        if not thread or not all(
            isinstance(value, str) and value.strip()
            for value in (target, text, category, scope)
        ):
            raise SourceAuthorityError("authority request needs exact action fields")
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= 900:
            raise SourceAuthorityError("authority request TTL must be 1-900 seconds")
        self.ensure()
        created = _now_ms(clock)
        unsigned = {
            "schema_version": 1,
            "action": "send_message",
            "target": target,
            "category": category,
            "payload_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "authority_scope": scope.strip(),
            "source_thread_key": thread,
            "created_unix_ms": created,
            "expires_unix_ms": created + ttl_seconds * 1000,
            "nonce": os.urandom(16).hex(),
        }
        request_id = hashlib.sha256(_canonical(unsigned)).hexdigest()
        record = {**unsigned, "request_id": request_id}
        _atomic_private_write(self.path(request_id), _canonical(record) + b"\n")
        return {
            **record,
            "confirmation_token": f"[authority_request:{request_id}]",
        }

    def read(self, request_id: object) -> dict[str, Any]:
        path = self.path(request_id)
        try:
            value = json.loads(_read_private(path, maximum=8192))
        except (OSError, UnicodeError, ValueError) as exc:
            raise SourceAuthorityError("authority request is missing or malformed") from exc
        if not isinstance(value, Mapping):
            raise SourceAuthorityError("authority request is malformed")
        record = dict(value)
        claimed = record.pop("request_id", None)
        if claimed != request_id or hashlib.sha256(_canonical(record)).hexdigest() != request_id:
            raise SourceAuthorityError("authority request immutable digest mismatch")
        record["request_id"] = claimed
        return record

    def verify_send(
        self,
        request_id: object,
        *,
        verification: Mapping[str, Any],
        target: str,
        text: str,
        category: str,
        scope: str,
        clock: Callable[[], float] = time.time,
    ) -> dict[str, Any]:
        request = self.read(request_id)
        expected = {
            "target": target,
            "category": category,
            "payload_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "authority_scope": scope.strip(),
        }
        for key, value in expected.items():
            if request.get(key) != value:
                raise SourceAuthorityError(f"authority request {key} does not match the send")
        if request.get("expires_unix_ms", 0) <= _now_ms(clock):
            raise SourceAuthorityError("authority request expired before approval")
        if verification.get("source_thread_key") != request.get("source_thread_key"):
            raise SourceAuthorityError("authority reply came from the wrong source thread")
        token = f"[authority_request:{request_id}]"
        if token not in str(verification.get("authority_text") or ""):
            raise SourceAuthorityError("authority reply does not reference the exact request")
        return request


def capture_hook_event(
    payload: Mapping[str, Any],
    *,
    state_dir: Path | str,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SourceAuthorityError("source hook payload is not an object")
    for marker in SIDECHAIN_MARKERS:
        value = payload.get(marker)
        if value is True or (isinstance(value, str) and value.strip()):
            raise SourceAuthorityError("source authority is root-session only")
    provider = payload.get("provider")
    session = payload.get("session_id") or payload.get("sessionId")
    return SourceEventStore(state_dir).capture(
        provider=provider if isinstance(provider, str) else "",
        session_id=session if isinstance(session, str) else "",
        turn_id=payload.get("turn_id") or payload.get("turnId"),
        raw_prompt=payload.get("prompt"),
        transcript_path=payload.get("transcript_path") or payload.get("transcriptPath"),
        clock=clock,
    )


def write_acceptance_marker(
    event: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Acknowledge one W2 headless intake only after the hook captured it.

    The launcher supplies an absolute destination inside its owner-private
    spool and the expected raw-prompt digest.  Absence means an ordinary
    interactive prompt and writes nothing.  A configured mismatch fails
    closed, leaving the launcher's item available for one retry.  Atomic
    replacement plus fsync means a marker survives an agent crash immediately
    after this hook returns; process exit is not the acceptance signal.
    """
    raw_path = environ.get(ACCEPTANCE_PATH_ENV, "")
    expected = environ.get(ACCEPTANCE_DIGEST_ENV, "")
    if not raw_path and not expected:
        return {"configured": False, "accepted": False}
    if not raw_path or not EVENT_ID_RE.fullmatch(expected):
        raise SourceAuthorityError("source acceptance path and digest must both be exact")
    path = Path(raw_path)
    if not path.is_absolute() or "\x00" in raw_path:
        raise SourceAuthorityError("source acceptance path must be absolute")
    parent = path.parent
    info = parent.lstat()
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SourceAuthorityError("source acceptance parent must be owner-private")
    if event.get("prompt_sha256") != expected:
        raise SourceAuthorityError("source acceptance prompt digest mismatch")
    if event.get("authority_capable") is not True:
        raise SourceAuthorityError("source acceptance needs exact session and turn identity")
    payload = {
        "schema": 1,
        "accepted": True,
        "event_id": event.get("event_id"),
        "provider": event.get("provider"),
        "session_id": event.get("session_id"),
        "turn_id": event.get("turn_id"),
        "prompt_sha256": expected,
        "accepted_unix_ms": _now_ms(clock),
    }
    if path.exists() or path.is_symlink():
        raw = _read_private(path, maximum=4096)
        try:
            existing = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise SourceAuthorityError("source acceptance marker is malformed") from exc
        if not isinstance(existing, Mapping) or any(
            existing.get(key) != payload.get(key)
            for key in ("event_id", "provider", "session_id", "turn_id", "prompt_sha256")
        ):
            raise SourceAuthorityError("source acceptance marker collision")
        return {"configured": True, "accepted": True, "event_id": event.get("event_id")}
    _atomic_private_write(path, _canonical(payload) + b"\n")
    return {"configured": True, "accepted": True, "event_id": event.get("event_id")}


def _intake_commit_path(environ: Mapping[str, str]) -> str:
    explicit = environ.get(INTAKE_COMMIT_PATH_ENV, "")
    if explicit:
        return explicit
    handoff = environ.get("SESSION_KIT_PROMPT_HANDOFF", "")
    if handoff:
        return f"{handoff}.intake_committed"
    accepted = environ.get("SESSION_KIT_PROMPT_HANDOFF_ACCEPTANCE", "")
    if accepted.endswith(".accepted"):
        return accepted[: -len(".accepted")] + ".intake_committed"
    source = environ.get(ACCEPTANCE_PATH_ENV, "")
    if source.endswith(".accepted"):
        return source[: -len(".accepted")] + ".intake_committed"
    return ""


def write_intake_commit_marker(
    event: Mapping[str, Any],
    *,
    intake_msg_id: str,
    requirements_revision: int,
    requirements_digest: str,
    environ: Mapping[str, str],
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Publish v2 acceptance only after the fsynced intake commit exists."""
    raw_path = _intake_commit_path(environ)
    expected = environ.get(ACCEPTANCE_DIGEST_ENV) or environ.get(
        "SESSION_KIT_PROMPT_HANDOFF_SHA256", ""
    )
    if not raw_path and not expected:
        return {"configured": False, "intake_committed": False}
    generation = environ.get(MANAGED_GENERATION_ENV) or environ.get(
        "SESSION_KIT_SESSION_GENERATION", ""
    )
    if (
        not raw_path
        or not EVENT_ID_RE.fullmatch(expected)
        or not re.fullmatch(r"[0-9a-f]{8}", intake_msg_id)
        or not isinstance(requirements_revision, int)
        or isinstance(requirements_revision, bool)
        or requirements_revision < 0
        or not EVENT_ID_RE.fullmatch(requirements_digest)
        or not isinstance(generation, str)
        or not generation
        or len(generation.encode("utf-8")) > 300
    ):
        raise SourceAuthorityError("intake commit marker inputs are incomplete or malformed")
    path = Path(raw_path)
    if not path.is_absolute() or "\x00" in raw_path:
        raise SourceAuthorityError("intake commit marker path must be absolute")
    parent = path.parent
    info = parent.lstat()
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SourceAuthorityError("intake commit marker parent must be owner-private")
    if event.get("prompt_sha256") != expected:
        raise SourceAuthorityError("intake commit prompt digest mismatch")
    raw_prompt = event.get("raw_prompt")
    if not isinstance(raw_prompt, str):
        raise SourceAuthorityError("intake commit source prompt is malformed")
    payload = {
        "schema_version": 2,
        "status": "intake_committed",
        "provider": event.get("provider"),
        "session_id": event.get("session_id"),
        "submission_key": event.get("submission_key"),
        "prompt_sha256": expected,
        "bytes": len(raw_prompt.encode("utf-8")),
        "source_event_id": event.get("event_id"),
        "intake_msg_id": intake_msg_id,
        "requirements_revision": requirements_revision,
        "requirements_digest": requirements_digest,
        "managed_generation": generation,
        "committed_unix_ms": _now_ms(clock),
    }
    identity_fields = tuple(key for key in payload if key != "committed_unix_ms")
    if path.exists() or path.is_symlink():
        try:
            existing = json.loads(_read_private(path, maximum=8192))
        except (UnicodeError, ValueError) as exc:
            raise SourceAuthorityError("intake commit marker is malformed") from exc
        if not isinstance(existing, Mapping) or any(
            existing.get(key) != payload[key] for key in identity_fields
        ):
            raise SourceAuthorityError("intake commit marker collision")
        return {
            "configured": True,
            "intake_committed": True,
            "source_event_id": event.get("event_id"),
            "intake_msg_id": intake_msg_id,
        }
    _atomic_private_write(path, _canonical(payload) + b"\n")
    return {
        "configured": True,
        "intake_committed": True,
        "source_event_id": event.get("event_id"),
        "intake_msg_id": intake_msg_id,
    }


def acceptance_status(
    path: Path | str,
    expected_digest: str,
    *,
    process_exit_code: int | None = None,
) -> dict[str, Any]:
    """Launcher-side interpretation of the exact hook marker.

    This is intentionally read-only.  Missing or mismatched evidence retains
    the item for one retry; a zero process exit without evidence is named as an
    anomaly because completion is not acceptance.
    """
    marker = Path(path)
    accepted = False
    event_id = ""
    reason = "acceptance marker is absent"
    if not EVENT_ID_RE.fullmatch(expected_digest):
        reason = "expected prompt digest is malformed"
    elif marker.exists() or marker.is_symlink():
        try:
            parent = marker.parent
            parent_info = parent.lstat()
            if (
                parent.is_symlink()
                or not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != os.geteuid()
                or stat.S_IMODE(parent_info.st_mode) != 0o700
            ):
                raise SourceAuthorityError(
                    "source acceptance parent is not owner-private"
                )
            raw = _read_private(marker, maximum=4096)
            value = json.loads(raw.decode("utf-8"))
            if (
                isinstance(value, Mapping)
                and value.get("accepted") is True
                and value.get("prompt_sha256") == expected_digest
                and isinstance(value.get("event_id"), str)
                and EVENT_ID_RE.fullmatch(str(value["event_id"]))
            ):
                accepted = True
                event_id = str(value["event_id"])
                reason = "hook acceptance is durable"
            else:
                reason = "acceptance marker does not match the intake"
        except (OSError, UnicodeError, ValueError, SourceAuthorityError):
            reason = "acceptance marker is unreadable or unsafe"
    return {
        "accepted": accepted,
        "event_id": event_id,
        "retain_for_retry": not accepted,
        "anomaly": process_exit_code == 0 and not accepted,
        "reason": reason,
    }


def _strings(value: object) -> Sequence[str]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                result.append(str(item["text"]))
        return tuple(result)
    return ()


def _records(raw: bytes) -> tuple[list[Mapping[str, Any]], bool]:
    complete = raw.endswith(b"\n")
    complete_raw = raw if complete else raw.rpartition(b"\n")[0]
    lines = complete_raw.splitlines()
    records: list[Mapping[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise SourceAuthorityError("provider transcript has malformed complete JSONL") from exc
        if isinstance(value, Mapping):
            records.append(value)
    return records, not complete


def _session_claims(record: Mapping[str, Any]) -> set[str]:
    claims: set[str] = set()
    for container in (record, record.get("payload")):
        if not isinstance(container, Mapping):
            continue
        for key in (
            "id", "session_id", "sessionId", "thread_id", "threadId", "conversation_id"
        ):
            exact = valid_uuid(container.get(key))
            if exact:
                claims.add(exact)
    return claims


def _user_texts(record: Mapping[str, Any]) -> list[str]:
    texts: list[str] = []
    root_kind = str(record.get("type") or "").casefold()
    containers = [record]
    if isinstance(record.get("payload"), Mapping):
        containers.append(record["payload"])
    for container in containers:
        kind = str(container.get("type") or "").casefold()
        role = str(container.get("role") or "").casefold()
        message = container.get("message")
        if kind in {"user", "user_message"} or role == "user" or root_kind == "user_message":
            texts.extend(_strings(container.get("content")))
            texts.extend(_strings(container.get("text")))
            texts.extend(_strings(message))
        if isinstance(message, Mapping) and message.get("role") == "user":
            texts.extend(_strings(message.get("content")))
            texts.extend(_strings(message.get("text")))
    return texts


def _verify_codex_transcript(
    event: Mapping[str, Any], head: bytes, tail: bytes
) -> str:
    target_session = str(event["session_id"])
    target_turn = str(event["submission_key"])
    head_records, _head_partial = _records(head)
    saw_session_meta = False
    for record in head_records:
        kind = str(record.get("type") or "").casefold()
        payload = record.get("payload")
        payload_kind = (
            str(payload.get("type") or "").casefold()
            if isinstance(payload, Mapping)
            else ""
        )
        if kind != "session_meta" and payload_kind != "session_meta":
            continue
        saw_session_meta = True
        claims = _session_claims(record)
        if claims != {target_session}:
            raise SourceAuthorityError("Codex transcript source session mismatch")
    if not saw_session_meta:
        raise SourceAuthorityError("Codex transcript has no exact session_meta corroboration")

    records, partial = _records(tail)
    active_turn = ""
    saw_target_turn = False
    saw_user_for_turn = False
    for record in records:
        kind = str(record.get("type") or "").casefold()
        payload = record.get("payload")
        payload_kind = (
            str(payload.get("type") or "").casefold()
            if isinstance(payload, Mapping)
            else ""
        )
        claims = _session_claims(record)
        if kind == "session_meta" or payload_kind == "session_meta":
            if claims != {target_session}:
                raise SourceAuthorityError("Codex transcript source session mismatch")
        turn = ""
        for container in (record, payload):
            if not isinstance(container, Mapping):
                continue
            candidate = container.get("turn_id") or container.get("turnId")
            if isinstance(candidate, str) and candidate:
                turn = candidate
        if payload_kind == "task_started" or kind == "task_started" or turn:
            active_turn = turn or active_turn
            saw_target_turn = saw_target_turn or active_turn == target_turn
        texts = _user_texts(record)
        record_turn = turn or active_turn
        if texts and record_turn == target_turn:
            saw_user_for_turn = True
            if claims and claims != {target_session}:
                raise SourceAuthorityError("Codex transcript source session mismatch")
            if any(prompt_sha256(text) == event["prompt_sha256"] for text in texts):
                return "verified"
            raise SourceAuthorityError("Codex transcript prompt digest mismatch")
    if records and not saw_target_turn and not partial:
        raise SourceAuthorityError("Codex transcript source turn mismatch")
    if saw_user_for_turn:
        raise SourceAuthorityError("Codex transcript prompt digest mismatch")
    return "lag"


def _verify_claude_transcript(
    event: Mapping[str, Any], path: Path, raw: bytes, total_size: int
) -> str:
    anchor = event.get("transcript_anchor")
    if not isinstance(anchor, Mapping):
        raise SourceAuthorityError("Claude source event has no stable transcript anchor")
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or info.st_dev != anchor.get("device")
        or info.st_ino != anchor.get("inode")
    ):
        raise SourceAuthorityError("Claude transcript identity changed after submission")
    offset = anchor.get("pre_submit_size")
    if not isinstance(offset, int) or offset < 0 or total_size < offset:
        raise SourceAuthorityError("Claude transcript was truncated after submission")
    prefix_start = max(0, offset - MAX_TRANSCRIPT_BYTES)
    prefix, _current_size, _opened = _read_private_region(
        path, offset=prefix_start, maximum=offset - prefix_start
    )
    complete_prefix = prefix.rstrip(b"\n")
    prior = complete_prefix.rfind(b"\n")
    if prior < 0 and prefix_start and complete_prefix:
        raise SourceAuthorityError("Claude transcript anchor record exceeds its bound")
    last = complete_prefix[prior + 1 :] if complete_prefix else b""
    if hashlib.sha256(last).hexdigest() != anchor.get("last_complete_sha256"):
        raise SourceAuthorityError("Claude transcript pre-submit anchor mismatch")
    records, _partial = _records(raw)
    saw_user = False
    for record in records:
        texts = _user_texts(record)
        if not texts:
            continue
        claims = _session_claims(record)
        if claims and event["session_id"] not in claims:
            raise SourceAuthorityError("Claude transcript source session mismatch")
        for text in texts:
            if prompt_sha256(text) == event["prompt_sha256"]:
                return "verified"
            # Claude writes this provider-owned continuation record when a
            # managed TUI resumes. It can land after the pre-submit anchor but
            # before the actual UserPromptSubmit record; it is not another
            # human prompt and must not break the anchor-to-prompt scan.
            if text == "Continue from where you left off.":
                continue
            saw_user = True
            raise SourceAuthorityError("Claude transcript prompt digest mismatch")
    return "lag" if not saw_user else "lag"


def _verify_transcript(event: Mapping[str, Any], path: Path) -> str:
    if event.get("provider") == "claude":
        anchor = event.get("transcript_anchor")
        offset = anchor.get("pre_submit_size") if isinstance(anchor, Mapping) else -1
        if not isinstance(offset, int) or offset < 0:
            raise SourceAuthorityError("Claude source event has no stable transcript anchor")
        raw, size, _info = _read_private_region(
            path, offset=offset, maximum=MAX_TRANSCRIPT_BYTES
        )
        return _verify_claude_transcript(event, path, raw, size)
    head, raw, tail_start = _read_private_codex_windows(path)
    if tail_start:
        _discarded, separator, raw = raw.partition(b"\n")
        if not separator:
            return "lag"
    return _verify_codex_transcript(event, head, raw)


def verify_source_event(
    state_dir: Path | str,
    event_id: object,
    *,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Verify one event, preferring the provider transcript when available."""
    store = SourceEventStore(state_dir)
    try:
        event = store.read(event_id)
        with store.locked():
            store.recover()
            store.ledger.verify_event(event["event_id"], event["event_hash"])
        if event.get("authority_capable") is not True:
            raise SourceAuthorityError(
                str(event.get("authority_limit_reason") or "source event is not authority-capable")
            )
        # Defense in depth, and the only screen that reaches events already on
        # disk: an envelope captured before this check existed, or under an
        # older prefix list, must not certify now just because its stored flag
        # says capable.
        if not _direct_source_prompt(str(event.get("raw_prompt") or "")):
            raise SourceAuthorityError(
                "machine/operator envelope is not direct source authority"
            )
        transcript = str(event.get("transcript_path") or "")
        basis = "hook-ledger"
        if transcript:
            path = Path(transcript)
            try:
                path.lstat()
            except FileNotFoundError:
                pass
            else:
                result = _verify_transcript(event, path)
                if result == "verified":
                    basis = "transcript"
        with store.locked():
            store.mark_verification(str(event["event_id"]), basis=basis, clock=clock)
        return {
            "verified": True,
            "event_id": event["event_id"],
            "provider": event["provider"],
            "source_thread_key": f"{event['provider']}:{event['session_id']}",
            "turn_id": event["turn_id"],
            "submission_key": event["submission_key"],
            "prompt_sha256": event["prompt_sha256"],
            "authority_text": event["raw_prompt"],
            "display_summary": event["display_summary"],
            "basis": basis,
            "transcript_verified": basis == "transcript",
            "non_cryptographic": basis == "hook-ledger",
        }
    except (OSError, LedgerError, SourceAuthorityError) as exc:
        return {
            "verified": False,
            "event_id": event_id if isinstance(event_id, str) else "",
            "basis": "none",
            "transcript_verified": False,
            "non_cryptographic": False,
            "reason": str(exc),
        }
