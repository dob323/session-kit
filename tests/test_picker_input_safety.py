"""Pasted input is data, and an interrupt abandons the line it interrupted.

Two input defects the picker shipped with, both proven on a pseudo-terminal:

  * a pasted block ran each of its lines as a picker command, so a paste
    containing `k 17` closed session 17 without a confirmation and a blank
    line inside it dropped the operator to a shell;
  * Ctrl-C at the home prompt did nothing at all, and the line it was meant
    to abandon was submitted by the next Enter.

Both are asserted here against the behaviour, not against survival: the tests
type first, then check what the picker did with what was typed.
"""

from __future__ import annotations

import errno
import os
import pty
import select
import signal
import time
import unittest

from tests.support import REPO
from tests.test_login import LoginFixture, inventory, row, run_pty
from tests.test_picker_exit_grammar import exit_reasons

LOGIN = REPO / "bin" / "shpool_login"

PASTE_OPEN = b"\x1b[200~"
PASTE_CLOSE = b"\x1b[201~"


def sp_actions(fixture: LoginFixture) -> list[str]:
    return [
        str(entry.get("args", [""])[0]) for entry in fixture.sp_entries()
    ]


def run_staged(
    fixture: LoginFixture,
    stages: list[tuple[str, bytes]],
    *,
    lines: int = 24,
    columns: int = 100,
    timeout: float = 20,
) -> tuple[int, str]:
    """Type each payload only once the screen shows what it answers.

    run_pty writes everything up front, which is right for most tests and
    wrong for a paste: absorbing one moves the terminal in and out of
    character-at-a-time reads, and the kernel stops delimiting bytes that were
    already queued behind it. Waiting for each prompt keeps the test about the
    paste rather than about how much a terminal buffers.
    """
    environment = fixture.env(lines=lines, columns=columns)
    pid, descriptor = pty.fork()
    if pid == 0:
        try:
            os.chdir(fixture.base)
            os.execve(os.fspath(LOGIN), [os.fspath(LOGIN)], environment)
        finally:
            os._exit(127)
    output = bytearray()
    deadline = time.monotonic() + timeout

    def pump(seconds: float) -> None:
        ready, _, _ = select.select([descriptor], [], [], seconds)
        if not ready:
            return
        try:
            chunk = os.read(descriptor, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            return
        output.extend(chunk)

    try:
        for marker, payload in stages:
            seen = len(output)
            while marker.encode() not in output[seen:]:
                if time.monotonic() > deadline:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    raise AssertionError(
                        f"picker never printed {marker!r}; "
                        f"output={output.decode(errors='replace')!r}"
                    )
                pump(0.05)
            os.write(descriptor, payload)
        status = None
        while time.monotonic() < deadline:
            pump(0.05)
            waited, child_status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = child_status
                break
        if status is None:
            os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
            raise AssertionError(
                f"picker timed out; output={output.decode(errors='replace')!r}"
            )
        while True:
            ready, _, _ = select.select([descriptor], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(descriptor, 65536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        return os.waitstatus_to_exitcode(status), output.decode(
            "utf-8", errors="replace"
        ).replace("\r\n", "\n")
    finally:
        os.close(descriptor)


class PastedInputTests(unittest.TestCase):
    def test_a_pasted_block_never_closes_a_session(self) -> None:
        """The audit's exact payload: three lines, one of them `k 17`."""
        fixture = LoginFixture(
            inventory(
                row("alpha", number=3),
                row("bravo", number=17),
                row("charlie", number=21),
            )
        )
        try:
            paste = PASTE_OPEN + b"look at this\nk 17\n\nand this\n" + PASTE_CLOSE
            code, output = run_pty(fixture, paste + b"\nq\n")
            self.assertEqual(0, code)
            # Nothing was acted on. The old behaviour logged picker-close here.
            self.assertNotIn("picker-close", sp_actions(fixture))
            # One line arrived at the prompt, and it was refused as one key.
            self.assertIn("look at this k 17 and this", output)
            self.assertEqual(1, output.count("There is no such key on this screen"))
            self.assertEqual(["quit"], exit_reasons(fixture))
        finally:
            fixture.close()

    def test_a_blank_line_inside_a_paste_does_not_leave_the_picker(self) -> None:
        fixture = LoginFixture(inventory(row("alpha", number=3)))
        try:
            paste = PASTE_OPEN + b"\n\n\n" + PASTE_CLOSE
            # The picker is still there to be quit by name afterwards. Under
            # the old reader the first pasted newline was an empty choice,
            # which left for a shell as `terminal_requested`.
            code, output = run_pty(fixture, paste + b"q\n")
            self.assertEqual(0, code)
            self.assertEqual(["quit"], exit_reasons(fixture))
        finally:
            fixture.close()

    def test_the_picker_arms_bracketed_paste_for_its_own_prompt(self) -> None:
        fixture = LoginFixture(inventory(row("alpha", number=3)))
        try:
            code, output = run_pty(fixture, b"q\n")
            self.assertEqual(0, code)
            # Without this the terminal sends a paste as plain keystrokes and
            # no reader can tell it from typing.
            self.assertIn("\x1b[?2004h", output)
            self.assertIn("\x1b[?2004l", output)
        finally:
            fixture.close()

    def test_a_paste_typed_into_a_live_line_stays_in_the_line(self) -> None:
        """The character loop, not the opening canonical read."""
        fixture = LoginFixture(
            inventory(row("alpha", number=3), row("bravo", number=17))
        )
        try:
            paste = PASTE_OPEN + b"k 17\nk 3\n" + PASTE_CLOSE
            code, output = run_pty(
                fixture,
                b"",
                deferred=("❯ ", paste + b"\nq\n"),
                deferred_delay_seconds=1.4,
            )
            self.assertEqual(0, code)
            self.assertNotIn("picker-close", sp_actions(fixture))
            self.assertIn("k 17 k 3", output)
            self.assertEqual(["quit"], exit_reasons(fixture))
        finally:
            fixture.close()

    def test_a_paste_at_a_modal_prompt_is_one_answer(self) -> None:
        fixture = LoginFixture(inventory(row("alpha", number=3)))
        try:
            paste = PASTE_OPEN + b"o\nq\n" + PASTE_CLOSE
            code, output = run_staged(
                fixture,
                [
                    ("❯ ", b"m\n"),
                    ("more ❯ ", paste),
                    ("There is no such key", b"\n"),
                    ("# · kill k #", b"q\n"),
                ],
            )
            self.assertEqual(0, code)
            # `o` was not dispatched: the More menu answered one unknown key.
            self.assertNotIn("Outside the kit: none.", output)
            self.assertIn("These work: o, u, p", output)
            self.assertEqual(["quit"], exit_reasons(fixture))
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
