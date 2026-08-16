"""The terminal a person is holding when the kit hands a session over.

Not a log string, not an argument list: the actual line discipline of the
actual terminal, sampled from the master side of a real pty, at the moments a
person would notice it.

Two separate things are guarded here.

1. THE HANDOFF. `libshpool/src/tty.rs:91` (`set_attach_flags`): if ANY of
   stdin, stdout or stderr is not a tty, the shpool client returns a no-op
   guard, never sets raw mode, and pipes bytes anyway. The picker has already
   restored cooked mode by then (`lib/sh/shpool_login_live.sh:156`), so the
   person is left typing into a cooked terminal with a live client: keystrokes
   echo as literal text, Ctrl-L and Esc do nothing, nothing reaches the
   session. That is the operator's 2026-08-15 00:50 deaf terminal, and it
   lasted 11 minutes 51 seconds.

   Two known ways to hand down a non-terminal descriptor. The one that hit them:
   `picker_events_stop` (`shpool_login_live.sh:376`) runs
   `exec {PICKER_EVENTS_FD}<&- 2>/dev/null`, and in bash a redirection on a
   bare `exec` sticks to the shell for good, so the picker's own stderr became
   /dev/null permanently. The other: the TUI screen, which
   `bin/shpool_login_launcher:75` runs with stderr on a FIFO.

   The first is fixed at its source on a separate branch. What is tested here
   is the second line of defence -- the client gets a terminal on all three
   before the handover, so no source of a non-terminal descriptor, known or
   not, can reach the client's all-three-must-be-a-tty test.

2. THE NET. After the picker's attach returns, the terminal is checked and put
   back if the session left it unusable -- the `stty raw -echo` correction they
   had to run from another window, done from inside the handoff. It fires only
   after the client has exited, and it shouts when it fires.

The stub client implements `tty.rs:91` and nothing else, so a failure here is
the terminal a person would actually get.

WHICH TEST PROVES WHAT — read this before trusting any of them.

A test that passes at the commit it is named for guards nothing. Every test
below is tagged with the commit it actually discriminates against, measured by
copying this file into a worktree at each sha and running it there. `68fd53e`
is the base, `08956ff` and `6df843d` are the two superseded attempts on this
branch.

  DIFFERENTIAL vs 68fd53e  (fails at base; passes at 08956ff, 6df843d, tip)
    test_the_person_is_not_left_typing_into_a_cooked_terminal
    test_new_session_leaves_the_terminal_raw_when_stderr_is_not_a_tty
    test_picker_open_leaves_the_terminal_raw_when_stderr_is_not_a_tty

  DIFFERENTIAL vs 08956ff  (fails at 08956ff; passes at 6df843d and tip)
    test_no_controlling_terminal_puts_nothing_on_the_terminal

  DIFFERENTIAL vs 6df843d  (fails at round 1; passes here)
    test_stdout_and_stderr_redirected_to_files_still_reach_the_files
    test_a_piped_stdout_is_not_taken_away
    test_stderr_after_the_attach_goes_back_to_the_caller
    test_a_session_that_dies_leaves_a_usable_terminal   (4 subtests)
    test_the_repair_shouts_with_the_measurements

  GUARD ONLY  (passes at 6df843d as well -- these are not evidence of any bug,
  they exist so a future change cannot break the common case)
    test_ordinary_terminal_is_unchanged
    test_terminal_is_sane_again_after_a_detach
    test_a_healthy_session_is_not_touched_and_says_nothing
    test_the_net_never_fights_a_live_client
    test_the_terminal_vanishing_mid_attach_hangs_nothing
    test_a_headless_caller_is_untouched

Totals: 15 tests. 10 pass at 6df843d, 5 fail there. All 15 pass here.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import tempfile
import termios
import time
import unittest

REPO = Path(__file__).resolve().parents[1]


def write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


CLIENT = '''#!/usr/bin/env python3
"""A stand-in for `shpool attach` that follows libshpool/src/tty.rs:91 exactly.

All three descriptors are terminals -> take the terminal raw and hold it. Any
one of them is not -> decline raw mode silently and pipe bytes anyway, which is
the whole bug.

CLIENT_MODE picks how it leaves, covering every abnormal death a real client
can suffer while holding the terminal raw:
  normal -> put the terminal back, the way AttachFlagsGuard's drop does.
  crash  -> SIGKILL, the one no handler can catch (a killed client, or the
            daemon vanishing under it and the client being reaped).
  segv   -> SIGSEGV, a client that faults with the terminal still raw.
  term   -> SIGTERM with the default disposition.
  forget -> exits 0 and simply never restores, which is what a client whose
            guard did not run leaves behind.
