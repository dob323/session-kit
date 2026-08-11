"""The `msg` verbs end to end, against synthetic sessions only.

Target selection, the send ledger, replies, and the facade command surface.
Nothing here reaches a real session: inventories are synthetic dictionaries,
delivery is injected, and the one facade subprocess test drives a fake
process tree with the collector's own fixture hooks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_messages import verbs  # noqa: E402
from sessionkit_messages.envelope import MessageError  # noqa: E402
from sessionkit_messages.ledger import Ledger  # noqa: E402

CORE = REPO / "lib" / "session_inventory.py"
CODEX_UUID = "019fdf1e-8b4c-7573-a089-be495bfece6a"
CLAUDE_UUID = "dcbdf940-4eda-4967-8e41-23a5760c32b5"
SECOND_CLAUDE_UUID = "96d743aa-1111-4222-8333-444444444444"
OPTIONAL_CODEX_UUID = "019fdf22-6d71-7a70-bda6-d22939a7579a"
WAITING_CLAUDE_UUID = "5f1a2b3c-4d5e-4f60-8712-9a8b7c6d5e4f"
CODEX_KEY = f"codex:{CODEX_UUID}"
CLAUDE_KEY = f"claude:{CLAUDE_UUID}"
SECOND_CLAUDE_KEY = f"claude:{SECOND_CLAUDE_UUID}"
OPTIONAL_CODEX_KEY = f"codex:{OPTIONAL_CODEX_UUID}"
WAITING_CLAUDE_KEY = f"claude:{WAITING_CLAUDE_UUID}"
WAITING_NOTE = "(this session is waiting on the operator's reply)"


def session(
    *,
    provider: str,
    uuid: str | None,
    shpool_id: str,
    title: str,
    status: str = "idle",
    terminal_number: int | None = None,
    row: int = 1,
) -> dict:
    return {
        "row": row,
        "terminal_number": terminal_number,
        "shpool_id": shpool_id,
        "shpool_id_raw": shpool_id,
        "provider": provider,
        "display_provider": provider,
        "identity": {"uuid": uuid, "pid": 100 + row, "provenance": "test"},
        "title": title,
        "display_title": title,
        "agent_status": status,
        "needs_you": False,
    }


def inventory(*rows: dict) -> dict:
    return {"schema_version": 1, "source": "live", "stale": False, "sessions": list(rows)}


CODEX_ROW = session(
    provider="codex",
    uuid=CODEX_UUID,
    shpool_id="s-codex",
    title="Codex work",
    terminal_number=1,
    row=1,
)
CLAUDE_ROW = session(
    provider="claude",
    uuid=CLAUDE_UUID,
    shpool_id="s-claude",
    title="v2-ec",
    terminal_number=2,
    row=2,
)
BUSY_CLAUDE_ROW = session(
    provider="claude",
    uuid=SECOND_CLAUDE_UUID,
    shpool_id="s-claude-2",
    title="core-sh",
    status="working",
    terminal_number=3,
    row=3,
)
SHELL_ROW = session(
    provider="shell",
    uuid=None,
    shpool_id="s-shell",
    title="Idle shell",
    terminal_number=4,
    row=4,
)
OPTIONAL_CODEX_ROW = session(
    provider="codex",
    uuid=OPTIONAL_CODEX_UUID,
    shpool_id="s-codex-optional",
    title="Codex follow-up",
    status="reply optional",
    terminal_number=5,
    row=5,
)
WAITING_CLAUDE_ROW = session(
    provider="claude",
    uuid=WAITING_CLAUDE_UUID,
    shpool_id="s-claude-asked",
    title="asked-operator",
    status="needs your reply",
    terminal_number=6,
    row=6,
)
FULL = inventory(CODEX_ROW, CLAUDE_ROW, BUSY_CLAUDE_ROW, SHELL_ROW)
STATUS_MIX = inventory(
    CODEX_ROW, OPTIONAL_CODEX_ROW, BUSY_CLAUDE_ROW, WAITING_CLAUDE_ROW
)


class Sandbox:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".session-kit-msgverbs-", dir=REPO
        )
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.ledger = Ledger(self.state)

    def close(self) -> None:
        self.temporary.cleanup()


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()

    def tearDown(self) -> None:
        self.sandbox.close()

    def resolve(self, selector: str, registry: dict | None = None) -> dict:
        return verbs.resolve(
            FULL,
            selector,
            state_dir=self.sandbox.state,
            registry=registry if registry is not None else {},
        )

    def test_all_takes_every_provider_row_and_lists_the_rest_as_skipped(self) -> None:
        payload = self.resolve("all")
        self.assertEqual(
            [CODEX_KEY, CLAUDE_KEY, SECOND_CLAUDE_KEY],
            [target["thread_key"] for target in payload["targets"]],
        )
        self.assertEqual({"claude": 2, "codex": 1, "unreachable_hint": 3}, payload["counts"])
        self.assertEqual(["s-shell"], [row["shpool_id"] for row in payload["skipped"]])
        self.assertIn("no message path", payload["skipped"][0]["reason"])

    def test_targets_carry_exactly_the_frozen_resolve_fields(self) -> None:
        target = self.resolve("all")["targets"][0]
        self.assertEqual(
            {
                "thread_key",
                "provider",
                "shpool_id",
                "uuid",
                "terminal_number",
                "title",
                "agent_status",
            },
            set(target),
        )
        self.assertEqual("s-codex", target["shpool_id"])
        self.assertEqual(1, target["terminal_number"])
        self.assertEqual("Codex work", target["title"])

    def test_every_target_reports_the_state_it_was_resolved_in(self) -> None:
        """The confirmation table shows who is mid-task before the operator commits."""
        statuses = {
            target["thread_key"]: target["agent_status"]
            for target in verbs.resolve(
                STATUS_MIX, "all", state_dir=self.sandbox.state, registry={}
            )["targets"]
        }
        self.assertEqual(
            {
                CODEX_KEY: "idle",
                OPTIONAL_CODEX_KEY: "reply optional",
                SECOND_CLAUDE_KEY: "working",
                WAITING_CLAUDE_KEY: "needs your reply",
            },
            statuses,
        )

    def test_a_status_free_row_reports_unknown_rather_than_nothing(self) -> None:
        bare = dict(CODEX_ROW)
        bare.pop("agent_status")
        target = verbs.resolve(
            inventory(bare), "all", state_dir=self.sandbox.state, registry={}
        )["targets"][0]
        self.assertEqual("unknown", target["agent_status"])

    def test_idle_takes_only_idle_rows(self) -> None:
        payload = self.resolve("idle")
        self.assertEqual(
            [CODEX_KEY, CLAUDE_KEY],
            [target["thread_key"] for target in payload["targets"]],
        )
        # The busy Claude row is simply not idle, so it is not a skip; the
        # idle shell is, and says why.
        self.assertEqual(["s-shell"], [row["shpool_id"] for row in payload["skipped"]])

    def keys(self, payload: dict) -> list[str]:
        return [target["thread_key"] for target in payload["targets"]]

    def test_idle_means_unoccupied_which_includes_an_optional_follow_up(self) -> None:
        payload = verbs.resolve(
            STATUS_MIX, "idle", state_dir=self.sandbox.state, registry={}
        )
        self.assertEqual([CODEX_KEY, OPTIONAL_CODEX_KEY], self.keys(payload))

    def test_idle_leaves_out_a_session_already_waiting_on_the_operator(self) -> None:
        """It is occupied by a question of its own, not free for a new one."""
        payload = verbs.resolve(
            STATUS_MIX, "idle", state_dir=self.sandbox.state, registry={}
        )
        self.assertNotIn(WAITING_CLAUDE_KEY, self.keys(payload))

    def test_a_session_waiting_on_the_operator_is_reachable_by_all_and_by_number(
        self,
    ) -> None:
        everyone = verbs.resolve(
            STATUS_MIX, "all", state_dir=self.sandbox.state, registry={}
        )
        self.assertIn(WAITING_CLAUDE_KEY, self.keys(everyone))
        by_number = verbs.resolve(
            STATUS_MIX, "6", state_dir=self.sandbox.state, registry={}
        )
        self.assertEqual([WAITING_CLAUDE_KEY], self.keys(by_number))

    def test_a_terminal_number_selects_one_session(self) -> None:
        payload = self.resolve("2")
        self.assertEqual([CLAUDE_KEY], [t["thread_key"] for t in payload["targets"]])

    def test_a_shpool_id_selects_one_session(self) -> None:
        payload = self.resolve("s-codex")
        self.assertEqual([CODEX_KEY], [t["thread_key"] for t in payload["targets"]])

    def test_a_thread_key_selects_one_session(self) -> None:
        payload = self.resolve(f"key:{CLAUDE_KEY}")
        self.assertEqual([CLAUDE_KEY], [t["thread_key"] for t in payload["targets"]])

    def test_a_key_list_selects_several_sessions_in_the_order_given(self) -> None:
        payload = self.resolve(f"keys:{CLAUDE_KEY},{CODEX_KEY}")
        self.assertEqual([CLAUDE_KEY, CODEX_KEY], self.keys(payload))
        self.assertEqual({"claude": 1, "codex": 1, "unreachable_hint": 2}, payload["counts"])

    def test_a_key_list_tolerates_spacing_and_repeats(self) -> None:
        payload = self.resolve(f"keys: {CODEX_KEY} , {CODEX_KEY},")
        self.assertEqual([CODEX_KEY], self.keys(payload))

    def test_a_key_list_refuses_an_empty_or_malformed_entry(self) -> None:
        for bad in ("keys:", "keys: , ", f"keys:{CODEX_KEY},nonsense", "keys:shell:x"):
            with self.assertRaises(MessageError):
                self.resolve(bad)

    def test_a_departed_key_is_skipped_so_the_rest_still_go(self) -> None:
        """One session exiting must not cancel a follow-up to everyone else."""
        gone = f"claude:{WAITING_CLAUDE_UUID}"
        payload = self.resolve(f"keys:{CODEX_KEY},{gone}")
        self.assertEqual([CODEX_KEY], self.keys(payload))
        self.assertEqual(
            [{"provider": "claude", "title": gone, "reason": "session is no longer live"}],
            [
                {"provider": row["provider"], "title": row["title"], "reason": row["reason"]}
                for row in payload["skipped"]
            ],
        )

    def test_a_key_list_where_nothing_is_live_is_refused(self) -> None:
        with self.assertRaises(MessageError):
            self.resolve(f"keys:claude:{WAITING_CLAUDE_UUID}")

    def test_row_numbers_are_used_only_when_no_terminal_numbers_exist(self) -> None:
        rows = inventory(
            session(provider="codex", uuid=CODEX_UUID, shpool_id="s1", title="a", row=7)
        )
        payload = verbs.resolve(rows, "7", state_dir=self.sandbox.state, registry={})
        self.assertEqual([CODEX_KEY], [t["thread_key"] for t in payload["targets"]])

    def test_selecting_a_shell_yields_no_target_and_says_why(self) -> None:
        payload = self.resolve("4")
        self.assertEqual([], payload["targets"])
        self.assertEqual(1, len(payload["skipped"]))

    def test_an_unknown_or_ambiguous_selector_is_refused(self) -> None:
        with self.assertRaises(MessageError):
            self.resolve("nope")
        with self.assertRaises(MessageError):
            self.resolve("")
        duplicate = inventory(CODEX_ROW, dict(CODEX_ROW, identity={"uuid": CLAUDE_UUID}))
        with self.assertRaises(MessageError):
            verbs.resolve(duplicate, "s-codex", state_dir=self.sandbox.state, registry={})

    def test_the_reachability_hint_counts_missing_sockets_and_registrations(self) -> None:
        socket_dir = self.sandbox.state / "app-server" / "s-codex"
        socket_dir.mkdir(parents=True)
        import socket as socketmod

        server = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        server.bind(os.fspath(socket_dir / "app.sock"))
        try:
            payload = self.resolve("all", registry={CLAUDE_UUID: {"name": "v2-ec"}})
        finally:
            server.close()
        self.assertEqual(1, payload["counts"]["unreachable_hint"])


class SendTests(unittest.TestCase):
    def setUp(self) -> None:
        import socket as socketmod

        self.sandbox = Sandbox()
        self.clock = 1_700_000_000.0
        self.delivered: list[dict] = []
        # A bound socket only: delivery itself is injected, so nothing on the
        # other end is ever spoken to.
        socket_dir = self.sandbox.state / "app-server" / "s-codex"
        socket_dir.mkdir(parents=True)
        self.codex_socket = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        self.codex_socket.bind(os.fspath(socket_dir / "app.sock"))

    def tearDown(self) -> None:
        self.codex_socket.close()
        self.sandbox.close()

    def tick(self) -> float:
        self.clock += 0.001
        return self.clock

    def codex_ok(self, **kwargs: object) -> dict:
        self.delivered.append(dict(kwargs))
        return {
            "status": "delivered-woke",
            "method": "wake",
            "detail": "turn/start accepted on an idle thread",
        }

    def send(
        self,
        inventory_value: dict = FULL,
        selector: str = "all",
        *,
        text: str = "Status?",
        fyi: bool = False,
        environ: dict | None = None,
        registry: list | None = None,
        codex_deliver=None,
        sender_rows: list | None = None,
        sender_ok: bool = True,
        sender_detail: str = "sender completed",
        found: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        def runner(argv, timeout, cwd=None):  # type: ignore[no-untyped-def]
            self.sender_argv = list(argv)
            self.sender_cwd = cwd
            self.sender_runs = getattr(self, "sender_runs", 0) + 1
            return subprocess.CompletedProcess(
                list(argv),
                0 if sender_ok else 1,
                json.dumps(sender_rows if sender_rows is not None else []),
                "" if sender_ok else sender_detail,
            )

        def verify(targets, *, msg_id, home, **kwargs):  # type: ignore[no-untyped-def]
            self.verified = [dict(target) for target in targets]
            self.verify_calls = getattr(self, "verify_calls", [])
            self.verify_calls.append(dict(kwargs))
            if found is not None:
                return dict(found)
            return {str(target["thread_key"]): True for target in targets}

        return verbs.send(
            idempotency_key=idempotency_key,
            inventory=inventory_value,
            selector=selector,
            text=text,
            fyi=fyi,
            state_dir=self.sandbox.state,
            environ=environ if environ is not None else {},
            home=self.sandbox.home,
            ledger=self.sandbox.ledger,
            clock=self.tick,
            registry_entries=lambda: (
                registry
                if registry is not None
                else [
                    {"sessionId": CLAUDE_UUID, "name": "v2-ec", "cwd": "/repo"},
                    {"sessionId": SECOND_CLAUDE_UUID, "name": "core-sh", "cwd": "/repo"},
                ]
            ),
            codex_deliver=codex_deliver or self.codex_ok,
            sender_runner=runner,
            verify=verify,
        )

    def statuses(self, payload: dict) -> dict[str, str]:
        return {row["thread_key"]: row["status"] for row in payload["targets"]}

    def test_a_full_send_records_every_target_and_its_method(self) -> None:
        payload = self.send()
        self.assertEqual(
            {
                CODEX_KEY: "delivered-woke",
                CLAUDE_KEY: "delivered-woke",
                SECOND_CLAUDE_KEY: "delivered-midturn",
            },
            self.statuses(payload),
        )
        methods = {row["thread_key"]: row["method"] for row in payload["targets"]}
        self.assertEqual("wake", methods[CODEX_KEY])
        self.assertEqual("headless-send", methods[CLAUDE_KEY])
        stored = self.sandbox.ledger.read_send(payload["msg_id"])
        assert stored is not None
        self.assertEqual("Status?", stored["operator_text"])
        self.assertFalse(stored["fyi"])

    def test_the_envelope_reaches_codex_and_carries_the_reply_command(self) -> None:
        payload = self.send()
        envelope = str(self.delivered[0]["envelope"])
        self.assertIn(f"[session-kit operator message {payload['msg_id']}]", envelope)
        self.assertIn(f'sp msg reply {payload["msg_id"]}', envelope)
        self.assertEqual(CODEX_UUID, self.delivered[0]["uuid"])

    def test_an_fyi_send_asks_for_no_reply_everywhere(self) -> None:
        payload = self.send(fyi=True)
        envelope = str(self.delivered[0]["envelope"])
        self.assertNotIn("sp msg reply", envelope)
        self.assertIn("FYI only — no reply needed.", envelope)
        self.assertIn("FYI only — no reply needed.", self.sender_argv[2])
        stored = self.sandbox.ledger.read_send(payload["msg_id"])
        assert stored is not None
        self.assertTrue(stored["fyi"])

    def test_every_target_gets_an_outbound_thread_line(self) -> None:
        payload = self.send()
        for key, via in (
            (CODEX_KEY, "wake"),
            (CLAUDE_KEY, "headless-send"),
            (SECOND_CLAUDE_KEY, "headless-send"),
        ):
            lines = self.sandbox.ledger.read_thread(key)
            self.assertEqual(1, len(lines), key)
            self.assertEqual("out", lines[0]["dir"])
            self.assertEqual(via, lines[0]["via"])
            self.assertEqual(payload["msg_id"], lines[0]["msg_id"])

    def test_the_attempt_is_on_disk_before_the_rpc_leaves(self) -> None:
        """At most once: a crash mid-call must read as unknown, never as sent."""
        observed: dict[str, object] = {}

        def deliver(*, uuid: str, envelope: str, path: Path) -> dict:
            newest = self.sandbox.ledger.newest_send_id()
            assert newest is not None
            stored = self.sandbox.ledger.read_send(newest)
            assert stored is not None
            observed.update(
                {
                    row["thread_key"]: (row["status"], row["detail"])
                    for row in stored["targets"]
                    if row["thread_key"] == CODEX_KEY
                }
            )
            return {"status": "delivered-woke", "method": "wake", "detail": "ok"}

        self.send(codex_deliver=deliver)
        self.assertEqual(
            ("in-flight", "dispatch attempted, outcome unknown"), observed[CODEX_KEY]
        )

    def test_a_codex_target_with_no_socket_is_unreachable_without_a_call(self) -> None:
        calls: list[dict] = []

        def deliver(**kwargs: object) -> dict:
            calls.append(dict(kwargs))
            return {"status": "delivered-woke", "method": "wake", "detail": "ok"}

        homeless = session(
            provider="codex",
            uuid=CODEX_UUID,
            shpool_id="s-no-socket",
            title="Plain TUI Codex",
        )
        payload = self.send(inventory(homeless), "all", codex_deliver=deliver)
        self.assertEqual([], calls)
        self.assertEqual({CODEX_KEY: "unreachable"}, self.statuses(payload))
        self.assertIn("plain-TUI Codex", payload["targets"][0]["detail"])

    def test_an_unregistered_claude_target_is_never_messaged(self) -> None:
        payload = self.send(inventory(CLAUDE_ROW), "all", registry=[])
        self.assertEqual({CLAUDE_KEY: "unreachable"}, self.statuses(payload))
        self.assertEqual(
            "not registered (possible trust prompt)", payload["targets"][0]["detail"]
        )
        self.assertFalse(hasattr(self, "sender_argv"))

    def test_a_delivered_claim_with_no_receipt_yet_is_landed_not_failed(self) -> None:
        """The message is in the target's queue; a newborn takes it at its
        next turn boundary. Calling that failed is what makes a caller send
        the same message again."""
        payload = self.send(
            inventory(CLAUDE_ROW),
            "all",
            sender_rows=[{"name": "v2-ec", "send_ok": True}],
            found={CLAUDE_KEY: False},
        )
        self.assertEqual({CLAUDE_KEY: "landed-unconfirmed"}, self.statuses(payload))
        self.assertIn("has not shown it", payload["targets"][0]["detail"])
        # Still not delivery: only the target's own transcript says that.
        self.assertNotIn("delivered", self.statuses(payload)[CLAUDE_KEY])

    def test_a_skipped_name_is_reported_ambiguous_with_the_senders_reason(self) -> None:
        payload = self.send(
            inventory(CLAUDE_ROW),
            "all",
            sender_rows=[
                {"name": "v2-ec", "send_ok": False, "reason": "two rows named v2-ec"}
            ],
            found={CLAUDE_KEY: False},
        )
        self.assertEqual({CLAUDE_KEY: "ambiguous"}, self.statuses(payload))
        self.assertEqual("two rows named v2-ec", payload["targets"][0]["detail"])

    def test_an_unreported_target_is_ambiguous(self) -> None:
        payload = self.send(
            inventory(CLAUDE_ROW), "all", sender_rows=[], found={CLAUDE_KEY: False}
        )
        self.assertEqual({CLAUDE_KEY: "ambiguous"}, self.statuses(payload))
        self.assertIn("did not report", payload["targets"][0]["detail"])

    def test_a_sender_that_never_ran_fails_the_targets(self) -> None:
        payload = self.send(
            inventory(CLAUDE_ROW),
            "all",
            sender_ok=False,
            sender_detail="quota exhausted",
            found={CLAUDE_KEY: False},
        )
        self.assertEqual({CLAUDE_KEY: "failed"}, self.statuses(payload))
        self.assertIn("quota exhausted", payload["targets"][0]["detail"])

    def test_the_sender_runs_once_for_every_claude_target(self) -> None:
        self.send()
        self.assertEqual("claude", self.sender_argv[0])
        self.assertIn("--allowedTools", self.sender_argv)
        self.assertIn("ListAgents,SendMessage", self.sender_argv)
        self.assertEqual(self.sandbox.home, self.sender_cwd)
        instructions = self.sender_argv[2]
        self.assertIn('"v2-ec"', instructions)
        self.assertIn('"core-sh"', instructions)
        self.assertEqual(2, len(self.verified))

    def test_a_key_list_send_stays_one_linked_send_with_one_claude_run(self) -> None:
        """Follow-up-to-all is a single message id, not one send per session."""
        payload = self.send(selector=f"keys:{CLAUDE_KEY},{SECOND_CLAUDE_KEY},{CODEX_KEY}")
        self.assertEqual(
            [CLAUDE_KEY, SECOND_CLAUDE_KEY, CODEX_KEY],
            [row["thread_key"] for row in payload["targets"]],
        )
        self.assertEqual(1, len(self.sandbox.ledger.send_ids()))
        # One headless run carried both Claude targets.
        self.assertEqual(2, len(self.verified))
        instructions = self.sender_argv[2]
        self.assertIn('"v2-ec"', instructions)
        self.assertIn('"core-sh"', instructions)

    def test_a_key_list_send_refuses_a_malformed_key_before_delivering(self) -> None:
        with self.assertRaises(MessageError):
            self.send(selector=f"keys:{CODEX_KEY},nonsense")
        self.assertEqual([], self.sandbox.ledger.send_ids())
        self.assertEqual([], self.delivered)

    def test_the_global_kill_switch_refuses_the_send(self) -> None:
        with self.assertRaises(MessageError):
            self.send(environ={"SESSION_KIT_MSG": "0"})
        self.assertEqual([], self.sandbox.ledger.send_ids())

    def test_per_provider_switches_stand_targets_down(self) -> None:
        payload = self.send(environ={"SESSION_KIT_MSG_CODEX": "0"})
        self.assertEqual("unreachable", self.statuses(payload)[CODEX_KEY])
        self.assertEqual([], self.delivered)
        self.assertEqual("delivered-woke", self.statuses(payload)[CLAUDE_KEY])

        other = self.send(environ={"SESSION_KIT_MSG_CLAUDE": "0"})
        self.assertEqual("unreachable", self.statuses(other)[CLAUDE_KEY])
        self.assertIn("SESSION_KIT_MSG_CLAUDE=0", other["targets"][1]["detail"])

    def details(self, payload: dict) -> dict[str, str]:
        return {row["thread_key"]: row["detail"] for row in payload["targets"]}

    def test_a_target_waiting_on_the_operator_says_so_on_its_receipt(self) -> None:
        """Whatever the outcome, the receipt has to show whose turn it is."""
        payload = self.send(
            inventory(WAITING_CLAUDE_ROW, CLAUDE_ROW),
            "all",
            registry=[
                {"sessionId": WAITING_CLAUDE_UUID, "name": "asked-operator", "cwd": "/repo"},
                {"sessionId": CLAUDE_UUID, "name": "v2-ec", "cwd": "/repo"},
            ],
        )
        details = self.details(payload)
        self.assertTrue(details[WAITING_CLAUDE_KEY].endswith(WAITING_NOTE))
        self.assertNotIn(WAITING_NOTE, details[CLAUDE_KEY])
        self.assertEqual("delivered-woke", self.statuses(payload)[WAITING_CLAUDE_KEY])

    def test_the_waiting_note_survives_an_unreachable_outcome(self) -> None:
        waiting_codex = session(
            provider="codex",
            uuid=CODEX_UUID,
            shpool_id="s-no-socket",
            title="Codex asked the operator",
            status="needs your reply",
        )
        payload = self.send(inventory(waiting_codex), "all")
        self.assertEqual("unreachable", self.statuses(payload)[CODEX_KEY])
        self.assertTrue(self.details(payload)[CODEX_KEY].endswith(WAITING_NOTE))
        self.assertIn("plain-TUI Codex", self.details(payload)[CODEX_KEY])

    def test_the_waiting_note_is_on_the_record_before_any_dispatch(self) -> None:
        observed: dict[str, str] = {}

        def deliver(*, uuid: str, envelope: str, path: Path) -> dict:
            newest = self.sandbox.ledger.newest_send_id()
            assert newest is not None
            stored = self.sandbox.ledger.read_send(newest)
            assert stored is not None
            observed.update(
                {row["thread_key"]: row["detail"] for row in stored["targets"]}
            )
            return {"status": "delivered-woke", "method": "wake", "detail": "ok"}

        waiting_codex = dict(CODEX_ROW, agent_status="needs your reply")
        self.send(inventory(waiting_codex), "all", codex_deliver=deliver)
        self.assertTrue(observed[CODEX_KEY].endswith(WAITING_NOTE))
        self.assertIn("dispatch attempted, outcome unknown", observed[CODEX_KEY])

    def test_a_selector_matching_nothing_deliverable_is_refused(self) -> None:
        with self.assertRaises(MessageError):
            self.send(inventory(SHELL_ROW), "all")

    def test_message_ids_do_not_repeat_across_sends(self) -> None:
        first = self.send(inventory(CODEX_ROW), "all")
        second = self.send(inventory(CODEX_ROW), "all")
        self.assertNotEqual(first["msg_id"], second["msg_id"])
        self.assertEqual(2, len(self.sandbox.ledger.send_ids()))

    # ---- idempotency ----------------------------------------------------

    def test_the_same_key_resumes_one_message_instead_of_writing_a_second(self) -> None:
        first = self.send(inventory(CODEX_ROW), "all", idempotency_key="brief:s-codex")
        second = self.send(inventory(CODEX_ROW), "all", idempotency_key="brief:s-codex")
        self.assertEqual(first["msg_id"], second["msg_id"])
        self.assertFalse(first["resumed"])
        self.assertTrue(second["resumed"])
        self.assertEqual(1, len(self.sandbox.ledger.send_ids()))
        self.assertEqual("brief:s-codex", second["idempotency_key"])

    def test_concurrent_same_key_calls_reserve_once_before_dispatch(self) -> None:
        """A second process observes in-flight and never calls the provider."""
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []
        results: list[dict] = []
        failures: list[BaseException] = []

        def deliver(*, uuid: str, envelope: str, path: Path) -> dict:
            calls.append(uuid)
            entered.set()
            self.assertTrue(release.wait(5), "test barrier was not released")
            return {"status": "delivered-woke", "method": "wake", "detail": "ok"}

        def invoke() -> None:
            try:
                results.append(
                    self.send(
                        inventory(CODEX_ROW),
                        "all",
                        codex_deliver=deliver,
                        idempotency_key="brief:concurrent",
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        first = threading.Thread(target=invoke)
        second = threading.Thread(target=invoke)
        first.start()
        self.assertTrue(entered.wait(5), "first provider call did not start")
        second.start()
        second.join(5)
        self.assertFalse(second.is_alive(), "repeat blocked behind provider dispatch")
        release.set()
        first.join(5)
        self.assertFalse(first.is_alive(), "first provider call did not finish")

        self.assertEqual([], failures)
        self.assertEqual([CODEX_UUID], calls)
        self.assertEqual(2, len(results))
        self.assertEqual(1, len(self.sandbox.ledger.send_ids()))
        self.assertEqual(1, len(self.sandbox.ledger.read_thread(CODEX_KEY)))
        self.assertEqual({results[0]["msg_id"]}, {row["msg_id"] for row in results})

    def test_a_repeat_never_reaches_a_target_that_already_has_the_message(self) -> None:
        """The whole point: a caller that cannot prove delivery may retry."""
        self.send(inventory(CODEX_ROW), "all", idempotency_key="brief:s-codex")
        self.assertEqual(1, len(self.delivered))
        repeat = self.send(inventory(CODEX_ROW), "all", idempotency_key="brief:s-codex")
        self.assertEqual(1, len(self.delivered))
        self.assertEqual([CODEX_KEY], repeat["suppressed"])
        # And the conversation shows it once, not twice.
        self.assertEqual(1, len(self.sandbox.ledger.read_thread(CODEX_KEY)))

    def test_a_landed_unconfirmed_target_is_never_sent_to_again(self) -> None:
        """A message whose arrival is merely unproven has still arrived."""
        first = self.send(
            inventory(CLAUDE_ROW),
            "all",
            sender_rows=[{"name": "v2-ec", "send_ok": True}],
            found={CLAUDE_KEY: False},
            idempotency_key="brief:s-claude",
        )
        self.assertEqual({CLAUDE_KEY: "landed-unconfirmed"}, self.statuses(first))
        runs = self.sender_runs
        repeat = self.send(
            inventory(CLAUDE_ROW),
            "all",
            sender_rows=[{"name": "v2-ec", "send_ok": True}],
            found={CLAUDE_KEY: False},
            idempotency_key="brief:s-claude",
        )
        self.assertEqual(runs, self.sender_runs)
        self.assertEqual([CLAUDE_KEY], repeat["suppressed"])
        self.assertEqual({CLAUDE_KEY: "landed-unconfirmed"}, self.statuses(repeat))

    def test_a_repeat_re_sends_only_the_targets_that_never_landed(self) -> None:
        first = self.send(
            inventory(CLAUDE_ROW, BUSY_CLAUDE_ROW),
            "all",
            registry=[{"sessionId": CLAUDE_UUID, "name": "v2-ec", "cwd": "/repo"}],
            sender_rows=[{"name": "v2-ec", "send_ok": True}],
            found={CLAUDE_KEY: True},
            idempotency_key="brief:pair",
        )
        self.assertEqual("delivered-woke", self.statuses(first)[CLAUDE_KEY])
        self.assertEqual("unreachable", self.statuses(first)[SECOND_CLAUDE_KEY])
        repeat = self.send(
            inventory(CLAUDE_ROW, BUSY_CLAUDE_ROW),
            "all",
            sender_rows=[
                {"name": "v2-ec", "send_ok": True},
                {"name": "core-sh", "send_ok": True},
            ],
            found={SECOND_CLAUDE_KEY: True},
            idempotency_key="brief:pair",
        )
        self.assertEqual([CLAUDE_KEY], repeat["suppressed"])
        self.assertEqual(
            ["core-sh"], [target["display_name"] for target in self.verified]
        )
        self.assertEqual("delivered-midturn", self.statuses(repeat)[SECOND_CLAUDE_KEY])

    def test_a_repeat_reads_the_transcript_before_dispatching_again(self) -> None:
        """The first receipt can miss a message that has since appeared."""
        self.send(
            inventory(CLAUDE_ROW),
            "all",
            sender_rows=[],
            found={CLAUDE_KEY: False},
            idempotency_key="brief:s-claude",
        )
        self.assertEqual(1, self.sender_runs)
        repeat = self.send(
            inventory(CLAUDE_ROW),
            "all",
            found={CLAUDE_KEY: True},
            idempotency_key="brief:s-claude",
        )
        self.assertEqual(1, self.sender_runs)
        self.assertEqual({CLAUDE_KEY: "delivered-woke"}, self.statuses(repeat))
        # The pre-flight read is one pass, not another wait for the window.
        self.assertEqual(0.0, self.verify_calls[-1]["deadline_seconds"])

    def test_different_words_under_the_same_key_are_a_different_message(self) -> None:
        first = self.send(inventory(CODEX_ROW), "all", idempotency_key="brief:s-codex")
        second = self.send(
            inventory(CODEX_ROW), "all", text="Newer brief", idempotency_key="brief:s-codex"
        )
        self.assertNotEqual(first["msg_id"], second["msg_id"])
        self.assertFalse(second["resumed"])
        self.assertEqual(2, len(self.delivered))

    def test_a_malformed_key_is_refused_before_anything_is_sent(self) -> None:
        for bad in ("../escape", "with space", ".hidden", "a" * 129, "key/sub"):
            with self.assertRaises(MessageError):
                self.send(inventory(CODEX_ROW), "all", idempotency_key=bad)
        self.assertEqual([], self.sandbox.ledger.send_ids())
        self.assertEqual([], self.delivered)

    def test_a_send_without_a_key_records_none_and_repeats_freely(self) -> None:
        payload = self.send(inventory(CODEX_ROW), "all")
        self.assertIsNone(payload["idempotency_key"])
        self.assertEqual([], payload["suppressed"])
        stored = self.sandbox.ledger.read_send(payload["msg_id"])
        assert stored is not None
        self.assertIsNone(stored["idempotency_key"])

    def test_report_finds_a_message_by_the_purpose_that_sent_it(self) -> None:
        payload = self.send(inventory(CODEX_ROW), "all", idempotency_key="brief:s-codex")
        found = verbs.report(self.sandbox.ledger, None, "brief:s-codex")
        self.assertEqual(payload["msg_id"], found["msg_id"])
        # A purpose that has never sent anything is a state, not an error: the
        # caller polling it does not yet know whether a send happened.
        self.assertEqual(
            {"msg_id": None, "send": None, "threads": {}},
            verbs.report(self.sandbox.ledger, None, "brief:never-used"),
        )
        with self.assertRaises(MessageError):
            verbs.report(self.sandbox.ledger, None, "../escape")

    def test_a_key_with_a_missing_send_record_refuses_an_unknown_retry(self) -> None:
        first = self.send(inventory(CODEX_ROW), "all", idempotency_key="brief:s-codex")
        self.sandbox.ledger.send_path(first["msg_id"]).unlink()
        provider_calls = len(self.delivered)
        with self.assertRaisesRegex(MessageError, "outcome is unknown"):
            self.send(inventory(CODEX_ROW), "all", idempotency_key="brief:s-codex")
        self.assertEqual(provider_calls, len(self.delivered))

    def test_a_failed_dispatch_outcome_is_unknown_and_not_retried(self) -> None:
        calls = 0

        def failed(**kwargs: object) -> dict:
            nonlocal calls
            calls += 1
            return {
                "status": "failed",
                "method": "wake",
                "detail": "provider outcome unknown",
            }

        first = self.send(
            inventory(CODEX_ROW),
            "all",
            codex_deliver=failed,
            idempotency_key="brief:unknown",
        )
        second = self.send(
            inventory(CODEX_ROW),
            "all",
            codex_deliver=failed,
            idempotency_key="brief:unknown",
        )
        self.assertEqual(1, calls)
        self.assertEqual([CODEX_KEY], second["suppressed"])
        self.assertEqual(first["msg_id"], second["msg_id"])

    def test_an_explicit_unreachable_target_is_retryable(self) -> None:
        calls = 0

        def delivered(**kwargs: object) -> dict:
            nonlocal calls
            calls += 1
            return {"status": "delivered-woke", "method": "wake", "detail": "ok"}

        first = self.send(
            inventory(CODEX_ROW),
            "all",
            environ={"SESSION_KIT_MSG_CODEX": "0"},
            codex_deliver=delivered,
            idempotency_key="brief:unreachable",
        )
        second = self.send(
            inventory(CODEX_ROW),
            "all",
            codex_deliver=delivered,
            idempotency_key="brief:unreachable",
        )
        self.assertEqual({CODEX_KEY: "unreachable"}, self.statuses(first))
        self.assertEqual({CODEX_KEY: "delivered-woke"}, self.statuses(second))
        self.assertEqual(1, calls)
        self.assertEqual(first["msg_id"], second["msg_id"])


class ReplyAndReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.ledger = self.sandbox.ledger
        self.clock = 1_700_000_000.0

    def tearDown(self) -> None:
        self.sandbox.close()

    def tick(self) -> float:
        self.clock += 1.0
        return self.clock

    def make_send(self) -> str:
        payload = verbs.send(
            inventory=inventory(CODEX_ROW),
            selector="all",
            text="Status?",
            fyi=False,
            state_dir=self.sandbox.state,
            environ={},
            home=self.sandbox.home,
            ledger=self.ledger,
            clock=self.tick,
            registry_entries=lambda: [],
            codex_deliver=lambda **_kwargs: {
                "status": "delivered-woke",
                "method": "wake",
                "detail": "ok",
            },
        )
        return str(payload["msg_id"])

    def test_a_targeted_reply_marks_the_row_replied_and_flags_unread(self) -> None:
        msg_id = self.make_send()
        outcome = verbs.reply(
            self.ledger,
            msg_id=msg_id,
            text="Runner is green.",
            caller={"provider": "codex", "uuid": CODEX_UUID},
            clock=self.tick,
        )
        self.assertTrue(outcome["ok"])
        stored = self.ledger.read_send(msg_id)
        assert stored is not None
        self.assertEqual("replied", stored["targets"][0]["status"])
        self.assertEqual("Runner is green.", stored["targets"][0]["detail"])
        self.assertEqual([CODEX_KEY], self.ledger.unread_keys())
        lines = self.ledger.read_thread(CODEX_KEY)
        self.assertEqual("in", lines[-1]["dir"])
        self.assertEqual("reply", lines[-1]["via"])
        self.assertEqual("Runner is green.", lines[-1]["text"])

    def test_an_unknown_message_id_is_kept_as_unsolicited_and_refused(self) -> None:
        outcome = verbs.reply(
            self.ledger,
            msg_id="deadbeef",
            text="anyone there?",
            caller={"provider": "codex", "uuid": CODEX_UUID},
            clock=self.tick,
        )
        self.assertFalse(outcome["ok"])
        self.assertIn("no stored send", str(outcome["reason"]))
        lines = self.ledger.read_thread(CODEX_KEY)
        self.assertEqual("unsolicited", lines[-1]["via"])
        self.assertEqual("anyone there?", lines[-1]["text"])
        self.assertEqual([CODEX_KEY], self.ledger.unread_keys())

    def test_a_reply_from_a_session_that_was_not_targeted_is_kept_and_refused(self) -> None:
        msg_id = self.make_send()
        outcome = verbs.reply(
            self.ledger,
            msg_id=msg_id,
            text="not my message",
            caller={"provider": "claude", "uuid": CLAUDE_UUID},
            clock=self.tick,
        )
        self.assertFalse(outcome["ok"])
        self.assertIn("not a target", str(outcome["reason"]))
        self.assertEqual(
            "unsolicited", self.ledger.read_thread(CLAUDE_KEY)[-1]["via"]
        )
        stored = self.ledger.read_send(msg_id)
        assert stored is not None
        self.assertNotEqual("replied", stored["targets"][0]["status"])

    def test_a_reply_that_lands_mid_send_is_not_overwritten(self) -> None:
        """The delivery write must never demote an answer that already arrived."""
        msg_id = self.make_send()
        verbs.reply(
            self.ledger,
            msg_id=msg_id,
            text="already answered",
            caller={"provider": "codex", "uuid": CODEX_UUID},
            clock=self.tick,
        )
        verbs._commit(
            self.ledger,
            msg_id,
            CODEX_KEY,
            status="failed",
            detail="a late delivery write",
            method="wake",
            updated_unix_ms=1,
        )
        stored = self.ledger.read_send(msg_id)
        assert stored is not None
        self.assertEqual("replied", stored["targets"][0]["status"])

    def test_a_reply_needs_an_exact_caller_and_some_text(self) -> None:
        msg_id = self.make_send()
        with self.assertRaises(MessageError):
            verbs.reply(
                self.ledger,
                msg_id=msg_id,
                text="hi",
                caller={"provider": "shell", "uuid": CODEX_UUID},
            )
        with self.assertRaises(MessageError):
            verbs.reply(
                self.ledger,
                msg_id=msg_id,
                text="   ",
                caller={"provider": "codex", "uuid": CODEX_UUID},
            )

    def test_report_defaults_to_the_newest_send(self) -> None:
        first = self.make_send()
        second = self.make_send()
        verbs.reply(
            self.ledger,
            msg_id=second,
            text="done",
            caller={"provider": "codex", "uuid": CODEX_UUID},
            clock=self.tick,
        )
        payload = verbs.report(self.ledger)
        self.assertEqual(second, payload["msg_id"])
        self.assertNotEqual(first, payload["msg_id"])
        self.assertTrue(payload["threads"][CODEX_KEY]["unread"])

    def test_reading_a_report_never_clears_an_unread_mark(self) -> None:
        """The console polls this every couple of seconds; the cue must not lie."""
        msg_id = self.make_send()
        verbs.reply(
            self.ledger,
            msg_id=msg_id,
            text="done",
            caller={"provider": "codex", "uuid": CODEX_UUID},
            clock=self.tick,
        )
        for _ in range(3):
            payload = verbs.report(self.ledger)
            self.assertTrue(payload["threads"][CODEX_KEY]["unread"])
        self.assertNotIn("unread_cleared", payload)
        self.assertEqual(1, self.ledger.unread_count())

    def test_marking_a_thread_read_clears_it_once_and_stays_quiet_after(self) -> None:
        msg_id = self.make_send()
        verbs.reply(
            self.ledger,
            msg_id=msg_id,
            text="done",
            caller={"provider": "codex", "uuid": CODEX_UUID},
            clock=self.tick,
        )
        first = verbs.mark_read(self.ledger, CODEX_KEY)
        self.assertEqual({"thread_key": CODEX_KEY, "cleared": True}, first)
        self.assertEqual(0, self.ledger.unread_count())
        again = verbs.mark_read(self.ledger, CODEX_KEY)
        self.assertEqual({"thread_key": CODEX_KEY, "cleared": False}, again)
        self.assertFalse(verbs.report(self.ledger)["threads"][CODEX_KEY]["unread"])

    def test_marking_an_untouched_thread_read_is_not_an_error(self) -> None:
        self.assertEqual(
            {"thread_key": CLAUDE_KEY, "cleared": False},
            verbs.mark_read(self.ledger, CLAUDE_KEY),
        )

    def test_mark_read_refuses_anything_that_is_not_a_thread_key(self) -> None:
        for bad in ("", "codex:nope", "shell:" + CODEX_UUID, "../escape"):
            with self.assertRaises(MessageError):
                verbs.mark_read(self.ledger, bad)

    def test_report_on_an_empty_store_is_a_state_not_a_failure(self) -> None:
        """The console polls before the first send exists."""
        self.assertEqual(
            {"msg_id": None, "send": None, "threads": {}}, verbs.report(self.ledger)
        )

    def test_report_tails_a_thread_at_twenty_lines(self) -> None:
        msg_id = self.make_send()
        for index in range(25):
            self.ledger.append_thread(
                CODEX_KEY,
                ts_unix_ms=index,
                direction="in",
                msg_id=msg_id,
                text=f"line {index}",
                via="reply",
            )
        payload = verbs.report(self.ledger, msg_id)
        self.assertEqual(20, len(payload["threads"][CODEX_KEY]["lines"]))

    def test_report_refuses_an_unknown_or_malformed_id(self) -> None:
        """An id the operator typed is still an error when it does not exist."""
        with self.assertRaises(MessageError):
            verbs.report(self.ledger, "zzzz")
        with self.assertRaises(MessageError):
            verbs.report(self.ledger, "deadbeef")
        self.make_send()
        with self.assertRaises(MessageError):
            verbs.report(self.ledger, "deadbeef")

    def test_list_and_unread_count_are_cheap_views(self) -> None:
        first = self.make_send()
        second = self.make_send()
        rows = verbs.list_sends(self.ledger)["sends"]
        self.assertEqual([second, first], [row["msg_id"] for row in rows])
        self.assertEqual("Status?", rows[0]["preview"])
        self.assertEqual({"count": 0}, verbs.unread_count(self.ledger))
        verbs.reply(
            self.ledger,
            msg_id=second,
            text="done",
            caller={"provider": "codex", "uuid": CODEX_UUID},
            clock=self.tick,
        )
        self.assertEqual({"count": 1}, verbs.unread_count(self.ledger))
        self.assertEqual(1, verbs.list_sends(self.ledger)["sends"][0]["n_replied"])


class FacadeCommandTests(unittest.TestCase):
    """The `msg` verbs through the real facade process, in a sandboxed home."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".session-kit-msgcli-", dir=REPO
        )
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.proc = self.base / "proc"
        self.proc.mkdir()
        started = int(time.time() * 1000) - 60_000
        self._write_proc(10, 1, "shpool", b"shpool\0daemon\0")
        self._write_proc(
            100, 10, "bash", b"bash\0", b"SHPOOL_SESSION_NAME=sk-msg-a\0"
        )
        self._write_proc(
            101, 100, "claude", b"claude\0--session-id\0" + CLAUDE_UUID.encode() + b"\0"
        )
        self.shpool_json = self.base / "shpool.json"
        self.shpool_json.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "sk-msg-a",
                            "status": "Attached",
                            "started_at_unix_ms": started,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.agents_json = self.base / "agents.json"
        self.agents_json.write_text(
            json.dumps(
                [
                    {
                        "pid": 101,
                        "sessionId": CLAUDE_UUID,
                        "cwd": str(self.base),
                        "name": "core-py",
                        "status": "idle",
                        "startedAt": started,
                    }
                ]
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_proc(
        self, pid: int, ppid: int, comm: str, cmdline: bytes, environ: bytes = b""
    ) -> None:
        directory = self.proc / str(pid)
        directory.mkdir()
        fields = ["S", str(ppid), *(["0"] * 17), str(pid * 100)]
        (directory / "stat").write_text(
            f"{pid} ({comm}) {' '.join(fields)}\n", encoding="utf-8"
        )
        (directory / "comm").write_text(comm + "\n", encoding="utf-8")
        (directory / "cmdline").write_bytes(cmdline)
        (directory / "environ").write_bytes(environ)

    def run_msg(self, *arguments: str, extra: dict | None = None) -> subprocess.CompletedProcess:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(self.home),
            "SESSION_KIT_STATE_DIR": str(self.state),
            "SESSION_KIT_TESTING": "1",
            "SESSION_KIT_PROC_ROOT": str(self.proc),
            "SESSION_KIT_SHPOOL_JSON_FILE": str(self.shpool_json),
            "SESSION_KIT_CLAUDE_JSON_FILE": str(self.agents_json),
            "SESSION_KIT_CODEX_AUTOTITLE": "0",
        }
        environment.update(extra or {})
        return subprocess.run(
            [sys.executable, str(CORE), "msg", *arguments],
            cwd=REPO,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_empty_views_are_valid_json_and_exit_zero(self) -> None:
        listed = self.run_msg("list")
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertEqual({"sends": []}, json.loads(listed.stdout))
        unread = self.run_msg("unread-count")
        self.assertEqual({"count": 0}, json.loads(unread.stdout))

    def test_report_without_a_send_is_a_clean_empty_answer(self) -> None:
        """The Phase 2 console polls report before the first send exists."""
        result = self.run_msg("report")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {"msg_id": None, "send": None, "threads": {}}, json.loads(result.stdout)
        )
        self.assertEqual("", result.stderr)

    def test_report_with_an_unknown_id_still_exits_one(self) -> None:
        result = self.run_msg("report", "--id", "deadbeef")
        self.assertEqual(1, result.returncode)
        self.assertIn("no stored send", result.stderr)

    def test_mark_read_on_an_untouched_thread_exits_zero(self) -> None:
        result = self.run_msg("mark-read", "--thread", CLAUDE_KEY)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {"thread_key": CLAUDE_KEY, "cleared": False}, json.loads(result.stdout)
        )

    def test_report_by_key_is_reachable_and_empty_before_any_send(self) -> None:
        """The supervisor polls this to learn what its own brief did."""
        result = self.run_msg("report", "--key", "supervisor-brief:sk-msg-a")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {"msg_id": None, "send": None, "threads": {}}, json.loads(result.stdout)
        )
        refused = self.run_msg("report", "--key", "../escape")
        self.assertEqual(1, refused.returncode)
        self.assertIn("idempotency key", refused.stderr)

    def test_mark_read_refuses_a_bad_thread_key(self) -> None:
        result = self.run_msg("mark-read", "--thread", "codex:nope")
        self.assertEqual(1, result.returncode)
        self.assertIn("thread key", result.stderr)

    def test_resolve_finds_the_sandboxed_session(self) -> None:
        result = self.run_msg("resolve", "--target", "all")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            [CLAUDE_KEY], [target["thread_key"] for target in payload["targets"]]
        )
        self.assertEqual("sk-msg-a", payload["targets"][0]["shpool_id"])
        self.assertEqual(1, payload["counts"]["claude"])
        self.assertEqual(0, payload["counts"]["unreachable_hint"])

    def test_resolve_by_shpool_id_and_by_thread_key_agree(self) -> None:
        by_id = json.loads(
            self.run_msg("resolve", "--target", "sk-msg-a").stdout
        )
        by_key = json.loads(
            self.run_msg("resolve", "--target", f"key:{CLAUDE_KEY}").stdout
        )
        by_keys = json.loads(
            self.run_msg("resolve", "--target", f"keys:{CLAUDE_KEY}").stdout
        )
        self.assertEqual(by_id["targets"], by_key["targets"])
        self.assertEqual(by_id["targets"], by_keys["targets"])
        self.assertEqual("idle", by_id["targets"][0]["agent_status"])

    def test_a_malformed_key_list_exits_one(self) -> None:
        result = self.run_msg("resolve", "--target", "keys:nonsense")
        self.assertEqual(1, result.returncode)
        self.assertIn("not a thread key", result.stderr)

    def test_an_unknown_selector_exits_one(self) -> None:
        result = self.run_msg("resolve", "--target", "nope")
        self.assertEqual(1, result.returncode)
        self.assertIn("no live session matches", result.stderr)

    def test_the_kill_switch_refuses_before_any_provider_is_touched(self) -> None:
        result = self.run_msg(
            "send", "--target", "all", "--text", "hi", extra={"SESSION_KIT_MSG": "0"}
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("SESSION_KIT_MSG=0", result.stderr)
        self.assertFalse((self.state / "messages" / "sends").exists())

    def test_reply_outside_a_managed_session_is_refused(self) -> None:
        result = self.run_msg("reply", "deadbeef", "--text", "hi")
        self.assertEqual(1, result.returncode)
        self.assertIn("outside a managed shell", result.stderr)

    def test_the_verbs_are_all_reachable_from_the_parser(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CORE), "msg", "--help"],
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, result.returncode)
        self.assertIn(
            "{resolve,send,report,mark-read,reply,list,unread-count,queue,intake}",
            result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
