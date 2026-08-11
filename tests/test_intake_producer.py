"""The automatic intake producer: a project recorded with nobody's help.

A root session states what it wants; the machine writes that down as an intake
and asks for a supervisor. No agent messages anything, and the human's prompt
waits for none of it. These tests drive the real provider hook scripts as
subprocesses against a disposable state directory, with a fake `supervisor`
command standing in for the real one.

Codex boundary, stated where it can be read next to the test: the Codex hook is
exercised by writing `UserPromptSubmit` payloads to its stdin, exactly as a
user-level `~/.codex/hooks.json` command hook is invoked. That proves its
logic, its dedup, and its refusal to guess an identity. It does NOT prove that
a live Codex process discovers, trusts by hash, and invokes it — nothing in
this suite starts Codex. That fresh-process proof is the end-to-end drill's.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor import intake as intake_mod  # noqa: E402
from sessionkit_supervisor.intake import (  # noqa: E402
    ENSURE_COOLDOWN_MS,
    Spool,
    payload_fields,
    produce,
    request_supervisor,
    substantive_prompt,
)

def remove_sandbox(temporary: tempfile.TemporaryDirectory) -> None:
    """Remove a sandbox that a detached delivery ask may still be writing into.

    The producer spawns its delivery ask fire-and-forget by design, so a
    marker write can land between rmtree emptying the directory and removing
    it. Retry briefly; the writer is a one-shot append and finishes in
    milliseconds.
    """
    deadline = time.monotonic() + 5
    while True:
        try:
            temporary.cleanup()
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


CLAUDE_HOOK = REPO / "extras" / "hooks" / "sk_session_events.py"
CODEX_HOOK = REPO / "extras" / "hooks" / "sk_codex_intake.py"
CLAUDE_UUID = "dcbdf940-4eda-4967-8e41-23a5760c32b5"
CODEX_UUID = "019fdf1e-8b4c-7573-a089-be495bfece6a"
SUBAGENT_UUID = "96d743aa-1111-4222-8333-444444444444"
CLAUDE_KEY = f"claude:{CLAUDE_UUID}"
CODEX_KEY = f"codex:{CODEX_UUID}"
PROJECT = "rebuild the sitemap generator and ship it"
SOURCE_EVENT_A = "a" * 64
SOURCE_EVENT_B = "b" * 64
SOURCE_EVENT_C = "c" * 64


class ProducerCase(unittest.TestCase):
    """A sandboxed state dir, a fake supervisor, and the real hook scripts."""

    ENSURE_SLEEP = "0"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".session-kit-producer-", dir=REPO
        )
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.ensured = self.base / "ensured"
        self.supervisor = self.base / "fake-supervisor"
        self.supervisor.write_text(
            "#!/bin/sh\n"
            f"sleep {self.ENSURE_SLEEP}\n"
            'printf "%s\\n" "$1" >> "$SK_ENSURE_MARKER"\n',
            encoding="utf-8",
        )
        self.supervisor.chmod(0o755)
        # Amendment delivery is a detached run of the messaging core. The
        # producer's job is to ASK for it; what it asks is recorded here rather
        # than actually messaging anybody.
        self.delivered = self.base / "delivery-asked"
        self.core = self.base / "fake-core.py"
        self.core.write_text(
            "import os, sys\n"
            "with open(os.environ['SK_DELIVERY_MARKER'], 'a') as handle:\n"
            "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        remove_sandbox(self.temporary)

    def env(self, **overrides: str) -> dict[str, str]:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": os.fspath(self.home),
            "SESSION_KIT_STATE_DIR": os.fspath(self.state),
            "SESSION_KIT_SUPERVISOR_BIN": os.fspath(self.supervisor),
            "SESSION_KIT_INVENTORY_CORE": os.fspath(self.core),
            "SK_ENSURE_MARKER": os.fspath(self.ensured),
            "SK_DELIVERY_MARKER": os.fspath(self.delivered),
        }
        environment.update(overrides)
        return environment

    def _fire(self, hook: Path, payload: dict) -> None:
        """One provider hook, driven exactly as its provider drives it."""
        completed = subprocess.run(
            [sys.executable, os.fspath(hook)],
            env=self.env(),
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stdout)

    def claude_prompt(self, prompt: str = PROJECT, **fields: object) -> None:
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": CLAUDE_UUID,
            "turn_id": "test-turn-" + intake_mod.prompt_digest(prompt)[:16],
            "prompt": prompt,
            "cwd": "/srv/example",
        }
        payload.update(fields)
        self._fire(CLAUDE_HOOK, payload)

    def codex_prompt(self, prompt: str = PROJECT, **fields: object) -> None:
        """A Codex user-level UserPromptSubmit hook: JSON on stdin."""
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": CODEX_UUID,
            "turn_id": "turn-1",
            "transcript_path": "/tmp/rollout.jsonl",
            "model": "gpt-5-codex",
            "cwd": "/srv/example",
            "prompt": prompt,
        }
        payload.update(fields)
        self._fire(CODEX_HOOK, payload)

    def delivery_requests(self) -> list[str]:
        if not self.delivered.exists():
            return []
        return [
            line
            for line in self.delivered.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def wait_for_delivery(self, count: int = 1, seconds: float = 6.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if len(self.delivery_requests()) >= count:
                return True
            time.sleep(0.05)
        return False

    def entries(self) -> list[dict]:
        return Spool(self.state).open_entries()

    def ensure_calls(self) -> list[str]:
        if not self.ensured.exists():
            return []
        return [
            line
            for line in self.ensured.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def wait_for_ensure(self, seconds: float = 6.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.ensure_calls():
                return True
            time.sleep(0.05)
        return False


class ClaudeProducerTests(ProducerCase):
    def test_a_fresh_claude_root_produces_one_intake_with_no_agent_help(self) -> None:
        self.claude_prompt()
        rows = self.entries()
        self.assertEqual(1, len(rows))
        entry = rows[0]
        self.assertEqual("auto", entry["origin"])
        self.assertEqual(CLAUDE_KEY, entry["source_thread_key"])
        self.assertEqual("received", entry["state"])
        self.assertEqual(PROJECT, entry["summary"])
        self.assertEqual("/srv/example", entry["source_cwd"])
        self.assertEqual(f"auto-intake:{CLAUDE_KEY}", entry["intake_key"])
        self.assertTrue(self.wait_for_ensure())
        self.assertTrue(self.wait_for_delivery())
        self.assertEqual(["ensure"], self.ensure_calls())
        self.assertEqual(["msg intake flush"], self.delivery_requests())

    def test_a_hook_that_fires_again_still_yields_exactly_one_intake(self) -> None:
        """Double delivery, a relaunch, a second prompt: one project either way."""
        self.claude_prompt()
        self.claude_prompt()
        self.claude_prompt("and also fix the redirects while you are there")
        self.assertEqual(1, len(self.entries()))
        # Three asks, one project. The middle prompt recorded nothing new, and
        # it still asks for delivery: the arrival notice this sandbox never
        # lands is owed, and a prompt is the wake that retries it.
        self.assertTrue(self.wait_for_delivery(3))
        self.assertEqual(3, len(self.delivery_requests()))

    def test_a_subagent_prompt_produces_nothing(self) -> None:
        for marker in ("parent_tool_use_id", "parent_session_id", "isSidechain"):
            value: object = True if marker == "isSidechain" else "toolu_0123"
            self.claude_prompt(
                "search the repository for the sitemap builder",
                session_id=SUBAGENT_UUID,
                **{marker: value},
            )
        self.assertEqual([], self.entries())

    def test_a_greeting_or_a_slash_command_is_not_a_project(self) -> None:
        for prompt in ("hi", "/clear", "   ", "ok", "yes please"):
            self.claude_prompt(prompt)
        self.assertEqual([], self.entries())

    def test_an_operator_message_never_becomes_an_intake(self) -> None:
        """The supervisor's own standing brief arrives as one of these."""
        self.claude_prompt(
            "[session-kit operator message 4f3a2b1c]\nFrom: the operator.\n---\n"
            "status in one line please"
        )
        self.assertEqual([], self.entries())

    def test_the_headless_delivery_runner_never_becomes_a_project(self) -> None:
        self.claude_prompt(
            "You are a Session Kit delivery runner. Deliver one operator "
            "message, verbatim, to the named session and nobody else."
        )
        self.assertEqual([], self.entries())
        self.assertEqual([], self.ensure_calls())
        self.assertEqual([], self.delivery_requests())

    def test_wrapped_operator_delivery_never_becomes_a_project(self) -> None:
        self.claude_prompt(
            '<cross-session-message from="uds:/run/user/1000/cc.sock" '
            'from-mode="bypass">[session-kit operator message deadbeef]'
            "From: the operator.---worker assignment"
        )
        self.assertEqual([], self.entries())
        self.assertEqual([], self.ensure_calls())

    def test_automation_wake_never_becomes_a_project(self) -> None:
        self.claude_prompt(
            "RUNTIME FOR THIS WAKE (ground truth, read off the machine): "
            "Claude current, activated fresh for this message."
        )
        self.assertEqual([], self.entries())
        self.assertEqual([], self.ensure_calls())

    def test_the_supervisor_never_intakes_its_own_thread(self) -> None:
        marker = self.state / "supervisor"
        marker.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity = marker / "identity"
        identity.write_text(f"{CLAUDE_KEY}\n", encoding="utf-8")
        identity.chmod(0o600)
        self.claude_prompt()
        self.assertEqual([], self.entries())

    def test_a_root_prompt_with_no_supervisor_to_start_is_still_recorded(self) -> None:
        """The project outlives a supervisor that cannot start; that is the point."""
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": CLAUDE_UUID,
            "prompt": PROJECT,
        }
        completed = subprocess.run(
            [sys.executable, os.fspath(CLAUDE_HOOK)],
            env=self.env(
                SESSION_KIT_SUPERVISOR_BIN=os.fspath(self.base / "no-such-supervisor")
            ),
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(1, len(self.entries()))
        self.assertEqual([], self.ensure_calls())

    def test_the_events_hook_still_records_its_own_event_kinds(self) -> None:
        """The producer is an addition to this hook, never a replacement."""
        completed = subprocess.run(
            [sys.executable, os.fspath(CLAUDE_HOOK)],
            env=self.env(),
            input=json.dumps(
                {"hook_event_name": "SessionStart", "session_id": CLAUDE_UUID}
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        lines = (self.state / "events" / f"{CLAUDE_KEY}.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertIn('"event":"session_start"', lines)
        self.assertEqual([], self.entries())


class SlowEnsureTests(ProducerCase):
    ENSURE_SLEEP = "2"

    def test_the_prompt_never_waits_for_the_supervisor(self) -> None:
        started = time.monotonic()
        self.claude_prompt()
        elapsed = time.monotonic() - started
        # The entry is durable the moment the hook returns; the supervisor is
        # still starting behind it.
        self.assertEqual(1, len(self.entries()))
        self.assertLess(elapsed, 1.5, "the hook waited for supervisor ensure")
        self.assertEqual([], self.ensure_calls())
        self.assertTrue(self.wait_for_ensure(), "ensure never ran at all")

    def test_a_fleet_of_roots_starting_at_once_asks_for_one_supervisor(self) -> None:
        payloads = [
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": f"aaaaaaaa-bbbb-4ccc-8ddd-00000000000{index}",
                "prompt": f"take project number {index} to completion",
            }
            for index in range(5)
        ]
        running = [
            subprocess.Popen(
                [sys.executable, os.fspath(CLAUDE_HOOK)],
                env=self.env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for _ in payloads
        ]
        for process, payload in zip(running, payloads):
            process.communicate(json.dumps(payload))
        self.assertEqual(5, len(self.entries()))
        self.assertTrue(self.wait_for_ensure())
        time.sleep(0.5)
        self.assertEqual(["ensure"], self.ensure_calls())


class CodexProducerTests(ProducerCase):
    """Payload-driven only — see this module's docstring for the boundary."""

    def test_a_fresh_codex_root_produces_one_intake_with_no_agent_help(self) -> None:
        self.codex_prompt()
        rows = self.entries()
        self.assertEqual(1, len(rows))
        self.assertEqual(CODEX_KEY, rows[0]["source_thread_key"])
        self.assertEqual("auto", rows[0]["origin"])
        self.assertEqual(f"auto-intake:{CODEX_KEY}", rows[0]["intake_key"])
        self.assertEqual("turn-1", rows[0]["source_turn_id"])
        self.assertRegex(rows[0]["source_event_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            rows[0]["source_event_id"],
            next(
                (self.state / "supervisor/source-events/entries").glob("*.json")
            ).stem,
        )
        self.assertEqual("/srv/example", rows[0]["source_cwd"])
        self.assertTrue(self.wait_for_ensure())
        self.assertTrue(self.wait_for_delivery())

    def test_a_codex_hook_firing_twice_on_one_turn_records_one_thing(self) -> None:
        self.codex_prompt()
        self.codex_prompt()
        rows = self.entries()
        self.assertEqual(1, len(rows))
        self.assertEqual([], rows[0]["amendments"])

    def test_a_later_codex_prompt_amends_the_same_project(self) -> None:
        self.codex_prompt()
        self.codex_prompt("also port the redirect map", turn_id="turn-2")
        rows = self.entries()
        self.assertEqual(1, len(rows))
        self.assertEqual(
            ["also port the redirect map"],
            [item["text"] for item in rows[0]["amendments"]],
        )
        self.assertRegex(
            rows[0]["amendments"][0]["source_event_id"], r"^[0-9a-f]{64}$"
        )
        self.assertTrue(self.wait_for_ensure())
        self.assertTrue(self.wait_for_delivery(2))

    def test_a_codex_subagent_prompt_produces_nothing(self) -> None:
        self.codex_prompt(parent_session_id=CLAUDE_UUID)
        self.assertEqual([], self.entries())

    def test_a_codex_payload_with_no_exact_session_records_nothing(self) -> None:
        """No identity, no guess: the wrong session's project is worse than none."""
        self.codex_prompt(session_id=None)
        self.codex_prompt(session_id="not-a-uuid")
        self.assertEqual([], self.entries())

    def test_a_malformed_codex_payload_never_disturbs_the_prompt(self) -> None:
        for raw in ("", "{", "[]", "null", '{"prompt": null}'):
            completed = subprocess.run(
                [sys.executable, os.fspath(CODEX_HOOK)],
                env=self.env(),
                input=raw,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, completed.returncode, raw)
            self.assertEqual("", completed.stdout)
            self.assertEqual("", completed.stderr)
        self.assertEqual([], self.entries())

    def test_a_codex_event_that_is_not_a_prompt_records_nothing(self) -> None:
        self.codex_prompt(hook_event_name="SessionStart")
        self.assertEqual([], self.entries())


class AmendmentTests(ProducerCase):
    """A project stated once and then added to, which is how they really run."""

    def test_two_later_prompts_land_as_ordered_amendments_on_one_intake(self) -> None:
        self.claude_prompt()
        self.claude_prompt("also regenerate the redirect map")
        self.claude_prompt("and add a test for the trailing slash case")
        rows = self.entries()
        self.assertEqual(1, len(rows))
        entry = rows[0]
        self.assertEqual(PROJECT, entry["summary"])
        self.assertEqual(
            [
                "also regenerate the redirect map",
                "and add a test for the trailing slash case",
            ],
            [item["text"] for item in entry["amendments"]],
        )
        self.assertEqual([1, 2], [item["seq"] for item in entry["amendments"]])
        self.assertEqual(
            [f"intake-amend:{entry['msg_id']}:1", f"intake-amend:{entry['msg_id']}:2"],
            [item["relay_key"] for item in entry["amendments"]],
        )
        # The arrival and each amendment ask for delivery once; none is inline.
        self.assertTrue(self.wait_for_delivery(3))
        self.assertEqual(
            ["msg intake flush", "msg intake flush", "msg intake flush"],
            self.delivery_requests(),
        )
        self.assertTrue(all(not item["delivered"] for item in entry["amendments"]))

    def test_a_hook_that_fires_twice_on_one_prompt_amends_once(self) -> None:
        self.claude_prompt()
        self.claude_prompt("also regenerate the redirect map")
        self.claude_prompt("also regenerate the redirect map")
        entry = self.entries()[0]
        self.assertEqual(1, len(entry["amendments"]))

    def test_an_amendment_never_starts_a_second_project(self) -> None:
        self.claude_prompt()
        first = self.entries()[0]["msg_id"]
        self.claude_prompt("one more requirement for the same job")
        rows = self.entries()
        self.assertEqual(1, len(rows))
        self.assertEqual(first, rows[0]["msg_id"])
        self.assertEqual("received", rows[0]["state"])

    def test_a_prompt_after_the_report_opens_a_fresh_linked_intake(self) -> None:
        self.claude_prompt()
        first = self.entries()[0]["msg_id"]
        spool = Spool(self.state)

        def deliver(*, thread_key: str, text: str, key: str) -> dict:
            return {
                "msg_id": "aa11bb22",
                "targets": [{"thread_key": thread_key, "status": "delivered-woke"}],
            }

        intake_mod.relay(
            spool,
            msg_id=first,
            text="the sitemap is live",
            kind="completion",
            deliver=deliver,
        )
        self.assertEqual([], spool.open_entries())

        self.claude_prompt("now do the same for the news section")
        rows = spool.open_entries()
        self.assertEqual(1, len(rows))
        self.assertNotEqual(first, rows[0]["msg_id"])
        self.assertEqual(first, rows[0]["follows"])
        self.assertEqual("now do the same for the news section", rows[0]["summary"])
        self.assertEqual([], rows[0]["amendments"])
        self.assertNotEqual(
            f"intake-arrival:{first}", rows[0]["arrival_notice"]["relay_key"]
        )
        self.assertTrue(self.wait_for_delivery(2))


class AmendmentDeliveryTests(unittest.TestCase):
    """Every automatic intake notice reaches the supervisor exactly once."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".session-kit-flush-", dir=REPO
        )
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir(mode=0o700, parents=True)
        self.spool = Spool(self.state)
        self.sends: list[dict] = []
        self.status = "delivered-woke"
        produce(
            self.spool,
            thread_key=CLAUDE_KEY,
            prompt=PROJECT,
            turn_id="t1",
            source_event_id=SOURCE_EVENT_A,
            source_digest=intake_mod.prompt_digest(PROJECT),
        )
        produce(
            self.spool,
            thread_key=CLAUDE_KEY,
            prompt="also regenerate the redirect map",
            turn_id="t2",
            source_event_id=SOURCE_EVENT_B,
            source_digest=intake_mod.prompt_digest(
                "also regenerate the redirect map"
            ),
        )
        produce(
            self.spool,
            thread_key=CLAUDE_KEY,
            prompt="and add a trailing slash test",
            turn_id="t3",
            source_event_id=SOURCE_EVENT_C,
            source_digest=intake_mod.prompt_digest("and add a trailing slash test"),
        )

    def tearDown(self) -> None:
        remove_sandbox(self.temporary)

    def deliver(self, *, thread_key: str, text: str, key: str) -> dict:
        self.sends.append({"thread_key": thread_key, "text": text, "key": key})
        return {
            "msg_id": "aa11bb22",
            "targets": [{"thread_key": thread_key, "status": self.status}],
        }

    def name_a_supervisor(self) -> None:
        root = self.state / "supervisor"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity = root / "identity"
        identity.write_text(f"claude:{SUBAGENT_UUID}\n", encoding="utf-8")
        identity.chmod(0o600)

    def flush(self, *, after_ms: int = 0) -> dict:
        """One sweep. ``after_ms`` moves the clock past a relay's backoff."""
        if not after_ms:
            return intake_mod.flush(
                self.spool, deliver=self.deliver, state_dir=self.state
            )
        later = time.time() + after_ms / 1000
        return intake_mod.flush(
            self.spool,
            deliver=self.deliver,
            state_dir=self.state,
            clock=lambda: later,
        )

    def test_each_amendment_is_delivered_once_and_only_once(self) -> None:
        self.name_a_supervisor()
        first = self.flush()
        self.assertEqual(3, first["delivered"])
        self.assertEqual(
            [f"claude:{SUBAGENT_UUID}"] * 3, [send["thread_key"] for send in self.sends]
        )
        keys = [send["key"] for send in self.sends]
        self.assertEqual(sorted(set(keys)), sorted(keys))
        entry = self.spool.open_entries()[0]
        self.assertTrue(entry["arrival_notice"]["delivered"])
        self.assertTrue(all(item["delivered"] for item in entry["amendments"]))
        self.assertIn("Automatic intake", self.sends[0]["text"])
        self.assertIn(SOURCE_EVENT_A, self.sends[0]["text"])
        self.assertNotIn(PROJECT, self.sends[0]["text"])
        self.assertIn("at sequence 1", self.sends[1]["text"])
        self.assertIn(SOURCE_EVENT_B, self.sends[1]["text"])
        self.assertNotIn("regenerate the redirect map", self.sends[1]["text"])
        self.assertIn("grants no authority", self.sends[1]["text"])

        again = self.flush()
        self.assertEqual(0, again["delivered"])
        self.assertEqual("nothing is owed", again["reason"])
        self.assertEqual(3, len(self.sends))

    def test_amendments_wait_until_there_is_a_supervisor_to_tell(self) -> None:
        outcome = self.flush()
        self.assertEqual(0, outcome["delivered"])
        self.assertEqual(3, outcome["pending"])
        self.assertEqual([], self.sends)
        entry = self.spool.open_entries()[0]
        self.assertFalse(entry["arrival_notice"]["delivered"])
        self.assertTrue(all(not item["delivered"] for item in entry["amendments"]))

    def test_an_amendment_that_did_not_land_is_retried_under_its_own_key(self) -> None:
        self.name_a_supervisor()
        self.status = "unreachable"
        self.assertEqual(0, self.flush()["delivered"])
        entry = self.spool.open_entries()[0]
        self.assertTrue(all(not item["delivered"] for item in entry["amendments"]))
        self.assertEqual("unreachable", entry["amendments"][0]["relay_status"])

        # A failed relay earns the first rung of the backoff, so the sweep
        # that follows it immediately leaves it alone rather than spending a
        # send per wake on a supervisor nobody can reach.
        held = self.flush()
        self.assertEqual(0, held["attempted"])
        self.assertEqual(3, held["deferred"])
        self.assertEqual(3, len(self.sends))

        self.status = "delivered-woke"
        self.assertEqual(3, self.flush(after_ms=intake_mod.RELAY_BACKOFF_MS[0])["delivered"])
        keys = [send["key"] for send in self.sends]
        self.assertEqual(keys[:3], keys[3:])

    def test_one_delivery_runs_at_a_time(self) -> None:
        """Two flushes racing would put one amendment on the wire twice."""
        self.name_a_supervisor()
        self.spool.ensure()
        holder = os.open(self.spool.root / "flush.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            outcome = self.flush()
        finally:
            os.close(holder)
        self.assertEqual(0, outcome["delivered"])
        self.assertIn("already running", outcome["reason"])
        self.assertEqual([], self.sends)


class ProducerRuleTests(unittest.TestCase):
    """The rules both providers share, without a subprocess in sight."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".session-kit-rules-", dir=REPO
        )
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir(mode=0o700, parents=True)
        self.spool = Spool(self.state)

    def tearDown(self) -> None:
        remove_sandbox(self.temporary)

    def test_a_prompt_is_a_project_or_it_is_not(self) -> None:
        self.assertEqual(PROJECT, substantive_prompt(PROJECT))
        self.assertEqual("fix the redirects", substantive_prompt("  fix the\n redirects "))
        for refused in (
            "",
            "   ",
            "hi",
            "ok",
            "/status",
            "Continue from where you left off.",
            "You are a Session Kit delivery runner. Deliver one message.",
            '<cross-session-message from="uds:/tmp/x.sock">'
            "[session-kit operator message deadbeef]worker assignment",
            "RUNTIME FOR THIS WAKE (ground truth, read off the machine): bot task",
            "[session-kit operator message 4f3a2b1c]\n---\nstatus please",
            None,
            42,
        ):
            self.assertEqual("", substantive_prompt(refused), refused)

    def test_legacy_delivery_intakes_are_retired_without_sending(self) -> None:
        machine = produce(
            self.spool,
            thread_key=CLAUDE_KEY,
            prompt=PROJECT,
            source_event_id=SOURCE_EVENT_A,
            source_digest=intake_mod.prompt_digest(PROJECT),
        )["entry"]
        machine["summary"] = (
            "You are a Session Kit delivery runner. Deliver one operator message."
        )
        self.spool.write_entry(machine)
        wrapped = produce(
            self.spool,
            thread_key="claude:12345678-1234-4234-8234-123456789abc",
            prompt=PROJECT,
            turn_id="turn-wrapped",
            source_event_id="c" * 64,
            source_digest=intake_mod.prompt_digest(PROJECT),
        )["entry"]
        wrapped["summary"] = (
            '<cross-session-message from="uds:/tmp/x.sock">'
            "[session-kit operator message deadbeef]worker assignment"
        )
        self.spool.write_entry(wrapped)
        wake = produce(
            self.spool,
            thread_key="codex:12345678-1234-4234-8234-123456789abc",
            prompt=PROJECT,
            turn_id="turn-wake",
            source_event_id="d" * 64,
            source_digest=intake_mod.prompt_digest(PROJECT),
        )["entry"]
        wake["summary"] = (
            "RUNTIME FOR THIS WAKE (ground truth, read off the machine): bot task"
        )
        self.spool.write_entry(wake)
        legitimate = produce(
            self.spool,
            thread_key=CODEX_KEY,
            prompt="audit the real project queue",
            turn_id="turn-real",
            source_event_id=SOURCE_EVENT_B,
            source_digest=intake_mod.prompt_digest("audit the real project queue"),
        )["entry"]

        outcome = intake_mod.dismiss_machine_intakes(self.spool)
        self.assertEqual(
            {"dismissed": 3, "examined": 4, "messages_sent": 0}, outcome
        )
        self.assertEqual("reported", self.spool.read_entry(machine["msg_id"])["state"])
        self.assertEqual("reported", self.spool.read_entry(wrapped["msg_id"])["state"])
        self.assertEqual("reported", self.spool.read_entry(wake["msg_id"])["state"])
        self.assertEqual(
            [legitimate["msg_id"]],
            [row["msg_id"] for row in self.spool.open_entries()],
        )

    def test_payload_fields_take_only_a_root_thread_s_own_prompt(self) -> None:
        good = {"provider": "claude", "session_id": CLAUDE_UUID, "prompt": PROJECT}
        self.assertEqual(CLAUDE_KEY, payload_fields(good)["thread_key"])
        for marker in intake_mod.SIDECHAIN_MARKERS:
            self.assertEqual({}, payload_fields({**good, marker: "x"}))
        self.assertEqual({}, payload_fields({**good, "isSidechain": True}))
        self.assertEqual({}, payload_fields({**good, "provider": "shell"}))
        self.assertEqual({}, payload_fields({**good, "session_id": "nope"}))
        self.assertEqual({}, payload_fields({**good, "prompt": "/clear"}))
        self.assertEqual({}, payload_fields("not a payload"))

    def test_the_two_paths_write_the_same_entry_shape(self) -> None:
        """One spool, one format: a replacement reads both the same way."""
        produce(
            self.spool,
            thread_key=CLAUDE_KEY,
            prompt=PROJECT,
            source_event_id=SOURCE_EVENT_A,
            source_digest=intake_mod.prompt_digest(PROJECT),
        )
        recorded = intake_mod.record(
            self.spool,
            msg_id="4f3a2b1c",
            source=CODEX_KEY,
            summary="a project that arrived as a message",
        )
        automatic = next(
            entry for entry in self.spool.open_entries() if entry["origin"] == "auto"
        )
        self.assertEqual(sorted(recorded["entry"]), sorted(automatic))
        self.assertEqual("message", recorded["entry"]["origin"])

    def test_the_intake_is_durable_before_the_supervisor_is_asked_for(self) -> None:
        """Order is the contract: a supervisor that never starts loses nothing."""
        seen: list[tuple[bool, list[str]]] = []
        environ = {"SESSION_KIT_SUPERVISOR_BIN": sys.executable}

        def spawn(argv: list[str]) -> None:
            seen.append((bool(Spool(self.state).open_entries()), list(argv)))

        outcome = intake_mod.from_hook(
            {"provider": "claude", "session_id": CLAUDE_UUID, "prompt": PROJECT},
            state_dir=self.state,
            environ=environ,
            spawn=spawn,
        )
        self.assertTrue(outcome["produced"])
        self.assertTrue(outcome["supervisor"]["requested"])
        self.assertEqual(
            [
                (True, [sys.executable, "ensure"]),
                (
                    True,
                    [sys.executable, os.fspath(REPO / "lib/session_inventory.py"),
                     "msg", "intake", "flush"],
                ),
            ],
            seen,
        )

    def test_managed_worker_bootstrap_never_becomes_a_project(self) -> None:
        outcome = intake_mod.from_hook(
            {
                "provider": "codex",
                "session_id": CODEX_UUID,
                "turn_id": "worker-bootstrap",
                "prompt": "Session Kit initialized this managed worker.",
            },
            state_dir=self.state,
            environ={
                "SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY": "worker:operations:1"
            },
        )
        self.assertFalse(outcome["produced"])
        self.assertEqual(intake_mod.REFUSED, outcome["action"])
        self.assertEqual([], self.spool.entry_ids())
        self.assertEqual([], list((self.state / "supervisor" / "source-events" / "entries").glob("*.json")))

    def test_relay_landing_after_an_amendment_cannot_advance_new_generation(self) -> None:
        initial = produce(
            self.spool, thread_key=CLAUDE_KEY, prompt=PROJECT,
            source_event_id=SOURCE_EVENT_A,
            source_digest=intake_mod.prompt_digest(PROJECT),
        )["entry"]

        def amend_while_sending(**fields: object) -> dict:
            prompt = "also verify the generation-bound relay"
            produce(
                self.spool, thread_key=CLAUDE_KEY, prompt=prompt,
                source_event_id=SOURCE_EVENT_B,
                source_digest=intake_mod.prompt_digest(prompt),
            )
            return {
                "msg_id": "aa11bb22",
                "targets": [{"thread_key": fields["thread_key"], "status": "delivered-woke"}],
            }

        code, stale = intake_mod.relay(
            self.spool, msg_id=initial["msg_id"], text="work completed",
            kind="completion", deliver=amend_while_sending,
        )
        self.assertEqual(0, code)
        self.assertTrue(stale["note"]["stale_generation"])
        self.assertEqual("received", stale["entry"]["state"])

        code, current = intake_mod.relay(
            self.spool, msg_id=initial["msg_id"], text="work completed",
            kind="completion",
            deliver=lambda **fields: {
                "msg_id": "bb22cc33",
                "targets": [{"thread_key": fields["thread_key"], "status": "delivered-woke"}],
            },
        )
        self.assertEqual(0, code)
        self.assertFalse(current["note"]["stale_generation"])
        self.assertEqual("reported", current["entry"]["state"])
        self.assertEqual(2, len(current["entry"]["notes"]))

    def test_one_ensure_covers_the_roots_that_follow_it(self) -> None:
        calls: list[list[str]] = []
        environ = {"SESSION_KIT_SUPERVISOR_BIN": sys.executable}
        clock = iter([1_700_000_000.0, 1_700_000_000.5, 1_700_000_100.0])

        def spawn(argv: list[str]) -> None:
            calls.append(list(argv))

        first = request_supervisor(
            self.state, environ=environ, spawn=spawn, clock=lambda: next(clock)
        )
        second = request_supervisor(
            self.state, environ=environ, spawn=spawn, clock=lambda: next(clock)
        )
        third = request_supervisor(
            self.state, environ=environ, spawn=spawn, clock=lambda: next(clock)
        )
        self.assertTrue(first["requested"])
        self.assertFalse(second["requested"])
        self.assertIn("cooldown", second["reason"])
        # Past the cooldown a later root may ask again: the supervisor it asked
        # for a minute ago may be gone.
        self.assertTrue(third["requested"])
        self.assertEqual(2, len(calls))
        self.assertGreater(ENSURE_COOLDOWN_MS, 1_000)

    def test_a_missing_supervisor_command_is_reported_not_raised(self) -> None:
        outcome = request_supervisor(
            self.state,
            environ={"SESSION_KIT_SUPERVISOR_BIN": os.fspath(self.state / "nothing")},
            spawn=lambda argv: self.fail("nothing should have been started"),
        )
        self.assertFalse(outcome["requested"])
        self.assertIn("unavailable", outcome["reason"])

    def test_an_automatic_intake_is_acknowledged_on_its_source_thread(self) -> None:
        """Nobody sent it, so there is no message to reply to."""
        sends: list[dict] = []

        def deliver(*, thread_key: str, text: str, key: str) -> dict:
            sends.append({"thread_key": thread_key, "text": text, "key": key})
            return {
                "msg_id": "aa11bb22",
                "targets": [{"thread_key": thread_key, "status": "delivered-woke"}],
            }

        def reply(**_fields: object) -> dict:
            self.fail("an automatic intake has no message to reply to")

        produced = produce(
            self.spool,
            thread_key=CLAUDE_KEY,
            prompt=PROJECT,
            source_event_id=SOURCE_EVENT_A,
            source_digest=intake_mod.prompt_digest(PROJECT),
        )
        code, payload = intake_mod.run(
            "ack",
            spool=self.spool,
            deliver=deliver,
            reply=reply,
            msg_id=produced["entry"]["msg_id"],
            text="taken; two workers on it",
        )
        self.assertEqual(0, code)
        self.assertTrue(payload["acknowledged"])
        self.assertEqual("acknowledged", payload["entry"]["state"])
        self.assertEqual(1, len(sends))
        self.assertEqual(CLAUDE_KEY, sends[0]["thread_key"])

    def test_progress_and_completion_relay_the_exact_source_event_id(self) -> None:
        sends: list[dict] = []

        def deliver(*, thread_key: str, text: str, key: str) -> dict:
            sends.append({"thread_key": thread_key, "text": text, "key": key})
            return {
                "msg_id": "aa11bb22",
                "targets": [{"thread_key": thread_key, "status": "delivered-woke"}],
            }

        produced = produce(
            self.spool,
            thread_key=CLAUDE_KEY,
            prompt=PROJECT,
            source_event_id=SOURCE_EVENT_A,
            source_digest=intake_mod.prompt_digest(PROJECT),
        )
        for action in ("progress", "complete"):
            code, payload = intake_mod.run(
                action,
                spool=self.spool,
                deliver=deliver,
                reply=lambda **_fields: {},
                msg_id=produced["entry"]["msg_id"],
                text=f"{action} text",
            )
            self.assertEqual(0, code)
            self.assertEqual(SOURCE_EVENT_A, payload["note"]["source_event_id"])
            self.assertIn(f"source_event_id {SOURCE_EVENT_A}", sends[-1]["text"])
        self.assertEqual("reported", payload["entry"]["state"])


if __name__ == "__main__":
    unittest.main()
