"""Say when anything the "needs you" column is made of has changed.

The daemon's event stream carries exactly four things -- a session created,
attached, detached, removed -- and a session that stops working and starts
WAITING FOR A PERSON does none of them. Measured on a live estate: 25 seconds of
`shpool events`, eleven live sessions with agents mid-turn, zero lines. So the
stream cannot be the reason to poll less often, because the one column an
operator actually watches would get slower, not faster.

This is the missing half. It watches the small set of files the needs-you
answer is derived from, and prints one line when any of them moves. The picker
reads those lines exactly as it reads daemon events: collect now. With both
streams live, every source of an attention change pushes, and the timed poll is
free to widen without the picker ever learning about a waiting session later
than it used to.

What it watches, and why each one is here:

  * ``<state>/attention/claude/`` -- the Notification hook's records: a Claude
    session raising its hand writes here at the moment it happens;
  * every Claude session record (``~/.claude/sessions/<pid>.json`` and the same
    under each account profile below ``~/.local/share/session-kit/accounts/``,
    which is where most sessions on a multi-account estate live) --
    ``statusUpdatedAt`` moves on a status transition, which is what the poll
    reads;
  * Codex rollouts -- a Codex session waiting for approval shows up as an
    appended rollout record and nothing else; there is no hook and no event;
  * the fleet's ``stalls.json`` -- the other feed the picker's attention count
    reads.

Everything is bounded, and that now includes every directory this READS, not
only the set it fingerprints: a node cap on the rollout walk, a cap on how many
entries any one directory listing may look at, a cap on how many files are
fingerprinted, and stat-only reads (nothing here opens a file). The listing cap
is the one that was missing -- ``sorted(dir.glob(...))[:cap]`` bounds what is
watched while still reading the whole directory, and a session directory grown
to 60 000 entries cost 0.5-0.8 s of a 1 s loop. One pass is a few
hundred stats, which is why it can afford to run every second while the
expensive thing -- a whole status collection -- runs when there is a reason.

It also has to END with the picker that started it. The picker's trap stops it
on every ordinary exit, but a SIGKILLed picker runs no trap, and stdout here is
the picker's event file -- a regular file, so there is no reader to go away and
no BrokenPipeError to notice. ``--parent-pid`` is the answer: the launcher
hands over its own pid and this loop stops the moment that process is gone.

Usage: ``python3 pulse.py [--interval 1.0] [--parent-pid N] [--once]``; one
JSON line per change on stdout, flushed. Kill switch: the picker starts it only
when SESSION_KIT_PICKER_PULSE is not 0/off/no/false, in any capitalisation and
with whitespace around it.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Iterator, Mapping


# A pass must stay cheap enough to run every second on a busy estate, and must
# never wander: a stalled or enormous tree is a reason to stop looking, not to
# hold the loop open.
MAX_NODES = 4000
MAX_TRACKED = 1500
MAX_DEPTH = 6
# The session directories fill with kit markers next to the records (315
# .nameintent, 196 .colorset, 182 .titleset here today), so a listing must not
# sort the whole directory -- but it MUST look at every entry, because scandir
# order is name-hash order and the records the watcher exists for can land
# anywhere in it. An entries-examined cap here once made the watcher blind to
# every session record past ~4 000 entries while the poll stayed widened on
# its word; looking at everything and keeping only matches costs ~13 ms at
# 60 000 entries against the 1 s loop, and the `keep` bound still caps what
# is fingerprinted.
# Account profiles are directories too, and the same rule applies to them.
MAX_PROFILES = 64
DEFAULT_INTERVAL_SECONDS = 1.0


def _state_dir(environ: Mapping[str, str], home: Path) -> Path:
    configured = environ.get("SESSION_KIT_STATE_DIR")
    if configured:
        return Path(configured)
    state_home = environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else home / ".local" / "state"
    return base / "session-kit"


def _stamp(path: Path) -> str:
    try:
        info = os.stat(path)
    except OSError:
        return ""
    return f"{int(info.st_mtime_ns)}:{info.st_size}"


def _bounded_walk(root: Path, suffix: str, budget: list[int]) -> Iterator[Path]:
    """Files under root ending in suffix, with a hard node budget."""
    stack = [(root, 0)]
    while stack and budget[0] > 0:
        directory, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    budget[0] -= 1
                    if budget[0] <= 0:
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append((Path(entry.path), depth + 1))
                        elif entry.name.endswith(suffix):
                            yield Path(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue


def _bounded_listing(
    directory: Path, suffix: str, *, keep: int = MAX_TRACKED
) -> list[Path]:
    """Files ending in suffix; every entry examined, at most `keep` kept.

    ``sorted(directory.glob(...))[:keep]`` reads and sorts the WHOLE directory
    before the slice throws most of it away; scandir with a keep-bound avoids
    the sort without ever skipping an entry, so a record can never be missed
    because of where its filename hashes in the listing order.
    """
    found: list[Path] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if len(found) >= keep:
                    break
                if entry.name.endswith(suffix):
                    found.append(Path(entry.path))
    except OSError:
        return sorted(found)
    return sorted(found)


def watched_paths(environ: Mapping[str, str], home: Path) -> list[Path]:
    """Every file whose movement can change what needs a person."""
    state = _state_dir(environ, home)
    paths: list[Path] = []
    budget = [MAX_NODES]

    attention = state / "attention" / "claude"
    paths.append(attention)
    paths.extend(sorted(_bounded_walk(attention, ".json", budget))[:MAX_TRACKED])

    # Every profile, not just the ambient one: on a multi-account estate nearly
    # every live Claude session is registered under an account profile, so
    # watching ~/.claude alone would have watched the one session nobody is
    # waiting on.
    session_roots = [Path(environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")]
    override = environ.get("SESSION_KIT_ACCOUNT_ROOT")
    if override:
        accounts = Path(override) / "claude"
    else:
        data_home = environ.get("XDG_DATA_HOME")
        base = Path(data_home) if data_home else home / ".local" / "share"
        accounts = base / "session-kit" / "accounts" / "claude"
    profiles: list[Path] = []
    try:
        with os.scandir(accounts) as entries:
            for entry in entries:
                if len(profiles) >= MAX_PROFILES:
                    break
                try:
                    if entry.is_dir():
                        profiles.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        profiles = []
    session_roots.extend(sorted(profiles))
    for root in session_roots:
        sessions = root / "sessions"
        paths.append(sessions)
        paths.extend(_bounded_listing(sessions, ".json"))

    codex_home = Path(environ.get("CODEX_HOME") or home / ".codex")
    paths.extend(
        sorted(_bounded_walk(codex_home / "sessions", ".jsonl", budget))[:MAX_TRACKED]
    )
    paths.append(codex_home / "session_index.jsonl")

    fleet = Path(environ.get("FLEET_STATE_DIR") or home / ".local" / "state" / "fleet")
    paths.append(fleet / "stalls.json")
    paths.append(fleet / "inbox.jsonl")
    return paths


def _parent_is_zombie(pid: int) -> bool:
    """A killed parent nobody has waited for yet is already gone.

    Linux only, and unknown counts as alive: on a box without /proc this says
    nothing and the ppid check below is what decides.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            fields = handle.read().rsplit(")", 1)[-1].split()
    except OSError:
        return False
    return bool(fields) and fields[0] == "Z"


