"""End-to-end delegate wiring: a recorded intake to a real worker launch.

`sp msg intake delegate` is the only path that starts a worker, and everything
in front of the launch is real here. The intake is written by the spool's own
record verb through the installed core CLI, the preflight is that CLI's, and
the delegate run reads the entry back off disk with nothing stubbed between the
stored entry and the launcher. That gap is exactly where the delegate verb was
dead: the launcher was handed an empty directory because the reader asked the
entry for a key the writer never wrote, and the older test suite could not see
it because it passed its own launcher in.

Two boundaries are stated instead of faked away. The launcher runs the
installed `sp new`, which would start a real provider session, so
`SESSION_KIT_SP_CMD` points at a script that records the directory and argv it
was handed — that record IS the fact under test. Reconciliation then needs a
live inventory row for the launch key, which no sandbox can produce, so
delegate fails immediately after the launch; every assertion here is about what
the launcher actually received before that boundary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor import intake  # noqa: E402
from sessionkit_supervisor.source_authority import capture_hook_event  # noqa: E402


CORE = REPO / "lib" / "session_inventory.py"
SOURCE_UUID = "019fdf1e-8b4c-7573-a089-be495bfece6a"
SUPERVISOR_UUID = "dcbdf940-4eda-4967-8e41-23a5760c32b5"
INTAKE_ID = "a1b2c3d4"
ALIAS_ID = "b2c3d4e5"
CLAUDE_MODEL = "claude-opus-test"
CODEX_MODEL = "gpt-codex-test"
PROJECT = "rebuild the sitemap generator and ship it"


class DelegateWiringCase(unittest.TestCase):
    """A disposable state dir, a real project directory, and a recording `sp`."""

    def setUp(self) -> None:
        # System temp, never the repo: a sandbox under the checkout dirties
        # `git status` and trips the installer's clean-tree gate mid-run.
        self.temporary = tempfile.TemporaryDirectory(prefix="intake-delegate-")
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        # The directory the intake names: the launcher refuses anything that is
        # not an existing absolute directory, so this one is real on disk.
        self.project = self.base / "project"
        self.project.mkdir(mode=0o700)
        self.transcripts = self.state / "test-transcripts"
        self.transcripts.mkdir(mode=0o700)
        self.launched = self.base / "launched"
        self.sp = self.base / "fake-sp"
        self.sp.write_text(
            "#!/bin/sh\n"
            'printf \'{"cwd": "%s", "argv": "%s"}\\n\' "$(pwd -P)" "$*"'
            ' >> "$SK_LAUNCH_MARKER"\n',
            encoding="utf-8",
        )
        self.sp.chmod(0o755)
        # Reconciliation asks for a live inventory. Unstubbed, the core shells
        # out to the real `shpool list --json`, and shpool self-daemonizes
        # against this sandbox's socket and detaches -- one stray daemon per
        # test, on the operator's own machine. These two fixtures are the
        # collector's supported test seams (same pair tests/test_intake_spool.py
        # uses), so no external command runs at all. The assertions are
        # untouched: an empty session list still carries no row for the launch
        # key, so reconciliation still fails exactly where it did.
        self.shpool_json = self.base / "shpool.json"
        self.shpool_json.write_text(json.dumps({"sessions": []}), encoding="utf-8")
        self.agents_json = self.base / "agents.json"
        self.agents_json.write_text("[]", encoding="utf-8")
        self.name_supervisor()

    def tearDown(self) -> None:
        self._stop_sandbox_shpool_daemons()
        self.temporary.cleanup()

    def _sandbox_shpool_pids(self) -> list[int]:
        needle = os.fspath(self.base)
        found: list[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            if "shpool" in cmdline and needle in cmdline:
                found.append(int(entry.name))
        return found

    def _stop_sandbox_shpool_daemons(self) -> None:
        """A net under the fixtures above, not the thing that keeps us clean.

        If any path ever reaches the real shpool again, it self-daemonizes
        against this sandbox's socket and detaches. A stray daemon is not a
        test-tidiness problem: the live inventory collector counts daemons on
        the host, and extra ones make it invent an unresolved session and fail
        numbering closed, which degrades every real picker on the machine to
        cached and actions-disabled. So sweep, and be certain about it.

        Every wait here is bounded and every signal escalates, because a
        teardown that can hang or give up is worse than no teardown -- it
        turns a stray process into a stuck CI job.
        """
        seen: set[int] = set()
        settle = time.monotonic() + 2.0
        while True:
            victims = [pid for pid in self._sandbox_shpool_pids() if pid not in seen]
            for pid in victims:
                seen.add(pid)
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            # The daemon detaches asynchronously, so keep watching for a
            # short window after the last sighting rather than scanning once.
            if victims:
                settle = time.monotonic() + 2.0
            elif time.monotonic() >= settle:
                break
            time.sleep(0.05)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            alive = self._sandbox_shpool_pids()
            if not alive:
                return
            time.sleep(0.05)
        for pid in self._sandbox_shpool_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.assertEqual(
            [],
            self._sandbox_shpool_pids(),
            "a sandbox shpool daemon outlived its test and is now loose on the host",
        )

    def name_supervisor(self) -> None:
        root = self.state / "supervisor"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / "identity"
        path.write_text(f"claude:{SUPERVISOR_UUID}\n", encoding="utf-8")
        path.chmod(0o600)

    def env(self, **overrides: str) -> dict[str, str]:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": os.fspath(self.home),
            "XDG_CONFIG_HOME": os.fspath(self.home / "config"),
            "XDG_STATE_HOME": os.fspath(self.home / "state"),
            "SESSION_KIT_STATE_DIR": os.fspath(self.state),
            "SESSION_KIT_SP_CMD": os.fspath(self.sp),
            # The suite-wide belt: a sandboxed run of the core must never
            # reach out and title the machine's live threads.
            "SESSION_KIT_CODEX_AUTOTITLE": "0",
            "SESSION_KIT_TESTING": "1",
            "SESSION_KIT_SHPOOL_JSON_FILE": os.fspath(self.shpool_json),
            "SESSION_KIT_CLAUDE_JSON_FILE": os.fspath(self.agents_json),
            "SK_LAUNCH_MARKER": os.fspath(self.launched),
        }
        environment.update(overrides)
        return environment

    def core(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [sys.executable, os.fspath(CORE), *argv],
            env=self.env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        if check:
            self.assertEqual(
                0, completed.returncode, completed.stderr or completed.stdout
            )
        return completed

    def launches(self) -> list[dict[str, str]]:
        if not self.launched.exists():
            return []
        return [
            json.loads(line)
            for line in self.launched.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # ---- fixtures --------------------------------------------------------

    @staticmethod
    def one_worker_plan() -> list[dict[str, str]]:
        return [
            {
                "branch": "agent-implementation",
                "idempotency_key": "worker:implementation:1",
                "workstream": "implementation and tests",
                "scope": "implement the bounded change and run verification",
                "provider": "claude",
                "requested_model": CLAUDE_MODEL,
                "expertise": "implementation",
                "rationale": "one worker is enough to prove the launch wiring",
                "task_text": "implement the bounded change and run the suite",
                "acceptance_criteria": "the suite passes and the change is committed",
                "deliverable": "commits on the branch plus the test output",
            }
        ]

    @staticmethod
    def automatic_plan() -> list[dict[str, str]]:
        return [
            {
                "branch": "agent-implementation",
                "idempotency_key": "worker:implementation:1",
                "workstream": "implementation and tests",
                "scope": "implement the bounded change and run verification",
                "provider": "claude",
                "requested_model": CLAUDE_MODEL,
                "expertise": "implementation",
                "rationale": "implementation expertise for the code itself",
                "task_text": "implement the bounded change and run the suite",
                "acceptance_criteria": "the suite passes and the change is committed",
                "deliverable": "commits on the branch plus the test output",
            },
            {
                "branch": "agent-research",
                "idempotency_key": "worker:research:1",
                "workstream": "source and risk analysis",
                "scope": "independently audit requirements and failure modes",
                "provider": "codex",
                "requested_model": CODEX_MODEL,
                "expertise": "security",
                "rationale": "a separate model family for independent review",
                "task_text": "audit the requirements and name every failure mode",
                "acceptance_criteria": "every declared risk has a named mitigation",
                "deliverable": "a written risk analysis on the branch",
            },
        ]

    def record_intake(self, msg_id: str, cwd: Path, key: str = "") -> None:
        argv = [
            "msg",
            "intake",
            "record",
            "--msg-id",
            msg_id,
            "--source",
            f"codex:{SOURCE_UUID}",
            "--summary",
            PROJECT,
            "--cwd",
            os.fspath(cwd),
        ]
        if key:
            argv.extend(("--key", key))
        self.core(*argv)

    def preflight(
        self,
        msg_id: str,
        plan: list[dict[str, str]],
        *,
        source_event_id: str = "",
        exception: str = "",
        required_tags: str = "",
    ) -> None:
        argv = [
            "msg",
            "intake",
            "preflight",
            "--msg-id",
            msg_id,
            "--analysis",
            "read the recorded intake and its ordered requirements",
            "--scope",
            "the current source event and this intake's requirements",
            "--required-expertise",
            "Python implementation and verification",
            "--required-tags",
            required_tags or ",".join(
                dict.fromkeys(str(row["expertise"]) for row in plan)
            ),
            "--worker-plan-json",
            json.dumps(plan),
            "--risks",
            "a worker launched in the wrong working directory",
            "--tests",
            "this end-to-end delegate wiring test",
        ]
        if source_event_id:
            argv.extend(("--source-event-id", source_event_id))
        if exception:
            argv.extend(("--manual-policy-exception", exception))
        self.core(*argv)

    def delegate(self, msg_id: str, *branches: str) -> subprocess.CompletedProcess[str]:
        argv = ["msg", "intake", "delegate", "--msg-id", msg_id]
        for branch in branches:
            argv.extend(("--branch", branch))
        return self.core(*argv, check=False)


class DelegateLaunchDirectoryTests(DelegateWiringCase):
    def test_delegate_launches_the_worker_in_the_intake_source_directory(self) -> None:
        """The launcher gets the directory the intake was recorded with."""
        self.record_intake(INTAKE_ID, self.project)
        self.preflight(
            INTAKE_ID,
            self.one_worker_plan(),
            exception="one worker: this proves launch wiring, not fleet policy",
        )
        completed = self.delegate(INTAKE_ID, "agent-implementation")
        rows = self.launches()
        self.assertEqual(1, len(rows), completed.stderr)
        self.assertEqual(self.project.resolve(), Path(rows[0]["cwd"]).resolve())
        self.assertEqual(
            f"new claude --model {CLAUDE_MODEL} --launch-key worker:implementation:1",
            rows[0]["argv"],
        )
        # The launch happened; only reconciliation — which needs a live
        # inventory row this sandbox cannot have — is left. A run that never
        # resolved the directory fails one step earlier, at dispatch.
        self.assertNotIn("dispatch is uncertain", completed.stderr)
        self.assertIn("inventory reconciliation failed", completed.stderr)

    def test_delegate_resolves_an_alias_message_id_to_the_same_directory(self) -> None:
        """A project re-sent under a second id delegates from the first entry."""
        self.record_intake(INTAKE_ID, self.project, key="project:sitemap:1")
        self.record_intake(ALIAS_ID, self.project, key="project:sitemap:1")
        self.preflight(
            INTAKE_ID,
            self.one_worker_plan(),
            exception="one worker: this proves launch wiring, not fleet policy",
        )
        completed = self.delegate(ALIAS_ID, "agent-implementation")
        rows = self.launches()
        self.assertEqual(1, len(rows), completed.stderr)
        self.assertEqual(self.project.resolve(), Path(rows[0]["cwd"]).resolve())

    def test_delegate_launches_an_automatic_intake_from_its_prompt_directory(
        self,
    ) -> None:
        """The producer's own entry carries the directory to the launcher too."""
        event = capture_hook_event(
            {
                "provider": "codex",
                "session_id": SOURCE_UUID,
                "turn_id": "turn-1",
                "prompt": PROJECT,
                "cwd": os.fspath(self.project),
                "transcript_path": os.fspath(self.transcripts / "turn-1.jsonl"),
            },
            state_dir=self.state,
        )
        outcome = intake.produce(
            intake.Spool(self.state),
            thread_key=f"codex:{SOURCE_UUID}",
            prompt=PROJECT,
            turn_id="turn-1",
            cwd=os.fspath(self.project),
            source_event_id=event["event_id"],
            source_digest=event["prompt_sha256"],
        )
        entry = outcome["entry"]
        self.assertIsNotNone(entry)
        msg_id = entry["msg_id"]
        self.assertEqual(os.fspath(self.project), entry["source_cwd"])
        self.preflight(msg_id, self.automatic_plan(), source_event_id=event["event_id"])
        completed = self.delegate(msg_id, "agent-implementation", "agent-research")
        rows = self.launches()
        self.assertGreaterEqual(len(rows), 1, completed.stderr)
        self.assertEqual(self.project.resolve(), Path(rows[0]["cwd"]).resolve())
        self.assertNotIn("dispatch is uncertain", completed.stderr)


if __name__ == "__main__":
    unittest.main()
