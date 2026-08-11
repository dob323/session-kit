from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.support import REPO

from lib.sessionkit_supervisor.adapter import KitAdapter
from lib.sessionkit_supervisor.source_authority import capture_hook_event
from lib.sessionkit_supervisor.vendor.tools import (
    annotate_worker,
    check_idle_workers,
    list_workers,
    poll_worker_changes,
    read_worker_logs,
    wait_idle_workers,
    worker_events,
)


UUID_A = "00000000-0000-4000-8000-000000000001"
UUID_B = "00000000-0000-4000-8000-000000000002"
UUID_C = "00000000-0000-4000-8000-000000000003"
KEY_A = f"claude:{UUID_A}"
KEY_B = f"codex:{UUID_B}"


def fixture_inventory() -> dict:
    return {
        "source": "live",
        "stale": False,
        "sessions": [
            {
                "provider": "claude",
                "identity": {"uuid": UUID_A},
                "title": "Researcher",
                "cwd": "/srv/one",
                "agent_status": "needs your reply",
                "terminal_number": 2,
                "shpool_id_raw": "main",
                "availability": "ready",
                "recent_output_age_seconds": 1300,
            },
            {
                "provider": "codex",
                "identity": {"uuid": UUID_B},
                "title": "Builder",
                "cwd": "/srv/two",
                "agent_status": "idle",
                "terminal_number": 3,
                "shpool_id_raw": "main2",
                "availability": "ready",
                "recent_output_age_seconds": 10,
            },
        ],
    }


class AdapterFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".supervisor-adapter-", dir=REPO)
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.state = self.base / "state"
        self.home.mkdir()
        self.state.mkdir()
        self.payload = self.base / "inventory.json"
        self.payload.write_text(json.dumps(fixture_inventory()), encoding="utf-8")
        self.core = self.base / "inventory_core.py"
        self.core.write_text(
            "import json, os, sys\n"
            "if sys.argv[1:3] == ['msg', 'queue']:\n"
            " print(json.dumps({'as_of_unix_ms': 42, 'items': []}))\n"
            "else:\n"
            " print(open(os.environ['SUPERVISOR_FIXTURE'], encoding='utf-8').read())\n",
            encoding="utf-8",
        )
        self.env = {
            "HOME": os.fspath(self.home),
            "SESSION_KIT_STATE_DIR": os.fspath(self.state),
            "SESSION_KIT_INVENTORY_CORE": os.fspath(self.core),
            "SUPERVISOR_FIXTURE": os.fspath(self.payload),
        }
        self.adapter = KitAdapter(environ=self.env)

    def close(self) -> None:
        self.temporary.cleanup()

    def authority(
        self,
        *,
        target: str,
        text: str,
        category: str = "fleet_broadcast",
        scope: str = "send the exact confirmed message",
    ) -> dict:
        request = self.adapter.create_authority_request(
            target=target,
            text=text,
            category=category,
            authority_scope=scope,
            source_thread_key=KEY_A,
        )
        prompt = f"{scope}\n{request['confirmation_token']}"
        transcript_root = self.state / "test-transcripts"
        transcript_root.mkdir(mode=0o700, exist_ok=True)
        transcript = transcript_root / f"{request['request_id']}.jsonl"
        transcript.write_text("", encoding="utf-8")
        transcript.chmod(0o600)
        event = capture_hook_event(
            {
                "provider": "claude",
                "session_id": UUID_A,
                "prompt": prompt,
                "transcript_path": os.fspath(transcript),
            },
            state_dir=self.state,
        )
        transcript.write_text(
            json.dumps({"type": "user", "session_id": UUID_A, "text": prompt}) + "\n",
            encoding="utf-8",
        )
        transcript.chmod(0o600)
        return {
            "authority_event_id": event["event_id"],
            "authority_scope": scope,
            "authority_request_id": request["request_id"],
        }


