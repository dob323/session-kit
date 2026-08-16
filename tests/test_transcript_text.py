"""Unit contracts for the provider-neutral conversation renderer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))
from sessionkit_inventory import transcript_text  # noqa: E402


def codex(payload: dict) -> str:
    return json.dumps({"type": "response_item", "payload": payload})


class TranscriptTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".transcript-text-", dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name: str, *records: str) -> Path:
        path = self.root / name
        path.write_text("\n".join(records) + "\n", encoding="utf-8")
        return path

    def test_claude_and_codex_use_one_visual_language(self) -> None:
        claude = self.write(
            "claude.jsonl",
            json.dumps(
                {"type": "user", "message": {"content": "Find the chorus"}}
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [
                        {"type": "tool_use", "name": "Search", "input": {"pattern": "chorus"}},
                        {"type": "text", "text": "Found it."},
                    ]},
                }
            ),
            json.dumps(
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "content": "line 1\nline 2"}
                ]}}
            ),
        )
        codex_path = self.write(
            "codex.jsonl",
            codex({"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Find the chorus"}
            ]}),
            codex({"type": "function_call", "name": "Search", "call_id": "1",
                   "arguments": json.dumps({"pattern": "chorus"})}),
            codex({"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Found it."}
            ]}),
            codex({"type": "function_call_output", "call_id": "1",
                   "output": "line 1\nline 2"}),
        )
        self.assertEqual(
            transcript_text.render_transcript(claude),
            transcript_text.render_rollout(codex_path),
        )

    def test_both_codex_tool_families_and_block_outputs_render(self) -> None:
        path = self.write(
            "tools.jsonl",
            codex({"type": "custom_tool_call", "name": "exec", "call_id": "a",
                   "input": "run tests"}),
            codex({"type": "custom_tool_call_output", "call_id": "a", "output": [
                {"type": "input_text", "text": "54 passed"}
            ]}),
            codex({"type": "function_call", "name": "open", "call_id": "b",
                   "arguments": "{"}),
            codex({"type": "function_call_output", "call_id": "b", "output": "done"}),
        )
        self.assertEqual(
            ["⏺ exec: run tests", "  │ 54 passed", "⏺ open: {", "  │ done"],
            transcript_text.render_rollout(path),
        )

    def test_tool_output_is_bounded_and_empty_output_disappears(self) -> None:
        long_output = "\n".join(f"line {number}" for number in range(13))
        path = self.write(
            "bounded.jsonl",
            codex({"type": "function_call_output", "call_id": "a", "output": long_output}),
            codex({"type": "function_call_output", "call_id": "b", "output": ""}),
            "not json",
        )
        lines = transcript_text.render_rollout(path)
        self.assertEqual(1, len(lines))
        self.assertIn("  … (1 more lines)", lines[0])
        self.assertNotIn("line 12", lines[0])

    def test_scaffolding_records_disappear_but_markup_prompts_remain(self) -> None:
        path = self.write(
            "scaffold.jsonl",
            codex({"type": "message", "role": "user", "content": [{
                "type": "input_text",
                "text": "<recommended_plugins>noise</recommended_plugins>\n"
                        "# AGENTS.md instructions for /srv/project\n\n"
                        "<INSTRUCTIONS>entire rulebook</INSTRUCTIONS>\n"
                        "<environment_context>cwd</environment_context>",
            }]}),
            codex({"type": "message", "role": "user", "content": [{
                "type": "input_text", "text": "<tag>Keep this request</tag>"
            }]}),
        )
        body = "\n".join(transcript_text.render_rollout(path))
        self.assertNotIn("rulebook", body)
        self.assertNotIn("/srv/project", body)
        self.assertIn("<tag>Keep this request</tag>", body)

    def test_attributed_harness_envelopes_split_away_from_operator_text(self) -> None:
        path = self.write(
            "attributed-scaffold.jsonl",
            codex({"type": "message", "role": "user", "content": [{
                "type": "input_text",
                "text": '<codex_internal_context source="goal">machine continuation'
                        "</codex_internal_context>",
            }]}),
            codex({"type": "message", "role": "user", "content": [{
                "type": "input_text",
                "text": '<hook_prompt hook_run_id="stop:15:internal-id">machine hook'
                        "</hook_prompt>\nFix the real request.",
            }]}),
        )

        body = "\n".join(transcript_text.render_rollout(path))

        self.assertNotIn("machine continuation", body)
        self.assertNotIn("machine hook", body)
        self.assertNotIn("internal-id", body)
        self.assertIn("Fix the real request.", body)

    def test_real_rollout_subagent_final_answer_is_a_bounded_result(self) -> None:
        # Structurally exact, redacted excerpt from rollout 019ffa14-5b4c.
        result_lines = [f"finding {number}" for number in range(14)]
        path = self.write(
            "real-agent-result.jsonl",
            codex({
                "type": "agent_message",
                "author": "/root/history_review",
                "recipient": "/root",
                "content": [{
                    "type": "input_text",
                    "text": "Message Type: FINAL_ANSWER\n"
                            "Task name: /root/history_review\n"
                            "Sender: /root/history_review\nPayload:\n"
                            + "\n".join(result_lines),
                }],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "internal-turn-id"
                },
            }),
            codex({
                "type": "agent_message",
                "author": "/root",
                "recipient": "/root/worker",
                "content": [{
                    "type": "input_text",
                    "text": "Message Type: NEW_TASK\nTask name: /root/worker\n"
                            "Sender: /root\nPayload:\n",
                }, {
                    "type": "encrypted_content", "encrypted_content": "ciphertext"
                }],
            }),
        )

        body = "\n".join(transcript_text.render_rollout(path))

        self.assertIn("⏺ sub-agent result: /root/history_review", body)
        self.assertIn("  │ finding 0", body)
        self.assertIn("  … (2 more lines)", body)
        self.assertNotIn("finding 12", body)
        self.assertNotIn("internal-turn-id", body)
        self.assertNotIn("ciphertext", body)
        self.assertNotIn("NEW_TASK", body)

    def test_subagent_result_bounds_a_single_long_line(self) -> None:
        path = self.write(
            "long-agent-result.jsonl",
            codex({
                "type": "agent_message",
                "author": "/root/worker",
                "recipient": "/root",
                "content": [{
                    "type": "input_text",
                    "text": "Message Type: FINAL_ANSWER\nTask name: /root/worker\n"
                            "Sender: /root/worker\nPayload:\n"
                            + ("x" * (transcript_text.MAX_AGENT_RESULT_CHARS + 500)),
                }],
            }),
        )

        lines = transcript_text.render_rollout(path)

        self.assertEqual(2, len(lines))
        self.assertLess(len(lines[1]), transcript_text.MAX_AGENT_RESULT_CHARS + 100)
        self.assertIn("… (more text)", lines[1])

    def test_real_rollout_compaction_restores_and_labels_the_beginning(self) -> None:
        # Structurally exact, redacted excerpt from rollout 019fb04f-29ea.
        opening = {"type": "message", "role": "user", "content": [{
            "type": "input_text", "text": "Original overnight request"
        }]}
        overlap = {"type": "message", "role": "user", "content": [{
            "type": "input_text", "text": "Formerly first visible request"
        }]}
        compacted = json.dumps({
            "type": "compacted",
            "payload": {
                "message": "",
                "replacement_history": [
                    opening,
                    {"type": "message", "role": "developer", "content": [{
                        "type": "input_text", "text": "private harness rulebook"
                    }]},
                    overlap,
                    {"type": "compaction", "encrypted_content": "ciphertext"},
                ],
                "window_number": 1,
                "window_id": "internal-window-id",
            },
        })
        path = self.write(
            "real-compacted-history.jsonl",
            codex(overlap),
            codex({"type": "function_call", "name": "exec", "call_id": "1",
                   "arguments": json.dumps({"cmd": "pre-compaction check"})}),
            compacted,
            codex({"type": "message", "role": "user", "content": [{
                "type": "input_text", "text": "Request after compaction"
            }]}),
            compacted,
        )

        body = "\n".join(transcript_text.render_rollout(path))

        self.assertTrue(body.lstrip().startswith("══ COMPACTED HISTORY"), body)
        self.assertLess(body.index("Original overnight request"),
                        body.index("Formerly first visible request"))
        self.assertEqual(1, body.count("Original overnight request"))
        self.assertEqual(1, body.count("Formerly first visible request"))
        self.assertEqual(1, body.count("══ COMPACTED HISTORY"))
        self.assertIn("══ CONTINUED HISTORY", body)
        self.assertIn("⏺ exec: pre-compaction check", body)
        self.assertLess(body.index("Formerly first visible request"),
                        body.index("Request after compaction"))
        self.assertNotIn("private harness rulebook", body)
        self.assertNotIn("internal-window-id", body)
        self.assertNotIn("ciphertext", body)

    def test_request_after_agents_scaffolding_is_preserved(self) -> None:
        path = self.write(
            "scaffold-with-request.jsonl",
            codex({"type": "message", "role": "user", "content": [{
                "type": "input_text",
                "text": "# AGENTS.md instructions for /srv/project\n\n"
                        "<INSTRUCTIONS>entire rulebook</INSTRUCTIONS>\n"
                        "<environment_context>cwd</environment_context>\n"
                        "Fix the broken chorus search.",
            }]}),
        )
        body = "\n".join(transcript_text.render_rollout(path))
        self.assertNotIn("rulebook", body)
        self.assertNotIn("environment_context", body)
        self.assertIn("Fix the broken chorus search.", body)

    def test_operator_use_of_agents_heading_is_preserved(self) -> None:
        path = self.write(
            "quoted-heading.jsonl",
            codex({"type": "message", "role": "user", "content": [{
                "type": "input_text",
                "text": "# AGENTS.md instructions\n"
                        "Explain what this heading means in the rendered history.",
            }]}),
        )
        body = "\n".join(transcript_text.render_rollout(path))
        self.assertIn("# AGENTS.md instructions", body)
        self.assertIn("Explain what this heading means", body)


if __name__ == "__main__":
    unittest.main()
