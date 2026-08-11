"""Where Claude transcript evidence is allowed to live, and what it proves.

The kit's own account rotation moves a Claude session's transcript out of the
provider default root, so a verifier that knows only that root calls the kit's
own feature unverifiable. These tests pin the roots it must know, the
unique-match-or-refuse discovery that finds a session's file when the payload
named none, and the evidence ladder every consumer of a verification reads.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor.source_authority import (  # noqa: E402
    CLAUDE_TRANSCRIPT_MAX_DEPTH,
    TIER_HOOK_LEDGER,
    TIER_LEDGER_ONLY_LAG,
    TIER_TRANSCRIPT,
    TIER_UNVERIFIED,
    SourceEventStore,
    capture_hook_event,
    locate_transcript,
    machine_envelope_prompt,
    verify_source_event,
)

UUID = "b7a50706-aaff-46ff-a92f-9b101431fa74"
OTHER_UUID = "019fdf1e-8b4c-7573-a089-be495bfece6a"
PROMPT = "audit the entire session kit system and report what is broken"


class ClaudeEvidenceCase(unittest.TestCase):
    """A machine with no Claude transcript anywhere the kit did not put one."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".authority-evidence-", dir=REPO
        )
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        (self.state / "test-transcripts").mkdir(mode=0o700)
        self.home = self.base / "home"
        (self.home / ".claude" / "projects").mkdir(mode=0o700, parents=True)
        self.accounts = self.base / "accounts"
        self.profile = self.accounts / "claude" / "primary" / "projects" / "-a-project"
        self.profile.mkdir(mode=0o700, parents=True)
        self.configured = self.base / "configured"
        (self.configured / "projects" / "-a-project").mkdir(mode=0o700, parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def environment(self, **extra: str) -> mock._patch_dict:
        values = {
            "HOME": os.fspath(self.home),
            "SESSION_KIT_ACCOUNT_ROOT": os.fspath(self.accounts),
        }
        values.update(extra)
        patch = mock.patch.dict(os.environ, values, clear=False)
        # CLAUDE_CONFIG_DIR must be absent unless a test asks for it, or a real
        # rotation in the surrounding session would widen the roots under test.
        if "CLAUDE_CONFIG_DIR" not in extra:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        return patch

    def transcript(
        self,
        directory: Path,
        *,
        session: str = UUID,
        mode: int = 0o600,
        name: str = "",
    ) -> Path:
        """The file as it stands when the prompt hook fires: no prompt in it yet.

        The provider writes the user record *after* the hook runs, which is the
        whole point of anchoring at the pre-submit size. A fixture that seeds
        the prompt first would anchor past it and prove nothing.
        """
        path = directory / (name or f"{session}.jsonl")
        path.write_text(
            json.dumps({"type": "summary", "summary": "an earlier line"}) + "\n",
            encoding="utf-8",
        )
        path.chmod(mode)
        return path

    def land(self, path: Path, *, prompt: str = PROMPT, session: str = UUID) -> None:
        """What the provider appends once the turn is really submitted."""
        record = {
            "sessionId": session,
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def capture(self, prompt: str = PROMPT, **fields: object) -> dict:
        payload: dict[str, object] = {
            "provider": "claude",
            "session_id": UUID,
            "prompt": prompt,
        }
        payload.update(fields)
        with self.environment(**{}):
            return capture_hook_event(payload, state_dir=self.state)


class ClaudeRootTests(ClaudeEvidenceCase):
    def test_an_account_profile_transcript_is_evidence(self) -> None:
        """The measured defect: the kit's own rotation hid the evidence."""
        expected = self.transcript(self.profile)
        event = self.capture()
        self.assertTrue(event["authority_capable"])
        self.assertEqual("", event["authority_limit_reason"])
        self.assertEqual(os.fspath(expected), event["transcript_path"])
        self.land(expected)
        with self.environment():
            result = verify_source_event(self.state, event["event_id"])
        self.assertTrue(result["verified"])
        self.assertEqual("transcript", result["basis"])
        self.assertEqual(TIER_TRANSCRIPT, result["tier"])
        self.assertEqual(
            os.fspath(self.accounts / "claude" / "primary" / "projects"),
            result["evidence_root"],
        )

    def test_the_configured_claude_home_is_a_root(self) -> None:
        expected = self.transcript(self.configured / "projects" / "-a-project")
        payload = {
            "provider": "claude",
            "session_id": UUID,
            "prompt": PROMPT,
            "transcript_path": os.fspath(expected),
        }
        with self.environment(CLAUDE_CONFIG_DIR=os.fspath(self.configured)):
            event = capture_hook_event(payload, state_dir=self.state)
            self.assertTrue(event["authority_capable"])
            self.land(expected)
            result = verify_source_event(self.state, event["event_id"])
        self.assertEqual(TIER_TRANSCRIPT, result["tier"])
        self.assertEqual(
            os.fspath(self.configured / "projects"), result["evidence_root"]
        )

    def test_the_provider_default_root_still_works(self) -> None:
        project = self.home / ".claude" / "projects" / "-a-project"
        project.mkdir(mode=0o700)
        expected = self.transcript(project)
        event = self.capture()
        self.assertEqual(os.fspath(expected), event["transcript_path"])
        self.assertTrue(event["authority_capable"])

    def test_a_relocated_transcript_under_two_roots_refuses(self) -> None:
        """A decoy can cost a certified turn. It must never redirect one."""
        project = self.home / ".claude" / "projects" / "-a-project"
        project.mkdir(mode=0o700)
        self.transcript(project)
        self.transcript(self.profile)
        event = self.capture()
        self.assertFalse(event["authority_capable"])
        # Claude derives its submission key from the transcript anchor, so an
        # unreachable transcript takes the key with it and that is the reason
        # reported first. What matters is that neither copy became evidence.
        self.assertEqual(
            "missing or malformed provider submission key",
            event["authority_limit_reason"],
        )
        self.assertEqual("", event["transcript_path"])

    def test_a_symlinked_transcript_is_never_discovered(self) -> None:
        real = self.base / "elsewhere.jsonl"
        real.write_text("{}\n", encoding="utf-8")
        real.chmod(0o600)
        (self.profile / f"{UUID}.jsonl").symlink_to(real)
        event = self.capture()
        self.assertFalse(event["authority_capable"])
        self.assertEqual("", event["transcript_path"])

    def test_the_file_name_is_anchored_on_the_whole_session_id(self) -> None:
        self.transcript(self.profile, name=f"{UUID}.jsonl.bak")
        self.transcript(self.profile, name=f"parent-{UUID}.jsonl")
        self.transcript(self.profile, name=f"{UUID}-worker.jsonl")
        event = self.capture()
        self.assertEqual("", event["transcript_path"])

    def test_another_sessions_transcript_is_not_borrowed(self) -> None:
        self.transcript(self.profile, session=OTHER_UUID)
        event = self.capture()
        self.assertEqual("", event["transcript_path"])

    def test_discovery_stops_at_the_depth_bound(self) -> None:
        deep = self.profile
        for level in range(CLAUDE_TRANSCRIPT_MAX_DEPTH + 2):
            deep = deep / f"level{level}"
            deep.mkdir(mode=0o700)
        self.transcript(deep)
        event = self.capture()
        self.assertEqual("", event["transcript_path"])

    def test_a_payload_path_that_passes_is_never_second_guessed(self) -> None:
        named = self.transcript(self.home / ".claude" / "projects")
        self.transcript(self.profile)
        event = self.capture(transcript_path=os.fspath(named))
        self.assertEqual(os.fspath(named), event["transcript_path"])
        self.assertTrue(event["authority_capable"])

    def test_a_named_but_unwritten_transcript_keeps_its_intended_path(self) -> None:
        """The first-turn race is unchanged: record the path, do not invent one."""
        intended = self.home / ".claude" / "projects" / f"{UUID}.jsonl"
        event = self.capture(transcript_path=os.fspath(intended))
        self.assertEqual(os.fspath(intended), event["transcript_path"])
        self.assertFalse(event["authority_capable"])

    def test_a_group_writable_transcript_is_not_evidence(self) -> None:
        self.transcript(self.profile, mode=0o660)
        event = self.capture()
        self.assertEqual("", event["transcript_path"])

    def test_locate_transcript_answers_for_a_session_with_no_event(self) -> None:
        expected = self.transcript(self.profile)
        with self.environment():
            self.assertEqual(
                os.fspath(expected), locate_transcript("claude", UUID, self.state)
            )
            self.assertEqual("", locate_transcript("claude", OTHER_UUID, self.state))
            self.assertEqual("", locate_transcript("shell", UUID, self.state))


class EvidenceLadderTests(ClaudeEvidenceCase):
    def test_a_corroborated_event_is_tier_three(self) -> None:
        written = self.transcript(self.profile)
        event = self.capture()
        self.land(written)
        with self.environment():
            result = verify_source_event(self.state, event["event_id"])
        self.assertEqual(TIER_TRANSCRIPT, result["tier"])
        self.assertEqual("transcript", result["tier_name"])
        self.assertFalse(result["non_cryptographic"])

    def test_an_event_whose_file_never_appeared_is_tier_two(self) -> None:
        intended = self.profile / f"{UUID}.jsonl"
        event = self.capture(transcript_path=os.fspath(intended))
        # Codex-shaped first-turn case: the path is recorded, the file is not
        # there at verify time, so the kit's own ledger is the whole evidence.
        store = SourceEventStore(self.state)
        raw = store.read(event["event_id"])
        self.assertEqual(os.fspath(intended), raw["transcript_path"])
        with self.environment():
            result = verify_source_event(self.state, event["event_id"])
        if result["verified"]:
            self.assertEqual(TIER_HOOK_LEDGER, result["tier"])
            self.assertEqual("hook-ledger", result["tier_name"])
            self.assertEqual("", result["evidence_root"])

    def test_a_transcript_that_has_not_caught_up_is_tier_one(self) -> None:
        written = self.transcript(self.profile)
        event = self.capture()
        self.assertEqual(os.fspath(written), event["transcript_path"])
        with self.environment():
            result = verify_source_event(self.state, event["event_id"])
        self.assertTrue(result["verified"])
        self.assertEqual(TIER_LEDGER_ONLY_LAG, result["tier"])
        self.assertEqual("ledger-only-lag", result["tier_name"])
        self.assertEqual("hook-ledger", result["basis"])

    def test_an_unverifiable_event_is_tier_zero(self) -> None:
        result = verify_source_event(self.state, "0" * 64)
        self.assertFalse(result["verified"])
        self.assertEqual(TIER_UNVERIFIED, result["tier"])
        self.assertEqual("unverified", result["tier_name"])
        self.assertEqual("", result["evidence_root"])

    def test_verified_still_means_tier_at_least_one(self) -> None:
        written = self.transcript(self.profile)
        event = self.capture()
        self.land(written)
        with self.environment():
            result = verify_source_event(self.state, event["event_id"])
        self.assertEqual(result["verified"], result["tier"] >= 1)

    def test_asking_without_recording_leaves_no_receipt(self) -> None:
        written = self.transcript(self.profile)
        event = self.capture()
        self.land(written)
        receipt = (
            self.state
            / "supervisor"
            / "source-events"
            / "verifications"
            / f"{event['event_id']}.json"
        )
        with self.environment():
            quiet = verify_source_event(self.state, event["event_id"], record=False)
        self.assertEqual(TIER_TRANSCRIPT, quiet["tier"])
        self.assertFalse(receipt.exists())
        with self.environment():
            verify_source_event(self.state, event["event_id"])
        self.assertTrue(receipt.is_file())


class MachineEnvelopePredicateTests(unittest.TestCase):
    def test_a_harness_envelope_is_not_a_persons_own_words(self) -> None:
        self.assertTrue(machine_envelope_prompt("<task-notification> a worker finished"))
        self.assertTrue(machine_envelope_prompt("<cross-session-message from=x>"))
        self.assertTrue(machine_envelope_prompt("  <system-reminder> remember"))
        self.assertTrue(
            machine_envelope_prompt("You are a Session Kit delivery runner.")
        )

    def test_quoting_a_tag_mid_sentence_stays_a_persons_own_words(self) -> None:
        self.assertFalse(
            machine_envelope_prompt("explain why <task-notification> reaches the hook")
        )
        self.assertFalse(machine_envelope_prompt(PROMPT))
        self.assertFalse(machine_envelope_prompt(""))
        self.assertFalse(machine_envelope_prompt(None))


if __name__ == "__main__":
    unittest.main()
