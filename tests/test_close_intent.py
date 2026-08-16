"""The crash queue offers what was lost, never what somebody closed.

`recovery-pending.json` is the queue of conversations whose sessions vanished
before anyone claimed them. It could not tell a crash from a decision: a
session closed on purpose came back as recovery work, so the offer that means
"something broke" also meant "you finished something", and neither meaning
survives being both.

Intent is recorded as a tombstone keyed `provider:uuid` by every deliberate
close — a clean provider exit, the recovery menu's `c`, `bye`, and `k` from
the picker. The queue honours it at the one place every consumer passes
through, so an entry queued before the close disappears from the offers, the
header count, and the ack together. Tombstones expire on the same clock as the
terminal number the close freed: one policy, not two.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import contextlib
import io

from tests.support import REPO, run
from tests.test_inventory import inventory_core, uuid_for
from tests.test_recovery_pending import pending_document

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_inventory import closed_sessions, lifecycle  # noqa: E402


DAY_MS = 86_400_000
CORE = REPO / "lib" / "session_inventory.py"


class CloseIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".intent-", dir=REPO)
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir()
        self.state.chmod(0o700)
        self.data = Path(self.temp.name) / "data"
        self.data.mkdir(mode=0o700)
        self.environ = mock.patch.dict(
            os.environ, {"SESSION_KIT_DATA_DIR": str(self.data)}
        )
        self.environ.start()
        self.pending_path = self.state / "recovery-pending.json"
        self.pending_path.write_text(
            json.dumps(pending_document(), sort_keys=True), encoding="utf-8"
        )
        self.config = {
            "schema_version": 1,
            "state_dir": self.state,
            "aliases": {},
            "max_proc_nodes": 8192,
            "max_proc_depth": 32,
        }

    def tearDown(self) -> None:
        self.environ.stop()
        self.temp.cleanup()

    def closed_row(self, provider: str, uuid: str) -> None:
        closed_sessions.record_close(
            provider=provider,
            uuid=uuid,
            title="Closed on purpose",
            environ=os.environ,
            now_unix_ms=1,
        )

    def offered(self) -> set[str]:
        return {
            entry["uuid"]
            for entry in inventory_core.list_pending(self.config)["entries"]
        }

    def test_two_sessions_closing_at_once_keep_both_tombstones(self) -> None:
        """One document, every session on the machine, and no lock.

        `record_close_intent` is a read/modify/replace of the single file all
        closes tombstone into, so two sessions ending in the same second is
        ordinary rather than exotic -- and it got MORE ordinary the moment a
        crashed provider began closing its own session automatically.
        Unlocked, both writers read the same predecessor and each replaces it:
        the last one wins, the other's intent is gone, and a conversation
        somebody deliberately closed comes back offered as unclaimed lost
        work. Atomic replacement stops a torn file; it never stopped a lost
        update (found in review, 2026-08-15).

        Real processes, the real verb, no mocked writer. Every child waits on
        one barrier file so their read/modify/replace windows genuinely
        overlap.
        """
        import subprocess
        import time

        barrier = Path(self.temp.name) / "go"
        uuids = [uuid_for(index) for index in range(1, 9)]
        program = f"""
import sys, time
sys.path.insert(0, {os.fspath(REPO / "lib")!r})
from pathlib import Path
from sessionkit_inventory import lifecycle
state = Path({os.fspath(self.state)!r})
barrier = Path({os.fspath(barrier)!r})
while not barrier.exists():
    time.sleep(0.005)
