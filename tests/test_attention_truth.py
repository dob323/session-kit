"""Who needs a person, taken from Claude's push signal and pinned to real strings.

Two failures are covered here, and the second one is the reason for the first.

A poll cannot see a question that appears between two polls. `claude agents
--json` is the most expensive part of a snapshot, so it cannot simply run more
often, and a session that asks something the moment after a collection is
invisible until the next one. Claude Code fires a Notification hook at the
instant attention is wanted, so the hook becomes the primary signal and the
poll becomes reconciliation.

And the vocabulary in the documentation is not the vocabulary on the wire. A
live 2.1.229 reports `"status": "busy"` for a working session; the docs say
"working". Anything written against the documented word matches nothing and
says so to no one. Every vendor string this kit compares against is therefore
recorded here with the version and date it was read on, and changing one
without new live evidence fails this file.

Live evidence behind the golden values (2026-08-13, Claude Code 2.1.229):
  * `claude agents --json` returned exactly one interactive row,
    `"status": "busy"` -- reproduced in GOLDEN_POLL_ROW;
  * the Notification matcher enum in the 2.1.229 binary lists eight
    notification_type values -- reproduced in NOTIFICATION_TYPES;
  * the hook payload schema for Notification is base fields plus `message`,
    optional `title`, and `notification_type`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))

from sessionkit_inventory import attention  # noqa: E402
from sessionkit_inventory import providers_claude  # noqa: E402
from sessionkit_inventory import pulse  # noqa: E402


HOOK = REPO / "config" / "claude" / "attention_hook.sh"
SESSION = "22222222-3333-4444-8555-666666666666"


def run_hook(state_dir: Path, payload: dict) -> subprocess.CompletedProcess:
    fixture_root = state_dir.parent
    fixture_bin = fixture_root / "bin"
    fixture_bin.mkdir(parents=True, exist_ok=True)
    refusing_shpool = fixture_bin / "shpool"
    refusing_shpool.write_text(
        "#!/bin/sh\necho 'test fixture refuses shpool' >&2\nexit 97\n",
        encoding="utf-8",
    )
    refusing_shpool.chmod(0o755)
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env={
            "HOME": str(fixture_root),
            "PATH": f"{fixture_bin}{os.pathsep}/usr/bin:/bin",
            "XDG_STATE_HOME": str(fixture_root / "xdg-state"),
            "XDG_DATA_HOME": str(fixture_root / "xdg-data"),
            "SESSION_KIT_STATE_DIR": str(state_dir),
            "SESSION_KIT_CONFIG": str(fixture_root / "config.toml"),
        },
        timeout=30,
    )


class GoldenVendorVocabularyTests(unittest.TestCase):
    """The strings, exactly as a live install says them."""

    def test_a_working_session_reports_busy_not_working(self) -> None:
        self.assertEqual("busy", attention.GOLDEN_POLL_ROW["status"])
        self.assertEqual("busy", attention.POLL_BUSY_STATUS)
        rows = providers_claude._parse_claude_payload(
            [dict(attention.GOLDEN_POLL_ROW)], palette=()
        )
        self.assertEqual(1, len(rows))
        # The kit's own word for it is "working" -- that translation is the
        # whole point, and it only happens because the comparison is against
        # the string the vendor actually sends.
        self.assertEqual("working", rows[0]["status"])
        self.assertFalse(rows[0]["needs_you"])

    def test_the_live_statuses_are_recorded_with_their_kit_wording(self) -> None:
        self.assertEqual(("busy", "idle"), attention.GOLDEN_POLL_STATUSES)
        self.assertEqual(("working", False), attention.poll_attention("busy", None))
        self.assertEqual(("idle", False), attention.poll_attention("idle", None))

    def test_the_notification_enum_is_the_one_the_binary_carries(self) -> None:
        self.assertEqual(
            (
                "permission_prompt",
                "idle_prompt",
                "auth_success",
                "elicitation_dialog",
                "elicitation_complete",
                "elicitation_response",
                "agent_needs_input",
                "agent_completed",
            ),
            attention.NOTIFICATION_TYPES,
        )
        # Every type is classified deliberately: raising attention, answering
        # it, or -- for none of them today -- left to the poll.
        for name in attention.NOTIFICATION_TYPES:
            self.assertIn(
                name,
                attention.NEEDS_YOU_NOTIFICATIONS | attention.CLEARING_NOTIFICATIONS,
                f"{name} is classified nowhere",
            )

    def test_a_waiting_row_is_still_read_from_the_poll_alone(self) -> None:
        row = dict(attention.GOLDEN_POLL_ROW)
        row["waitingFor"] = "a decision"
        rows = providers_claude._parse_claude_payload([row], palette=())
        self.assertTrue(rows[0]["needs_you"])
        self.assertEqual("needs your reply", rows[0]["status"])
        self.assertEqual("poll", rows[0]["attention_source"])


class HookRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="attention-")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state"

    def test_a_permission_prompt_is_recorded_as_needing_a_person(self) -> None:
        result = run_hook(
            self.state,
            {
                "session_id": SESSION,
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
                "message": "Claude needs your permission to use Bash",
            },
        )
        self.assertEqual(0, result.returncode, result.stderr)
        record = attention.read_record(self.state, SESSION)
        self.assertIsNotNone(record)
        self.assertTrue(record["needs_you"])
        self.assertEqual("permission_prompt", record["notification_type"])
        path = attention.record_path(self.state, SESSION)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_a_submitted_prompt_says_the_person_answered(self) -> None:
        run_hook(
            self.state,
            {
                "session_id": SESSION,
                "hook_event_name": "Notification",
                "notification_type": "idle_prompt",
                "message": "waiting",
            },
        )
        run_hook(
            self.state,
            {"session_id": SESSION, "hook_event_name": "UserPromptSubmit"},
        )
        record = attention.read_record(self.state, SESSION)
        self.assertFalse(record["needs_you"])
        self.assertEqual("UserPromptSubmit", record["hook_event"])

    def test_a_record_more_than_five_seconds_in_the_future_is_not_evidence(
        self,
    ) -> None:
        attention.write_record(
            self.state,
            session_id=SESSION,
            hook_event="UserPromptSubmit",
            needs_you=False,
            recorded_at_ms=int((time.time() + 3600) * 1000),
        )
        self.assertIsNone(attention.read_record(self.state, SESSION))

    def test_small_clock_skew_is_tolerated(self) -> None:
        attention.write_record(
            self.state,
            session_id=SESSION,
            hook_event="UserPromptSubmit",
            needs_you=False,
            recorded_at_ms=int((time.time() + 2) * 1000),
        )
        self.assertIsNotNone(attention.read_record(self.state, SESSION))

    def test_a_type_this_kit_does_not_know_leaves_the_record_alone(self) -> None:
        run_hook(
            self.state,
            {
                "session_id": SESSION,
                "hook_event_name": "Notification",
                "notification_type": "agent_needs_input",
            },
        )
        result = run_hook(
            self.state,
            {
                "session_id": SESSION,
                "hook_event_name": "Notification",
                "notification_type": "some_future_vendor_event",
            },
        )
        self.assertEqual(0, result.returncode)
        # Still needing a person: an unrecognised event may not quietly clear
        # a question that is still on the screen.
        self.assertTrue(attention.read_record(self.state, SESSION)["needs_you"])

    def test_a_finished_session_leaves_no_record_behind(self) -> None:
        run_hook(
            self.state,
            {
                "session_id": SESSION,
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
            },
        )
        run_hook(self.state, {"session_id": SESSION, "hook_event_name": "SessionEnd"})
        self.assertIsNone(attention.read_record(self.state, SESSION))

    def test_rubbish_input_writes_nothing_and_still_exits_zero(self) -> None:
        for payload in (
            {"hook_event_name": "Notification", "notification_type": "idle_prompt"},
            {"session_id": "not-a-uuid", "hook_event_name": "Notification"},
            {"session_id": SESSION, "hook_event_name": "PreToolUse"},
        ):
            result = run_hook(self.state, payload)
            self.assertEqual(0, result.returncode, payload)
            self.assertEqual(b"", result.stdout)
        self.assertIsNone(attention.read_record(self.state, SESSION))


class PulseTests(unittest.TestCase):
    """The watcher that lets the poll widen without anyone waiting longer.

    Every source of a needs-you change that is NOT a shpool event has to be in
    here, or the picker would be widening its poll over a blind spot: the
    Claude hook records, every Claude session record (ambient AND account
    profiles -- on a multi-account machine nearly every live session is in a
    profile), the Codex rollouts, and the fleet's stall file.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pulse-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.state = self.home / ".local" / "state" / "session-kit"
        self.environ = {
            "SESSION_KIT_STATE_DIR": str(self.state),
            "SESSION_KIT_ACCOUNT_ROOT": str(self.home / "accounts"),
            "CODEX_HOME": str(self.home / ".codex"),
            "FLEET_STATE_DIR": str(self.home / "fleet"),
        }

    def digest(self) -> str:
        return pulse.fingerprint(self.environ, self.home)

    def write(self, path: Path, text: str = "{}") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_every_source_of_a_waiting_session_moves_the_fingerprint(self) -> None:
        sources = {
            "the Claude hook record": self.state
            / "attention"
            / "claude"
            / f"{SESSION}.json",
            "an ambient Claude session record": self.home
            / ".claude"
            / "sessions"
            / "4242.json",
            "a profiled Claude session record": self.home
            / "accounts"
            / "claude"
            / "work"
            / "sessions"
            / "4243.json",
            "a Codex rollout": self.home
            / ".codex"
            / "sessions"
            / "2026"
            / "08"
            / "rollout-2026-08-13.jsonl",
            "the fleet stall file": self.home / "fleet" / "stalls.json",
        }
        for label, path in sources.items():
            before = self.digest()
            self.write(path, '{"probe": 1}')
            self.assertNotEqual(
                before, self.digest(), f"{label} did not move the pulse"
            )
            # And a later append to the same file is seen too: a rollout grows,
            # it is not recreated.
            before = self.digest()
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n{}\n")
            self.assertNotEqual(
                before, self.digest(), f"{label} did not move the pulse on append"
            )

    def test_a_quiet_estate_produces_no_line(self) -> None:
        self.write(self.state / "attention" / "claude" / f"{SESSION}.json")
        self.assertEqual(self.digest(), self.digest())

    def test_the_walk_is_bounded_on_a_big_tree(self) -> None:
        rollouts = self.home / ".codex" / "sessions" / "deep"
        rollouts.mkdir(parents=True)
        for index in range(60):
            (rollouts / f"rollout-{index}.jsonl").write_text("{}", encoding="utf-8")
        paths = pulse.watched_paths(self.environ, self.home)
        self.assertLessEqual(len(paths), pulse.MAX_NODES)
        self.assertGreater(len(paths), 10)

    def test_a_missing_home_is_not_an_error(self) -> None:
        empty = Path(self.temporary.name) / "nothing-here"
        self.assertIsInstance(pulse.fingerprint({}, empty), str)


