"""Clear what an interrupted run left behind, before the next run starts.

Fixtures build their sandbox with `tempfile.TemporaryDirectory(dir=REPO)` and
rely on interpreter shutdown to remove it. That covers a clean exit and a
KeyboardInterrupt; it does not cover SIGKILL, a harness timeout, or a traceback
that keeps the fixture alive. And some suites start a REAL provider CLI inside
the sandbox: two `claude` processes were found alive one day and twenty hours
after the run that spawned them, holding a conversation id with their sandbox
working directory deleted underneath them. The crontab orphan reaper missed
them because it requires `ppid == 1` and these had reparented to
`systemd --user`.

So the sweep is by sandbox, not by parent: any same-user process whose working
directory is inside a stale sandbox goes with the sandbox. Nothing younger than
the grace period is touched, so a run in flight -- including a parallel one --
is never disturbed.

Run for its effect, from tests/run. It prints only what it actually removed.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import sys
import time

REPO = Path(__file__).resolve().parents[1]
# Prefixes the fixtures use for their sandboxes, all of them dotted so
# .gitignore's `/.*/` already keeps them out of a commit.
PREFIXES = (
    ".login-",
    ".commands-",
    ".msg-cli-",
    ".account-",
    ".projects-",
    # tests/sandbox_guard.py builds one per run and removes it at interpreter
    # exit; a SIGKILLed run leaves it behind like any other sandbox.
    ".sandbox-guard-",
)
# Long enough that no live run is ever in range, short enough that a leak costs
# one run rather than a week. The override exists so this file can be tested
# against a sandbox created a moment ago.
GRACE_SECONDS = int(os.environ.get("SESSION_KIT_SWEEP_GRACE_SECONDS") or 2 * 3600)


def stale_sandboxes() -> list[Path]:
    cutoff = time.time() - GRACE_SECONDS
    found = []
    for entry in REPO.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        if not entry.name.startswith(PREFIXES):
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        found.append(entry)
    return sorted(found)


def in_a_sandbox(cwd: str) -> bool:
    """Is this working directory inside a fixture sandbox, live or deleted?

    The kernel appends " (deleted)" once the directory is gone, and gone is the
    normal case: the fixture's own cleanup removes the sandbox and the process it
    started survives with a working directory that no longer exists. That is the
    pair the audit found still running after a day and twenty hours.
    """
    if cwd.endswith(" (deleted)"):
        cwd = cwd[: -len(" (deleted)")]
    try:
        relative = Path(cwd).relative_to(REPO)
    except ValueError:
        return False
    head = relative.parts[0] if relative.parts else ""
    return head.startswith(PREFIXES)


def process_age_seconds(pid: str) -> float | None:
    """How long this process has been running, from its own start time.

    The /proc entry's mtime is not the start time -- the kernel refreshes it --
    so the age comes from field 22 of /proc/<pid>/stat against system uptime.
    That is the only age a process whose sandbox is already gone can be judged
    by, and the judgement has to be exact: it decides what gets killed.
    """
    try:
        with open("/proc/uptime", encoding="utf-8") as handle:
            uptime = float(handle.read().split()[0])
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            fields = handle.read()
    except (OSError, ValueError, IndexError):
        return None
    # The command name is parenthesised and may contain spaces: split after it.
    try:
        rest = fields[fields.rindex(")") + 1 :].split()
        started_ticks = float(rest[19])
    except (ValueError, IndexError):
        return None
    ticks = os.sysconf("SC_CLK_TCK") or 100
    return max(0.0, uptime - started_ticks / ticks)


def leaked_processes(stale: list[Path]) -> list[tuple[int, str]]:
    """Same-user pids that cannot belong to a live run, inside a sandbox.

    Two ways to be sure. A process sitting in a sandbox this sweep has already
    judged stale goes with it whatever its own age. Otherwise the sandbox is
    gone -- the fixture removed it and the process outlived it -- and the
    process must be older than the grace period before anything is killed.
    """
    uid = os.geteuid()
    condemned = [os.fspath(root) for root in stale]
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        base = f"/proc/{name}"
        try:
            if os.stat(base, follow_symlinks=False).st_uid != uid:
                continue
            cwd = os.readlink(f"{base}/cwd")
        except OSError:
            continue
        if not in_a_sandbox(cwd):
            continue
        plain = cwd[: -len(" (deleted)")] if cwd.endswith(" (deleted)") else cwd
        inside_stale = any(
            plain == root or plain.startswith(root + os.sep) for root in condemned
        )
        if not inside_stale:
            age = process_age_seconds(name)
            if age is None or age < GRACE_SECONDS:
                continue
        try:
            with open(f"{base}/cmdline", "rb") as handle:
                command = handle.read(200).replace(b"\0", b" ").decode(
                    "utf-8", "replace"
                ).strip()
        except OSError:
            command = "?"
        out.append((int(name), command))
    return out


def main() -> int:
    roots = stale_sandboxes()
    leaked = leaked_processes(roots)
    if not roots and not leaked:
        return 0
    for pid, command in leaked:
        # The process group: a provider CLI runs under `script`, and killing the
        # wrapper alone leaves the CLI holding its conversation.
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        print(f"tests: ended leaked process {pid} ({command[:60]})")
    if leaked:
        time.sleep(0.5)
        for pid, _command in leaked:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
    for root in roots:
        shutil.rmtree(root, ignore_errors=True)
        print(f"tests: removed stale sandbox {root.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
