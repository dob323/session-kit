"""Envelope identity and the owner-private message store.

Every test runs against a disposable state directory; nothing here reads or
writes a real home, a real socket, or a real session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_messages import envelope as env_mod  # noqa: E402
from sessionkit_messages.envelope import (  # noqa: E402
    MessageError,
    compose_envelope,
    dispatch_row,
    landed,
    new_msg_id,
    preview,
    split_thread_key,
    target_row,
    thread_key,
    valid_idempotency_key,
    valid_msg_id,
)
from sessionkit_messages.ledger import (  # noqa: E402
    MAX_SENDS_KEPT,
    RETENTION_MS,
    Ledger,
    messages_root,
    now_unix_ms,
)

UUID_A = "019fdf1e-8b4c-7573-a089-be495bfece6a"
UUID_B = "dcbdf940-4eda-4967-8e41-23a5760c32b5"
KEY_A = f"codex:{UUID_A}"
KEY_B = f"claude:{UUID_B}"


class Sandbox:
    """A disposable SK_STATE_DIR with no relationship to the real home."""

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".session-kit-messages-", dir=REPO
        )
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)

    def close(self) -> None:
        self.temporary.cleanup()


class EnvelopeTests(unittest.TestCase):
    def test_thread_key_refuses_anything_that_is_not_an_exact_identity(self) -> None:
        self.assertEqual(KEY_A, thread_key("codex", UUID_A))
        self.assertEqual(("codex", UUID_A), split_thread_key(KEY_A))
        for provider, uuid in (
            ("shell", UUID_A),
            ("codex", "not-a-uuid"),
            ("codex", ""),
            ("codex", f"../../{UUID_A}"),
            ("claude", None),
        ):
            with self.assertRaises(MessageError):
                thread_key(provider, uuid)

    def test_thread_key_uppercase_uuid_is_normalised_not_duplicated(self) -> None:
        self.assertEqual(KEY_A, thread_key("codex", UUID_A.upper()))

    def test_message_ids_are_eight_hex_and_avoid_taken_ids(self) -> None:
        minted = new_msg_id(1_700_000_000_000)
        self.assertTrue(valid_msg_id(minted))
        taken = {minted}
        second = new_msg_id(1_700_000_000_000, taken=lambda value: value in taken)
        self.assertNotEqual(minted, second)

    def test_message_ids_stay_unique_when_the_clock_repeats(self) -> None:
        """A frozen clock must not produce a frozen id: entropy carries it."""
        ids = {new_msg_id(1_700_000_000_000) for _ in range(200)}
        self.assertGreater(len(ids), 190)

    def test_message_id_minting_gives_up_rather_than_reusing_an_id(self) -> None:
        with self.assertRaises(MessageError):
            new_msg_id(1, taken=lambda _value: True)

    def test_envelope_is_the_frozen_block(self) -> None:
        text = compose_envelope("abcd1234", "Status on the burn runner?")
        self.assertEqual(
            text,
            "[session-kit operator message abcd1234]\n"
            "From: the operator. When you have your answer, reply with EXACTLY "
            "this command:\n"
            '  sp msg reply abcd1234 "your one-line answer"\n'
            "Do not reply via SendMessage — the sending process is ephemeral and "
            "replies to it\n"
            "misdeliver. Then continue your prior work.\n"
            "---\n"
            "Status on the burn runner?",
        )

    def test_fyi_envelope_says_no_reply_instead_of_saying_nothing(self) -> None:
        text = compose_envelope("abcd1234", "Deploy is done.", fyi=True)
        self.assertEqual(
            text,
            "[session-kit operator message abcd1234]\n"
            "FYI only — no reply needed.\n"
            "---\n"
            "Deploy is done.",
        )
        self.assertNotIn("sp msg reply", text)
        self.assertNotIn("misdeliver", text)

    def test_envelope_refuses_a_bad_id_or_empty_text(self) -> None:
        with self.assertRaises(MessageError):
            compose_envelope("nope", "text")
        with self.assertRaises(MessageError):
            compose_envelope("abcd1234", "   \n  ")

    def test_envelope_text_is_bounded_and_control_stripped(self) -> None:
        text = compose_envelope("abcd1234", "a\x00b\x07c\r\nd" + "x" * 20_000)
        body = text.split("---\n", 1)[1]
        self.assertTrue(body.startswith("abc\nd"))
        self.assertLessEqual(len(body), env_mod.MAX_OPERATOR_TEXT)

    def test_an_idempotency_key_is_bounded_and_cannot_be_a_path(self) -> None:
        """It becomes a filename, so a path is unrepresentable, not filtered."""
        for good in ("brief:s-codex", "a", "supervisor-brief:s20260809-1", "a" * 128):
            self.assertEqual(good, valid_idempotency_key(good))
        for bad in (
            "",
            "   ",
            "../escape",
            "key/sub",
            ".hidden",
            "-leading",
            "with space",
            "a" * 129,
            b"brief",
            None,
        ):
            self.assertEqual("", valid_idempotency_key(bad))

    def test_landed_covers_every_arrival_and_nothing_else(self) -> None:
        """A message that reached the target must never be sent again."""
        for arrived in (
            "delivered-woke",
            "delivered-midturn",
            "landed-unconfirmed",
            "replied",
        ):
            self.assertTrue(landed(arrived), arrived)
        for missed in (
            "pre-dispatch",
            "in-flight",
            "failed",
            "unreachable",
            "ambiguous",
            "",
            None,
            7,
        ):
            self.assertFalse(landed(missed), missed)

    def test_preview_flattens_and_bounds(self) -> None:
        self.assertEqual("a b c", preview("a\n  b\tc"))
        self.assertEqual(60, len(preview("y" * 200)))

    def test_a_dispatch_row_starts_undelivered(self) -> None:
        row = dispatch_row(
            target_row(
                key=KEY_A,
                provider="codex",
                shpool_id="s1",
                uuid=UUID_A,
                terminal_number=3,
                title="Codex work",
                agent_status="idle",
            ),
            1234,
        )
        self.assertEqual("pre-dispatch", row["status"])
        self.assertIsNone(row["method"])
        self.assertEqual(1234, row["updated_unix_ms"])

    def test_resolve_rows_carry_exactly_the_frozen_fields(self) -> None:
        row = target_row(
            key=KEY_A,
            provider="codex",
            shpool_id="s1",
            uuid=UUID_A,
            terminal_number=None,
            title="Codex work",
            agent_status="working",
        )
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
            set(row),
        )
        self.assertEqual("working", row["agent_status"])


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.ledger = Ledger(self.sandbox.state)

    def tearDown(self) -> None:
        self.sandbox.close()

    def _send(self, msg_id: str, created: int, keys: tuple[str, ...] = (KEY_A,)) -> dict:
        record = {
            "msg_id": msg_id,
            "created_unix_ms": created,
            "operator_text": f"text for {msg_id}",
            "fyi": False,
            "targets": [
                dispatch_row(
                    target_row(
                        key=key,
                        provider=key.split(":", 1)[0],
                        shpool_id="s1",
                        uuid=key.split(":", 1)[1],
                        terminal_number=None,
                        title="A session",
                        agent_status="idle",
                    ),
                    created,
                )
                for key in keys
            ],
        }
        self.ledger.write_send(record)
        return record

    def test_store_lives_under_the_supplied_state_dir_only(self) -> None:
        self.assertEqual(
            self.sandbox.state / "messages", messages_root(self.sandbox.state)
        )
        self.ledger.ensure()
        for path in (
            self.ledger.root,
            self.ledger.sends,
            self.ledger.keys,
            self.ledger.threads,
            self.ledger.unread,
        ):
            self.assertEqual(0o700, stat.S_IMODE(path.stat().st_mode))

    def test_a_key_claims_one_message_id_and_reads_it_back(self) -> None:
        self._send("aaaa1111", 1000)
        self.assertIsNone(self.ledger.msg_id_for_key("brief:s1"))
        self.ledger.claim_key("brief:s1", "aaaa1111")
        self.assertEqual("aaaa1111", self.ledger.msg_id_for_key("brief:s1"))
        self.assertEqual(
            0o600, stat.S_IMODE(self.ledger.key_path("brief:s1").stat().st_mode)
        )

    def test_a_claim_whose_send_is_gone_is_not_a_claim(self) -> None:
        """Resuming an id with no record leaves a repeat nothing to suppress."""
        self._send("aaaa1111", 1000)
        self.ledger.claim_key("brief:s1", "aaaa1111")
        self.ledger.send_path("aaaa1111").unlink()
        self.assertIsNone(self.ledger.msg_id_for_key("brief:s1"))

    def test_a_bad_key_or_id_never_reaches_the_filesystem(self) -> None:
        for bad in ("../escape", "", "key/sub", ".hidden"):
            with self.assertRaises(MessageError):
                self.ledger.key_path(bad)
            with self.assertRaises(MessageError):
                self.ledger.claim_key(bad, "aaaa1111")
            self.assertIsNone(self.ledger.msg_id_for_key(bad))
        with self.assertRaises(MessageError):
            self.ledger.claim_key("brief:s1", "nope")

    def test_a_corrupt_claim_reads_as_no_claim(self) -> None:
        self.ledger.ensure()
        self.ledger.key_path("brief:s1").write_text("not an id\n", encoding="utf-8")
        self.assertIsNone(self.ledger.msg_id_for_key("brief:s1"))

    def test_retention_drops_a_claim_whose_send_expired(self) -> None:
        now = now_unix_ms()
        self._send("0000dead", now - RETENTION_MS - 60_000)
        self._send("aaaa1111", now)
        self.ledger.claim_key("brief:old", "0000dead")
        self.ledger.claim_key("brief:live", "aaaa1111")
        dropped = self.ledger.prune(now)
        self.assertEqual(["brief:old"], dropped["keys"])
        self.assertFalse(self.ledger.key_path("brief:old").exists())
        self.assertEqual("aaaa1111", self.ledger.msg_id_for_key("brief:live"))

    def test_send_records_are_owner_only_and_round_trip(self) -> None:
        self._send("aaaa1111", 1000)
        path = self.ledger.send_path("aaaa1111")
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        stored = self.ledger.read_send("aaaa1111")
        assert stored is not None
        self.assertEqual("text for aaaa1111", stored["operator_text"])
        self.assertTrue(self.ledger.has_send("aaaa1111"))
        self.assertFalse(self.ledger.has_send("bbbb2222"))

    def test_a_bad_message_id_never_reaches_the_filesystem(self) -> None:
        for bad in ("../escape", "AAAA1111!", "", "aaaa11"):
            with self.assertRaises(MessageError):
                self.ledger.send_path(bad)

    def test_a_bad_thread_key_never_reaches_the_filesystem(self) -> None:
        for bad in ("codex:../../etc/passwd", "shell:" + UUID_A, "codex:nope"):
            with self.assertRaises(MessageError):
                self.ledger.thread_path(bad)
            with self.assertRaises(MessageError):
                self.ledger.unread_path(bad)

    def test_threads_append_and_read_back_the_tail(self) -> None:
        for index in range(30):
            self.ledger.append_thread(
                KEY_A,
                ts_unix_ms=1000 + index,
                direction="out" if index % 2 else "in",
                msg_id="aaaa1111",
                text=f"line {index}",
                via="wake",
            )
        lines = self.ledger.read_thread(KEY_A, 20)
        self.assertEqual(20, len(lines))
        self.assertEqual("line 10", lines[0]["text"])
        self.assertEqual("line 29", lines[-1]["text"])
        self.assertEqual(
            0o600, stat.S_IMODE(self.ledger.thread_path(KEY_A).stat().st_mode)
        )

    def test_thread_reads_survive_a_corrupt_line(self) -> None:
        self.ledger.append_thread(
            KEY_A, ts_unix_ms=1, direction="out", msg_id="aaaa1111", text="ok", via="wake"
        )
        with self.ledger.thread_path(KEY_A).open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        self.assertEqual(["ok"], [line["text"] for line in self.ledger.read_thread(KEY_A)])

    def test_thread_direction_is_constrained(self) -> None:
        with self.assertRaises(MessageError):
            self.ledger.append_thread(
                KEY_A, ts_unix_ms=1, direction="sideways", msg_id="a", text="t", via="v"
            )

    def test_unread_markers_are_zero_byte_and_countable(self) -> None:
        self.ledger.mark_unread(KEY_A)
        self.ledger.mark_unread(KEY_B)
        self.ledger.mark_unread(KEY_A)
        self.assertEqual(2, self.ledger.unread_count())
        self.assertEqual(0, self.ledger.unread_path(KEY_A).stat().st_size)
        self.ledger.clear_unread(KEY_A)
        self.assertEqual([KEY_B], self.ledger.unread_keys())
        self.ledger.clear_unread(KEY_A)
        self.assertEqual(1, self.ledger.unread_count())

    def test_update_send_is_read_modify_write_under_the_lock(self) -> None:
        self._send("aaaa1111", 1000)

        def mutate(record: dict) -> dict:
            record["targets"][0]["status"] = "replied"
            return record

        self.ledger.update_send("aaaa1111", mutate)
        stored = self.ledger.read_send("aaaa1111")
        assert stored is not None
        self.assertEqual("replied", stored["targets"][0]["status"])

    def test_update_send_refuses_an_unknown_id(self) -> None:
        with self.assertRaises(MessageError):
            self.ledger.update_send("aaaa1111", lambda record: record)

    def test_sends_are_ordered_newest_first_by_recorded_time(self) -> None:
        self._send("aaaa1111", 3000)
        self._send("bbbb2222", 1000)
        self._send("cccc3333", 2000)
        self.assertEqual(
            ["aaaa1111", "cccc3333", "bbbb2222"], self.ledger.send_ids()
        )
        self.assertEqual("aaaa1111", self.ledger.newest_send_id())

    def test_list_view_counts_replies_and_unreachable_targets(self) -> None:
        record = self._send("aaaa1111", 1000, keys=(KEY_A, KEY_B))
        record["targets"][0]["status"] = "replied"
        record["targets"][1]["status"] = "unreachable"
        record["operator_text"] = "z" * 200
        self.ledger.write_send(record)
        rows = self.ledger.list_sends()
        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0]["n_targets"])
        self.assertEqual(1, rows[0]["n_replied"])
        self.assertEqual(1, rows[0]["n_unreachable"])
        self.assertEqual(60, len(rows[0]["preview"]))

    def test_retention_drops_old_sends_and_keeps_the_newest_five_hundred(self) -> None:
        now = now_unix_ms()
        self._send("0000dead", now - RETENTION_MS - 60_000)
        for index in range(MAX_SENDS_KEPT + 5):
            self._send(f"{index:08x}", now - index)
        dropped = self.ledger.prune(now)
        remaining = self.ledger.send_ids()
        self.assertEqual(MAX_SENDS_KEPT, len(remaining))
        self.assertIn("0000dead", dropped["sends"])
        self.assertNotIn("0000dead", remaining)

    def test_retention_keeps_a_thread_whose_send_is_gone(self) -> None:
        """A late reply still needs somewhere to land after its send expires."""
        now = now_unix_ms()
        self._send("0000dead", now - RETENTION_MS - 60_000)
        self.ledger.append_thread(
            KEY_A, ts_unix_ms=now, direction="out", msg_id="0000dead", text="hi", via="wake"
        )
        self.ledger.prune(now)
        self.assertFalse(self.ledger.send_path("0000dead").exists())
        self.assertTrue(self.ledger.thread_path(KEY_A).exists())

    def test_retention_drops_a_thread_only_once_it_is_silent(self) -> None:
        now = now_unix_ms()
        self.ledger.append_thread(
            KEY_A, ts_unix_ms=1, direction="out", msg_id="0000dead", text="hi", via="wake"
        )
        self.ledger.mark_unread(KEY_A)
        stale = (now - RETENTION_MS - 86_400_000) / 1000
        os.utime(self.ledger.thread_path(KEY_A), (stale, stale))
        dropped = self.ledger.prune(now)
        self.assertEqual([KEY_A], dropped["threads"])
        self.assertFalse(self.ledger.thread_path(KEY_A).exists())
        self.assertEqual(0, self.ledger.unread_count())

    def test_writes_are_atomic_and_leave_no_temporary_behind(self) -> None:
        self._send("aaaa1111", 1000)
        self._send("aaaa1111", 2000)
        leftovers = [name for name in os.listdir(self.ledger.sends) if name.startswith(".")]
        self.assertEqual([], leftovers)
        stored = self.ledger.read_send("aaaa1111")
        assert stored is not None
        self.assertEqual(2000, stored["created_unix_ms"])

    def test_a_symlinked_send_is_not_read(self) -> None:
        self.ledger.ensure()
        outside = self.sandbox.base / "outside.json"
        outside.write_text(json.dumps({"msg_id": "aaaa1111"}), encoding="utf-8")
        self.ledger.send_path("aaaa1111").symlink_to(outside)
        self.assertIsNone(self.ledger.read_send("aaaa1111"))


if __name__ == "__main__":
    unittest.main()