class MergeTests(unittest.TestCase):
    """Which of the two sources decides, and what happens when one is absent."""

    def hook_record(self, *, needs_you: bool, at: int) -> dict:
        return {
            "session_id": SESSION,
            "hook_event": "Notification",
            "notification_type": "idle_prompt" if needs_you else "agent_completed",
            "needs_you": needs_you,
            "message": "",
            "recorded_at_ms": at,
        }

    def test_a_question_asked_after_the_last_poll_still_reaches_the_picker(
        self,
    ) -> None:
        decided = attention.merge(
            poll_status="working",
            poll_needs_you=False,
            poll_stamp_ms=1_000,
            record=self.hook_record(needs_you=True, at=2_000),
            source="auto",
        )
        self.assertTrue(decided["needs_you"])
        self.assertEqual("needs your reply", decided["agent_status"])
        self.assertEqual("hook", decided["attention_source"])

    def test_a_newer_poll_outranks_an_older_hook_record(self) -> None:
        decided = attention.merge(
            poll_status="working",
            poll_needs_you=False,
            poll_stamp_ms=9_000,
            record=self.hook_record(needs_you=True, at=1_000),
            source="auto",
        )
        self.assertFalse(decided["needs_you"])
        self.assertEqual("poll", decided["attention_source"])

    def test_without_a_poll_stamp_only_a_raised_hand_outranks_the_poll(self) -> None:
        raised = attention.merge(
            poll_status="working",
            poll_needs_you=False,
            poll_stamp_ms=None,
            record=self.hook_record(needs_you=True, at=1_000),
            source="auto",
        )
        self.assertTrue(raised["needs_you"])
        lowered = attention.merge(
            poll_status="needs your reply",
            poll_needs_you=True,
            poll_stamp_ms=None,
            record=self.hook_record(needs_you=False, at=1_000),
            source="auto",
        )
        self.assertTrue(
            lowered["needs_you"],
            "an undated comparison must never drop a waiting session",
        )

    def test_the_kill_switch_restores_the_poll_only_reading(self) -> None:
        decided = attention.merge(
            poll_status="working",
            poll_needs_you=False,
            poll_stamp_ms=1_000,
            record=self.hook_record(needs_you=True, at=2_000),
            source="poll",
        )
        self.assertFalse(decided["needs_you"])
        self.assertEqual("poll", decided["attention_source"])

    def test_no_hook_record_is_exactly_the_old_behaviour(self) -> None:
        for needs_you, status in ((True, "needs your reply"), (False, "working")):
            decided = attention.merge(
                poll_status=status,
                poll_needs_you=needs_you,
                poll_stamp_ms=1_000,
                record=None,
                source="auto",
            )
            self.assertEqual(needs_you, decided["needs_you"])
            self.assertEqual(status, decided["agent_status"])
            self.assertEqual("poll", decided["attention_source"])

    def test_a_record_that_is_not_exactly_what_the_hook_writes_is_ignored(
        self,
    ) -> None:
        """Corruption falls back to the poll, like unreadable JSON already did.

        Both coercions lie: `bool("false")` is True, so a needs_you that
        arrived as a STRING would raise a hand nobody raised, and
        `isinstance(True, int)` is True, so a recorded_at_ms of `true` reads as
        the timestamp 1 and loses every comparison it is in.
        """
        for damage in (
            {"needs_you": "false"},
            {"needs_you": 1},
            {"recorded_at_ms": True},
            {"recorded_at_ms": "9999"},
        ):
            with tempfile.TemporaryDirectory(prefix="attention-") as temporary:
                state = Path(temporary)
                attention.write_record(
                    state,
                    session_id=SESSION,
                    hook_event="Notification",
                    notification_type="idle_prompt",
                    needs_you=True,
                    recorded_at_ms=5,
                )
                path = attention.record_path(state, SESSION)
                record = json.loads(path.read_text(encoding="utf-8"))
                record.update(damage)
                path.write_text(json.dumps(record), encoding="utf-8")
                self.assertIsNone(
                    attention.read_record(state, SESSION),
                    f"{damage} was accepted as evidence",
                )

    def test_the_hook_implements_exactly_the_clearing_contract(self) -> None:
        source = HOOK.read_text(encoding="utf-8")
        for event in attention.CLEARING_EVENTS:
            self.assertIn(event, source, f"the hook handles no {event}")
        for name in attention.NEEDS_YOU_NOTIFICATIONS | attention.CLEARING_NOTIFICATIONS:
            self.assertIn(name, source, f"the hook classifies no {name}")

    def test_a_record_from_another_session_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="attention-") as temporary:
            state = Path(temporary)
            attention.write_record(
                state,
                session_id=SESSION,
                hook_event="Notification",
                notification_type="idle_prompt",
                needs_you=True,
                recorded_at_ms=5,
            )
            path = attention.record_path(state, SESSION)
            record = json.loads(path.read_text(encoding="utf-8"))
            record["session_id"] = "11111111-2222-4333-8444-555555555555"
            path.write_text(json.dumps(record), encoding="utf-8")
            self.assertIsNone(attention.read_record(state, SESSION))


class PollIsStillCollectedTests(unittest.TestCase):
    def test_the_hook_never_replaces_the_poll_for_sessions_it_never_saw(self) -> None:
        """A record without a live row is nobody's evidence."""
        with tempfile.TemporaryDirectory(prefix="attention-") as temporary:
            state = Path(temporary)
            attention.write_record(
                state,
                session_id=SESSION,
                hook_event="Notification",
                notification_type="idle_prompt",
                needs_you=True,
                recorded_at_ms=5,
            )
            self.assertEqual({}, attention.read_all(state, []))
            self.assertEqual([SESSION], list(attention.read_all(state, [SESSION])))


if __name__ == "__main__":
    unittest.main()
