from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from tests.support import REPO

from lib.sessionkit_supervisor.vendor.idle_detection import (
    DEFAULT_TIMEOUT,
    SessionInfo,
    is_claude_idle,
    is_codex_idle,
    wait_for_all_idle,
)
from lib.sessionkit_supervisor.vendor.registry import SessionRegistry


SHA = "0987ccf59552989600f6134e6602abe72a3214d0"
UUID_A = "00000000-0000-4000-8000-000000000001"
UUID_B = "00000000-0000-4000-8000-000000000002"


def inventory() -> dict:
    return {
        "sessions": [
            {
                "provider": "claude",
                "identity": {"uuid": UUID_A},
                "title": "Researcher",
                "cwd": "/srv/one",
                "agent_status": "needs your reply",
                "terminal_number": 4,
                "shpool_id_raw": "main",
                "availability": "ready",
                "started_at_unix_ms": 1_700_000_000_000,
                "recent_output_age_seconds": 20,
            },
            {
                "provider": "codex",
                "identity": {"uuid": UUID_B},
                "title": "Builder",
                "cwd": "/srv/two",
                "agent_status": "idle",
                "terminal_number": 7,
                "shpool_id_raw": "main2",
                "availability": "attached",
                "started_at_unix_ms": 1_700_000_001_000,
                "recent_output_age_seconds": 5,
            },
            {
                "provider": "shell",
                "identity": {"uuid": ""},
                "title": "not a provider worker",
            },
        ]
    }


class SupervisorRegistryTests(unittest.TestCase):
    def test_inventory_seeds_exact_thread_keys_and_preserves_status(self) -> None:
        registry = SessionRegistry.from_inventory(inventory())
        self.assertEqual(2, registry.count())
        claude = registry.get(f"claude:{UUID_A}")
        assert claude is not None
        self.assertEqual("needs your reply", claude.status)
        self.assertFalse(claude.is_idle())
        self.assertEqual("needs your reply", claude.to_dict()["agent_status"])
        codex = registry.resolve("7")
        assert codex is not None
        self.assertEqual(f"codex:{UUID_B}", codex.session_id)
        self.assertTrue(codex.is_idle())

    def test_unknown_status_is_not_hard_coded_or_collapsed(self) -> None:
        fixture = inventory()
        fixture["sessions"][0]["agent_status"] = "future-provider-state"
        worker = SessionRegistry.from_inventory(fixture).resolve("Researcher")
        assert worker is not None
        self.assertEqual("future-provider-state", worker.status)
        self.assertFalse(worker.is_idle())

    def test_last_activity_uses_registry_clock(self) -> None:
        worker = SessionRegistry.from_inventory(
            inventory(), clock=lambda: 1_700_000_100.0
        ).resolve("Researcher")
        assert worker is not None
        self.assertEqual(1_700_000_080, int(worker.last_activity.timestamp()))

    def test_ambiguous_display_selector_fails_closed(self) -> None:
        fixture = inventory()
        fixture["sessions"][1]["title"] = "Researcher"
        registry = SessionRegistry.from_inventory(fixture)
        self.assertIsNone(registry.resolve("Researcher"))
        self.assertIsNotNone(registry.resolve(f"codex:{UUID_B}"))

    def test_every_vendored_python_file_has_exact_attribution(self) -> None:
        root = REPO / "lib/sessionkit_supervisor/vendor"
        files = sorted(root.rglob("*.py")) + [
            REPO / "lib/sessionkit_supervisor/server.py"
        ]
        self.assertGreaterEqual(len(files), 11)
        for path in files:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("github.com/Martian-Engineering/maniple", text)
                self.assertIn(SHA, text)
                self.assertIn("MIT", text)


class ProviderJsonlIdleTests(unittest.TestCase):
    def test_claude_stop_marker_must_be_latest_for_that_worker(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".supervisor-idle-", dir=REPO) as raw:
            path = Path(raw) / "claude.jsonl"
            rows = [
                {"type": "assistant", "message": {"content": "done"}},
                {
                    "type": "system",
                    "subtype": "stop_hook_summary",
                    "hookInfos": [{"command": "echo [worker-done:worker-1]"}],
                },
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            self.assertTrue(is_claude_idle(path, "worker-1"))
            self.assertFalse(is_claude_idle(path, "worker-2"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "user", "message": {"content": "more"}}) + "\n")
            self.assertFalse(is_claude_idle(path, "worker-1"))

    def test_codex_last_decisive_lifecycle_event_controls_idle(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".supervisor-codex-", dir=REPO) as raw:
            path = Path(raw) / "rollout.jsonl"
            path.write_text(
                json.dumps({"type": "event_msg", "payload": {"type": "task_started"}})
                + "\n"
                + json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}})
                + "\n",
                encoding="utf-8",
            )
            self.assertTrue(is_codex_idle(path))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message"}}) + "\n")
            self.assertFalse(is_codex_idle(path))

    def test_vendored_wait_contract_returns_immediately_for_idle_cohort(self) -> None:
        self.assertEqual(600.0, DEFAULT_TIMEOUT)
        with tempfile.TemporaryDirectory(prefix=".supervisor-wait-", dir=REPO) as raw:
            path = Path(raw) / "codex.jsonl"
            path.write_text(
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n",
                encoding="utf-8",
            )
            result = asyncio.run(
                wait_for_all_idle(
                    [SessionInfo(path, "codex:one", "codex")],
                    timeout=1,
                    poll_interval=0.05,
                )
            )
            self.assertTrue(result["all_idle"])
            self.assertFalse(result["timed_out"])


if __name__ == "__main__":
    unittest.main()
