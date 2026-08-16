"""A budget for the two costs the operator feels, asserted on a pty.

"It flashes" and "typing feels laggy" survived several fix attempts because
neither was a number. These are numbers, measured the way a terminal
experiences them:

  * **the blank interval** -- how long a cleared screen is exposed before the
    frame that replaces it arrives. The picker computes a frame first and emits
    it with its own erase in one write, so this is bounded by one write; the
    erase-first shape it replaced left the screen empty for 122 ms on every
    keystroke and seconds at launch.
  * **the frame's subprocess count** -- one home frame used to start eight
    python interpreters over the same file, ~105 ms of which under 4 ms was
    real work, plus a whole `shpool_status` in the foreground of the typing
    loop for one integer the background collector already had.

The budgets are the measured post-fix numbers with headroom, so an honest
improvement never fails and a regression to either shape does.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import sys
import tempfile
import termios
import time
import unittest

from tests.support import REPO
from tests.test_login import LOGIN, LoginFixture, inventory, row

sys.path.insert(0, os.fspath(REPO / "lib"))
from sessionkit_inventory import pulse  # noqa: E402

SHIM = Path(__file__).resolve().parent / "latency" / "shim"
SHIM_PPID = Path(__file__).resolve().parent / "latency" / "shim-ppid"

# Measured on a 32-core box under a load average near 20, at 3/20/60 rows:
# frame subprocesses 5-6, blank interval under 1 ms (one write). The budgets
# leave room for a slower machine without leaving room for the old shapes.
FRAME_SUBPROCESS_BUDGET = 8
BLANK_INTERVAL_BUDGET_MS = 60.0
TYPING_STALL_BUDGET_MS = 400.0

ERASE = re.compile(rb"\x1b\[2J|\x1b\[H\x1b\[J|\x1b\[J")
PRINTABLE = re.compile(rb"[\x20-\x7e]")


def rows(count: int) -> list[dict]:
    names = "alpha bravo charlie delta echo foxtrot golf hotel india juliet".split()
    return [
        row(
            f"{names[index % len(names)]}{index // len(names)}",
            number=index + 1,
            provider=("claude", "codex", "shell")[index % 3],
            needs_you=(index % 7 == 0),
        )
        for index in range(count)
    ]


class Session:
    """One picker on a real pty, with byte arrival times."""

    def __init__(self, fixture: LoginFixture, updates: dict[str, str]) -> None:
        environment = fixture.env(lines=24, columns=100)
        # The real ioctl path, not a pinned size: a frame laid out for a
        # fallback terminal is its own defect (rows wrapped at 99 columns on
        # every width), and pinning these is what hid it.
        for name in ("COLUMNS", "LINES"):
            environment.pop(name, None)
        environment.update(updates)
        self.start = time.monotonic()
        pid, descriptor = pty.fork()
        if pid == 0:
            try:
                os.chdir(fixture.base)
                os.execve(LOGIN, [os.fspath(LOGIN)], environment)
            finally:
                os._exit(127)
        fcntl.ioctl(
            descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0)
        )
        self.pid = pid
        self.descriptor = descriptor
        self.events: list[tuple[float, bytes]] = []
        self.seen = bytearray()

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            ready, _, _ = select.select(
                [self.descriptor], [], [], min(0.02, remaining)
            )
            if not ready:
                continue
            try:
                chunk = os.read(self.descriptor, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                return
            if not chunk:
                return
            self.events.append((time.monotonic() - self.start, chunk))
            self.seen.extend(chunk)

    def wait_for(self, marker: bytes, seconds: float = 10.0) -> None:
        end = time.monotonic() + seconds
        while marker not in self.seen:
            if time.monotonic() > end:
                raise AssertionError(
                    f"picker never printed {marker!r}; "
                    f"output={bytes(self.seen).decode(errors='replace')!r}"
                )
            self.pump(0.05)

    def write(self, payload: bytes) -> float:
        os.write(self.descriptor, payload)
        return time.monotonic() - self.start

    def close(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except OSError:
            pass
        os.close(self.descriptor)

    def blank_intervals(self, after: float) -> list[float]:
        """Milliseconds between an erase and the next visible byte after it."""
        out: list[float] = []
        pending: float | None = None
        for offset, chunk in self.events:
            if offset < after:
                continue
            has_erase = ERASE.search(chunk) is not None
            visible = PRINTABLE.search(ERASE.sub(b"", chunk)) is not None
            if has_erase and visible:
                # Erase and replacement in one write: nothing was ever blank.
                pending = None
                continue
            if has_erase:
                pending = offset
                continue
            if pending is not None and visible:
                out.append(round(1000 * (offset - pending), 2))
                pending = None
        return out


def frame_subprocesses(log: Path, after: float, before: float) -> list[str]:
    if not log.exists():
        return []
    calls = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        stamp, _, rest = line.partition("\t")
        try:
            when = float(stamp)
        except ValueError:
            continue
        if after <= when <= before:
            calls.append(rest)
    return calls


class LatencyBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log = Path(os.environ.get("TMPDIR", "/tmp")) / (
            f"session-kit-shim-{os.getpid()}.log"
        )
        if self.log.exists():
            self.log.unlink()

    def tearDown(self) -> None:
        if self.log.exists():
            self.log.unlink()

    def shim_env(self) -> dict[str, str]:
        return {
            "PATH": f"{SHIM}:{os.environ['PATH']}",
            "SK_SHIM_LOG": str(self.log),
            "SESSION_KIT_PICKER_REFRESH_SECONDS": "0",
        }

    def test_one_home_frame_stays_inside_its_subprocess_budget(self) -> None:
        for count in (3, 20, 60):
            with self.subTest(rows=count):
                if self.log.exists():
                    self.log.unlink()
                fixture = LoginFixture(inventory(*rows(count)))
                session = Session(fixture, self.shim_env())
                try:
                    session.wait_for("❯".encode())
                    session.pump(0.4)
                    before = time.time()
                    session.write(b"zzz\n")
                    session.wait_for(b"no such key")
                    session.pump(0.3)
                    after = time.time()
                finally:
                    session.close()
                    fixture.close()
                calls = frame_subprocesses(self.log, before, after)
                self.assertLessEqual(
                    len(calls),
                    FRAME_SUBPROCESS_BUDGET,
                    msg=f"{count} rows: one keystroke started {calls}",
                )

    def test_a_keystroke_never_leaves_the_screen_blank(self) -> None:
        fixture = LoginFixture(inventory(*rows(20)))
        session = Session(fixture, {"SESSION_KIT_PICKER_REFRESH_SECONDS": "0"})
        try:
            session.wait_for("❯".encode())
            session.pump(0.4)
            typed = session.write(b"zzz\n")
            session.wait_for(b"no such key")
            session.pump(0.3)
            intervals = session.blank_intervals(typed)
        finally:
            session.close()
            fixture.close()
        for interval in intervals:
            self.assertLess(
                interval,
                BLANK_INTERVAL_BUDGET_MS,
                msg=f"blank intervals after one keystroke: {intervals}",
            )

    def test_boot_never_leaves_the_screen_blank(self) -> None:
        fixture = LoginFixture(inventory(*rows(20)))
        session = Session(fixture, {"SESSION_KIT_PICKER_REFRESH_SECONDS": "0"})
        try:
            session.wait_for("open ".encode())
            session.pump(0.3)
            intervals = session.blank_intervals(0.0)
        finally:
            session.close()
            fixture.close()
        for interval in intervals:
            self.assertLess(
                interval,
                BLANK_INTERVAL_BUDGET_MS,
                msg=f"blank intervals during boot: {intervals}",
            )

    def test_typing_stays_visible_across_a_refresh(self) -> None:
        """Nothing the refresh does may sit between a keystroke and its echo.

        The picker echoes each character itself in `-echo` mode, so anything it
        runs in the typing loop is invisible typing. A whole `shpool_status`
        used to run there on every adopted refresh -- 150 ms of dead keyboard
        every five seconds -- for one integer the background collector already
        had. Only the pending query is slowed here, which is the real one's
        shape: a whole process for one number.
        """
        fixture = LoginFixture(inventory(*rows(20)))
        slow = fixture.base / "slow-status"
        slow.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ ${1:-} == --recovery-pending-list ]]; then sleep 1; fi\n'
            f"exec {fixture.fake_status} \"$@\"\n",
            encoding="utf-8",
        )
        slow.chmod(0o755)
        session = Session(
            fixture,
            {
                "SESSION_KIT_PICKER_REFRESH_SECONDS": "2",
                "SESSION_KIT_STATUS_CMD": str(slow),
            },
        )
        # Characters the picker's own frames never print, and that no escape
        # sequence contains: a marker already on the screen would read as an
        # echo that never happened.
        typed: dict[str, float] = {}
        try:
            session.wait_for("❯".encode())
            # Past the opening canonical second, so the picker owns the echo,
            # and long enough to cross two refresh cycles while typing.
            session.pump(1.2)
            for key in "ZQWYVGBAEFIMTU":
                typed[key] = session.write(key.encode())
                # Slower than the read timeout on purpose: a loop that never
                # times out never reaches its refresh, and the stall this
                # measures lives in the refresh.
                session.pump(0.6)
            session.pump(6.0)
            delays = {}
            for key, sent in typed.items():
                echoed = [
                    offset
                    for offset, chunk in session.events
                    if key.encode() in chunk and offset >= sent - 0.01
                ]
                self.assertTrue(
                    echoed, msg=f"{key!r} typed at {sent:.2f}s was never echoed"
                )
                delays[key] = 1000 * (echoed[0] - sent)
        finally:
            session.close()
            fixture.close()
        worst = max(delays.values())
        self.assertLess(
            worst,
            TYPING_STALL_BUDGET_MS,
            msg=f"echo delays in ms: { {k: round(v, 1) for k, v in delays.items()} }",
        )

class ResizeTests(unittest.TestCase):
    def test_a_resize_repaints_a_quiet_list(self) -> None:
        """Geometry is not session content, so no fingerprint can see it.

        The repaint fingerprint hashes row fields on purpose -- an unchanged
        estate must not repaint -- which left a resized window holding the old
        layout until a key was pressed. On a quiet estate that is forever: rows
        wrapped, or half a screen of empty space, with the prompt scrolled off
        after a shrink.
        """
        fixture = LoginFixture(inventory(*rows(8)))
        session = Session(fixture, {"SESSION_KIT_PICKER_REFRESH_SECONDS": "2"})
        try:
            session.wait_for("open ".encode())
            # Past the opening canonical second, with nothing typed and nothing
            # changing in the estate.
            session.pump(1.4)
            before = bytes(session.seen).count(b"\x1b[H")
            fcntl.ioctl(
                session.descriptor,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", 24, 50, 0, 0),
            )
            os.kill(session.pid, signal.SIGWINCH)
            session.pump(1.5)
            after = bytes(session.seen).count(b"\x1b[H")
        finally:
            session.close()
            fixture.close()
        self.assertGreater(after, before, msg="the resize drew no new frame")


# One pass of the attention watcher, at 1 Hz, next to a pathological
# directory. Measured on one machine: 8.6 ms with the listing bounded, 512 ms
# with it unbounded (the shipped `sorted(dir.glob("*.json"))[:cap]` reads and
# sorts every entry before the cap throws most of them away). The budget is
# far above the first and far below the second, so a slower machine passes and
# the unbounded shape cannot.
PULSE_PASS_BUDGET_MS = 200.0
PULSE_PATHOLOGICAL_ENTRIES = 60_000


class PulsePassCostTests(unittest.TestCase):
    """The watcher's own docstring says everything is bounded. Make it true.

    A session directory is not only session records: the kit's own markers
    accumulate next to them (315 .nameintent, 196 .colorset, 182 .titleset
    were sitting in one live ambient directory when this was written, of
    705 entries, only 2 of which were the .json this watches). The cap on how
    many files are FINGERPRINTED hid that, because it kept `watched_paths`
    flat while the listing underneath it grew without a bound.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".pulse-cost-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        (self.home / ".claude" / "sessions").mkdir(parents=True)
        self.environ = {
            "SESSION_KIT_STATE_DIR": os.fspath(self.base / "state"),
            "SESSION_KIT_ACCOUNT_ROOT": os.fspath(self.base / "accounts"),
            "CODEX_HOME": os.fspath(self.base / "codex"),
            "FLEET_STATE_DIR": os.fspath(self.base / "fleet"),
        }

    def fill(self, count: int, suffix: str) -> None:
        sessions = self.home / ".claude" / "sessions"
        for index in range(count):
            (sessions / f"{index:07d}{suffix}").touch()

    def fastest_pass_ms(self) -> float:
        best = []
        for _ in range(3):
            started = time.monotonic()
            paths = pulse.watched_paths(self.environ, self.home)
            best.append((time.monotonic() - started) * 1000)
        self.paths = paths
        return min(best)

    def test_a_pathological_session_directory_cannot_eat_the_loop(self) -> None:
        self.fill(PULSE_PATHOLOGICAL_ENTRIES, ".json")
        elapsed = self.fastest_pass_ms()
        self.assertLess(
            elapsed,
            PULSE_PASS_BUDGET_MS,
            f"one pass took {elapsed:.0f} ms against a 1 s loop with "
            f"{PULSE_PATHOLOGICAL_ENTRIES} entries in one directory",
        )
        self.assertLessEqual(
            len(self.paths),
            pulse.MAX_TRACKED + 16,
            "the set that gets fingerprinted grew with the directory",
        )

    def test_the_markers_a_directory_fills_with_are_bounded_too(self) -> None:
        """The real shape here: entries that are not the files being watched."""
        self.fill(PULSE_PATHOLOGICAL_ENTRIES, ".nameintent")
        elapsed = self.fastest_pass_ms()
        self.assertLess(
            elapsed,
            PULSE_PASS_BUDGET_MS,
            f"one pass took {elapsed:.0f} ms with "
            f"{PULSE_PATHOLOGICAL_ENTRIES} non-matching entries",
        )

    def test_an_ordinary_directory_is_still_watched_whole(self) -> None:
        """The cap is a ceiling, not a sample: today's estate is well under it."""
        self.fill(700, ".json")
        sessions = self.home / ".claude" / "sessions"
        paths = pulse.watched_paths(self.environ, self.home)
        records = [
            path
            for path in paths
            if path.parent == sessions and path.suffix == ".json"
        ]
        self.assertEqual(700, len(records))

    def test_records_buried_in_markers_are_never_dropped_from_the_watch(self) -> None:
        """A marker haystack must not blind the watcher to the records.

        scandir order is name-hash order, so the two records can land anywhere
        in a 20 000-entry listing; an entries-examined cap once dropped both
        while the poll stayed widened on the watcher's word. Coverage of every
        record, wherever it hashes, is the contract.
        """
        self.fill(20_000, ".nameintent")
        sessions = self.home / ".claude" / "sessions"
        (sessions / "zz-late-record.json").write_text("{}", encoding="utf-8")
        (sessions / "aa-early-record.json").write_text("{}", encoding="utf-8")
        paths = pulse.watched_paths(self.environ, self.home)
        records = sorted(
            path.name
            for path in paths
            if path.parent == sessions and path.suffix == ".json"
        )
        self.assertEqual(["aa-early-record.json", "zz-late-record.json"], records)


if __name__ == "__main__":
    unittest.main()
