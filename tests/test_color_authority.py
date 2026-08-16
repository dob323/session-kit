"""One colour wins on every surface that draws a session.

Measured live on 2026-08-12: a session showed green in the picker and another
colour in its own terminal. Three owners, not one.

  1. The colour push wrote only into ``~/.claude/projects``. A session started
     on an enrolled account keeps its transcript under that account's profile,
     so the push found nothing and said so to nobody — six of seven live
     sessions had no colour record at all, and every provider window therefore
     picked its own colour while the picker showed the kit's.
  2. The in-session prompt bar was a colour constant. It could agree with the
     picker only by luck.
  3. Nothing gave a live window the new colour after `sp color`; the store
     changed and the window did not.

The transcript record is what the provider renders AND what the collector
reads, so once the push lands in the right profile the two cannot disagree.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from tests.support import REPO, run
from tests.test_commands import CommandFixture, inventory_document, session_row

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_inventory import colors, names_push  # noqa: E402

SP = REPO / "bin" / "sp"
CORE = REPO / "lib" / "session_inventory.py"
UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def facade() -> object:
    spec = importlib.util.spec_from_file_location("session_inventory_colors", CORE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ColorPushReachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".color-reach-", dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def transcript(self, root: Path) -> Path:
        projects = root / "projects" / "-srv-project"
        projects.mkdir(parents=True)
        path = projects / f"{UUID}.jsonl"
        path.write_text('{"type":"user"}\n', encoding="utf-8")
        return path

    def records(self, path: Path, kind: str) -> list[dict]:
        found = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") == kind:
                found.append(record)
        return found

    def test_a_colour_reaches_the_transcript_in_the_default_profile(self) -> None:
        path = self.transcript(self.home / ".claude")
        pushed, warnings = names_push._push_claude_color(self.home, UUID, "green")
        self.assertEqual([], warnings)
        self.assertIn("claude-transcript-color", pushed)
        self.assertEqual("green", self.records(path, "agent-color")[-1]["agentColor"])

    def test_a_colour_reaches_a_session_running_on_an_enrolled_account(self) -> None:
        """The measured blindness: the transcript is under the profile."""
        path = self.transcript(
            self.home / ".local/share/session-kit/accounts/claude/work"
        )
        pushed, warnings = names_push._push_claude_color(self.home, UUID, "cyan")
        self.assertEqual([], warnings)
        self.assertIn("claude-transcript-color", pushed)
        self.assertEqual("cyan", self.records(path, "agent-color")[-1]["agentColor"])

    def test_a_name_reaches_the_same_profile_transcript(self) -> None:
        (self.home / ".claude" / "sessions").mkdir(parents=True)
        path = self.transcript(
            self.home / ".local/share/session-kit/accounts/claude/work"
        )
        pushed, _ = names_push._push_claude_title(
            self.home,
            UUID,
            "Cache Sweep",
            atomic_write_json=lambda target, payload: target.write_text(
                json.dumps(payload), encoding="utf-8"
            ),
            max_session_records=8,
        )
        self.assertIn("claude-transcript-name", pushed)
        self.assertEqual("Cache Sweep", self.records(path, "agent-name")[-1]["agentName"])

    def test_a_conversation_with_no_transcript_anywhere_says_so(self) -> None:
        pushed, warnings = names_push._push_claude_color(self.home, UUID, "green")
        self.assertEqual([], pushed)
        self.assertEqual(
            ["no Claude transcript for this conversation; color not pushed"], warnings
        )


class PublishedColorTests(unittest.TestCase):
    """What the in-session prompt reads instead of holding its own opinion."""

    def setUp(self) -> None:
        self.core = facade()
        self.temp = tempfile.TemporaryDirectory(prefix=".color-publish-", dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def published(self, name: str) -> str:
        return (self.state / "session-color" / name).read_text(encoding="utf-8")

    def test_every_session_publishes_its_colour_for_its_own_shell(self) -> None:
        written = self.core.publish_session_colors(
            {
                "sessions": [
                    {"shpool_id_raw": "s1", "display_color": "green"},
                    {"shpool_id_raw": "s2", "display_color": "cyan"},
                ]
            },
            state_dir=self.state,
        )
        self.assertEqual(2, written)
        self.assertEqual("green 38;2;63;221;115\n", self.published("s1"))
        self.assertEqual("cyan 38;2;64;216;209\n", self.published("s2"))
        self.assertEqual(
            0o600, os.stat(self.state / "session-color" / "s1").st_mode & 0o777
        )

    def test_the_colour_the_picker_draws_is_the_colour_published(self) -> None:
        """Same table, so the two can never drift apart."""
        for name in colors.SESSION_COLORS:
            self.assertIn(name, colors.SESSION_SGR)

    def test_an_unchanged_colour_is_not_rewritten(self) -> None:
        rows = {"sessions": [{"shpool_id_raw": "s1", "display_color": "green"}]}
        self.core.publish_session_colors(rows, state_dir=self.state)
        self.assertEqual(0, self.core.publish_session_colors(rows, state_dir=self.state))

    def test_a_colour_file_never_outlives_its_session(self) -> None:
        self.core.publish_session_colors(
            {
                "sessions": [
                    {
                        "shpool_id_raw": "s1",
                        "display_color": "green",
                        "shpool_shell": {"pid": 101, "process_start_ticks": 1001},
                    }
                ]
            },
            state_dir=self.state,
        )
        captured = self.core.capture_session_color_generations(self.state)
        self.core.publish_session_colors(
            {
                "sessions": [
                    {
                        "shpool_id_raw": "s2",
                        "display_color": "cyan",
                        "shpool_shell": {"pid": 102, "process_start_ticks": 1002},
                    }
                ]
            },
            state_dir=self.state,
            retire_generations=captured,
        )
        self.assertFalse((self.state / "session-color" / "s1").exists())
        self.assertTrue((self.state / "session-color" / "s2").exists())

    def test_a_stale_colour_sweep_cannot_delete_a_newer_session_generation(
        self,
    ) -> None:
        def row(pid: int, ticks: int) -> dict:
            return {
                "shpool_id_raw": "same",
                "display_color": "green",
                "shpool_shell": {"pid": pid, "process_start_ticks": ticks},
            }

        self.core.publish_session_colors(
            {"sessions": [row(101, 1001)]}, state_dir=self.state
        )
        captured = self.core.capture_session_color_generations(self.state)
        self.core.publish_session_colors(
            {"sessions": [row(202, 2002)]}, state_dir=self.state
        )
        self.core.publish_session_colors(
            {"sessions": []},
            state_dir=self.state,
            retire_generations=captured,
        )

        self.assertTrue((self.state / "session-color" / "same").exists())

    def test_a_row_with_no_usable_colour_publishes_nothing(self) -> None:
        self.assertEqual(
            0,
            self.core.publish_session_colors(
                {
                    "sessions": [
                        {"shpool_id_raw": "s1", "display_color": "chartreuse"},
                        {"shpool_id_raw": "", "display_color": "green"},
                        {"shpool_id_raw": "../escape", "display_color": "green"},
                    ]
                },
                state_dir=self.state,
            ),
        )


class PromptBarTests(unittest.TestCase):
    """The prompt draws the published colour, and old prompts still work."""

    BASHRC = REPO / "bashrc" / "shpool.bashrc"

    def _bar(self, state: Path, session: str) -> str:
        text = self.BASHRC.read_text(encoding="utf-8")
        start = text.index("  __sk_bar() {")
        body = text[start : text.index("\n  }\n", start) + len("\n  }\n")]
        program = body.replace("  __sk_bar()", "__sk_bar()", 1)
        completed = run(
            [
                "bash",
                "-c",
                f"{program}\n__sk_bar",
            ],
            env={
                "XDG_STATE_HOME": os.fspath(state),
                "SHPOOL_SESSION_NAME": session,
                "PATH": os.environ["PATH"],
                "HOME": os.fspath(state),
            },
        )
        return completed.stdout

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".color-bar-", dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.directory = self.state / "session-kit" / "session-color"
        self.directory.mkdir(parents=True)

    def test_the_bar_is_the_colour_the_session_was_published_with(self) -> None:
        (self.directory / "s1").write_text("cyan 38;2;64;216;209\n", encoding="utf-8")
        self.assertIn("\033[38;2;64;216;209m", self._bar(self.state, "s1"))

    def test_a_session_with_no_published_colour_keeps_the_old_bar(self) -> None:
        self.assertIn("\033[38;5;71m", self._bar(self.state, "unpublished"))

    def test_a_corrupt_colour_file_never_reaches_the_terminal(self) -> None:
        (self.directory / "s1").write_text(
            "evil \033]0;pwned\007\n", encoding="utf-8"
        )
        bar = self._bar(self.state, "s1")
        self.assertNotIn("pwned", bar)
        self.assertIn("\033[38;5;71m", bar)


class ColorChangeReachesTheWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CommandFixture()
        row = session_row("main1", provider="claude", uuid=UUID)
        row["agent_status"] = "idle"
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(row)), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.fixture.close()

    def test_colouring_a_session_marks_its_window_for_the_new_colour(self) -> None:
        colored = run([SP, "color", "main1", "cyan"], env=self.fixture.env())
        self.assertIn("Colored", colored.stdout)
        self.assertIn(
            "The picker and the prompt show it now; the Claude window shows it"
            " from its next start.",
            colored.stdout,
        )
        self.assertTrue(
            (self.fixture.state / "provider-untitled" / "main1").is_file()
        )
        # Marking is not restarting: nothing was killed to recolour a session.
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_resetting_a_colour_marks_the_window_too(self) -> None:
        run([SP, "color", "reset", "main1"], env=self.fixture.env())
        self.assertTrue(
            (self.fixture.state / "provider-untitled" / "main1").is_file()
        )


if __name__ == "__main__":
    unittest.main()
