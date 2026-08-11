"""The duty a worker is launched with, and what it says for itself afterwards.

A launched session is not a worker. Until the duty text has actually landed in
it, it is a process holding a branch name and waiting for something nobody
sent — which is exactly what the delegation path used to do: it launched, it
proved the model, and it never delivered the work. These tests hold that line
from both ends. The duty reaches the exact proven identity or the worker is
reported undelivered, and what became of the duty comes back as a file the
worker wrote, so a worker that finished and went quiet is visible in the
record rather than assumed to be fine.

Every test runs against a disposable state directory with the courier injected,
so nothing here reaches a session, a socket, or a real home.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor import intake  # noqa: E402
from sessionkit_supervisor.source_authority import capture_hook_event  # noqa: E402


CORE = REPO / "lib" / "session_inventory.py"
SOURCE_UUID = "019fdf1e-8b4c-7573-a089-be495bfece6a"
SUPERVISOR_UUID = "dcbdf940-4eda-4967-8e41-23a5760c32b5"
CLAUDE_WORKER_UUID = "00000000-0000-4000-8000-000000000001"
CODEX_WORKER_UUID = "00000000-0000-4000-8000-000000000002"
IMPLEMENTATION = "agent-implementation"
RESEARCH = "agent-research"


class Clock:
    """A clock that only ever moves, so every stamp is orderable."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        self.value += 1.0
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Courier:
    """The messaging core, recorded rather than run."""

    def __init__(self, *, status: str = "delivered-woke") -> None:
        self.status = status
        self.sends: list[dict[str, str]] = []

    def __call__(self, *, thread_key: str, text: str, key: str) -> dict:
        self.sends.append({"thread_key": thread_key, "text": text, "key": key})
        return {
            "msg_id": "cc33dd44",
            "targets": [
                {"thread_key": thread_key, "status": self.status, "detail": "test"}
            ],
        }


