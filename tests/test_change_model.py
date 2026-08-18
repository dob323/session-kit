"""Which model a session is on, and moving it to another one.

Two promises. Every row says what model its provider is running, because a
person deciding where to send the next hour of work cannot see that anywhere
else. And a conversation can move to another model without being retyped: the
provider restarts on the model asked for and resumes the exact same
conversation.

The safety is the account switch's, because the risk is the same one, a
restart in the middle of a turn loses that turn. A single stable row, a
conversation that is not mid-computation, no subagents, and a process tree with
nothing unrecognized in it, or the answer is one refusal line and nothing
changed. Nothing is ever asked (D7); automation names the exact session,
exactly as it does to close one.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest

from tests.support import REPO, run
from tests.test_commands import CommandFixture

SP = REPO / "bin" / "sp"
CORE = REPO / "lib" / "session_inventory.py"
SESSION = "s20260101-000000-1"
UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def facade() -> object:
    sys.path.insert(0, os.fspath(REPO / "lib"))
    spec = importlib.util.spec_from_file_location("session_inventory_model", CORE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ModelColumnTests(unittest.TestCase):
    """The model comes off the live provider, or the row says nothing."""

    def setUp(self) -> None:
        self.core = facade()

    def row(self, pid: int | None = 4242) -> dict:
        return {"identity": {"pid": pid}}

    def test_the_command_line_model_is_the_answer(self) -> None:
        self.assertEqual(
            "claude-opus-5",
            self.core._session_model(
                self.row(),
                {4242: {"cmdline": ["claude", "--model", "claude-opus-5", "--resume", UUID]}},
            ),
        )

    def test_the_launch_environment_answers_when_the_command_line_does_not(self) -> None:
        self.assertEqual(
            "gpt-5.4",
            self.core._session_model(
                self.row(),
                {4242: {"cmdline": ["codex"], "requested_model": "gpt-5.4"}},
            ),
        )

    def test_a_session_on_the_providers_own_default_claims_no_model(self) -> None:
        self.assertEqual(
            "", self.core._session_model(self.row(), {4242: {"cmdline": ["claude"]}})
        )

    def test_a_row_with_no_live_provider_claims_no_model(self) -> None:
        self.assertEqual("", self.core._session_model(self.row(None), {}))
        self.assertEqual("", self.core._session_model({}, {}))

    def test_control_bytes_never_reach_the_column(self) -> None:
        self.assertEqual(
            "opus 5",
            self.core._session_model(
                self.row(),
                {4242: {"cmdline": ["claude", "--model", "opus\r\n5"]}},
            ),
        )


class ChangeModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CommandFixture()
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": SESSION,
                            "status": "Disconnected",
                            "started_at_unix_ms": 1_700_000_000_001,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.fixture.close()

    def env(self, **overrides: str) -> dict[str, str]:
        table = {
            "processes": [
                {"pid": 1001, "ppid": 1, "start_ticks": 10001, "cmdline": ["bash"]},
                {
                    "pid": 2001,
                    "ppid": 1001,
                    "start_ticks": 20001,
                    "cmdline": ["claude", "--model", "claude-old"],
                },
            ]
        }
        env = {
            **self.fixture.env(),
            "STUB_DYNAMIC_PROVIDER": "claude",
            "STUB_DYNAMIC_UUID": UUID,
            "STUB_DYNAMIC_CWD": str(self.fixture.project),
            "STUB_DYNAMIC_AGENT_STATUS": "idle",
            "STUB_DYNAMIC_MODEL": "claude-old",
            "STUB_PROCESS_TABLE": json.dumps(table),
            "SESSION_KIT_CONFIRM_ID": SESSION,
            "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
        }
        env.update(overrides)
        return env

    def closed_records(self) -> list[list[str]]:
        if not self.fixture.closed_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.fixture.closed_log.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def test_an_idle_conversation_moves_to_the_model_asked_for(self) -> None:
        moved = run([SP, "change-model", SESSION, "claude-new"], env=self.env())
        self.assertIn("Restored", moved.stdout)
        self.assertIn("claude-new", moved.stdout)
        log = self.fixture.shpool_log.read_text(encoding="utf-8").split()
        self.assertEqual(["attach-exit", SESSION, "attach"], log[:3])
        # The same conversation, resumed, on the new model: the launch record
        # the session shell reads names both.
        launches = sorted(self.fixture.start.glob("*.launch"))
        self.assertEqual(1, len(launches), launches)
        fields = launches[0].read_text(encoding="utf-8").rstrip("\n").split("\t")
        self.assertEqual("claude", fields[0])
        self.assertEqual("claude-new", fields[2])
        # The proven start clears its own records; what stays is the model
        # request the session shell reads when it launches the provider.
        self.assertEqual(
            [path.name for path in launches],
            sorted(path.name for path in self.fixture.start.iterdir()),
        )
        # The old conversation was closed on purpose. Its findable evidence
        # remains available to the shared live projection rather than being
        # deleted after one point-in-time restore check.
        intents = self.fixture.close_intent_log.read_text(encoding="utf-8")
        self.assertIn(UUID, intents)

    def test_an_idle_codex_conversation_moves_to_the_model_asked_for(self) -> None:
        table = {
            "processes": [
                {"pid": 1001, "ppid": 1, "start_ticks": 10001, "cmdline": ["bash"]},
                {
                    "pid": 2001,
                    "ppid": 1001,
                    "start_ticks": 20001,
                    "cmdline": ["codex", "--model", "gpt-old"],
                },
            ]
        }
        moved = run(
            [SP, "change-model", SESSION, "gpt-new"],
            env=self.env(
                STUB_DYNAMIC_PROVIDER="codex",
                STUB_DYNAMIC_MODEL="gpt-old",
                STUB_PROCESS_TABLE=json.dumps(table),
            ),
        )
        self.assertIn("Restored", moved.stdout)
        self.assertIn("gpt-new", moved.stdout)
        log = self.fixture.shpool_log.read_text(encoding="utf-8").split()
        self.assertEqual(["attach-exit", SESSION, "attach"], log[:3])
        launches = sorted(self.fixture.start.glob("*.launch"))
        self.assertEqual(1, len(launches), launches)
        fields = launches[0].read_text(encoding="utf-8").rstrip("\n").split("\t")
        self.assertEqual("codex", fields[0])
        self.assertEqual("gpt-new", fields[2])
        records = self.closed_records()
        self.assertNotIn(["closed-sessions", "forget", "codex", UUID], records)
        intents = self.fixture.close_intent_log.read_text(encoding="utf-8")
        self.assertIn(UUID, intents)

    def test_reopening_retains_the_closed_evidence_for_self_healing(self) -> None:
        run([SP, "change-model", SESSION, "claude-new"], env=self.env())
        records = self.closed_records()
        self.assertNotIn(["closed-sessions", "forget", "claude", UUID], records)

    def test_a_machine_session_stays_a_machine_session_after_the_restart(self) -> None:
        run(
            [SP, "change-model", SESSION, "claude-new"],
            env=self.env(STUB_DYNAMIC_ORIGIN="machine"),
        )
        stamps = [
            json.loads(line)
            for line in self.fixture.origin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual("machine", stamps[-1][1])

    def test_a_working_session_keeps_its_model(self) -> None:
        refused = run(
            [SP, "change-model", SESSION, "claude-new"],
            env=self.env(STUB_DYNAMIC_AGENT_STATUS="working"),
            check=False,
        )
        self.assertEqual(1, refused.returncode)
        self.assertEqual(
            "session-kit: that session is working, so its model was not changed;"
            " try again when it is idle\n",
            refused.stderr,
        )
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_a_turn_that_starts_while_the_model_is_chosen_is_not_killed(self) -> None:
        env = self.env()
        env["STUB_DYNAMIC_AGENT_STATUS_AFTER_FIRST"] = "working"

        refused = run(
            [SP, "change-model", SESSION, "claude-new"],
            env=env,
            check=False,
        )
        self.assertEqual(1, refused.returncode)
        self.assertIn("started working", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_an_unrecognized_child_process_keeps_its_model(self) -> None:
        table = {
            "processes": [
                {"pid": 1001, "ppid": 1, "start_ticks": 10001, "cmdline": ["bash"]},
                {"pid": 2001, "ppid": 1001, "start_ticks": 20001, "cmdline": ["claude"]},
                {"pid": 3001, "ppid": 1001, "start_ticks": 30001, "cmdline": ["rsync"]},
            ]
        }
        refused = run(
            [SP, "change-model", SESSION, "claude-new"],
            env=self.env(STUB_PROCESS_TABLE=json.dumps(table)),
            check=False,
        )
        self.assertEqual(1, refused.returncode)
        self.assertIn("does not recognize", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_an_explicit_model_request_is_applied_despite_last_reply_evidence(self) -> None:
        changed = run(
            [SP, "change-model", SESSION, "claude-old"], env=self.env(), check=False
        )
        self.assertEqual(0, changed.returncode)
        self.assertIn("Restored", changed.stdout)
        self.assertIn("claude-old", changed.stdout)
        self.assertEqual(
            ["attach-exit", SESSION, "attach"],
            self.fixture.shpool_log.read_text(encoding="utf-8").split()[:3],
        )
        launches = sorted(self.fixture.start.glob("*.launch"))
        self.assertEqual(1, len(launches), launches)
        self.assertEqual(
            "claude-old",
            launches[0].read_text(encoding="utf-8").rstrip("\n").split("\t")[2],
        )

    def test_an_unsafe_model_identifier_is_refused(self) -> None:
        refused = run(
            [SP, "change-model", SESSION, "claude new; rm -rf /"],
            env=self.env(),
            check=False,
        )
        self.assertEqual(2, refused.returncode)
        self.assertEqual(
            "session-kit: unsupported or unsafe claude model identifier\n",
            refused.stderr,
        )
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_a_shell_session_has_no_model_to_change(self) -> None:
        refused = run(
            [SP, "change-model", SESSION, "claude-new"],
            env=self.env(STUB_DYNAMIC_PROVIDER="shell", STUB_DYNAMIC_UUID=""),
            check=False,
        )
        self.assertEqual(1, refused.returncode)
        self.assertEqual(
            "session-kit: only a Claude or Codex session runs on a model\n",
            refused.stderr,
        )

    def test_automation_names_the_exact_session_or_nothing_changes(self) -> None:
        env = self.env()
        env.pop("SESSION_KIT_CONFIRM_ID")
        refused = run(
            [SP, "change-model", SESSION, "claude-new"], env=env, check=False
        )
        self.assertEqual(1, refused.returncode)
        self.assertEqual("Nothing changed.\n", refused.stdout)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_no_screen_prints_a_session_id_or_a_conversation_uuid(self) -> None:
        moved = run([SP, "change-model", SESSION, "claude-new"], env=self.env())
        self.assertNotIn(SESSION, moved.stdout)
        self.assertNotIn(UUID, moved.stdout)


if __name__ == "__main__":
    unittest.main()
