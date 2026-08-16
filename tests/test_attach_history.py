"""Attach refills the terminal's scrollback from the rendered journal.

Restore mode "simple" repaints only the live frame, so before this feature a
freshly opened window had no scrollback at all -- the operator could scroll
back "only a couple screens" (operator finding 2026-08-14). These tests drive
the real ``attach_id`` on a real pty behind a stub shpool and assert that the
settled rendered journal -- and only the settled text, never the raw bytes and
never the still-live frame -- is written to the terminal before the attach.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty as pty_module
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import unittest

from tests.support import REPO
from tests.test_tab_title_ownership import run_on_pty


def run_on_sized_pty(script: str, rows: int, cols: int = 120) -> bytes:
    """Like run_on_pty, but with a real window size on the terminal.

    The plain helper never sets a winsize, so it cannot tell "a screenful"
    from a constant -- the exact blindness that let a 24-row pad ship
    (lane finding X19-c).
    """
    parent, child = pty_module.openpty()
    size = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(child, termios.TIOCSWINSZ, size)
    process = subprocess.Popen(
        ["bash", "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=child,
        stderr=subprocess.DEVNULL,
        env=dict(os.environ),
    )
    os.close(child)
    collected = b""
    try:
        while True:
            try:
                chunk = os.read(parent, 65536)
            except OSError:
                break
            if not chunk:
                break
            collected += chunk
    finally:
        process.wait(timeout=30)
        os.close(parent)
    return collected

RENDER_TOOL = REPO / "lib" / "sessionkit_inventory" / "journal_render.py"
SEPARATOR = b"earlier output above"


class AttachHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        raw = tempfile.TemporaryDirectory(prefix=".attach-history-", dir=REPO)
        self.addCleanup(raw.cleanup)
        self.base = Path(raw.name)
        stub = self.base / "fake-shpool"
        self.shpool_log = self.base / "shpool.log"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'printf \'%s\\n\' "$*" >> "{self.shpool_log}"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        core = self.base / "fake-core"
        core.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        core.chmod(0o755)
        (self.base / "state").mkdir()
        self.journals = self.base / "journals"
        self.journals.mkdir()
        self.stub = stub
        self.core = core

    def write_journal(self, session: str, line_count: int = 100) -> None:
        lines = "".join(
            f"history line {index:03d}\n" for index in range(1, line_count + 1)
        )
        (self.journals / f"{session}.raw").write_text(lines, encoding="utf-8")

    def settled_lines(self, session: str) -> list[str]:
        """Ground truth from the renderer itself, not a guess at its geometry."""
        journal = self.journals / f"{session}.raw"
        sidecar = self.base / "truth.txt"
        state = self.base / "truth.state.json"
        subprocess.run(
            [
                "python3",
                os.fspath(RENDER_TOOL),
                "render",
                "--journal",
                os.fspath(journal),
                "--out",
                os.fspath(sidecar),
                "--state",
                os.fspath(state),
            ],
            check=True,
            capture_output=True,
        )
        return sidecar.read_text(encoding="utf-8").splitlines()

    def attach_script(
        self, session: str, prelude: str = "", function: str = "attach_id"
    ) -> str:
        return (
            f'export SESSION_KIT_SHPOOL_CMD="{self.stub}"\n'
            f'export SESSION_KIT_INVENTORY_CORE="{self.core}"\n'
            f'export SESSION_KIT_STATE_DIR="{self.base / "state"}"\n'
            f'export SESSION_KIT_JOURNAL_DIR="{self.journals}"\n'
            f"export SESSION_KIT_TESTING=1\n"
            f'export SESSION_KIT_JOURNAL_RENDER_TOOL="{RENDER_TOOL}"\n'
            f"{prelude}"
            f'source "{REPO}/bin/session_kit_common"\n'
            f'INVENTORY_CORE="{self.core}"\n'
            f'source "{REPO}/lib/sh/sp_core.sh"\n'
            f'source "{REPO}/lib/sh/sp_picker.sh"\n'
            f'SK_TITLE="Drill"; SK_PROVIDER="claude"; SK_NUMBER="3"\n'
            f"{function} {session} /tmp\n"
        )

    def test_attach_replays_settled_history_into_scrollback(self) -> None:
        self.write_journal("drill-session")
        settled = self.settled_lines("drill-session")
        self.assertTrue(settled, "fixture journal must settle some lines")
        written = run_on_pty(self.attach_script("drill-session"))
        for line in (settled[0], settled[-1]):
            self.assertIn(line.encode(), written)
        self.assertIn(SEPARATOR, written)
        # The still-live frame is the application's to repaint after attach;
        # replaying it here would show every frame twice.
        self.assertNotIn(b"history line 100", written)
        self.assertNotIn("history line 100", settled)
        # After the separator, a screenful of blank rows must push the replay
        # into scrollback so the application repaints on a clean region
        # instead of over the replay's tail (X19-b: a transferred TUI session
        # showed the two interleaved).
        tail_bytes = written.split(SEPARATOR, 1)[1]
        pad = tail_bytes.split(b"attach ")[0] if b"attach " in tail_bytes else tail_bytes
        self.assertGreaterEqual(pad.count(b"\n"), 20)
        self.assertEqual(
            "attach --cmd /bin/false --dir /tmp drill-session\n",
            self.shpool_log.read_text(encoding="utf-8"),
        )

    def test_the_replay_honors_the_line_bound(self) -> None:
        self.write_journal("drill-session")
        settled = self.settled_lines("drill-session")
        self.assertGreater(len(settled), 3)
        written = run_on_pty(
            self.attach_script(
                "drill-session",
                prelude="export SESSION_KIT_ATTACH_HISTORY=3\n",
            )
        )
        for line in settled[-3:]:
            self.assertIn(line.encode(), written)
        self.assertNotIn(settled[-4].encode(), written)

    def test_zero_turns_the_refill_off_and_never_blocks_the_attach(self) -> None:
        self.write_journal("drill-session")
        written = run_on_pty(
            self.attach_script(
                "drill-session",
                prelude="export SESSION_KIT_ATTACH_HISTORY=0\n",
            )
        )
        self.assertNotIn(b"history line", written)
        self.assertNotIn(SEPARATOR, written)
        self.assertTrue(self.shpool_log.exists())

    def test_a_session_without_a_journal_attaches_silently(self) -> None:
        written = run_on_pty(self.attach_script("bare-session"))
        self.assertNotIn(SEPARATOR, written)
        self.assertEqual(
            "attach --cmd /bin/false --dir /tmp bare-session\n",
            self.shpool_log.read_text(encoding="utf-8"),
        )

    def test_the_picker_door_replays_the_same_history(self) -> None:
        """The login menu and the TUI picker attach through picker_attach_id,
        not attach_id -- the second human door must refill too (lane F1)."""
        self.write_journal("drill-session")
        settled = self.settled_lines("drill-session")
        written = run_on_pty(
            self.attach_script("drill-session", function="picker_attach_id")
        )
        self.assertIn(settled[0].encode(), written)
        self.assertIn(SEPARATOR, written)
        self.assertNotIn(b"history line 100", written)
        self.assertEqual(
            "attach --cmd /bin/false --dir /tmp drill-session\n",
            self.shpool_log.read_text(encoding="utf-8"),
        )

    def test_a_leading_zero_bound_is_read_as_decimal(self) -> None:
        """"008" must mean eight lines -- bash octal parsing printed an
        arithmetic error onto the operator's terminal (lane F3)."""
        self.write_journal("drill-session")
        settled = self.settled_lines("drill-session")
        self.assertGreater(len(settled), 8)
        written = run_on_pty(
            self.attach_script(
                "drill-session",
                prelude="export SESSION_KIT_ATTACH_HISTORY=008\n",
            )
        )
        for line in settled[-8:]:
            self.assertIn(line.encode(), written)
        self.assertNotIn(settled[-9].encode(), written)

    def test_a_gigantic_bound_replays_the_whole_sidecar(self) -> None:
        """A bound past 9 digits used to wrap 64-bit arithmetic negative and
        silently disable the refill (lane F3); it must cap sanely instead."""
        self.write_journal("drill-session")
        settled = self.settled_lines("drill-session")
        written = run_on_pty(
            self.attach_script(
                "drill-session",
                prelude="export SESSION_KIT_ATTACH_HISTORY=" + "9" * 24 + "\n",
            )
        )
        for line in (settled[0], settled[-1]):
            self.assertIn(line.encode(), written)
        self.assertIn(SEPARATOR, written)

    def test_a_held_render_lock_falls_back_to_the_stale_sidecar(self) -> None:
        """Two concurrent renders duplicate settled lines permanently (lane
        O1), so a contended lock must skip the freshen -- immediately, not
        after the render timeout -- and replay what the sidecar holds."""
        session_dir = self.journals / "drill-session"
        session_dir.mkdir()
        lines = "".join(
            f"history line {index:03d}\n" for index in range(1, 101)
        )
        (session_dir / "segment-000001.raw").write_text(lines, encoding="utf-8")
        sidecar = session_dir / "rendered.txt"
        sidecar.write_text("a stale settled line\n", encoding="utf-8")
        lock = session_dir / "rendered.txt.lock"
        holder = subprocess.Popen(
            ["flock", os.fspath(lock), "sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                probe = subprocess.run(
                    ["flock", "-n", os.fspath(lock), "true"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if probe.returncode != 0:
                    break
                time.sleep(0.05)
            else:
                self.fail("the lock holder never acquired the lock")
            started = time.monotonic()
            written = run_on_pty(self.attach_script("drill-session"))
            elapsed = time.monotonic() - started
        finally:
            holder.terminate()
            holder.wait()
        self.assertIn(b"a stale settled line", written)
        self.assertIn(SEPARATOR, written)
        self.assertNotIn(b"history line", written)
        self.assertLess(elapsed, 3.5)
        self.assertEqual(
            "a stale settled line\n", sidecar.read_text(encoding="utf-8")
        )

    def test_the_pad_is_a_real_screenful_not_a_constant(self) -> None:
        """The pad must track the window's actual height. tput inside $( )
        with stderr dropped answers the terminfo constant 24 everywhere,
        which left most of the replay exposed on tall windows (X19-c)."""
        self.write_journal("drill-session")
        for rows, low, high in ((40, 40, 46), (12, 12, 18)):
            with self.subTest(rows=rows):
                written = run_on_sized_pty(
                    self.attach_script("drill-session"), rows=rows
                )
                self.assertIn(SEPARATOR, written)
                pad = written.split(SEPARATOR, 1)[1]
                count = pad.count(b"\n")
                self.assertGreaterEqual(count, low)
                self.assertLessEqual(count, high)

    def no_flock_path(self) -> str:
        """A PATH holding every binary these code paths exec EXCEPT flock(1)
        -- an honest simulation of a macOS box, which has no util-linux."""
        linkdir = self.base / "no-flock-bin"
        linkdir.mkdir(exist_ok=True)
        for name in ("python3", "find", "tail", "cat", "sort", "uname", "mkdir"):
            target = shutil.which(name)
            if target and not (linkdir / name).exists():
                (linkdir / name).symlink_to(target)
        return os.fspath(linkdir)

    def history_script(self, session: str, prelude: str = "") -> str:
        return (
            f'export SESSION_KIT_SHPOOL_CMD="{self.stub}"\n'
            f'export SESSION_KIT_INVENTORY_CORE="{self.core}"\n'
            f'export SESSION_KIT_STATE_DIR="{self.base / "state"}"\n'
            f'export SESSION_KIT_JOURNAL_DIR="{self.journals}"\n'
            f"export SESSION_KIT_TESTING=1\n"
            f'export SESSION_KIT_JOURNAL_RENDER_TOOL="{RENDER_TOOL}"\n'
            f"export SESSION_KIT_NONINTERACTIVE=1\n"
            f"{prelude}"
            f'source "{REPO}/bin/session_kit_common"\n'
            f'INVENTORY_CORE="{self.core}"\n'
            f'source "{REPO}/lib/sh/sp_core.sh"\n'
            f'source "{REPO}/lib/sh/sp_commands.sh"\n'
            f"show_history_id {session}\n"
        )

    def test_a_box_without_flock_still_refills_scrollback(self) -> None:
        """flock(1) does not exist on macOS; a missing locker must mean an
        unlocked render, never a dead refill (lane F4)."""
        self.write_journal("drill-session")
        settled = self.settled_lines("drill-session")
        written = run_on_pty(
            self.attach_script(
                "drill-session",
                prelude=f'export PATH="{self.no_flock_path()}"\n',
            )
        )
        self.assertIn(settled[0].encode(), written)
        self.assertIn(SEPARATOR, written)

    def test_history_without_flock_still_pages_rendered_text(self) -> None:
        """On a flock-less box, sp history must keep paging rendered text --
        an unconditional locker made it fall to the raw capture (lane F4)."""
        self.write_journal("drill-session")
        written = run_on_pty(
            self.history_script(
                "drill-session",
                prelude=f'export PATH="{self.no_flock_path()}"\n',
            )
        )
        self.assertIn("live screen at".encode(), written)
        self.assertTrue(
            (self.journals / "drill-session.rendered.txt").is_file(),
            "the flush must still produce a rendered sidecar without flock",
        )

    def test_a_segment_directory_journal_replays_the_same_way(self) -> None:
        """Live sessions journal as ``<id>/segment-*.raw`` directories."""
        session_dir = self.journals / "drill-session"
        session_dir.mkdir()
        lines = "".join(
            f"history line {index:03d}\n" for index in range(1, 101)
        )
        (session_dir / "segment-000001.raw").write_text(lines, encoding="utf-8")
        written = run_on_pty(self.attach_script("drill-session"))
        self.assertIn(SEPARATOR, written)
        self.assertIn(b"history line 001", written)
        self.assertNotIn(b"history line 100", written)
        sidecar = session_dir / "rendered.txt"
        self.assertTrue(sidecar.is_file(), "sidecar must land inside the dir")

    def test_an_unreadable_bound_falls_back_to_the_default(self) -> None:
        self.write_journal("drill-session")
        settled = self.settled_lines("drill-session")
        written = run_on_pty(
            self.attach_script(
                "drill-session",
                prelude="export SESSION_KIT_ATTACH_HISTORY=banana\n",
            )
        )
        self.assertIn(settled[-1].encode(), written)
        self.assertIn(SEPARATOR, written)


if __name__ == "__main__":
    unittest.main()
