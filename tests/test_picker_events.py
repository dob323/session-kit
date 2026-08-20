"""The picker learns from the daemon's push events, and survives losing them.

The list used to be re-collected every five seconds whether or not anything
had happened -- a whole status collection, in the background, forever, on an
estate that changes a few times an hour. The daemon publishes exactly the four
changes that move the list (created, attached, detached, removed; EVENTS.md),
so the picker subscribes and the timed collection becomes the net under a
stream rather than the way the picker learns.

Everything here is about what happens when that stream is NOT there:

  * the daemon drops subscribers that fall behind and never replays what they
    missed, so a stream that ends has to be answered by a full collection and a
    new subscription -- treating the silence as "nothing changed" is exactly
    the staleness this replaces;
  * a shpool without the subcommand, and an operator who turns events off, both
    have to land on the five-second poll that shipped before this, unchanged;
  * a subscription that cannot be kept has to stop trying and say so, rather
    than restarting a failing child under a person's prompt forever.

The stub shpool below is the whole event source: `events --help` answers the
capability probe, `events` prints event lines on a schedule and then behaves
the way the real daemon does with a subscriber it keeps.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest

from tests.test_login import (
    LOGIN,
    REPO,
    LoginFixture,
    inventory,
    row,
    run_pty,
    write_executable,
)


HELP_OK_STREAM = """#!/usr/bin/env bash
if [[ ${1:-} == events ]]; then
  if [[ ${2:-} == --help ]]; then
    echo "Subscribe to the daemon's push-event stream"
    exit 0
  fi
  # A real subscription: lines arrive when the table moves, and the stream
  # stays open in between. `exec` so the process the picker kills IS this one.
  printf '{"type":"session.created"}\\n'
  sleep 2.2
  printf '{"type":"session.removed"}\\n'
  exec sleep 30
fi
exit 0
"""

HELP_OK_STREAM_DIES = """#!/usr/bin/env bash
if [[ ${1:-} == events ]]; then
  if [[ ${2:-} == --help ]]; then
    echo "Subscribe to the daemon's push-event stream"
    exit 0
  fi
  # The daemon dropped this subscriber. No line, no replay, stream over.
  exit 0
fi
exit 0
"""

NO_EVENTS_SUBCOMMAND = """#!/usr/bin/env bash
if [[ ${1:-} == events ]]; then
  echo "error: unrecognized subcommand 'events'" >&2
  exit 2
fi
exit 0
"""


def two_rows() -> dict:
    return inventory(
        row("s20260813-000001-1", number=1, provider="claude"),
        row("s20260813-000002-2", number=2, provider="codex"),
    )


def picker_actions(fixture: LoginFixture, action: str) -> list[str]:
    log = fixture.state / "action-events.jsonl"
    if not log.exists():
        return []
    outcomes = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("action") == action:
            outcomes.append(record.get("outcome"))
    return outcomes


def collections(fixture: LoginFixture) -> int:
    if not fixture.status_log.exists():
        return 0
    return sum(1 for entry in fixture.status_entries() if entry == ["--json"])


def run_picker_with(
    fixture: LoginFixture,
    *,
    seconds: float,
    extra: dict[str, str],
    during=None,
    case: unittest.TestCase | None = None,
) -> str:
    """Hold the picker open, optionally doing something part-way through."""
    environment = {
        "SESSION_KIT_PICKER_REFRESH_SECONDS": "30",
        "SESSION_KIT_PICKER_EVENT_POLL_SECONDS": "300",
        "SESSION_KIT_PICKER_EVENTS": "auto",
    }
    environment.update(extra)
    if during is not None:
        timer = threading.Timer(1.6, during)
        timer.daemon = True
        timer.start()
        if case is not None:
            case.addCleanup(timer.cancel)
    _, output = run_pty(
        fixture,
        deferred=("❯ ", b"q\n"),
        deferred_delay_seconds=seconds,
        env_updates=environment,
    )
    return output


SLOW_STREAM = """#!/usr/bin/env bash
if [[ ${1:-} == events ]]; then
  if [[ ${2:-} == --help ]]; then
    echo "Subscribe to the daemon's push-event stream"
    exit 0
  fi
  exec sleep 30
fi
exit 0
"""

# A collection that takes real time, so an event can land in the middle of one.
SLOW_STATUS = """#!/usr/bin/env python3
import json,os,pathlib,sys,time
args=sys.argv[1:]
with pathlib.Path(os.environ["LOGIN_STATUS_LOG"]).open("a") as log:
    log.write(json.dumps(args)+"\\n")
