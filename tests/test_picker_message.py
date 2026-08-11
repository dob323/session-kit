"""The picker's envelope cue and its compose action.

Both drive the real login picker through the pty harness in
tests.test_login, against its isolated fixture tree: no live session, no
real message store, and an sp that only records what it was asked to do.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from tests.test_login import LoginFixture, inventory, row, run_pty


def unreadable(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o000)


class PickerMessageCueTests(unittest.TestCase):
    """The picker's envelope cue and its compose action."""

    def setUp(self) -> None:
        self.fixture = LoginFixture(inventory(row("ready", number=9)))

    def tearDown(self) -> None:
        self.fixture.close()

    def mark_unread(self, *keys: str) -> Path:
        """One unread reply per key, each with the send it answers.

        The send matters: a marker with no send behind it is the orphan case,
        which renders differently on purpose. A fixture that only ever built
        orphans would let a dead-row bug through without a word.
        """
        import json

        messages = self.fixture.state / "messages"
        unread = messages / "unread"
        for name in ("unread", "threads", "sends"):
            (messages / name).mkdir(parents=True, exist_ok=True)
        for index, key in enumerate(keys):
            (unread / key).write_bytes(b"")
            (messages / "threads" / f"{key}.jsonl").write_text(
                "{}\n", encoding="utf-8"
            )
            msg_id = f"{index:08x}"
            (messages / "sends" / f"{msg_id}.json").write_text(
                json.dumps(
                    {"msg_id": msg_id, "targets": [{"thread_key": key, "title": key}]}
                ),
                encoding="utf-8",
            )
        return unread

    def test_unopened_replies_are_rows_a_person_can_open(self) -> None:
        """A count told the operator there was something to read, then hid it."""
        self.mark_unread("claude:a", "codex:b")
        code, text = run_pty(self.fixture, b"a\n\n\n")
        self.assertEqual(2, code)
        self.assertIn("Replies", text)
        self.assertIn("r1", text)
        self.assertIn("r2", text)
        # The counter it replaced does not also appear.
        self.assertNotIn("✉ 2 new replies", text)

    def test_one_reply_is_still_a_row(self) -> None:
        self.mark_unread("claude:a")
        code, text = run_pty(self.fixture, b"a\n\n\n")
        self.assertEqual(2, code)
        self.assertIn("Replies", text)
        self.assertIn("r1", text)

    def test_no_unread_replies_means_no_cue(self) -> None:
        code, text = run_pty(self.fixture, b"\n")
        self.assertEqual(2, code)
        self.assertNotIn("✉", text)

    def test_a_message_store_in_any_odd_state_never_breaks_the_picker(self) -> None:
        """Fail open, always: a cue is worth nothing beside a broken picker."""
        messages = self.fixture.state / "messages"
        messages.mkdir(parents=True, exist_ok=True)
        for label, prepare in (
            ("unread is a file", lambda: (messages / "unread").write_text("x")),
            ("unread is unreadable", lambda: unreadable(messages / "unread")),
        ):
            with self.subTest(label=label):
                target = messages / "unread"
                if target.is_dir():
                    target.chmod(0o700)
                    for child in target.iterdir():
                        child.unlink()
                    target.rmdir()
                elif target.exists():
                    target.unlink()
                prepare()
                code, text = run_pty(self.fixture, b"\n")
                self.assertEqual(2, code)
                self.assertNotIn("✉", text)
                self.assertIn("1 session", text)
        (messages / "unread").chmod(0o700)

    def test_the_send_key_is_on_the_menu_and_in_help(self) -> None:
        code, text = run_pty(self.fixture, b"?\n\n\n")
        self.assertEqual(2, code)
        self.assertIn("Needs a", text)
        self.assertIn("Message centre", text)

    def test_the_send_key_opens_the_message_centre_and_comes_back(self) -> None:
        """One surface, no arguments: `s` hands the window to `sp msg`."""
        code, text = run_pty(self.fixture, b"s\n\n\n")
        self.assertEqual(2, code)
        self.assertEqual(
            [["msg"]], [entry["args"] for entry in self.fixture.sp_entries()]
        )
        # And the picker is drawing again afterwards.
        self.assertIn("1 session", text)