class DutyCase(unittest.TestCase):
    """One automatic intake, reviewed, with a two-worker plan carrying duties."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".intake-duty-", dir=REPO)
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir(mode=0o700)
        self.transcripts = self.state / "test-transcripts"
        self.transcripts.mkdir(mode=0o700)
        self.spool = intake.Spool(self.state)
        self.clock = Clock()
        self.courier = Courier()
        self.prompt = "audit the delegation path and implement the approved fix"
        self.event = self.capture("turn-1", self.prompt)
        outcome = intake.produce(
            self.spool,
            thread_key=f"codex:{SOURCE_UUID}",
            prompt=self.prompt,
            turn_id="turn-1",
            source_event_id=self.event["event_id"],
            source_digest=self.event["prompt_sha256"],
            clock=self.clock,
        )
        self.msg_id = outcome["entry"]["msg_id"]
        self.name_supervisor()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self, turn: str, prompt: str) -> dict:
        return capture_hook_event(
            {
                "provider": "codex",
                "session_id": SOURCE_UUID,
                "turn_id": turn,
                "prompt": prompt,
                "transcript_path": os.fspath(self.transcripts / f"{turn}.jsonl"),
            },
            state_dir=self.state,
        )

    def name_supervisor(self) -> None:
        root = self.state / "supervisor"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / "identity"
        path.write_text(f"claude:{SUPERVISOR_UUID}\n", encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def plan() -> list[dict]:
        return [
            {
                "branch": IMPLEMENTATION,
                "idempotency_key": "worker:implementation:1",
                "workstream": "implementation and tests",
                "scope": "implement the bounded change and run verification",
                "provider": "claude",
                "requested_model": "claude-opus-test",
                "expertise": "implementation",
                "rationale": "implementation expertise for the code itself",
                "task_text": (
                    "Deliver the commissioning path.\n\n"
                    "1. Send the duty at launch.\n"
                    "2. Record what became of the send."
                ),
                "acceptance_criteria": "the full suite passes on the branch",
                "deliverable": "commits on the branch plus the test output",
            },
            {
                "branch": RESEARCH,
                "idempotency_key": "worker:research:1",
                "workstream": "source and risk analysis",
                "scope": "independently audit requirements and failure modes",
                "provider": "codex",
                "requested_model": "gpt-codex-test",
                "expertise": "security",
                "rationale": "a separate model family for independent review",
                "task_text": "Audit the delegation path for silent failures.",
                "acceptance_criteria": "every declared risk has a named mitigation",
                "deliverable": "a written risk analysis on the branch",
            },
        ]

    def preflight(self, plan: list[dict] | None = None, **overrides: object) -> dict:
        fields: dict = {
            "analysis": "read the exact source event and inspected current state",
            "scope": "the current source event and its complete requirements",
            "required_expertise": "implementation plus independent security review",
            "required_expertise_tags": ("implementation", "security"),
            "worker_plan": plan if plan is not None else self.plan(),
            "risks": "a launched worker that is never told what it is for",
            "tests": "delivery, failure, report, retry, and silence detection",
        }
        fields.update(overrides)
        return intake.preflight(
            self.spool,
            msg_id=self.msg_id,
            source_event_id=self.event["event_id"],
            state_dir=self.state,
            clock=self.clock,
            **fields,
        )

    @staticmethod
    def launch(_assignment: dict) -> None:
        return None

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

    def delegate(self, *, deliver: object | None = None, branches=None) -> dict:
        """Delegate with the recorded courier; `deliver=False` means none at all."""
        courier = self.courier if deliver is None else deliver
        return intake.delegate(
            self.spool,
            msg_id=self.msg_id,
            branches=tuple(branches or (row["branch"] for row in self.plan())),
            launcher=self.launch,
            reconciler=self.inventory_proof,
            deliver=None if courier is False else courier,
            state_dir=self.state,
            clock=self.clock,
        )

    def worker(self, branch: str = IMPLEMENTATION) -> dict:
        entry = self.spool.read_entry(self.msg_id)
        assert entry is not None
        return next(row for row in entry["workers"] if row["branch"] == branch)

    def commission(self) -> dict:
        self.preflight()
        return self.delegate()


class DutyDeliveryTests(DutyCase):
    def test_the_duty_reaches_the_proven_worker_identity_at_launch(self) -> None:
        result = self.commission()
        self.assertEqual([], result["undelivered"])
        self.assertEqual(2, len(self.courier.sends))
        sent = next(
            row
            for row in self.courier.sends
            if row["thread_key"] == f"claude:{CLAUDE_WORKER_UUID}"
        )
        # What the worker is for, what finishing means, what it hands back, and
        # how it reports — the four things a launched session used to be sent
        # none of.
        self.assertIn("Deliver the commissioning path.", sent["text"])
        self.assertIn("1. Send the duty at launch.", sent["text"])
        self.assertIn("the full suite passes on the branch", sent["text"])
        self.assertIn("commits on the branch plus the test output", sent["text"])
        self.assertIn(
            f"sp msg intake report --msg-id {self.msg_id} --branch {IMPLEMENTATION}",
            sent["text"],
        )
        # The worker is handed its own key, so its report is attributable.
        self.assertIn(f"--reporter claude:{CLAUDE_WORKER_UUID}", sent["text"])
        worker = self.worker()
        self.assertEqual("commissioned", worker["launch_state"])
        self.assertEqual("assigned", worker["duty_state"])
        self.assertEqual(1, worker["duty_attempt"])
        delivery = worker["duty_delivery"]
        self.assertEqual("delivered", delivery["state"])
        self.assertEqual(
            intake.duty_key(self.msg_id, IMPLEMENTATION, 1), delivery["relay_key"]
        )
        self.assertEqual("cc33dd44", delivery["relay_msg_id"])
        self.assertTrue(delivery["delivered_unix_ms"])

    def test_the_line_breaks_a_duty_was_written_with_survive_the_trip(self) -> None:
        self.commission()
        self.assertIn(
            "1. Send the duty at launch.\n2. Record what became of the send.",
            self.worker()["task_text"],
        )

    def test_a_duty_that_did_not_land_leaves_the_worker_verified_and_named(
        self,
    ) -> None:
        unreachable = Courier(status="unreachable")
        self.preflight()
        result = self.delegate(deliver=unreachable)
        self.assertEqual([IMPLEMENTATION, RESEARCH], sorted(result["undelivered"]))
        worker = self.worker()
        self.assertEqual("verified", worker["launch_state"])
        self.assertEqual("failed", worker["duty_delivery"]["state"])
        self.assertIsNone(worker["duty_delivery"]["delivered_unix_ms"])
        self.assertEqual("unreachable", worker["duty_delivery"]["relay_status"])

    def test_an_undelivered_duty_makes_the_delegate_verb_exit_nonzero(self) -> None:
        unreachable = Courier(status="unreachable")
        self.preflight()
        code, payload = intake.run(
            "delegate",
            spool=self.spool,
            deliver=unreachable,
            reply=lambda **_fields: {},
            msg_id=self.msg_id,
            branches=(IMPLEMENTATION, RESEARCH),
            launcher=self.launch,
            reconciler=self.inventory_proof,
            state_dir=self.state,
            clock=self.clock,
        )
        self.assertEqual(1, code)
        self.assertEqual(2, len(payload["undelivered"]))

    def test_a_duty_is_redelivered_under_one_key_until_it_lands(self) -> None:
        unreachable = Courier(status="unreachable")
        self.preflight()
        self.delegate(deliver=unreachable)
        first_key = unreachable.sends[0]["key"]
        second = self.delegate()
        # The second call launched nothing; it only finished the delivery the
        # first call could not, under the key that first attempt used.
        self.assertEqual([], second["delegated"])
        self.assertEqual([], second["undelivered"])
        self.assertEqual(first_key, self.courier.sends[0]["key"])
        self.assertEqual("commissioned", self.worker()["launch_state"])
        third = self.delegate()
        self.assertEqual([], third["undelivered"])
        self.assertEqual(2, len(self.courier.sends))

    def test_a_duty_too_long_to_travel_whole_is_refused_while_it_is_a_plan(
        self,
    ) -> None:
        """The messaging core truncates; a half-delivered brief never launches."""
        plan = [dict(row) for row in self.plan()]
        plan[0]["task_text"] = "x" * intake.MAX_TASK_TEXT
        plan[0]["acceptance_criteria"] = "y" * intake.MAX_ACCEPTANCE
        plan[0]["deliverable"] = "z" * intake.MAX_DELIVERABLE
        with self.assertRaisesRegex(intake.IntakeError, "longer than one message"):
            self.preflight(plan=plan)

    def test_a_worker_with_a_duty_and_no_courier_is_refused(self) -> None:
        self.preflight()
        with self.assertRaisesRegex(intake.IntakeError, "no courier"):
            self.delegate(deliver=False)

    def test_a_plan_written_before_duties_existed_still_launches(self) -> None:
        plan = [
            {
                key: value
                for key, value in row.items()
                if key not in ("task_text", "acceptance_criteria", "deliverable")
            }
            for row in self.plan()
        ]
        # A stored preflight from an older kit is read, not rewritten: the old
        # entry keeps working and only new plans are held to the duty contract.
        entry = self.spool.read_entry(self.msg_id)
        assert entry is not None
        self.preflight()
        legacy = dict(entry["preflights"][0] if entry["preflights"] else {})
        stored = self.spool.read_entry(self.msg_id)
        assert stored is not None
        legacy = dict(stored["preflights"][-1])
        legacy["worker_plan"] = plan
        stored["preflights"][-1] = legacy
        self.spool.write_entry(stored)
        result = self.delegate(deliver=False)
        self.assertEqual([], result["undelivered"])
        self.assertEqual("verified", self.worker()["launch_state"])
        self.assertEqual("", self.worker()["task_text"])


class DutyReportTests(DutyCase):
    def test_a_report_lands_on_disk_and_in_the_entry(self) -> None:
        self.commission()
        result = intake.report(
            self.spool,
            msg_id=self.msg_id,
            branch=IMPLEMENTATION,
            state="completed",
            summary="suite green, change committed",
            reporter_identity=f"claude:{CLAUDE_WORKER_UUID}",
            clock=self.clock,
        )
        self.assertTrue(result["reported"])
        path = self.spool.receipt_path(self.msg_id, IMPLEMENTATION, 1)
        self.assertTrue(path.is_file())
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        receipt = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("completed", receipt["state"])
        self.assertEqual(IMPLEMENTATION, receipt["branch"])
        self.assertEqual(f"claude:{CLAUDE_WORKER_UUID}", receipt["reporter_identity"])
        # The launch this duty was sent to, so a run receipt for the same
        # attempt lines up without reading the intake entry.
        self.assertEqual("worker:implementation:1", receipt["launch_key"])
        worker = self.worker()
        self.assertEqual("completed", worker["duty_state"])
        self.assertEqual(1, len(worker["duty_reports"]))
        self.assertEqual(path.name, worker["duty_reports"][0]["receipt"])
        self.assertEqual("commissioned", worker["launch_state"])

    def test_a_worker_waiting_on_its_next_launch_has_nothing_to_report(self) -> None:
        self.commission()
        intake.report(
            self.spool,
            msg_id=self.msg_id,
            branch=IMPLEMENTATION,
            state="failed",
            summary="blocked",
            clock=self.clock,
        )
        intake.retry(
            self.spool,
            msg_id=self.msg_id,
            branch=IMPLEMENTATION,
            idempotency_key="worker:implementation:2",
            clock=self.clock,
        )
        with self.assertRaisesRegex(intake.IntakeError, "not been launched"):
            intake.report(
                self.spool,
                msg_id=self.msg_id,
                branch=IMPLEMENTATION,
                state="completed",
                summary="nothing happened",
                clock=self.clock,
            )

    def test_only_the_states_a_worker_can_report_are_accepted(self) -> None:
        self.commission()
        for state in ("abandoned", "assigned", "done"):
            with self.subTest(state=state), self.assertRaises(intake.IntakeError):
                intake.report(
                    self.spool,
                    msg_id=self.msg_id,
                    branch=IMPLEMENTATION,
                    state=state,
                    summary="not a worker's word to use",
                    clock=self.clock,
                )

    def test_a_worker_row_recorded_before_duties_is_refused_not_lost(self) -> None:
        """A duty state the validator would drop on write is refused up front."""
        self.commission()
        entry = self.spool.read_entry(self.msg_id)
        assert entry is not None
        entry["workers"] = [
            {"branch": IMPLEMENTATION, "recorded_unix_ms": intake.now_unix_ms(self.clock)}
        ]
        self.spool.write_entry(entry)
        for verb, fields in (
            (intake.report, {"state": "completed", "summary": "done"}),
            (intake.reset, {}),
            (intake.cancel, {"summary": "no longer needed"}),
            (intake.retry, {"idempotency_key": "worker:implementation:2"}),
        ):
            with self.subTest(verb=verb.__name__), self.assertRaisesRegex(
                intake.IntakeError, "before duties existed"
            ):
                verb(
                    self.spool,
                    msg_id=self.msg_id,
                    branch=IMPLEMENTATION,
                    clock=self.clock,
                    **fields,
                )

    def test_an_unknown_branch_is_refused_rather_than_recorded(self) -> None:
        self.commission()
        with self.assertRaisesRegex(intake.IntakeError, "no worker on branch"):
            intake.report(
                self.spool,
                msg_id=self.msg_id,
                branch="agent-nobody",
                state="completed",
                summary="a branch this intake never planned",
                clock=self.clock,
            )


class DutyVisibilityTests(DutyCase):
    def report(self, state: str = "completed", branch: str = IMPLEMENTATION) -> dict:
        return intake.report(
            self.spool,
            msg_id=self.msg_id,
            branch=branch,
            state=state,
            summary=f"{branch} says {state}",
            clock=self.clock,
        )

    def test_a_commissioned_worker_that_says_nothing_is_flagged_silent(self) -> None:
        self.commission()
        early = intake.duties(self.spool, msg_id=self.msg_id, clock=self.clock)
        self.assertEqual([], early["silent"])
        self.assertEqual(2, early["count"])
        self.clock.advance(intake.SILENT_DUTY_MS / 1000 + 60)
        quiet = intake.duties(self.spool, msg_id=self.msg_id, clock=self.clock)
        self.assertEqual([IMPLEMENTATION, RESEARCH], sorted(quiet["silent"]))
        self.report()
        after = intake.duties(self.spool, msg_id=self.msg_id, clock=self.clock)
        self.assertEqual([RESEARCH], after["silent"])
        row = next(
            item for item in after["duties"] if item["branch"] == IMPLEMENTATION
        )
        self.assertEqual("completed", row["duty_state"])
        self.assertEqual(1, row["receipts_on_disk"])
        self.assertEqual([], row["unfolded_receipts"])

    def test_a_receipt_the_entry_never_recorded_is_flagged(self) -> None:
        self.commission()
        # The worker wrote its report and the entry write never happened: the
        # receipt is on disk with nothing in the record pointing at it.
        self.spool.write_receipt(
            {
                "msg_id": self.msg_id,
                "branch": IMPLEMENTATION,
                "seq": 1,
                "state": "completed",
                "summary": "finished, and the entry never learned",
                "reporter_identity": f"claude:{CLAUDE_WORKER_UUID}",
                "duty_attempt": 1,
                "requirements_digest": "",
                "recorded_unix_ms": intake.now_unix_ms(self.clock),
            }
        )
        view = intake.duties(self.spool, msg_id=self.msg_id, clock=self.clock)
        self.assertEqual([IMPLEMENTATION], view["unfolded_receipts"])
        row = next(
            item for item in view["duties"] if item["branch"] == IMPLEMENTATION
        )
        self.assertEqual(1, row["receipts_on_disk"])
        self.assertEqual(0, row["reports"])

    def test_an_undelivered_duty_is_listed_as_owed(self) -> None:
        self.preflight()
        self.delegate(deliver=Courier(status="unreachable"))
        view = intake.duties(self.spool, clock=self.clock)
        self.assertEqual([IMPLEMENTATION, RESEARCH], sorted(view["owed"]))
        self.assertEqual([], view["silent"])

    def test_the_view_covers_every_open_intake_when_none_is_named(self) -> None:
        self.commission()
        view = intake.duties(self.spool, clock=self.clock)
        self.assertEqual(2, view["count"])
        self.assertEqual(
            {self.msg_id}, {row["msg_id"] for row in view["duties"]}
        )


class DutyVerbTests(DutyCase):
    def fail_duty(self, branch: str = IMPLEMENTATION) -> None:
        intake.report(
            self.spool,
            msg_id=self.msg_id,
            branch=branch,
            state="failed",
            summary="blocked on an unreadable fixture",
            clock=self.clock,
        )

    def test_a_failed_duty_is_retried_under_a_new_launch_key(self) -> None:
        self.commission()
        self.fail_duty()
        result = intake.retry(
            self.spool,
            msg_id=self.msg_id,
            branch=IMPLEMENTATION,
            idempotency_key="worker:implementation:2",
            clock=self.clock,
        )
        worker = result["worker"]
        self.assertEqual("not_started", worker["launch_state"])
        self.assertEqual("assigned", worker["duty_state"])
        self.assertEqual(2, worker["duty_attempt"])
        self.assertEqual("worker:implementation:2", worker["idempotency_key"])
        self.assertIsNone(worker["duty_delivery"])
        self.assertEqual("", worker["worker_identity"])
        self.assertEqual("", worker["verified_actual_model"])
        # The history is kept: the failure and the retry both stay readable.
        self.assertEqual(
            ["failed", "assigned"], [row["state"] for row in worker["duty_reports"]]
        )

    def test_a_retry_that_reuses_the_dead_attempts_key_is_refused(self) -> None:
        self.commission()
        self.fail_duty()
        with self.assertRaisesRegex(intake.IntakeError, "launch key"):
            intake.retry(
                self.spool,
                msg_id=self.msg_id,
                branch=IMPLEMENTATION,
                idempotency_key="worker:implementation:1",
                clock=self.clock,
            )

    def test_a_duty_that_has_not_failed_is_not_retried(self) -> None:
        self.commission()
        with self.assertRaisesRegex(intake.IntakeError, "has not failed"):
            intake.retry(
                self.spool,
                msg_id=self.msg_id,
                branch=IMPLEMENTATION,
                idempotency_key="worker:implementation:2",
                clock=self.clock,
            )

    def test_a_retried_duty_is_relaunched_and_recommissioned(self) -> None:
        self.commission()
        self.fail_duty()
        intake.retry(
            self.spool,
            msg_id=self.msg_id,
            branch=IMPLEMENTATION,
            idempotency_key="worker:implementation:2",
            clock=self.clock,
        )
        self.delegate(branches=(IMPLEMENTATION,))
        worker = self.worker()
        self.assertEqual("commissioned", worker["launch_state"])
        self.assertEqual(
            intake.duty_key(self.msg_id, IMPLEMENTATION, 2),
            worker["duty_delivery"]["relay_key"],
        )
        self.assertEqual(3, len(self.courier.sends))

    def test_reset_returns_a_settled_duty_to_assigned_and_keeps_the_history(
        self,
    ) -> None:
        self.commission()
        intake.report(
            self.spool,
            msg_id=self.msg_id,
            branch=IMPLEMENTATION,
            state="completed",
            summary="reported done too early",
            clock=self.clock,
        )
        result = intake.reset(
            self.spool,
            msg_id=self.msg_id,
            branch=IMPLEMENTATION,
            summary="the deliverable was not on the branch",
            clock=self.clock,
        )
        worker = result["worker"]
        self.assertEqual("assigned", worker["duty_state"])
        self.assertEqual("commissioned", worker["launch_state"])
        self.assertEqual(
            ["completed", "assigned"], [row["state"] for row in worker["duty_reports"]]
        )
        with self.assertRaisesRegex(intake.IntakeError, "nothing to reset"):
            intake.reset(
                self.spool,
                msg_id=self.msg_id,
                branch=IMPLEMENTATION,
                clock=self.clock,
            )

    def test_cancel_abandons_a_duty_and_a_later_report_is_refused(self) -> None:
        self.commission()
        result = intake.cancel(
            self.spool,
            msg_id=self.msg_id,
            branch=RESEARCH,
            summary="the audit was folded into the implementation duty",
            clock=self.clock,
        )
        self.assertEqual("abandoned", result["worker"]["duty_state"])
        with self.assertRaisesRegex(intake.IntakeError, "cancelled"):
            intake.report(
                self.spool,
                msg_id=self.msg_id,
                branch=RESEARCH,
                state="completed",
                summary="finished anyway",
                clock=self.clock,
            )
        with self.assertRaisesRegex(intake.IntakeError, "already abandoned"):
            intake.cancel(
                self.spool,
                msg_id=self.msg_id,
                branch=RESEARCH,
                summary="again",
                clock=self.clock,
            )
        # A cancelled duty is reopened deliberately, never by a worker.
        intake.reset(
            self.spool,
            msg_id=self.msg_id,
            branch=RESEARCH,
            summary="the audit is needed after all",
            clock=self.clock,
        )
        self.assertEqual("assigned", self.worker(RESEARCH)["duty_state"])


class DutyCliTests(unittest.TestCase):
    """The installed verbs, run as the CLI a worker actually types."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".duty-cli-", dir=REPO)
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.shpool_json = self.base / "shpool.json"
        self.shpool_json.write_text(json.dumps({"sessions": []}), encoding="utf-8")
        self.agents_json = self.base / "agents.json"
        self.agents_json.write_text("[]", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def core(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CORE), "msg", "intake", *arguments],
            cwd=REPO,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": str(self.home),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_SHPOOL_JSON_FILE": str(self.shpool_json),
                "SESSION_KIT_CLAUDE_JSON_FILE": str(self.agents_json),
                "SESSION_KIT_CODEX_AUTOTITLE": "0",
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def seed(self) -> str:
        """One recorded intake with one commissioned worker, written directly."""
        spool = intake.Spool(self.state)
        clock = Clock()
        recorded = intake.record(
            spool,
            msg_id="4f3a2b1c",
            source=f"claude:{SUPERVISOR_UUID}",
            summary="prove the installed duty verbs",
            clock=clock,
        )
        entry = recorded["entry"]
        entry["workers"] = [
            {
                "branch": IMPLEMENTATION,
                "idempotency_key": "worker:implementation:1",
                "workstream": "implementation",
                "scope": "prove the installed verbs",
                "provider": "claude",
                "requested_model": "claude-opus-test",
                "verified_actual_model": "claude-opus-test",
                "expertise": "implementation",
                "rationale": "one worker proves the CLI wiring",
                "task_text": "prove the installed duty verbs",
                "acceptance_criteria": "the verbs answer in machine JSON",
                "deliverable": "the recorded report",
                "worker_identity": f"claude:{CLAUDE_WORKER_UUID}",
                "launch_state": "commissioned",
                "duty_state": "assigned",
                "duty_attempt": 1,
                "duty_delivery": {
                    "state": "delivered",
                    "relay_key": intake.duty_key("4f3a2b1c", IMPLEMENTATION, 1),
                    "relay_msg_id": "cc33dd44",
                    "relay_status": "delivered-woke",
                    "relay_detail": "",
                    "delivered_unix_ms": intake.now_unix_ms(clock),
                },
                "duty_reports": [],
                "dispatch_unix_ms": intake.now_unix_ms(clock),
                "reconciled_unix_ms": intake.now_unix_ms(clock),
                "launched_unix_ms": intake.now_unix_ms(clock),
                "verified_unix_ms": intake.now_unix_ms(clock),
                "recorded_unix_ms": intake.now_unix_ms(clock),
                "preflight_revision": 1,
                "intake_generation": 0,
                "requirements_digest": intake.requirements_digest(entry),
                "authority_verifications": [],
            }
        ]
        spool.write_entry(entry)
        return str(entry["msg_id"])

    def test_a_worker_reports_and_the_duties_view_shows_it(self) -> None:
        msg_id = self.seed()
        reported = self.core(
            "report",
            "--msg-id",
            msg_id,
            "--branch",
            IMPLEMENTATION,
            "--state",
            "completed",
            "--summary",
            "installed verbs answered",
            "--reporter",
            f"claude:{CLAUDE_WORKER_UUID}",
        )
        self.assertEqual(0, reported.returncode, reported.stderr)
        payload = json.loads(reported.stdout)
        self.assertTrue(payload["reported"])
        self.assertEqual("completed", payload["worker"]["duty_state"])
        listed = self.core("duties", "--msg-id", msg_id)
        self.assertEqual(0, listed.returncode, listed.stderr)
        view = json.loads(listed.stdout)
        self.assertEqual(1, view["count"])
        self.assertEqual([], view["silent"])
        self.assertEqual("completed", view["duties"][0]["duty_state"])
        self.assertEqual(1, view["duties"][0]["receipts_on_disk"])

    def test_the_supervisor_cancels_and_resets_through_the_installed_verbs(
        self,
    ) -> None:
        msg_id = self.seed()
        cancelled = self.core(
            "cancel",
            "--msg-id",
            msg_id,
            "--branch",
            IMPLEMENTATION,
            "--summary",
            "the work moved to another intake",
        )
        self.assertEqual(0, cancelled.returncode, cancelled.stderr)
        self.assertEqual(
            "abandoned", json.loads(cancelled.stdout)["worker"]["duty_state"]
        )
        reset = self.core("reset", "--msg-id", msg_id, "--branch", IMPLEMENTATION)
        self.assertEqual(0, reset.returncode, reset.stderr)
        self.assertEqual("assigned", json.loads(reset.stdout)["worker"]["duty_state"])


if __name__ == "__main__":
    unittest.main()
