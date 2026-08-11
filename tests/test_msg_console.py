"""The message centre: `sp msg` on a terminal.

Every test drives the real console through a pty against the stubbed core in
tests.test_msg_cli — no session, socket, provider, or real ledger is reachable
from here, and every path the console touches is inside a disposable tree.
"""

from __future__ import annotations

import errno
import json
import os
import pty
import re
import select
import signal
import time
import unittest

from tests.support import REPO
from tests.test_msg_cli import (
    CLAUDE_UUID,
    CODEX_UUID,
    MsgFixture,
    nested_report_document,
    write_executable,
)


SP = REPO / "bin" / "sp"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CLAUDE_KEY = f"claude:{CLAUDE_UUID}"
CODEX_KEY = f"codex:{CODEX_UUID}"

# The console clears an unread mark by asking the core to, so the stub has to
# answer that verb. Everything else is delegated to the shared stub rather
# than copied, so the two files cannot drift into disagreeing about the
# contract they are both pretending to be.
MARK_READ_STUB = """#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys

args = sys.argv[1:]
if args[:2] == ["msg", "mark-read"]:
    with pathlib.Path(os.environ["STUB_MSG_LOG"]).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\\n")
    thread = args[args.index("--thread") + 1] if "--thread" in args else ""
    print(json.dumps({"cleared": True, "thread_key": thread}, sort_keys=True))
    raise SystemExit(0)
raise SystemExit(subprocess.run([os.environ["STUB_MSG_INNER"], *args]).returncode)
"""


def visible(text: str) -> str:
    return ANSI.sub("", text).replace("\r\n", "\n")


def report_with_unread(unread: bool) -> dict:
    """The report shape the core returns now: reading it clears nothing."""
    document = nested_report_document()
    document["threads"][CLAUDE_KEY]["unread"] = unread
    document.pop("unread_cleared", None)
    return document


class ConsoleFixture(MsgFixture):
    """The shared CLI sandbox, plus the mark-read verb the console needs."""

    def __init__(self) -> None:
        super().__init__()
        self.inner_core = self.base / "inner-core"
        self.fake_core.replace(self.inner_core)
        write_executable(self.fake_core, MARK_READ_STUB)

    def env(self, **overrides: str) -> dict[str, str]:
        environment = super().env(**overrides)
        environment.setdefault("STUB_MSG_INNER", str(self.inner_core))
        return environment


