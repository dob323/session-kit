"""The picker in a real terminal.

Everything else in this suite tests the core without curses. These prove the
shell around it: the program starts, paints a list a person can read, and
leaves with the status the launcher reads.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import tempfile
import termios
import time
import unittest

from tests.tui_support import REPO, document, row, write_executable

PICKER = REPO / "bin" / "shpool_login_tui"
CTRL_D = b"\x04"


class Terminal:
    """A pty running the picker against a snapshot written on disk."""

    def __init__(self, *records, term: str = "xterm", lines: int = 24, columns: int = 80):
        self.temp = tempfile.TemporaryDirectory(prefix="tui-smoke.")
        self.base = Path(self.temp.name)
        self.snapshot = self.base / "inventory.json"
        self.snapshot.write_text(json.dumps(document(*records)), encoding="utf-8")
        self.status = write_executable(
            self.base / "fake-status",
            "#!/usr/bin/env bash\n" f'cat "{self.snapshot}"\n',
        )
        self.sp = write_executable(
            self.base / "fake-sp", "#!/usr/bin/env bash\nexit 0\n"
        )
        self.environ = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base),
            "TERM": term,
            "LINES": str(lines),
            "COLUMNS": str(columns),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SESSION_KIT_STATUS_CMD": str(self.status),
            "SESSION_KIT_SP_CMD": str(self.sp),
            "SESSION_KIT_STATE_DIR": str(self.base / "state"),
            "SESSION_KIT_CLOSED_LEDGER": str(self.base / "closed.jsonl"),
            "SESSION_KIT_PROJECTS_FILE": str(self.base / "projects.tsv"),
        }
        self.lines, self.columns = lines, columns

    def close(self) -> None:
        self.temp.cleanup()

    def drive(self, keystrokes: bytes = CTRL_D, *, wait_for: str = "ready ·"):
        pid, descriptor = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.base)
                os.execve("/usr/bin/python3", ["python3", os.fspath(PICKER)], self.environ)
            finally:
                os._exit(127)
        try:
            fcntl.ioctl(
                descriptor,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", self.lines, self.columns, 0, 0),
            )
        except OSError:
            pass
        output = bytearray()
        deadline = time.monotonic() + 20
        marker = wait_for.encode()
        drawn = False
        try:
            while time.monotonic() < deadline and not drawn:
                ready, _, _ = select.select([descriptor], [], [], 0.1)
                if not ready:
                    continue
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError as failure:
                    if failure.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
                drawn = marker in output
            if drawn and keystrokes:
                os.write(descriptor, keystrokes)
            status = None
            while time.monotonic() < deadline:
                ready, _, _ = select.select([descriptor], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(descriptor, 65536)
                    except OSError as failure:
                        if failure.errno != errno.EIO:
                            raise
                        chunk = b""
                    if chunk:
                        output.extend(chunk)
                        continue
                waited, raw = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    status = raw
                    break
            if status is None:
                os.kill(pid, signal.SIGKILL)
                _, status = os.waitpid(pid, 0)
                raise AssertionError(
                    "the picker did not leave; output="
                    + output.decode(errors="replace")
                )
        finally:
            os.close(descriptor)
        code = os.waitstatus_to_exitcode(status)
        return code, output.decode(errors="replace")


class SmokeTests(unittest.TestCase):
    def test_the_picker_draws_the_list_and_leaves_cleanly(self) -> None:
        terminal = Terminal(
            row("Blueprint", number=1, needs_you=True, account_alias="primary"),
            row("Config Sweep", number=2, provider="codex"),
            row("Drill", number=3, origin="machine"),
        )
        try:
            code, output = terminal.drive()
        finally:
            terminal.close()
        self.assertEqual(0, code)
        self.assertIn("sessions ·", output)
        # No attention summary line above the footer (operator ruling
        # 2026-08-15); the rows themselves still carry the state.
        self.assertNotIn("needs you:", output)
        self.assertIn("Blueprint", output)
        self.assertIn("Config Sweep", output)
        self.assertIn("1 machine session", output)
        self.assertIn("New session", output)
        self.assertIn("open ", output)
        self.assertNotIn("Drill", output)

    def test_the_drawn_screen_prints_no_identifier(self) -> None:
        record = row("Blueprint", number=1)
        terminal = Terminal(record)
        try:
            _, output = terminal.drive()
        finally:
            terminal.close()
        self.assertNotIn(record["shpool_id"], output)
        self.assertNotIn(record["identity"]["uuid"], output)

    def test_typing_filters_the_list_in_place(self) -> None:
        terminal = Terminal(
            row("Blueprint", number=1),
            row("Config Sweep", number=2),
        )
        try:
            code, output = terminal.drive(b"con" + CTRL_D)
        finally:
            terminal.close()
        self.assertEqual(0, code)
        self.assertIn("Filter:", output)
        # Painted once in the opening list and again once the filter narrowed
        # it: the screen answered the keystrokes rather than only echoing them.
        self.assertGreaterEqual(output.count("Config Sweep"), 2)
        self.assertEqual(1, output.count("Blueprint"))

    def test_a_terminal_the_screen_cannot_use_says_so_and_does_not_hang(self) -> None:
        terminal = Terminal(row("Blueprint", number=1), term="dumb")
        try:
            code, output = terminal.drive(wait_for="")
        finally:
            terminal.close()
        self.assertIn(code, (0, 1))
        if code == 1:
            self.assertIn("session-kit:", output)


class WithoutATerminalTests(unittest.TestCase):
    def test_the_picker_refuses_without_a_terminal_and_never_exits_two(self) -> None:
        import subprocess

        environ = dict(os.environ)
        environ["PYTHONDONTWRITEBYTECODE"] = "1"
        done = subprocess.run(
            ["python3", os.fspath(PICKER)],
            capture_output=True,
            text=True,
            input="",
            env=environ,
        )
        self.assertEqual(1, done.returncode)
        self.assertEqual("session-kit: the picker needs a terminal.", done.stderr.strip())


if __name__ == "__main__":
    unittest.main()
