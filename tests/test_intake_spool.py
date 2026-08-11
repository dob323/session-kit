"""The supervisor's intake spool: one project, one entry, one report home.

Every test runs against a disposable state directory, and both relay paths are
injected, so nothing here reaches a session, a socket, or a real home. What is
under test is the record: that one intake is recognised however often it
arrives, that its lifecycle only moves forward, and that a note the source
never received is still owed afterwards.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor import intake as intake_mod  # noqa: E402
from sessionkit_supervisor.intake import (  # noqa: E402
    MAX_ENTRIES_KEPT,
    RETENTION_MS,
    IntakeError,
    Spool,
    relay_text,
    run,
)

CORE = REPO / "lib" / "session_inventory.py"
SOURCE_UUID = "dcbdf940-4eda-4967-8e41-23a5760c32b5"
OTHER_UUID = "019fdf1e-8b4c-7573-a089-be495bfece6a"
SOURCE_KEY = f"claude:{SOURCE_UUID}"
OTHER_KEY = f"codex:{OTHER_UUID}"
INTAKE_A = "4f3a2b1c"
INTAKE_B = "9a8b7c6d"
INTAKE_KEY = "project-intake:7:sitemap"


class Clock:
    """A clock that only ever moves, so every stamp is orderable."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        self.value += 1.0
        return self.value


class Relay:
    """The messaging core, recorded rather than run."""

    def __init__(self, *, status: str = "delivered-woke", ok: bool = True) -> None:
        self.status = status
        self.ok = ok
        self.msg_id = "aa11bb22"
        self.sends: list[dict[str, str]] = []
        self.replies: list[dict[str, str]] = []

    def deliver(self, *, thread_key: str, text: str, key: str) -> dict:
        self.sends.append({"thread_key": thread_key, "text": text, "key": key})
        return {
            "msg_id": self.msg_id,
            "targets": [
                {"thread_key": thread_key, "status": self.status, "detail": "test"}
            ],
        }

    def reply(self, *, msg_id: str, text: str) -> dict:
        self.replies.append({"msg_id": msg_id, "text": text})
        return {
            "ok": self.ok,
            "msg_id": msg_id,
            "reason": "" if self.ok else "this session was not a target of that message",
        }


class Sandbox:
    """A disposable SK_STATE_DIR with no relationship to the real home."""

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".session-kit-intake-", dir=REPO
        )
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)

    def close(self) -> None:
        self.temporary.cleanup()