lifecycle.record_close_intent(state, provider="codex", uuid=sys.argv[1])
"""
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", program, uuid],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for uuid in uuids
        ]
        time.sleep(0.4)
        barrier.write_text("go", encoding="utf-8")
        for worker in workers:
            _, errors = worker.communicate(timeout=60)
            self.assertEqual(0, worker.returncode, errors)

        recorded = lifecycle.load_close_intents(self.state)["closed"]
        missing = [uuid for uuid in uuids if f"codex:{uuid}" not in recorded]
        self.assertEqual(
            [], missing, f"lost {len(missing)} of {len(uuids)} simultaneous closes"
        )

    def test_a_crash_is_offered_when_nobody_closed_it(self) -> None:
        # The filter must not swallow the queue it protects: with no
        # tombstones at all, every queued conversation is still work.
        self.assertEqual(
            {uuid_for(1), uuid_for(2), uuid_for(3)}, self.offered()
        )

    def test_a_conversation_closed_on_purpose_is_never_offered(self) -> None:
        self.closed_row("claude", uuid_for(1))
        lifecycle.record_close_intent(
            self.state, provider="claude", uuid=uuid_for(1)
        )
        self.assertEqual({uuid_for(2), uuid_for(3)}, self.offered())

    def test_a_tombstone_drops_an_entry_that_was_already_queued(self) -> None:
        # The entry existed first; the close came second. Order does not
        # decide whether it was a crash.
        self.assertIn(uuid_for(2), self.offered())
        self.closed_row("codex", uuid_for(2))
        lifecycle.record_close_intent(
            self.state, provider="codex", uuid=uuid_for(2)
        )
        self.assertNotIn(uuid_for(2), self.offered())

    def test_a_tombstone_reaches_a_queued_generation_too(self) -> None:
        # Not just the primary generation: the older queued ones as well.
        self.closed_row("codex", uuid_for(3))
        lifecycle.record_close_intent(
            self.state, provider="codex", uuid=uuid_for(3)
        )
        self.assertEqual({uuid_for(1), uuid_for(2)}, self.offered())

    def test_a_tombstone_without_its_closed_row_does_not_hide_recovery(self) -> None:
        lifecycle.record_close_intent(
            self.state, provider="claude", uuid=uuid_for(1)
        )

        payload = inventory_core.list_pending(self.config)

        self.assertIn(uuid_for(1), {row["uuid"] for row in payload["entries"]})
        diagnostic = " ".join(payload.get("diagnostics", ()))
        self.assertIn(f"claude:{uuid_for(1)}", diagnostic)
        self.assertIn("row is missing", diagnostic)
        self.assertIn("recovery will keep offering", diagnostic)

    def test_an_invalid_ledger_cannot_make_a_tombstone_authoritative(self) -> None:
        closed_sessions.ledger_path(os.environ).mkdir()
        lifecycle.record_close_intent(
            self.state, provider="claude", uuid=uuid_for(1)
        )

        payload = inventory_core.list_pending(self.config)

        self.assertIn(uuid_for(1), {row["uuid"] for row in payload["entries"]})
        diagnostic = " ".join(payload.get("diagnostics", ()))
        self.assertIn(f"claude:{uuid_for(1)}", diagnostic)
        self.assertIn("could not be completely validated", diagnostic)

    def test_a_tombstone_expires_with_the_number_quarantine(self) -> None:
        # One clock. A conversation closed longer ago than the quarantine on
        # the number it freed is not remembered as closed either.
        now = 10 * lifecycle.CLOSE_INTENT_RETENTION_SECONDS * 1000
        stale = now - int(lifecycle.CLOSE_INTENT_RETENTION_SECONDS * 1000) - 1
        fresh = now - int(lifecycle.CLOSE_INTENT_RETENTION_SECONDS * 1000) + DAY_MS
        lifecycle.record_close_intent(
            self.state, provider="claude", uuid=uuid_for(1), now_unix_ms=stale
        )
        lifecycle.record_close_intent(
            self.state, provider="codex", uuid=uuid_for(2), now_unix_ms=fresh
        )
        live = lifecycle.load_close_intents(self.state, now_unix_ms=now)
        self.assertEqual([f"codex:{uuid_for(2)}"], sorted(live["closed"]))
        self.assertFalse(
            lifecycle.closed_on_purpose(
                self.state,
                provider="claude",
                uuid=uuid_for(1),
                now_unix_ms=now,
            )
        )
        self.assertTrue(
            lifecycle.closed_on_purpose(
                self.state,
                provider="codex",
                uuid=uuid_for(2),
                now_unix_ms=now,
            )
        )

    def test_recording_an_intent_expires_the_stale_ones_it_finds(self) -> None:
        now = 10 * lifecycle.CLOSE_INTENT_RETENTION_SECONDS * 1000
        stale = now - int(lifecycle.CLOSE_INTENT_RETENTION_SECONDS * 1000) - 1
        lifecycle.record_close_intent(
            self.state, provider="claude", uuid=uuid_for(1), now_unix_ms=stale
        )
        document = lifecycle.record_close_intent(
            self.state, provider="codex", uuid=uuid_for(2), now_unix_ms=now
        )
        self.assertEqual([f"codex:{uuid_for(2)}"], sorted(document["closed"]))

    def test_the_store_is_private_and_refuses_a_key_it_cannot_trust(self) -> None:
        lifecycle.record_close_intent(
            self.state, provider="claude", uuid=uuid_for(1)
        )
        path = lifecycle.close_intent_path(self.state)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        for provider, uuid in (
            ("shell", uuid_for(1)),
            ("claude", "not-a-uuid"),
            ("claude", ""),
        ):
            with self.subTest(provider=provider, uuid=uuid):
                with self.assertRaises(Exception):
                    lifecycle.record_close_intent(
                        self.state, provider=provider, uuid=uuid
                    )
                self.assertFalse(
                    lifecycle.closed_on_purpose(
                        self.state, provider=provider, uuid=uuid
                    )
                )

    def test_an_unreadable_store_hides_no_recovery_work(self) -> None:
        # Failing open here shows MORE offers, never fewer: a tombstone store
        # that cannot be read must not be able to swallow a real crash.
        lifecycle.close_intent_path(self.state).write_text(
            "{not json", encoding="utf-8"
        )
        lifecycle.close_intent_path(self.state).chmod(0o600)
        self.assertEqual(
            {uuid_for(1), uuid_for(2), uuid_for(3)}, self.offered()
        )


class SharedCloseVerbTests(unittest.TestCase):
    """`close-intent record`: the verb BOTH live close paths run after a kill.

    `sp close` (lib/sh/sp_commands.sh) and the picker's `k` (lib/sh/sp_picker.sh)
    call this one command once the session is already dead. It writes the two
    records that decide where the conversation can be found afterwards: the
    closed-sessions ledger row, which is what a person restores from, and the
    tombstone, which tells the crash queue not to offer it as lost work.

    Written tombstone-first with the ledger result discarded, a ledger that
    could not be written produced the one state with no way back -- absent from
    Closed sessions, suppressed in recovery -- and printed `recorded: true` at a
    caller that had already killed the session (found in review, 2026-08-15).

    Real subprocesses running the shipped file, with HOME, state, data and
    config pinned into the fixture and a fake `shpool` first on PATH, so nothing
    here can read or touch a real session manager.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".shared-close-", dir=REPO)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.state = self.base / "state"
        self.data = self.home / ".local" / "share" / "session-kit"
        self.bin = self.home / ".local" / "bin"
        for path in (self.home, self.state):
            path.mkdir(mode=0o700, parents=True)
        self.data.mkdir(mode=0o700, parents=True)
        self.bin.mkdir(mode=0o700, parents=True)
        self.config = self.base / "inventory.json"
        self.config.write_text(
            json.dumps(
                {"schema_version": 1, "state_dir": str(self.state), "aliases": {}}
            )
            + "\n",
            encoding="utf-8",
        )
        self.config.chmod(0o600)
        self.boot = self.base / "boot-id"
        self.boot.write_text("boot-current\n", encoding="utf-8")
        # A fake session manager. The collector behind these verbs runs
        # `shpool list --json`; without this stub on PATH it reaches the real
        # daemon on its default socket.
        stub = self.bin / "shpool"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ $1 == list ]]; then printf '{\"sessions\": []}\\n'; exit 0; fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        self.uuid = uuid_for(2)
        self.provider_transcript(self.uuid)
        self.queue_as_lost(self.uuid)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def provider_transcript(self, uuid: str) -> Path:
        """The conversation's own file: what a restore reads, and what the
        Closed-sessions reader checks before it will show the row at all."""
        day = self.home / ".codex" / "sessions" / "2026" / "08" / "15"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"rollout-2026-08-15T03-00-00-{uuid}.jsonl"
        path.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": uuid}}) + "\n",
            encoding="utf-8",
        )
        return path

    def queue_as_lost(self, uuid: str) -> None:
        """The crash queue already holds this conversation.

        That is the state a close acts on, and the only remaining way back to
        the conversation when the ledger cannot be written.
        """
        document = {
            "schema_version": 1,
            "generated_at": "2026-08-15T00:00:00Z",
            "source_boot_id": "boot-primary",
            "source_daemon_generation": {"pid": 10, "process_start_ticks": 100},
            "detected_boot_id": "boot-current",
            "detected_daemon_generation": {"pid": 20, "process_start_ticks": 200},
            "sessions": {
                "old-codex": {
                    "provider": "codex",
                    "uuid": uuid,
                    "title": "Codex pending",
                    "started_at_unix_ms": 1_700_000_000_000,
                    "cwd": "/srv/project",
                    "argv": ["codex", "resume", uuid],
                    "command": f"codex resume {uuid}",
                }
            },
            "queued_generations": [],
        }
        (self.state / "recovery-pending.json").write_text(
            json.dumps(document, sort_keys=True), encoding="utf-8"
        )

    def ledger_cannot_be_written(self) -> Path:
        """A directory where the ledger file belongs: every append raises
        EISDIR, the shape of a full disk or a lost mount without needing
        either."""
        path = self.data / "closed-sessions.jsonl"
        path.mkdir()
        return path

    def environment(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "SESSION_KIT_CONFIG": str(self.config),
            "SESSION_KIT_STATE_DIR": str(self.state),
            "SESSION_KIT_BOOT_ID_FILE": str(self.boot),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "SESSION_KIT_DATA_DIR": str(self.data),
            "SHPOOL_JOURNAL": "disabled",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def record_close(self, uuid: str | None = None):
        """The shipped command, exactly as both close paths spell it."""
        return run(
            [CORE, "close-intent", "record", "codex", uuid or self.uuid],
            env=self.environment(),
            check=False,
        )

    def offered(self) -> set[str]:
        """What the crash queue would offer, excluding closed-ledger rows."""
        completed = run(
            [CORE, "recovery-pending", "list", "--without-closed"],
            env=self.environment(),
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return {
            entry["uuid"]
            for entry in json.loads(completed.stdout)["entries"]
        }

    def closed_rows(self) -> list:
        """What Closed sessions shows a person, via the real reader."""
        completed = run(
            [CORE, "closed-sessions", "list"],
            env=self.environment(),
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)["closed"]

    def tombstones(self) -> set[str]:
        return set(lifecycle.load_close_intents(self.state)["closed"])

    def test_a_close_that_lands_is_findable_and_no_longer_offered(self) -> None:
        """The control: nothing about the ordinary close changes."""
        completed = self.record_close()
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["recorded"])
        self.assertTrue(payload["tombstoned"])
        self.assertEqual(
            [self.uuid], [row["uuid"] for row in self.closed_rows()]
        )
        self.assertEqual({f"codex:{self.uuid}"}, self.tombstones())
        self.assertEqual(set(), self.offered())

    def test_replacement_after_final_agreement_is_reoffered_and_diagnosed(
        self,
    ) -> None:
        """The success claim heals if its ledger row later becomes absent."""
        ledger = closed_sessions.ledger_path(self.environment())
        ledger.touch(mode=0o600)
        before = ledger.read_bytes()
        real_prove = closed_sessions._prove_descriptor_authoritative
        replaced = False

        def prove_then_replace(descriptor: int, path: Path, *, action: str) -> None:
            nonlocal replaced
            real_prove(descriptor, path, action=action)
            if action == "appended" and not replaced:
                replacement = path.with_name("after-agreement-replacement.jsonl")
                replacement.write_bytes(before)
                replacement.chmod(0o600)
                os.replace(replacement, path)
                replaced = True

        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment(), clear=True), mock.patch.object(
            closed_sessions,
            "_prove_descriptor_authoritative",
            prove_then_replace,
        ), contextlib.redirect_stdout(output):
            code = inventory_core.main(
                ["close-intent", "record", "codex", self.uuid]
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertTrue(payload["recorded"])
        self.assertTrue(payload["tombstoned"])
        self.assertTrue(replaced)
        self.assertNotIn(self.uuid.encode(), ledger.read_bytes())

        completed = run(
            [CORE, "recovery-pending", "list", "--without-closed"],
            env=self.environment(),
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        recovery = json.loads(completed.stdout)
        self.assertIn(self.uuid, {row["uuid"] for row in recovery["entries"]})
        diagnostic = " ".join(recovery.get("diagnostics", ()))
        self.assertIn(f"codex:{self.uuid}", diagnostic)
        self.assertIn("row is missing", diagnostic)

    def test_a_ledger_that_cannot_be_written_closes_nothing_and_says_so(
        self,
    ) -> None:
        """The defect. A deterministic ledger failure must not end with the
        conversation on NEITHER surface, and must not report success.

        Both callers branch on the exit status, so a zero here is what let a
        killed session's conversation disappear silently.
        """
        self.ledger_cannot_be_written()
        before = self.offered()
        self.assertEqual({self.uuid}, before)

        completed = self.record_close()

        self.assertNotEqual(
            0,
            completed.returncode,
            "a failed record must not report success to a caller that has"
            f" already killed the session: {completed.stdout}",
        )
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["recorded"])
        self.assertIn("ledger", payload["reason"])
        # No row anywhere means no tombstone: the internal crash candidate is
        # retained. The fused `sp recover` command still refuses its whole
        # list while this deliberately broken ledger cannot be validated.
        self.assertEqual(set(), self.tombstones())
        listed = run(
            [CORE, "closed-sessions", "list"],
            env=self.environment(),
            check=False,
        )
        self.assertNotEqual(0, listed.returncode)
        self.assertIn("regular file", listed.stderr)
        self.assertEqual({self.uuid}, self.offered())
        shipped = run(
            [REPO / "bin" / "sp", "recover"],
            env=self.environment(),
            check=False,
        )
        self.assertNotEqual(0, shipped.returncode)
        self.assertEqual("", shipped.stdout)
        self.assertIn("closed-conversations list could not be read", shipped.stderr)

    def test_a_shell_row_that_cannot_be_written_is_not_reported_as_filed(
        self,
    ) -> None:
        """The sibling verb, on the same close path, for a plain managed shell.

        `sp close` and picker `k` both spell it `>/dev/null 2>&1 && return 0`,
        so its exit status is the whole answer they get. It printed
        `recorded: false` and exited 0, which made every warning either shell
        could raise unreachable and printed `Closed ...` over a session that
        reached no list at all.
        """
        self.ledger_cannot_be_written()
        completed = run(
            [CORE, "closed-sessions", "record", "shell", "--session", "s1"],
            env=self.environment(),
            check=False,
        )
        self.assertNotEqual(
            0,
            completed.returncode,
            f"a lost shell row reported as filed: {completed.stdout}",
        )
        self.assertFalse(json.loads(completed.stdout)["recorded"])
        listed = run(
            [CORE, "closed-sessions", "list"],
            env=self.environment(),
            check=False,
        )
        self.assertNotEqual(0, listed.returncode)
        self.assertIn("regular file", listed.stderr)

    def test_a_shell_row_that_lands_reports_success(self) -> None:
        """The control for the status above: an ordinary shell close is zero."""
        completed = run(
            [CORE, "closed-sessions", "record", "shell", "--session", "s1"],
            env=self.environment(),
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        # A filed row answers with the row itself; only a failure carries
        # `recorded`, which is what the exit status is derived from.
        self.assertNotIn("recorded", json.loads(completed.stdout))
        self.assertEqual(["s1"], [row["shpool_id"] for row in self.closed_rows()])

    def test_a_uuid_the_tombstone_would_reject_records_nothing_at_all(
        self,
    ) -> None:
        """The ledger now goes first, so an argument only the tombstone store
        validates must be refused BEFORE either record is written -- otherwise
        the reorder trades one bad state for a closed-list row naming a
        conversation that can never be tombstoned or found."""
        completed = self.record_close("not-a-uuid")
        self.assertNotEqual(0, completed.returncode, completed.stdout)
        self.assertEqual([], self.closed_rows())
        self.assertEqual(set(), self.tombstones())
        self.assertEqual({self.uuid}, self.offered())


class LostConversationQueueTests(unittest.TestCase):
    """One session dying on its own is a loss the queue can now see.

    Before this, only a daemon restart could queue anything: a window killed
    mid-conversation was invisible until the next generation change, by which
    time the row it came from had aged off every screen.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".lost-", dir=REPO)
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir()
        self.state.chmod(0o700)
        self.data = Path(self.temp.name) / "data"
        self.data.mkdir(mode=0o700)
        self.environ = mock.patch.dict(
            os.environ, {"SESSION_KIT_DATA_DIR": str(self.data)}
        )
        self.environ.start()
        self.config = {
            "schema_version": 1,
            "state_dir": self.state,
            "aliases": {},
            "max_proc_nodes": 8192,
            "max_proc_depth": 32,
        }
        self.paths = {"pending": self.state / "recovery-pending.json"}

    def tearDown(self) -> None:
        self.environ.stop()
        self.temp.cleanup()

    def closed_row(self) -> None:
        closed_sessions.record_close(
            provider="claude",
            uuid=uuid_for(1),
            title="Closed on purpose",
            environ=os.environ,
            now_unix_ms=1,
        )

    def previous(self, **overrides) -> dict:
        row = {
            "shpool_id_raw": "main7",
            "provider": "claude",
            "title": "Parser work",
            "started_at_unix_ms": 1_700_000_000_000,
            "terminal_number": 7,
            "account_alias": "work",
            "identity": {"uuid": uuid_for(1), "confidence": "exact"},
            "recovery": {
                "available": True,
                "provider": "claude",
                "uuid": uuid_for(1),
                "cwd": "/srv/project",
                "argv": ["claude", "--resume", uuid_for(1)],
                "command": f"claude --resume {uuid_for(1)}",
            },
        }
        row.update(overrides)
        return {"sessions": [row], "daemon_generation": {"pid": 10, "process_start_ticks": 100}}

    def current(self, *sessions: dict) -> dict:
        return {
            "generated_at": "2026-08-11T00:00:00Z",
            "daemon_generation": {"pid": 10, "process_start_ticks": 100},
            "sessions": list(sessions),
        }

    def enqueue(self, current: dict, previous: dict | None) -> list[str]:
        return inventory_core.enqueue_lost_conversations(
            self.paths,
            current,
            previous,
            boot_id="boot-current",
            config=self.config,
        )

    def offered(self) -> set[str]:
        return {
            entry["uuid"]
            for entry in inventory_core.list_pending(self.config)["entries"]
        }

    def test_a_session_that_vanished_with_its_provider_is_queued(self) -> None:
        queued = self.enqueue(self.current(), self.previous())
        self.assertEqual([f"claude:{uuid_for(1)}"], queued)
        self.assertEqual({uuid_for(1)}, self.offered())
        entry = next(
            item
            for item in inventory_core.list_pending(self.config)["entries"]
        )
        self.assertTrue(entry["actionable"])
        self.assertEqual("/srv/project", entry["cwd"])
        stored = json.loads(self.paths["pending"].read_text(encoding="utf-8"))
        record = stored["sessions"]["main7"]
        self.assertEqual(7, record["terminal_number"])
        self.assertEqual("work", record["account_alias"])
        self.assertIsInstance(record["crashed_at_unix_ms"], int)

    def test_a_closed_conversation_is_retained_but_not_offered(self) -> None:
        self.closed_row()
        lifecycle.record_close_intent(
            self.state, provider="claude", uuid=uuid_for(1)
        )
        self.assertEqual(
            [f"claude:{uuid_for(1)}"],
            self.enqueue(self.current(), self.previous()),
        )
        self.assertTrue(self.paths["pending"].exists())
        self.assertNotIn(uuid_for(1), self.offered())

    def test_a_tombstone_without_its_row_is_queued_and_diagnosed(self) -> None:
        lifecycle.record_close_intent(
            self.state, provider="claude", uuid=uuid_for(1)
        )
        errors = io.StringIO()

        with contextlib.redirect_stderr(errors):
            queued = self.enqueue(self.current(), self.previous())

        self.assertEqual([f"claude:{uuid_for(1)}"], queued)
        self.assertIn(uuid_for(1), self.offered())
        self.assertIn(f"claude:{uuid_for(1)}", errors.getvalue())
        self.assertIn("row is missing", errors.getvalue())

    def test_a_session_that_is_still_there_is_not_a_loss(self) -> None:
        previous = self.previous()
        self.assertEqual([], self.enqueue(self.current(*previous["sessions"]), previous))
        self.assertFalse(self.paths["pending"].exists())

    def test_the_same_conversation_living_elsewhere_is_not_a_loss(self) -> None:
        # Moved, restored, or reopened under a new session ID: the
        # conversation is on screen, so it is not recovery work.
        moved = dict(self.previous()["sessions"][0], shpool_id_raw="main9")
        self.assertEqual([], self.enqueue(self.current(moved), self.previous()))

    def crashed_shell(self, **overrides) -> dict:
        """What a crash leaves behind: an idle shell holding the conversation.

        This is the row the reaper's auto-close eventually takes.
        """
        return self.previous(
            provider="shell",
            exited_provider="claude",
            identity=None,
            exited_identity={
                "uuid": uuid_for(1),
                "provenance": "last exact live inventory for this shell generation",
                "confidence": "historical-exact",
            },
            **overrides,
        )

    def test_the_auto_close_of_a_crashed_shell_queues_its_conversation(self) -> None:
        # An automatic reap is not intent (operator ruling, 2026-08-11): the
        # terminal was safe to close, but nobody said the conversation was
        # finished, so it becomes recovery work rather than disappearing.
        queued = self.enqueue(self.current(), self.crashed_shell())
        self.assertEqual([f"claude:{uuid_for(1)}"], queued)
        self.assertEqual({uuid_for(1)}, self.offered())

    def test_the_auto_close_of_a_closed_conversation_retains_hidden_evidence(
        self,
    ) -> None:
        # A person's k, c, bye, or clean /exit left a tombstone; the reap that
        # follows is bookkeeping. Its raw evidence remains self-healing while
        # the agreed closed row keeps it off the public recovery list.
        self.closed_row()
        lifecycle.record_close_intent(
            self.state, provider="claude", uuid=uuid_for(1)
        )
        self.assertEqual(
            [f"claude:{uuid_for(1)}"],
            self.enqueue(self.current(), self.crashed_shell()),
        )
        self.assertTrue(self.paths["pending"].exists())
        self.assertNotIn(uuid_for(1), self.offered())

    def test_an_idle_shell_with_a_guessed_identity_is_never_queued(self) -> None:
        previous = self.crashed_shell()
        previous["sessions"][0]["exited_identity"]["confidence"] = "heuristic"
        self.assertEqual([], self.enqueue(self.current(), previous))

    def test_a_guess_at_identity_is_never_queued(self) -> None:
        previous = self.previous(
            identity={"uuid": uuid_for(1), "confidence": "heuristic"}
        )
        self.assertEqual([], self.enqueue(self.current(), previous))

    def test_a_second_pass_keeps_the_first_record_of_the_loss(self) -> None:
        first = self.enqueue(self.current(), self.previous())
        self.assertEqual(1, len(first))
        stamped = json.loads(self.paths["pending"].read_text(encoding="utf-8"))
        crashed_at = stamped["sessions"]["main7"]["crashed_at_unix_ms"]
        self.enqueue(self.current(), self.previous())
        again = json.loads(self.paths["pending"].read_text(encoding="utf-8"))
        self.assertEqual(crashed_at, again["sessions"]["main7"]["crashed_at_unix_ms"])
        self.assertEqual({uuid_for(1)}, self.offered())

    def test_an_existing_queue_from_another_generation_is_preserved(self) -> None:
        self.paths["pending"].write_text(
            json.dumps(pending_document(), sort_keys=True), encoding="utf-8"
        )
        self.enqueue(self.current(), self.previous())
        self.assertEqual(
            {uuid_for(1), uuid_for(2), uuid_for(3)}, self.offered()
        )

    def test_nothing_is_queued_without_an_exact_daemon_generation(self) -> None:
        # An entry nobody could acknowledge later is worse than no entry.
        current = self.current()
        current.pop("daemon_generation")
        self.assertEqual([], self.enqueue(current, self.previous()))
        self.assertFalse(self.paths["pending"].exists())

    def test_a_first_snapshot_with_no_previous_state_queues_nothing(self) -> None:
        self.assertEqual([], self.enqueue(self.current(), None))
        self.assertFalse(self.paths["pending"].exists())


if __name__ == "__main__":
    unittest.main()