def parent_gone(parent_pid: int | None) -> bool:
    """True once the process that started this watcher is no longer there.

    The picker stops this child from its EXIT trap, and a SIGKILLed picker
    runs no trap -- so without this check one killed picker leaves a process
    stat-walking the estate once a second forever, appending to a temp file
    nothing reaps. A CHANGED ppid is the proof, not ``getppid() == 1``: the
    orphan is adopted by whatever subreaper is closest, which on a systemd
    user session is not init.
    """
    if parent_pid is None or parent_pid <= 0:
        return os.getppid() == 1
    if os.getppid() != parent_pid:
        return True
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        # Still there, and not ours to signal. Alive is the safe reading.
        return False
    return _parent_is_zombie(parent_pid)


def fingerprint(environ: Mapping[str, str], home: Path) -> str:
    parts = [f"{path}={_stamp(path)}" for path in watched_paths(environ, home)]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pulse", description="Print a line when the attention inputs move."
    )
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL_SECONDS
    )
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help="stop as soon as this process is gone (the picker that started us)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="print the current fingerprint and exit (for tests and doctor)",
    )
    arguments = parser.parse_args(argv)
    interval = arguments.interval
    if not 0.05 <= interval <= 60:
        interval = DEFAULT_INTERVAL_SECONDS
    home = Path(os.environ.get("HOME") or Path.home())
    if arguments.once:
        print(fingerprint(os.environ, home))
        return 0
    parent_pid = arguments.parent_pid
    previous = fingerprint(os.environ, home)
    while True:
        time.sleep(interval)
        # Before the work, not after it: a watcher whose picker is gone has
        # nobody to tell, and one wasted pass is one pass too many.
        if parent_gone(parent_pid):
            return 0
        try:
            current = fingerprint(os.environ, home)
        except Exception:  # pragma: no cover - a watcher may never take the box down
            continue
        if current == previous:
            continue
        previous = current
        print(json.dumps({"type": "attention.changed"}), flush=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(0)