if args == ["--json"]:
    counter=pathlib.Path(os.environ["LOGIN_SNAPSHOT_COUNT"])
    try: count=int(counter.read_text())
    except (OSError,ValueError): count=0
    count += 1
    counter.write_text(str(count))
    time.sleep(float(os.environ.get("LOGIN_SLOW_SECONDS","1.5")))
    source=(
        pathlib.Path(os.environ["LOGIN_REFRESHED_INVENTORY"])
        if count > 1
        else pathlib.Path(os.environ["LOGIN_INVENTORY"])
    )
    print(source.read_text(),end="")
elif args == ["--recovery-pending-list"]:
    print(pathlib.Path(os.environ["LOGIN_PENDING"]).read_text(),end="")
elif len(args) == 2 and args[0] == "--recovery-remember-printed":
    # Records what the screen is about to print; prints nothing itself.
    raise SystemExit(0)
else:
    raise SystemExit(2)
"""


class AttentionLatencyTests(unittest.TestCase):
    """The bound this whole item is allowed to exist under.

    The daemon's stream cannot carry "a session is waiting for a person" -- no
    create, no attach, no detach, no remove -- proven live: 25 s of
    `shpool events` over eleven busy sessions, zero lines. So widening the poll
    on the strength of that stream alone would make the one column an operator
    watches six times slower. Two rules keep the old bound:

      * the poll only widens while the attention watcher is ALSO live, and
      * an attention change is a pushed reason to collect, so it arrives in
        about a second rather than at the end of a poll window.
    """

    def setUp(self) -> None:
        self.fixture = LoginFixture(two_rows())
        self.addCleanup(self.fixture.close)

    def test_without_the_attention_watcher_the_poll_never_widens(self) -> None:
        """The guarantee, stated as the picker states it."""
        write_executable(self.fixture.fake_shpool, SLOW_STREAM)
        _, output = run_pty(
            self.fixture,
            deferred=("❯ ", b"q\n"),
            deferred_delay_seconds=3.4,
            env_updates={
                "SESSION_KIT_PICKER_EVENTS": "auto",
                "SESSION_KIT_PICKER_PULSE": "0",
                "SESSION_KIT_PICKER_REFRESH_SECONDS": "2",
                "SESSION_KIT_PICKER_EVENT_POLL_SECONDS": "300",
            },
        )
        # With the pulse off, the 300 s widened setting must be ignored
        # entirely: boot, the subscribe resync, and the 2 s poll still running.
        self.assertGreaterEqual(
            collections(self.fixture),
            3,
            f"the poll widened without an attention watcher: "
            f"{self.fixture.status_entries()}",
        )

    def test_an_attention_change_reaches_the_screen_inside_the_old_bound(
        self,
    ) -> None:
        """A hook record is written; a collection must follow within ~5 s."""
        write_executable(self.fixture.fake_shpool, SLOW_STREAM)
        records = self.fixture.state / "attention" / "claude"
        marks: list[float] = []

        def raise_a_hand() -> None:
            records.mkdir(parents=True, exist_ok=True)
            marks.append(time.monotonic())
            (records / "00000000-0000-4000-8000-000000000001.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": "00000000-0000-4000-8000-000000000001",
                        "hook_event": "Notification",
                        "notification_type": "idle_prompt",
                        "message": "",
                        "needs_you": True,
                        "recorded_at_ms": 1,
                    }
                ),
                encoding="utf-8",
            )

        before = collections(self.fixture)
        started = time.monotonic()
        run_picker_with(
            self.fixture,
            case=self,
            seconds=6.0,
            extra={
                "SESSION_KIT_PICKER_REFRESH_SECONDS": "30",
                "SESSION_KIT_PICKER_EVENT_POLL_SECONDS": "300",
                "SESSION_KIT_PICKER_PULSE_SECONDS": "0.5",
            },
            during=raise_a_hand,
        )
        self.assertTrue(marks, "the hand was never raised")
        # Boot + subscribe resync are the only collections the setup explains;
        # the poll is 30/300 s away. Anything beyond them came from the pulse,
        # and it happened inside the window this test ran in.
        self.assertGreaterEqual(
            collections(self.fixture),
            before + 3,
            "an attention change did not produce a collection: "
            f"{self.fixture.status_entries()}",
        )
        self.assertLess(time.monotonic() - started, 12)

    def test_an_event_during_a_collection_is_answered_by_a_later_one(
        self,
    ) -> None:
        """codex 1: a snapshot in flight may have read the table too early.

        The collection here takes 2 s and the event lands inside it, with a
        whole typing-loop tick still to come before it finishes. Without
        a trailing collection the change waits for the next poll -- 300 s away
        in this fixture, which is why call-counting alone could not see it.
        """
        write_executable(self.fixture.fake_shpool, SLOW_STREAM)
        write_executable(self.fixture.fake_status, SLOW_STATUS)
        events = self.fixture.base / "events-in-flight"

        def event_during_the_snapshot() -> None:
            # The subscriber is a stub `sleep`; the picker reads its stream
            # from the file, so writing a line there IS an event arriving.
            # It has to land INSIDE the collection the subscription's own
            # resync started -- that is the race being measured.
            #
            # The stream file appearing is not that moment. One subscription
            # creates the file and starts the resync, so a write fired on the
            # file alone can land a breath BEFORE the collection begins, and a
            # collection that starts after an event is already the answer to
            # it: nothing is deferred, no trailing collection is earned, and
            # this test fails having measured nothing. That is the 2026-08-18
            # ubuntu-22.04/3.12 failure, with the window already at 12 s.
            #
            # The stub status appends its arguments the instant it is called
            # and only then sleeps LOGIN_SLOW_SECONDS, so a second `--json`
            # in the log means the resync is running and has seconds left to
            # run. Wait for that, and the write is inside it by construction.
            deadline = time.monotonic() + 8
            stream = None
            while time.monotonic() < deadline:
                if stream is None:
                    for path in sorted(self.fixture.state.iterdir()):
                        if path.name.startswith("login-events.json."):
                            stream = path
                            break
                if stream is not None and collections(self.fixture) >= 2:
                    with stream.open("a", encoding="utf-8") as handle:
                        handle.write('{"type":"session.created"}\n')
                    events.write_text(stream.name, encoding="utf-8")
                    return
                time.sleep(0.05)

        run_picker_with(
            self.fixture,
            case=self,
            # Three 2 s collections have to finish inside this window; 7 s
            # left one second of slack and a loaded CI runner spent it, ending
            # the run with the trailing collection still in flight. The poll
            # is 300 s and the refresh 30 s away, so the extra headroom cannot
            # add a collection of its own.
            seconds=12.0,
            extra={
                "SESSION_KIT_PICKER_REFRESH_SECONDS": "30",
                "SESSION_KIT_PICKER_EVENT_POLL_SECONDS": "300",
                "SESSION_KIT_PICKER_PULSE": "0",
                "LOGIN_SLOW_SECONDS": "2.0",
            },
            during=event_during_the_snapshot,
        )
        self.assertTrue(events.exists(), "the test never found the event stream")
        # Boot, the subscribe resync (still running when the event lands), and
        # the trailing collection the event earned.
        self.assertGreaterEqual(
            collections(self.fixture),
            3,
            "an event that landed during a collection was swallowed: "
            f"{self.fixture.status_entries()}",
        )


class PushEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = LoginFixture(two_rows())
        self.addCleanup(self.fixture.close)

    def run_picker(self, *, seconds: float, extra: dict[str, str]) -> str:
        """Hold the picker open for a while, then leave it on purpose."""
        environment = {
            # Far longer than this test runs: any collection beyond the two the
            # subscription itself explains can only have come from an event.
            "SESSION_KIT_PICKER_REFRESH_SECONDS": "30",
            "SESSION_KIT_PICKER_EVENT_POLL_SECONDS": "300",
            "SESSION_KIT_PICKER_EVENTS": "auto",
        }
        environment.update(extra)
        _, output = run_pty(
            self.fixture,
            deferred=("❯ ", b"q\n"),
            deferred_delay_seconds=seconds,
            env_updates=environment,
        )
        return output

    def test_an_event_collects_without_waiting_for_the_poll(self) -> None:
        write_executable(self.fixture.fake_shpool, HELP_OK_STREAM)
        started = time.monotonic()
        self.run_picker(seconds=4.2, extra={})
        elapsed = time.monotonic() - started
        self.assertLess(
            elapsed,
            25,
            "the test itself must finish well inside one poll window",
        )
        self.assertEqual(["subscribed"], picker_actions(self.fixture, "picker_events"))
        # One collection at boot, one because subscribing resyncs, and one for
        # each of the two events. The poll is 30 seconds away and cannot have
        # produced any of them.
        self.assertGreaterEqual(
            collections(self.fixture),
            3,
            f"status calls: {self.fixture.status_entries()}",
        )

    def test_a_dropped_stream_resyncs_resubscribes_then_falls_back(self) -> None:
        write_executable(self.fixture.fake_shpool, HELP_OK_STREAM_DIES)
        self.run_picker(seconds=4.2, extra={})
        outcomes = picker_actions(self.fixture, "picker_events")
        self.assertEqual("subscribed", outcomes[0])
        self.assertIn("dropped", outcomes)
        self.assertIn("resubscribed_resync", outcomes)
        # Two subscriptions that end immediately are a broken stream, not a
        # race: the picker stops restarting it and says which mechanism it is
        # running on now.
        self.assertEqual("fallback_poll", outcomes[-1])
        self.assertLessEqual(
            outcomes.count("subscribed"),
            2,
            f"subscription restarted without a bound: {outcomes}",
        )

    def test_a_shpool_without_the_subcommand_keeps_the_timed_poll(self) -> None:
        write_executable(self.fixture.fake_shpool, NO_EVENTS_SUBCOMMAND)
        self.run_picker(
            seconds=1.2, extra={"SESSION_KIT_PICKER_REFRESH_SECONDS": "2"}
        )
        self.assertEqual(
            ["unsupported"], picker_actions(self.fixture, "picker_events")
        )
        self.assertEqual(
            [],
            [path for path in self.fixture.picker_temps() if "events" in path.name],
            "no subscriber may be started when the capability probe refuses",
        )

    def test_the_kill_switch_restores_the_five_second_poll_exactly(self) -> None:
        write_executable(self.fixture.fake_shpool, HELP_OK_STREAM)
        self.run_picker(
            seconds=2.6,
            extra={
                "SESSION_KIT_PICKER_EVENTS": "0",
                "SESSION_KIT_PICKER_REFRESH_SECONDS": "2",
            },
        )
        self.assertEqual([], picker_actions(self.fixture, "picker_events"))
        # The poll is the only mechanism left, and it still runs at its own
        # cadence rather than the widened one.
        self.assertGreaterEqual(collections(self.fixture), 2)

    def test_the_subscriber_process_is_gone_after_the_picker(self) -> None:
        """codex 9: unlinking the files is not the same as ending the child.

        The subscriber is a long-lived process holding a descriptor on a file
        the picker is about to delete. Counting files would still pass with the
        kill removed, so this counts processes.
        """
        write_executable(self.fixture.fake_shpool, HELP_OK_STREAM)
        before = subprocess.run(
            ["pgrep", "-fa", str(self.fixture.fake_shpool)],
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual("", before.strip(), "a stray stub was already running")
        self.run_picker(seconds=1.6, extra={})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            alive = subprocess.run(
                ["pgrep", "-fa", str(self.fixture.fake_shpool)],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if not alive:
                break
            time.sleep(0.2)
        self.assertEqual("", alive, f"the subscriber outlived the picker: {alive}")

    def test_the_subscriber_and_its_temp_files_are_gone_after_the_picker(
        self,
    ) -> None:
        write_executable(self.fixture.fake_shpool, HELP_OK_STREAM)
        self.run_picker(seconds=1.6, extra={})
        leftovers = [
            path for path in self.fixture.picker_temps() if "events" in path.name
        ]
        self.assertEqual([], leftovers, f"picker left event temps: {leftovers}")


PULSE_TOOL = REPO / "lib" / "sessionkit_inventory" / "pulse.py"


def _alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


class PulseLifetimeTests(unittest.TestCase):
    """The always-on child has to end with the picker in EVERY exit path.

    The picker stops it from its EXIT trap, and a trap is exactly what a
    SIGKILLed picker does not run -- an OOM kill of the picker's shell is this
    shape, and an OOM kill of that shell is what prompted this test. Nothing
    else ends the child: its stdout is the picker's event file, a REGULAR
    file, so no reader can go away and the BrokenPipeError handler at the foot
    of pulse.py is unreachable.

    Two things make this test see what the shipped ones could not:

      * it pgreps for `pulse.py` itself. The two tests above pgrep for the fake
        shpool stub, so the watcher was never looked at;
      * the terminal is HELD OPEN by a shell that outlives the picker, the way
        a real shpool session holds one when the picker is a subprocess of the
        session's shell. When the picker is itself the pty session leader,
        killing it hangs up the terminal and the kernel reaps the orphan --
        a property of the harness, not of the product.
    """

    def setUp(self) -> None:
        self.fixture = LoginFixture(two_rows())
        self.addCleanup(self.fixture.close)
        write_executable(self.fixture.fake_shpool, HELP_OK_STREAM)
        # A byte-for-byte copy of the shipped watcher, run from a path no other
        # process on this box can be running, so the pgrep below cannot match
        # somebody else's picker -- or a second copy of this suite.
        self.tool = self.fixture.base / "pulse.py"
        shutil.copyfile(PULSE_TOOL, self.tool)
        self.assertEqual(
            PULSE_TOOL.read_bytes(),
            self.tool.read_bytes(),
            "the copy under test is not the shipped watcher",
        )
        self.terminal_pid: int | None = None
        self.descriptor: int | None = None
        self.addCleanup(self.tear_down_processes)

    # -- the held-open terminal ---------------------------------------------
    def tear_down_processes(self) -> None:
        for pid in self.pulse_pids() + self.stub_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if self.terminal_pid is not None:
            try:
                os.kill(self.terminal_pid, signal.SIGKILL)
                os.waitpid(self.terminal_pid, 0)
            except OSError:
                pass
            self.terminal_pid = None
        if self.descriptor is not None:
            try:
                os.close(self.descriptor)
            except OSError:
                pass
            self.descriptor = None

    def pgrep(self, pattern: str) -> list[int]:
        completed = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True
        )
        return [int(line) for line in completed.stdout.split() if line.isdigit()]

    def pulse_pids(self) -> list[int]:
        return self.pgrep(os.fspath(self.tool))

    def stub_pids(self) -> list[int]:
        return self.pgrep(os.fspath(self.fixture.fake_shpool))

    def start_picker_under_a_held_open_terminal(self) -> int:
        """Start the picker as a CHILD of a shell that keeps the pty open."""
        environment = self.fixture.env()
        environment.update(
            {
                "SESSION_KIT_PICKER_EVENTS": "auto",
                "SESSION_KIT_PICKER_REFRESH_SECONDS": "30",
                "SESSION_KIT_PICKER_EVENT_POLL_SECONDS": "300",
                "SESSION_KIT_PICKER_PULSE_SECONDS": "0.3",
                "SESSION_KIT_PULSE_TOOL": os.fspath(self.tool),
            }
        )
        script = f"{shlex.quote(os.fspath(LOGIN))}; exec sleep 25"
        pid, descriptor = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.fixture.base)
                os.execve(
                    "/usr/bin/bash", ["/usr/bin/bash", "-c", script], environment
                )
            finally:
                os._exit(127)
        self.terminal_pid = pid
        self.descriptor = descriptor
        self.wait_for_the_menu()
        return pid

    def wait_for_the_menu(self) -> None:
        output = bytearray()
        deadline = time.monotonic() + 12
        while b"\xe2\x9d\xaf " not in output and time.monotonic() < deadline:
            ready, _, _ = select.select([self.descriptor], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(self.descriptor, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
            if not chunk:
                break
            output.extend(chunk)
        if b"\xe2\x9d\xaf " not in output:
            raise AssertionError(
                "the picker never painted under the held-open terminal: "
                f"{output.decode(errors='replace')!r}"
            )

    def wait_for_the_pulse(self) -> int:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            pids = self.pulse_pids()
            if len(pids) == 1:
                return pids[0]
            self.assertLessEqual(len(pids), 1, f"more than one watcher: {pids}")
            time.sleep(0.1)
        raise AssertionError("the picker never started its attention watcher")

    # -- the test ------------------------------------------------------------
    def test_the_watcher_dies_with_a_picker_that_was_killed_outright(self) -> None:
        terminal = self.start_picker_under_a_held_open_terminal()
        pulse = self.wait_for_the_pulse()
        children = subprocess.run(
            ["pgrep", "-P", str(terminal)], capture_output=True, text=True
        ).stdout.split()
        self.assertEqual(1, len(children), f"expected one picker: {children}")
        picker = int(children[0])
        self.assertNotEqual(picker, pulse)
        # Five intervals with the picker alive and well: the parent check must
        # not fire on a picker that is still there, or the push half of the
        # attention answer is dead and the poll silently carries it alone.
        time.sleep(5 * 0.3)
        self.assertTrue(
            _alive(pulse), "the watcher stopped while its picker was running"
        )

        os.kill(picker, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while _alive(pulse) and time.monotonic() < deadline:
            time.sleep(0.1)
        # The terminal is the whole point: it is still open, so no SIGHUP
        # reached the orphan and nothing but the watcher itself can have
        # ended it.
        self.assertTrue(
            _alive(terminal),
            "the terminal closed, so this proves nothing about the watcher",
        )
        self.assertFalse(
            _alive(pulse),
            "the attention watcher outlived a SIGKILLed picker: "
            + subprocess.run(
                ["ps", "-o", "pid=,ppid=,args=", "-p", str(pulse)],
                capture_output=True,
                text=True,
            ).stdout.strip(),
        )

    def test_the_watcher_still_stops_when_the_picker_leaves_on_purpose(self) -> None:
        """The trap-based stop is not replaced by the parent check."""
        self.start_picker_under_a_held_open_terminal()
        pulse = self.wait_for_the_pulse()
        os.write(self.descriptor, b"q\n")
        deadline = time.monotonic() + 10
        while _alive(pulse) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(_alive(pulse), "a clean quit left the watcher running")


# The reaper's own comment: "A label absent from this list has no backstop at
# all." These two labels are written by the event subscription for as long as
# the picker runs, and the picker that leaves them behind is the one that ran
# no trap -- so the backstop is the only thing left.
REAPER = REPO / "bin" / "shpool_reaper"

STUB_SHPOOL_LIST = """#!/usr/bin/env bash
if [[ ${1:-} == list ]]; then
  echo '{"sessions": []}'
  exit 0