if __name__ == "__main__":
    unittest.main()


class PickerUnreadReplyRowTests(unittest.TestCase):
    """Every unread reply is on the screen the operator already looks at."""

    CLAUDE_UUID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
    CODEX_UUID = "f9e8d7c6-b5a4-4938-a271-6f5e4d3c2b1a"

    def setUp(self) -> None:
        live = row("ready", number=9, provider="codex")
        live["identity"]["uuid"] = self.CODEX_UUID
        live["title"] = "Ledger work"
        live["display_title"] = "Ledger work"
        self.fixture = LoginFixture(inventory(live))

    def tearDown(self) -> None:
        self.fixture.close()

    def _store(self) -> Path:
        store = self.fixture.state / "messages"
        for name in ("unread", "threads", "sends"):
            (store / name).mkdir(parents=True, exist_ok=True)
        return store

    def _reply(self, key: str, *, msg_id: str = "", title: str = "") -> None:
        store = self._store()
        (store / "unread" / key).write_bytes(b"")
        (store / "threads" / f"{key}.jsonl").write_text("{}\n", encoding="utf-8")
        if msg_id:
            import json

            (store / "sends" / f"{msg_id}.json").write_text(
                json.dumps(
                    {
                        "msg_id": msg_id,
                        "targets": [{"thread_key": key, "title": title}],
                    }
                ),
                encoding="utf-8",
            )

    def _eighteen(self) -> list[str]:
        """Eighteen replies with real sends behind them, as a person would have."""
        ids = []
        for index in range(18):
            msg_id = f"{index:08x}"
            self._reply(
                f"codex:{index:08d}-0000-4000-8000-000000000000",
                msg_id=msg_id,
                title=f"Task {index}",
            )
            ids.append(msg_id)
        return ids

    def test_eighteen_replies_all_appear_and_stay_selectable(self) -> None:
        """The observed case: a counter said 18 and showed none of them."""
        self._eighteen()
        code, text = run_pty(self.fixture, b"a\n\n\n")
        self.assertEqual(2, code)
        self.assertIn("Replies", text)
        # All eighteen are on the screen, not summarised into a number.
        for position in range(1, 19):
            self.assertIn(f"r{position}", text)
        self.assertIn("1 session", text)
        self.assertIn("Task 0", text)
        self.assertIn("Task 17", text)

    def test_the_deepest_row_opens_its_own_report(self) -> None:
        """r18 is as real as r1 — every row resolves or is not a row."""
        ids = self._eighteen()
        code, text = run_pty(self.fixture, b"a\nr18\n\n\n\n")
        self.assertEqual(2, code)
        self.assertEqual(
            [["msg", "report", ids[17]]],
            [entry["args"] for entry in self.fixture.sp_entries()],
        )

    def test_every_row_that_looks_selectable_resolves(self) -> None:
        """Selecting each row in turn opens a distinct, correct report."""
        ids = self._eighteen()
        for position in (1, 9, 18):
            self.fixture.sp_log.unlink(missing_ok=True)
            code, _ = run_pty(
                self.fixture, f"a\nr{position}\n\n\n\n".encode()
            )
            self.assertEqual(2, code)
            self.assertEqual(
                [["msg", "report", ids[position - 1]]],
                [entry["args"] for entry in self.fixture.sp_entries()],
                f"r{position} opened the wrong report",
            )

    def test_a_reply_whose_message_is_gone_is_never_a_selectable_row(self) -> None:
        """An orphan marker is shown, and shown as what it is.

        The send was pruned or never recorded. The reply is real, so hiding it
        would lose it; giving it an r-key would be worse — a row that looks
        like every other one and does nothing when a person picks it.
        """
        orphan = "codex:99999999-0000-4000-8000-000000000000"
        self._reply(orphan, title="")
        self._reply(f"codex:{self.CODEX_UUID}", msg_id="4f3a2b1c", title="Ledger work")
        code, text = run_pty(self.fixture, b"a\n\n\n")
        self.assertEqual(2, code)
        self.assertIn("no report — s", text)
        self.assertIn("whose message is gone", text)
        # One real reply, so exactly one key, and it is r1 — the orphan never
        # consumes a number that a person could then type.
        self.assertIn("r1", text)
        self.assertNotIn("r2", text)

    def test_the_orphan_row_takes_no_key_even_when_it_sorts_first(self) -> None:
        """Numbering follows the keys, not the rows."""
        self._reply("codex:00000000-0000-4000-8000-000000000000", title="")
        self._reply(f"codex:{self.CODEX_UUID}", msg_id="4f3a2b1c", title="Ledger work")
        code, text = run_pty(self.fixture, b"a\nr1\n\n\n\n")
        self.assertEqual(2, code)
        self.assertEqual(
            [["msg", "report", "4f3a2b1c"]],
            [entry["args"] for entry in self.fixture.sp_entries()],
        )

    def test_the_oldest_send_of_many_hundred_still_owns_its_reply(self) -> None:
        """Age must not orphan a row: the oldest reply is the one that waited.

        A scan bounded to the newest N sends looks fine until the reply a
        person most needs is the one that has been waiting longest. Two
        hundred and fifty sends, and the one that owns the unread reply is the
        oldest file in the directory.
        """
        import json
        import os
        import time

        store = self._store()
        key = f"codex:{self.CODEX_UUID}"
        self._reply(key, msg_id="00000000", title="The long wait")
        now = time.time()
        for index in range(1, 251):
            msg_id = f"{index:08x}"
            (store / "sends" / f"{msg_id}.json").write_text(
                json.dumps(
                    {
                        "msg_id": msg_id,
                        "targets": [
                            {
                                "thread_key": (
                                    f"codex:{index:08d}-0000-4000-8000-000000000000"
                                ),
                                "title": f"Task {index}",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            os.utime(store / "sends" / f"{msg_id}.json", (now, now))
        owning = store / "sends" / "00000000.json"
        stale = now - 365 * 86400
        os.utime(owning, (stale, stale))
        self.assertEqual(251, len(list((store / "sends").iterdir())))
        code, text = run_pty(self.fixture, b"a\nr1\n\n\n\n")
        self.assertEqual(2, code)
        self.assertNotIn("no report — s", text)
        self.assertEqual(
            [["msg", "report", "00000000"]],
            [entry["args"] for entry in self.fixture.sp_entries()],
        )

    def test_selecting_an_orphan_row_says_so_and_opens_nothing(self) -> None:
        """The orphan contract, pinned from the keyboard.

        An orphan takes no key, so there is no key that reaches it — and the
        refusal says which key was not there rather than failing silently.
        That is the whole promise: a row with a key opens something, and a row
        without one is visibly without one.
        """
        orphan = "codex:99999999-0000-4000-8000-000000000000"
        self._reply(orphan, title="")
        code, text = run_pty(self.fixture, b"a\nr1\n\n\n\n")
        self.assertEqual(2, code)
        # Rendered, visibly non-actionable, and routed to the message centre.
        self.assertIn("no report — s", text)
        self.assertIn("whose message is gone", text)
        # Not selectable, and it says so instead of doing nothing.
        self.assertIn("r1 is not a reply shown here", text)
        self.assertEqual([], self.fixture.sp_entries())

    def test_a_send_whose_body_disagrees_with_its_name_owns_nothing(self) -> None:
        """A record that contradicts its own filename is malformed.

        `sp msg report` looks for the file named by the id. A body claiming a
        different id means one of the two is wrong and nothing here can say
        which, so the thread keeps no owner rather than minting a key that
        opens a report that may not exist.
        """
        import json

        store = self._store()
        key = f"codex:{self.CODEX_UUID}"
        (store / "unread" / key).write_bytes(b"")
        (store / "threads" / f"{key}.jsonl").write_text("{}\n", encoding="utf-8")
        (store / "sends" / "aaaaaaaa.json").write_text(
            json.dumps(
                {"msg_id": "bbbbbbbb", "targets": [{"thread_key": key, "title": "Odd"}]}
            ),
            encoding="utf-8",
        )
        code, text = run_pty(self.fixture, b"a\nr1\n\n\n")
        self.assertEqual(2, code)
        self.assertIn("no report — s", text)
        self.assertIn("r1 is not a reply shown here", text)
        entries = [entry["args"] for entry in self.fixture.sp_entries()]
        self.assertEqual([], entries)
        for identifier in ("aaaaaaaa", "bbbbbbbb"):
            self.assertNotIn(
                ["msg", "report", identifier],
                entries,
                f"a mismatched record opened {identifier}",
            )

    def test_a_reply_from_a_live_session_shows_its_number_and_title(self) -> None:
        self._reply(f"codex:{self.CODEX_UUID}", msg_id="4f3a2b1c", title="Ledger work")
        code, text = run_pty(self.fixture, b"a\n\n\n")
        self.assertEqual(2, code)
        self.assertIn("Ledger work", text)
        self.assertIn("#9", text)
        self.assertIn("codex", text)

    def test_a_reply_outlives_the_session_that_sent_it(self) -> None:
        """Closed, outside, or open elsewhere: the reply is still readable."""
        self._reply(
            f"claude:{self.CLAUDE_UUID}", msg_id="7a1b2c3d", title="Fleet rebuild"
        )
        code, text = run_pty(self.fixture, b"a\n\n\n")
        self.assertEqual(2, code)
        self.assertIn("Replies", text)
        # No row for it in the session list, so it is named by what the send
        # recorded and marked as not open here.
        self.assertIn("Fleet rebuild", text)
        self.assertIn("not open here", text)

    def test_a_row_opens_the_report_its_reply_belongs_to(self) -> None:
        self._reply(f"codex:{self.CODEX_UUID}", msg_id="4f3a2b1c", title="Ledger work")
        code, text = run_pty(self.fixture, b"a\nr1\n\n\n\n")
        self.assertEqual(2, code)
        self.assertEqual(
            [["msg", "report", "4f3a2b1c"]],
            [entry["args"] for entry in self.fixture.sp_entries()],
        )

    def test_a_row_nobody_is_showing_changes_nothing(self) -> None:
        self._reply(f"codex:{self.CODEX_UUID}", msg_id="4f3a2b1c")
        code, text = run_pty(self.fixture, b"r7\n\n\n")
        self.assertEqual(2, code)
        self.assertIn("r7 is not a reply shown here", text)
        self.assertEqual([], self.fixture.sp_entries())

    def test_the_section_names_no_identifier(self) -> None:
        """The zero-display rule covers this section like every other."""
        self._reply(f"codex:{self.CODEX_UUID}", msg_id="4f3a2b1c", title="Ledger work")
        self._reply(f"claude:{self.CLAUDE_UUID}", msg_id="7a1b2c3d", title="Fleet")
        code, text = run_pty(self.fixture, b"a\n\n\n")
        self.assertEqual(2, code)
        for identifier in (self.CODEX_UUID, self.CLAUDE_UUID):
            for start in range(0, len(identifier) - 8 + 1):
                window = identifier[start : start + 8]
                if window.isdigit() or "-" in window:
                    continue
                self.assertNotIn(window, text, f"{identifier} leaked via {window!r}")

    def test_more_replies_than_the_screen_holds_say_so(self) -> None:
        """A bounded section that admits its bound, not a silent truncation."""
        for index in range(25):
            self._reply(
                f"codex:{index:08d}-0000-4000-8000-000000000000",
                msg_id=f"{index:08x}",
                title=f"Task {index}",
            )
        code, text = run_pty(self.fixture, b"a\n\n\n")
        self.assertEqual(2, code)
        self.assertIn("r20", text)
        self.assertIn("5 more in the message centre", text)
        self.assertIn("1 session", text)
