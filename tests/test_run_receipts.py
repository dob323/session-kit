"""Per-plan cost caps and run receipts: what a delegated run cost and why it stopped.

The cases here are the ones a receipt exists for. A cap that is only shown at
approval and never enforced. A run that stops at the cap but leaves no record
saying so. A worker that closes as "completed" with nothing having checked it.
A receipt edited afterwards that still reads as evidence.

Spend is reported, never measured by the kit, so every sample names its source
and the totals are estimates. The assertions below are about arithmetic,
refusals, and what ends up on disk — never about the kit knowing a price.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.support import REPO, run

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor import receipts  # noqa: E402


CORE = REPO / "lib" / "session_inventory.py"
PLAN = "a1b2c3d4"


def load_core(name: str):
    spec = importlib.util.spec_from_file_location(name, CORE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PlanCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="receipt-caps-")
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_cap_needs_at_least_one_limit(self) -> None:
        with self.assertRaises(receipts.ReceiptError):
            receipts.set_cap(state_dir=self.state, msg_id=PLAN)

    def test_a_soft_limit_above_the_hard_cap_is_refused(self) -> None:
        with self.assertRaises(receipts.ReceiptError):
            receipts.set_cap(
                state_dir=self.state, msg_id=PLAN, max_usd_est=1.0, soft_usd_est=2.0
            )

    def test_a_cap_is_private_revisioned_and_readable_back(self) -> None:
        first = receipts.set_cap(
            state_dir=self.state,
            msg_id=PLAN,
            max_usd_est=5.0,
            soft_usd_est=4.0,
            max_tokens=200_000,
            max_iterations=20,
        )
        self.assertEqual(1, first["revision"])
        path = self.state / "receipts" / "caps" / f"{PLAN}.json"
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        second = receipts.set_cap(state_dir=self.state, msg_id=PLAN, max_usd_est=9.0)
        self.assertEqual(2, second["revision"])
        self.assertEqual(9.0, receipts.read_cap(self.state, PLAN)["max_usd_est"])

    def test_the_approval_line_states_every_limit(self) -> None:
        cap = receipts.set_cap(
            state_dir=self.state,
            msg_id=PLAN,
            max_usd_est=5.0,
            soft_usd_est=4.0,
            max_tokens=200_000,
            max_iterations=20,
        )
        line = receipts.format_cap(cap)
        self.assertIn("$5.00", line)
        self.assertIn("warn at $4.00", line)
        self.assertIn("200,000 tokens", line)
        self.assertIn("20 iterations", line)
        self.assertIn("hard stop", line)
        self.assertEqual(
            "Cost cap: none recorded for this plan.", receipts.format_cap(None)
        )


class RunReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="receipt-runs-")
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def open_run(self, **fields) -> dict:
        return receipts.open_run(
            state_dir=self.state,
            msg_id=PLAN,
            branch="worker/one",
            provider="claude",
            model="claude-opus-test",
            **fields,
        )

    def test_an_open_run_snapshots_the_cap_and_is_owner_private(self) -> None:
        receipts.set_cap(state_dir=self.state, msg_id=PLAN, max_usd_est=5.0)
        record = self.open_run(isolation_mode="worktree", isolation_path="/tmp/tree")
        self.assertEqual(5.0, record["cap"]["max_usd_est"])
        self.assertEqual("running", record["stop_reason"])
        self.assertEqual("worktree", record["isolation"]["mode"])
        path = self.state / "receipts" / "runs" / f"{record['receipt_id']}.json"
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual("verified", receipts.integrity_of(record))

    def test_an_edited_receipt_reads_as_tampered_and_is_never_built_on(self) -> None:
        record = self.open_run()
        path = self.state / "receipts" / "runs" / f"{record['receipt_id']}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["spend"]["usd_est"] = 0.0
        document["stop_reason"] = "completed"
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(
            "tamper_detected", receipts.integrity_of(receipts.read_run(self.state, record["receipt_id"]))
        )
        with self.assertRaises(receipts.ReceiptError):
            receipts.record_spend(
                state_dir=self.state,
                receipt_id=record["receipt_id"],
                usd_est=0.1,
                source="test",
            )

    def test_spend_accumulates_with_the_source_that_reported_it(self) -> None:
        record = self.open_run()
        receipts.record_spend(
            state_dir=self.state,
            receipt_id=record["receipt_id"],
            usd_est=0.25,
            tokens=1000,
            iterations=1,
            source="supervisor lane",
        )
        updated = receipts.record_spend(
            state_dir=self.state,
            receipt_id=record["receipt_id"],
            usd_est=0.25,
            tokens=1500,
            iterations=1,
            source="supervisor lane",
        )
        self.assertEqual(0.5, updated["spend"]["usd_est"])
        self.assertEqual(2500, updated["spend"]["tokens"])
        self.assertEqual(2, updated["spend"]["iterations"])
        self.assertEqual(
            ["supervisor lane", "supervisor lane"],
            [sample["source"] for sample in updated["spend"]["samples"]],
        )

    def test_a_sample_without_a_source_or_an_amount_is_refused(self) -> None:
        record = self.open_run()
        with self.assertRaises(receipts.ReceiptError):
            receipts.record_spend(
                state_dir=self.state,
                receipt_id=record["receipt_id"],
                usd_est=1.0,
                source="   ",
            )
        with self.assertRaises(receipts.ReceiptError):
            receipts.record_spend(
                state_dir=self.state,
                receipt_id=record["receipt_id"],
                source="supervisor lane",
            )

    def test_the_soft_limit_warns_and_the_hard_cap_stops_the_run(self) -> None:
        receipts.set_cap(
            state_dir=self.state, msg_id=PLAN, max_usd_est=1.0, soft_usd_est=0.5
        )
        record = self.open_run()
        warned = receipts.record_spend(
            state_dir=self.state,
            receipt_id=record["receipt_id"],
            usd_est=0.6,
            source="supervisor lane",
        )
        self.assertEqual("running", warned["stop_reason"])
        self.assertEqual(1, len(warned["cap_state"]["warnings"]))
        stopped = receipts.record_spend(
            state_dir=self.state,
            receipt_id=record["receipt_id"],
            usd_est=0.5,
            source="supervisor lane",
        )
        self.assertEqual("cap_breached", stopped["stop_reason"])
        self.assertIn("reached the $1.00 cap", stopped["stop_detail"])
        self.assertIsNotNone(stopped["closed_unix_ms"])
        # The stop is durable, and the plan refuses to start anything else.
        stored = receipts.read_run(self.state, record["receipt_id"])
        self.assertEqual("cap_breached", stored["stop_reason"])
        gate = receipts.gate(self.state, PLAN)
        self.assertFalse(gate["allowed"])
        self.assertIn("reached the $1.00 cap", gate["reason"])
        with self.assertRaises(receipts.ReceiptError):
            receipts.record_spend(
                state_dir=self.state,
                receipt_id=record["receipt_id"],
                usd_est=0.1,
                source="supervisor lane",
            )

    def test_the_cap_counts_every_run_in_the_plan(self) -> None:
        receipts.set_cap(state_dir=self.state, msg_id=PLAN, max_tokens=1000)
        first = self.open_run()
        second = receipts.open_run(
            state_dir=self.state, msg_id=PLAN, branch="worker/two", provider="codex"
        )
        receipts.record_spend(
            state_dir=self.state,
            receipt_id=first["receipt_id"],
            tokens=600,
            source="transcript reader",
        )
        stopped = receipts.record_spend(
            state_dir=self.state,
            receipt_id=second["receipt_id"],
            tokens=500,
            source="transcript reader",
        )
        self.assertEqual("cap_breached", stopped["stop_reason"])
        self.assertEqual(1100, receipts.plan_spend(self.state, PLAN)["tokens"])
        # A plan with no cap never stops on spend.
        other = receipts.open_run(state_dir=self.state, msg_id="b2c3d4e5")
        keeps_going = receipts.record_spend(
            state_dir=self.state,
            receipt_id=other["receipt_id"],
            usd_est=99.0,
            source="operator",
        )
        self.assertEqual("running", keeps_going["stop_reason"])

    def test_completion_needs_a_verifier_or_an_explicit_admission(self) -> None:
        record = self.open_run()
        with self.assertRaises(receipts.ReceiptError) as refusal:
            receipts.close_run(
                state_dir=self.state,
                receipt_id=record["receipt_id"],
                stop_reason="completed",
            )
        self.assertIn("passing verifier result", str(refusal.exception))
        admitted = receipts.close_run(
            state_dir=self.state,
            receipt_id=record["receipt_id"],
            stop_reason="completed",
            allow_unverified=True,
        )
        self.assertEqual("unverified", admitted["verifier"]["result"])

        checked = self.open_run()
        receipts.record_verifier(
            state_dir=self.state,
            receipt_id=checked["receipt_id"],
            result="passed",
            command="python3 -m unittest discover",
            exit_code=0,
            evidence="Ran 18 tests OK",
        )
        closed = receipts.close_run(
            state_dir=self.state,
            receipt_id=checked["receipt_id"],
            stop_reason="completed",
            worker_identity="claude:00000000-0000-4000-8000-000000000001",
        )
        self.assertEqual("passed", closed["verifier"]["result"])
        self.assertEqual("completed", closed["stop_reason"])
        self.assertEqual("verified", receipts.integrity_of(closed))

    def test_changed_files_come_from_the_worktree_git_reports(self) -> None:
        repo = self.base / "project"
        repo.mkdir()
        for arguments in (
            ("init", "--quiet", "--initial-branch=main"),
            ("config", "user.email", "tester@invalid.example"),
            ("config", "user.name", "Session Kit Tests"),
            ("config", "commit.gpgsign", "false"),
        ):
            subprocess.run(
                ["git", "-C", os.fspath(repo), *arguments], check=True
            )
        (repo / "kept.txt").write_text("first\n", encoding="utf-8")
        subprocess.run(["git", "-C", os.fspath(repo), "add", "kept.txt"], check=True)
        subprocess.run(
            ["git", "-C", os.fspath(repo), "commit", "--quiet", "-m", "first"],
            check=True,
        )
        (repo / "committed.txt").write_text("worker work\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", os.fspath(repo), "add", "committed.txt"], check=True
        )
        subprocess.run(
            ["git", "-C", os.fspath(repo), "commit", "--quiet", "-m", "work"],
            check=True,
        )
        (repo / "kept.txt").write_text("changed\n", encoding="utf-8")
        (repo / "new.txt").write_text("added\n", encoding="utf-8")
        record = self.open_run(isolation_mode="worktree", isolation_path=os.fspath(repo))
        recorded = receipts.record_changed_files(
            state_dir=self.state, receipt_id=record["receipt_id"]
        )
        observed = {entry["path"] for entry in recorded["changed_files"]["entries"]}
        self.assertEqual({"kept.txt", "new.txt"}, observed)
        self.assertFalse(recorded["changed_files"]["truncated"])
        # A worker that commits its work leaves a clean status; without the base
        # ref the receipt would claim the run changed nothing.
        committed = receipts.record_changed_files(
            state_dir=self.state,
            receipt_id=record["receipt_id"],
            since="HEAD~1",
        )
        self.assertEqual(
            {"kept.txt", "new.txt", "committed.txt"},
            {entry["path"] for entry in committed["changed_files"]["entries"]},
        )
        self.assertEqual("HEAD~1", committed["changed_files"]["since"])

    def test_the_rendered_receipt_states_spend_verifier_files_and_stop(self) -> None:
        receipts.set_cap(state_dir=self.state, msg_id=PLAN, max_usd_est=2.0)
        record = self.open_run(isolation_mode="worktree", isolation_path="/tmp/tree")
        receipts.record_spend(
            state_dir=self.state,
            receipt_id=record["receipt_id"],
            usd_est=0.4,
            tokens=1200,
            iterations=2,
            source="supervisor lane",
        )
        receipts.record_changed_files(
            state_dir=self.state,
            receipt_id=record["receipt_id"],
            entries=[{"status": "M", "path": "lib/thing.py"}],
        )
        receipts.record_verifier(
            state_dir=self.state,
            receipt_id=record["receipt_id"],
            result="failed",
            command="tests/run",
            exit_code=1,
        )
        closed = receipts.close_run(
            state_dir=self.state,
            receipt_id=record["receipt_id"],
            stop_reason="failed",
            stop_detail="two tests red",
        )
        rendered = receipts.render_run(closed)
        self.assertIn("$0.40 est", rendered)
        self.assertIn("1,200 tokens", rendered)
        self.assertIn("worktree", rendered)
        self.assertIn("failed", rendered)
        self.assertIn("tests/run", rendered)
        self.assertIn("lib/thing.py", rendered)
        self.assertIn("two tests red", rendered)
        self.assertIn("$2.00", rendered)
        plan = receipts.render_plan(self.state, PLAN)
        self.assertIn("Recorded so far: $0.40 est", plan)
        self.assertIn(record["receipt_id"], plan)


class ReceiptCommandTests(unittest.TestCase):
    """The verbs an approval, a worker, and a person actually run."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="receipt-cli-")
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.environment = {
            **os.environ,
            "SESSION_KIT_STATE_DIR": os.fspath(self.state),
            "HOME": os.fspath(self.base),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def core(self, *arguments: str, check: bool = True):
        return run(
            [sys.executable, os.fspath(CORE), *arguments],
            env=self.environment,
            check=check,
        )

    def test_the_cap_verb_prints_the_line_the_operator_approves(self) -> None:
        capped = self.core(
            "receipt", "cap", "--msg-id", PLAN, "--max-usd", "3", "--max-tokens", "50000"
        )
        self.assertIn("Cost cap for this plan: $3.00", capped.stderr)
        self.assertIn("50,000 tokens", capped.stderr)
        self.assertEqual(3.0, json.loads(capped.stdout)["max_usd_est"])

    def test_spend_past_the_cap_exits_three_with_the_stop_report(self) -> None:
        self.core("receipt", "cap", "--msg-id", PLAN, "--max-usd", "1")
        opened = json.loads(
            self.core(
                "receipt", "open", "--msg-id", PLAN, "--branch", "worker/one",
                "--provider", "claude", "--model", "claude-opus-test",
                "--isolation", "worktree", "--isolation-path", "/tmp/tree",
            ).stdout
        )
        breached = self.core(
            "receipt", "spend", "--receipt", opened["receipt_id"],
            "--usd", "1.5", "--source", "supervisor lane", check=False,
        )
        self.assertEqual(3, breached.returncode)
        self.assertIn("Stop reason     cap_breached", breached.stderr)
        gated = self.core("receipt", "gate", "--msg-id", PLAN, check=False)
        self.assertEqual(3, gated.returncode)
        self.assertFalse(json.loads(gated.stdout)["allowed"])
        shown = self.core("receipt", "show", "--msg-id", PLAN)
        self.assertIn("STOPPED:", shown.stdout)

    def test_show_renders_one_receipt_and_list_names_the_plan_runs(self) -> None:
        opened = json.loads(
            self.core("receipt", "open", "--msg-id", PLAN, "--branch", "worker/one").stdout
        )
        self.core(
            "receipt", "verifier", "--receipt", opened["receipt_id"],
            "--result", "passed", "--command", "tests/run", "--exit-code", "0",
        )
        self.core(
            "receipt", "close", "--receipt", opened["receipt_id"],
            "--stop-reason", "completed",
        )
        shown = self.core("receipt", "show", "--receipt", opened["receipt_id"])
        self.assertIn("Verifier        passed", shown.stdout)
        self.assertIn("Integrity       verified", shown.stdout)
        listed = json.loads(self.core("receipt", "list", "--msg-id", PLAN).stdout)
        self.assertEqual(
            [opened["receipt_id"]], [row["receipt_id"] for row in listed["receipts"]]
        )
        missing = self.core("receipt", "show", "--receipt", "f" * 32, check=False)
        self.assertEqual(2, missing.returncode)

    def test_closing_as_completed_without_a_verifier_is_refused(self) -> None:
        opened = json.loads(self.core("receipt", "open", "--msg-id", PLAN).stdout)
        refused = self.core(
            "receipt", "close", "--receipt", opened["receipt_id"],
            "--stop-reason", "completed", check=False,
        )
        self.assertEqual(1, refused.returncode)
        self.assertIn("passing verifier result", refused.stderr)


class DelegateCapGateTests(unittest.TestCase):
    """A plan at its cap starts no further worker."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="receipt-gate-")
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir(mode=0o700)
        self.inventory_core = load_core("session_inventory_receipt_gate")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_the_gate_the_launcher_calls_opens_and_closes_with_the_cap(self) -> None:
        self.assertTrue(receipts.gate(self.state, PLAN)["allowed"])
        receipts.set_cap(state_dir=self.state, msg_id=PLAN, max_iterations=1)
        record = receipts.open_run(state_dir=self.state, msg_id=PLAN)
        self.assertTrue(receipts.gate(self.state, PLAN)["allowed"])
        receipts.record_spend(
            state_dir=self.state,
            receipt_id=record["receipt_id"],
            iterations=1,
            source="supervisor lane",
        )
        blocked = receipts.gate(self.state, PLAN)
        self.assertFalse(blocked["allowed"])
        self.assertIn("iteration cap", blocked["reason"])

    def test_the_receipt_command_is_reachable_through_the_facade(self) -> None:
        code = self.inventory_core.main(
            ["receipt", "cap", "--msg-id", PLAN, "--max-usd", "2"]
        )
        self.assertEqual(0, code)


if __name__ == "__main__":
    unittest.main()
