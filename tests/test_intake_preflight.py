"""Supervisor preflight and gated worker launch lifecycle."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import unittest

from tests.support import REPO

import sys

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor import intake  # noqa: E402
from sessionkit_supervisor.source_authority import capture_hook_event  # noqa: E402


SOURCE_UUID = "019fdf1e-8b4c-7573-a089-be495bfece6a"
SUPERVISOR_UUID = "dcbdf940-4eda-4967-8e41-23a5760c32b5"
OTHER_SUPERVISOR_UUID = "96d743aa-1111-4222-8333-444444444444"
CLAUDE_WORKER_UUID = "00000000-0000-4000-8000-000000000001"
CODEX_WORKER_UUID = "00000000-0000-4000-8000-000000000002"


class PreflightCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".intake-preflight-", dir=REPO
        )
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir(mode=0o700)
        self.transcript_root = self.state / "test-transcripts"
        self.transcript_root.mkdir(mode=0o700)
        self.spool = intake.Spool(self.state)
        self.prompt = "audit the fleet and implement the exact approved fix"
        self.event = self.capture("turn-1", self.prompt)
        outcome = intake.produce(
            self.spool,
            thread_key=f"codex:{SOURCE_UUID}",
            prompt=self.prompt,
            turn_id="turn-1",
            source_event_id=self.event["event_id"],
            source_digest=self.event["prompt_sha256"],
        )
        self.msg_id = outcome["entry"]["msg_id"]
        self.delivered: list[dict] = []
        self.name_supervisor(SUPERVISOR_UUID)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self, turn: str, prompt: str) -> dict:
        return capture_hook_event(
            {
                "provider": "codex",
                "session_id": SOURCE_UUID,
                "turn_id": turn,
                "prompt": prompt,
                "transcript_path": os.fspath(self.transcript_root / f"{turn}.jsonl"),
            },
            state_dir=self.state,
        )

    def name_supervisor(self, uuid: str) -> None:
        root = self.state / "supervisor"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / "identity"
        path.write_text(f"claude:{uuid}\n", encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def plan() -> list[dict]:
        return [
            {
                "branch": "agent-research",
                "idempotency_key": "worker:research:1",
                "workstream": "source and risk analysis",
                "scope": "independently audit requirements and failure modes",
                "provider": "claude",
                "requested_model": "claude-opus-test",
                "expertise": "security",
                "rationale": "independent analysis before integration",
                "task_text": "audit the requirements and name every failure mode",
                "acceptance_criteria": "every declared risk has a named mitigation",
                "deliverable": "a written risk analysis on the branch",
            },
            {
                "branch": "agent-implementation",
                "idempotency_key": "worker:implementation:1",
                "workstream": "implementation and tests",
                "scope": "implement the bounded code and run verification",
                "provider": "codex",
                "requested_model": "gpt-codex-test",
                "expertise": "implementation",
                "rationale": "separate implementation expertise and model family",
                "task_text": "implement the approved change and its tests",
                "acceptance_criteria": "the full suite passes on the branch",
                "deliverable": "commits on the branch plus the test output",
            },
        ]

    def preflight(self, plan: list[dict] | None = None, **overrides: object) -> dict:
        fields = {
            "analysis": "read the exact source event and inspected current intake state",
            "scope": "the current source event and its complete ordered requirements",
            "required_expertise": "security analysis plus Python state-machine implementation",
            "required_expertise_tags": ("security", "implementation"),
            "worker_plan": plan if plan is not None else self.plan(),
            "risks": "duplicate launch, stale authority, provider drift, and incomplete tests",
            "tests": "identity, mismatch, crash, concurrency, amendment, and full-suite tests",
        }
        fields.update(overrides)
        return intake.preflight(
            self.spool,
            msg_id=self.msg_id,
            source_event_id=self.event["event_id"],
            state_dir=self.state,
            **fields,
        )

    @staticmethod
    def inventory_proof(assignment: dict) -> dict:
        uuid = (
            CLAUDE_WORKER_UUID
            if assignment["provider"] == "claude"
            else CODEX_WORKER_UUID
        )
        return {
            "provider": assignment["provider"],
            "actual_model": assignment["requested_model"],
            "worker_identity": f"{assignment['provider']}:{uuid}",
            "inventory_verified": True,
            "launch_idempotency_key": assignment["idempotency_key"],
        }

    @staticmethod
    def launch(_assignment: dict) -> None:
        return None

    def courier(self, *, thread_key: str, text: str, key: str) -> dict:
        """A send that lands, recording what the worker was actually told."""
        self.delivered.append({"thread_key": thread_key, "text": text, "key": key})
        return {
            "msg_id": "cc33dd44",
            "targets": [{"thread_key": thread_key, "status": "delivered-woke"}],
        }

    def delegate(
        self, launcher=None, reconciler=None, branches=None, workers=(), deliver=None
    ):
        return intake.delegate(
            self.spool,
            msg_id=self.msg_id,
            branches=tuple(branches or (row["branch"] for row in self.plan())),
            workers=workers,
            launcher=launcher or self.launch,
            reconciler=reconciler or self.inventory_proof,
            deliver=deliver or self.courier,
            state_dir=self.state,
        )


class PreflightPolicyTests(PreflightCase):
    def test_received_and_acknowledged_intakes_refuse_delegate_without_preflight(self) -> None:
        with self.assertRaisesRegex(intake.IntakeError, "reviewed preflight"):
            self.delegate()
        intake.acknowledge(
            self.spool,
            msg_id=self.msg_id,
            text="accepted for analysis",
            reply=lambda **_fields: {},
            deliver=lambda **fields: {
                "msg_id": "aa11bb22",
                "targets": [
                    {
                        "thread_key": fields["thread_key"],
                        "status": "delivered-woke",
                    }
                ],
            },
        )
        with self.assertRaisesRegex(intake.IntakeError, "reviewed preflight"):
            self.delegate()

    def test_preflight_binds_full_requirements_and_refuses_partial_analysis(self) -> None:
        with self.assertRaises(intake.IntakeError):
            self.preflight(analysis="")
        result = self.preflight()
        row = result["preflight"]
        self.assertEqual(0, row["requirements_revision"])
        self.assertEqual(intake.requirements_digest(result["entry"]), row["requirements_digest"])
        self.assertEqual(f"claude:{SUPERVISOR_UUID}", row["supervisor_thread_key"])
        self.assertEqual("hook-ledger", row["verification_basis"])

    def test_a_plan_of_any_composition_that_covers_the_declared_needs_is_accepted(
        self,
    ) -> None:
        """Composition is the plan's business; covering the stated need is not."""
        one_claude_worker = [
            {**self.plan()[0], "expertise": "implementation"},
        ]
        two_claude_workers = [
            {**self.plan()[0]},
            {
                **self.plan()[1],
                "provider": "claude",
                "requested_model": "claude-opus-test",
                "expertise": "implementation",
            },
        ]
        cases = (
            (one_claude_worker, ("implementation",)),
            (two_claude_workers, ("security", "implementation")),
            (self.plan(), ("implementation",)),
        )
        for plan, tags in cases:
            with self.subTest(size=len(plan), tags=tags):
                result = self.preflight(plan=plan, required_expertise_tags=tags)
                self.assertEqual(list(tags), result["preflight"]["required_expertise_tags"])
                self.assertEqual(len(plan), len(result["preflight"]["worker_plan"]))

    def test_a_plan_that_misses_a_declared_need_is_refused(self) -> None:
        with self.assertRaisesRegex(intake.IntakeError, "does not cover"):
            self.preflight(required_expertise_tags=("security", "operations"))
        with self.assertRaisesRegex(intake.IntakeError, "needs the expertise"):
            self.preflight(required_expertise_tags=())
        with self.assertRaises(intake.IntakeError):
            self.preflight(required_expertise_tags=("security", "security"))
        with self.assertRaises(intake.IntakeError):
            self.preflight(required_expertise_tags=("wizardry",))

    def test_a_planned_worker_without_a_duty_is_refused(self) -> None:
        for field in ("task_text", "acceptance_criteria", "deliverable"):
            plan = [dict(row) for row in self.plan()]
            plan[1][field] = ""
            with self.subTest(field=field), self.assertRaisesRegex(
                intake.IntakeError, "required"
            ):
                self.preflight(plan=plan)

    def test_automatic_delegate_must_launch_the_complete_plan(self) -> None:
        self.preflight()
        with self.assertRaisesRegex(intake.IntakeError, "complete reviewed plan"):
            self.delegate(branches=("agent-research",))

    def test_new_amendment_invalidates_preflight_for_future_workers(self) -> None:
        self.preflight()
        amendment_prompt = "also add the provider mismatch regression test"
        amendment = self.capture("turn-2", amendment_prompt)
        intake.produce(
            self.spool,
            thread_key=f"codex:{SOURCE_UUID}",
            prompt=amendment_prompt,
            turn_id="turn-2",
            source_event_id=amendment["event_id"],
            source_digest=amendment["prompt_sha256"],
        )
        with self.assertRaisesRegex(intake.IntakeError, "current requirements"):
            self.delegate()

    def test_supervisor_identity_change_requires_new_preflight(self) -> None:
        self.preflight()
        self.name_supervisor(OTHER_SUPERVISOR_UUID)
        with self.assertRaisesRegex(intake.IntakeError, "this supervisor"):
            self.delegate()

    def test_requested_or_actual_provider_model_mismatch_fails_closed(self) -> None:
        self.preflight()
        with self.assertRaisesRegex(intake.IntakeError, "provider differs"):
            self.delegate(
                workers=(
                    {
                        "branch": "agent-research",
                        "provider": "codex",
                        "requested_model": "claude-opus-test",
                    },
                ),
                branches=("agent-implementation",),
            )

        # A fresh intake proves the post-launch actual-model gate separately.
        self.tearDown()
        self.setUp()
        self.preflight()

        def wrong_model(assignment: dict) -> dict:
            result = self.inventory_proof(assignment)
            result["actual_model"] = "unexpected-model"
            return result

        with self.assertRaisesRegex(intake.IntakeError, "inventory model differs"):
            self.delegate(reconciler=wrong_model)
        workers = self.spool.read_entry(self.msg_id)["workers"]
        self.assertIn(workers[0]["launch_state"], ("dispatching", "provider_reconciled"))


