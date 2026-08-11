from __future__ import annotations

import json
import importlib.util
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tests.support import REPO


CONFIG = REPO / "config" / "supervisor"
DAY_MS = 1_800_000_000_000
RATCHET_SPEC = importlib.util.spec_from_file_location(
    "sessionkit_supervisor_ratchet_under_test",
    REPO / "lib" / "sessionkit_supervisor" / "ratchet.py",
)
assert RATCHET_SPEC is not None and RATCHET_SPEC.loader is not None
ratchet_module = importlib.util.module_from_spec(RATCHET_SPEC)
RATCHET_SPEC.loader.exec_module(ratchet_module)
PROMOTION_STREAK = ratchet_module.PROMOTION_STREAK
Ratchet = ratchet_module.Ratchet
RatchetError = ratchet_module.RatchetError


def _concurrent_authorize(state: str, start: object, results: object) -> None:
    start.wait()
    decision = Ratchet(
        Path(state), config_dir=CONFIG, clock=lambda: DAY_MS / 1000
    ).authorize("status_compilation", now_ms=DAY_MS)
    results.put(decision)


class RatchetPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ratchet-")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state"
        self.ratchet = Ratchet(
            self.state,
            config_dir=CONFIG,
            clock=lambda: DAY_MS / 1000,
        )

    def test_ensure_copies_private_valid_seeds(self) -> None:
        self.ratchet.ensure()
        root = self.state / "supervisor"
        self.assertEqual(0o700, stat.S_IMODE(root.stat().st_mode))
        for name in ("lanes.json", "budgets.json", "ledger.jsonl"):
            self.assertEqual(0o600, stat.S_IMODE((root / name).stat().st_mode))
        self.assertEqual(2, self.ratchet.lanes()["single_target_nudge"]["lane"])
        self.assertEqual(40, self.ratchet.budgets()["per_day_autonomous_turns"])

    def test_twenty_clean_lane2_acts_are_eligible_but_never_auto_promote(self) -> None:
        result = {}
        for number in range(PROMOTION_STREAK):
            result = self.ratchet.record(
                category="single_target_nudge",
                action="request current status",
                target="claude:00000000-0000-4000-8000-000000000001",
                outcome="ok",
                brief_shown=False,
                ts=DAY_MS + number,
            )
        self.assertTrue(result["eligible"])
        self.assertEqual(PROMOTION_STREAK, result["streak"])
        self.assertEqual(2, result["lane"])
        self.assertEqual(
            2, self.ratchet.lanes()["single_target_nudge"]["lane"]
        )

        with self.assertRaisesRegex(RatchetError, "explicit confirmation"):
            self.ratchet.promote(
                "single_target_nudge", operator_confirmed=False, ts=DAY_MS + 100
            )
        promoted = self.ratchet.promote(
            "single_target_nudge", operator_confirmed=True, ts=DAY_MS + 101
        )
        self.assertEqual(
            {"lane": 1, "streak": 0, "promoted_at": DAY_MS + 101}, promoted
        )

    def test_unknown_breaks_streak_and_bad_demotes_immediately(self) -> None:
        self.ratchet.record(
            category="factual_agent_reply",
            action="answer",
            target="claude:00000000-0000-4000-8000-000000000001",
            outcome="ok",
            brief_shown=False,
            ts=DAY_MS,
        )
        unknown = self.ratchet.record(
            category="factual_agent_reply",
            action="answer",
            target="claude:00000000-0000-4000-8000-000000000001",
            outcome="unknown",
            brief_shown=False,
            ts=DAY_MS + 1,
        )
        self.assertEqual(0, unknown["streak"])
        bad = self.ratchet.record(
            category="factual_agent_reply",
            action="answer",
            target="claude:00000000-0000-4000-8000-000000000001",
            outcome="bad",
            brief_shown=True,
            ts=DAY_MS + 2,
        )
        self.assertTrue(bad["demoted"])
        self.assertEqual(3, bad["lane"])
        self.assertEqual(0, bad["streak"])
        with self.assertRaisesRegex(RatchetError, "Lane 3"):
            self.ratchet.record(
                category="factual_agent_reply",
                action="retry",
                target="worker",
                outcome="ok",
                brief_shown=False,
                ts=DAY_MS + 3,
            )

    def test_ledger_has_the_frozen_shape_and_is_append_only(self) -> None:
        expected = {
            "ts": DAY_MS,
            "lane": 1,
            "operator_confirmed": False,
            "category": "status_compilation",
            "action": "compile queue",
            "target": "fleet",
            "outcome": "ok",
            "brief_shown": True,
        }
        result = self.ratchet.record(
            category="status_compilation",
            action="compile queue",
            target="fleet",
            outcome="ok",
            brief_shown=True,
            ts=DAY_MS,
        )
        self.assertEqual(expected, result["entry"])
        ledger = self.state / "supervisor" / "ledger.jsonl"
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual([expected], rows)
        self.assertEqual(0o600, stat.S_IMODE(ledger.stat().st_mode))

    def test_lane3_and_daily_budgets_return_skip_not_abort(self) -> None:
        lane3 = self.ratchet.authorize("fleet_broadcast", now_ms=DAY_MS)
        self.assertFalse(lane3["allowed"])
        self.assertTrue(lane3["skip_not_abort"])
        self.assertIn("lane3_requires_operator", lane3["reasons"])

        for number in range(40):
            self.ratchet.record(
                category="status_compilation",
                action="compile",
                target="fleet",
                outcome="ok",
                brief_shown=True,
                ts=DAY_MS + number,
            )
        blocked = self.ratchet.authorize("status_compilation", now_ms=DAY_MS + 50)
        self.assertFalse(blocked["allowed"])
        self.assertEqual(
            ["per_day_autonomous_turns", "per_day_usd_est"],
            blocked["reasons"],
        )
        self.assertTrue(blocked["budget"]["usd_est_inferred"])

    def test_projected_usd_can_stop_before_the_turn_ceiling(self) -> None:
        decision = self.ratchet.authorize(
            "single_target_nudge",
            now_ms=DAY_MS,
            usd_est_today=4.9,
            next_usd_est=0.11,
        )
        self.assertFalse(decision["allowed"])
        self.assertEqual(["per_day_usd_est"], decision["reasons"])

    def test_budget_status_rotates_at_bound_with_private_single_backup(self) -> None:
        self.ratchet.ensure()
        ledger = self.state / "supervisor" / "ledger.jsonl"
        old_row = (json.dumps({
            "ts": DAY_MS - 86_400_000,
            "lane": 1,
            "category": "status_compilation",
            "action": "old",
            "target": "fleet",
            "outcome": "ok",
            "brief_shown": True,
        }, separators=(",", ":")) + "\n").encode()
        ledger.write_bytes(old_row * (ratchet_module.MAX_FILE_BYTES // len(old_row) + 1))
        ledger.chmod(0o600)

        decision = self.ratchet.authorize("status_compilation", now_ms=DAY_MS)
        status = decision["budget"]

        rotated = ledger.with_name("ledger.jsonl.1")
        self.assertTrue(rotated.is_file())
        self.assertTrue(ledger.is_file())
        self.assertEqual(0o600, stat.S_IMODE(rotated.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(ledger.stat().st_mode))
        self.assertEqual([rotated], list(ledger.parent.glob("ledger.jsonl.*")))
        self.assertEqual([], status["warnings"])

    def test_second_rotation_warns_that_history_was_discarded(self) -> None:
        self.ratchet.ensure()
        ledger = self.state / "supervisor" / "ledger.jsonl"
        rotated = ledger.with_name("ledger.jsonl.1")
        old_row = (json.dumps({
            "ts": DAY_MS - 86_400_000,
            "lane": 1,
            "category": "status_compilation",
            "action": "old",
            "target": "fleet",
            "outcome": "ok",
            "brief_shown": True,
        }, separators=(",", ":")) + "\n").encode()
        overfull = old_row * (ratchet_module.MAX_FILE_BYTES // len(old_row) + 1)
        ledger.write_bytes(overfull)
        ledger.chmod(0o600)
        first = self.ratchet.authorize("status_compilation", now_ms=DAY_MS)
        self.assertEqual([], first["budget"]["warnings"])

        ledger.write_bytes(overfull)
        ledger.chmod(0o600)
        second = self.ratchet.authorize("status_compilation", now_ms=DAY_MS)
        self.assertTrue(rotated.is_file())
        self.assertIn(
            "1 earlier supervisor ledger rotation discarded by the"
            " single-rotation bound",
            second["budget"]["warnings"],
        )

    def test_dead_process_claims_are_dropped_on_read(self) -> None:
        import subprocess

        self.ratchet.ensure()
        child = subprocess.Popen(["true"])
        child.wait()
        dead_pid = child.pid
        claims = self.state / "supervisor" / "budget-claims.json"
        day = ratchet_module._utc_day(DAY_MS)
        claims.write_text(
            json.dumps(
                [
                    {"day_utc": day, "pid": dead_pid, "category": "silence_chase",
                     "usd_est": 0.125},
                    {"day_utc": day, "pid": os.getpid(), "category": "silence_chase",
                     "usd_est": 0.125},
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        claims.chmod(0o600)
        status = self.ratchet.budget_status(now_ms=DAY_MS)
        self.assertEqual(1, status["autonomous_turns"])
        remaining = json.loads(claims.read_text(encoding="utf-8"))
        self.assertEqual([os.getpid()], [claim["pid"] for claim in remaining])

    def test_torn_trailing_ledger_line_is_counted_and_skipped(self) -> None:
        self.ratchet.record(
            category="status_compilation",
            action="compile",
            target="fleet",
            outcome="ok",
            brief_shown=True,
            ts=DAY_MS,
        )
        ledger = self.state / "supervisor" / "ledger.jsonl"
        with ledger.open("ab") as handle:
            handle.write(b'{"ts":')
        status = self.ratchet.budget_status(now_ms=DAY_MS)
        self.assertEqual(1, status["autonomous_turns"])
        self.assertEqual(
            ["skipped 1 malformed supervisor ledger line"], status["warnings"]
        )

    def test_demotion_is_audited_before_lane_state_write(self) -> None:
        self.ratchet.ensure()
        real_atomic_write = ratchet_module._atomic_private_json

        def crash_on_lanes(path: Path, value: object) -> None:
            if path == self.ratchet.lanes_path:
                raise RuntimeError("injected crash")
            real_atomic_write(path, value)

        with mock.patch.object(
            ratchet_module, "_atomic_private_json", side_effect=crash_on_lanes
        ):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.ratchet.record(
                    category="factual_agent_reply",
                    action="answer",
                    target="worker",
                    outcome="bad",
                    brief_shown=True,
                    ts=DAY_MS,
                )
        rows = [
            json.loads(line)
            for line in (self.state / "supervisor" / "ledger.jsonl").read_text().splitlines()
        ]
        self.assertEqual("bad", rows[-1]["outcome"])
        self.assertEqual(2, self.ratchet.lanes()["factual_agent_reply"]["lane"])

    def test_two_processes_cannot_both_pass_the_fortieth_turn(self) -> None:
        for number in range(39):
            self.ratchet.record(
                category="status_compilation",
                action="compile",
                target="fleet",
                outcome="ok",
                brief_shown=True,
                ts=DAY_MS + number,
            )
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_concurrent_authorize,
                args=(str(self.state), start, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        start.set()
        decisions = [results.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(0, process.exitcode)
        self.assertEqual([False, True], sorted(row["allowed"] for row in decisions))
        blocked = next(row for row in decisions if not row["allowed"])
        self.assertIn("per_day_autonomous_turns", blocked["reasons"])

    def test_budget_status_names_the_utc_day(self) -> None:
        status = self.ratchet.budget_status(now_ms=DAY_MS)
        self.assertEqual(ratchet_module._utc_day(DAY_MS), status["day_utc"])

    def test_confirmed_act_without_a_scope_is_refused_before_the_ledger(
        self,
    ) -> None:
        """An operator-confirmed act carries a scope or it is not recorded.

        `record` writes `authority_scope` into the ledger row roughly sixty
        lines after the branch that proves the scope exists, so the two can
        drift apart. Every value the caller may legally pass — including the
        ``None`` default the MCP tool schema allows — has to end in a
        ``RatchetError`` here, never an ``AttributeError`` from an absent
        scope reaching the row builder, and never a half-written ledger.
        """
        self.ratchet.ensure()
        ledger = self.state / "supervisor" / "ledger.jsonl"
        exact = "a" * 64
        for offset, scope in enumerate((None, "", "   ")):
            with self.subTest(scope=scope):
                with self.assertRaisesRegex(RatchetError, "bounded authority scope"):
                    self.ratchet.record(
                        category="single_target_nudge",
                        action="request current status",
                        target="claude:00000000-0000-4000-8000-000000000001",
                        outcome="ok",
                        brief_shown=True,
                        ts=DAY_MS + offset,
                        operator_confirmed=True,
                        authority_event_id=exact,
                        authority_request_id=exact,
                        authority_scope=scope,
                    )
                self.assertEqual("", ledger.read_text())
        recorded = self.ratchet.record(
            category="single_target_nudge",
            action="request current status",
            target="claude:00000000-0000-4000-8000-000000000001",
            outcome="ok",
            brief_shown=True,
            ts=DAY_MS + 9,
            operator_confirmed=True,
            authority_event_id=exact,
            authority_request_id=exact,
            authority_scope="  ask for the current status  ",
        )
        self.assertEqual(
            "ask for the current status", recorded["entry"]["authority_scope"]
        )
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual(
            ["ask for the current status"], [row["authority_scope"] for row in rows]
        )

    def test_insecure_runtime_state_is_refused(self) -> None:
        self.ratchet.ensure()
        lanes = self.state / "supervisor" / "lanes.json"
        os.chmod(lanes, 0o644)
        with self.assertRaisesRegex(RatchetError, "mode-0600"):
            self.ratchet.lanes()


if __name__ == "__main__":
    unittest.main()
