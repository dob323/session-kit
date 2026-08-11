"""Needs You handling for prompt handoffs with no inventory row."""

from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tests.support import REPO

import sys

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor import prompt_quarantine  # noqa: E402


SESSION = "00000000-0000-4000-8000-000000000007"
TURN = "11111111-1111-4111-8111-111111111111"


class PromptQuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".prompt-needs-you-", dir=REPO.parent)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.base = self.root / "managed-42.prompt"
        self.prompt = b"durable intake recovery\n"
        self.pending = Path(f"{self.base}.intake_pending")
        self.pending.write_bytes(self.prompt)
        self.pending.chmod(0o600)
        self.acceptance = Path(f"{self.base}.accepted")
        self.write_json(
            self.acceptance,
            {
                "bytes": len(self.prompt),
                "schema_version": 1,
                "session_id": SESSION,
                "sha256": hashlib.sha256(self.prompt).hexdigest(),
                "status": "accepted",
                "turn_id": TURN,
            },
        )
        self.completion = Path(f"{self.base}.completed")
        self.write_json(
            self.completion,
            {
                "exit_code": 0,
                "managed_generation": "boot:101:202:303",
                "schema_version": 3,
                "sha256": hashlib.sha256(self.prompt).hexdigest(),
                "status": "intake_pending",
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def test_list_surfaces_title_age_and_needs_you_without_session_uuid(self) -> None:
        key = prompt_quarantine._key(self.pending)
        output = io.StringIO()
        with redirect_stdout(output):
            prompt_quarantine.list_items(self.root)
        visible = output.getvalue()
        self.assertIn("Needs You", visible)
        self.assertIn("Codex prompt intake pending", visible)
        self.assertIn("old", visible)
        self.assertNotIn(SESSION, visible)
        self.assertNotIn("managed-42", visible)
        self.assertNotIn(key, visible)

    def test_json_list_keeps_selection_key_in_machine_channel(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            prompt_quarantine.list_items(self.root, json_mode=True)
        record = json.loads(output.getvalue())
        self.assertEqual(1, record["schema_version"])
        self.assertEqual(prompt_quarantine._key(self.pending), record["items"][0]["key"])
        self.assertEqual("Codex prompt intake pending", record["items"][0]["title"])

    def test_discard_is_recoverable_and_retained_private(self) -> None:
        key = prompt_quarantine._key(self.pending)
        prompt_quarantine.discard(self.root, key)
        self.assertFalse(self.pending.exists())
        retained = list((self.root / "prompt-quarantine-retained").glob("*.discarded"))
        self.assertEqual(1, len(retained))
        self.assertEqual(self.prompt, retained[0].read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(retained[0].stat().st_mode))

    def test_ingest_calls_supervisor_only_and_requires_exact_commit(self) -> None:
        key = prompt_quarantine._key(self.pending)

        def fake_run(command, *, input, env, timeout, check, stdout, stderr):  # type: ignore[no-untyped-def]
            self.assertIn("sk_codex_intake.py", command[1])
            self.assertNotIn("codex", Path(command[0]).name)
            payload = json.loads(input)
            self.assertEqual(self.prompt.decode(), payload["prompt"])
            self.assertEqual("boot:101:202:303", env["SESSION_KIT_MANAGED_GENERATION"])
            self.assertEqual(
                hashlib.sha256(self.prompt).hexdigest(),
                env["SESSION_KIT_SOURCE_ACCEPTANCE_DIGEST"],
            )
            self.write_json(
                Path(f"{self.base}.intake_committed"),
                {
                    "schema_version": 2,
                    "status": "intake_committed",
                    "session_id": SESSION,
                    "submission_key": TURN,
                    "prompt_sha256": hashlib.sha256(self.prompt).hexdigest(),
                    "source_event_id": "1" * 64,
                },
            )
            return mock.Mock(returncode=0)

        with mock.patch.object(prompt_quarantine.subprocess, "run", side_effect=fake_run):
            prompt_quarantine.ingest(self.root, key)
        self.assertFalse(self.pending.exists())
        retained = list((self.root / "prompt-quarantine-retained").glob("*.ingested"))
        self.assertEqual(self.prompt, retained[0].read_bytes())

    def test_resume_captures_created_session_id_and_archives_prompt(self) -> None:
        key = prompt_quarantine._key(self.pending)
        created = "s20260809-222222-9"

        def fake_run(command, *, check, text, stdout, stderr, timeout):  # type: ignore[no-untyped-def]
            self.assertEqual("restore-exact", command[1])
            self.assertEqual(SESSION, command[3])
            return mock.Mock(returncode=0, stdout=created + "\n", stderr="")

        output = io.StringIO()
        with mock.patch.object(prompt_quarantine.subprocess, "run", side_effect=fake_run):
            with redirect_stdout(output):
                prompt_quarantine.resume(self.root, key, self.root)
        visible = output.getvalue()
        self.assertIn("Exact Codex conversation was resumed", visible)
        self.assertNotIn(created, visible)
        self.assertNotIn(SESSION, visible)
        self.assertFalse(self.pending.exists())
        self.assertEqual(
            1,
            len(list((self.root / "prompt-quarantine-retained").glob("*.resumed"))),
        )

    def test_linked_root_and_retention_directory_cannot_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".prompt-outside-", dir=REPO.parent) as raw:
            outside = Path(raw)
            outside.chmod(0o700)
            outside_prompt = outside / self.pending.name
            outside_prompt.write_bytes(b"outside must survive\n")
            outside_prompt.chmod(0o600)
            linked_root = self.root.parent / f"{self.root.name}-linked"
            linked_root.symlink_to(outside, target_is_directory=True)
            try:
                with self.assertRaises(OSError):
                    prompt_quarantine.list_items(linked_root)
                self.assertEqual(b"outside must survive\n", outside_prompt.read_bytes())
            finally:
                linked_root.unlink()

            retained_link = self.root / "prompt-quarantine-retained"
            retained_link.symlink_to(outside, target_is_directory=True)
            key = prompt_quarantine._key(self.pending)
            with self.assertRaises(OSError):
                prompt_quarantine.discard(self.root, key)
            self.assertEqual(self.prompt, self.pending.read_bytes())
            self.assertEqual(b"outside must survive\n", outside_prompt.read_bytes())


if __name__ == "__main__":
    unittest.main()