class IntakeCase(unittest.TestCase):
    """One spool, one recorded relay, one moving clock."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.spool = Spool(self.sandbox.state)
        self.relay = Relay()
        self.clock = Clock()

    def tearDown(self) -> None:
        self.sandbox.close()

    def call(self, action: str, **fields: object) -> tuple[int, dict]:
        code, payload = run(
            action,
            spool=self.spool,
            deliver=self.relay.deliver,
            reply=self.relay.reply,
            clock=self.clock,
            **fields,
        )
        assert isinstance(payload, dict)
        return code, payload

    def record(self, msg_id: str = INTAKE_A, **fields: object) -> dict:
        arguments = {"source": SOURCE_KEY, "summary": "rebuild the sitemap"}
        arguments.update(fields)
        _code, payload = self.call("record", msg_id=msg_id, **arguments)
        return payload

    def prepare_delegation(self, *branches: str, msg_id: str = INTAKE_A) -> None:
        supervisor = self.sandbox.state / "supervisor"
        supervisor.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity = supervisor / "identity"
        identity.write_text(f"{OTHER_KEY}\n", encoding="utf-8")
        identity.chmod(0o600)
        plan = [
            {
                "branch": branch,
                "idempotency_key": f"worker:{msg_id}:{index}",
                "workstream": branch,
                "scope": f"complete {branch} scope",
                "provider": "codex",
                "requested_model": "gpt-test",
                "expertise": "testing",
                "rationale": "isolated test assignment",
                "task_text": f"carry out the {branch} fixture work",
                "acceptance_criteria": "the fixture assertions pass",
                "deliverable": "a note saying what the fixture proved",
            }
            for index, branch in enumerate(dict.fromkeys(branches), 1)
        ]
        intake_mod.preflight(
            self.spool,
            msg_id=msg_id,
            source_event_id=None,
            analysis="analyzed the complete intake and current state",
            scope="the exact fixture scope",
            required_expertise="test worker expertise",
            required_expertise_tags=("testing",),
            worker_plan=plan,
            risks="duplicate launch and incomplete work",
            tests="verify durable lifecycle state",
            manual_policy_exception="legacy manual fixture uses one Codex test worker",
            state_dir=self.sandbox.state,
            clock=self.clock,
        )

    @staticmethod
    def launch(_assignment: dict) -> None:
        return None

    @staticmethod
    def reconcile(assignment: dict) -> dict:
        return {
            "provider": assignment["provider"],
            "actual_model": assignment["requested_model"],
            "worker_identity": OTHER_KEY,
            "inventory_verified": True,
            "launch_idempotency_key": assignment["idempotency_key"],
        }


class SpoolStateTests(IntakeCase):
    def _entry(self, msg_id: str = INTAKE_A, **fields: object) -> dict:
        stamp = int(self.clock() * 1000)
        record = {
            "msg_id": msg_id,
            "intake_key": None,
            "also_delivered_as": [],
            "source_thread_key": SOURCE_KEY,
            "source_terminal": 7,
            "source_title": "Sitemap rebuild",
            "summary": "rebuild the sitemap",
            "state": "received",
            "received_unix_ms": stamp,
            "updated_unix_ms": stamp,
            "acknowledged_unix_ms": None,
            "delegated_unix_ms": None,
            "reported_unix_ms": None,
            "workers": [],
            "notes": [],
        }
        record.update(fields)
        return record

    def test_the_spool_is_owner_only_and_under_the_supplied_state_dir(self) -> None:
        self.spool.ensure()
        self.assertEqual(
            self.sandbox.state / "supervisor" / "intake", self.spool.root
        )
        for directory in (
            self.spool.root,
            self.spool.entries,
            self.spool.keys,
            self.spool.aliases,
        ):
            self.assertEqual(0o700, stat.S_IMODE(directory.lstat().st_mode))
        self.spool.write_entry(self._entry())
        self.assertEqual(
            0o600, stat.S_IMODE(self.spool.entry_path(INTAKE_A).lstat().st_mode)
        )

    def test_reading_an_empty_spool_neither_fails_nor_creates_it(self) -> None:
        self.assertEqual([], self.spool.entry_ids())
        self.assertEqual([], self.spool.open_entries())
        self.assertFalse(self.spool.root.exists())

    def test_a_bad_message_id_or_key_never_reaches_the_filesystem(self) -> None:
        for bad in ("../../etc/passwd", "", "not-hex", "4F3A2B1C9"):
            with self.assertRaises(IntakeError):
                self.spool.entry_path(bad)
        for bad in ("../escape", ".hidden", "with/slash", ""):
            with self.assertRaises(IntakeError):
                self.spool.key_path(bad)

    def test_a_damaged_entry_is_an_error_not_an_absent_intake(self) -> None:
        """A spool that reads a broken entry as "no such intake" delegates twice."""
        self.spool.write_entry(self._entry())
        path = self.spool.entry_path(INTAKE_A)
        path.chmod(0o644)
        with self.assertRaises(IntakeError):
            self.spool.read_entry(INTAKE_A)
        path.chmod(0o600)
        path.write_text('{"msg_id": "4f3a2b1c"', encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaises(IntakeError):
            self.spool.read_entry(INTAKE_A)

    def test_a_symlinked_entry_is_never_read(self) -> None:
        self.spool.ensure()
        outside = self.sandbox.base / "outside.json"
        outside.write_text(json.dumps(self._entry()), encoding="utf-8")
        self.spool.entry_path(INTAKE_A).symlink_to(outside)
        with self.assertRaises(IntakeError):
            self.spool.read_entry(INTAKE_A)

    def test_an_invalid_entry_is_refused_before_it_is_written(self) -> None:
        for broken in (
            self._entry(state="finished"),
            self._entry(source_thread_key="terminal 7"),
            self._entry(msg_id="not-hex"),
            self._entry(received_unix_ms=0),
            self._entry(notes=[{"seq": 2, "kind": "progress", "via": "send",
                                "text": "x", "landed": False}]),
        ):
            with self.assertRaises(IntakeError):
                self.spool.write_entry(broken)
        self.assertEqual([], self.spool.entry_ids())

    def test_writes_are_atomic_and_leave_no_temporary_behind(self) -> None:
        self.spool.write_entry(self._entry())
        self.spool.write_entry(self._entry(summary="second pass"))
        leftovers = [
            name for name in os.listdir(self.spool.entries) if name.startswith(".")
        ]
        self.assertEqual([], leftovers)
        stored = self.spool.read_entry(INTAKE_A)
        assert stored is not None
        self.assertEqual("second pass", stored["summary"])

    def test_retention_drops_reported_intakes_and_pointers_to_nothing(self) -> None:
        now = 1_900_000_000_000
        old = now - RETENTION_MS - 86_400_000
        self.spool.write_entry(
            self._entry(
                state="reported",
                received_unix_ms=old,
                updated_unix_ms=old,
                acknowledged_unix_ms=old,
                reported_unix_ms=old,
            )
        )
        self.spool.write_entry(
            self._entry(INTAKE_B, received_unix_ms=old, updated_unix_ms=old)
        )
        self.spool.claim_key(INTAKE_KEY, INTAKE_A)
        with self.spool.locked():
            dropped = self.spool.prune(now)
        self.assertEqual([INTAKE_A], dropped["entries"])
        # The unfinished one stays: age is not a report to its source.
        self.assertEqual([INTAKE_B], self.spool.entry_ids())
        self.assertEqual([INTAKE_KEY], dropped["pointers"])
        self.assertEqual("", self.spool.msg_id_for_key(INTAKE_KEY))

    def test_retention_keeps_the_newest_reported_intakes(self) -> None:
        now = 1_900_000_000_000
        kept = f"{MAX_ENTRIES_KEPT:08x}"
        self.spool.write_entry(
            self._entry(
                kept,
                state="reported",
                received_unix_ms=now - 1000,
                updated_unix_ms=now - 1000,
                reported_unix_ms=now - 1000,
            )
        )
        with self.spool.locked():
            dropped = self.spool.prune(now)
        self.assertEqual([], dropped["entries"])


class IntakeLifecycleTests(IntakeCase):
    def test_recording_an_intake_starts_it_received(self) -> None:
        payload = self.record(intake_key=INTAKE_KEY, terminal=7, title="Sitemap")
        entry = payload["entry"]
        self.assertTrue(payload["recorded"])
        self.assertFalse(payload["duplicate"])
        self.assertEqual("received", entry["state"])
        self.assertEqual(SOURCE_KEY, entry["source_thread_key"])
        self.assertEqual(7, entry["source_terminal"])
        self.assertEqual(INTAKE_KEY, entry["intake_key"])
        self.assertEqual([], entry["workers"])
        self.assertEqual(entry["received_unix_ms"], entry["updated_unix_ms"])
        self.assertIsNone(entry["acknowledged_unix_ms"])

    def test_an_intake_needs_an_exact_id_and_an_exact_source(self) -> None:
        with self.assertRaises(IntakeError):
            self.record("not-hex")
        with self.assertRaises(IntakeError):
            self.record(source="terminal 7")
        self.assertEqual([], self.spool.entry_ids())

    def test_the_same_message_delivered_twice_is_one_intake(self) -> None:
        first = self.record(intake_key=INTAKE_KEY)
        second = self.record(intake_key=INTAKE_KEY)
        self.assertTrue(second["duplicate"])
        self.assertFalse(second["recorded"])
        self.assertEqual(INTAKE_A, second["duplicate_of"])
        self.assertEqual(
            first["entry"]["received_unix_ms"],
            second["entry"]["received_unix_ms"],
        )
        self.assertEqual([INTAKE_A], self.spool.entry_ids())

    def test_the_same_intake_key_under_a_new_message_id_is_one_intake(self) -> None:
        """The sender retried and got a second id; the project is still one."""
        self.record(INTAKE_A, intake_key=INTAKE_KEY)
        second = self.record(INTAKE_B, intake_key=INTAKE_KEY)
        self.assertTrue(second["duplicate"])
        self.assertEqual(INTAKE_A, second["duplicate_of"])
        self.assertEqual([INTAKE_B], second["entry"]["also_delivered_as"])
        self.assertEqual([INTAKE_A], self.spool.entry_ids())
        # Every later verb answers to the id the resident actually received.
        self.assertEqual(INTAKE_A, self.spool.resolve(INTAKE_B))

    def test_an_intake_with_no_key_is_deduplicated_by_message_id_alone(self) -> None:
        self.record(INTAKE_A)
        self.record(INTAKE_B)
        self.assertEqual(sorted((INTAKE_A, INTAKE_B)), self.spool.entry_ids())

    def test_acknowledging_replies_once_and_records_the_answer(self) -> None:
        self.record(intake_key=INTAKE_KEY)
        code, payload = self.call("ack", msg_id=INTAKE_A, text="taken; two workers")
        self.assertEqual(0, code)
        self.assertTrue(payload["acknowledged"])
        entry = payload["entry"]
        self.assertEqual("acknowledged", entry["state"])
        self.assertIsNotNone(entry["acknowledged_unix_ms"])
        self.assertEqual(1, len(entry["notes"]))
        note = entry["notes"][0]
        self.assertEqual(("acknowledgement", "reply", True), (note["kind"], note["via"], note["landed"]))
        self.assertEqual([{"msg_id": INTAKE_A, "text": "taken; two workers"}], self.relay.replies)

        code, again = self.call("ack", msg_id=INTAKE_A, text="taken; two workers")
        self.assertEqual(0, code)
        self.assertTrue(again["duplicate"])
        self.assertFalse(again["acknowledged"])
        self.assertEqual(1, len(self.relay.replies))
        self.assertEqual(1, len(again["entry"]["notes"]))

    def test_an_acknowledgement_answers_the_id_the_resident_received(self) -> None:
        self.record(INTAKE_A, intake_key=INTAKE_KEY)
        self.record(INTAKE_B, intake_key=INTAKE_KEY)
        code, payload = self.call("ack", msg_id=INTAKE_B, text="taken")
        self.assertEqual(0, code)
        self.assertEqual(INTAKE_A, payload["entry"]["msg_id"])
        self.assertEqual([{"msg_id": INTAKE_B, "text": "taken"}], self.relay.replies)

    def test_a_reply_that_was_not_recorded_leaves_the_intake_unacknowledged(self) -> None:
        self.record()
        self.relay.ok = False
        code, payload = self.call("ack", msg_id=INTAKE_A, text="taken")
        self.assertEqual(1, code)
        self.assertFalse(payload["acknowledged"])
        self.assertIn("not a target", payload["reason"])
        self.assertEqual("received", payload["entry"]["state"])
        self.assertEqual([], payload["entry"]["notes"])

    def test_delegating_records_each_branch_once_and_moves_the_state(self) -> None:
        self.record()
        self.prepare_delegation("agent-a", "agent-b")
        _code, first = self.call(
            "delegate",
            msg_id=INTAKE_A,
            branches=("agent-a", "agent-b", "agent-a"),
            launcher=self.launch,
            reconciler=self.reconcile,
        )
        self.assertEqual(["agent-a", "agent-b"], first["delegated"])
        self.assertEqual("delegated", first["entry"]["state"])
        _code, second = self.call(
            "delegate", msg_id=INTAKE_A, branches=("agent-b",), launcher=self.launch,
            reconciler=self.reconcile,
        )
        self.assertEqual([], second["delegated"])
        self.assertEqual(["agent-b"], second["already_recorded"])
        self.assertEqual(
            ["agent-a", "agent-b"],
            [worker["branch"] for worker in second["entry"]["workers"]],
        )
        self.assertEqual(
            first["entry"]["delegated_unix_ms"],
            second["entry"]["delegated_unix_ms"],
        )

    def test_delegating_refuses_a_branch_that_is_not_a_plain_ref(self) -> None:
        self.record()
        for bad in ("-dash-first", "space here", "a" * 129, ""):
            with self.assertRaises(IntakeError):
                self.call("delegate", msg_id=INTAKE_A, branches=(bad,))
        with self.assertRaises(IntakeError):
            self.call("delegate", msg_id=INTAKE_A, branches=())

    def test_a_verb_naming_an_unrecorded_intake_is_refused(self) -> None:
        for action, fields in (
            ("ack", {"text": "hello"}),
            ("delegate", {"branches": ("agent-a",)}),
            ("progress", {"text": "halfway"}),
            ("complete", {"text": "done"}),
        ):
            with self.assertRaises(IntakeError):
                self.call(action, msg_id=INTAKE_A, **fields)
        with self.assertRaises(IntakeError):
            self.call("archive", msg_id=INTAKE_A)

    def test_a_progress_note_is_relayed_once_and_recorded(self) -> None:
        self.record()
        code, payload = self.call("progress", msg_id=INTAKE_A, text="two branches open")
        self.assertEqual(0, code)
        self.assertTrue(payload["relayed"])
        note = payload["note"]
        self.assertEqual("progress", note["kind"])
        self.assertEqual("delivered-woke", note["relay_status"])
        self.assertEqual("test", note["relay_detail"])
        self.assertEqual("intake-note:4f3a2b1c:1", note["relay_key"])
        self.assertEqual("aa11bb22", note["relay_msg_id"])
        self.assertEqual(1, len(self.relay.sends))
        sent = self.relay.sends[0]
        self.assertEqual(SOURCE_KEY, sent["thread_key"])
        self.assertEqual("intake-note:4f3a2b1c:1", sent["key"])
        self.assertEqual(
            relay_text("progress", INTAKE_A, "two branches open"), sent["text"]
        )
        # A progress note leaves the project where it was.
        self.assertEqual("received", payload["entry"]["state"])

        code, repeat = self.call("progress", msg_id=INTAKE_A, text="two branches open")
        self.assertEqual(0, code)
        self.assertTrue(repeat["duplicate"])
        self.assertFalse(repeat["relayed"])
        self.assertEqual(1, len(self.relay.sends))
        self.assertEqual(1, len(repeat["entry"]["notes"]))

    def test_the_relayed_note_names_its_author_rather_than_the_operator(self) -> None:
        text = relay_text("completion", INTAKE_A, "sitemap is live")
        self.assertTrue(text.startswith("Fleet Supervisor completion report"))
        self.assertIn(INTAKE_A, text)
        self.assertIn("sitemap is live", text)

    def test_different_words_are_a_different_note(self) -> None:
        self.record()
        self.call("progress", msg_id=INTAKE_A, text="branches open")
        _code, payload = self.call("progress", msg_id=INTAKE_A, text="tests green")
        self.assertEqual(2, len(self.relay.sends))
        self.assertEqual(
            ["intake-note:4f3a2b1c:1", "intake-note:4f3a2b1c:2"],
            [send["key"] for send in self.relay.sends],
        )
        self.assertEqual([1, 2], [note["seq"] for note in payload["entry"]["notes"]])

    def test_a_note_that_did_not_land_is_still_owed_and_retries_under_its_key(self) -> None:
        self.record()
        self.relay.status = "unreachable"
        code, payload = self.call("progress", msg_id=INTAKE_A, text="two branches open")
        self.assertEqual(1, code)
        self.assertFalse(payload["relayed"])
        note = payload["note"]
        self.assertFalse(note["landed"])
        self.assertEqual("unreachable", note["relay_status"])
        self.assertEqual("test", note["relay_detail"])
        self.assertIsNone(note["relayed_unix_ms"])

        self.relay.status = "delivered-woke"
        code, retried = self.call("progress", msg_id=INTAKE_A, text="two branches open")
        self.assertEqual(0, code)
        self.assertTrue(retried["relayed"])
        self.assertEqual(1, len(retried["entry"]["notes"]))
        self.assertEqual(
            ["intake-note:4f3a2b1c:1", "intake-note:4f3a2b1c:1"],
            [send["key"] for send in self.relay.sends],
        )

    def test_completion_closes_the_intake_only_when_it_lands(self) -> None:
        self.record()
        self.relay.status = "failed"
        code, payload = self.call("complete", msg_id=INTAKE_A, text="sitemap is live")
        self.assertEqual(1, code)
        self.assertEqual("received", payload["entry"]["state"])
        self.assertIsNone(payload["entry"]["reported_unix_ms"])
        self.assertEqual(1, len(self.spool.open_entries()))

        self.relay.status = "landed-unconfirmed"
        code, closed = self.call("complete", msg_id=INTAKE_A, text="sitemap is live")
        self.assertEqual(0, code)
        self.assertEqual("reported", closed["entry"]["state"])
        self.assertIsNotNone(closed["entry"]["reported_unix_ms"])
        self.assertEqual([], self.spool.open_entries())

    def test_a_reported_intake_refuses_new_workers(self) -> None:
        self.record()
        self.call("complete", msg_id=INTAKE_A, text="done")
        with self.assertRaises(IntakeError):
            self.call("delegate", msg_id=INTAKE_A, branches=("agent-c",))

    def test_the_lifecycle_only_moves_forward(self) -> None:
        self.record()
        self.call("ack", msg_id=INTAKE_A, text="taken")
        self.prepare_delegation("agent-a")
        self.call(
            "delegate", msg_id=INTAKE_A, branches=("agent-a",), launcher=self.launch,
            reconciler=self.reconcile,
        )
        _code, payload = self.call("progress", msg_id=INTAKE_A, text="halfway")
        entry = payload["entry"]
        self.assertEqual("delegated", entry["state"])
        self.assertLess(entry["received_unix_ms"], entry["acknowledged_unix_ms"])
        self.assertLess(entry["acknowledged_unix_ms"], entry["delegated_unix_ms"])
        _code, done = self.call("complete", msg_id=INTAKE_A, text="done")
        self.assertEqual("reported", done["entry"]["state"])
        self.assertLess(
            done["entry"]["delegated_unix_ms"], done["entry"]["reported_unix_ms"]
        )

    def test_a_fresh_resident_reads_the_unfinished_intakes_oldest_first(self) -> None:
        """The resident is gone; the spool is all its replacement has."""
        self.record(INTAKE_A)
        self.record(INTAKE_B, source=OTHER_KEY, summary="ledger audit")
        self.prepare_delegation("agent-a")
        self.call(
            "delegate", msg_id=INTAKE_A, branches=("agent-a",), launcher=self.launch,
            reconciler=self.reconcile,
        )
        self.call("complete", msg_id=INTAKE_B, text="audit posted")

        replacement = Spool(self.sandbox.state)
        code, payload = run(
            "open",
            spool=replacement,
            deliver=self.relay.deliver,
            reply=self.relay.reply,
            clock=self.clock,
        )
        self.assertEqual(0, code)
        self.assertEqual(1, payload["count"])
        self.assertFalse(payload["truncated"])
        waiting = payload["open"][0]
        self.assertEqual(INTAKE_A, waiting["msg_id"])
        self.assertEqual("delegated", waiting["state"])
        self.assertEqual(["agent-a"], [w["branch"] for w in waiting["workers"]])
        self.assertGreater(payload["as_of_unix_ms"], waiting["received_unix_ms"])

    def test_the_open_list_is_read_live_on_every_call(self) -> None:
        self.record(INTAKE_A)
        first = intake_mod.open_intakes(self.spool, clock=self.clock)
        self.call("complete", msg_id=INTAKE_A, text="done")
        second = intake_mod.open_intakes(self.spool, clock=self.clock)
        self.assertEqual(1, first["count"])
        self.assertEqual(0, second["count"])
        self.assertGreater(second["as_of_unix_ms"], first["as_of_unix_ms"])


class FacadeIntakeTests(unittest.TestCase):
    """The intake verbs through the real facade process, in a sandboxed home."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.shpool_json = self.sandbox.base / "shpool.json"
        self.shpool_json.write_text(json.dumps({"sessions": []}), encoding="utf-8")
        self.agents_json = self.sandbox.base / "agents.json"
        self.agents_json.write_text("[]", encoding="utf-8")

    def tearDown(self) -> None:
        self.sandbox.close()

    def run_intake(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CORE), "msg", "intake", *arguments],
            cwd=REPO,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": str(self.sandbox.home),
                "SESSION_KIT_STATE_DIR": str(self.sandbox.state),
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_SHPOOL_JSON_FILE": str(self.shpool_json),
                "SESSION_KIT_CLAUDE_JSON_FILE": str(self.agents_json),
                "SESSION_KIT_CODEX_AUTOTITLE": "0",
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_the_facade_records_an_intake_and_lists_it_as_machine_json(self) -> None:
        empty = self.run_intake("open")
        self.assertEqual(0, empty.returncode, empty.stderr)
        self.assertEqual(0, json.loads(empty.stdout)["count"])

        created = self.run_intake(
            "record",
            "--msg-id",
            INTAKE_A,
            "--source",
            SOURCE_KEY,
            "--key",
            INTAKE_KEY,
            "--summary",
            "rebuild the sitemap",
            "--terminal",
            "7",
            "--title",
            "Sitemap rebuild",
        )
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertEqual("", created.stderr)
        self.assertTrue(json.loads(created.stdout)["recorded"])

        repeated = self.run_intake(
            "record", "--msg-id", INTAKE_B, "--source", SOURCE_KEY, "--key", INTAKE_KEY
        )
        self.assertEqual(0, repeated.returncode, repeated.stderr)
        self.assertEqual(INTAKE_A, json.loads(repeated.stdout)["duplicate_of"])

        listed = self.run_intake("open")
        payload = json.loads(listed.stdout)
        self.assertEqual(1, payload["count"])
        self.assertEqual(7, payload["open"][0]["source_terminal"])

    def test_the_real_facade_registers_the_detached_flush_verb(self) -> None:
        completed = self.run_intake("flush")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            {
                "delivered": 0,
                "attempted": 0,
                "pending": 0,
                "deferred": 0,
                "reason": "nothing is owed",
            },
            json.loads(completed.stdout),
        )

    def test_the_real_facade_registers_machine_intake_cleanup(self) -> None:
        completed = self.run_intake("dismiss-machine")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            {"dismissed": 0, "examined": 0, "messages_sent": 0},
            json.loads(completed.stdout),
        )

    def test_the_facade_takes_a_hook_payload_and_produces_one_intake(self) -> None:
        """The same door the provider hooks use, for anything that cannot import."""
        payload = json.dumps(
            {
                "provider": "claude",
                "session_id": SOURCE_UUID,
                "prompt": "rebuild the sitemap generator and ship it",
            }
        )
        produced = subprocess.run(
            [sys.executable, str(CORE), "msg", "intake", "from-hook"],
            cwd=REPO,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": str(self.sandbox.home),
                "SESSION_KIT_STATE_DIR": str(self.sandbox.state),
                "SESSION_KIT_SUPERVISOR_BIN": str(self.sandbox.base / "absent"),
                "SESSION_KIT_CODEX_AUTOTITLE": "0",
            },
            input=payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, produced.returncode, produced.stderr)
        self.assertTrue(json.loads(produced.stdout)["produced"])
        listed = json.loads(self.run_intake("open").stdout)
        self.assertEqual(1, listed["count"])
        self.assertEqual("auto", listed["open"][0]["origin"])
        self.assertEqual(SOURCE_KEY, listed["open"][0]["source_thread_key"])

    def test_a_note_to_a_source_that_is_gone_is_recorded_as_still_owed(self) -> None:
        """The send is refused, so the record says owed — it does not vanish."""
        created = self.run_intake(
            "record", "--msg-id", INTAKE_A, "--source", SOURCE_KEY
        )
        self.assertEqual(0, created.returncode, created.stderr)
        relayed = self.run_intake(
            "progress", "--msg-id", INTAKE_A, "--text", "two branches open"
        )
        self.assertEqual(1, relayed.returncode)
        payload = json.loads(relayed.stdout)
        self.assertFalse(payload["relayed"])
        note = payload["note"]
        self.assertFalse(note["landed"])
        self.assertEqual("unreachable", note["relay_status"])
        self.assertIn("no live session matches", note["relay_detail"])
        still_open = json.loads(self.run_intake("open").stdout)
        self.assertEqual(1, still_open["count"])
        self.assertEqual(1, len(still_open["open"][0]["notes"]))

    def test_the_facade_refuses_a_bad_intake_without_writing_anything(self) -> None:
        refused = self.run_intake(
            "record", "--msg-id", INTAKE_A, "--source", "terminal 7"
        )
        self.assertEqual(1, refused.returncode)
        self.assertIn("source thread key", refused.stderr)
        self.assertFalse((self.sandbox.state / "supervisor" / "intake" / "entries").exists())


if __name__ == "__main__":
    unittest.main()