fi
exit 0
"""


class EventTempReaperTests(unittest.TestCase):
    """Every temp label the picker writes has to be in the hourly sweep."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".events-reap-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        os.chmod(self.state, 0o700)
        self.proc = self.base / "proc"
        self.proc.mkdir()
        self.shpool = self.base / "fake-shpool"
        write_executable(self.shpool, STUB_SHPOOL_LIST)

    def aged_temp(self, name: str) -> Path:
        path = self.state / name
        path.write_text("{}\n", encoding="utf-8")
        os.chmod(path, 0o600)
        old = time.time() - 25 * 60 * 60
        os.utime(path, (old, old))
        return path

    def test_the_pickers_event_temp_files_are_reaped_like_every_other_label(
        self,
    ) -> None:
        events = self.aged_temp("login-events.json.Ab12Cd")
        down = self.aged_temp("login-events-down.json.Ab12Cd")
        # A label that was already in the list, aged the same way: if the sweep
        # did not run at all, this one would survive too and the failure below
        # would be about the harness rather than about the labels.
        control = self.aged_temp("login-live.json.Ab12Cd")
        fresh = self.state / "login-events.json.Zz99Yy"
        fresh.write_text("{}\n", encoding="utf-8")
        os.chmod(fresh, 0o600)

        environment = os.environ.copy()
        environment.update(
            {
                "HOME": os.fspath(self.base),
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_STATE_DIR": os.fspath(self.state),
                "SESSION_KIT_PROC_ROOT": os.fspath(self.proc),
                "SESSION_KIT_SHPOOL_CMD": os.fspath(self.shpool),
                "SESSION_KIT_REAPER_SENTINEL": os.fspath(self.base / "no-sentinel"),
            }
        )
        subprocess.run(
            [os.fspath(REAPER)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertFalse(control.exists(), "the sweep did not run at all")
        self.assertFalse(events.exists(), "login-events has no reaper backstop")
        self.assertFalse(
            down.exists(), "login-events-down has no reaper backstop"
        )
        # The 24-hour cutoff still protects a picker that is running now.
        self.assertTrue(fresh.exists(), "the sweep took a file a live picker owns")


SWITCH_PROBE = r"""
source "$LOGIN_LIVE_MODULE"
PICKER_SCREEN=1
SK_SHPOOL=$FAKE_SHPOOL
PICKER_EVENTS_FILE=$EVENTS_FILE
events=off
pulse=off
if picker_events_wanted; then
  events=on
  PICKER_EVENTS_STATE=live
  if picker_pulse_start; then pulse=on; fi
fi
picker_live_interval 5
printf '%s %s %s\n' "$events" "$pulse" "$PICKER_LIVE_INTERVAL"
picker_pulse_stop
"""

PULSE_STUB = """#!/usr/bin/env python3
import time

time.sleep(20)
"""


class KillSwitchSpellingTests(unittest.TestCase):
    """The documented rollback levers, driven as the picker drives them.

    `SESSION_KIT_PICKER_EVENTS` and `SESSION_KIT_PICKER_PULSE` are what an
    operator reaches for when a new always-on child misbehaves. Matched
    against bare lowercase patterns, `OFF` left both of them ON and said
    nothing -- and doctor's kill-switches row does not know these variables,
    so nothing else would have said so either. Every switch this round added
    on the Python side already reads `.strip().casefold()`.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".switch-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.events_file = self.base / "events"
        self.events_file.write_text("", encoding="utf-8")
        self.shpool = self.base / "fake-shpool"
        write_executable(self.shpool, "#!/usr/bin/env bash\nexit 0\n")
        # Never the real watcher: this probe is about the switch, and a stub
        # keeps a stray process out of the suite.
        self.tool = self.base / "pulse-stub.py"
        write_executable(self.tool, PULSE_STUB)

    def probe(self, **switches: str) -> tuple[str, str, str]:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LOGIN_LIVE_MODULE": os.fspath(
                REPO / "lib" / "sh" / "shpool_login_live.sh"
            ),
            "FAKE_SHPOOL": os.fspath(self.shpool),
            "EVENTS_FILE": os.fspath(self.events_file),
            "SESSION_KIT_PULSE_TOOL": os.fspath(self.tool),
            "SESSION_KIT_PICKER_EVENT_POLL_SECONDS": "300",
        }
        environment.update(switches)
        completed = subprocess.run(
            ["bash", "-c", SWITCH_PROBE],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        events, pulse, interval = completed.stdout.split()[:3]
        return events, pulse, interval

    def test_with_no_switch_set_both_children_run_and_the_poll_widens(self) -> None:
        """The control: this is what the two spellings below have to undo."""
        self.assertEqual(("on", "on", "300"), self.probe())

    def test_the_pulse_switch_is_read_the_way_it_is_typed(self) -> None:
        for spelling in ("0", "off", "no", "false", "OFF", "Off", " off ", "FALSE"):
            with self.subTest(spelling=spelling):
                events, pulse, interval = self.probe(
                    SESSION_KIT_PICKER_PULSE=spelling
                )
                self.assertEqual("on", events, "only the pulse was switched off")
                self.assertEqual("off", pulse, f"{spelling!r} did not stop the pulse")
                self.assertEqual(
                    "5",
                    interval,
                    f"{spelling!r} left the poll widened past the old bound",
                )

    def test_the_events_switch_is_read_the_way_it_is_typed(self) -> None:
        for spelling in ("0", "off", "no", "false", "OFF", "Off", " off ", "NO"):
            with self.subTest(spelling=spelling):
                events, pulse, interval = self.probe(
                    SESSION_KIT_PICKER_EVENTS=spelling
                )
                self.assertEqual(
                    "off", events, f"{spelling!r} did not stop the subscription"
                )
                self.assertEqual("off", pulse)
                self.assertEqual(
                    "5",
                    interval,
                    f"{spelling!r} left the poll widened past the old bound",
                )

    def test_a_value_that_is_not_an_off_word_still_leaves_them_on(self) -> None:
        """Normalising is not the same as guessing: only off means off."""
        for spelling in ("auto", "1", "on", "yes", "offline"):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    ("on", "on", "300"),
                    self.probe(
                        SESSION_KIT_PICKER_EVENTS=spelling,
                        SESSION_KIT_PICKER_PULSE=spelling,
                    ),
                )


PULSE_VALUE_PROBE = r"""
source "$LOGIN_LIVE_MODULE"
PICKER_SCREEN=1
SK_SHPOOL=$FAKE_SHPOOL
PICKER_EVENTS_FILE=$EVENTS_FILE
PICKER_EVENTS_STATE=live
pulse=off
if picker_pulse_start; then pulse=on; fi
# The picker asks for the interval once per loop pass. A watcher that died has
# to be noticed by one of those passes, so this is driven the same way rather
# than asked once at start-up.
ticks=${PROBE_TICKS:-10}
for (( i = 0; i < ticks; i++ )); do
  picker_live_interval 5
  if [[ $PICKER_LIVE_INTERVAL == 5 ]]; then break; fi
  sleep 0.1
done
printf '%s %s %s\n' "$pulse" "$PICKER_LIVE_INTERVAL" "$PICKER_PULSE_STATE"
picker_pulse_stop
"""

# argparse the same way pulse.py does it: --interval is a float, and a value it
# cannot read exits 2 before anything is watched. The argv is recorded FIRST so
# the test can see what the picker actually passed, parseable or not.
ARGV_PULSE_STUB = """#!/usr/bin/env python3
import argparse
import os
import sys
import time

with open(os.environ["PULSE_ARGV_FILE"], "w", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]))
parser = argparse.ArgumentParser()
parser.add_argument("--interval", type=float, default=1.0)
parser.add_argument("--parent-pid", type=int)
parser.parse_args()
time.sleep(20)
"""

# A watcher that cannot start, for any reason at all.
DEAD_PULSE_STUB = """#!/usr/bin/env python3
import sys

