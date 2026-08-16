"""The five-word state column and its priority order."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

from sessionkit_inventory import labels  # noqa: E402
from sessionkit_inventory import processes  # noqa: E402
from sessionkit_inventory.model import canonical_session_order_key  # noqa: E402
from sessionkit_inventory.providers_claude import _pending_claude_tools  # noqa: E402
from sessionkit_inventory.providers_claude import _parse_claude_payload  # noqa: E402
from sessionkit_inventory.render import _display_width  # noqa: E402
from sessionkit_inventory.render import stall_threshold_seconds  # noqa: E402
from sessionkit_tui.model import stall_seconds as tui_stall_seconds  # noqa: E402
from tests.test_inventory import inventory_core, inventory_fixture, uuid_for  # noqa: E402


def claude_record(*parts: dict) -> bytes:
    return (
        json.dumps(
            {
                "type": "assistant",
                "isSidechain": False,
                "message": {"role": "assistant", "content": list(parts)},
            }
        ).encode()
        + b"\n"
    )


class ClaudeQuestionTailTests(unittest.TestCase):
    def test_unmatched_ask_user_question_is_open_until_its_exact_result(self) -> None:
        question = claude_record(
            {"type": "tool_use", "name": "AskUserQuestion", "id": "toolu-q"}
        )
        unrelated = json.dumps(
            {
                "type": "user",
                "isSidechain": False,
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu-other"}
                    ],
                },
            }
        ).encode() + b"\n"
        answer = json.dumps(
            {
                "type": "user",
                "isSidechain": False,
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu-q"}
                    ],
                },
            }
        ).encode() + b"\n"

        self.assertEqual((True, True), _pending_claude_tools(question + unrelated))
        self.assertEqual((False, False), _pending_claude_tools(question + answer))

    def test_malformed_tail_never_invents_a_question(self) -> None:
        self.assertEqual((False, False), _pending_claude_tools(b"{\nnot-json\n"))

    def test_user_record_cannot_invent_an_ask_user_question(self) -> None:
        malformed = (
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "AskUserQuestion",
                                "id": "toolu-q",
                            }
                        ],
                    },
                    "isSidechain": False,
                }
            ).encode()
            + b"\n"
        )

        ask, pending = _pending_claude_tools(malformed)
        self.assertEqual((False, False), (ask, pending))
        parsed = _parse_claude_payload(
            [
                {
                    "pid": 77,
                    "sessionId": uuid_for(7),
                    "status": "idle",
                    "_session_kit_pending_ask_user_question": ask,
                    "_session_kit_pending_tool_use": pending,
                }
            ],
            palette=(),
            attention_records={},
            attention_source="hook",
        )
        self.assertFalse(parsed[0]["blocking_question"])

    def test_permission_prompt_must_match_the_current_unresolved_tool(self) -> None:
        exact = uuid_for(7)
        item = {
            "pid": 77,
            "sessionId": exact,
            "status": "busy",
            "_session_kit_pending_tool_use": True,
            "_session_kit_pending_tool_use_at_unix_ms": 100_000,
        }
        record = {
            "notification_type": "permission_prompt",
            "needs_you": True,
            "recorded_at_ms": 105_000,
        }
        parsed = _parse_claude_payload(
            [item], palette=(), attention_records={exact: record}, attention_source="hook"
        )
        self.assertTrue(parsed[0]["blocking_question"])

        # A keypress can leave the old hook raised. A later ordinary tool must
        # not borrow that stale permission marker and become `question`.
        item["_session_kit_pending_tool_use_at_unix_ms"] = 200_000
        parsed = _parse_claude_payload(
            [item], palette=(), attention_records={exact: record}, attention_source="hook"
        )
        self.assertFalse(parsed[0]["blocking_question"])


class ChurnedQuestionCensusTests(unittest.TestCase):
    """Positive transcript evidence survives unrelated process-table churn."""

    @staticmethod
    def build(*, churned: bool, provider: str = "claude") -> dict:
        fixture = list(inventory_fixture(1, providers=(provider,)))
        if provider == "claude":
            fixture[1][0]["_session_kit_pending_ask_user_question"] = True
        else:
            fixture[3][2001][0]["_turn_state"] = "needs your reply"
        if churned:
            table = processes.ProcessTable(fixture[2])
            table.complete = False
            fixture[2] = table
        return inventory_core.build_inventory(
            *fixture,
            now=1_800_000_000,
            daemon_binding={"pid": 10, "process_start_ticks": 100},
        )["sessions"][0]

    def test_claude_question_survives_a_churned_census(self) -> None:
        complete = self.build(churned=False)
        churned = self.build(churned=True)
        self.assertTrue(complete["blocking_question"])
        self.assertTrue(churned["blocking_question"])

    def test_codex_without_a_proven_app_server_marker_never_says_question(self) -> None:
        for churned in (False, True):
            with self.subTest(churned=churned):
                row = self.build(churned=churned, provider="codex")
                self.assertTrue(row["needs_you"])
                self.assertFalse(row["blocking_question"])


class StatePriorityAndWidthTests(unittest.TestCase):
    def test_exact_valid_state_priority(self) -> None:
        rows = [
            {"needs_you": True, "transcript_idle": True, "shpool_id": "four", "availability": "ready"},
            {"agent_status": "working", "shpool_id": "three", "availability": "ready"},
            {"blocking_question": True, "shpool_id": "one", "availability": "ready"},
            {"needs_you": True, "shpool_id": "two", "availability": "ready"},
        ]
        ordered = sorted(rows, key=canonical_session_order_key)
        self.assertEqual(
            ["question", "needs you", "working", "idle"],
            [labels.session_state(row, stall_seconds=2700) for row in ordered],
        )

    def test_question_never_widens_the_needs_you_state_column(self) -> None:
        self.assertLessEqual(
            _display_width(labels.QUESTION), _display_width(labels.NEEDS_YOU)
        )

    def test_non_default_stall_value_drives_both_sorting_and_rendering(self) -> None:
        rows = [
            {
                "agent_status": "working",
                "provider": "claude",
                "recent_output_age_seconds": 3_000,
                "shpool_id": "quiet-working",
                "availability": "ready",
            },
            {
                "needs_you": True,
                "provider": "codex",
                "shpool_id": "needs-you",
                "availability": "ready",
            },
        ]
        with mock.patch.dict(
            os.environ, {"SESSION_KIT_STALL_SECONDS": "3600"}, clear=False
        ):
            ordered = sorted(rows, key=canonical_session_order_key)
            self.assertEqual(3_600, stall_threshold_seconds())
            self.assertEqual(3_600, tui_stall_seconds())
            self.assertEqual(
                ["needs you", "working"],
                [
                    labels.session_state(
                        row, stall_seconds=stall_threshold_seconds()
                    )
                    for row in ordered
                ],
            )


if __name__ == "__main__":
    unittest.main()
