"""Two promises the rebuild dropped: a Codex history, and a doctor that checks.

`sp history` falls back to the conversation's own transcript when a session was
never recorded — and did it for Claude only, so a Codex session from the same
weeks answered "no live history" while its rollout sat on this disk unread.

The doctor's transcript-reachability check went with the supervisor that used
to own the resolver. It is the only thing that notices when a conversation this
machine recorded can no longer be read here — the failure that has already
happened once, silently, under a rotated account profile.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_inventory import transcript_text, transcripts  # noqa: E402

CORE = REPO / "lib" / "session_inventory.py"
ONE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
TWO = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"


def rollout_line(role: str, text: str, kind: str = "input_text") -> str:
    return json.dumps(
        {
            "timestamp": "2026-08-09T20:10:17.758Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": role,
                "content": [{"type": kind, "text": text}],
            },
        }
    )


class ResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".transcripts-", dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.environ = {"HOME": str(self.home)}

    def claude_transcript(self, uuid: str, root: Path | None = None) -> Path:
        base = root or self.home / ".claude"
        projects = base / "projects" / "srv-project"
        projects.mkdir(parents=True, exist_ok=True)
        path = projects / f"{uuid}.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        return path

    def codex_rollout(self, uuid: str, root: Path | None = None) -> Path:
        base = root or self.home / ".codex"
        sessions = base / "sessions" / "2026" / "08" / "12"
        sessions.mkdir(parents=True, exist_ok=True)
        path = sessions / f"rollout-2026-08-12T10-00-00-{uuid}.jsonl"
        path.write_text(rollout_line("user", "hello") + "\n", encoding="utf-8")
        return path

    def test_a_claude_transcript_is_found_in_the_default_profile(self) -> None:
        path = self.claude_transcript(ONE)
        self.assertEqual(
            path, transcripts.locate_transcript("claude", ONE, environ=self.environ)
        )

    def test_a_transcript_under_a_rotated_account_profile_is_found_too(self) -> None:
        """The exact blindness the check exists for."""
        root = self.home / ".local/share/session-kit/accounts/claude/work"
        path = self.claude_transcript(ONE, root)
        self.assertEqual(
            path, transcripts.locate_transcript("claude", ONE, environ=self.environ)
        )

    def test_a_codex_rollout_is_found_by_its_conversation(self) -> None:
        path = self.codex_rollout(ONE)
        self.assertEqual(
            path, transcripts.locate_transcript("codex", ONE, environ=self.environ)
        )

    def test_a_conversation_with_no_record_resolves_to_nothing(self) -> None:
        self.assertIsNone(
            transcripts.locate_transcript("claude", ONE, environ=self.environ)
        )
        self.assertIsNone(
            transcripts.locate_transcript("shell", ONE, environ=self.environ)
        )
        self.assertIsNone(
            transcripts.locate_transcript("claude", "not-a-uuid", environ=self.environ)
        )


class CodexHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".codex-history-", dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.rollout = (
            self.home / ".codex" / "sessions" / "2026" / "08" / "12"
        )
        self.rollout.mkdir(parents=True)
        self.path = self.rollout / f"rollout-2026-08-12T10-00-00-{ONE}.jsonl"
        self.path.write_text(
            "\n".join(
                (
                    json.dumps({"type": "session_meta", "payload": {"id": ONE}}),
                    rollout_line(
                        "user",
                        "<permissions instructions>\nnoise</permissions instructions>",
                    ),
                    rollout_line("user", "Fix the blueprint"),
                    rollout_line("assistant", "Fixed it.", kind="output_text"),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                )
            )
            + "\n",
            encoding="utf-8",
        )

    def test_a_rollout_reads_as_the_conversation_it_holds(self) -> None:
        lines = transcript_text.render_rollout(self.path)
        body = "\n".join(lines)
        self.assertIn("══ OPERATOR", body)
        self.assertIn("Fix the blueprint", body)
        self.assertIn("● Fixed it.", body)
        # The harness preamble is not something anybody said.
        self.assertNotIn("permissions instructions", body)

    def test_the_tool_renders_a_codex_conversation_by_its_uuid(self) -> None:
        shown = subprocess.run(
            [
                sys.executable,
                os.fspath(REPO / "lib" / "sessionkit_inventory" / "transcript_text.py"),
                "codex",
                ONE,
            ],
            env={**os.environ, "HOME": os.fspath(self.home)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, shown.returncode, shown.stderr)
        self.assertIn("Fix the blueprint", shown.stdout)

    def test_a_codex_conversation_with_no_rollout_says_so(self) -> None:
        shown = subprocess.run(
            [
                sys.executable,
                os.fspath(REPO / "lib" / "sessionkit_inventory" / "transcript_text.py"),
                "codex",
                TWO,
            ],
            env={**os.environ, "HOME": os.fspath(self.home)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(1, shown.returncode)
        self.assertIn("no transcript found", shown.stderr)


class ReachabilityReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".reach-", dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.state = self.home / "state"
        self.state.mkdir(mode=0o700)

    def write_list(self, *rows: dict) -> None:
        path = self.state / "inventory.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-08-12T00:00:00Z",
                    "sessions": list(rows),
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def report(self) -> dict:
        shown = subprocess.run(
            [sys.executable, os.fspath(CORE), "transcript", "reachability"],
            env={
                **os.environ,
                "HOME": os.fspath(self.home),
                "SESSION_KIT_STATE_DIR": os.fspath(self.state),
            },
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, shown.returncode, shown.stderr)
        return json.loads(shown.stdout)

    def test_no_list_is_a_warning_about_the_check_not_the_sessions(self) -> None:
        value = self.report()
        self.assertEqual("warn", value["status"])
        self.assertEqual(0, value["checked"])
        self.assertIn("nothing was checked", value["detail"])

    def test_a_list_with_no_provider_session_is_fine(self) -> None:
        self.write_list({"provider": "shell", "identity": {"uuid": None}})
        value = self.report()
        self.assertEqual("ok", value["status"])
        self.assertEqual(0, value["checked"])

    def test_a_conversation_this_machine_cannot_read_is_named(self) -> None:
        self.write_list(
            {
                "provider": "claude",
                "identity": {"uuid": ONE},
                "title": "Orphaned Record",
            }
        )
        value = self.report()
        self.assertEqual("warn", value["status"])
        self.assertEqual(1, value["checked"])
        self.assertEqual(["Orphaned Record"], value["unreadable"])

    def test_a_readable_conversation_passes(self) -> None:
        projects = self.home / ".claude" / "projects" / "srv-project"
        projects.mkdir(parents=True)
        (projects / f"{ONE}.jsonl").write_text("{}\n", encoding="utf-8")
        self.write_list({"provider": "claude", "identity": {"uuid": ONE}})
        value = self.report()
        self.assertEqual("ok", value["status"])
        self.assertEqual(1, value["checked"])
        self.assertEqual([], value["unreadable"])


if __name__ == "__main__":
    unittest.main()