class Console:
    """A real `sp msg` on a real terminal, driven one key at a time."""

    def __init__(
        self,
        fixture: MsgFixture,
        *args: str,
        columns: int = 100,
        lines: int = 30,
        **overrides: str,
    ) -> None:
        self.fixture = fixture
        environment = fixture.env(
            SESSION_KIT_NONINTERACTIVE="0",
            SESSION_KIT_MSG_CONSOLE_SECONDS="0.4",
            TERM="xterm",
            COLUMNS=str(columns),
            LINES=str(lines),
            **overrides,
        )
        environment.pop("SESSION_KIT_NO_COLOR", None)
        self.output = ""
        self.status: int | None = None
        self.pid, self.descriptor = pty.fork()
        if self.pid == 0:  # pragma: no cover - the child execs immediately
            os.chdir(REPO)
            argv = [os.fspath(SP), "msg", *args]
            if os.environ.get("SESSION_KIT_TEST_EXEC_ROOT"):
                os.execve("/usr/bin/bash", ["/usr/bin/bash", *argv], environment)
            os.execve(SP, argv, environment)

    def _pump(self, timeout: float) -> None:
        ready, _, _ = select.select([self.descriptor], [], [], timeout)
        if not ready:
            return
        try:
            chunk = os.read(self.descriptor, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            return
        self.output += visible(chunk.decode("utf-8", "replace"))

    def wait_for(self, needle: str, *, after: int = 0, timeout: float = 20) -> int:
        """Index of `needle` at or past `after`; fails loudly with the screen."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            position = self.output.find(needle, after)
            if position >= 0:
                return position
            self._pump(0.1)
        raise AssertionError(
            f"never saw {needle!r} after offset {after}; screen was:\n{self.output}"
        )

    def send(self, data: bytes) -> None:
        os.write(self.descriptor, data)

    def send_until_exit(self, data: bytes, *, timeout: float = 15) -> None:
        """Press a key until it lands, the way a person does."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive():
                return
            self.send(data)
            self._pump(0.5)

    def _reap(self) -> bool:
        """True once the console has exited; a waited-for status is kept."""
        if self.status is not None:
            return True
        try:
            waited, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.status = 0
            return True
        if waited != self.pid:
            return False
        self.status = os.waitstatus_to_exitcode(status)
        return True

    def alive(self) -> bool:
        return not self._reap()

    def wait_exit(self, timeout: float = 20) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._pump(0.1)
            if self._reap():
                while True:
                    ready, _, _ = select.select([self.descriptor], [], [], 0)
                    if not ready:
                        break
                    try:
                        chunk = os.read(self.descriptor, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    self.output += visible(chunk.decode("utf-8", "replace"))
                assert self.status is not None
                return self.status
        os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)
        raise AssertionError(f"console never exited; screen was:\n{self.output}")

    def close(self) -> None:
        try:
            if self.alive():
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
        except (ChildProcessError, OSError):
            pass
        try:
            os.close(self.descriptor)
        except OSError:
            pass


class MsgConsoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ConsoleFixture()
        self.fixture.set_report(report_with_unread(True))
        self.consoles: list[Console] = []

    def tearDown(self) -> None:
        for console in self.consoles:
            console.close()
        self.fixture.close()

    def console(self, *args: str, **kwargs) -> Console:
        """The centre, entered the way a person does: `sp msg`, no arguments."""
        started = Console(self.fixture, *args, **kwargs)
        self.consoles.append(started)
        return started

    def actions(self) -> list[dict]:
        log = self.fixture.state / "action-events.jsonl"
        if not log.exists():
            return []
        return [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def outcomes(self) -> list[str]:
        return [
            entry.get("outcome")
            for entry in self.actions()
            if entry.get("action") == "msg_console"
        ]

    def sends(self) -> list[list[str]]:
        return [call for call in self.fixture.calls() if call[:2] == ["msg", "send"]]

    def leave_after_receipt(self, console: Console, sent_at: int) -> int:
        """Dismiss the receipt, wait for the list to come back, then quit."""
        console.wait_for("Press a key to go back", after=sent_at)
        console.send(b"\n")
        back = console.wait_for("follow up to all", after=sent_at)
        console.send(b"q")
        return back

    def marks(self) -> list[str]:
        return [
            call[call.index("--thread") + 1]
            for call in self.fixture.calls()
            if call[:2] == ["msg", "mark-read"] and "--thread" in call
        ]

    def test_bare_sp_msg_on_a_terminal_opens_the_centre(self) -> None:
        """No verb, no arguments: the whole feature is one surface."""
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.wait_for("Fleet Rebuild")
        console.wait_for("follow up to all")
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        self.assertIn("Left the message centre", console.output)
        # It really was polling, not a one-shot render.
        self.assertGreaterEqual(
            len([call for call in self.fixture.calls() if call[:2] == ["msg", "report"]]),
            1,
        )
        self.assertEqual(["opened", "quit"], self.outcomes())


    def test_enter_alone_leaves_the_centre(self) -> None:
        """Enter goes back one level everywhere; the centre is no exception."""
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.wait_for("follow up to all")
        console.send(b"\n")
        self.assertEqual(0, console.wait_exit())
        self.assertIn("Left the message centre", console.output)

    def test_the_console_redraws_unattended_and_survives_idle_polls(self) -> None:
        """Nobody types for several poll intervals; the console must still live.

        A timed-out read reports a status the caller has to fetch from an
        explicit else — after a bare `if`, $? is the `if`'s own zero, which
        reads as closed input. That bug ends the console one poll after it
        opens, and only an unattended test sees it.
        """
        console = self.console()
        first = console.wait_for("Fleet Rebuild")
        changed = report_with_unread(False)
        changed["send"]["targets"][0]["title"] = "Fleet Rebuilt"
        self.fixture.set_report(changed)
        # No keystroke: the poll loop alone has to notice and repaint.
        console.wait_for("Fleet Rebuilt", after=first, timeout=15)
        self.assertTrue(console.alive())
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        self.assertEqual(["opened", "quit"], self.outcomes())
        # At least the opening fetch and one nobody asked for.
        self.assertGreaterEqual(
            len([call for call in self.fixture.calls() if call[:2] == ["msg", "report"]]),
            2,
        )

    def test_without_a_terminal_the_static_report_is_unchanged(self) -> None:
        completed = self.fixture.sp("msg", "report")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Message 4f3a2b1c", completed.stdout)
        self.assertIn("sp msg reply 4f3a2b1c", completed.stdout)
        self.assertNotIn("follow up to all", completed.stdout)
        self.assertEqual([], self.outcomes())

    def test_the_static_report_marks_the_threads_it_printed(self) -> None:
        """It showed the reply, so the envelope must stop being offered.

        Without this the picker would keep advertising a reply that has
        already been read on screen, which is the cue lying in the other
        direction.
        """
        completed = self.fixture.sp("msg", "report")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("ledger tests green", completed.stdout)
        self.assertIn(CLAUDE_KEY, self.marks())

    def test_a_report_with_nothing_unread_asks_the_core_for_nothing(self) -> None:
        """Marking what carries no marker reaches the same state and costs a
        process per target, so only the marked threads are named."""
        self.fixture.set_report(report_with_unread(False))
        completed = self.fixture.sp("msg", "report")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([], self.marks())

    def test_the_console_can_be_switched_off_on_a_terminal(self) -> None:
        console = self.console("report", SESSION_KIT_MSG_CONSOLE="0")
        console.wait_for("sp msg reply 4f3a2b1c")
        self.assertEqual(0, console.wait_exit())
        self.assertNotIn("follow up to all", console.output)

    def test_a_first_run_with_nothing_sent_asks_for_a_message(self) -> None:
        """An empty ledger is a first run, not a dead end.

        The core answers a null id there, and the only useful thing to do with
        an empty centre is write something, so it asks instead of parking on
        an empty screen. Backing out leaves the screen, which names its keys.
        """
        self.fixture.set_report({"msg_id": None, "send": None, "threads": {}})
        console = self.console()
        console.wait_for("New message")
        console.wait_for("target>")
        console.send(b"\n")
        console.wait_for("No message has been sent yet.")
        console.wait_for("n  write one")
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        self.assertEqual([], self.sends())

    def test_an_unread_reply_stays_marked_until_its_thread_is_opened(self) -> None:
        """Polling must not consume the mark; opening the thread must.

        Reading a report leaves the ledger alone, so the envelope survives
        every redraw. Clearing it is a deliberate call, and it is the same
        marker the picker counts in its header — a console that decided for
        itself when to stop showing one would disagree with that count.
        """
        console = self.console()
        console.wait_for("✉")
        # Several polls later, with nothing touched, it is still there.
        renamed = report_with_unread(True)
        renamed["send"]["targets"][0]["title"] = "Fleet Rebuilt"
        self.fixture.set_report(renamed)
        moved = console.wait_for("Fleet Rebuilt", timeout=15)
        console.wait_for("✉", after=moved)
        self.assertEqual([], self.marks())

        console.send(b"2")
        opened = console.wait_for("Thread #2", after=moved)
        console.wait_for("ledger tests green", after=opened)
        self.assertEqual([CLAUDE_KEY], self.marks())
        # A shell fault in the console reaches the operator's screen as noise
        # and nothing else, so the screen is where it has to be caught.
        self.assertNotIn("command not found", console.output)
        # The ledger has dropped it, so the frame the console comes back to
        # has dropped it too.
        self.fixture.set_report(report_with_unread(False))
        console.send(b"\n")
        back = console.wait_for("follow up to all", after=opened)
        console.wait_for("Fleet Rebuild", after=back)
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        self.assertNotIn("✉", console.output[back:])

    def test_a_thread_reply_goes_to_that_thread_key_and_nothing_else(self) -> None:
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.send(b"2")
        opened = console.wait_for("reply>")
        console.send(b"on it now\n")
        sent = console.wait_for("Sent 4f3a2b1c", after=opened)
        # The receipt pauses, then the thread redraws with the new line in it.
        console.send(b"\n")
        again = console.wait_for("reply>", after=sent)
        console.send(b"\n")
        console.wait_for("follow up to all", after=again)
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        self.assertEqual(
            [["msg", "send", "--target", f"key:{CLAUDE_KEY}", "--text", "on it now"]],
            self.sends(),
        )
        self.assertIn("send_recorded", self.outcomes())

    def test_an_empty_composer_line_goes_back_without_sending(self) -> None:
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.send(b"2")
        opened = console.wait_for("reply>")
        console.send(b"\n")
        console.wait_for("follow up to all", after=opened)
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        self.assertEqual([], self.sends())

    def test_a_number_no_target_has_says_so_and_opens_nothing(self) -> None:
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.send(b"7")
        console.wait_for("No target is numbered 7")
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        self.assertEqual([], self.sends())

    def test_follow_up_to_all_is_one_send_to_every_target(self) -> None:
        """One send, not one per target: the Claude batching depends on it.

        A send per thread key would run the headless sender once per Claude
        target, serially. The keys: selector names the whole set in the order
        the send recorded it, so they share a single run.
        """
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.send(b"a")
        console.wait_for("message>")
        console.send(b"one more question\n")
        console.wait_for("Confirm?")
        console.send(b"y\n")
        sent = console.wait_for("Sent 4f3a2b1c")
        self.leave_after_receipt(console, sent)
        self.assertEqual(0, console.wait_exit())
        self.assertEqual(
            [
                [
                    "msg",
                    "send",
                    "--target",
                    f"keys:{CLAUDE_KEY},{CODEX_KEY}",
                    "--text",
                    "one more question",
                ]
            ],
            self.sends(),
        )

    def test_a_target_that_has_exited_is_named_on_the_receipt(self) -> None:
        """A keys: send skips a departed session; the receipt is where it shows."""
        receipt = self.fixture.send.read_text(encoding="utf-8")
        document = json.loads(receipt)
        document["skipped"] = [
            {
                "shpool_id": None,
                "terminal_number": None,
                "provider": "codex",
                "title": CODEX_KEY,
                "reason": "session is no longer live",
            }
        ]
        self.fixture.set_send(document)
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.send(b"a")
        console.wait_for("message>")
        console.send(b"anyone still there\n")
        console.wait_for("Confirm?")
        console.send(b"y\n")
        console.wait_for("Not sent to 1 session(s):")
        sent = console.wait_for("session is no longer live")
        self.leave_after_receipt(console, sent)
        self.assertEqual(0, console.wait_exit())

    def test_n_writes_a_message_and_switches_to_watching_it(self) -> None:
        """Writing a message and watching for its answers is one action."""
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.send(b"n")
        console.wait_for("target>")
        console.send(b"a\n")
        console.wait_for("message>")
        console.send(b"status in one line please\n")
        console.wait_for("Confirm?")
        console.send(b"y\n")
        sent = console.wait_for("Sent 4f3a2b1c")
        console.wait_for("Watching 4f3a2b1c", after=sent)
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        self.assertEqual(
            [
                [
                    "msg",
                    "send",
                    "--target",
                    "all",
                    "--text",
                    "status in one line please",
                ]
            ],
            self.sends(),
        )
        # The view moved to what was just sent, with nothing else typed.
        self.assertIn(
            ["msg", "report", "--id", "4f3a2b1c"], self.fixture.calls()
        )

    def test_l_opens_an_older_message(self) -> None:
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.send(b"l")
        console.wait_for("Recent messages")
        console.send(b"1\n")
        console.wait_for("Message 4f3a2b1c", after=console.wait_for("Recent messages"))
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        self.assertIn(["msg", "list"], self.fixture.calls())
        self.assertIn(
            ["msg", "report", "--id", "4f3a2b1c"], self.fixture.calls()
        )

    def test_declining_the_follow_up_confirm_sends_nothing(self) -> None:
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.send(b"a")
        console.wait_for("message>")
        console.send(b"one more question\n")
        console.wait_for("Confirm?")
        console.send(b"n\n")
        console.wait_for("Follow-up cancelled")
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        self.assertEqual([], self.sends())

    def test_ctrl_c_redraws_the_console_instead_of_killing_it(self) -> None:
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.send(b"\x03")
        console.wait_for("Ctrl-C redraws this console")
        self.assertTrue(console.alive())
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        self.assertEqual(["opened", "quit"], self.outcomes())

    def test_ctrl_d_leaves_the_console_like_q(self) -> None:
        """A single-key read gets Ctrl-D as a byte, not as end of input.

        Typed between two frames it is still the terminal's own end-of-file
        character and the mode switch for the next read drops it, so this
        presses it the way a person would — again — rather than pretending one
        keystroke always lands.
        """
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.send_until_exit(b"\x04")
        self.assertEqual(0, console.wait_exit())
        self.assertIn("Left the message centre", console.output)
        self.assertEqual(["opened", "quit"], self.outcomes())

    def test_input_closing_under_the_composer_leaves_and_records_why(self) -> None:
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.send(b"2")
        console.wait_for("reply>")
        # Readline does report end-of-input, and the picker's rule holds: no
        # exit from an interactive surface is allowed to be silent.
        console.send(b"\x04")
        self.assertEqual(0, console.wait_exit())
        self.assertEqual(["opened", "input_closed"], self.outcomes())
        self.assertEqual([], self.sends())

    def test_a_report_that_never_loads_refuses_instead_of_drawing_a_frame(self) -> None:
        console = self.console(STUB_MSG_REPORT_FAIL="ledger unreadable")
        console.wait_for("could not read that message report")
        self.assertEqual(1, console.wait_exit())
        self.assertEqual(["opened", "report_failed"], self.outcomes())

    def test_hostile_reply_text_can_never_write_escapes_to_the_terminal(self) -> None:
        hostile = report_with_unread(True)
        hostile["threads"][CLAUDE_KEY]["lines"][1]["text"] = "done \x1b[2J\x1b[H wiped"
        hostile["send"]["targets"][0]["title"] = "red \x1b[31mALERT\x1b[0m"
        self.fixture.set_report(hostile)
        console = self.console()
        console.wait_for("Message 4f3a2b1c")
        console.send(b"2")
        console.wait_for("reply>")
        console.send(b"\n")
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        # Only the console's own clear sequences reach the terminal.
        stray = [
            match
            for match in re.findall(r"\x1b\[[0-9;?]*[A-Za-z]", console.output)
            if match not in ("\x1b[2J", "\x1b[H")
        ]
        self.assertEqual([], stray, console.output)

    def test_the_frame_never_exceeds_the_window(self) -> None:
        wide = report_with_unread(True)
        wide["send"]["targets"][0]["title"] = "A name far too long for a narrow window"
        wide["threads"][CLAUDE_KEY]["lines"][1]["text"] = "x" * 400
        self.fixture.set_report(wide)
        console = self.console(columns=52, lines=14)
        console.wait_for("Message 4f3a2b1c")
        console.send(b"q")
        self.assertEqual(0, console.wait_exit())
        frame = console.output.split("Message 4f3a2b1c")[-1]
        for line in frame.splitlines():
            self.assertLessEqual(len(line), 52, repr(line))


if __name__ == "__main__":
    unittest.main()
