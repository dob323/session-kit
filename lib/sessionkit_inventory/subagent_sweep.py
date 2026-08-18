#!/usr/bin/env python3
"""Close completed sub-agent processes that their owners never closed.

The same timer also runs an independent background-shell pass.  It recognizes
only a real bash carrying Claude's anchored snapshot-script argv shape whose
exact direct parent is a live managed provider root.  Its idle evidence is the
regular file currently bound to fd/1, snapshotted through one no-follow
descriptor with path, device, inode, size, mtime, and ctime.  A shell with any
live descendant is never eligible.  For a childless shell, fd/1 movement or a
change in that shell's own utime+stime resets the fifteen-minute clock.  A
pipe, socket, tty, missing file, unreadable candidate-local proc entry, or a
shell-owned foreground terminal is a refusal.  Delivery pins and stops the
shell through a pidfd, verifies the stopped state, re-proves zero descendants,
identity, own CPU, terminal state, and fd/1, then sends the closing signal and
attempts to resume a surviving shell.  A target that exits or is reaped before
that resume is still logged as receiving the closing signal.  Shell records are
tagged ``kind=background-shell`` in the shared decision log.

Completion means closure (operator ruling X20, 2026-08-14). A provider
sub-agent -- a Claude worker process carrying ``--parent-session-id`` -- keeps
running after its task ends so its owner can continue it; when the owner never
does, it sits for days holding memory (two 25-hour workers survived the
2026-08-13 out-of-memory incident's cleanup and had to be killed by hand).

Self-reported state is worthless here: both hand-killed zombies reported
"running" while a full day idle. CPU is worthless too: the Node provider CLI
keeps ticking its event loop after the worker has stopped producing anything;
an estate measurement found 42 ticks in 60 seconds from a Claude session idle
for a full day. The evidence this sweep trusts is the worker's own output
transcript. Legacy Claude workers bind that file to ``--parent-session-id``
plus ``--agent-id``; current named workers are full sessions and bind it to
the exact ``sessionId`` in Claude's PID/start-bound session record. Agent-mode
workers that have no PID record bind instead to every standalone transcript
whose head or tail records declare the exact name and team in their ``--agent-id``.
Both layouts live beneath the same profile roots the collector searches. Every
exact copy's path, device, inode, size, nanosecond mtime, and nanosecond ctime
is evidence. If that full copy set does not move for
``SESSION_KIT_SUBAGENT_IDLE_MINUTES`` (default 15, 0 disables the sweep), the
worker is finished or abandoned; both deserve closing. CPU movement never
resets this clock.

Fifteen minutes is an operator ruling (2026-08-15): "close it 15 minutes
after it's finished." It replaces a six-hour window, and the margin that
window bought is now gone -- KNOWINGLY. A worker in one long silent compute,
with no transcript write for the whole window, is indistinguishable from a
worker that has stopped. It may be swept even while computing. That is the
accepted cost: a swept worker is not lost, its transcript persists, and
continuing it respawns a process. Conversely, a stopped worker whose provider
still writes output will stay alive until those writes stop; keeping it is the
safe error. Raise
``SESSION_KIT_SUBAGENT_IDLE_MINUTES`` if a real worker is ever cut off.

A transcript that cannot be found, opened, or identified as a regular file is
not evidence of idleness. The worker is left alone and the refusal is appended
to the pass log, so damaged output evidence is visible rather than silently
granting immortality. This refusal applies before TERM. Once a real TERM has
landed, escalation remains committed and the next pass sends KILL even if the
transcript moves or becomes unreadable.

The window is only as sharp as the pass that applies it: idle is measured
between passes, so a sweep every five minutes delivers the fifteen-minute
promise within about twenty. That is why the sweep has its own five-minute
timer instead of riding the hourly reaper, where fifteen minutes would have
meant up to seventy-five.

This module still requires a REAL Linux /proc, but CPU counters are no longer
the reason. The reaper's Darwin proc-shaped tree is regenerated from a single
process-table snapshot: mtimes inside that tree say when the snapshot files
were written, not when a worker produced output, and its PID generation cannot
be re-read live at signal time. No proc-entry mtime may substitute for missing
transcript evidence. The output snapshot comes directly from the transcript;
the reaper keeps the invocation gated off Darwin because the settled exact-PID
delivery proof still requires live procfs (re-derived X20-F1).

A candidate must BE a provider worker, not merely mention the flag: the
executable (argv[0], or argv[1] behind a node wrapper) must be named
``claude`` or live under a ``claude`` path component -- an exact component,
never a substring, so ``/tmp/notclaude`` and a ``claude-tmp`` working
directory both fail (lane finding X20-F2) -- and ``--parent-session-id`` and
``--agent-id`` must both be exact argv elements followed by a value. Only
processes owned by this uid are ever candidates.

Closing is two-step across passes: TERM first, KILL on the next pass if the
same process (pid + start ticks) is still alive. Immediately before any
signal the identity is re-read, and delivery pins the process with a pidfd
where the kernel offers one, so a pid recycled between scan and signal can
never be struck (lane finding X20-F3). A TERM that was not delivered is not
recorded as sent, so escalation to KILL always follows a real TERM (lane
finding X20-F5). Once a real TERM is sent, escalation is committed: a TERM
handler writing output on its way down must not reset its own idle clock;
post-TERM output movement is logged, never obeyed. One sweep at a time: the
pass flocks the state directory for its whole read-decide-write span, so
overlapping runs cannot collapse the TERM-to-KILL grace pass (lane finding
X20-F7) and a skipped pass leaves no lock file behind.

Every real action or output-evidence refusal appends one JSON line to
``subagent-sweep.log`` in the state directory. ``--dry-run`` prints what
would happen and touches nothing
on disk -- no signals, no state, no log (lane finding X20-F4); note a cold
dry run reports nothing, because only real passes arm the idle clock.

EVERY switch that stops this pass, in the order it is read. The first four
are checked by ``bin/shpool_reaper`` before this module is even started; the
rest are checked here. ``session-kit doctor`` names 1, 6, 7 and 8 directly on
its ``subagent-sweep`` line; 2 is reported as "does not run on macOS"; 3 and 5
reach only a hand-run pass, never the timer, and 4 is caught indirectly,
because all three leave the sweep with no completed pass to show and the
staleness rule reports that:

  1. ``~/.no_shpool_reaper`` (``SESSION_KIT_REAPER_SENTINEL``) -- the estate's
     oldest brake. It stops every closing pass, this one included; giving the
     sweep its own entry point once walked it out from behind this file, and
     that is the regression the check inside ``sweep_subagents()`` prevents by
     construction (lane A F1).
  2. macOS -- the sweep never runs there at all, because the reaper's Darwin
     process snapshot cannot support live PID re-verification (X20-F1).
  3. ``SESSION_KIT_REAPER_DRY_RUN=1`` -- reports, touches nothing.
  4. removing or renaming this module -- the reaper skips the sweep.
  5. ``SESSION_KIT_SUBAGENT_SWEEP`` set to ``0``/``off``/``no``/``false``.
     A VARIABLE, so it stops a hand-run pass and never the timer.
  6. ``<state>/subagent-sweep-off`` -- a file, so it reaches the systemd timer,
     which inherits none of their shell environment. This is the one to use.
  7. a window of ``0``, by any of the three routes below.
  8. a window that cannot be read, by any of those routes: unreadable is a
     REFUSAL, not a guess (lane A F3).

and the window itself, highest precedence first:
``SESSION_KIT_SUBAGENT_IDLE_MINUTES``, ``SESSION_KIT_SUBAGENT_IDLE_HOURS``,
``<state>/subagent-sweep-minutes``, then the default. Only the file reaches
the timer.

A pass that does nothing says so on stderr, and records ``last_pass`` in its
state file so that a sweep which has silently stopped running is visible to
``session-kit doctor`` (lane B F9).

stdlib only; /proc only; no provider APIs.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path, PurePath
import re
import signal
import stat
import sys
import time
from typing import Callable, cast

from .common import valid_uuid
from .transcripts import claude_roots

# An operator-editable window that actually reaches the timer, unlike an
# exported variable (lane A F4). One number, in minutes, beside the off switch.
IDLE_WINDOW_FILE = "subagent-sweep-minutes"
DEFAULT_IDLE_MINUTES = 15.0
# Kept as a derived value: the old environment variable still works, and a
# name that says hours cannot express fifteen minutes without a fraction.
DEFAULT_IDLE_HOURS = DEFAULT_IDLE_MINUTES / 60.0
STATE_VERSION = 3

_AGENT_FLAG = "--parent-session-id"
_ID_FLAG = "--agent-id"
_NAME_FLAG = "--agent-name"
_TEAM_FLAG = "--team-name"
_TYPE_FLAG = "--agent-type"
_SHELL_SNAPSHOT_PATH_RE = re.compile(
    r"^/.*/(?:\.claude|accounts/claude/[^/]+)/shell-snapshots/"
    r"snapshot-bash-[0-9]+-[A-Za-z0-9]+\.sh$"
)
_SHELL_SOURCE_PREFIX_RE = re.compile(
    r"^source (?P<path>/.*/(?:\.claude|accounts/claude/[^/]+)/shell-snapshots/"
    r"snapshot-bash-[0-9]+-[A-Za-z0-9]+\.sh)(?:\s|$)"
)
_MAX_SESSION_RECORD_BYTES = 1024 * 1024
_MAX_TRANSCRIPT_PROBE_BYTES = 256 * 1024
# The provider names its version directories `2.1.233`, and that directory IS
# the executable in the current packaging. Anchored and deliberately narrow: a
# component that merely contains digits is not a version.
_VERSION_RE = re.compile(r"\d+(\.\d+)+([-+][0-9A-Za-z.\-]+)?")

_HAS_PIDFD = hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")
SignalSender = Callable[[int, int], None]


def _finite_non_negative(raw: str) -> float | None:
    """A usable duration, or None for anything that is not one."""
    try:
        value = float(raw)
    except ValueError:
        return None
    # NaN fails its own equality test; infinity would disable the sweep by
    # accident rather than by the documented 0.
    if value < 0 or value != value or value == float("inf"):
        return None
    return value


def _idle_window_file(state_dir: Path) -> str:
    """The window an operator left in the state directory, if any.

    The systemd user manager does not inherit their shell environment and the
    kit never bridges it, so an exported variable never reaches the timer that
    does the closing (lane A F4). A file beside the off switch does, because
    the pass reads it itself.
    """
    try:
        path = state_dir / IDLE_WINDOW_FILE
        if path.is_symlink() or not path.is_file():
            return ""
        # Bytes, then a REPLACING decode. `read_text` decodes strictly, so a
        # single non-UTF-8 byte in this hand-editable file raised
        # UnicodeDecodeError -- a ValueError, which `except OSError` does not
        # catch -- out of the whole pass. The replacement character then fails
        # to parse as a number, which disables the sweep and says why. Both
        # outcomes close nothing; only one of them explains itself.
        return path.read_bytes()[:64].decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _env_idle_hours(
    environ: dict[str, str], state_dir: Path | None = None
) -> float:
    """The idle window, in hours, from whichever source is set.

    Order: the minutes variable, the hours variable, the state-directory file,
    then the default. Minutes is the name the ruling is written in, so it wins;
    the hours name keeps working unchanged, including 0 to disable.

    A value that cannot be read is a REFUSAL, not a default. It used to fall
    back to fifteen minutes -- the narrowest window in the system -- so a typo
    in the very variable the docs tell them to raise made the closer more
    aggressive, and a malformed minutes value silently revived a sweep they had
    turned off with hours=0 (lane A F3). Anything unparseable now disables the
    sweep and says so on stderr. Wrong-and-quiet is the failure this pass can
    least afford.
    """
    for name in ("SESSION_KIT_SUBAGENT_IDLE_MINUTES", "SESSION_KIT_SUBAGENT_IDLE_HOURS"):
        if name not in environ:
            continue
        raw = (environ.get(name) or "").strip()
        if not raw:
            _refuse(f"{name} is set but empty")
            return 0.0
        value = _finite_non_negative(raw)
        if value is None:
            _refuse(f"{name}={raw!r} is not a number of {'minutes' if 'MINUTES' in name else 'hours'}")
            return 0.0
        return value / 60.0 if "MINUTES" in name else value
    if state_dir is not None:
        raw = _idle_window_file(state_dir)
        if raw:
            value = _finite_non_negative(raw)
            if value is None:
                _refuse(f"{IDLE_WINDOW_FILE} contains {raw!r}, which is not a number of minutes")
                return 0.0
            return value / 60.0
    return DEFAULT_IDLE_HOURS


def _refuse(reason: str) -> None:
    """Say why the sweep is standing down. Never silent."""
    print(
        f"session inventory: sub-agent sweep disabled -- {reason}",
        file=sys.stderr,
    )


def _skipped(reason: str) -> None:
    """Say why a pass did nothing. A silent pass is indistinguishable from a
    pass that ran and found nothing, and those are not the same fact."""
    print(
        f"session inventory: sub-agent sweep skipped -- {reason}; "
        "nothing was closed on this pass",
        file=sys.stderr,
    )


def _read_cmdline(proc: Path, pid: int) -> list[str]:
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part for part in raw.decode("utf-8", "replace").split("\0") if part]


def _stat_numbers(proc: Path, pid: int) -> tuple[str, int, int, int, int] | None:
    """(state, ppid, own_cpu, reaped_child_cpu, start_ticks), or None."""
    try:
        raw = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # comm may contain spaces/parens; fields are fixed after the LAST ')'.
    tail = raw.rsplit(")", 1)
    if len(tail) != 2:
        return None
    fields = tail[1].split()
    # tail fields, 0-indexed: state=0 ppid=1 ... utime=11 stime=12 cutime=13
    # cstime=14 ... starttime=19
    if len(fields) < 20:
        return None
    try:
        ppid = int(fields[1])
        own = int(fields[11]) + int(fields[12])
        reaped = int(fields[13]) + int(fields[14])
        start_ticks = int(fields[19])
    except ValueError:
        return None
    return fields[0], ppid, own, reaped, start_ticks


def _shell_stat(
    proc: Path, pid: int
) -> tuple[str, int, int, int, int, int, int] | None:
    """The process/terminal identity needed by the background-shell pass.

    Returns ``(state, ppid, pgid, session, tty, tpgid, start_ticks)``.  This is
    deliberately separate from :func:`_stat_numbers`: the worker pass keeps
    reading exactly the fields it always has, while this independent pass
    refuses unless every terminal field it relies on is parseable.
    """
    try:
        raw = (proc / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    tail = raw.rsplit(")", 1)
    if len(tail) != 2:
        return None
    fields = tail[1].split()
    # tail fields: state=0, ppid=1, pgrp=2, session=3, tty_nr=4,
    # tpgid=5, ... starttime=19.
    if len(fields) < 20:
        return None
    try:
        return (
            fields[0],
            int(fields[1]),
            int(fields[2]),
            int(fields[3]),
            int(fields[4]),
            int(fields[5]),
            int(fields[19]),
        )
    except ValueError:
        return None


def _background_shell_stat(
    proc: Path, pid: int
) -> tuple[tuple[str, int, int, int, int, int, int, int] | None, str]:
    """Read one proc stat without confusing disappearance with unreadability."""
    path = proc / str(pid) / "stat"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, ""
    except OSError as error:
        return None, f"cannot read process stat {pid}: {error}"
    tail = raw.rsplit(")", 1)
    if len(tail) != 2:
        return None, f"cannot parse process stat {pid}"
    fields = tail[1].split()
    if len(fields) < 20:
        return None, f"cannot parse process stat {pid}"
    try:
        return (
            (
                fields[0],
                int(fields[1]),
                int(fields[2]),
                int(fields[3]),
                int(fields[4]),
                int(fields[5]),
                int(fields[19]),
                int(fields[11]) + int(fields[12]),
            ),
            "",
        )
    except ValueError:
        return None, f"cannot parse process stat {pid}"


def _flag_value(cmdline: list[str], flag: str) -> str:
    """The value following an exact-element flag; the ``=`` form is refused."""
    for index, part in enumerate(cmdline[:-1]):
        if part == flag:
            value = cmdline[index + 1]
            if value and not value.startswith("-"):
                return value
    return ""


def _is_provider_binary(executable: PurePath) -> bool:
    """The provider's OWN binary, by the provider's own packaging.

    This used to accept any executable with a path component named ``claude``
    (``"claude" in executable.parts``). That reaches a process the operator
    started: a tool of theirs under any directory called ``claude``, run with the
    two flags, was selected and TERMed after fifteen minutes -- reproduced by a
    review lane, and reproduced again here before this was changed. Destroy
    class 1 on the branch whose whole subject is an automatic closer.

    The fix is not "require the name ``claude``". MEASURED on the live process
    table 2026-08-15: all fourteen genuine workers run as
    ``/home/<user>/.local/share/claude/versions/2.1.233`` -- the version
    DIRECTORY is the executable, a Bun single-file build, basename ``2.1.233``.
    Requiring the name would have refused every real worker on the box and
    quietly retired the sweep. So the bind is to the provider's LAYOUT:

      * an executable named exactly ``claude`` -- the launcher on PATH and the
        older ``.../versions/<v>/claude`` layout; or
      * ``<...>/claude/versions/<version>``, the current layout, with the
        version component required to LOOK like a version.

    What this binds: the provider's packaging. What it does not: a file the
    operator deliberately places at ``<anything>/claude/versions/<semver>`` and
    runs with both provider flags is still eligible -- at which point it is
    indistinguishable from the provider by anything short of a signature check.
    That is the honest limit, and it is far away from "any directory called
    claude".
    """
    if executable.name == "claude":
        return True
    parent = executable.parent
    return (
        parent.name == "versions"
        and parent.parent.name == "claude"
        and _VERSION_RE.fullmatch(executable.name) is not None
    )


def _is_worker(cmdline: list[str]) -> bool:
    """A provider worker, never a bystander.

    ``grep claude ...`` fails (grep is not node, so argv[1] is never
    consulted), ``/tmp/notclaude`` fails, a helper under ``claude-tmp`` fails,
    and -- since the round-3 tightening -- so does any tool of the operator's
    that merely lives under a directory named ``claude``. See
    :func:`_is_provider_binary`.
    """
    if not cmdline:
        return False
    if not _flag_value(cmdline, _AGENT_FLAG) or not _flag_value(cmdline, _ID_FLAG):
        return False
    executable = PurePath(cmdline[0])
    if executable.name in ("node", "nodejs"):
        if len(cmdline) < 2:
            return False
        executable = PurePath(cmdline[1])
    return _is_provider_binary(executable)


def _provider_executable(cmdline: list[str]) -> PurePath | None:
    """The provider executable named by argv, including the Node wrapper."""
    if not cmdline:
        return None
    executable = PurePath(cmdline[0])
    if executable.name in ("node", "nodejs"):
        if len(cmdline) < 2:
            return None
        executable = PurePath(cmdline[1])
    return executable


def _is_provider_root(cmdline: list[str]) -> bool:
    """A managed Claude/Codex root, never one of its machine children.

    Claude reuses the exact packaging identity already settled for workers,
    with the worker/fork flags excluded.  Codex roots use its native launcher;
    non-interactive/server subcommands are machine children, not a person's
    root provider.  The shell fingerprint remains Claude-specific today, but
    retaining the managed Codex root shape costs no breadth: every shell still
    has to satisfy the independent exact fingerprint and terminal guards.
    """
    executable = _provider_executable(cmdline)
    if executable is None:
        return False
    if _is_provider_binary(executable):
        return not any(
            flag in cmdline
            for flag in (_AGENT_FLAG, "--fork-session")
        )
    if executable.name != "codex" or PurePath(cmdline[0]).name != "codex":
        return False
    return not any(
        command in cmdline[1:]
        for command in ("exec", "review", "app-server", "mcp-server")
    )


def _is_background_shell_cmdline(cmdline: list[str]) -> bool:
    """The narrow harness argv fingerprint; ordinary bash never qualifies."""
    if not cmdline or PurePath(cmdline[0]).name != "bash":
        return False
    if any(_SHELL_SNAPSHOT_PATH_RE.fullmatch(argument) for argument in cmdline[1:]):
        return True
    for index, argument in enumerate(cmdline[:-1]):
        if argument == "-c" and _SHELL_SOURCE_PREFIX_RE.match(cmdline[index + 1]):
            return True
    return False


def _exe_is_bash(proc: Path, pid: int) -> bool:
    """Bind bash argv to the executable procfs says is actually running."""
    try:
        target = os.readlink(proc / str(pid) / "exe")
    except OSError:
        return False
    return PurePath(target).name == "bash"


def _plain_component(value: object) -> str:
    """An exact path component supplied by the provider, or no component."""
    candidate = value if isinstance(value, str) else ""
    if not candidate or candidate in {".", ".."} or Path(candidate).name != candidate:
        return ""
    return candidate


def _stable_session_record(path: Path) -> tuple[dict[str, object] | None, str]:
    """Read one exact provider session record without link or rewrite races."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        return None, f"cannot read exact worker session record {path}: {error}"
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None, f"exact worker session record is not a regular file: {path}"
        if before.st_size > _MAX_SESSION_RECORD_BYTES:
            return None, f"exact worker session record is too large: {path}"
        chunks: list[bytes] = []
        remaining = _MAX_SESSION_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            return None, f"exact worker session record is too large: {path}"
        after = os.fstat(descriptor)
    except OSError as error:
        return None, f"cannot read exact worker session record {path}: {error}"
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        return None, f"exact worker session record changed while being read: {path}"
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError):
        return None, f"exact worker session record is not valid JSON: {path}"
    if not isinstance(payload, dict):
        return None, f"exact worker session record is not an object: {path}"
    return payload, ""


