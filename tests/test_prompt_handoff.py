"""Secure, retryable prompt handoff for new managed Codex sessions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest

from tests.support import REPO, run


COMMON = REPO / "bin/session_kit_common"
SESSIONS = REPO / "lib/sh/sp_sessions.sh"
BASHRC = REPO / "bashrc/shpool.bashrc"


class PromptHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".prompt-handoff-", dir=REPO.parent
        )
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.handoff = self.root / "handoff.prompt"
        self.capture = self.root / "capture.bin"
        self.argv = self.root / "argv.json"
        self.calls = self.root / "calls"
        self.observed = self.root / "observed.json"
        self.acceptance = self.handoff.with_suffix(".prompt.accepted")
        self.intake = self.handoff.with_suffix(".prompt.intake_committed")
        self.completion = self.handoff.with_suffix(".prompt.completed")
        self.stub = self.root / "codex"
        self.stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, subprocess, sys, time\n"
            "handoff=pathlib.Path(os.environ['HANDOFF'])\n"
            "accepted=pathlib.Path(os.environ['ACCEPTANCE'])\n"
            "intake=pathlib.Path(os.environ['INTAKE'])\n"
            "observed={\n"
            " 'handoff_exists': handoff.exists(),\n"
            " 'acceptance_exists': accepted.exists(),\n"
            "}\n"
            "capture=pathlib.Path(os.environ['CAPTURE'])\n"
            "prompt=sys.stdin.buffer.read()\n"
            "capture.write_bytes(prompt)\n"
            "pathlib.Path(os.environ['ARGV']).write_text(json.dumps(sys.argv[1:]))\n"
            "calls=pathlib.Path(os.environ['CALLS'])\n"
            "calls.write_text(str(int(calls.read_text())+1) if calls.exists() else '1')\n"
            "if os.environ.get('WRITE_CONFLICT') == '1':\n"
            " accepted.write_text('{\\\"conflict\\\":true}\\n')\n"
            " accepted.chmod(0o600)\n"
            "if os.environ.get('WRITE_ACCEPTANCE') == '1':\n"
            " payload=json.dumps({\n"
            "  'hook_event_name':'UserPromptSubmit',\n"
            "  'session_id':'00000000-0000-4000-8000-000000000007',\n"
            "  'turn_id':'11111111-1111-4111-8111-111111111111',\n"
            "  'prompt':prompt.decode('utf-8'),\n"
            " }).encode()\n"
            " subprocess.run([sys.executable, os.environ['PROVIDER_HOOK'], 'codex-hook'], input=payload, check=True)\n"
            " if os.environ.get('WRITE_INTAKE') == '1' and not intake.exists():\n"
            "  record={\n"
            "   'schema_version':2, 'status':'intake_committed', 'provider':'codex',\n"
            "   'session_id':'00000000-0000-4000-8000-000000000007',\n"
            "   'submission_key':'11111111-1111-4111-8111-111111111111',\n"
            "   'prompt_sha256':__import__('hashlib').sha256(prompt).hexdigest(),\n"
            "   'bytes':len(prompt), 'source_event_id':'1'*64,\n"
            "   'intake_msg_id':'msg-test', 'requirements_revision':0,\n"
            "   'requirements_digest':'2'*64,\n"
            "   'managed_generation':os.environ['SESSION_KIT_MANAGED_GENERATION'],\n"
            "   'committed_unix_ms':1,\n"
            "  }\n"
            "  encoded=(json.dumps(record)+'\\n').encode()\n"
            "  temporary=intake.with_name(f'.{intake.name}.{os.getpid()}')\n"
            "  descriptor=os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)\n"
            "  with os.fdopen(descriptor, 'wb') as stream:\n"
            "   stream.write(encoded)\n"
            "   stream.flush()\n"
            "   os.fsync(stream.fileno())\n"
            "  os.replace(temporary, intake)\n"
            "  parent=os.open(intake.parent, os.O_RDONLY|os.O_DIRECTORY)\n"
            "  try:\n"
            "   os.fsync(parent)\n"
            "  finally:\n"
            "   os.close(parent)\n"
            " if os.environ.get('WAIT_FOR_HANDOFF_REMOVAL') == '1':\n"
            "  intake_deadline=time.monotonic()+5\n"
            "  while not intake.exists() and time.monotonic() < intake_deadline:\n"
            "   time.sleep(0.01)\n"
            "  observed['intake_before_handoff_wait']=intake.exists()\n"
            "  handoff_deadline=time.monotonic()+5\n"
            "  while handoff.exists() and time.monotonic() < handoff_deadline:\n"
            "   time.sleep(0.01)\n"
            " observed['handoff_after_hook_wait']=handoff.exists()\n"
            " observed['acceptance_after_hook']=accepted.exists()\n"
            "pathlib.Path(os.environ['OBSERVED']).write_text(json.dumps(observed))\n"
            "raise SystemExit(int(os.environ.get('DELIVERY_RC', '0')))\n",
            encoding="utf-8",
        )
        self.stub.chmod(0o755)

    def tearDown(self) -> None:
        # The delivery stub runs detached and can still be writing its
        # observation file when the test ends; retry briefly rather than
        # racing a one-shot writer that finishes in milliseconds.
        deadline = time.monotonic() + 5
        while True:
            try:
                self.temporary.cleanup()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def env(
        self, *, rc: int = 0, accept: bool = True, intake: bool = True,
        conflict: bool = False,
        wait_for_handoff_removal: bool = False,
    ) -> dict[str, str]:
        environment = {
            "ARGV": str(self.argv),
            "CALLS": str(self.calls),
            "CAPTURE": str(self.capture),
            "DELIVERY_RC": str(rc),
            "HANDOFF": str(self.handoff),
            "ACCEPTANCE": str(self.acceptance),
            "INTAKE": str(self.intake),
            "OBSERVED": str(self.observed),
            "WAIT_FOR_HANDOFF_REMOVAL": "1" if wait_for_handoff_removal else "0",
            "PROVIDER_HOOK": str(REPO / "lib/sessionkit_supervisor/provider_hooks.py"),
            "SESSION_KIT_STATE_DIR": str(self.state),
            "SESSION_KIT_MANAGED_GENERATION": "boot:101:202:303",
            "WRITE_ACCEPTANCE": "1" if accept else "0",
            "WRITE_INTAKE": "1" if intake and accept else "0",
            "WRITE_CONFLICT": "1" if conflict else "0",
        }
        if accept and not intake:
            environment["SESSION_KIT_TESTING"] = "1"
            environment["SESSION_KIT_TEST_FAILPOINT"] = "codex-intake-after-acceptance"
        return environment

    def write_handoff(self, payload: bytes) -> None:
        self.handoff.write_bytes(payload)
        self.handoff.chmod(0o600)

    def deliver(
        self,
        *,
        rc: int = 0,
        accept: bool = True,
        intake: bool = True,
        conflict: bool = False,
        wait_for_handoff_removal: bool = False,
        check: bool = True,
    ):
        return run(
            [
                "bash",
                "-c",
                'source "$1"; __sk_codex_exec_handoff "$2" "$3" "$4" "$5" "$6" exec -',
                "prompt-delivery-test",
                BASHRC,
                self.handoff,
                self.acceptance,
                self.intake,
                self.completion,
                self.stub,
            ],
            env=self.env(
                rc=rc, accept=accept, intake=intake, conflict=conflict,
                wait_for_handoff_removal=wait_for_handoff_removal,
            ),
            check=check,
        )

    def test_no_tty_injection_and_no_prompt_bytes_in_delivery_argv(self) -> None:
        payload = b"multi-line secret that must never reach argv\nsecond line\n"
        self.write_handoff(payload)
        self.deliver()
        argv = json.loads(self.argv.read_text(encoding="utf-8"))
        self.assertEqual(["exec", "-"], argv)
        command = [str(self.stub), *argv]
        self.assertNotIn("shpool", " ".join(command))
        self.assertNotIn("attach", command)
        self.assertTrue(all(payload not in item.encode() for item in command))
        source = BASHRC.read_text(encoding="utf-8")
        helper = source.split("__sk_codex_exec_handoff() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("shpool attach", helper)
        self.assertNotIn("\\x1b[200~", helper)

    def test_started_nonzero_exit_is_quarantined_without_replay(self) -> None:
        payload = b"quarantine me after provider start\n"
        self.write_handoff(payload)
        failed = self.deliver(rc=23, accept=False, check=False)
        self.assertNotEqual(0, failed.returncode)
        quarantined = Path(f"{self.handoff}.quarantined")
        self.assertEqual(payload, quarantined.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(quarantined.stat().st_mode))

    def test_launch_failure_before_process_start_retains_handoff(self) -> None:
        payload = b"retain me when Codex cannot be launched\n"
        self.write_handoff(payload)
        missing = self.root / "missing-codex"
        failed = run(
            [
                "bash",
                "-c",
                'source "$1"; __sk_codex_exec_handoff "$2" "$3" "$4" "$5" "$6" exec -',
                "prompt-spawn-failure-test",
                BASHRC,
                self.handoff,
                self.acceptance,
                self.intake,
                self.completion,
                missing,
            ],
            env=self.env(accept=False),
            check=False,
        )
        self.assertNotEqual(0, failed.returncode)
        self.assertEqual(payload, self.handoff.read_bytes())
        self.assertFalse(self.acceptance.exists())

    def test_multiline_prompt_is_delivered_exactly_over_stdin(self) -> None:
        payload = "first line\nsecond line with 'quotes'\nthird line\n".encode()
        self.write_handoff(payload)
        self.deliver()
        self.assertEqual(payload, self.capture.read_bytes())
        argv = json.loads(self.argv.read_text(encoding="utf-8"))
        self.assertEqual(["exec", "-"], argv)
        self.assertTrue(all(payload not in item.encode() for item in argv))

    def test_handoff_is_retained_until_exact_acceptance_record_exists(self) -> None:
        payload = b"accept this exact content\n"
        self.write_handoff(payload)
        self.deliver()
        observed = json.loads(self.observed.read_text(encoding="utf-8"))
        self.assertTrue(observed["handoff_exists"])
        self.assertFalse(observed["acceptance_exists"])
        record = json.loads(self.acceptance.read_text(encoding="utf-8"))
        self.assertEqual(1, record["schema_version"])
        self.assertEqual("accepted", record["status"])
        self.assertEqual(len(payload), record["bytes"])
        self.assertEqual("11111111-1111-4111-8111-111111111111", record["turn_id"])
        self.assertFalse(self.handoff.exists())
        completion = json.loads(self.completion.read_text(encoding="utf-8"))
        self.assertEqual("intake_committed", completion["status"])
        self.assertEqual(0, completion["exit_code"])

    def test_successful_handoff_is_consumed_exactly_once(self) -> None:
        self.write_handoff(b"one delivery only\n")
        self.deliver()
        self.assertFalse(self.handoff.exists())
        repeated = self.deliver(check=False)
        self.assertNotEqual(0, repeated.returncode)
        self.assertEqual("1", self.calls.read_text(encoding="utf-8"))

    def test_crash_after_hook_acceptance_consumes_without_replay(self) -> None:
        payload = b"accepted before the simulated process crash\n"
        self.write_handoff(payload)
        crashed = self.deliver(
            rc=31, accept=True, wait_for_handoff_removal=True, check=False
        )
        self.assertEqual(31, crashed.returncode)
        observed = json.loads(self.observed.read_text(encoding="utf-8"))
        self.assertTrue(observed["acceptance_after_hook"])
        self.assertTrue(observed["intake_before_handoff_wait"])
        self.assertFalse(observed["handoff_after_hook_wait"])
        self.assertFalse(self.handoff.exists())
        retried = self.deliver(check=False)
        self.assertNotEqual(0, retried.returncode)
        self.assertEqual("1", self.calls.read_text(encoding="utf-8"))

    def test_started_without_marker_is_quarantined_without_retry(self) -> None:
        payload = b"never retry after a provider process starts\n"
        self.write_handoff(payload)
        failed = self.deliver(rc=29, accept=False, check=False)
        self.assertEqual(29, failed.returncode)
        self.assertFalse(self.handoff.exists())
        self.assertEqual(payload, Path(f"{self.handoff}.quarantined").read_bytes())
        exhausted = json.loads(self.completion.read_text())
        self.assertEqual("outcome_unknown", exhausted["status"])
        refused = self.deliver(check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertEqual("1", self.calls.read_text(encoding="utf-8"))

    def test_started_with_conflicting_marker_is_quarantined_without_retry(self) -> None:
        payload = b"conflicting marker after process start\n"
        self.write_handoff(payload)
        failed = self.deliver(accept=False, conflict=True, check=False)
        self.assertEqual(70, failed.returncode)
        self.assertFalse(self.handoff.exists())
        self.assertEqual(payload, Path(f"{self.handoff}.quarantined").read_bytes())
        record = json.loads(self.completion.read_text())
        self.assertEqual("outcome_unknown", record["status"])

    def test_provider_acceptance_without_intake_moves_to_needs_you(self) -> None:
        payload = b"accepted but intake durability failed\n"
        self.write_handoff(payload)
        failed = self.deliver(accept=True, intake=False, check=False)
        self.assertEqual(70, failed.returncode)
        self.assertFalse(self.handoff.exists())
        self.assertEqual(payload, Path(f"{self.handoff}.intake_pending").read_bytes())
        record = json.loads(self.completion.read_text())
        self.assertEqual("intake_pending", record["status"])
        retried = self.deliver(check=False)
        self.assertNotEqual(0, retried.returncode)
        self.assertEqual("1", self.calls.read_text(encoding="utf-8"))

    def test_restart_with_prior_acceptance_never_replays_provider(self) -> None:
        prompt = "accepted immediately before launcher crash\n"
        self.write_handoff(prompt.encode())
        environment = self.env(accept=False, intake=False)
        environment.update(
            {
                "SESSION_KIT_PROMPT_HANDOFF": str(self.handoff),
                "SESSION_KIT_PROMPT_HANDOFF_ACCEPTANCE": str(self.acceptance),
                "SESSION_KIT_PROMPT_HANDOFF_BYTES": str(len(prompt.encode())),
                "SESSION_KIT_PROMPT_HANDOFF_SHA256": hashlib.sha256(prompt.encode()).hexdigest(),
                "SESSION_KIT_START_DIR": str(self.root),
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_TEST_FAILPOINT": "codex-intake-after-acceptance",
            }
        )
        run(
            [sys.executable, REPO / "lib/sessionkit_supervisor/provider_hooks.py", "codex-hook"],
            env=environment,
            input_text=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": prompt,
                    "session_id": "00000000-0000-4000-8000-000000000007",
                    "turn_id": "11111111-1111-4111-8111-111111111111",
                }
            ),
        )
        refused = self.deliver(accept=False, check=False)
        self.assertEqual(70, refused.returncode)
        self.assertFalse(self.calls.exists())
        self.assertEqual(prompt.encode(), Path(f"{self.handoff}.intake_pending").read_bytes())
        self.assertEqual("intake_pending", json.loads(self.completion.read_text())["status"])

    def test_zero_exit_without_hook_marker_is_an_anomaly_not_acceptance(self) -> None:
        payload = b"zero exit is not prompt acceptance\n"
        self.write_handoff(payload)
        anomaly = self.deliver(accept=False, check=False)
        self.assertEqual(70, anomaly.returncode)
        self.assertIn("without a durable intake result", anomaly.stderr)
        self.assertFalse(self.handoff.exists())
        self.assertEqual(payload, Path(f"{self.handoff}.quarantined").read_bytes())
        self.assertFalse(self.acceptance.exists())
        record = json.loads(self.completion.read_text())
        self.assertEqual("outcome_unknown", record["status"])
        refused = self.deliver(check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertEqual("1", self.calls.read_text(encoding="utf-8"))

    def test_acceptance_requires_exact_user_prompt_submit_event(self) -> None:
        prompt = "event-bound prompt\n"
        self.write_handoff(prompt.encode())
        hook_env = self.env(accept=False)
        hook_env.update(
            {
                "SESSION_KIT_PROMPT_HANDOFF": str(self.handoff),
                "SESSION_KIT_PROMPT_HANDOFF_ACCEPTANCE": str(self.acceptance),
                "SESSION_KIT_PROMPT_HANDOFF_BYTES": str(len(prompt.encode())),
                "SESSION_KIT_PROMPT_HANDOFF_SHA256": hashlib.sha256(
                    prompt.encode()
                ).hexdigest(),
                "SESSION_KIT_START_DIR": str(self.root),
            }
        )
        base = {
            "prompt": prompt,
            "session_id": "00000000-0000-4000-8000-000000000007",
            "turn_id": "turn-event-test",
        }
        for event in (None, "", "user_prompt_submit", "userpromptsubmit"):
            with self.subTest(event=event):
                payload = dict(base)
                if event is not None:
                    payload["hook_event_name"] = event
                run(
                    [
                        sys.executable,
                        REPO / "lib/sessionkit_supervisor/provider_hooks.py",
                        "codex-hook",
                    ],
                    env=hook_env,
                    input_text=json.dumps(payload),
                )
                self.assertFalse(self.acceptance.exists())

    def test_acceptance_marker_must_be_exact_private_handoff_sibling(self) -> None:
        prompt = "contained marker prompt\n"
        self.write_handoff(prompt.encode())
        outside = self.state / "arbitrary.accepted"
        hook_env = self.env(accept=False)
        hook_env.update(
            {
                "SESSION_KIT_PROMPT_HANDOFF": str(self.handoff),
                "SESSION_KIT_PROMPT_HANDOFF_ACCEPTANCE": str(outside),
                "SESSION_KIT_PROMPT_HANDOFF_BYTES": str(len(prompt.encode())),
                "SESSION_KIT_PROMPT_HANDOFF_SHA256": hashlib.sha256(
                    prompt.encode()
                ).hexdigest(),
                "SESSION_KIT_START_DIR": str(self.root),
            }
        )
        run(
            [
                sys.executable,
                REPO / "lib/sessionkit_supervisor/provider_hooks.py",
                "codex-hook",
            ],
            env=hook_env,
            input_text=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": prompt,
                    "session_id": "00000000-0000-4000-8000-000000000007",
                    "turn_id": "turn-path-test",
                }
            ),
        )
        self.assertFalse(outside.exists())
        self.assertFalse(self.acceptance.exists())

    def test_handoff_creation_is_private_and_audited(self) -> None:
        source = self.root / "source.prompt"
        source.write_text("audited prompt\n", encoding="utf-8")
        source.chmod(0o600)
        run(
            [
                "bash",
                "-c",
                'source "$1"; source "$2"; sk_prepare_state; '
                'sk_stage_prompt_handoff "$3" "$4"; '
                'identity=$SK_STAGED_PROMPT_IDENTITY; '
                'sk_log_action prompt_handoff created; '
                'sk_finalize_prompt_source "$3" "$identity"',
                "prompt-creation-test",
                COMMON,
                SESSIONS,
                source,
                self.handoff,
            ],
            env=self.env(),
        )
        self.assertFalse(source.exists())
        self.assertEqual(0o600, stat.S_IMODE(self.handoff.stat().st_mode))
        events = [
            json.loads(line)
            for line in (self.state / "action-events.jsonl").read_text().splitlines()
        ]
        self.assertEqual("prompt_handoff", events[-1]["action"])
        self.assertEqual("created", events[-1]["outcome"])

    def test_audit_failure_preserves_source_copy(self) -> None:
        source = self.root / "source.prompt"
        payload = b"must survive an audit append failure\n"
        source.write_bytes(payload)
        source.chmod(0o600)
        run(
            [
                "bash",
                "-c",
                'source "$1"; source "$2"; sk_prepare_state; '
                'sk_stage_prompt_handoff "$3" "$4"; '
                'identity=$SK_STAGED_PROMPT_IDENTITY; '
                'sk_log_action() { return 1; }; '
                'if ! sk_log_action prompt_handoff created; then '
                'sk_abort_prompt_stage "$3" "$4" "$identity" || true; fi',
                "prompt-audit-failure-test",
                COMMON,
                SESSIONS,
                source,
                self.handoff,
            ],
            env=self.env(),
        )
        self.assertEqual(payload, source.read_bytes())
        self.assertFalse(self.handoff.exists())

    def test_crash_after_atomic_quarantine_move_cannot_replay(self) -> None:
        payload = b"move me before the terminal record\n"
        self.write_handoff(payload)
        environment = self.env(accept=False)
        environment["SESSION_KIT_TESTING"] = "1"
        environment["SESSION_KIT_TEST_FAILPOINT"] = "prompt-after-quarantine-move"
        crashed = run(
            [
                "bash", "-c",
                'source "$1"; __sk_codex_exec_handoff "$2" "$3" "$4" "$5" "$6" exec -',
                "prompt-quarantine-crash-test", BASHRC, self.handoff,
                self.acceptance, self.intake, self.completion, self.stub,
            ],
            env=environment,
            check=False,
        )
        self.assertEqual(91, crashed.returncode)
        self.assertFalse(self.handoff.exists())
        self.assertEqual(payload, Path(f"{self.handoff}.quarantined").read_bytes())
        self.assertFalse(self.completion.exists())
        replay = self.deliver(check=False)
        self.assertNotEqual(0, replay.returncode)
        self.assertEqual("1", self.calls.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
