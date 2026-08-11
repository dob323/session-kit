from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO


sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_events import EventStore, append_event, build_attention_queue  # noqa: E402

CORE_PATH = REPO / "lib" / "session_inventory.py"
import importlib.util  # noqa: E402

CORE_SPEC = importlib.util.spec_from_file_location("queue_inventory", CORE_PATH)
assert CORE_SPEC is not None and CORE_SPEC.loader is not None
inventory_core = importlib.util.module_from_spec(CORE_SPEC)
sys.modules[CORE_SPEC.name] = inventory_core
CORE_SPEC.loader.exec_module(inventory_core)

UUIDS = (
    "019fdf1e-8b4c-7573-a089-be495bfece6a",
    "dcbdf940-4eda-4967-8e41-23a5760c32b5",
    "71dd26e3-8e68-4c69-946e-21f8a15d88b0",
    "97273b86-5dab-4a70-ac4d-c66e65c64bf6",
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
)


def row(index: int, provider: str, status: str, *, changed: int = 1000) -> dict:
    uuid = UUIDS[index]
    return {
        "terminal_number": index + 1,
        "provider": provider,
        "identity": {"uuid": uuid, "confidence": "exact"},
        "title": f"Task {index + 1}",
        "agent_status": status,
        "started_at_unix_ms": changed,
        "recent_output_at_unix_ms": changed,
        "availability": "ready",
    }


def inventory(*rows: dict, live: bool = True, generated_ms: int = 1000) -> dict:
    return {
        "generated_at": datetime.fromtimestamp(
            generated_ms / 1000, timezone.utc
        ).isoformat().replace("+00:00", "Z"),
        "source": "live" if live else "cache",
        "stale": not live,
        "sessions": list(rows),
    }


class AttentionQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".queue-", dir=REPO)
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_frozen_shape_buckets_and_unknown_status_default(self) -> None:
        rows = (
            row(0, "claude", "needs your reply"),
            row(1, "codex", "idle"),
            row(2, "claude", "brand new provider state"),
            row(3, "claude", "idle"),
        )
        result = build_attention_queue(inventory(*rows), self.state, now_ms=10_000)
        self.assertEqual({"as_of_unix_ms", "items"}, set(result))
        self.assertEqual(
            ["needs_you", "working", "idle", "idle"],
            [item["bucket"] for item in result["items"]],
        )
        expected_fields = {
            "thread_key",
            "terminal_number",
            "title",
            "provider",
            "bucket",
            "question",
            "waiting_ms",
            "waiting_since_unix_ms",
            "last_response_at_unix_ms",
            "stale",
        }
        self.assertTrue(all(set(item) == expected_fields for item in result["items"]))
        self.assertEqual(9000, result["items"][0]["waiting_ms"])
        self.assertEqual(1000, result["items"][0]["waiting_since_unix_ms"])
        self.assertIsNone(result["items"][0]["last_response_at_unix_ms"])
        self.assertIsNone(result["items"][0]["question"])

    def test_hook_question_outweighs_working_inventory_until_answered(self) -> None:
        item = row(0, "claude", "working", changed=1000)
        key = f"claude:{UUIDS[0]}"
        append_event(
            self.state,
            key,
            "permission_prompt",
            question="Allow the deploy?",
            source="hook",
            ts_unix_ms=5000,
        )
        result = build_attention_queue(inventory(item), self.state, now_ms=8000)
        queued = result["items"][0]
        self.assertEqual("needs_you", queued["bucket"])
        self.assertEqual("Allow the deploy?", queued["question"])
        self.assertEqual(3000, queued["waiting_ms"])
        self.assertEqual(5000, queued["waiting_since_unix_ms"])
        self.assertIsNone(queued["last_response_at_unix_ms"])

        append_event(
            self.state, key, "turn_done", source="hook", ts_unix_ms=9000
        )
        result = build_attention_queue(inventory(item), self.state, now_ms=10_000)
        self.assertEqual("finished_unseen", result["items"][0]["bucket"])
        self.assertEqual(9000, result["items"][0]["last_response_at_unix_ms"])
        self.assertIsNone(result["items"][0]["waiting_since_unix_ms"])

    def test_seen_mark_clears_finished_unseen_without_removing_history(self) -> None:
        item = row(0, "claude", "idle")
        key = f"claude:{UUIDS[0]}"
        append_event(
            self.state, key, "turn_done", source="hook", ts_unix_ms=5000
        )
        first = build_attention_queue(inventory(item), self.state, now_ms=6000)
        self.assertEqual("finished_unseen", first["items"][0]["bucket"])
        EventStore(self.state).mark_seen(key, 5000)
        second = build_attention_queue(inventory(item), self.state, now_ms=7000)
        self.assertEqual("idle", second["items"][0]["bucket"])
        self.assertEqual(1, len(EventStore(self.state).read(key)))

    def test_codex_transitions_are_synthesized_once(self) -> None:
        item = row(0, "codex", "working")
        key = f"codex:{UUIDS[0]}"
        build_attention_queue(inventory(item), self.state, now_ms=1000)
        build_attention_queue(inventory(item), self.state, now_ms=2000)
        item["agent_status"] = "needs your reply"
        build_attention_queue(inventory(item), self.state, now_ms=3000)
        item["agent_status"] = "idle"
        build_attention_queue(inventory(item), self.state, now_ms=4000)
        build_attention_queue(inventory(), self.state, now_ms=5000)
        self.assertEqual(
            ["session_start", "needs_input", "turn_done", "session_end"],
            [event["event"] for event in EventStore(self.state).read(key)],
        )
        self.assertTrue(
            all(event["source"] == "synth" for event in EventStore(self.state).read(key))
        )

    def test_first_sighting_uses_inventory_age_without_synthetic_alerts(self) -> None:
        now_ms = 7_201_000
        claude_waiting = row(0, "claude", "needs your reply", changed=1000)
        codex_waiting = row(1, "codex", "needs your reply", changed=1000)
        waiting = build_attention_queue(
            inventory(
                claude_waiting,
                codex_waiting,
                generated_ms=now_ms,
            ),
            self.state,
            now_ms=now_ms,
        )
        waits = {
            item["provider"]: item["waiting_ms"] for item in waiting["items"]
        }
        self.assertEqual(7_200_000, waits["claude"])
        self.assertEqual(waits["claude"], waits["codex"])
        self.assertEqual(
            ["session_start"],
            [
                event["event"]
                for event in EventStore(self.state).read(
                    f"codex:{codex_waiting['identity']['uuid']}"
                )
            ],
        )

        idle_rows = tuple(
            row(index, "codex", "idle", changed=1000)
            for index in range(2, 7)
        )
        idle = build_attention_queue(
            inventory(*idle_rows, generated_ms=now_ms),
            self.state,
            now_ms=now_ms,
        )
        self.assertNotIn("finished_unseen", [item["bucket"] for item in idle["items"]])
        self.assertTrue(all(item["bucket"] == "idle" for item in idle["items"]))

    def test_working_thread_is_stale_only_after_no_event_or_change_for_20m(self) -> None:
        item = row(0, "codex", "working", changed=1000)
        start = 10_000
        first = build_attention_queue(inventory(item), self.state, now_ms=start)
        self.assertFalse(first["items"][0]["stale"])
        old = build_attention_queue(
            inventory(item, generated_ms=start + 20 * 60 * 1000 + 1),
            self.state,
            now_ms=start + 20 * 60 * 1000 + 1,
        )
        self.assertTrue(old["items"][0]["stale"])
        item["recent_output_at_unix_ms"] = start + 20 * 60 * 1000 + 2
        changed = build_attention_queue(
            inventory(item, generated_ms=start + 20 * 60 * 1000 + 3),
            self.state,
            now_ms=start + 20 * 60 * 1000 + 3,
        )
        self.assertFalse(changed["items"][0]["stale"])

    def test_stale_inventory_does_not_synthesize_disappearance(self) -> None:
        item = row(0, "codex", "working")
        key = f"codex:{UUIDS[0]}"
        build_attention_queue(inventory(item), self.state, now_ms=1000)
        build_attention_queue(inventory(live=False), self.state, now_ms=2000)
        self.assertEqual(
            ["session_start"],
            [event["event"] for event in EventStore(self.state).read(key)],
        )

    def test_old_snapshot_labelled_live_does_not_synthesize_churn(self) -> None:
        item = row(0, "codex", "working")
        key = f"codex:{UUIDS[0]}"
        build_attention_queue(
            inventory(item, generated_ms=1000), self.state, now_ms=1000
        )
        build_attention_queue(
            inventory(generated_ms=1000), self.state, now_ms=122_000
        )
        build_attention_queue(
            inventory(item, generated_ms=123_000), self.state, now_ms=123_000
        )
        self.assertEqual(
            ["session_start"],
            [event["event"] for event in EventStore(self.state).read(key)],
        )

    def test_non_exact_and_non_provider_inventory_rows_are_omitted(self) -> None:
        bad = row(0, "shell", "working")
        malformed = row(1, "claude", "working")
        malformed["identity"]["uuid"] = "not-a-uuid"
        result = build_attention_queue(
            inventory(bad, malformed), self.state, now_ms=1000
        )
        self.assertEqual([], result["items"])

    def test_inventory_facade_exposes_only_msg_queue_for_this_feature(self) -> None:
        item = row(0, "claude", "idle")
        with (
            mock.patch.object(
                inventory_core,
                "load_config",
                return_value={"state_dir": os.fspath(self.state)},
            ),
            mock.patch.object(
                inventory_core, "snapshot", return_value=inventory(item)
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = inventory_core.main(["msg", "queue"])
        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("idle", payload["items"][0]["bucket"])

    def test_msg_queue_mark_seen_clears_finished_unseen(self) -> None:
        item = row(0, "claude", "idle")
        key = f"claude:{UUIDS[0]}"
        append_event(self.state, key, "turn_done", source="hook", ts_unix_ms=5000)
        with (
            mock.patch.object(
                inventory_core,
                "load_config",
                return_value={"state_dir": os.fspath(self.state)},
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = inventory_core.main(["msg", "queue", "--mark-seen", key])
        self.assertEqual(0, code)
        marked = json.loads(output.getvalue())
        self.assertEqual(key, marked["thread_key"])
        self.assertGreaterEqual(marked["seen_unix_ms"], 5000)
        queued = build_attention_queue(
            inventory(item, generated_ms=6000), self.state, now_ms=6000
        )
        self.assertEqual("idle", queued["items"][0]["bucket"])


if __name__ == "__main__":
    unittest.main()
