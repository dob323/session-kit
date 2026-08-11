"""The action/tier policy, the gate interface that reads it, and what still gates.

`authority_for_intake` is the one call a delegation gate is meant to make. It
is built and proved here, and deliberately wired to nothing: `delegate()` and
`preflight()` keep exactly the rule they have today until the policy behind
this table is decided. The last test in this file is what holds that line.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor import intake as intake_module  # noqa: E402
from sessionkit_supervisor.intake import Spool, produce  # noqa: E402
from sessionkit_supervisor.source_authority import (  # noqa: E402
    AUTHORITY_ACTIONS,
    AUTHORITY_POLICY_ENV,
    TIER_TRANSCRIPT,
    TIER_UNVERIFIED,
    SourceEventStore,
    authority_for_intake,
    authority_policy,
    capture_hook_event,
    required_tier,
)

UUID = "b7a50706-aaff-46ff-a92f-9b101431fa74"
# A session that never gets a transcript anywhere the kit looks. Reusing
# the corroborated session would let the discovery walk find its file and
# quietly upgrade the event these tests need to stay unverifiable.
UNREACHED = "4dfd9855-79f0-44fa-980b-c7382412cd5c"
SHIPPED = REPO / "config" / "authority_policy.json"


class PolicyFileTests(unittest.TestCase):
    """A policy nobody can read must be the strictest policy there is."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".authority-policy-", dir=REPO)
        self.base = Path(self.temporary.name)
        self.path = self.base / "policy.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def using(self, value: object = None, *, raw: str = "") -> mock._patch_dict:
        if raw:
            self.path.write_text(raw, encoding="utf-8")
        elif value is not None:
            self.path.write_text(json.dumps(value), encoding="utf-8")
        return mock.patch.dict(
            os.environ,
            {
                "SESSION_KIT_TESTING": "1",
                AUTHORITY_POLICY_ENV: os.fspath(self.path),
            },
            clear=False,
        )

    def test_the_shipped_table_is_the_designed_table(self) -> None:
        self.assertTrue(SHIPPED.is_file())
        policy = authority_policy()
        self.assertEqual(os.fspath(SHIPPED), policy["source"])
        self.assertEqual(2, required_tier("preflight"))
        self.assertEqual(2, required_tier("delegate"))
        self.assertEqual(3, required_tier("send_lane3"))
        self.assertEqual(3, required_tier("broadcast"))
        self.assertEqual(3, required_tier("raise_budget"))
        for action in AUTHORITY_ACTIONS:
            self.assertIn(action, policy["actions"])

    def test_an_action_nobody_wrote_down_needs_the_most(self) -> None:
        self.assertEqual(TIER_TRANSCRIPT, required_tier("delete_everything"))
        self.assertEqual(TIER_TRANSCRIPT, required_tier(None))

    def test_a_missing_policy_file_locks_everything_to_the_top(self) -> None:
        with self.using():
            self.assertEqual(TIER_TRANSCRIPT, required_tier("delegate"))
            self.assertEqual({}, authority_policy()["actions"])

    def test_malformed_json_locks_everything_to_the_top(self) -> None:
        with self.using(raw="{not json"):
            self.assertEqual(TIER_TRANSCRIPT, required_tier("delegate"))

    def test_a_policy_anyone_could_rewrite_is_not_a_policy(self) -> None:
        with self.using({"actions": {"delegate": {"required_tier": 0}}}):
            self.path.chmod(0o666)
            self.assertEqual(TIER_TRANSCRIPT, required_tier("delegate"))
            self.path.chmod(0o644)
            self.assertEqual(TIER_UNVERIFIED, required_tier("delegate"))

    def test_a_tier_outside_the_ladder_falls_back_to_the_default(self) -> None:
        with self.using(
            {
                "default_required_tier": 2,
                "actions": {"delegate": {"required_tier": 9}},
            }
        ):
            self.assertEqual(2, required_tier("delegate"))

    def test_the_override_is_a_test_seam_only(self) -> None:
        self.path.write_text(json.dumps({"default_required_tier": 0}), encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {AUTHORITY_POLICY_ENV: os.fspath(self.path), "SESSION_KIT_TESTING": "0"},
            clear=False,
        ):
            self.assertEqual(os.fspath(SHIPPED), authority_policy()["source"])


class AuthorityForIntakeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".authority-intake-", dir=REPO
        )
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.transcripts = self.state / "test-transcripts"
        self.transcripts.mkdir(mode=0o700)
        self.home = self.base / "home"
        (self.home / ".claude" / "projects").mkdir(mode=0o700, parents=True)
        self.accounts = self.base / "accounts"
        self.accounts.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def environment(self) -> mock._patch_dict:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        return mock.patch.dict(
            os.environ,
            {
                "HOME": os.fspath(self.home),
                "SESSION_KIT_ACCOUNT_ROOT": os.fspath(self.accounts),
            },
            clear=False,
        )

    def certified(self, prompt: str, *, session: str = UUID) -> str:
        """One event the provider's own transcript corroborates: tier 3."""
        path = self.transcripts / f"{session}.jsonl"
        if not path.exists():
            path.write_text(
                json.dumps({"type": "summary", "summary": "earlier"}) + "\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
        with self.environment():
            event = capture_hook_event(
                {
                    "provider": "claude",
                    "session_id": session,
                    "prompt": prompt,
                    "transcript_path": os.fspath(path),
                },
                state_dir=self.state,
            )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "sessionId": session,
                        "type": "user",
                        "message": {"role": "user", "content": prompt},
                    }
                )
                + "\n"
            )
        return str(event["event_id"])

    def uncertified(self, prompt: str, *, session: str = UNREACHED) -> str:
        """One event with no reachable provider evidence at all: tier 0."""
        with self.environment():
            event = capture_hook_event(
                {"provider": "claude", "session_id": session, "prompt": prompt},
                state_dir=self.state,
            )
        return str(event["event_id"])

    def lagging(self, prompt: str, *, session: str = UUID) -> str:
        """One event whose transcript exists but has not caught up: tier 1."""
        path = self.transcripts / f"{session}.jsonl"
        if not path.exists():
            path.write_text(
                json.dumps({"type": "summary", "summary": "earlier"}) + "\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
        with self.environment():
            event = capture_hook_event(
                {
                    "provider": "claude",
                    "session_id": session,
                    "prompt": prompt,
                    "transcript_path": os.fspath(path),
                },
                state_dir=self.state,
            )
        return str(event["event_id"])

    def entry(self, primary: str, *amendments: str) -> dict:
        return {
            "msg_id": "abcd1234",
            "origin": "auto",
            "source_thread_key": f"claude:{UUID}",
            "source_event_id": primary,
            "summary": "a project",
            "amendments": [{"source_event_id": value} for value in amendments],
        }

    def verdict(self, entry: dict, action: str = "delegate") -> dict:
        with self.environment():
            return authority_for_intake(self.state, entry, action=action)


class AuthorityForIntakeTests(AuthorityForIntakeCase):
    def test_a_corroborated_chain_meets_the_delegate_bar(self) -> None:
        first = self.certified("open the project and audit the whole kit")
        second = self.certified("also check the account rotation path")
        result = self.verdict(self.entry(first, second))
        self.assertTrue(result["allowed"])
        self.assertEqual("delegate", result["action"])
        self.assertEqual(2, result["required_tier"])
        self.assertEqual(1, result["current_generation"])
        self.assertEqual(TIER_TRANSCRIPT, result["min_tier_in_chain"])
        self.assertNotIn("refusal", result)
        self.assertEqual([0, 1], [row["generation"] for row in result["chain"]])
        self.assertEqual([first, second], [row["event_id"] for row in result["chain"]])
        for row in result["chain"]:
            self.assertEqual("transcript", row["basis"])
            self.assertEqual(64, len(row["prompt_sha256"]))

    def test_a_harness_envelope_in_the_chain_is_named_as_such(self) -> None:
        """The already-live bricking case: 5 intakes carry one of these."""
        first = self.certified("open the project and audit the whole kit")
        envelope = self.uncertified("<task-notification> a worker finished its task")
        result = self.verdict(self.entry(first, envelope))
        self.assertFalse(result["allowed"])
        self.assertEqual("envelope-in-chain", result["refusal"]["code"])
        self.assertEqual(envelope, result["refusal"]["event_id"])
        self.assertIn("harness envelope", result["refusal"]["message"])
        self.assertIn(f"claude:{UUID}", result["refusal"]["remedy"])

    def test_an_unverifiable_current_generation_is_named_as_such(self) -> None:
        first = self.certified("open the project and audit the whole kit")
        latest = self.uncertified("and now change the plan in this newer way")
        result = self.verdict(self.entry(first, latest))
        self.assertFalse(result["allowed"])
        self.assertEqual("generation-unverified", result["refusal"]["code"])
        self.assertEqual(latest, result["refusal"]["event_id"])

    def test_an_unverifiable_older_requirement_breaks_the_chain(self) -> None:
        first = self.uncertified("open the project and audit the whole kit")
        latest = self.certified("and now change the plan in this newer way")
        result = self.verdict(self.entry(first, latest))
        self.assertFalse(result["allowed"])
        self.assertEqual("chain-broken", result["refusal"]["code"])
        self.assertEqual(first, result["refusal"]["event_id"])

    def test_evidence_below_the_bar_says_which_bar_and_which_evidence(self) -> None:
        lagging = self.lagging("open the project and audit the whole kit")
        result = self.verdict(self.entry(lagging))
        self.assertFalse(result["allowed"])
        self.assertEqual("tier-too-low", result["refusal"]["code"])
        self.assertEqual(2, result["required_tier"])
        self.assertEqual(1, result["min_tier_in_chain"])
        self.assertIn("needs tier 2 (hook-ledger)", result["refusal"]["message"])
        self.assertIn("ledger-only-lag", result["refusal"]["message"])
        self.assertIn(f"{UUID}.jsonl", result["refusal"]["message"])

    def test_a_stronger_action_can_refuse_what_delegate_allows(self) -> None:
        first = self.certified("open the project and audit the whole kit")
        self.assertTrue(self.verdict(self.entry(first), action="delegate")["allowed"])
        self.assertTrue(
            self.verdict(self.entry(first), action="send_lane3")["allowed"]
        )
        lagging = self.lagging("a second thing, recorded before the file caught up")
        weak = self.entry(first, lagging)
        self.assertFalse(self.verdict(weak, action="delegate")["allowed"])
        self.assertEqual(
            3, self.verdict(weak, action="send_lane3")["required_tier"]
        )

    def test_an_intake_with_no_source_event_is_refused(self) -> None:
        entry = self.entry("")
        entry["source_event_id"] = None
        result = self.verdict(entry)
        self.assertFalse(result["allowed"])
        self.assertEqual("chain-broken", result["refusal"]["code"])
        self.assertEqual([], result["chain"])

    def test_an_event_missing_from_its_own_ledger_is_a_ledger_fault(self) -> None:
        first = self.certified("open the project and audit the whole kit")
        store = SourceEventStore(self.state)
        store.event_path(first).unlink()
        result = self.verdict(self.entry(first))
        self.assertFalse(result["allowed"])
        self.assertEqual("ledger-fault", result["refusal"]["code"])
        self.assertEqual(first, result["refusal"]["event_id"])

    def test_asking_the_question_records_nothing(self) -> None:
        first = self.certified("open the project and audit the whole kit")
        receipts = self.state / "supervisor" / "source-events" / "verifications"
        before = sorted(os.listdir(receipts)) if receipts.is_dir() else []
        self.verdict(self.entry(first))
        after = sorted(os.listdir(receipts)) if receipts.is_dir() else []
        self.assertEqual(before, after)
        self.assertFalse(any(name.startswith(first) for name in after))


class EnvelopeIsNotARequirementTests(AuthorityForIntakeCase):
    """A machine envelope must never become a requirement of an open project."""

    def digest(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_an_envelope_never_lands_as_an_amendment(self) -> None:
        spool = Spool(self.state)
        opening = "please audit the whole session kit and tell me what is broken"
        first = produce(
            spool,
            thread_key=f"claude:{UUID}",
            prompt=opening,
            source_event_id=self.certified(opening),
            source_digest=self.digest(opening),
        )
        self.assertEqual("created", first["action"])
        envelope = (
            "<task-notification>agent p2 finished its work and reported back"
            "</task-notification>"
        )
        blocked = produce(
            spool,
            thread_key=f"claude:{UUID}",
            prompt=envelope,
            source_event_id=self.uncertified(envelope),
            source_digest=self.digest(envelope),
        )
        self.assertEqual("refused", blocked["action"])
        self.assertIn("harness envelope", str(blocked["reason"]))
        entry = spool.read_entry(first["entry"]["msg_id"])
        assert entry is not None
        self.assertEqual([], entry["amendments"])

    def test_a_persons_own_amendment_still_lands(self) -> None:
        spool = Spool(self.state)
        opening = "please audit the whole session kit and tell me what is broken"
        first = produce(
            spool,
            thread_key=f"claude:{UUID}",
            prompt=opening,
            source_event_id=self.certified(opening),
            source_digest=self.digest(opening),
        )
        follow = "also check whether account rotation hides the transcripts"
        added = produce(
            spool,
            thread_key=f"claude:{UUID}",
            prompt=follow,
            source_event_id=self.certified(follow),
            source_digest=self.digest(follow),
        )
        self.assertEqual("amended", added["action"])
        entry = spool.read_entry(first["entry"]["msg_id"])
        assert entry is not None
        self.assertEqual(1, len(entry["amendments"]))


class NothingGatesYetTests(unittest.TestCase):
    """The line the maintainer drew: build the gate interface, arm nothing.

    `delegate()` and `preflight()` keep the rule they have today until the
    policy questions behind the tier table are answered. This test fails the
    moment the new interface is wired into either of them, which is exactly
    when someone should be reading the answers instead of this file.
    """

    def test_no_enforcing_call_site_exists_yet(self) -> None:
        for name in ("intake", "adapter", "ratchet", "server"):
            source = (REPO / "lib" / "sessionkit_supervisor" / f"{name}.py").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(
                "authority_for_intake",
                source,
                f"{name}.py calls the delegation gate; the tier policy gates that",
            )

    def test_delegate_still_reverifies_every_recorded_event(self) -> None:
        source = (REPO / "lib" / "sessionkit_supervisor" / "intake.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("delegate source-event reverification failed", source)
        self.assertTrue(hasattr(intake_module, "delegate"))


if __name__ == "__main__":
    unittest.main()
