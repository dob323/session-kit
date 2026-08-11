"""The acceptance instrument: it must count honestly and change nothing."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor.authority_doctor import (  # noqa: E402
    ACCEPTANCE_SHARE,
    authority_report,
    main,
    render,
)
from sessionkit_supervisor.intake import Spool, produce  # noqa: E402
from sessionkit_supervisor.source_authority import (  # noqa: E402
    capture_hook_event,
    prompt_sha256,
)

UUID = "b7a50706-aaff-46ff-a92f-9b101431fa74"
UNREACHED = "4dfd9855-79f0-44fa-980b-c7382412cd5c"


class AuthorityDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".authority-doctor-", dir=REPO
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
        self.spool = Spool(self.state)

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

    def unreachable(self, prompt: str) -> str:
        with self.environment():
            event = capture_hook_event(
                {"provider": "claude", "session_id": UNREACHED, "prompt": prompt},
                state_dir=self.state,
            )
        return str(event["event_id"])

    def opened(self, prompt: str, event_id: str, *, session: str = UUID) -> dict:
        return produce(
            self.spool,
            thread_key=f"claude:{session}",
            prompt=prompt,
            source_event_id=event_id,
            source_digest=prompt_sha256(prompt),
        )

    def report(self, **extra: object) -> dict:
        with self.environment():
            return authority_report(self.state, **extra)  # type: ignore[arg-type]

    def test_a_corroborated_project_counts_as_acceptance(self) -> None:
        prompt = "audit the entire session kit and report what is broken"
        self.opened(prompt, self.certified(prompt))
        report = self.report()
        self.assertEqual(1, report["events"]["total"])
        self.assertEqual(1, report["events"]["by_tier"]["transcript"])
        self.assertEqual(1, report["operator_intakes"]["in_window"])
        self.assertEqual(1, report["operator_intakes"]["at_required_tier"])
        self.assertEqual(1.0, report["operator_intakes"]["share"])
        self.assertTrue(report["acceptance"]["met"])
        self.assertEqual(ACCEPTANCE_SHARE, report["acceptance"]["target_share"])

    def test_an_unprovable_project_is_counted_and_named(self) -> None:
        prompt = "please look at the rotation code and tell me what it does"
        self.opened(prompt, self.unreachable(prompt), session=UNREACHED)
        report = self.report()
        self.assertEqual(0, report["operator_intakes"]["at_required_tier"])
        self.assertEqual(1, report["operator_intakes"]["in_window"])
        self.assertFalse(report["acceptance"]["met"])
        self.assertEqual(
            {"generation-unverified": 1}, report["operator_intakes"]["refusals"]
        )
        threads = [
            row["thread"] for row in report["sessions_without_recorded_transcript"]
        ]
        self.assertIn(f"claude:{UNREACHED}", threads)

    def test_evidence_the_event_never_recorded_is_reported_as_findable(self) -> None:
        """The measured defect, stated as a number a person can act on."""
        prompt = "please look at the rotation code and tell me what it does"
        self.unreachable(prompt)
        project = self.accounts / "claude" / "primary" / "projects" / "-a-project"
        project.mkdir(mode=0o700, parents=True)
        found = project / f"{UNREACHED}.jsonl"
        found.write_text("{}\n", encoding="utf-8")
        found.chmod(0o600)
        report = self.report()
        rows = {
            row["thread"]: row["discoverable"]
            for row in report["sessions_without_recorded_transcript"]
        }
        self.assertEqual(os.fspath(found), rows[f"claude:{UNREACHED}"])
        self.assertIn("1 of 1 sessions", render(report))

    def test_the_kits_own_transport_is_not_counted_as_an_operator_project(self) -> None:
        prompt = "audit the entire session kit and report what is broken"
        self.opened(prompt, self.certified(prompt))
        entries = self.state / "supervisor" / "intake" / "entries"
        name = sorted(os.listdir(entries))[0]
        entry = json.loads((entries / name).read_text(encoding="utf-8"))
        entry["summary"] = "You are a Session Kit delivery runner. Deliver this."
        (entries / name).write_text(json.dumps(entry), encoding="utf-8")
        report = self.report()
        self.assertEqual(0, report["operator_intakes"]["in_window"])

    def test_a_project_older_than_the_window_is_out_of_the_window(self) -> None:
        prompt = "audit the entire session kit and report what is broken"
        opened = self.opened(prompt, self.certified(prompt))
        entries = self.state / "supervisor" / "intake" / "entries"
        name = f"{opened['entry']['msg_id']}.json"
        entry = json.loads((entries / name).read_text(encoding="utf-8"))
        entry["received_unix_ms"] = int(time.time() * 1000) - 30 * 86_400_000
        (entries / name).write_text(json.dumps(entry), encoding="utf-8")
        self.assertEqual(0, self.report()["operator_intakes"]["in_window"])
        self.assertEqual(1, self.report(window_days=60)["operator_intakes"]["in_window"])

    def test_a_named_session_is_reported_on_its_own(self) -> None:
        prompt = "audit the entire session kit and report what is broken"
        self.opened(prompt, self.certified(prompt))
        report = self.report(thread=f"claude:{UUID}")
        self.assertEqual(f"claude:{UUID}", report["named_thread"]["thread"])
        self.assertEqual(3, report["named_thread"]["event"]["tier"])
        self.assertEqual(1, len(report["named_thread"]["intakes"]))
        self.assertTrue(report["named_thread"]["intakes"][0]["allowed"])
        self.assertIn("newest event tier 3", render(report))

    def test_the_report_records_nothing(self) -> None:
        prompt = "audit the entire session kit and report what is broken"
        self.opened(prompt, self.certified(prompt))
        receipts = self.state / "supervisor" / "source-events" / "verifications"
        before = sorted(os.listdir(receipts)) if receipts.is_dir() else []
        self.report()
        after = sorted(os.listdir(receipts)) if receipts.is_dir() else []
        self.assertEqual(before, after)

    def test_the_command_prints_both_shapes(self) -> None:
        prompt = "audit the entire session kit and report what is broken"
        self.opened(prompt, self.certified(prompt))
        for arguments, check in (
            ([], lambda text: "source authority\t" in text),
            (["--json"], lambda text: json.loads(text)["events"]["total"] == 1),
        ):
            stream = io.StringIO()
            with self.environment(), mock.patch.object(sys, "stdout", stream):
                status = main(["--state-dir", os.fspath(self.state), *arguments])
            self.assertEqual(0, status)
            self.assertTrue(check(stream.getvalue()))

    def test_an_empty_machine_reports_zero_without_claiming_acceptance(self) -> None:
        report = self.report()
        self.assertEqual(0, report["events"]["total"])
        self.assertEqual(0.0, report["operator_intakes"]["share"])
        self.assertFalse(report["acceptance"]["met"])


if __name__ == "__main__":
    unittest.main()