"""
import os
import signal
import sys
import termios
import time
import tty

MARK = os.environ["CLIENT_MARK"]
MODE = os.environ.get("CLIENT_MODE", "normal")
all_ttys = all(os.isatty(fd) for fd in (0, 1, 2))
with open(MARK, "w") as handle:
    handle.write("%d %d %d\\n" % tuple(int(os.isatty(fd)) for fd in (0, 1, 2)))

saved = None
if all_ttys:
    saved = termios.tcgetattr(0)
    tty.setraw(0)

sys.stdout.write("CLIENT-STDOUT-MARKER\\r\\n")
sys.stdout.flush()
sys.stderr.write("CLIENT-STDERR-MARKER\\r\\n")
sys.stderr.flush()
sys.stdout.write("CLIENT-UP\\r\\n")
sys.stdout.flush()
time.sleep(float(os.environ.get("CLIENT_SECONDS", "1.5")))
DEATHS = {
    "crash": signal.SIGKILL,
    "segv": signal.SIGSEGV,
    "term": signal.SIGTERM,
}
if MODE in DEATHS:
    # No restore, no clean exit: exactly what an abnormal death leaves.
    signal.signal(DEATHS[MODE], signal.SIG_DFL)
    os.kill(os.getpid(), DEATHS[MODE])
    time.sleep(5)
if MODE == "forget":
    os._exit(0)
if saved is not None:
    termios.tcsetattr(0, termios.TCSADRAIN, saved)
sys.stdout.write("CLIENT-DOWN\\r\\n")
sys.stdout.flush()
'''


# The smallest real caller of the door under test: source the kit's own
# sp_core.sh and sp_picker.sh and run their attach paths. Nothing is
# reimplemented here.
DRIVER = '''#!/usr/bin/env bash
set -u
SESSION_KIT_RELEASE_DIR={repo}
source {repo}/bin/session_kit_common
source {repo}/lib/sh/sp_core.sh
PICKER_REFUSED_STATUS=3
PICKER_ATTACH_FAILED_STATUS=4
source {repo}/lib/sh/sp_picker.sh
SK_SHPOOL={client}
INVENTORY_CORE={repo}/lib/session_inventory.py
SNAPSHOT=
history_files() {{ :; }}
cleanup_snapshot() {{ :; }}
sk_tab_title() {{ :; }}
sk_human_label() {{ :; }}
# The picker restores cooked mode before every action it takes
# (lib/sh/shpool_login_live.sh:156). Reproduce that, then hand over.
stty sane < /dev/tty 2>/dev/null || true
{door}
echo "DOOR-RETURNED-$?"
printf 'SP-STDERR-AFTER-ATTACH\\n' >&2
'''


class Handoff:
    """Drive one attach on a real pty and watch it from the master side."""

    def __init__(
        self,
        *,
        door: str,
        stderr_is_tty: bool = True,
        stdout_is_tty: bool = True,
        stdin_is_tty: bool = True,
        client_mode: str = "normal",
        seconds: float = 1.5,
    ):
        self.temp = tempfile.TemporaryDirectory(prefix="attach-tty.")
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.mark = self.base / "descriptors"
        self.stderr_file = self.base / "stderr.log"
        self.stdout_file = self.base / "stdout.log"
        self.client = write_executable(self.base / "client", CLIENT)
        self.driver = write_executable(
            self.base / "driver",
            DRIVER.format(repo=REPO, client=self.client, door=door),
        )
        self.stderr_is_tty = stderr_is_tty
        self.stdout_is_tty = stdout_is_tty
        self.stdin_is_tty = stdin_is_tty
        self.client_mode = client_mode
        self.seconds = seconds
        self.samples: list[tuple[float, bool, bool, bool]] = []
        self.screen = ""

    def close(self) -> None:
        self.temp.cleanup()

    @property
    def repair_log(self) -> Path:
        return self.state / "handoff-repair.log"

    def run(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "CLIENT_MARK": str(self.mark),
                "CLIENT_MODE": self.client_mode,
                "CLIENT_SECONDS": str(self.seconds),
                "HOME": str(self.base),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SESSION_KIT_JOURNAL_DIR": str(self.base / "journal"),
                "SESSION_KIT_ARCHIVE_DIR": str(self.base / "archive"),
                "SESSION_KIT_JOURNAL_RECOVERY_DIR": str(self.base / "recovery"),
                "SESSION_KIT_START_DIR": str(self.base / "start"),
                "SESSION_KIT_PROJECTS_FILE": str(self.base / "projects.tsv"),
                "SESSION_KIT_CONFIG": str(self.base / "inventory.json"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "TERM": "xterm",
            }
        )
        pid, master = pty.fork()
        if pid == 0:
            try:
                if not self.stderr_is_tty:
                    handle = os.open(self.stderr_file, os.O_WRONLY | os.O_CREAT, 0o600)
                    os.dup2(handle, 2)
                if not self.stdout_is_tty:
                    handle = os.open(self.stdout_file, os.O_WRONLY | os.O_CREAT, 0o600)
                    os.dup2(handle, 1)
                if not self.stdin_is_tty:
                    handle = os.open(os.devnull, os.O_RDONLY)
                    os.dup2(handle, 0)
                os.chdir(self.base)
                os.execve(str(self.driver), [str(self.driver)], environment)
            finally:
                os._exit(127)

        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))
        started = time.time()
        client_up = False
        buffer = bytearray()
        while time.time() - started < self.seconds + 12:
            ready, _, _ = select.select([master], [], [], 0.01)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buffer.extend(chunk)
            text = buffer.decode("utf-8", "replace")
            if "CLIENT-UP" in text:
                client_up = True
            try:
                lflag = termios.tcgetattr(master)[3]
            except termios.error:
                break
            self.samples.append(
                (
                    time.time() - started,
                    bool(lflag & termios.ECHO),
                    bool(lflag & termios.ICANON),
                    # The client holds the terminal from CLIENT-UP until it
                    # puts it back (CLIENT-DOWN) or dies without doing so, in
                    # which case the door returning is the end of its life.
                    client_up
                    and "CLIENT-DOWN" not in text
                    and "DOOR-RETURNED" not in text,
                )
            )
            if "SP-STDERR-AFTER-ATTACH" in text or "DOOR-RETURNED" in text:
                # let the trailing writes and the repair land
                if time.time() - started > self.seconds + 0.6:
                    break
        self.screen = buffer.decode("utf-8", "replace")
        try:
            os.close(master)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def run_until_client_then_close_master(self) -> None:
        """Pull the terminal out from under a live attach."""
        environment = os.environ.copy()
        environment.update(
            {
                "CLIENT_MARK": str(self.mark),
                "CLIENT_MODE": self.client_mode,
                "CLIENT_SECONDS": str(self.seconds),
                "HOME": str(self.base),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SESSION_KIT_JOURNAL_DIR": str(self.base / "journal"),
                "SESSION_KIT_ARCHIVE_DIR": str(self.base / "archive"),
                "SESSION_KIT_JOURNAL_RECOVERY_DIR": str(self.base / "recovery"),
                "SESSION_KIT_START_DIR": str(self.base / "start"),
                "SESSION_KIT_PROJECTS_FILE": str(self.base / "projects.tsv"),
                "SESSION_KIT_CONFIG": str(self.base / "inventory.json"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "TERM": "xterm",
            }
        )
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.base)
                os.execve(str(self.driver), [str(self.driver)], environment)
            finally:
                os._exit(127)
        buffer = bytearray()
        started = time.time()
        while time.time() - started < 10:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buffer.extend(chunk)
            if b"CLIENT-UP" in buffer:
                break
        os.close(master)
        deadline = time.time() + 12
        while time.time() < deadline:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                self.samples = [(0.0, True, True, False)]
                return
            time.sleep(0.05)
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        raise AssertionError("the driver never exited after the terminal closed")

    # ---- what a person would experience -------------------------------

    def descriptors(self) -> tuple[int, int, int]:
        values = self.mark.read_text().split()
        return tuple(int(value) for value in values)  # type: ignore[return-value]

    def longest_cooked_with_client_ms(self) -> float:
        longest = 0.0
        start = None
        previous = None
        for at, echo, icanon, alive in self.samples:
            cooked = alive and (echo or icanon)
            if cooked and start is None:
                start = at
            elif not cooked and start is not None:
                longest = max(longest, (previous or at) - start)
                start = None
            previous = at
        if start is not None and previous is not None:
            longest = max(longest, previous - start)
        return longest * 1000.0

    def raw_while_client_alive(self) -> bool:
        """Was the terminal raw for the whole time the client held it?"""
        alive = [s for s in self.samples if s[3]]
        return bool(alive) and all(not s[1] and not s[2] for s in alive)

    def final_modes(self) -> tuple[bool, bool]:
        _, echo, icanon, _ = self.samples[-1]
        return echo, icanon


class HandoffTestCase(unittest.TestCase):
    def drive(self, door: str, **kwargs) -> Handoff:
        handoff = Handoff(door=door, **kwargs)
        self.addCleanup(handoff.close)
        handoff.run()
        self.assertTrue(handoff.samples, "the pty produced no samples")
        return handoff


class AttachHandoffTest(HandoffTestCase):
    """DIFFERENTIALS. These fail at the base commit."""

    def test_the_person_is_not_left_typing_into_a_cooked_terminal(self):
        """The operator's report, as an assertion, with nothing else in it.

        No descriptors, no argv, no log line: only whether the terminal a
        person is holding is cooked while the session is attached.
        """
        handoff = self.drive("attach_id session-1", stderr_is_tty=False)
        cooked_ms = handoff.longest_cooked_with_client_ms()
        self.assertLess(
            cooked_ms,
            500.0,
            f"the terminal was cooked for {cooked_ms:.0f} ms while the session "
            "was attached -- keystrokes echo as literal text, Ctrl-L and Esc do "
            "nothing, and nothing reaches the session",
        )

    def test_new_session_leaves_the_terminal_raw_when_stderr_is_not_a_tty(self):
        """`sp new` through a picker whose stderr is a file, as the TUI
        launcher runs it."""
        handoff = self.drive("attach_id session-1", stderr_is_tty=False)
        self.assertEqual(
            handoff.descriptors(),
            (1, 1, 1),
            "the client was handed a descriptor that is not the terminal, so "
            "shpool declines raw mode (libshpool/src/tty.rs:91) and the person "
            "types into a cooked terminal",
        )

    def test_picker_open_leaves_the_terminal_raw_when_stderr_is_not_a_tty(self):
        """The picker's own open door. X19 finding F1 was these two drifting
        apart, so both are held to the same terminal."""
        handoff = self.drive("picker_attach_id session-1", stderr_is_tty=False)
        self.assertEqual(handoff.descriptors(), (1, 1, 1))
        self.assertLess(handoff.longest_cooked_with_client_ms(), 500.0)


class RedirectionIsHonouredTest(HandoffTestCase):
    """DIFFERENTIAL. A caller that redirected on purpose keeps its redirection.

    The first version of the gate opened on "a terminal on any one of the
    three" and rebound the other two, so `sp go X > out 2> err` printed the
    session to the screen and left `out` empty.
    """

    def test_stdout_and_stderr_redirected_to_files_still_reach_the_files(self):
        handoff = self.drive(
            "attach_id session-1", stdout_is_tty=False, stderr_is_tty=False
        )
        out = handoff.stdout_file.read_text(errors="replace")
        err = handoff.stderr_file.read_text(errors="replace")
        self.assertIn(
            "CLIENT-STDOUT-MARKER",
            out,
            "the caller redirected stdout to a file and the session's output "
            f"did not reach it; screen carried: {handoff.screen[:200]!r}",
        )
        self.assertIn("CLIENT-STDERR-MARKER", err)
        self.assertNotIn(
            "CLIENT-STDOUT-MARKER",
            handoff.screen,
            "session output was printed on the terminal even though the "
            "caller redirected it to a file",
        )

    def test_a_piped_stdout_is_not_taken_away(self):
        """`sp go 3 | tee log` -- stdout is a pipe, stdin is the terminal."""
        handoff = self.drive(
            "attach_id session-1", stdout_is_tty=False, stderr_is_tty=True
        )
        self.assertNotIn("CLIENT-STDOUT-MARKER", handoff.screen)
        self.assertIn(
            "CLIENT-STDOUT-MARKER",
            handoff.stdout_file.read_text(errors="replace"),
        )


class TerminalRepairNetTest(HandoffTestCase):
    """DIFFERENTIAL. The net: a session that leaves the terminal unusable is
    caught, corrected, and shouted about."""

    def test_a_session_that_dies_leaves_a_usable_terminal(self):
        """Every abnormal death, not just the convenient one.

        Lane C measured that after the round-1 binding an abnormally dying
        client left the terminal raw where before it left it usable. Each of
        these is that shape, and each must come back usable.
        """
        for mode in ("crash", "segv", "term", "forget"):
            with self.subTest(death=mode):
                handoff = self.drive(
                    "picker_attach_id session-net", client_mode=mode
                )
                echo, icanon = handoff.final_modes()
                self.assertTrue(
                    echo and icanon,
                    f"a client that died by {mode} left the terminal with no "
                    "echo or no line mode: the person is holding an unusable "
                    "window",
                )
                self.assertTrue(
                    handoff.repair_log.exists(),
                    f"the {mode} death was repaired without a record",
                )

    def test_the_terminal_vanishing_mid_attach_hangs_nothing(self):
        """GUARD ONLY (passes at 6df843d). The pty master closing under the
        attach.

        There is no terminal left to repair and nothing to say; the only
        requirement is that the door returns rather than blocking on a
        descriptor that no longer resolves.
        """
        handoff = Handoff(door="picker_attach_id session-gone", seconds=6.0)
        self.addCleanup(handoff.close)
        started = time.time()
        handoff.run_until_client_then_close_master()
        self.assertLess(
            time.time() - started,
            20.0,
            "the door did not return after its terminal disappeared",
        )

    def test_the_repair_shouts_with_the_measurements(self):
        handoff = self.drive("picker_attach_id session-net", client_mode="crash")
        self.assertTrue(
            handoff.repair_log.exists(),
            "the net corrected the terminal without recording anything; a "
            "silent net destroys the evidence for whoever is hunting the cause",
        )
        line = handoff.repair_log.read_text(errors="replace")
        for field in ("session=session-net", "tty=", "lflag=0x", "ECHO=",
                      "ICANON=", "fg_pgrp=", "members=", "client_status=",
                      "restored="):
            self.assertIn(field, line, f"the record is missing {field!r}: {line!r}")
        self.assertIn(
            "unusable",
            handoff.screen,
            "nothing was said on the screen, so the person has no idea their "
            "terminal was repaired",
        )

    def test_a_healthy_session_is_not_touched_and_says_nothing(self):
        """GUARD ONLY (passes at 6df843d). Idempotent and cheap: when the
        terminal comes back the way it went in, the net does nothing and
        records nothing."""
        handoff = self.drive("picker_attach_id session-ok", client_mode="normal")
        self.assertFalse(
            handoff.repair_log.exists(),
            "the net fired on a healthy handoff: "
            f"{handoff.repair_log.read_text(errors='replace') if handoff.repair_log.exists() else ''}",
        )
        self.assertNotIn("unusable", handoff.screen)

    def test_the_net_never_fights_a_live_client(self):
        """GUARD ONLY (passes at 6df843d). While the client holds the
        terminal it must stay exactly as the client set it: the check runs
        only after the client has exited."""
        handoff = self.drive(
            "picker_attach_id session-live", client_mode="normal", seconds=2.0
        )
        self.assertTrue(
            handoff.raw_while_client_alive(),
            "the terminal left raw mode while the client was still running -- "
            "something took the line discipline away from a live session",
        )


class CaptureScopeTest(HandoffTestCase):
    """DIFFERENTIAL. The picker door's binding must not outlive the attach.

    `attach_id` execs, so its binding dies with the process. `picker_attach_id`
    returns, and an unscoped binding sent everything `sp` said afterwards to
    the screen instead of the launcher's FIFO capture, out of order.
    """

    def test_stderr_after_the_attach_goes_back_to_the_caller(self):
        handoff = self.drive("picker_attach_id session-1", stderr_is_tty=False)
        captured = handoff.stderr_file.read_text(errors="replace")
        self.assertIn(
            "SP-STDERR-AFTER-ATTACH",
            captured,
            "what sp said after the attach bypassed the caller's capture; the "
            "launcher's traceback log loses it and the screen shows it out of "
            f"order. screen: {handoff.screen[-200:]!r}",
        )


class HeadlessAndGuardTest(unittest.TestCase):
    """The shapes where the gate must do nothing at all."""

    def probe(self, script: str, *, setsid: bool = False) -> subprocess.CompletedProcess:
        body = (
            f"source {REPO}/bin/session_kit_common\n"
            f"source {REPO}/lib/sh/sp_core.sh\n" + script
        )
        argv = ["bash", "-c", body]
        if setsid:
            argv = ["setsid", *argv]
        return subprocess.run(
            argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
            env={**os.environ, "SESSION_KIT_STATE_DIR": tempfile.mkdtemp()},
        )

    def on_a_terminal_without_a_controlling_terminal(self, script: str) -> str:
        """The one shape that actually reaches the historical defect.

        It needs BOTH conditions at once, and they are easy to lose:

          * at least one descriptor IS a terminal, or the gate returns at its
            first line and the /dev/tty check is never reached;
          * the process has NO controlling terminal, or /dev/tty opens fine and
            there is no failure to observe.

        `setsid … < /dev/null` from a terminal gives exactly that: stdout and
        stderr stay on the terminal, stdin is not one, and the session leader
        has no controlling terminal. A process holding a terminal on *stdin*
        acquires it as its controlling terminal on Linux, so that shape cannot
        reproduce this and an earlier version of this test, which used
        `stdin=DEVNULL` with everything captured, reproduced nothing at all --
        it passed at the known-bad commit.

        Returns everything that landed ON THE TERMINAL, which is where the
        operator would have seen it.
        """
        body = (
            f"source {REPO}/bin/session_kit_common\n"
            f"source {REPO}/lib/sh/sp_core.sh\n"
            'if ( exec 3< /dev/tty ) 2>/dev/null; then echo CTTY-PRESENT; '
            "else echo CTTY-ABSENT; fi\n" + script
        )
        master, slave = pty.openpty()
        devnull = os.open(os.devnull, os.O_RDONLY)
        try:
            subprocess.run(
                ["setsid", "bash", "-c", body],
                stdin=devnull, stdout=slave, stderr=slave, timeout=60,
                env={**os.environ, "SESSION_KIT_STATE_DIR": tempfile.mkdtemp()},
            )
        finally:
            os.close(devnull)
            os.close(slave)
        fcntl.fcntl(master, fcntl.F_SETFL, os.O_NONBLOCK)
        try:
            text = os.read(master, 1 << 20).decode("utf-8", "replace")
        except OSError:
            text = ""
        os.close(master)
        # The precondition, asserted rather than assumed: a test that quietly
        # stopped reproducing the shape would pass for the wrong reason.
        assert "CTTY-ABSENT" in text, (
            "the probe acquired a controlling terminal, so it is not testing "
            f"the defect at all: {text!r}"
        )
        return text

    def test_no_controlling_terminal_puts_nothing_on_the_terminal(self):
        """DIFFERENTIAL against `08956ff`, the commit `6df843d` was written for.

        `-r /dev/tty` is a permission check on a mode-0666 device: it answers
        yes even when there is no controlling terminal to open. The redirection
        then failed and bash printed the error onto the very terminal the
        binding exists to protect:

            .../lib/sh/sp_core.sh: line 77: /dev/tty: No such device or address

        `6df843d` silenced it by probing the open in a subshell first. The gate
        here does not touch /dev/tty at all, so the failure is gone by
        construction. This holds it gone, and -- unlike its predecessor -- it
        actually reaches the defect.
        """
        text = self.on_a_terminal_without_a_controlling_terminal(
            "sk_bind_handoff_tty; echo rc=$?"
        )
        self.assertIn("rc=0", text)
        self.assertNotIn(
            "No such device",
            text,
            "the gate tried to open a controlling terminal that does not "
            "exist and printed the failure onto the operator's screen",
        )
        self.assertNotIn("/dev/tty:", text)

    def test_a_headless_caller_is_untouched(self):
        """GUARD (passes at every commit). A caller with no terminal anywhere
        must be left exactly as it was."""
        done = self.probe(
            "sk_bind_handoff_tty; echo rc=$?; "
            "echo t0=$([[ -t 0 ]] && echo 1 || echo 0)"
            "t1=$([[ -t 1 ]] && echo 1 || echo 0)"
            "t2=$([[ -t 2 ]] && echo 1 || echo 0)"
        )
        self.assertIn("rc=0", done.stdout)
        self.assertIn("t0=0t1=0t2=0", done.stdout)
        self.assertEqual(done.stderr.strip(), "")


class RegressionGuardTest(HandoffTestCase):
    """GUARDS, NOT DIFFERENTIALS. These pass at the base commit too.

    They exist so a future change to the handoff cannot break the common case
    or strand a person in an unusable shell after a detach.
    """

    def test_ordinary_terminal_is_unchanged(self):
        handoff = self.drive("attach_id session-1", stderr_is_tty=True)
        self.assertEqual(handoff.descriptors(), (1, 1, 1))
        self.assertLess(handoff.longest_cooked_with_client_ms(), 500.0)

    def test_terminal_is_sane_again_after_a_detach(self):
        handoff = self.drive("picker_attach_id session-1", stderr_is_tty=True)
        echo, icanon = handoff.final_modes()
        self.assertTrue(echo, "echo never came back after the session detached")
        self.assertTrue(icanon, "line mode never came back after the detach")


if __name__ == "__main__":
    unittest.main()