sys.exit(2)
"""


class PulseIntervalTests(unittest.TestCase):
    """The poll may never widen behind a watcher that is not running.

    `pulse.py --interval abc` exits 2 inside argparse in milliseconds. The
    value went into the child's argv unchecked and the state was declared
    `live` regardless, so one typo in one environment variable took the
    attention watcher away AND left the poll widened to 30 seconds (300 with
    the documented widening set) -- with nothing on screen, nothing in the log
    and nothing in doctor to say so. The direction of that failure is the one
    this round exists to prevent: "needs your reply" went from about a second
    back to half a minute.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".pulse-value-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.events_file = self.base / "events"
        self.events_file.write_text("", encoding="utf-8")
        self.shpool = self.base / "fake-shpool"
        write_executable(self.shpool, "#!/usr/bin/env bash\nexit 0\n")
        self.argv_file = self.base / "argv"
        self.tool = self.base / "pulse-stub.py"
        write_executable(self.tool, ARGV_PULSE_STUB)

    def probe(self, value=None, *, ticks="1") -> tuple[str, str, str]:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LOGIN_LIVE_MODULE": os.fspath(
                REPO / "lib" / "sh" / "shpool_login_live.sh"
            ),
            "FAKE_SHPOOL": os.fspath(self.shpool),
            "EVENTS_FILE": os.fspath(self.events_file),
            "SESSION_KIT_PULSE_TOOL": os.fspath(self.tool),
            "SESSION_KIT_PICKER_EVENT_POLL_SECONDS": "300",
            "PULSE_ARGV_FILE": os.fspath(self.argv_file),
            "PROBE_TICKS": ticks,
        }
        if value is not None:
            environment["SESSION_KIT_PICKER_PULSE_SECONDS"] = value
        completed = subprocess.run(
            ["bash", "-c", PULSE_VALUE_PROBE],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        pulse, interval, state = completed.stdout.split()[:3]
        return pulse, interval, state

    def argv(self) -> str:
        # The watcher is a separate process, so its argv file appears after the
        # probe returns. Waiting five seconds was enough on an idle machine and
        # not on a loaded CI runner, and the old timeout returned an empty
        # string, so the caller's assertion blamed the interval value for a
        # watcher that had simply not started yet (2026-08-20). Wait longer,
        # require the write to be complete, and say what actually went wrong.
        budget = 30
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                recorded = self.argv_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                recorded = ""
            if recorded.strip():
                return recorded
            time.sleep(0.05)
        raise AssertionError(
            f"the watcher wrote no argv to {self.argv_file} within {budget}s; "
            "it never started, so nothing here is a statement about its arguments"
        )

    def test_a_value_argparse_cannot_read_falls_back_to_the_default(self) -> None:
        marker = self.base / "pwn"
        values = ("abc", "", "   ", "1e", "0x2", "1;2", "-1", f"$(touch {marker})")
        for value in values:
            with self.subTest(value=value):
                self.argv_file.unlink(missing_ok=True)
                pulse, interval, state = self.probe(value)
                self.assertIn(
                    "--interval 1",
                    self.argv(),
                    f"{value!r} reached the watcher's argv",
                )
                self.assertEqual("on", pulse, f"{value!r} stopped the watcher")
                self.assertEqual("live", state)
                self.assertEqual(
                    "300", interval, "the watcher runs, so the poll may widen"
                )
                # Quoted, and it stays quoted: the value is data, never a
                # command. (Proven separately by the security lane; kept here
                # because this is the function that interpolates it.)
                self.assertFalse(marker.exists())

    def test_a_value_it_can_read_is_passed_through_unchanged(self) -> None:
        for value in ("0.5", "2", ".25", " 0.5 "):
            with self.subTest(value=value):
                self.argv_file.unlink(missing_ok=True)
                pulse, interval, state = self.probe(value)
                self.assertIn(f"--interval {value.strip()}", self.argv())
                self.assertEqual(("on", "300", "live"), (pulse, interval, state))

    def test_with_the_variable_unset_the_default_is_still_one_second(self) -> None:
        pulse, interval, state = self.probe()
        self.assertIn("--interval 1", self.argv())
        self.assertEqual(("on", "300", "live"), (pulse, interval, state))

    def test_a_watcher_that_cannot_start_never_leaves_the_poll_widened(self) -> None:
        """The guarantee itself, driven against a watcher that dies at once.

        A bad interval can no longer produce this, but a missing interpreter,
        an import error or an OOM kill still can, and the answer has to be the
        same: the poll goes back to the cadence that finds a waiting session
        on its own rather than staying widened over nothing.
        """
        write_executable(self.tool, DEAD_PULSE_STUB)
        _pulse, interval, state = self.probe("1", ticks="30")
        self.assertEqual(
            "5", interval, "the poll stayed widened behind a dead watcher"
        )
        self.assertNotEqual("live", state)


if __name__ == "__main__":
    unittest.main()