def _worker_session_identity_details(
    agent: dict[str, object], environ: dict[str, str]
) -> tuple[str, str, bool]:
    """Bind a current full-session worker through Claude's exact PID record.

    Claude publishes ``<profile>/sessions/<pid>.json`` with the full
    ``sessionId`` and Linux ``procStart``.  Requiring pid plus start ticks makes
    a stale record for a recycled pid ineligible. Multiple matching records may
    be profile copies only when they all advertise the same UUID.
    """
    pid = agent.get("pid")
    start_ticks = agent.get("start_ticks")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(start_ticks, int)
        or isinstance(start_ticks, bool)
    ):
        return "", "worker session identity is missing pid or start ticks", False
    identities: set[str] = set()
    saw_record = False
    saw_stale = False
    for root in claude_roots(environ):
        candidate = root / "sessions" / f"{pid}.json"
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            return "", f"cannot inspect exact worker session record: {error}", True
        saw_record = True
        record, record_error = _stable_session_record(candidate)
        if record_error:
            return "", record_error, True
        assert record is not None
        record_pid = record.get("pid")
        if (
            not isinstance(record_pid, int)
            or isinstance(record_pid, bool)
            or record_pid != pid
        ):
            return "", "exact worker session record pid does not match the worker", True
        proc_start = record.get("procStart")
        if not isinstance(proc_start, str) or not proc_start.isdecimal():
            return "", "exact worker session record is missing valid procStart", True
        if proc_start != str(start_ticks):
            saw_stale = True
            continue
        session_id = valid_uuid(record.get("sessionId"))
        if session_id is None:
            return "", "exact worker session record is missing valid sessionId", True
        identities.add(session_id)
    if len(identities) > 1:
        return "", "worker session identity is ambiguous across exact session records", True
    if identities:
        return next(iter(identities)), "", True
    if saw_stale:
        return "", "no exact worker session record matches the worker procStart", True
    if saw_record:
        return "", "worker session identity could not be read", True
    return (
        "",
        "worker session identity is missing exact sessions/<pid>.json record",
        False,
    )