class LaunchLifecycleTests(PreflightCase):
    def test_amendment_race_rebases_only_untouched_old_reservation_once(self) -> None:
        self.preflight()

        def crash_first(assignment: dict) -> None:
            raise RuntimeError(f"crash after {assignment['idempotency_key']}")

        with self.assertRaisesRegex(intake.IntakeError, "uncertain"):
            self.delegate(launcher=crash_first)
        before = self.spool.read_entry(self.msg_id)["workers"]
        self.assertEqual("dispatching", before[0]["launch_state"])
        self.assertEqual("not_started", before[1]["launch_state"])
        old_first_key = before[0]["idempotency_key"]

        amendment_prompt = "also bind untouched reservations to the new generation"
        amendment = self.capture("turn-rebase", amendment_prompt)
        intake.produce(
            self.spool, thread_key=f"codex:{SOURCE_UUID}", prompt=amendment_prompt,
            turn_id="turn-rebase", source_event_id=amendment["event_id"],
            source_digest=amendment["prompt_sha256"],
        )
        new_plan = [
            {**row, "idempotency_key": row["idempotency_key"] + ":revision-1"}
            for row in self.plan()
        ]
        self.event = amendment
        self.preflight(plan=new_plan)
        with self.assertRaisesRegex(intake.IntakeError, "stale requirements"):
            self.delegate(branches=("agent-research",))
        entered = threading.Event()
        release = threading.Event()
        launch_keys = []

        def blocked_launch(assignment: dict) -> None:
            launch_keys.append(assignment["idempotency_key"])
            entered.set()
            release.wait(2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                self.delegate,
                branches=("agent-implementation",),
                launcher=blocked_launch,
                reconciler=self.inventory_proof,
            )
            self.assertTrue(entered.wait(2))
            second = executor.submit(
                self.delegate,
                branches=("agent-implementation",),
                launcher=blocked_launch,
                reconciler=self.inventory_proof,
            )
            second.result(timeout=2)
            release.set()
            result = first.result(timeout=2)
        self.assertEqual(1, len(launch_keys))
        workers = result["entry"]["workers"]
        self.assertEqual("dispatching", workers[0]["launch_state"])
        self.assertEqual(old_first_key, workers[0]["idempotency_key"])
        self.assertEqual("commissioned", workers[1]["launch_state"])
        self.assertTrue(workers[1]["idempotency_key"].endswith(":revision-1"))
        self.assertEqual(1, workers[1]["intake_generation"])
        self.assertEqual(intake.requirements_digest(result["entry"]), workers[1]["requirements_digest"])

    def test_reservations_are_durable_before_launcher_callback(self) -> None:
        self.preflight()
        observed = []

        def launcher(assignment: dict) -> dict:
            stored = self.spool.read_entry(self.msg_id)
            observed.append(
                [row["launch_state"] for row in stored["workers"]]
            )
            return {"untrusted": "launcher self-report is ignored"}

        result = self.delegate(launcher=launcher)
        self.assertEqual([["dispatching", "not_started"], ["verified", "dispatching"]], observed)
        self.assertEqual(["agent-research", "agent-implementation"], result["delegated"])
        for row in result["entry"]["workers"]:
            self.assertEqual("commissioned", row["launch_state"])
            self.assertEqual(row["requested_model"], row["verified_actual_model"])
            self.assertTrue(row["worker_identity"])
            self.assertTrue(row["idempotency_key"])

    def test_crash_retains_reservation_and_retry_does_not_relaunch(self) -> None:
        self.preflight()
        calls = []

        def crash(assignment: dict) -> dict:
            calls.append(assignment["idempotency_key"])
            raise RuntimeError("agent crashed after spawn")

        with self.assertRaisesRegex(intake.IntakeError, "uncertain"):
            self.delegate(launcher=crash)
        with self.assertRaisesRegex(intake.IntakeError, "inventory reconciliation failed"):
            self.delegate(launcher=crash, reconciler=lambda _row: (_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(1, len(calls))
        self.assertTrue(
            all(
                row["launch_state"] in ("dispatching", "not_started")
                for row in self.spool.read_entry(self.msg_id)["workers"]
            )
        )

    def test_concurrent_delegate_calls_launch_each_assignment_once(self) -> None:
        self.preflight()
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def launcher(assignment: dict) -> dict:
            calls.append(assignment["idempotency_key"])
            if len(calls) == 1:
                entered.set()
                release.wait(2)
            return self.launch(assignment)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.delegate, launcher=launcher)
            self.assertTrue(entered.wait(2))
            second = executor.submit(self.delegate, launcher=launcher)
            second_result = second.result(timeout=2)
            release.set()
            result = first.result(timeout=2)
        self.assertEqual(2, len(calls))
        self.assertEqual(2, len(set(calls)))
        self.assertEqual(
            {"agent-research", "agent-implementation"},
            set(result["delegated"]) | set(second_result["delegated"]),
        )


if __name__ == "__main__":
    unittest.main()
