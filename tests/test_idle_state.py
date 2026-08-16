from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.support import REPO

from lib.sessionkit_inventory import idle_state, labels, transcripts
from lib.sessionkit_inventory import processes as process_inventory
from tests.test_inventory import inventory_core, inventory_fixture


UUID = "00000000-0000-4000-8000-000000000001"
MINUTE_MS = 60_000


def waiting_inventory(provider: str = "claude") -> dict:
    return {
        "sessions": [
            {
                "provider": provider,
                "identity": {"uuid": UUID},
                "agent_status": "idle",
                "needs_you": False,
            }
        ]
    }


def signature(*, size: int = 10, mtime_ns: int = 20) -> dict[str, object]:
    return {"path": f"/tmp/{UUID}.jsonl", "size": size, "mtime_ns": mtime_ns}


class TranscriptSnapshotTests(unittest.TestCase):
    def test_claude_snapshot_uses_path_size_and_nanosecond_mtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".idle-transcript-", dir=REPO) as raw:
            home = Path(raw)
            transcript = home / ".claude" / "projects" / "fixture" / f"{UUID}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("one line\n", encoding="utf-8")
            os.utime(transcript, ns=(123_000_000_000, 123_456_789_012))

            found = transcripts.transcript_snapshot(
                "claude", UUID, environ={"HOME": os.fspath(home)}
            )

            self.assertEqual(os.fspath(transcript), found["path"])
            self.assertEqual(transcript.stat().st_size, found["size"])
            self.assertEqual(123_456_789_012, found["mtime_ns"])

    def test_codex_rollout_uses_the_same_stable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".idle-rollout-", dir=REPO) as raw:
            home = Path(raw)
            rollout = (
                home
                / ".codex"
                / "sessions"
                / "2026"
                / "08"
                / f"rollout-2026-08-16T00-00-00-{UUID}.jsonl"
            )
            rollout.parent.mkdir(parents=True)
            rollout.write_text("{\"type\":\"session_meta\"}\n", encoding="utf-8")

            found = transcripts.transcript_snapshot(
                "codex", UUID, environ={"HOME": os.fspath(home)}
            )

            self.assertEqual(os.fspath(rollout), found["path"])
            self.assertEqual(rollout.stat().st_size, found["size"])
            self.assertEqual(rollout.stat().st_mtime_ns, found["mtime_ns"])

    def test_missing_or_symlinked_transcript_is_not_idle_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".idle-transcript-", dir=REPO) as raw:
            home = Path(raw)
            project = home / ".claude" / "projects" / "fixture"
            project.mkdir(parents=True)
            target = project / "target.jsonl"
            target.write_text("output\n", encoding="utf-8")
            (project / f"{UUID}.jsonl").symlink_to(target)

            self.assertIsNone(
                transcripts.transcript_snapshot(
                    "claude", UUID, environ={"HOME": os.fspath(home)}
                )
            )


class IdleThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix=".idle-window-", dir=REPO)
        self.addCleanup(scratch.cleanup)
        self.state = Path(scratch.name)
        self.path = self.state / idle_state.IDLE_MINUTES_FILE

    def test_missing_file_uses_thirty_minutes_and_number_overrides(self) -> None:
        self.assertEqual(30.0, idle_state.idle_minutes(self.state))
        self.path.write_text("75\n", encoding="utf-8")
        self.assertEqual(75.0, idle_state.idle_minutes(self.state))

    def test_invalid_values_disable_idling(self) -> None:
        for raw in ("", "0", "-1", "nan", "inf", "half an hour", "9" * 65):
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                self.assertIsNone(idle_state.idle_minutes(self.state))

    def test_unreadable_threshold_disables_instead_of_using_default(self) -> None:
        self.path.write_text("120\n", encoding="utf-8")
        original = Path.read_bytes

        def refuse(path: Path) -> bytes:
            if path == self.path:
                raise PermissionError("operator file is unreadable")
            return original(path)

        with mock.patch.object(Path, "read_bytes", refuse):
            self.assertIsNone(idle_state.idle_minutes(self.state))

        current = waiting_inventory()
        with mock.patch.object(Path, "read_bytes", refuse):
            idle_state.apply_idle_evidence(
                current,
                waiting_inventory(),
                state_dir=self.state,
                transcript_snapshot=lambda *_args: signature(),
                now_ms=99 * MINUTE_MS,
            )
        self.assertFalse(current["sessions"][0][idle_state.IDLE_FIELD])


class TranscriptMovementIdleTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix=".idle-clock-", dir=REPO)
        self.addCleanup(scratch.cleanup)
        self.state = Path(scratch.name)

    def observe(self, current: dict, previous: dict | None, at_minutes: int, seen) -> dict:
        return idle_state.apply_idle_evidence(
            current,
            previous,
            state_dir=self.state,
            transcript_snapshot=lambda *_args: seen,
            now_ms=at_minutes * MINUTE_MS,
        )

    def test_first_observation_arms_and_exactly_thirty_minutes_becomes_idle(self) -> None:
        first = self.observe(waiting_inventory(), None, 0, signature())
        self.assertFalse(first["sessions"][0][idle_state.IDLE_FIELD])

        second = self.observe(waiting_inventory(), first, 30, signature())
        row = second["sessions"][0]
        self.assertTrue(row[idle_state.IDLE_FIELD])
        self.assertEqual(labels.IDLE, labels.session_state(row, stall_seconds=2700))

    def test_claude_and_codex_rows_share_the_same_idle_rule(self) -> None:
        outcomes = []
        for provider in ("claude", "codex"):
            first = self.observe(waiting_inventory(provider), None, 0, signature())
            second = self.observe(waiting_inventory(provider), first, 30, signature())
            outcomes.append(second["sessions"][0][idle_state.IDLE_FIELD])
        self.assertEqual([True, True], outcomes)

    def test_size_or_mtime_movement_resets_the_clock(self) -> None:
        for moved in (signature(size=11), signature(mtime_ns=21)):
            with self.subTest(moved=moved):
                first = self.observe(waiting_inventory(), None, 0, signature())
                changed = self.observe(waiting_inventory(), first, 30, moved)
                self.assertFalse(changed["sessions"][0][idle_state.IDLE_FIELD])
                aged = self.observe(waiting_inventory(), changed, 60, moved)
                self.assertTrue(aged["sessions"][0][idle_state.IDLE_FIELD])

    def test_unreadable_transcript_clears_an_older_idle_answer(self) -> None:
        first = self.observe(waiting_inventory(), None, 0, signature())
        idle = self.observe(waiting_inventory(), first, 30, signature())
        unreadable = self.observe(waiting_inventory(), idle, 31, None)
        row = unreadable["sessions"][0]
        self.assertFalse(row[idle_state.IDLE_FIELD])
        self.assertNotIn("_transcript_idle_evidence", row)

    def test_only_a_state_that_would_need_you_can_become_idle(self) -> None:
        cases = (
            ({"agent_status": "working", "needs_you": False}, labels.WORKING),
            ({"agent_status": "state unavailable", "needs_you": False}, labels.STATUS_UNAVAILABLE),
            ({"setup_incomplete": True, "needs_you": True}, labels.SETUP_INCOMPLETE),
        )
        for fields, expected in cases:
            with self.subTest(fields=fields):
                row = {**fields, idle_state.IDLE_FIELD: True}
                self.assertEqual(expected, labels.session_state(row, stall_seconds=2700))

    def test_non_needs_states_do_not_resolve_a_transcript(self) -> None:
        cases = (
            {"agent_status": "working", "needs_you": False},
            {
                "agent_status": "idle",
                "needs_you": True,
                "blocking_question": True,
            },
            {"agent_status": "idle", "needs_you": True, "setup_incomplete": True},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                current = waiting_inventory()
                current["sessions"][0].update(fields)
                idle_state.apply_idle_evidence(
                    current,
                    None,
                    state_dir=self.state,
                    transcript_snapshot=lambda *_args: self.fail(
                        "a non-needs state must not resolve its transcript"
                    ),
                    now_ms=0,
                )
                self.assertNotIn(
                    "_transcript_idle_evidence", current["sessions"][0]
                )


class ChurnedCensusIdleDifferentialTests(unittest.TestCase):
    @staticmethod
    def build(*, churned: bool) -> dict:
        fixture = list(inventory_fixture(1, providers=("claude",)))
        fixture[1][0]["status"] = "idle"
        if churned:
            table = process_inventory.ProcessTable(fixture[2])
            table.complete = False
            fixture[2] = table
        return inventory_core.build_inventory(
            *fixture,
            now=1_800_000_000,
            daemon_binding={"pid": 10, "process_start_ticks": 100},
        )

    def test_idle_evidence_is_identical_after_a_live_census_hole(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".idle-churn-", dir=REPO) as raw:
            state = Path(raw)
            outcomes = []
            for churned in (False, True):
                initial = self.build(churned=churned)
                idle_state.apply_idle_evidence(
                    initial,
                    None,
                    state_dir=state,
                    transcript_snapshot=lambda *_args: signature(),
                    now_ms=0,
                )
                current = self.build(churned=churned)
                idle_state.apply_idle_evidence(
                    current,
                    initial,
                    state_dir=state,
                    transcript_snapshot=lambda *_args: signature(),
                    now_ms=30 * MINUTE_MS,
                )
                row = current["sessions"][0]
                outcomes.append(
                    (
                        row["provider"],
                        row["identity"]["uuid"],
                        row[idle_state.IDLE_FIELD],
                        labels.session_state(row, stall_seconds=2700),
                    )
                )
            self.assertEqual(outcomes[0], outcomes[1])
            self.assertEqual(("claude", UUID, True, labels.IDLE), outcomes[0])


class SnapshotIdleWiringTests(unittest.TestCase):
    def test_facade_passes_the_live_transcript_reader_into_snapshot_overlay(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".idle-wiring-", dir=REPO) as raw:
            state = Path(raw)
            with mock.patch(
                "session_inventory._snapshot.snapshot",
                return_value={"sessions": []},
            ) as orchestrator:
                inventory_core.snapshot(config={"state_dir": state})
            callback = orchestrator.call_args.kwargs["apply_session_idle_states"]
            current = waiting_inventory()
            with mock.patch(
                "session_inventory._transcripts.transcript_snapshot",
                return_value=signature(),
            ) as reader:
                previous = waiting_inventory()
                previous["sessions"][0]["_transcript_idle_evidence"] = {
                    **signature(),
                    "last_moved_at_unix_ms": 0,
                    "observed_at_unix_ms": 0,
                    "window_seconds": 1800.0,
                }
                with mock.patch(
                    "sessionkit_inventory.idle_state.time.time", return_value=1800
                ):
                    callback(current, previous, state_dir=state)
            reader.assert_called_once_with("claude", UUID)
            self.assertIn("_transcript_idle_evidence", current["sessions"][0])
            self.assertEqual(labels.IDLE, current["sessions"][0]["state"])


if __name__ == "__main__":
    unittest.main()