def _worker_session_identity(
    agent: dict[str, object], environ: dict[str, str]
) -> tuple[str, str]:
    """Compatibility wrapper for the PID-record identity and refusal reason."""
    session_id, error, _record_exists = _worker_session_identity_details(
        agent, environ
    )
    return session_id, error


def _candidate_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _stat_quintuple(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _copy_evidence(candidate: Path, info: os.stat_result) -> dict[str, object]:
    """Movement evidence from the descriptor that verified this exact copy."""
    return {
        "path": os.fspath(candidate),
        "dev": info.st_dev,
        "ino": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _read_fd_bytes(descriptor: int, start: int, length: int) -> bytes:
    os.lseek(descriptor, start, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _window_membership_decision(
    payload: bytes,
    candidate: Path,
    agent_name: str,
    team_name: str,
    *,
    discard_first_fragment: bool,
    complete_final_line: bool,
) -> tuple[bool | None, str]:
    """The first identity-bearing (or root-session) record in one window."""
    if discard_first_fragment:
        _fragment, separator, payload = payload.partition(b"\n")
        if not separator:
            return None, ""
    lines = payload.split(b"\n")
    if not complete_final_line:
        lines.pop()
    for line in lines:
        if not line:
            continue
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if {"agentName", "teamName"}.issubset(record):
            member = (
                record.get("agentName") == agent_name
                and record.get("teamName") == team_name
            )
            if member and (
                valid_uuid(candidate.stem) is None
                or record.get("sessionId") != candidate.stem
            ):
                return None, (
                    "worker output transcript sessionId does not match "
                    f"its filename: {candidate}"
                )
            return member, ""
        # Root user/assistant records carry their own UUID but no agentName.
        # Other metadata records may also omit agentName, so they do not decide.
        if (
            record.get("type") in {"user", "assistant"}
            and record.get("sessionId") == candidate.stem
            and "agentName" not in record
        ):
            return False, ""
    return None, ""


def _content_transcript_probe(
    candidate: Path,
    agent_name: str,
    team_name: str,
    worker_started_ns: int | None = None,
) -> tuple[bool, dict[str, object] | None, str]:
    """Decide standalone membership and snapshot it through one descriptor."""
    try:
        descriptor = os.open(candidate, _candidate_open_flags())
    except OSError as error:
        return False, None, f"cannot read worker output transcript {candidate}: {error}"
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return (
                False,
                None,
                f"worker output transcript is not a regular file: {candidate}",
            )
        head_size = min(before.st_size, _MAX_TRANSCRIPT_PROBE_BYTES)
        head = _read_fd_bytes(descriptor, 0, head_size)
        decision, decision_error = _window_membership_decision(
            head,
            candidate,
            agent_name,
            team_name,
            discard_first_fragment=False,
            complete_final_line=before.st_size <= len(head),
        )
        if decision is None and not decision_error and before.st_size > head_size:
            tail_start = max(0, before.st_size - _MAX_TRANSCRIPT_PROBE_BYTES)
            previous = _read_fd_bytes(descriptor, tail_start - 1, 1)
            tail = _read_fd_bytes(
                descriptor, tail_start, before.st_size - tail_start
            )
            decision, decision_error = _window_membership_decision(
                tail,
                candidate,
                agent_name,
                team_name,
                discard_first_fragment=previous != b"\n",
                complete_final_line=True,
            )
        after = os.fstat(descriptor)
    except OSError as error:
        return False, None, f"cannot read worker output transcript {candidate}: {error}"
    finally:
        os.close(descriptor)
    if _stat_quintuple(before) != _stat_quintuple(after):
        return (
            False,
            None,
            f"worker output transcript changed while being probed: {candidate}",
        )
    if decision_error:
        return False, None, decision_error
    snapshot = _copy_evidence(candidate, after)
    if decision is None:
        if before.st_size <= head_size:
            # The whole file fit inside the head window, so every complete
            # record has been seen and none carried the agent identity
            # fields. That is TWO real shapes wearing one face: the husk an
            # aborted session left behind days ago, and a LIVE worker's
            # transcript in its first moments, before the first user record
            # brings the identity fields (review lanes rv-c10b-1/2 killed a
            # live worker through exactly this confusion). Age tells them
            # apart exactly: a worker's transcript cannot predate the worker
            # process. Older than the worker (with an hour of clock slack)
            # decides non-member; as new as the worker or newer REFUSES the
            # whole answer, the no-close direction.
            if worker_started_ns is None:
                return (
                    False,
                    None,
                    "worker start time is unreadable for transcript "
                    f"attribution: {candidate}",
                )
            newest_ns = max(before.st_mtime_ns, before.st_ctime_ns)
            if newest_ns >= worker_started_ns - _ATTRIBUTION_SLACK_NS:
                return (
                    False,
                    None,
                    "worker transcript is identityless but as recent as the "
                    f"worker itself: {candidate}",
                )
            return False, snapshot, ""
        if valid_uuid(candidate.stem) is not None:
            return (
                False,
                None,
                f"worker transcript membership undecidable: {candidate}",
            )
        return False, snapshot, ""
    return decision, snapshot, ""


def _content_transcript_member(
    candidate: Path,
    agent_name: str,
    team_name: str,
    worker_started_ns: int | None = None,
) -> tuple[bool, str]:
    """Compatibility wrapper for direct membership callers."""
    member, _snapshot, error = _content_transcript_probe(
        candidate, agent_name, team_name, worker_started_ns
    )
    return member, error


_ATTRIBUTION_SLACK_NS = 3600 * 1_000_000_000


def _worker_started_wallclock_ns(proc: Path, start_ticks: object) -> int | None:
    """The worker's start as wallclock nanoseconds, from boot time + ticks.

    Unreadable pieces return None — the caller must treat that as refusal
    territory, never as permission to dismiss a candidate transcript."""
    if not isinstance(start_ticks, int) or isinstance(start_ticks, bool):
        return None
    try:
        stat_text = (proc / "stat").read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    btime = None
    for line in stat_text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "btime" and fields[1].isdecimal():
            btime = int(fields[1])
            break
    if btime is None:
        return None
    try:
        hertz = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return None
    if hertz <= 0:
        return None
    return btime * 1_000_000_000 + (start_ticks * 1_000_000_000) // hertz


def _content_bound_candidates(
    projects: list[Path],
    agent_id: str,
    worker_started_ns: int | None = None,
) -> tuple[list[Path] | None, dict[str, dict[str, object]], str]:
    """Every standalone transcript declaring NAME@TEAM from exact argv."""
    agent_name, separator, team_name = agent_id.rpartition("@")
    if not separator or not agent_name or not team_name:
        return None, {}, "worker --agent-id is missing exact name@team fields"
    members: list[Path] = []
    probed: dict[str, dict[str, object]] = {}
    seen: set[str] = set()
    for project in projects:
        try:
            entries = sorted(os.scandir(project), key=lambda entry: entry.name)
        except FileNotFoundError:
            continue
        except OSError as error:
            return None, {}, f"cannot search {project}: {error}"
        for entry in entries:
            if not entry.name.endswith(".jsonl"):
                continue
            candidate = Path(entry.path)
            path = os.fspath(candidate)
            if path in seen:
                continue
            seen.add(path)
            member, snapshot, probe_error = _content_transcript_probe(
                candidate, agent_name, team_name, worker_started_ns
            )
            if probe_error:
                return None, {}, probe_error
            if snapshot is not None:
                probed[path] = snapshot
            if member:
                members.append(candidate)
    return members, probed, ""


def _candidate_snapshot(candidate: Path) -> tuple[dict[str, object] | None, str]:
    """Open, verify, and snapshot one exact candidate through one descriptor."""
    try:
        descriptor = os.open(candidate, _candidate_open_flags())
    except FileNotFoundError:
        return None, ""
    except OSError as error:
        return None, f"cannot read worker output transcript {candidate}: {error}"
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None, f"worker output transcript is not a regular file: {candidate}"
        os.read(descriptor, 1)
        after = os.fstat(descriptor)
    except OSError as error:
        return None, f"cannot read worker output transcript {candidate}: {error}"
    finally:
        os.close(descriptor)
    if _stat_quintuple(before) != _stat_quintuple(after):
        return None, f"worker output transcript changed while being read: {candidate}"
    return _copy_evidence(candidate, after), ""


def _background_shell_tree_snapshot(
    shell: dict[str, object], proc: Path, *, required_state: str | None = None
) -> tuple[dict[str, object] | None, str]:
    """Walk only this shell's descendants and snapshot its own CPU.

    Linux exposes each task's direct children below that process.  Walking
    those files keeps an unreadable candidate local to that candidate instead
    of making an unrelated numeric proc entry retire the whole shell pass.
    """
    shell_pid = shell.get("pid")
    shell_start = shell.get("start_ticks")
    shell_session = shell.get("session_id")
    if any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in (shell_pid, shell_start, shell_session)
    ):
        return None, "background shell identity is missing pid, start, or session"
    assert isinstance(shell_pid, int)
    assert isinstance(shell_start, int)
    assert isinstance(shell_session, int)
    shell_identity, error = _background_shell_stat(proc, shell_pid)
    if (
        shell_identity is None
        or shell_identity[0] == "Z"
        or (required_state is not None and shell_identity[0] != required_state)
        or shell_identity[6] != shell_start
        or shell_identity[3] != shell_session
    ):
        return None, error or "background shell identity changed during descendant scan"

    def direct_children(pid: int) -> tuple[list[int] | None, str]:
        task = proc / str(pid) / "task"
        try:
            tids = sorted(
                int(name)
                for name in os.listdir(task)
                if re.fullmatch(r"\d+", name)
            )
        except FileNotFoundError:
            return None, ""
        except OSError as task_error:
            return None, f"cannot enumerate process tasks for {pid}: {task_error}"
        if not tids:
            return None, f"process {pid} has no readable tasks"
        children: set[int] = set()
        for tid in tids:
            path = task / str(tid) / "children"
            try:
                raw = path.read_text(encoding="ascii", errors="strict")
            except FileNotFoundError:
                # A non-leader thread may vanish while tasks are enumerated.
                if not (task / str(tid)).exists():
                    continue
                return None, f"cannot read process children for {pid} task {tid}"
            except (OSError, UnicodeError) as child_error:
                return None, (
                    f"cannot read process children for {pid} task {tid}: "
                    f"{child_error}"
                )
            for word in raw.split():
                if not re.fullmatch(r"\d+", word):
                    return None, f"cannot parse process children for {pid} task {tid}"
                children.add(int(word))
        return sorted(children), ""

    initial, error = direct_children(shell_pid)
    if initial is None:
        return None, error or "background shell vanished during descendant scan"
    pending = list(initial)
    seen: set[int] = set()
    descendants: list[list[int]] = []
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        identity, error = _background_shell_stat(proc, pid)
        if identity is None:
            if error:
                return None, f"cannot read descendant {pid}: {error}"
            continue
        if identity[0] == "Z":
            continue
        children, error = direct_children(pid)
        if children is None:
            if error:
                return None, f"cannot walk descendant {pid}: {error}"
            continue
        pending.extend(children)
        (
            _state,
            _ppid,
            _pgid,
            session_id,
            tty_nr,
            _tpgid,
            start_ticks,
            _own_cpu,
        ) = identity
        if tty_nr != 0 and session_id != shell_session:
            return None, (
                f"descendant {pid} owns a tty in a different process session"
            )
        descendants.append([pid, start_ticks])
    descendants.sort(key=lambda item: (item[1], item[0]))
    shell_after, error = _background_shell_stat(proc, shell_pid)
    if (
        shell_after is None
        or shell_after[1:] != shell_identity[1:]
        or (required_state is not None and shell_after[0] != required_state)
    ):
        return None, error or "background shell changed during descendant scan"
    return (
        {
            "descendant_identities": descendants,
            "shell_cpu_ticks": shell_identity[7],
            "shell_identity": list(shell_identity[:7]),
        },
        "",
    )


def _background_shell_foreground_refusal(
    tree: dict[str, object],
) -> str:
    """Refuse when this childless shell owns its terminal foreground."""
    identity = tree.get("shell_identity")
    if (
        not isinstance(identity, list)
        or len(identity) != 7
        or not isinstance(identity[2], int)
        or isinstance(identity[2], bool)
        or not isinstance(identity[4], int)
        or isinstance(identity[4], bool)
        or not isinstance(identity[5], int)
        or isinstance(identity[5], bool)
    ):
        return "background shell tpgid is unreadable"
    pgid = identity[2]
    tty_nr = identity[4]
    tpgid = identity[5]
    if tty_nr != 0 and tpgid == pgid:
        return f"terminal foreground pgid {tpgid} belongs to the background shell"
    return ""


def _background_shell_own_snapshot(
    shell: dict[str, object], proc: Path
) -> tuple[dict[str, object] | None, str]:
    tree, error = _background_shell_tree_snapshot(shell, proc)
    if tree is None:
        return None, error
    descendants = tree.get("descendant_identities")
    if isinstance(descendants, list) and descendants:
        youngest = descendants[-1]
        return None, (
            f"youngest live descendant {youngest[0]} "
            f"(start ticks {youngest[1]}) prevents background shell closure"
        )
    foreground_error = _background_shell_foreground_refusal(tree)
    if foreground_error:
        return None, foreground_error
    return {"shell_cpu_ticks": tree["shell_cpu_ticks"]}, ""


def _background_shell_output_snapshot(
    shell: dict[str, object], proc: Path
) -> tuple[dict[str, object] | None, str]:
    """Snapshot the shell's exact fd/1 regular file as its idle evidence."""
    pid = shell.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None, "background shell identity is missing pid"
    fd_path = proc / str(pid) / "fd" / "1"
    try:
        target = os.readlink(fd_path)
    except OSError as error:
        return None, f"cannot resolve background shell fd/1: {error}"
    # Linux renders non-files as names such as pipe:[123], socket:[123], and
    # /dev/pts/4.  They have no stable regular-file tuple and are refusals.
    if re.match(r"^(?:pipe|socket|anon_inode):\[", target):
        return None, f"background shell fd/1 is not a regular file: {target}"
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = fd_path.parent / candidate
    try:
        fd_before = os.stat(fd_path)
    except OSError as error:
        return None, f"cannot inspect background shell fd/1: {error}"
    try:
        descriptor = os.open(candidate, _candidate_open_flags())
    except OSError as error:
        return None, f"cannot read background shell output {candidate}: {error}"
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(fd_before.st_mode):
            return None, f"background shell fd/1 is not a regular file: {target}"
        os.read(descriptor, 1)
        after = os.fstat(descriptor)
        fd_after = os.stat(fd_path)
        target_after = os.readlink(fd_path)
    except OSError as error:
        return None, f"cannot read background shell output {candidate}: {error}"
    finally:
        os.close(descriptor)
    if (
        _stat_quintuple(before) != _stat_quintuple(after)
        or (fd_before.st_dev, fd_before.st_ino)
        != (after.st_dev, after.st_ino)
        or _stat_quintuple(fd_after) != _stat_quintuple(after)
        or target_after != target
    ):
        return None, f"background shell output changed while being read: {candidate}"
    return {"output_copies": [_copy_evidence(candidate, after)]}, ""


def _candidate_copy_set(
    candidates: list[Path],
    probed: dict[str, dict[str, object]] | None = None,
) -> tuple[list[dict[str, object]] | None, str]:
    """Every readable exact copy, ordered by path for stable comparison."""
    copies: list[dict[str, object]] = []
    known = probed or {}
    by_path = {os.fspath(candidate): candidate for candidate in candidates}
    for path in sorted(by_path):
        snapshot = known.get(path)
        error = ""
        if snapshot is None:
            snapshot, error = _candidate_snapshot(by_path[path])
        if error:
            return None, error
        if snapshot is not None:
            copies.append(snapshot)
    return copies, ""


def _output_snapshot(
    agent: dict[str, object], environ: dict[str, str]
) -> tuple[dict[str, object] | None, str]:
    """The readable transcript this worker itself writes.

    Legacy workers bind by parent plus agent id. Current named workers are full
    sessions and bind by the exact ``sessionId`` in their PID/start-bound
    provider record. If no PID record exists, current agent-mode workers bind
    to standalone files whose own content declares the exact argv NAME@TEAM.
    All applicable layouts contribute to one full copy set.

    Every candidate is opened once with NOFOLLOW, verified as a readable regular
    file, and snapshotted from that same descriptor. Any exact copy that exists
    but cannot be read refuses the whole answer. Every readable copy publishes
    path, device, inode, size, nanosecond mtime, and nanosecond ctime in
    deterministic path order, so appearance, disappearance, replacement, or
    movement resets the idle clock.
    """
    parent = _plain_component(agent.get("parent"))
    agent_id = _plain_component(agent.get("agent_id"))
    if not parent or not agent_id:
        return None, "worker output identity is not one exact path component"
    projects_found: list[Path] = []
    search_errors: list[str] = []
    filename = f"agent-{agent_id}.jsonl"
    for root in claude_roots(environ):
        projects = root / "projects"
        try:
            entries = list(os.scandir(projects))
        except FileNotFoundError:
            continue
        except OSError as error:
            search_errors.append(f"cannot search {projects}: {error}")
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError as error:
                search_errors.append(f"cannot inspect {entry.path}: {error}")
                continue
            projects_found.append(Path(entry.path))
    if search_errors:
        return None, search_errors[0]
    candidates = [
        project / parent / "subagents" / filename for project in projects_found
    ]
    identity_error = agent.get("session_identity_error")
    session_id = valid_uuid(agent.get("session_id"))
    record_exists = agent.get("session_record_exists")
    current_shape = agent.get("current_worker_shape") is True
    required_registry_paths: set[str] = set()
    probed: dict[str, dict[str, object]] = {}

    # A full answer always re-reads the PID/start-bound registry. This is what
    # makes the signal-time answer fresh rather than a replay of process-scan
    # identity, while preserving registry precedence when a record exists.
    if current_shape:
        refreshed_id, refreshed_error, refreshed_exists = (
            _worker_session_identity_details(agent, environ)
        )
        record_exists = refreshed_exists
        identity_error = refreshed_error
        session_id = valid_uuid(refreshed_id)
    if record_exists is False and current_shape:
        worker_started_ns = agent.get("start_wallclock_ns")
        if not isinstance(worker_started_ns, int) or isinstance(
            worker_started_ns, bool
        ):
            worker_started_ns = None
        content_candidates, probed, candidate_error = _content_bound_candidates(
            projects_found,
            agent_id,
            worker_started_ns,
        )
        if candidate_error:
            return None, candidate_error
        assert content_candidates is not None
        candidates.extend(content_candidates)

        # A PID record can appear after process discovery. Retain the content
        # members already found and union the stronger registry candidate.
        refreshed_id, refreshed_error, refreshed_exists = (
            _worker_session_identity_details(agent, environ)
        )
        if refreshed_exists:
            if refreshed_error:
                return None, refreshed_error
            session_id = valid_uuid(refreshed_id)
            identity_error = refreshed_error
            if session_id is not None:
                required_registry_paths = {
                    os.fspath(project / f"{session_id}.jsonl")
                    for project in projects_found
                }
    if not identity_error and session_id is not None:
        candidates.extend(project / f"{session_id}.jsonl" for project in projects_found)
    copies, copy_error = _candidate_copy_set(candidates, probed)
    if copy_error:
        return None, copy_error
    assert copies is not None
    if required_registry_paths and not any(
        copy["path"] in required_registry_paths for copy in copies
    ):
        return None, "worker output transcript for exact sessionId was not found"
    if copies:
        return {"output_copies": copies}, ""
    if identity_error:
        return None, str(identity_error)
    if session_id is None:
        return None, "worker session identity is missing valid sessionId"
    return (
        None,
        "worker output transcript for exact sessionId was not found",
    )


def find_subagents(
    proc: Path, environ: dict[str, str] | None = None
) -> list[dict[str, object]]:
    """Every live provider sub-agent worker THIS uid owns."""
    values = dict(os.environ) if environ is None else environ
    found: list[dict[str, object]] = []
    try:
        entries = sorted(
            int(name) for name in os.listdir(proc) if re.fullmatch(r"\d+", name)
        )
    except OSError:
        return found
    own_uid = os.geteuid()
    for pid in entries:
        before = _stat_numbers(proc, pid)
        if before is None:
            continue
        state, _ppid, _own, _reaped, start_ticks = before
        if state == "Z":
            continue
        cmdline = _read_cmdline(proc, pid)
        if not _is_worker(cmdline):
            continue
        try:
            if (proc / str(pid)).stat().st_uid != own_uid:
                continue
        except OSError:
            continue
        agent = {
            "pid": pid,
            "start_ticks": start_ticks,
            "agent_id": _flag_value(cmdline, _ID_FLAG),
            "parent": _flag_value(cmdline, _AGENT_FLAG),
            "current_worker_shape": bool(_flag_value(cmdline, _NAME_FLAG))
            and bool(_flag_value(cmdline, _TEAM_FLAG))
            and bool(_flag_value(cmdline, _TYPE_FLAG)),
            "start_wallclock_ns": _worker_started_wallclock_ns(
                proc, start_ticks
            ),
        }
        session_id, session_identity_error, session_record_exists = (
            _worker_session_identity_details(agent, values)
        )
        after = _stat_numbers(proc, pid)
        after_cmdline = _read_cmdline(proc, pid)
        # CPU counters and ordinary S/R state churn are deliberately irrelevant.
        # Only the process generation and the identity-bearing argv must remain
        # the same around the exact provider-session record read.
        if (
            after is None
            or after[0] == "Z"
            or after[4] != start_ticks
            or after_cmdline != cmdline
        ):
            continue
        agent["session_id"] = session_id
        agent["session_identity_error"] = session_identity_error
        agent["session_record_exists"] = session_record_exists
        found.append(agent)
    return found


def find_background_shells(proc: Path) -> list[dict[str, object]]:
    """Every exact harness shell directly owned by a live provider root."""
    found: list[dict[str, object]] = []
    try:
        entries = sorted(
            int(name) for name in os.listdir(proc) if re.fullmatch(r"\d+", name)
        )
    except OSError:
        return found
    own_uid = os.geteuid()
    for pid in entries:
        cmdline = _read_cmdline(proc, pid)
        if not _is_background_shell_cmdline(cmdline):
            continue
        before = _shell_stat(proc, pid)
        if before is None or before[0] == "Z" or not _exe_is_bash(proc, pid):
            continue
        _state, ppid, pgid, session_id, tty_nr, tpgid, start_ticks = before
        parent_before = _shell_stat(proc, ppid)
        if parent_before is None or parent_before[0] == "Z":
            continue
        parent_cmdline = _read_cmdline(proc, ppid)
        if not _is_provider_root(parent_cmdline):
            continue
        parent_tty = parent_before[4]
        if tty_nr not in (0, parent_tty):
            continue
        try:
            if (
                (proc / str(pid)).stat().st_uid != own_uid
                or (proc / str(ppid)).stat().st_uid != own_uid
            ):
                continue
        except OSError:
            continue
        # Bind both generations and every identity-bearing field around the
        # scan. CPU and scheduling state are intentionally irrelevant.
        after = _shell_stat(proc, pid)
        parent_after = _shell_stat(proc, ppid)
        if (
            after is None
            or after[0] == "Z"
            or after[1:] != before[1:]
            or _read_cmdline(proc, pid) != cmdline
            or not _exe_is_bash(proc, pid)
            or parent_after is None
            or parent_after[0] == "Z"
            or parent_after[1:] != parent_before[1:]
            or _read_cmdline(proc, ppid) != parent_cmdline
        ):
            continue
        shell = {
            "pid": pid,
            "start_ticks": start_ticks,
            "provider_pid": ppid,
            "provider_start_ticks": parent_before[6],
            "session_id": session_id,
        }
        found.append(cast(dict[str, object], shell))
    return found


def _still_the_process(proc: Path, pid: int, start_ticks: int) -> bool:
    """The recorded process generation is live at this instant.

    A zombie retains its PID and start ticks only until its parent reaps it;
    it has already exited and cannot survive or perform work.
    """
    numbers = _stat_numbers(proc, pid)
    return numbers is not None and numbers[0] != "Z" and numbers[4] == start_ticks


def _still_the_worker(proc: Path, pid: int, start_ticks: int) -> bool:
    """The recorded identity, re-read at signal time: same start ticks AND
    still worker-shaped. A recycled pid fails one or both."""
    if not _still_the_process(proc, pid, start_ticks):
        return False
    return _is_worker(_read_cmdline(proc, pid))


def _background_shell_identity_snapshot(
    proc: Path,
    pid: int,
    start_ticks: int,
    shell: dict[str, object],
) -> tuple[dict[str, object] | None, str]:
    """Re-derive the shell and parent identity without making a tty decision."""
    identity = _shell_stat(proc, pid)
    provider_pid = shell.get("provider_pid")
    provider_start = shell.get("provider_start_ticks")
    if (
        identity is None
        or identity[0] == "Z"
        or identity[6] != start_ticks
        or not isinstance(provider_pid, int)
        or isinstance(provider_pid, bool)
        or not isinstance(provider_start, int)
        or isinstance(provider_start, bool)
        or identity[1] != provider_pid
        or identity[3] != shell.get("session_id")
        or not _is_background_shell_cmdline(_read_cmdline(proc, pid))
        or not _exe_is_bash(proc, pid)
    ):
        return None, "background shell identity changed before delivery"
    _state, _ppid, _pgid, _session, tty_nr, _tpgid, _start = identity
    parent = _shell_stat(proc, provider_pid)
    if (
        parent is None
        or parent[0] == "Z"
        or parent[6] != provider_start
        or not _is_provider_root(_read_cmdline(proc, provider_pid))
        or tty_nr not in (0, parent[4])
    ):
        return None, "background shell provider identity changed before delivery"
    return {
        "shell_identity": list(identity[1:]),
        "provider_identity": list(parent[1:]),
    }, ""


def _background_shell_evidence_snapshot(
    shell: dict[str, object], proc: Path
) -> tuple[dict[str, object] | None, str]:
    """The per-pass childless-shell own-CPU and output proof."""
    own, error = _background_shell_own_snapshot(shell, proc)
    if own is None:
        return None, error
    output, error = _background_shell_output_snapshot(shell, proc)
    if output is None:
        return None, error
    return {**own, **output}, ""


def _background_shell_own_cpu_moved(
    previous: dict[str, object], current: dict[str, object]
) -> bool:
    """Only strict equality of the shell's own CPU permits closure."""
    previous_cpu = previous.get("shell_cpu_ticks")
    current_cpu = current.get("shell_cpu_ticks")
    return (
        not isinstance(previous_cpu, int)
        or isinstance(previous_cpu, bool)
        or not isinstance(current_cpu, int)
        or isinstance(current_cpu, bool)
        or current_cpu != previous_cpu
    )


def _background_shell_final_proof(
    proc: Path,
    shell: dict[str, object],
    armed: dict[str, object],
) -> tuple[dict[str, object] | None, str]:
    """Full proof while the pinned shell is frozen in state T."""
    pid = int(cast(int, shell["pid"]))
    start_ticks = int(cast(int, shell["start_ticks"]))
    identity, error = _background_shell_identity_snapshot(
        proc, pid, start_ticks, shell
    )
    if identity is None:
        return None, error
    stopped = _shell_stat(proc, pid)
    if stopped is None or stopped[0] != "T":
        return None, "background shell did not remain stopped in state T"
    tree, error = _background_shell_tree_snapshot(shell, proc, required_state="T")
    if tree is None:
        return None, error
    descendants = tree.get("descendant_identities")
    if isinstance(descendants, list) and descendants:
        youngest = descendants[-1]
        return None, (
            f"youngest live descendant {youngest[0]} "
            f"(start ticks {youngest[1]}) prevents background shell closure"
        )
    own = {"shell_cpu_ticks": tree["shell_cpu_ticks"]}
    if _background_shell_own_cpu_moved(armed, own):
        return None, "background shell own CPU moved before delivery"
    foreground_error = _background_shell_foreground_refusal(tree)
    if foreground_error:
        return None, foreground_error
    # fd/1 equality is deliberately the last proof before the signal syscall.
    output, error = _background_shell_output_snapshot(shell, proc)
    if output is None:
        return None, error
    if output.get("output_copies") != armed.get("output_copies"):
        return None, "background shell fd/1 tuple moved before delivery"
    return {**identity, **own, **output}, ""


def _background_shell_wait_stopped(
    proc: Path, pid: int, start_ticks: int
) -> bool:
    """Wait briefly for the asynchronous SIGSTOP state to appear in procfs."""
    for attempt in range(20):
        identity = _shell_stat(proc, pid)
        if identity is None or identity[6] != start_ticks:
            return False
        if identity[0] == "T":
            return True
        if attempt != 19:
            time.sleep(0.005)
    return False


def _deliver_background_shell(
    proc: Path,
    shell: dict[str, object],
    armed: dict[str, object],
    signum: int,
    *,
    send_signal: Callable[[int, int], None] | None = None,
) -> tuple[bool, dict[str, object] | None, str]:
    """Pin, freeze, re-prove, close, and resume one exact shell.

    ``send_signal`` is the synthetic-proc test seam and represents pidfd signal
    delivery. Production refuses when pidfds are unavailable and never falls
    back to a PID-only shell kill.
    """
    pid = int(cast(int, shell["pid"]))
    if send_signal is None and not _HAS_PIDFD:
        return False, None, "pidfd delivery is unavailable for background shell"
    descriptor = os.pidfd_open(pid) if send_signal is None else None
    stopped = False
    delivered = False
    proof: dict[str, object] | None = None
    error = ""
    target_exited_before_cont = False
    target_reaped_before_cont = False

    def send(pidfd_signum: int) -> None:
        if send_signal is not None:
            send_signal(pid, pidfd_signum)
        else:
            assert descriptor is not None
            signal.pidfd_send_signal(descriptor, pidfd_signum)

    try:
        send(signal.SIGSTOP)
        stopped = True
        if not _background_shell_wait_stopped(
            proc, pid, int(cast(int, shell["start_ticks"]))
        ):
            error = "background shell did not enter stopped state T"
        else:
            proof, error = _background_shell_final_proof(proc, shell, armed)
            if proof is not None:
                send(signum)
                delivered = True
                after_signal = _shell_stat(proc, pid)
                if (
                    after_signal is None
                    or after_signal[6] != int(cast(int, shell["start_ticks"]))
                ):
                    target_exited_before_cont = True
                    target_reaped_before_cont = True
                elif after_signal[0] in {"X", "Z"}:
                    target_exited_before_cont = True
    finally:
        try:
            if stopped:
                try:
                    send(signal.SIGCONT)
                except OSError as resume_error:
                    if not delivered:
                        raise
                    if resume_error.errno == errno.ESRCH:
                        target_exited_before_cont = True
                        target_reaped_before_cont = True
                    else:
                        error = (
                            "background shell closing signal was delivered; "
                            f"SIGCONT failed: {resume_error}"
                        )
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as close_error:
                    if not delivered:
                        raise
                    error = (
                        "background shell closing signal was delivered; "
                        f"pidfd close failed: {close_error}"
                    )
    if delivered and proof is not None:
        if target_exited_before_cont:
            proof["target_exited_before_cont"] = True
        if target_reaped_before_cont:
            proof["target_reaped_before_cont"] = True
    return delivered, proof, error


def _deliver_exact_process(
    proc: Path,
    pid: int,
    start_ticks: int,
    signum: int,
    *,
    still_expected: Callable[[Path, int, int], bool] | None = None,
    send_signal: Callable[[int, int], None] | None = None,
    before_signal: Callable[[], bool] | None = None,
) -> bool:
    """Signal exactly the recorded process.

    With pidfd support the process is pinned first, then re-verified through
    /proc, then signalled through the fd -- the kernel guarantees the fd can
    never address a pid-reuse successor. Without pidfd the identity re-read
    narrows the race to the read-kill gap, the classic best available.
    ``before_signal`` runs only after the process-generation guard and may veto
    delivery when fresh external evidence no longer matches the armed answer.
    Returns false only for that safe veto. Raises ProcessLookupError when the
    recorded process is already gone or replaced.
    """
    expected = still_expected or _still_the_process
    if send_signal is not None:
        if not expected(proc, pid, start_ticks):
            raise ProcessLookupError(pid)
        if before_signal is not None and not before_signal():
            return False
        send_signal(pid, signum)
    elif _HAS_PIDFD:
        fd = os.pidfd_open(pid)
        try:
            if not expected(proc, pid, start_ticks):
                raise ProcessLookupError(pid)
            if before_signal is not None and not before_signal():
                return False
            signal.pidfd_send_signal(fd, signum)
        finally:
            os.close(fd)
    else:
        if not expected(proc, pid, start_ticks):
            raise ProcessLookupError(pid)
        if before_signal is not None and not before_signal():
            return False
        os.kill(pid, signum)
    return True


def _deliver(
    proc: Path,
    pid: int,
    start_ticks: int,
    signum: int,
    *,
    before_signal: Callable[[], bool] | None = None,
) -> bool:
    """Worker-shaped compatibility wrapper around exact-process delivery."""
    return _deliver_exact_process(
        proc,
        pid,
        start_ticks,
        signum,
        still_expected=_still_the_worker,
        before_signal=before_signal,
    )


def _boot_id(proc: Path) -> str:
    """This boot, so a pid:start_ticks pair cannot span two of them.

    Start ticks are counted from boot, so the same pair identifies a DIFFERENT
    process after a reboot. A persisted TERM decision could then land on a
    stranger as an immediate KILL. Unreadable is fine and compares equal to
    itself, which is what a fixture wants.
    """
    try:
        return (proc / "sys" / "kernel" / "random" / "boot_id").read_text(
            encoding="utf-8"
        ).strip()[:64]
    except OSError:
        return ""


def _load_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return {}
    return payload


def _save_state(
    path: Path,
    tracked: dict[str, dict[str, object]],
    *,
    boot_id: str,
    last_pass: float,
    window_seconds: float,
) -> None:
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(
        json.dumps(
            {
                "version": STATE_VERSION,
                "tracked": tracked,
                "boot_id": boot_id,
                "last_pass": last_pass,
                # The window the evidence below was gathered under. A shorter
                # window is a NEW rule, and evidence collected under the old
                # one cannot be used to close anything (see _locked_sweep).
                "window_seconds": window_seconds,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.replace(scratch, path)


def _log(log_path: Path, record: dict[str, object]) -> None:
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def sweep(
    *,
    proc: Path,
    state_dir: Path,
    environ: dict[str, str],
    now: float,
    kill: SignalSender | None = None,
    dry_run: bool = False,
) -> list[dict[str, object]]:
    """One pass. Returns the action records it produced.

    ``kill`` is a test seam: when provided it replaces only the delivery
    syscall -- the identity re-read still runs against ``proc`` first, so
    tests exercise the same refusal path production uses.
    """
    idle_hours = _env_idle_hours(environ, state_dir)
    switched_off = (
        idle_hours == 0
        or (environ.get("SESSION_KIT_SUBAGENT_SWEEP") or "").strip().casefold()
        in {"0", "off", "no", "false"}
        or (state_dir / "subagent-sweep-off").exists()
    )
    if switched_off:
        return []
    # One sweep at a time across the whole read-decide-write span. A second
    # runner (a hand-run reaper beside the timer) skipping is correct; the
    # dangerous alternative is it reading the first runner's fresh term_sent
    # and firing KILL in the same second TERM landed (lane finding X20-F7).
    # The lock is a flock on the state DIRECTORY itself, not on a lock file:
    # a dry pass must create nothing on disk, ever (lane finding X20-F4,
    # round 2), and the directory descriptor gives the same exclusion with
    # zero filesystem writes.
    #
    # A pass that does nothing must SAY it did nothing. Silence is the exact
    # symptom this whole change exists to remove, and a lock that is never
    # released -- a stuck process, a debugger, a hand-run pass that hung --
    # used to retire the rule permanently with no evidence anywhere (lane B
    # F9). Both skips are safe (nothing is closed) and both are now audible on
    # stderr, which is the journal under the timer. The durable half of the
    # trace is `last_pass` in the state file: `session-kit doctor` reads it and
    # reports a sweep that has stopped completing passes, whatever the cause.
    try:
        lock_handle = os.open(state_dir, os.O_RDONLY)
    except OSError as error:
        _skipped(f"cannot open its state directory {state_dir}: {error}")
        return []
    try:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _skipped(f"another pass holds {state_dir}")
            return []
        return _locked_sweep(
            proc=proc,
            state_dir=state_dir,
            environ=environ,
            now=now,
            idle_hours=idle_hours,
            kill=kill,
            dry_run=dry_run,
        )
    finally:
        os.close(lock_handle)


def _sweep_background_shells(
    *,
    proc: Path,
    tracked: dict[str, object],
    next_tracked: dict[str, dict[str, object]],
    boot_id: str,
    now: float,
    idle_seconds_required: float,
    kill: SignalSender | None,
    dry_run: bool,
    log_path: Path,
) -> list[dict[str, object]]:
    """Independent childless-shell pass with own-CPU and fd/1 evidence."""
    actions: list[dict[str, object]] = []
    for shell in find_background_shells(proc):
        pid = int(cast(int, shell["pid"]))
        start_ticks = int(cast(int, shell["start_ticks"]))
        key = f"background-shell:{boot_id}:{pid}:{start_ticks}"
        prior = tracked.get(key)
        evidence, evidence_error = _background_shell_evidence_snapshot(shell, proc)
        if evidence is None:
            refusal = {
                "at": now,
                "kind": "background-shell",
                "pid": pid,
                "start_ticks": start_ticks,
                "provider_pid": shell["provider_pid"],
                "decision": "refused-output",
                "reason": evidence_error,
            }
            if not dry_run:
                _log(log_path, refusal)
            continue
        record: dict[str, object] = {
            **evidence,
            "last_active": now,
            "term_sent": False,
        }
        if isinstance(prior, dict):
            evidence_unchanged = (
                prior.get("output_copies") == evidence.get("output_copies")
                and not _background_shell_own_cpu_moved(prior, evidence)
            )
            if evidence_unchanged:
                record["last_active"] = prior.get("last_active", now)
                record["term_sent"] = bool(prior.get("term_sent"))
        idle_seconds = now - float(cast(float, record["last_active"]))
        overdue = idle_seconds >= idle_seconds_required
        if bool(record["term_sent"]) or overdue:
            escalate = bool(record["term_sent"])
            signum = signal.SIGKILL if escalate else signal.SIGTERM
            action = {
                "at": now,
                "kind": "background-shell",
                "pid": pid,
                "start_ticks": start_ticks,
                "provider_pid": shell["provider_pid"],
                "idle_seconds": int(idle_seconds),
                "signal": "SIGKILL" if escalate else "SIGTERM",
                "moved_after_term": False,
                "dry_run": dry_run,
            }
            if not dry_run:
                try:
                    delivered, final_proof, delivery_error = (
                        _deliver_background_shell(
                            proc,
                            shell,
                            record,
                            signum,
                            send_signal=kill if kill is not None else None,
                        )
                    )
                except (ProcessLookupError, OSError) as error:
                    delivered = False
                    final_proof = None
                    delivery_error = f"background shell delivery failed: {error}"
                if not delivered:
                    _log(
                        log_path,
                        {
                            "at": now,
                            "kind": "background-shell",
                            "pid": pid,
                            "start_ticks": start_ticks,
                            "provider_pid": shell["provider_pid"],
                            "decision": "refused-final-proof",
                            "reason": delivery_error,
                        },
                    )
                    # A refusal never inherits a TERM commitment. A later
                    # readable/stable pass must arm a fresh full window.
                    refreshed, _refresh_error = _background_shell_evidence_snapshot(
                        shell, proc
                    )
                    if refreshed is not None:
                        next_tracked[key] = {
                            **refreshed,
                            "last_active": now,
                            "term_sent": False,
                        }
                    continue
                if not escalate:
                    record["term_sent"] = True
                if isinstance(final_proof, dict):
                    if final_proof.get("target_exited_before_cont") is True:
                        action["target_exited_before_cont"] = True
                    if final_proof.get("target_reaped_before_cont") is True:
                        action["target_reaped_before_cont"] = True
                _log(log_path, action)
                actions.append(action)
            else:
                actions.append(action)
        next_tracked[key] = record
    return actions


def _locked_sweep(
    *,
    proc: Path,
    state_dir: Path,
    environ: dict[str, str],
    now: float,
    idle_hours: float,
    kill: SignalSender | None,
    dry_run: bool,
) -> list[dict[str, object]]:
    state_path = state_dir / "subagent-sweep.state.json"
    log_path = state_dir / "subagent-sweep.log"
    stored = _load_state(state_path)
    tracked = stored.get("tracked")
    tracked = tracked if isinstance(tracked, dict) else {}
    boot_id = _boot_id(proc)
    stored_boot = stored.get("boot_id")
    last_pass = stored.get("last_pass")
    last_pass = (
        float(last_pass)
        if isinstance(last_pass, (int, float)) and not isinstance(last_pass, bool)
        else None
    )
    # NOTHING may be closed on evidence gathered before the rule that judges
    # it was running. `last_active` is not "when the worker stopped" -- it is
    # "when a pass last saw its output move", so it carries two assumptions: that the
    # passes were closer together than the window, and that the window has not
    # moved. Every way either breaks has the same answer -- discard the prior
    # observation and watch every worker afresh for a full window under the
    # CURRENT rule:
    #
    #   * the window is SHORTER than the one this evidence was gathered under.
    #     This is install day (lane A F2): the state file on disk was armed by
    #     the six-hour pass, and the first fifteen-minute pass would otherwise
    #     find every worker instantly overdue on evidence collected when
    #     seventeen idle minutes meant nothing at all. It is also every later
    #     narrowing -- a future default, or the operator lowering
    #     `<state>/subagent-sweep-minutes` -- so the guarantee holds by the
    #     rule itself and not by an old state file happening to lack a field.
    #
    #     A window that WIDENS keeps its evidence, because WIDENING CAN ONLY
    #     CLOSE LESS. That asymmetry is the whole rule in one line: an idle
    #     clock reads the same under either window, so what a narrowing
    #     invalidates is not the measurement, it is the CONSENT -- while those
    #     minutes accrued, nobody had asked for anything to be closed at the
    #     new window. Widening needs no restart, because under a wider rule a
    #     worker simply takes longer to become overdue; nothing is ever closed
    #     sooner than the current rule allows.
    #   * the window that gathered it is not recorded at all, so it cannot be
    #     compared. Unknown provenance is untrusted provenance.
    #   * the machine rebooted -- start ticks are counted from boot, so the
    #     same pid:start_ticks pair is a DIFFERENT process on the other side of
    #     one, and a persisted TERM could land on a stranger as a KILL.
    #   * passes were skipped, e.g. the flock held by the hourly reaper, so the
    #     gap exceeds the window and nobody watched during it.
    #   * the clock jumped (F8). Forward is caught by the gap rule; backward is
    #     caught here.
    window_seconds = idle_hours * 3600
    stored_window = stored.get("window_seconds")
    if isinstance(stored_window, bool) or not isinstance(stored_window, (int, float)):
        stored_window = None
    else:
        stored_window = float(stored_window)
        # A NaN compares false against everything, and a window of zero or less
        # is not a window at all -- a pass only ever saves state when the sweep
        # is ON, so a stored zero cannot have come from this code and cannot be
        # what gathered the evidence. Any of them would slip past the comparison
        # below and let the evidence be trusted, which is the direction that
        # closes MORE. A window this code cannot compare is a window it does not
        # know, so it takes the same answer as one that was never recorded.
        if stored_window != stored_window or stored_window <= 0:
            stored_window = None
    rearmed = ""
    if isinstance(stored_boot, str) and stored_boot != boot_id:
        rearmed = "the machine rebooted"
    elif last_pass is None:
        rearmed = "no previous pass under this rule"
    elif stored_window is None:
        rearmed = "the previous pass did not record the window it was judging by"
    elif window_seconds < stored_window:
        rearmed = (
            f"the window was shortened from {stored_window:g}s to {window_seconds:g}s"
        )
    elif now - last_pass > window_seconds:
        rearmed = f"{int(now - last_pass)}s since the previous pass, longer than the window"
    elif now < last_pass:
        rearmed = "the clock moved backwards"
    if rearmed and tracked:
        print(
            "session inventory: sub-agent sweep starting its clocks fresh -- "
            f"{rearmed}; nothing is closed on this pass",
            file=sys.stderr,
        )
    if rearmed:
        tracked = {}
    live = find_subagents(proc, environ)
    actions: list[dict[str, object]] = []
    next_tracked: dict[str, dict[str, object]] = {}
    for agent in live:
        pid = int(cast(int, agent["pid"]))
        start_ticks = int(cast(int, agent["start_ticks"]))
        # The boot is part of the identity, not just part of the document.
        # Start ticks are counted FROM BOOT, so `pid:start_ticks` names a
        # different process on the other side of one -- and a persisted
        # `term_sent` inherited by that stranger is an immediate KILL of a
        # brand-new worker (Codex 1/3, reproduced). The whole-document check
        # above already discards a previous boot's decisions; putting the boot
        # in the key too means a stale entry cannot even be LOOKED UP if that
        # check is ever defeated -- an unreadable boot id, a hand-edited state
        # file, a future edit to the re-arm chain. Two independent guards,
        # because this one ends processes.
        key = f"{boot_id}:{pid}:{start_ticks}"
        prior = tracked.get(key)
        committed = isinstance(prior, dict) and bool(prior.get("term_sent"))
        output, output_error = _output_snapshot(agent, environ)
        if output is None and not committed:
            refusal = {
                "at": now,
                "pid": pid,
                "start_ticks": start_ticks,
                "agent_id": agent["agent_id"],
                "parent": agent["parent"],
                "decision": "refused-output",
                "reason": output_error,
            }
            if not dry_run:
                _log(log_path, refusal)
            # Unreadable is never an idle answer. Do not retain an older idle
            # clock: if output becomes readable later it must be watched for a
            # fresh full window before TERM.
            continue
        record: dict[str, object] = {
            **(output or {}),
            "last_active": now,
            "term_sent": False,
        }
        moved_after_term = False
        if isinstance(prior, dict):
            # TERM committed: the decision stands even if the handler writes
            # on the way down -- otherwise a graceful-shutdown message resets
            # its own clock every pass and the kill never lands.
            record["term_sent"] = committed
            same_output = (
                output is not None
                and prior.get("output_copies") == output.get("output_copies")
            )
            if same_output:
                record["last_active"] = prior.get("last_active", now)
            elif record["term_sent"]:
                moved_after_term = output is not None
                record["last_active"] = prior.get("last_active", now)
                if output is None:
                    if "output_copies" in prior:
                        record["output_copies"] = prior["output_copies"]
        idle_seconds = now - float(cast(float, record["last_active"]))
        overdue = idle_seconds >= idle_hours * 3600
        if bool(record["term_sent"]) or overdue:
            escalate = bool(record["term_sent"])
            signum = signal.SIGKILL if escalate else signal.SIGTERM
            action = {
                "at": now,
                "pid": pid,
                "start_ticks": start_ticks,
                "agent_id": agent["agent_id"],
                "parent": agent["parent"],
                "idle_seconds": int(idle_seconds),
                "signal": "SIGKILL" if escalate else "SIGTERM",
                "moved_after_term": moved_after_term,
                "dry_run": dry_run,
            }
            if not dry_run:
                delivered = False
                signal_output: dict[str, object] | None = None
                signal_output_error = ""
                signal_vetoed = False

                def evidence_still_armed() -> bool:
                    nonlocal signal_output, signal_output_error, signal_vetoed
                    signal_output, signal_output_error = _output_snapshot(
                        agent, environ
                    )
                    signal_vetoed = not (
                        signal_output is not None
                        and signal_output.get("output_copies")
                        == record.get("output_copies")
                    )
                    return not signal_vetoed

                try:
                    if kill is not None:
                        delivered = _deliver_exact_process(
                            proc,
                            pid,
                            start_ticks,
                            signum,
                            still_expected=_still_the_worker,
                            send_signal=kill,
                            before_signal=evidence_still_armed,
                        )
                    else:
                        delivered = _deliver(
                            proc,
                            pid,
                            start_ticks,
                            signum,
                            before_signal=evidence_still_armed,
                        )
                except ProcessLookupError:
                    action["signal"] = "already-gone"
                except OSError as error:
                    action["signal"] = f"error:{error.errno}"
                if signal_vetoed and signal_output is not None:
                    # The copy set changed inside the guarded signal window.
                    # Observe that movement exactly as a normal pass would and
                    # require another stable pass before attempting delivery.
                    record = {
                        **signal_output,
                        "last_active": now,
                        "term_sent": committed,
                    }
                    if committed and isinstance(prior, dict):
                        record["last_active"] = prior.get("last_active", now)
                elif signal_vetoed and signal_output_error:
                    refusal = {
                        "at": now,
                        "pid": pid,
                        "start_ticks": start_ticks,
                        "agent_id": agent["agent_id"],
                        "parent": agent["parent"],
                        "decision": "refused-output",
                        "reason": signal_output_error,
                    }
                    _log(log_path, refusal)
                    if not committed:
                        # Unreadable is never an idle answer. A later readable
                        # pass must watch a fresh full window before TERM.
                        continue
                if signal_vetoed:
                    # A callback result means delivery was vetoed. It is not a
                    # signal action and must not commit TERM or enter the log.
                    next_tracked[key] = record
                    continue
                # An undelivered TERM is not a TERM: escalation to KILL must
                # always follow a signal that actually landed (X20-F5). A
                # failed KILL keeps term_sent so the next pass retries KILL.
                if delivered and not escalate:
                    record["term_sent"] = True
                _log(log_path, action)
            actions.append(action)
        next_tracked[key] = record
    actions.extend(
        _sweep_background_shells(
            proc=proc,
            tracked=tracked,
            next_tracked=next_tracked,
            boot_id=boot_id,
            now=now,
            idle_seconds_required=window_seconds,
            kill=kill,
            dry_run=dry_run,
            log_path=log_path,
        )
    )
    if not dry_run:
        _save_state(
            state_path,
            next_tracked,
            boot_id=boot_id,
            last_pass=now,
            window_seconds=window_seconds,
        )
    return actions


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sweep"])
    parser.add_argument("--proc", default="/proc")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    state_dir = Path(options.state_dir)
    if not state_dir.is_dir():
        print(f"subagent-sweep: no state directory: {state_dir}", file=sys.stderr)
        return 2
    actions = sweep(
        proc=Path(options.proc),
        state_dir=state_dir,
        environ=dict(os.environ),
        now=time.time(),
        dry_run=options.dry_run,
    )
    for action in actions:
        print(json.dumps(action, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