def dispatch_runner(fixture, send):
    # The adapter routes EVERY subprocess through one runner — the inventory
    # build included. A stub that impersonates sp must answer both: the
    # snapshot call with the fixture inventory, everything else with the
    # test's own send behavior.
    def runner(argv, *, timeout, env):
        if "snapshot" in argv:
            payload = fixture.payload.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, payload, "")
        return send(argv, timeout=timeout, env=env)

    return runner


class SupervisorAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = AdapterFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_registry_and_attention_queue_are_fresh_facade_calls(self) -> None:
        self.assertEqual(2, self.fixture.adapter.registry().count())
        self.assertEqual(42, self.fixture.adapter.attention_queue()["as_of_unix_ms"])

    def test_event_tools_read_only_exact_bounded_event_files(self) -> None:
        events = self.fixture.state / "events"
        events.mkdir()
        (events / f"{KEY_A}.jsonl").write_text(
            json.dumps({"ts_unix_ms": 10, "event": "session_start", "question": None, "source": "hook"}) + "\n"
            + json.dumps({"ts_unix_ms": 20, "event": "needs_input", "question": "Which?", "source": "hook"}) + "\n",
            encoding="utf-8",
        )
        result = worker_events.invoke(self.fixture.adapter, {"session_id": KEY_A, "since": 15, "include_summary": True})
        self.assertEqual(1, result["count"])
        self.assertEqual(KEY_A, result["events"][0]["thread_key"])
        poll = poll_worker_changes.invoke(self.fixture.adapter, {"stale_threshold_minutes": 20})
        self.assertEqual(1, poll["idle_count"])
        self.assertEqual([KEY_A], [row["session_id"] for row in poll["summary"]["stuck"]])

    def test_event_scan_uses_only_newest_200_files_and_reports_cap(self) -> None:
        events = self.fixture.state / "events"
        events.mkdir()
        for index in range(201):
            path = events / f"claude:event-{index:03d}.jsonl"
            path.write_text(
                json.dumps({"ts_unix_ms": index, "event": "turn_done"}) + "\n",
                encoding="utf-8",
            )
            os.utime(path, ns=(index + 1, index + 1))
        result = worker_events.invoke(self.fixture.adapter, {"limit": 2000})
        self.assertTrue(result["truncated"])
        self.assertEqual(200, result["count"])
        self.assertNotIn("claude:event-000", {row["thread_key"] for row in result["events"]})

    def test_provider_transcript_tail_is_bounded_and_paginated(self) -> None:
        transcript = self.fixture.home / ".claude/projects/-srv-one" / f"{UUID_A}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("".join(json.dumps({"index": index}) + "\n" for index in range(12)), encoding="utf-8")
        result = read_worker_logs.invoke(self.fixture.adapter, {"session_id": KEY_A, "pages": 1, "offset": 0})
        self.assertEqual([7, 8, 9, 10, 11], [row["index"] for row in result["records"]])
        self.assertEqual(256 * 1024, result["page_info"]["tail_byte_limit"])

    def test_annotation_persists_privately_and_returns_on_fresh_registry(self) -> None:
        result = annotate_worker.invoke(self.fixture.adapter, {"session_id": KEY_A, "badge": "  phase   one  "})
        self.assertTrue(result["success"])
        path = self.fixture.state / "supervisor/annotations.json"
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        worker = self.fixture.adapter.registry().get(KEY_A)
        assert worker is not None
        self.assertEqual("phase one", worker.coordinator_badge)

    def test_annotation_refuses_a_symlinked_store(self) -> None:
        supervisor = self.fixture.state / "supervisor"
        supervisor.mkdir()
        outside = self.fixture.base / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        os.symlink(outside, supervisor / "annotations.json")
        result = annotate_worker.invoke(
            self.fixture.adapter, {"session_id": KEY_A, "badge": "unsafe"}
        )
        self.assertIn("owner-private", result["error"])
        self.assertEqual("{}\n", outside.read_text(encoding="utf-8"))

    def test_idle_check_keeps_needs_reply_distinct(self) -> None:
        result = check_idle_workers.invoke(self.fixture.adapter, {"session_ids": [KEY_A, KEY_B]})
        self.assertEqual({KEY_A: False, KEY_B: True}, result["idle"])
        self.assertEqual("needs your reply", result["statuses"][KEY_A])

    def test_wait_clamps_polling_and_stops_when_human_reply_is_required(self) -> None:
        sleeps: list[float] = []
        blocked_adapter = KitAdapter(
            environ=self.fixture.env,
            sleeper=lambda seconds: sleeps.append(seconds),
        )
        blocked = wait_idle_workers.invoke(
            blocked_adapter,
            {"session_ids": [KEY_A], "timeout": 600, "poll_interval": 0.05},
        )
        self.assertTrue(blocked["blocked_on_human"])
        self.assertFalse(blocked["timed_out"])
        self.assertEqual([], sleeps)

        payload = fixture_inventory()
        payload["sessions"][0]["agent_status"] = "working"
        self.fixture.payload.write_text(json.dumps(payload), encoding="utf-8")
        now = [0.0]

        def sleep_and_advance(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        polling_adapter = KitAdapter(
            environ=self.fixture.env,
            clock=lambda: now[0],
            sleeper=sleep_and_advance,
        )
        timed_out = wait_idle_workers.invoke(
            polling_adapter,
            {"session_ids": [KEY_A], "timeout": 2, "poll_interval": 0.05},
        )
        self.assertTrue(timed_out["timed_out"])
        self.assertEqual(2.0, sleeps[-1])

    def test_list_workers_include_closed_controls_retained_rows(self) -> None:
        payload = fixture_inventory()
        closed = dict(payload["sessions"][1])
        closed.update(
            identity={"uuid": UUID_C},
            title="Closed worker",
            agent_status="provider exited",
        )
        payload["sessions"].append(closed)
        self.fixture.payload.write_text(json.dumps(payload), encoding="utf-8")
        hidden = list_workers.invoke(self.fixture.adapter, {})
        shown = list_workers.invoke(self.fixture.adapter, {"include_closed": True})
        self.assertEqual(2, hidden["count"])
        self.assertEqual(3, shown["count"])
        self.assertEqual(
            "provider exited",
            next(row for row in shown["workers"] if row["uuid"] == UUID_C)["status"],
        )

    def test_lane_three_and_broadcasts_refuse_before_sp(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, *, timeout, env):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "Sent x to 1 target(s) · delivered 1 · not delivered 0", "")

        adapter = KitAdapter(environ=self.fixture.env, runner=dispatch_runner(self.fixture, runner))
        lane_three = adapter.send_message(
            target=KEY_A, text="hello", lane=3, category="factual_agent_reply"
        )
        broadcast = adapter.send_message(
            target="all", text="hello", lane=2, category="factual_agent_reply"
        )
        self.assertTrue(lane_three["refused"])
        self.assertTrue(broadcast["refused"])
        self.assertEqual([], calls)

    def test_keys_selector_exact_bypass_argv_now_refuses(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, *, timeout, env):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "Sent x to 1 target(s) · delivered 1 · not delivered 0", "")

        adapter = KitAdapter(environ=self.fixture.env, runner=dispatch_runner(self.fixture, runner))
        target = f"keys:{KEY_A},{KEY_B},claude:{UUID_C}"
        result = adapter.send_message(
            target=target, text="do it", lane=1, category="factual_agent_reply"
        )
        self.assertTrue(result["refused"])
        self.assertIn("broadcast", result["reason"])
        self.assertEqual([], calls)

    def test_ratchet_effective_lane_authorization_and_ledger_outcomes(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, *, timeout, env):
            calls.append(list(argv))
            return subprocess.CompletedProcess(
                argv,
                # Thread keys translate to wire targets: KEY_A is
                # terminal 2 in the fixture, KEY_B terminal 3.
                0 if argv[-2] == "2" else 7,
                "Sent x to 1 target(s) · delivered 1 · not delivered 0" if argv[-2] == "2" else "Sent x to 1 target(s) · delivered 0 · not delivered 1",
                "failed" if argv[-2] != "2" else "",
            )

        adapter = KitAdapter(environ=self.fixture.env, runner=dispatch_runner(self.fixture, runner))
        raised = adapter.send_message(
            target=KEY_A,
            text="do it",
            lane=1,
            category="fleet_broadcast",
        )
        self.assertTrue(raised["refused"])
        self.assertEqual(3, raised["effective_lane"])
        confirmed = adapter.send_message(
            target=KEY_A,
            text="do it",
            lane=1,
            operator_confirmed=True,
            category="fleet_broadcast",
            **self.fixture.authority(target=KEY_A, text="do it"),
        )
        self.assertTrue(confirmed["success"])

        good = adapter.send_message(
            target=KEY_A,
            text="first",
            lane=1,
            category="factual_agent_reply",
        )
        bad = adapter.send_message(
            target=KEY_B,
            text="second",
            lane=1,
            category="single_target_nudge",
        )
        self.assertTrue(good["success"])
        self.assertFalse(bad["success"])
        ledger = self.fixture.state / "supervisor/ledger.jsonl"
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["ok", "ok", "unknown"], [row["outcome"] for row in rows])
        self.assertTrue(all(row["brief_shown"] is False for row in rows))

    def test_zero_delivered_receipt_records_unknown_not_ok(self) -> None:
        # Exit 0 with "delivered 0" is a send that did not land — the re-drill
        # caught it scoring ok and feeding a false promotion streak.
        from lib.sessionkit_supervisor.ratchet import Ratchet

        now = 1_700_000_000.0

        def runner(argv, *, timeout, env):
            return subprocess.CompletedProcess(
                argv, 0, "Sent x to 1 target(s) · delivered 0 · not delivered 1", ""
            )

        adapter = KitAdapter(
            environ=self.fixture.env,
            runner=dispatch_runner(self.fixture, runner),
            clock=lambda: now,
        )
        result = adapter.send_message(
            target=KEY_A, text="hello", lane=2, category="single_target_nudge"
        )
        self.assertFalse(result["success"])
        ratchet = Ratchet(self.fixture.state, clock=lambda: now)
        rows = [
            json.loads(line)
            for line in ratchet.ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual("unknown", rows[-1]["outcome"])
        self.assertEqual(0, ratchet.lanes()["single_target_nudge"]["streak"])

    def test_confirmed_acts_record_their_lane_and_build_no_streak(self) -> None:
        from lib.sessionkit_supervisor.ratchet import Ratchet

        now = 1_700_000_000.0

        def runner(argv, *, timeout, env):
            return subprocess.CompletedProcess(
                argv, 0, "Sent x to 1 target(s) · delivered 1 · not delivered 0", ""
            )

        adapter = KitAdapter(
            environ=self.fixture.env,
            runner=dispatch_runner(self.fixture, runner),
            clock=lambda: now,
        )
        result = adapter.send_message(
            target="all",
            text="operator says go",
            lane=3,
            operator_confirmed=True,
            category="fleet_broadcast",
            **self.fixture.authority(target="all", text="operator says go"),
        )
        self.assertTrue(result["success"])
        ratchet = Ratchet(self.fixture.state, clock=lambda: now)
        rows = [
            json.loads(line)
            for line in ratchet.ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        # The row records the lane the act RAN under and that the operator
        # confirmed
        # it, and a confirmed act never counts as an autonomous turn.
        self.assertEqual(3, rows[-1]["lane"])
        self.assertTrue(rows[-1]["operator_confirmed"])
        self.assertEqual(result["authority_event_id"], rows[-1]["authority_event_id"])
        self.assertEqual("send the exact confirmed message", rows[-1]["authority_scope"])
        self.assertEqual(0, ratchet.budget_status()["autonomous_turns"])

    def test_mark_briefed_flips_only_the_named_rows(self) -> None:
        from lib.sessionkit_supervisor.ratchet import Ratchet

        now = 1_700_000_000.0
        ratchet = Ratchet(self.fixture.state, clock=lambda: now)
        ratchet.ensure()
        first = ratchet.record(
            category="silence_chase", action="chase", target="w1",
            outcome="ok", brief_shown=False, ts=1_700_000_000_000,
        )["entry"]["ts"]
        second = ratchet.record(
            category="silence_chase", action="chase", target="w2",
            outcome="ok", brief_shown=False, ts=1_700_000_000_001,
        )["entry"]["ts"]
        outcome = self.fixture.adapter.mark_briefed([first])
        self.assertEqual({"flipped": 1, "requested": 1}, outcome)
        rows = {
            row["ts"]: row["brief_shown"]
            for row in map(
                json.loads,
                ratchet.ledger_path.read_text(encoding="utf-8").splitlines(),
            )
        }
        self.assertTrue(rows[first])
        self.assertFalse(rows[second])

    def test_unknown_thread_key_fails_before_any_claim_or_ledger_row(self) -> None:
        from lib.sessionkit_supervisor.adapter import SupervisorError
        from lib.sessionkit_supervisor.ratchet import Ratchet

        calls: list[list[str]] = []

        def runner(argv, *, timeout, env):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "Sent x to 1 target(s) · delivered 1 · not delivered 0", "")

        adapter = KitAdapter(environ=self.fixture.env, runner=dispatch_runner(self.fixture, runner))
        with self.assertRaisesRegex(SupervisorError, "target session not found"):
            adapter.send_message(
                target=f"claude:{UUID_C}",
                text="hello",
                lane=1,
                category="factual_agent_reply",
            )
        self.assertEqual([], [argv for argv in calls if "msg" in argv])
        ratchet = Ratchet(self.fixture.state)
        self.assertFalse(ratchet.claims_path.exists())
        self.assertFalse(
            (self.fixture.state / "supervisor/ledger.jsonl").exists()
        )

    def test_ratchet_budget_refusal_skips_sp(self) -> None:
        from lib.sessionkit_supervisor.ratchet import Ratchet

        now = 1_700_000_000.0
        ratchet = Ratchet(self.fixture.state, clock=lambda: now)
        ratchet.ensure()
        budgets = ratchet.budgets_path
        budgets.write_text(
            json.dumps({"per_day_autonomous_turns": 1, "per_day_usd_est": 5.0}),
            encoding="utf-8",
        )
        ratchet.record(
            category="status_compilation",
            action="prior act",
            target=KEY_A,
            outcome="ok",
            brief_shown=False,
        )
        calls: list[list[str]] = []

        def runner(argv, *, timeout, env):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "Sent x to 1 target(s) · delivered 1 · not delivered 0", "")

        adapter = KitAdapter(
            environ=self.fixture.env,
            runner=dispatch_runner(self.fixture, runner),
            clock=lambda: now,
        )
        result = adapter.send_message(
            target=KEY_A,
            text="budgeted",
            lane=1,
            category="status_compilation",
        )
        self.assertTrue(result["refused"])
        self.assertEqual("budget", result["reason"])
        self.assertEqual([], calls)

    def test_omitted_and_unknown_categories_refuse_without_budget_escape(self) -> None:
        from lib.sessionkit_supervisor.ratchet import Ratchet

        now = 1_700_000_000.0
        ratchet = Ratchet(self.fixture.state, clock=lambda: now)
        ratchet.ensure()
        ratchet.budgets_path.write_text(
            json.dumps({"per_day_autonomous_turns": 1, "per_day_usd_est": 5.0}),
            encoding="utf-8",
        )
        ratchet.record(
            category="status_compilation",
            action="prior act",
            target=KEY_A,
            outcome="ok",
            brief_shown=False,
        )
        calls: list[list[str]] = []

        def runner(argv, *, timeout, env):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "Sent x to 1 target(s) · delivered 1 · not delivered 0", "")

        adapter = KitAdapter(environ=self.fixture.env, runner=dispatch_runner(self.fixture, runner), clock=lambda: now)
        omitted = adapter.send_message(target=KEY_A, text="omitted", lane=1)
        known = adapter.send_message(
            target=KEY_A,
            text="known",
            lane=1,
            category="status_compilation",
        )
        unknown = adapter.send_message(
            target=KEY_A,
            text="unknown",
            lane=1,
            category="totally_new_category",
        )
        self.assertEqual("category", omitted["reason"])
        self.assertEqual("budget", known["reason"])
        self.assertEqual("category", unknown["reason"])
        self.assertTrue(all(row["refused"] for row in (omitted, known, unknown)))
        self.assertEqual([], calls)

    def test_operator_confirmed_lane_three_is_recorded_without_budget_usage(self) -> None:
        from lib.sessionkit_supervisor.ratchet import Ratchet

        now = 1_700_000_000.0
        ratchet = Ratchet(self.fixture.state, clock=lambda: now)
        before = ratchet.budget_status()
        calls: list[list[str]] = []

        def runner(argv, *, timeout, env):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "Sent x to 1 target(s) · delivered 1 · not delivered 0", "")

        adapter = KitAdapter(environ=self.fixture.env, runner=dispatch_runner(self.fixture, runner), clock=lambda: now)
        result = adapter.send_message(
            target="all",
            text="confirmed broadcast",
            lane=3,
            operator_confirmed=True,
            category="fleet_broadcast",
            **self.fixture.authority(target="all", text="confirmed broadcast"),
        )
        after = ratchet.budget_status()
        rows = [
            json.loads(line)
            for line in ratchet.ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(result["success"])
        self.assertEqual(1, len(calls))
        self.assertEqual(3, rows[-1]["lane"])
        self.assertEqual("fleet_broadcast", rows[-1]["category"])
        self.assertEqual(before["autonomous_turns"], after["autonomous_turns"])
        self.assertEqual(before["usd_est"], after["usd_est"])
        self.assertFalse(ratchet.claims_path.exists())

    def test_operator_confirmed_act_is_not_blocked_by_the_autonomous_budget(self) -> None:
        from lib.sessionkit_supervisor.ratchet import Ratchet

        now = 1_700_000_000.0
        ratchet = Ratchet(self.fixture.state, clock=lambda: now)
        ratchet.ensure()
        limits = ratchet.budgets()
        for _ in range(int(limits["per_day_autonomous_turns"])):
            ratchet.record(
                category="silence_chase",
                action="chase",
                target="fleet",
                outcome="ok",
                brief_shown=True,
            )
        self.assertTrue(ratchet.budget_status()["exceeded"])
        calls: list[list[str]] = []

        def runner(argv, *, timeout, env):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "Sent x to 1 target(s) · delivered 1 · not delivered 0", "")

        adapter = KitAdapter(environ=self.fixture.env, runner=dispatch_runner(self.fixture, runner), clock=lambda: now)
        result = adapter.send_message(
            target="all",
            text="operator says go",
            lane=3,
            operator_confirmed=True,
            category="fleet_broadcast",
            **self.fixture.authority(target="all", text="operator says go"),
        )
        self.assertTrue(result["success"])
        self.assertEqual(1, len(calls))

    def test_send_message_category_enum_matches_the_seeded_lanes(self) -> None:
        from pathlib import Path

        from lib.sessionkit_supervisor import server as server_module

        send_tool = next(
            tool for tool in server_module.TOOLS if tool["name"] == "send_message"
        )
        enum = send_tool["inputSchema"]["properties"]["category"]["enum"]
        seeded = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "config"
                / "supervisor"
                / "lanes.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(sorted(seeded.keys()), sorted(enum))

    def test_runner_exception_records_unknown_and_releases_budget_claim(self) -> None:
        from lib.sessionkit_supervisor.ratchet import Ratchet

        now = 1_700_000_000.0
        ratchet = Ratchet(self.fixture.state, clock=lambda: now)
        ratchet.ensure()

        def runner(argv, *, timeout, env):
            raise RuntimeError("injected runner failure")

        adapter = KitAdapter(environ=self.fixture.env, runner=dispatch_runner(self.fixture, runner), clock=lambda: now)
        with self.assertRaisesRegex(RuntimeError, "injected runner failure"):
            adapter.send_message(
                target=KEY_A,
                text="will fail",
                lane=2,
                category="factual_agent_reply",
            )
        claims = json.loads(ratchet.claims_path.read_text(encoding="utf-8"))
        rows = [
            json.loads(line)
            for line in ratchet.ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([], claims)
        # A raising runner is a transport fault: the outcome is unknown, the
        # claim is released, and the category is NOT demoted to lane 3.
        self.assertEqual("unknown", rows[-1]["outcome"])
        self.assertEqual(2, ratchet.lanes()["factual_agent_reply"]["lane"])

    def test_refusal_uses_authorize_reason_code_not_reason_text(self) -> None:
        class FakeRatchet:
            def __init__(self, state_dir, *, clock):
                pass

            def lanes(self):
                return {"factual_agent_reply": {"lane": 2}}

            def authorize(self, category, *, operator_confirmed=False):
                return {
                    "allowed": False,
                    "reason": "budget",
                    "reasons": ["diagnostic containing lane text"],
                }

        broken_text_module = mock.Mock(Ratchet=FakeRatchet)
        with mock.patch(
            "lib.sessionkit_supervisor.ratchet_module",
            return_value=broken_text_module,
        ):
            result = self.fixture.adapter.send_message(
                target=KEY_A,
                text="blocked",
                lane=1,
                category="factual_agent_reply",
            )
        self.assertTrue(result["refused"])
        self.assertEqual("budget", result["reason"])

    def test_missing_ratchet_class_refuses_closed(self) -> None:
        with mock.patch(
            "lib.sessionkit_supervisor.ratchet_module",
            return_value=object(),
        ):
            result = self.fixture.adapter.send_message(
                target=KEY_A,
                text="blocked",
                lane=1,
                category="factual_agent_reply",
            )
        self.assertTrue(result["refused"])
        self.assertEqual("ratchet-unavailable", result["reason"])

    def test_confirmed_gate_shells_exact_sp_msg_command(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, *, timeout, env):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "Sent x to 1 target(s) · delivered 1 · not delivered 0", "")

        env = dict(self.fixture.env, SESSION_KIT_SP_CMD="/fixture/sp")
        adapter = KitAdapter(environ=env, runner=dispatch_runner(self.fixture, runner))
        result = adapter.send_message(
            target="all",
            text="status",
            lane=3,
            operator_confirmed=True,
            category="fleet_broadcast",
            **self.fixture.authority(target="all", text="status"),
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            ["/fixture/sp", "msg", "--yes", "--", "all", "status"], calls[0]
        )

    def test_dash_prefixed_message_is_delivered_after_end_of_options(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, *, timeout, env):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "Sent x to 1 target(s) · delivered 1 · not delivered 0", "")

        env = dict(self.fixture.env, SESSION_KIT_SP_CMD="/fixture/sp")
        adapter = KitAdapter(environ=env, runner=dispatch_runner(self.fixture, runner))
        result = adapter.send_message(
            target=KEY_A,
            text="--fyi is my message",
            lane=1,
            category="factual_agent_reply",
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            ["/fixture/sp", "msg", "--yes", "--", "2", "--fyi is my message"],
            calls[0],
        )


if __name__ == "__main__":
    unittest.main()
