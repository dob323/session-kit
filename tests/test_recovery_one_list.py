"""One list of what can come back, behind both surfaces that show it.

The operator closed a session and could not get it back. `sp recover` listed
it; the picker's Closed-sessions screen, the screen whose only job is
bringing sessions back, did not list it at all. The two read different
stores: measured live on 2026-08-15 they shared three conversations out of
fifty-one, and thirty-four of the picker's seventy-seven entries were
conversations that were open at that moment, offered for restore.

These tests pin the single projection both surfaces now read. None of them
reads the real state directory: every case is built from literal records.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_inventory.recovery_list import MAX_ROWS  # noqa: E402
import session_inventory as inventory_core  # noqa: E402
from sessionkit_inventory import (  # noqa: E402
    closed_sessions,
    lifecycle,
    printed_selectors,
)
from sessionkit_inventory.printed_selectors import (  # noqa: E402
    printed_selectors_path,
)


PICKER = REPO / "lib" / "sh" / "shpool_login_recovery.sh"
COMMANDS = REPO / "lib" / "sh" / "sp_commands.sh"
RENDER_BLOCK = '  python3 - "$RECOVERY" <<\'PY\'\n'
SELECT_BLOCK = (
    '  selected=$(python3 - "$RECOVERY" "$answer" "$SCRIPT_DIR/../lib" <<\'PY\'\n'
)
PROVIDER_WORDS = {"CLD": "claude", "CDX": "codex", "SH": "shell", "?": "unknown"}


def shell_block(opener: str) -> str:
    """The exact python one of the picker's screens runs.

    Lifted out of the shell file rather than copied, so a test of what the
    picker prints cannot go on passing after the picker stops printing it.
    """
    text = PICKER.read_text(encoding="utf-8")
    start = text.index(opener) + len(opener)
    return text[start : text.index("\nPY\n", start)]


def uuid_for(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


LIVE = uuid_for(1)
CLOSED = uuid_for(2)
LOST = uuid_for(3)
CRASHED = uuid_for(4)
RETIRED = uuid_for(5)
UNREADABLE = uuid_for(6)


class RecoveryProjectionTests(unittest.TestCase):
    def rows(self, **overrides):
        # Imported here, not at module scope: the surface tests below must
        # still run against a commit where this module does not exist yet,
        # so the differential reads as a wrong list rather than a broken file.
        from sessionkit_inventory import recovery_list

        payload = {
            "manifest_sessions": [],
            "closed_rows": [],
            "pending_entries": [],
            "live_sessions": [],
            "aliases": {},
            "automatic_titles": {},
            "numbers": {},
        }
        payload.update(overrides)
        return recovery_list.recovery_rows(**payload)

    def test_a_live_conversation_is_never_offered_as_restorable(self) -> None:
        """It is open. A restore would collide with the session running it.

        The picker's store had no live filter at all, so a third of what it
        offered was work already on screen somewhere else.
        """
        rows = self.rows(
            pending_entries=[
                {
                    "provider": "claude",
                    "uuid": LIVE,
                    "cwd": "/srv/app",
                    "title": "Open right now",
                    "actionable": True,
                    "source_generation_key": "gen-1",
                    "old_shpool_id": "s1",
                    "started_at_unix_ms": 1_000,
                }
            ],
            closed_rows=[
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Count the great lakes",
                    "title_source": "native",
                    "restorable": True,
                    "closed_at_unix_ms": 2_000,
                }
            ],
            live_sessions=[
                {"provider": "claude", "identity": {"uuid": LIVE}},
            ],
        )

        self.assertEqual([CLOSED], [row["uuid"] for row in rows])

    def test_a_conversation_closed_on_purpose_is_offered_back(self) -> None:
        """The one they wanted. The pending store drops these by design."""
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Count the great lakes",
                    "title_source": "native",
                    "restorable": True,
                    "closed_at_unix_ms": 2_000,
                }
            ],
            numbers={f"ai:claude:{CLOSED}": 106},
        )

        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["restorable"])
        self.assertEqual("Count the great lakes", rows[0]["display_name"])
        self.assertEqual(106, rows[0]["number"])

    def test_a_session_keeps_the_number_it_has_everywhere_else(self) -> None:
        """A number is bound to the CONVERSATION and outlives the close.

        Both lists used to number their own rows 1..N, so a session was 106
        on every other screen and 1 here. Worse, `sp restore 3` indexed a
        list rebuilt since it was printed.
        """
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Second",
                    "title_source": "alias",
                    "restorable": True,
                    "closed_at_unix_ms": 2_000,
                },
                {
                    "provider": "codex",
                    "uuid": LOST,
                    "cwd": "/srv/app",
                    "title": "First",
                    "title_source": "alias",
                    "restorable": True,
                    "closed_at_unix_ms": 9_000,
                },
            ],
            numbers={f"ai:claude:{CLOSED}": 106, f"ai:codex:{LOST}": 42},
        )

        # Newest first, and each row carries its own number rather than its
        # position in this particular list.
        self.assertEqual([42, 106], [row["number"] for row in rows])

    def test_a_conversation_nothing_named_reads_unnamed(self) -> None:
        """No placeholders. "the Claude session" is the absence of a name."""
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Claude in v2 at Aug 14 23:44",
                    "title_source": "context",
                    "restorable": True,
                    "closed_at_unix_ms": 2_000,
                },
                {
                    "provider": "codex",
                    "uuid": LOST,
                    "cwd": "/srv/app",
                    "title": "",
                    "restorable": True,
                    "closed_at_unix_ms": 1_000,
                },
            ],
        )

        self.assertEqual(["unnamed", "unnamed"], [r["display_name"] for r in rows])
        self.assertEqual([False, False], [r["named"] for r in rows])

    def test_a_name_something_chose_is_shown(self) -> None:
        """Alias, provider rename, and kit-derived titles are all names."""
        for source in ("alias", "native", "automatic"):
            with self.subTest(source=source):
                rows = self.rows(
                    closed_rows=[
                        {
                            "provider": "claude",
                            "uuid": CLOSED,
                            "cwd": "/srv/app",
                            "title": "Count the great lakes",
                            "title_source": source,
                            "restorable": True,
                            "closed_at_unix_ms": 2_000,
                        }
                    ],
                )
                self.assertEqual("Count the great lakes", rows[0]["display_name"])
                self.assertTrue(rows[0]["named"])

    def test_a_name_store_outranks_a_recorded_label(self) -> None:
        """The stores that hold a chosen name outlive the session."""
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Claude in v2 at Aug 14 23:44",
                    "title_source": "context",
                    "restorable": True,
                    "closed_at_unix_ms": 2_000,
                }
            ],
            aliases={f"claude:{CLOSED}": "Great Lakes Count"},
        )

        self.assertEqual("Great Lakes Count", rows[0]["display_name"])

    def test_a_record_with_no_provenance_is_judged_by_shape(self) -> None:
        """Never invent a name; never throw one away either.

        Manifests and pending records keep no provenance at all, so a title
        with none is measured against the labels the kit itself generates,
        and against nothing else. Every one of these was found on the real
        machine being shown as though somebody had chosen it.
        """
        for title in (
            "the shell session",
            "the Claude session",
            "Idle shell",
            "Claude",
            "v2-35",
            "v2-af",
            "Claude in v2 at Aug 14 23:44",
            "Codex started Aug 14 23:44",
        ):
            with self.subTest(title=title):
                rows = self.rows(
                    closed_rows=[
                        {
                            "provider": "claude",
                            "uuid": CLOSED,
                            "cwd": "/srv/app",
                            "title": title,
                            "restorable": True,
                            "closed_at_unix_ms": 2_000,
                        }
                    ],
                )
                self.assertEqual("unnamed", rows[0]["display_name"])
                self.assertFalse(rows[0]["named"])

        # And a name a person typed, with the same missing provenance, is
        # still their name.
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Orphaned record",
                    "restorable": True,
                    "closed_at_unix_ms": 2_000,
                }
            ],
        )
        self.assertEqual("Orphaned record", rows[0]["display_name"])

    def test_history_only_says_so_in_a_sentence(self) -> None:
        """A shell has no conversation; an unreadable transcript cannot come
        back. Both are listed, neither pretends to be restorable."""
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "shell",
                    "uuid": "",
                    "cwd": "/srv/app",
                    "title": "",
                    "restorable": False,
                    "closed_at_unix_ms": 2_000,
                    "shpool_id": "s20260814-000000-1",
                }
            ],
        )

        self.assertEqual(1, len(rows))
        self.assertFalse(rows[0]["restorable"])
        self.assertIn("no conversation to reopen", rows[0]["history_only_reason"])
        self.assertEqual("unnamed", rows[0]["display_name"])

    def test_a_transcript_that_is_gone_is_history_on_every_store(self) -> None:
        """One readability rule, asked of all three stores.

        The ledger dropped the row outright, so the sentence written for this
        case could never print; the manifest and the pending queue never
        asked at all, so the same conversation was offered as a full restore
        and `sp restore` walked into a missing file.
        """
        stores = {
            "closed_rows": [
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Gone",
                    "title_source": "native",
                    "restorable": True,
                    "closed_at_unix_ms": 2_000,
                }
            ],
            "manifest_sessions": [
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Gone",
                    "title_source": "native",
                    "crashed_at_unix_ms": 2_000,
                }
            ],
            "pending_entries": [
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Gone",
                    "title_source": "native",
                    "actionable": True,
                    "source_generation_key": "gen-1",
                    "old_shpool_id": "s-old",
                    "started_at_unix_ms": 2_000,
                }
            ],
        }
        for store, records in stores.items():
            with self.subTest(store=store):
                rows = self.rows(
                    still_readable=lambda provider, uuid: False, **{store: records}
                )
                self.assertEqual(1, len(rows))
                self.assertFalse(rows[0]["restorable"])
                self.assertIn(
                    "no longer on this machine", rows[0]["history_only_reason"]
                )
                self.assertEqual("Gone", rows[0]["display_name"])

    def test_a_conversation_whose_records_disagree_is_not_called_a_shell(
        self,
    ) -> None:
        """It is a conversation with a transcript, held for them to look at.

        Every non-actionable pending row was told it was a plain shell with
        nothing to reopen, on the list, again when they typed its number, and
        again from `sp restore`.
        """
        rows = self.rows(
            pending_entries=[
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Conflicted work",
                    "title_source": "native",
                    "actionable": False,
                    "conflict_fields": ["cwd", "command"],
                    "source_generation_key": "gen-1",
                    "old_shpool_id": "s-old",
                    "started_at_unix_ms": 2_000,
                }
            ],
        )

        self.assertFalse(rows[0]["restorable"])
        self.assertIn("launch records disagree", rows[0]["history_only_reason"])
        self.assertNotIn("plain shell", rows[0]["history_only_reason"])
        self.assertEqual(["cwd", "command"], rows[0]["conflict_fields"])

    def test_a_closed_shell_with_no_directory_is_still_listed(self) -> None:
        """The ledger knows which session it was; the list has to say so.

        A record with no conversation is kept only by its own identity, and
        the closed loop was not passing the one the ledger holds, so a shell
        closed while the inventory cache was unreadable was on no screen at
        all.
        """
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "shell",
                    "uuid": "",
                    "cwd": "",
                    "title": "Idle shell",
                    "restorable": False,
                    "closed_at_unix_ms": 2_000,
                    "shpool_id": "s20260814-000000-1",
                }
            ],
        )

        self.assertEqual(1, len(rows))
        self.assertEqual("s20260814-000000-1", rows[0]["old_shpool_id"])
        self.assertIn("no conversation to reopen", rows[0]["history_only_reason"])

    def test_two_conversations_never_share_a_selector(self) -> None:
        """Growing a colliding short id once is not the same as growing it.

        These two share their first twelve hex characters, so the one widening
        step left both rows carrying "aaaaaaaaaaaa".
        """
        first = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
        second = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "claude",
                    "uuid": uuid,
                    "cwd": "/srv/app",
                    "title": "Work",
                    "title_source": "alias",
                    "restorable": True,
                    "closed_at_unix_ms": 2_000 + offset,
                }
                for offset, uuid in enumerate((first, second))
            ],
        )

        self.assertEqual(2, len({row["short_id"] for row in rows}))

    def test_one_number_claimed_twice_names_neither_conversation(self) -> None:
        """This read does not go through the validating registry reader.

        A number that stands for two conversations resolves to whichever the
        lookup reaches first, silently. Neither row keeps it.
        """
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "First",
                    "title_source": "alias",
                    "restorable": True,
                    "closed_at_unix_ms": 2_000,
                },
                {
                    "provider": "codex",
                    "uuid": LOST,
                    "cwd": "/srv/app",
                    "title": "Second",
                    "title_source": "alias",
                    "restorable": True,
                    "closed_at_unix_ms": 1_000,
                },
            ],
            numbers={f"ai:claude:{CLOSED}": 7, f"ai:codex:{LOST}": 7},
        )

        self.assertEqual([None, None], [row["number"] for row in rows])

    def test_the_cap_never_drops_a_conversation_that_can_come_back(self) -> None:
        """The reproduction as it was reported: five hundred newer closes and
        one older crash, all of them restorable, at the real cap.

        The aggregate limit is a bound on reading, and a history-only row is
        the only kind of row that exists to be read. Applied to the merged
        list it took the one thing the list is for.
        """
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "claude",
                    "uuid": f"{index:08x}-0000-4000-8000-000000000000",
                    "cwd": "/srv/app",
                    "title": f"Closed {index}",
                    "title_source": "alias",
                    "restorable": True,
                    "closed_at_unix_ms": 1_000_000 + index,
                }
                for index in range(MAX_ROWS)
            ],
            manifest_sessions=[
                {
                    "provider": "codex",
                    "uuid": LOST,
                    "cwd": "/srv/app",
                    "title": "Older crash",
                    "title_source": "alias",
                    "crashed_at_unix_ms": 1,
                }
            ],
        )

        self.assertEqual(MAX_ROWS + 1, len(rows))
        self.assertIn(LOST, [row["uuid"] for row in rows])
        self.assertTrue(all(row["restorable"] for row in rows))

    def test_the_cap_keeps_what_can_still_come_back(self) -> None:
        """The cap is a bound on the read, not a retirement policy.

        Applied to the merged list, it let history-only rows push a still
        restorable conversation off the one list that offers it.
        """
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "shell",
                    "uuid": "",
                    "cwd": "/srv/app",
                    "title": "",
                    "restorable": False,
                    "closed_at_unix_ms": 9_000 + index,
                    "shpool_id": f"s-shell-{index}",
                }
                for index in range(3)
            ]
            + [
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Old but restorable",
                    "title_source": "alias",
                    "restorable": True,
                    "closed_at_unix_ms": 1_000,
                }
            ],
            limit=3,
        )

        self.assertEqual(3, len(rows))
        self.assertIn(CLOSED, [row["uuid"] for row in rows])
        # Display order is untouched: newest first, the kept rows in place.
        self.assertEqual(
            sorted((row["when_unix_ms"] for row in rows), reverse=True),
            [row["when_unix_ms"] for row in rows],
        )

    def test_a_row_that_can_come_back_carries_no_excuse(self) -> None:
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Named",
                    "title_source": "alias",
                    "restorable": True,
                    "closed_at_unix_ms": 2_000,
                }
            ],
        )
        self.assertEqual("", rows[0]["history_only_reason"])

    def test_one_conversation_is_one_row_and_keeps_its_ack_keys(self) -> None:
        """The stores overlap. The newest event describes it; the keys the
        acknowledgment needs exist on one record and must survive."""
        rows = self.rows(
            pending_entries=[
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "",
                    "actionable": True,
                    "source_generation_key": "gen-7",
                    "old_shpool_id": "s-old",
                    "started_at_unix_ms": 1_000,
                }
            ],
            closed_rows=[
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Count the great lakes",
                    "title_source": "native",
                    "restorable": True,
                    "closed_at_unix_ms": 5_000,
                }
            ],
        )

        self.assertEqual(1, len(rows))
        self.assertEqual("Count the great lakes", rows[0]["display_name"])
        self.assertEqual("gen-7", rows[0]["source_generation_key"])
        self.assertEqual("s-old", rows[0]["old_shpool_id"])

    def test_retention_is_restorability_not_age(self) -> None:
        """An old conversation that still restores is still offered.

        An age cutoff would silently drop work that comes back perfectly --
        the same failure as the missing session that prompted all this.
        """
        rows = self.rows(
            closed_rows=[
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "cwd": "/srv/app",
                    "title": "Ancient but restorable",
                    "title_source": "alias",
                    "restorable": True,
                    "closed_at_unix_ms": 1,
                }
            ],
        )

        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["restorable"])


class WhatAScreenPrintedTests(unittest.TestCase):
    """The record a typed word is checked against, and what it will not claim.

    It exists for one question: does this word still mean the row it was
    printed beside? Every answer it gives that is not a plain yes has to be
    distinguishable from one, or the check is worse than not having it.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".printed-", dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def row(self, selector: str, uuid: str, provider: str = "claude") -> dict:
        return {"selector": selector, "provider": provider, "uuid": uuid}

    def verdict(self, selector: str, uuid: str, provider: str = "claude") -> str:
        return printed_selectors.check_printed(
            self.state, selector, provider, uuid
        )["verdict"]

    def test_a_word_answers_for_the_conversation_it_was_printed_beside(self) -> None:
        printed_selectors.remember_printed(
            self.state, [self.row("@19:00", CLOSED), self.row("unnamed", RETIRED)]
        )

        self.assertEqual("agrees", self.verdict("@19:00", CLOSED))
        self.assertEqual("disagrees", self.verdict("@19:00", RETIRED))
        # Case and spacing are folded the way both surfaces fold them.
        self.assertEqual("agrees", self.verdict("  UNNAMED ", RETIRED))
        self.assertEqual("disagrees", self.verdict("unnamed", CLOSED))

    def test_a_word_no_screen_printed_is_not_an_answer_at_all(self) -> None:
        """Unknown is a third answer, never a quiet yes.

        Nothing on this machine has shown them that word, so there is no
        earlier meaning for it to have drifted from -- and saying so is what
        lets the caller decide, instead of being told a check happened.
        """
        printed_selectors.remember_printed(self.state, [self.row("@19:00", CLOSED)])

        self.assertEqual("unknown", self.verdict("@20:00", RETIRED))
        self.assertEqual("unknown", self.verdict("", CLOSED))

    def test_a_conversation_that_leaves_the_list_keeps_its_word(self) -> None:
        """The next screen does not release a word by omitting it.

        A row that stops being restorable loses its selector, so the word it
        was printed under simply is not on the new screen. Forgetting it there
        would hand it to whichever row takes it next -- which is the failure
        this record exists to catch.
        """
        printed_selectors.remember_printed(self.state, [self.row("@19:00", CLOSED)])
        printed_selectors.remember_printed(self.state, [self.row("@20:00", RETIRED)])

        self.assertEqual("agrees", self.verdict("@19:00", CLOSED))
        self.assertEqual("disagrees", self.verdict("@19:00", LOST))
        self.assertEqual("agrees", self.verdict("@20:00", RETIRED))

    def test_the_screen_shown_last_is_the_one_that_counts(self) -> None:
        """Rebinding is what the fallback is FOR, while a name is shared.

        A word that legitimately moves to another row and is PRINTED there has
        been shown to them under its new meaning, so that is the meaning it
        keeps.
        """
        printed_selectors.remember_printed(self.state, [self.row("unnamed", CLOSED)])
        printed_selectors.remember_printed(self.state, [self.row("unnamed", RETIRED)])

        self.assertEqual("agrees", self.verdict("unnamed", RETIRED))
        self.assertEqual("disagrees", self.verdict("unnamed", CLOSED))

    def test_a_row_with_no_word_or_no_conversation_is_not_recorded(self) -> None:
        """Only a word they could type, for a conversation it could open."""
        written = printed_selectors.remember_printed(
            self.state,
            [
                self.row("", CLOSED),
                self.row("a shell", "", provider="shell"),
                self.row("@19:00", "not-a-uuid"),
                self.row("Older work", RETIRED),
                "not a row",
            ],
        )

        self.assertEqual(1, written["remembered"])
        self.assertEqual("agrees", self.verdict("Older work", RETIRED))

    def test_a_record_it_cannot_read_is_no_record_rather_than_a_claim(self) -> None:
        """Damage must not read as "this word means something else".

        A refusal built on an unreadable file would block restores for a
        reason nobody could act on; the honest answer is that this machine
        knows nothing about that word.
        """
        printed_selectors.remember_printed(self.state, [self.row("@19:00", CLOSED)])
        printed_selectors_path(self.state).write_text("{ not json", encoding="utf-8")

        self.assertEqual("unknown", self.verdict("@19:00", CLOSED))
        self.assertEqual({}, printed_selectors.load_printed(self.state))

        # And it recovers by being written again, rather than staying broken.
        printed_selectors.remember_printed(self.state, [self.row("@19:00", RETIRED)])
        self.assertEqual("agrees", self.verdict("@19:00", RETIRED))

    def test_it_is_bounded_and_drops_the_oldest_first(self) -> None:
        """A record that grows without limit is a different fault.

        What it drops it reports as unknown, which is allowed -- the direction
        this whole mechanism fails in.
        """
        limit = printed_selectors.MAX_REMEMBERED
        printed_selectors.remember_printed(
            self.state,
            [self.row(f"@{index}", uuid_for(index)) for index in range(limit + 10)],
            now_unix_ms=1_000,
        )
        printed_selectors.remember_printed(
            self.state, [self.row("newest", CLOSED)], now_unix_ms=2_000
        )

        self.assertEqual(limit, len(printed_selectors.load_printed(self.state)))
        self.assertEqual("agrees", self.verdict("newest", CLOSED))

    def test_the_record_is_owner_only(self) -> None:
        printed_selectors.remember_printed(self.state, [self.row("@19:00", CLOSED)])

        self.assertEqual(
            0o600, printed_selectors_path(self.state).stat().st_mode & 0o777
        )


class BothSurfacesShowOneListTests(unittest.TestCase):
    """The differential: same state in, same list out, on both surfaces."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".recovery-", dir=REPO)
        self.base = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.home = self.base / "home"
        (self.home / ".claude" / "projects" / "-srv-app").mkdir(
            parents=True, mode=0o700
        )
        self.data = self.home / ".local" / "share" / "session-kit"
        self.data.mkdir(parents=True, mode=0o700)

        # A conversation closed on purpose, with a real transcript so it is
        # genuinely restorable, and the number it has everywhere else.
        for uuid in (CLOSED, RETIRED):
            (
                self.home / ".claude" / "projects" / "-srv-app" / f"{uuid}.jsonl"
            ).write_text('{"type":"user"}\n', encoding="utf-8")
        (self.data / "closed-sessions.jsonl").write_text(
            "".join(
                json.dumps(record) + "\n"
                for record in (
                    {
                        "provider": "claude",
                        "uuid": CLOSED,
                        "title": "Count the great lakes",
                        "title_source": "native",
                        "cwd": os.fspath(self.base),
                        "closed_at_unix_ms": 5_000,
                        "origin": "human",
                        "shpool_id": "",
                        "account_alias": "",
                    },
                    # Closed long enough ago that its number finished its
                    # quarantine and went back into circulation. It still
                    # restores, so it is still offered, under its name,
                    # because it no longer has a number to be offered under.
                    {
                        "provider": "claude",
                        "uuid": RETIRED,
                        "title": "Older work",
                        "title_source": "native",
                        "cwd": os.fspath(self.base),
                        "closed_at_unix_ms": 3_000,
                        "origin": "human",
                        "shpool_id": "",
                        "account_alias": "",
                    },
                )
            ),
            encoding="utf-8",
        )
        (self.state / "terminal-numbers.json").write_text(
            json.dumps({"schema_version": 1, "bindings": {f"ai:claude:{CLOSED}": 106}}),
            encoding="utf-8",
        )
        # A live conversation, which neither surface may offer.
        (self.state / "inventory.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sessions": [
                        {"provider": "claude", "identity": {"uuid": LIVE}}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.state / "recovery-pending.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_boot_id": "boot-1",
                    "source_daemon_generation": 1,
                    "sessions": {
                        "s-live": {
                            "provider": "claude",
                            "uuid": LIVE,
                            "cwd": "/srv/app",
                            "title": "Open right now",
                            "started_at_unix_ms": 1_000,
                        }
                    },
                    "outside_agents": {},
                }
            ),
            encoding="utf-8",
        )
        self.config = self.base / "config.json"
        self.config.write_text(
            json.dumps({"schema_version": 1, "aliases": {}}), encoding="utf-8"
        )
        self.config.chmod(0o600)

    def env(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": os.fspath(self.home),
                "SESSION_KIT_CONFIG": os.fspath(self.config),
                "SESSION_KIT_STATE_DIR": os.fspath(self.state),
            }
        )
        environment.pop("CLAUDE_CONFIG_DIR", None)
        return environment

    def core(self, *argv: str) -> str:
        result = subprocess.run(
            [sys.executable, os.fspath(REPO / "lib" / "session_inventory.py"), *argv],
            env=self.env(),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def picker_entries(self) -> list[dict]:
        """The payload behind both screens."""
        return json.loads(self.core("recovery-pending", "list")).get("entries") or []

    def payload_file(self) -> Path:
        path = self.base / "recovery-payload.json"
        path.write_text(self.core("recovery-pending", "list"), encoding="utf-8")
        return path

    def picker_screen(self) -> str:
        """The picker's Closed-sessions screen, drawn by the picker's code."""
        block = self.base / "picker_render.py"
        block.write_text(shell_block(RENDER_BLOCK), encoding="utf-8")
        drawn = subprocess.run(
            [sys.executable, os.fspath(block), os.fspath(self.payload_file())],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, drawn.returncode, drawn.stderr)
        return drawn.stdout

    def cli_screen(
        self, *, verb: str = "show_recovery", core: Path | None = None
    ) -> tuple[int, str]:
        """`sp recover`, run out of the file `sp` runs it out of."""
        inventory_core = core or REPO / "lib" / "session_inventory.py"
        script = f"""
set -u
INVENTORY_CORE={os.fspath(inventory_core)!r}
sk_provider_name() {{
  case "$1" in claude) printf CLD;; codex) printf CDX;; shell) printf SH;; *) printf '?';; esac
}}
sk_die() {{ printf 'session-kit: %s\\n' "$*" >&2; return 1; }}
source {os.fspath(COMMANDS)!r}
restore_exact() {{ printf 'restore-exact %s %s %s\\n' "$1" "$2" "$3" >&2; }}
{verb}
"""
        shown = subprocess.run(
            ["bash", "-c", script],
            env=self.env(),
            text=True,
            capture_output=True,
            check=False,
        )
        return shown.returncode, shown.stdout + shown.stderr

    def test_one_recover_invocation_uses_one_closed_ledger_snapshot(self) -> None:
        """Forgetting between the former two reads cannot silently hide a row."""
        target = RETIRED
        (self.state / "recovery-pending.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_boot_id": "boot-race",
                    "source_daemon_generation": {
                        "pid": 10,
                        "process_start_ticks": 100,
                    },
                    "sessions": {
                        "retained-target": {
                            "provider": "claude",
                            "uuid": target,
                            "cwd": os.fspath(self.base),
                            "title": "Older work",
                            "started_at_unix_ms": 2_000,
                        }
                    },
                    "outside_agents": {},
                }
            ),
            encoding="utf-8",
        )
        lifecycle.record_close_intent(self.state, provider="claude", uuid=target)

        marker = self.base / "forget-landed"
        wrapper = self.base / "race_core.py"
        wrapper.write_text(
            """#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys

real = os.environ["RACE_REAL_CORE"]
completed = subprocess.run(
    [sys.executable, real, *sys.argv[1:]], text=True, capture_output=True
)
sys.stdout.write(completed.stdout)
sys.stderr.write(completed.stderr)
old_first_read = (
    sys.argv[1:3] == ["recovery-pending", "list"]
    and "--without-closed" in sys.argv[1:]
)
one_snapshot_read = (
    sys.argv[1:3] == ["recovery-pending", "list"]
    and "--stream-recovery-snapshot" in sys.argv[1:]
)
marker = Path(os.environ["RACE_MARKER"])
if completed.returncode == 0 and (old_first_read or one_snapshot_read) and not marker.exists():
    marker.touch(mode=0o600)
    subprocess.run(
        [sys.executable, real, "closed-sessions", "forget", "claude", os.environ["RACE_UUID"]],
        check=True,
        stdout=subprocess.DEVNULL,
    )
raise SystemExit(completed.returncode)
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        environment = self.env()
        environment.update(
            {
                "RACE_REAL_CORE": os.fspath(
                    REPO / "lib" / "session_inventory.py"
                ),
                "RACE_MARKER": os.fspath(marker),
                "RACE_UUID": target,
            }
        )
        with mock.patch.dict(os.environ, environment, clear=True):
            code, shown = self.cli_screen(core=wrapper)

        self.assertEqual(0, code, shown)
        self.assertTrue(marker.exists(), "the real forget transaction did not land")
        self.assertIn("Older work", shown)
        remaining = (self.data / "closed-sessions.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(target, remaining)

    def test_one_list_projection_uses_one_closed_ledger_snapshot(self) -> None:
        """The picker payload cannot pair suppression with a later ledger read."""
        target = RETIRED
        (self.state / "recovery-pending.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_boot_id": "boot-race",
                    "source_daemon_generation": {
                        "pid": 10,
                        "process_start_ticks": 100,
                    },
                    "sessions": {
                        "retained-target": {
                            "provider": "claude",
                            "uuid": target,
                            "cwd": os.fspath(self.base),
                            "title": "Older work",
                            "started_at_unix_ms": 2_000,
                        }
                    },
                    "outside_agents": {},
                }
            ),
            encoding="utf-8",
        )
        lifecycle.record_close_intent(self.state, provider="claude", uuid=target)
        environment = self.env()
        real_snapshot = closed_sessions.closed_snapshot
        snapshots = 0

        @contextlib.contextmanager
        def forget_after_first_snapshot(*args, **kwargs):
            nonlocal snapshots
            with real_snapshot(*args, **kwargs) as rows:
                yield rows
            snapshots += 1
            if snapshots == 1:
                forgotten = closed_sessions.forget(
                    provider="claude", uuid=target, environ=environment
                )
                self.assertEqual(1, forgotten)

        config = {"schema_version": 1, "state_dir": self.state, "aliases": {}}
        with mock.patch.dict(
            os.environ, environment, clear=True
        ), mock.patch.object(
            closed_sessions, "closed_snapshot", forget_after_first_snapshot
        ):
            payload = inventory_core.recovery_list_payload(config)

        self.assertEqual(1, snapshots)
        self.assertIn(target, {row["uuid"] for row in payload["entries"]})
        remaining = (self.data / "closed-sessions.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(target, remaining)

    def picker_rows(self, screen: str) -> list[dict]:
        rows: list[dict] = []
        for line in screen.splitlines():
            if not line.strip():
                continue
            if line.startswith("        "):
                rows[-1]["reason"] = line.strip()
                continue
            shape = re.fullmatch(
                r"\s+(\S+)\s\s\[([A-Za-z]+)\] (.+?) \[([^\[\]]+)\]"
                r"(?: \[review required: (.+)\])?",
                line,
            )
            self.assertIsNotNone(shape, f"picker row not parsed: {line!r}")
            assert shape is not None
            rows.append(
                {
                    "token": shape.group(1),
                    "provider": shape.group(2).casefold(),
                    "name": shape.group(3),
                    "age": shape.group(4),
                    "reason": "",
                }
            )
        return rows

    def cli_rows(self, screen: str) -> list[dict]:
        rows: list[dict] = []
        note = ", lost with its session"
        for line in screen.splitlines():
            if not line.strip() or line.startswith("Bring one back"):
                continue
            if line.startswith("        "):
                rows[-1]["reason"] = line.strip()
                continue
            shape = re.fullmatch(r"\s*(\S+)\s+(\S+)\s+(.+) · (.+)", line)
            self.assertIsNotNone(shape, f"`sp recover` row not parsed: {line!r}")
            assert shape is not None
            age = shape.group(4)
            if age.endswith(note):
                age = age[: -len(note)]
            rows.append(
                {
                    "token": shape.group(1),
                    "provider": PROVIDER_WORDS.get(shape.group(2), shape.group(2)),
                    "name": shape.group(3),
                    "age": age,
                    "reason": "",
                }
            )
        return rows

    def test_both_surfaces_show_the_same_screen_for_the_same_state(self) -> None:
        """Ruling 1, as they meet it: two renderers, one state, one list.

        The test this replaces called the same core command twice and asserted
        the results matched, so it could not see the two screens disagree,
        which is what they were doing about every age on the list and about
        how to act on a row with no number.
        """
        entries = self.picker_entries()

        # The right list first: the conversation closed on purpose is offered,
        # the one that is open right now never is.
        self.assertEqual([CLOSED, RETIRED], [row["uuid"] for row in entries])
        self.assertNotIn(LIVE, [row["uuid"] for row in entries])

        picker_screen = self.picker_screen()
        code, cli_screen = self.cli_screen()
        self.assertEqual(0, code, cli_screen)
        picker = self.picker_rows(picker_screen)
        cli = self.cli_rows(cli_screen)

        self.assertEqual(len(entries), len(picker), picker_screen)
        self.assertEqual(
            [(row["token"], row["provider"], row["name"]) for row in picker],
            [(row["token"], row["provider"], row["name"]) for row in cli],
        )
        for shown, said in zip(picker, cli):
            # The picker says which event it is timing; the age itself is the
            # same measurement of the same field.
            self.assertTrue(
                shown["age"].endswith(said["age"]),
                f"{shown['age']!r} is not {said['age']!r}",
            )
            self.assertNotIn("unknown", shown["age"])
            self.assertEqual(shown["reason"], said["reason"])
        # And no screen prints a conversation identifier to get there.
        for screen in (picker_screen, cli_screen):
            for uuid in (CLOSED, RETIRED, LIVE):
                self.assertNotIn(uuid[:8], screen)

    def test_the_picker_offers_the_session_closed_on_purpose(self) -> None:
        """The exact failure: this screen could not offer what they closed."""
        entries = self.picker_entries()

        self.assertEqual(CLOSED, entries[0]["uuid"])
        self.assertEqual("Count the great lakes", entries[0]["display_name"])
        self.assertEqual(106, entries[0]["number"])
        self.assertTrue(entries[0]["restorable"])

    def test_missing_closed_row_is_offered_and_diagnosed_on_the_cli(self) -> None:
        """A tombstone cannot hide retained recovery evidence by itself."""
        ledger = self.data / "closed-sessions.jsonl"
        retained = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("uuid") != CLOSED
        ]
        ledger.write_text(
            "".join(json.dumps(row) + "\n" for row in retained),
            encoding="utf-8",
        )
        lifecycle.record_close_intent(
            self.state, provider="claude", uuid=CLOSED
        )
        (self.state / "recovery-pending.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_boot_id": "boot-1",
                    "source_daemon_generation": {
                        "pid": 10,
                        "process_start_ticks": 100,
                    },
                    "sessions": {
                        "closed-before-replacement": {
                            "provider": "claude",
                            "uuid": CLOSED,
                            "cwd": os.fspath(self.base),
                            "title": "Count the great lakes",
                            "started_at_unix_ms": 5_000,
                        }
                    },
                    "outside_agents": {},
                }
            ),
            encoding="utf-8",
        )

        entries = self.picker_entries()
        self.assertIn(CLOSED, {row["uuid"] for row in entries})
        code, shown = self.cli_screen()
        self.assertEqual(0, code, shown)
        self.assertIn(f"close-intent inconsistency for claude:{CLOSED}", shown)
        self.assertIn("row is missing", shown)
        self.assertIn("Count the great lakes", shown)

    def test_a_conversation_open_outside_the_kit_is_not_offered(self) -> None:
        """Ruling 3 does not care whose terminal it is open in.

        The inventory's outside collection was dropped before the live set was
        built, so an exact conversation running outside the kit was listed as
        restorable. The picker caught it at the last moment; `sp restore` did
        not catch it at all.
        """
        (self.state / "inventory.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sessions": [{"provider": "claude", "identity": {"uuid": LIVE}}],
                    "outside_agents": [
                        {
                            "provider": "claude",
                            "identity": {"uuid": CLOSED, "confidence": "exact"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        entries = self.picker_entries()

        self.assertEqual([RETIRED], [row["uuid"] for row in entries])

    def test_a_row_with_no_number_is_acted_on_by_the_name_it_shows(self) -> None:
        """Ruling 1's "either way in", for the row that has no number.

        The screen printed a dash where the selector belongs while the parser
        quietly accepted a short id it never showed, and `sp recover` printed
        that short id, a conversation identifier, as the way in. Both
        surfaces now offer the same row the same way: by its name.
        """
        entries = self.picker_entries()
        retired = next(row for row in entries if row["uuid"] == RETIRED)
        self.assertIsNone(retired["number"])
        self.assertTrue(retired["restorable"])

        picker = self.picker_rows(self.picker_screen())
        self.assertEqual("—", picker[1]["token"])
        self.assertEqual("Older work", picker[1]["name"])

        chosen = self.select("Older work")
        self.assertEqual(0, chosen.returncode, chosen.stderr)
        self.assertEqual([retired["short_id"]], chosen.stdout.split())

        code, shown = self.cli_screen(verb='restore_listed "older work"')
        self.assertEqual(0, code, shown)
        self.assertIn(f"restore-exact claude {RETIRED} {self.base}", shown)
        self.assertIn("Restored Older work.", shown)

    def select(self, answer: str) -> subprocess.CompletedProcess:
        """The picker's selection parser, run as the picker runs it."""
        return self.select_from(self.payload_file(), answer)

    def select_from(self, payload: Path, answer: str) -> subprocess.CompletedProcess:
        """The same parser, against a screen that was drawn earlier.

        The picker builds its payload once per screen and acts out of that
        file, so this is how it answers a word typed at a screen the state has
        moved on from.
        """
        block = self.base / "picker_select.py"
        block.write_text(shell_block(SELECT_BLOCK), encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                os.fspath(block),
                os.fspath(payload),
                answer,
                os.fspath(REPO / "lib"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_a_selection_it_cannot_read_restores_nothing(self) -> None:
        """A typo is not a command.

        The parser's refusal was caught and then undone: every comma-separated
        fragment was added back, so "2,garbage" restored session 106 and
        "2,,4" restored two sessions.
        """
        for answer in ("106,garbage", "106,,42", "106,", "garbage", "0", "1-"):
            with self.subTest(answer=answer):
                refused = self.select(answer)
                self.assertEqual(2, refused.returncode, refused.stdout)
                self.assertEqual("", refused.stdout.strip())

        # And the selection it can read still works.
        chosen = self.select("106")
        self.assertEqual(0, chosen.returncode, chosen.stderr)
        self.assertEqual(["106"], chosen.stdout.split())

    def shared_name_ledger(self, *, same_second: bool) -> None:
        """Two numberless conversations that nothing named."""
        (self.state / "terminal-numbers.json").write_text(
            json.dumps({"schema_version": 1, "bindings": {}}), encoding="utf-8"
        )
        self.ledger(
            [
                self.closed_record(
                    uuid,
                    "",
                    closed_at_unix_ms=5_000 if same_second else 5_000 - index * 1_000,
                )
                for index, uuid in enumerate((CLOSED, RETIRED))
            ]
        )

    def test_two_rows_with_one_name_each_get_their_own_selector(self) -> None:
        """Everything nobody named reads "unnamed", so the name cannot be it.

        Typing that one word restored the first row on one surface and was
        refused on the other. Each of them now goes by the time of its own
        event, which is fixed for as long as the row exists, is printed where
        its number would be, and is the same string on both screens.
        """
        self.shared_name_ledger(same_second=False)

        picker = self.picker_rows(self.picker_screen())
        code, shown = self.cli_screen()
        self.assertEqual(0, code, shown)
        cli = self.cli_rows(shown)

        tokens = [row["token"] for row in picker]
        self.assertEqual(tokens, [row["token"] for row in cli])
        self.assertEqual(2, len(set(tokens)), tokens)
        for token in tokens:
            self.assertRegex(token, r"^@\d{2}:\d{2}(:\d{2})?$")
        self.assertEqual(["unnamed", "unnamed"], [row["name"] for row in picker])

        # Each token brings back its own row, on both surfaces.
        entries = self.picker_entries()
        for entry, token in zip(entries, tokens):
            chosen = self.select(token)
            self.assertEqual(0, chosen.returncode, chosen.stderr)
            self.assertEqual([entry["short_id"]], chosen.stdout.split())
            code, restored = self.cli_screen(verb=f'restore_listed "{token}"')
            self.assertEqual(0, code, restored)
            self.assertIn(f"restore-exact claude {entry['uuid']}", restored)

        # And the word they share is nobody's selector now.
        refused = self.select("unnamed")
        self.assertEqual(2, refused.returncode, refused.stdout)
        code, refused_cli = self.cli_screen(verb='restore_listed "unnamed"')
        self.assertEqual(2, code, refused_cli)
        self.assertNotIn("restore-exact", refused_cli)

    def test_one_word_means_the_same_thing_on_both_surfaces(self) -> None:
        """A closed shell is called "unnamed" too, and it is the ordinary case.

        The CLI counted the shell as a second answer and refused; the picker
        never had a way to select it and restored the conversation. One typed
        word, two surfaces, two outcomes -- on the mechanism that replaced the
        missing selector.
        """
        (self.state / "terminal-numbers.json").write_text(
            json.dumps({"schema_version": 1, "bindings": {}}), encoding="utf-8"
        )
        self.ledger(
            [
                self.closed_record(CLOSED, "", closed_at_unix_ms=5_000),
                {
                    "provider": "shell",
                    "uuid": "",
                    "title": "Idle shell",
                    "cwd": os.fspath(self.base),
                    "closed_at_unix_ms": 4_000,
                    "origin": "human",
                    "shpool_id": "s20260814-000000-1",
                    "account_alias": "",
                },
            ]
        )

        picker = self.picker_rows(self.picker_screen())
        self.assertEqual(["unnamed", "unnamed"], [row["name"] for row in picker])

        chosen = self.select("unnamed")
        self.assertEqual(0, chosen.returncode, chosen.stderr)
        entries = self.picker_entries()
        conversation = next(row for row in entries if row["uuid"] == CLOSED)
        self.assertEqual([conversation["short_id"]], chosen.stdout.split())

        code, restored = self.cli_screen(verb='restore_listed "unnamed"')
        self.assertEqual(0, code, restored)
        self.assertIn(f"restore-exact claude {CLOSED}", restored)

    def test_a_selector_that_names_two_rows_is_refused(self) -> None:
        """Same name, same second: nothing left to tell them apart.

        Both surfaces refuse rather than one of them guessing, which is the
        standard the module already held short ids to.
        """
        self.shared_name_ledger(same_second=True)

        refused = self.select("unnamed")
        self.assertEqual(2, refused.returncode, refused.stdout)
        self.assertEqual("", refused.stdout.strip())

        code, shown = self.cli_screen(verb='restore_listed "unnamed"')
        self.assertEqual(2, code, shown)
        self.assertNotIn("restore-exact", shown)

    def ledger(self, records: list[dict]) -> None:
        (self.data / "closed-sessions.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def closed_record(self, uuid: str, title: str, **overrides) -> dict:
        record = {
            "provider": "claude",
            "uuid": uuid,
            "title": title,
            "cwd": os.fspath(self.base),
            "closed_at_unix_ms": 5_000,
            "origin": "human",
            "shpool_id": "",
            "account_alias": "",
        }
        record.update(overrides)
        return record

    def test_every_made_up_name_reads_unnamed_on_both_screens(self) -> None:
        """Ruling 4, on the labels the real machine was actually showing.

        None of these records carries provenance, manifests and pending
        records never have any, so each was shown as though somebody had
        chosen it.
        """
        placeholders = (
            "the shell session",
            "the Claude session",
            "Idle shell",
            "Claude",
            "v2-35",
            "v2-af",
            "Claude in v2 at Aug 14 23:44",
        )
        records = []
        for index, title in enumerate(placeholders, 10):
            uuid = uuid_for(index)
            (
                self.home / ".claude" / "projects" / "-srv-app" / f"{uuid}.jsonl"
            ).write_text('{"type":"user"}\n', encoding="utf-8")
            records.append(
                self.closed_record(uuid, title, closed_at_unix_ms=9_000 - index)
            )
        # One name a person typed, to prove the shape test is not a blanket.
        typed = uuid_for(30)
        (
            self.home / ".claude" / "projects" / "-srv-app" / f"{typed}.jsonl"
        ).write_text('{"type":"user"}\n', encoding="utf-8")
        records.append(
            self.closed_record(typed, "Orphaned record", closed_at_unix_ms=8_000)
        )
        self.ledger(records)

        picker = self.picker_rows(self.picker_screen())
        code, shown = self.cli_screen()
        self.assertEqual(0, code, shown)
        cli = self.cli_rows(shown)

        self.assertEqual(len(placeholders) + 1, len(picker))
        self.assertEqual([row["name"] for row in picker], [row["name"] for row in cli])
        self.assertEqual(
            ["unnamed"] * len(placeholders) + ["Orphaned record"],
            [row["name"] for row in picker],
        )

    def test_a_transcript_that_is_gone_says_so_on_both_screens(self) -> None:
        """Ruling 5 for the class that had no way to say it.

        The ledger dropped the row, so this sentence could never print, while
        the crash manifest offered the same conversation as a full restore.
        """
        self.ledger([self.closed_record(UNREADABLE, "Gone with the disk")])
        (self.state / "terminal-numbers.json").write_text(
            json.dumps(
                {"schema_version": 1, "bindings": {f"ai:claude:{UNREADABLE}": 106}}
            ),
            encoding="utf-8",
        )

        picker = self.picker_rows(self.picker_screen())
        code, shown = self.cli_screen()
        self.assertEqual(0, code, shown)
        cli = self.cli_rows(shown)

        self.assertEqual(1, len(picker))
        self.assertEqual("Gone with the disk", picker[0]["name"])
        self.assertIn("no longer on this machine", picker[0]["reason"])
        self.assertEqual(picker[0]["reason"], cli[0]["reason"])

        code, refused = self.cli_screen(verb="restore_listed 106")
        self.assertEqual(1, code, refused)
        self.assertIn("no longer on this machine", refused)
        self.assertNotIn("restore-exact", refused)

    def test_a_conversation_whose_records_disagree_says_so_on_both_screens(
        self,
    ) -> None:
        """The sentence that was unreachable, printing on both surfaces.

        Two launch records for one conversation that disagree about where it
        ran. It is a Claude conversation with a transcript; it was being
        called a plain shell with nothing to reopen.
        """
        self.ledger([])
        (self.home / ".claude" / "projects" / "-srv-app" / f"{LOST}.jsonl").write_text(
            '{"type":"user"}\n', encoding="utf-8"
        )
        (self.state / "recovery-pending.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_boot_id": "boot-1",
                    "source_daemon_generation": 1,
                    "sessions": {
                        "s-one": {
                            "provider": "claude",
                            "uuid": LOST,
                            "cwd": os.fspath(self.base),
                            "title": "Orphaned record",
                            "started_at_unix_ms": 9_000,
                        },
                        "s-two": {
                            "provider": "claude",
                            "uuid": LOST,
                            "cwd": os.fspath(self.base / "other"),
                            "title": "Orphaned record",
                            "started_at_unix_ms": 9_000,
                        },
                    },
                    "outside_agents": {},
                }
            ),
            encoding="utf-8",
        )
        (self.state / "terminal-numbers.json").write_text(
            json.dumps({"schema_version": 1, "bindings": {f"ai:claude:{LOST}": 110}}),
            encoding="utf-8",
        )

        picker = self.picker_rows(self.picker_screen())
        code, shown = self.cli_screen()
        self.assertEqual(0, code, shown)
        cli = self.cli_rows(shown)

        self.assertEqual(1, len(picker))
        self.assertIn("launch records disagree", picker[0]["reason"])
        self.assertNotIn("plain shell", picker[0]["reason"])
        self.assertEqual(picker[0]["reason"], cli[0]["reason"])

        code, refused = self.cli_screen(verb="restore_listed 110")
        self.assertEqual(1, code, refused)
        self.assertIn("launch records disagree", refused)
        self.assertNotIn("plain shell", refused)
        self.assertNotIn("restore-exact", refused)

    def transcript_path(self, uuid: str) -> Path:
        return self.home / ".claude" / "projects" / "-srv-app" / f"{uuid}.jsonl"

    def write_transcript(self, uuid: str) -> Path:
        path = self.transcript_path(uuid)
        path.write_text('{"type":"user"}\n', encoding="utf-8")
        return path

    @staticmethod
    def at(day: int, hour: int, minute: int = 0) -> int:
        """One local wall-clock moment, in the milliseconds a record holds.

        Built through local time on purpose: the selector a shared name falls
        back to is the local clock face of the row's own event, so a fixture
        that wants two events to print the same face has to mean the same
        face on the machine running the test.
        """
        return int(datetime.datetime(2026, 8, day, hour, minute).timestamp() * 1000)

    def selectors(self) -> dict[str, str]:
        return {row["uuid"]: row["selector"] for row in self.picker_entries()}

    @staticmethod
    def handle(row: dict) -> str:
        """What the picker passes on once a word has chosen a row.

        Its number when it has one, and otherwise the conversation's own id --
        which no screen prints and nothing accepts as input.
        """
        number = row["number"]
        return str(number) if isinstance(number, int) else str(row["short_id"])

    def test_a_printed_selector_never_names_a_different_conversation(self) -> None:
        """The word they read means the row they read it beside, or nothing.

        Two conversations named Alpha share the name, so each goes by the
        clock face of its own event: `@19:00` and `@20:00`. They read that
        screen. Before they type, `@19:00`'s transcript goes missing -- so it
        stays listed as history only, with no selector -- and a third Alpha
        closes the NEXT DAY at the same time of day, taking the word `@19:00`
        for itself. `sp restore @19:00` then restored that third conversation
        and said "Restored Alpha.", which is the name of the one they asked for.
        Nothing on the screen could tell them a different conversation opened.
        """
        read_row, kept, newcomer = uuid_for(70), uuid_for(71), uuid_for(72)
        (self.state / "terminal-numbers.json").write_text(
            json.dumps({"schema_version": 1, "bindings": {}}), encoding="utf-8"
        )
        for uuid in (read_row, kept):
            self.write_transcript(uuid)
        self.ledger(
            [
                self.closed_record(read_row, "Alpha", closed_at_unix_ms=self.at(13, 19)),
                self.closed_record(kept, "Alpha", closed_at_unix_ms=self.at(13, 20)),
            ]
        )

        # The screen they read, printed by the code that prints it.
        code, screen = self.cli_screen()
        self.assertEqual(0, code, screen)
        printed = self.selectors()
        token = printed[read_row]
        self.assertEqual("@19:00", token)
        self.assertEqual("@20:00", printed[kept])
        self.assertIn(token, screen)

        # The list changes under them: the row they read can no longer come back,
        # and another Alpha closes a day later at the same time of day.
        self.transcript_path(read_row).unlink()
        self.write_transcript(newcomer)
        self.ledger(
            [
                self.closed_record(read_row, "Alpha", closed_at_unix_ms=self.at(13, 19)),
                self.closed_record(kept, "Alpha", closed_at_unix_ms=self.at(13, 20)),
                self.closed_record(newcomer, "Alpha", closed_at_unix_ms=self.at(14, 19)),
            ]
        )
        rebuilt = self.selectors()
        self.assertEqual("", rebuilt[read_row])
        self.assertEqual(token, rebuilt[newcomer])

        # They type what they read. It named one conversation; it does not name
        # that one now, so it names nothing.
        code, refused = self.cli_screen(verb=f'restore_listed "{token}"')
        self.assertEqual(1, code, refused)
        self.assertNotIn("restore-exact", refused)
        self.assertNotIn("Restored", refused)
        self.assertIn("was printed beside a different conversation", refused)
        self.assertIn("sp recover", refused)

        # And the word that still means its own row still brings it back.
        code, restored = self.cli_screen(verb=f'restore_listed "{rebuilt[kept]}"')
        self.assertEqual(0, code, restored)
        self.assertIn(f"restore-exact claude {kept}", restored)

    def test_the_picker_acts_on_the_list_it_printed(self) -> None:
        """The other surface's half of the same promise.

        The picker builds its payload once and both draws and acts out of that
        one file, so a rebuild between the two cannot move a word onto another
        row. This pins that: the same token, against the screen it was printed
        from, still answers for the conversation it was printed beside -- even
        after the state underneath says something else.
        """
        read_row, kept, newcomer = uuid_for(70), uuid_for(71), uuid_for(72)
        (self.state / "terminal-numbers.json").write_text(
            json.dumps({"schema_version": 1, "bindings": {}}), encoding="utf-8"
        )
        for uuid in (read_row, kept):
            self.write_transcript(uuid)
        self.ledger(
            [
                self.closed_record(read_row, "Alpha", closed_at_unix_ms=self.at(13, 19)),
                self.closed_record(kept, "Alpha", closed_at_unix_ms=self.at(13, 20)),
            ]
        )
        drawn = self.base / "drawn-screen.json"
        drawn.write_text(self.core("recovery-pending", "list"), encoding="utf-8")
        printed = {
            row["uuid"]: row
            for row in json.loads(drawn.read_text(encoding="utf-8"))["entries"]
        }

        self.transcript_path(read_row).unlink()
        self.write_transcript(newcomer)
        self.ledger(
            [
                self.closed_record(read_row, "Alpha", closed_at_unix_ms=self.at(13, 19)),
                self.closed_record(kept, "Alpha", closed_at_unix_ms=self.at(13, 20)),
                self.closed_record(newcomer, "Alpha", closed_at_unix_ms=self.at(14, 19)),
            ]
        )
        self.assertEqual("@19:00", self.selectors()[newcomer])

        chosen = self.select_from(drawn, "@19:00")
        self.assertEqual(0, chosen.returncode, chosen.stderr)
        self.assertEqual([self.handle(printed[read_row])], chosen.stdout.split())

    def test_a_name_of_digits_and_spaces_means_one_row_on_both_screens(self) -> None:
        """The headline claim, in the shape it did not consider.

        A conversation a person named `2 4`, sitting beside sessions 2 and 4.
        The picker split the typed word on whitespace before deciding what
        kind of answer it was, so it read a name as a list of numbers and
        restored Beta and Gamma; `sp restore` matched the whole word and
        brought back the conversation named `2 4`. One typed word, two
        screens, two entirely different answers.
        """
        beta, gamma = uuid_for(20), uuid_for(40)
        for uuid in (CLOSED, beta, gamma):
            self.write_transcript(uuid)
        (self.state / "terminal-numbers.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "bindings": {f"ai:claude:{beta}": 2, f"ai:claude:{gamma}": 4},
                }
            ),
            encoding="utf-8",
        )
        self.ledger(
            [
                self.closed_record(CLOSED, "2 4", closed_at_unix_ms=5_000),
                self.closed_record(beta, "Beta", closed_at_unix_ms=4_000),
                self.closed_record(gamma, "Gamma", closed_at_unix_ms=3_000),
            ]
        )

        named = next(row for row in self.picker_entries() if row["uuid"] == CLOSED)
        self.assertEqual("2 4", named["selector"])

        chosen = self.select("2 4")
        self.assertEqual(0, chosen.returncode, chosen.stderr)
        self.assertEqual([named["short_id"]], chosen.stdout.split())

        code, restored = self.cli_screen(verb='restore_listed "2 4"')
        self.assertEqual(0, code, restored)
        self.assertIn(f"restore-exact claude {CLOSED}", restored)

        # The numbers still mean the sessions that own them.
        chosen = self.select("2,4")
        self.assertEqual(0, chosen.returncode, chosen.stderr)
        self.assertEqual(["2", "4"], chosen.stdout.split())

    def test_a_name_that_is_a_number_never_takes_that_number(self) -> None:
        """A person may call a conversation `2`, and session 2 may exist.

        The name group and the number column were settled separately, so both
        rows answered to `2` -- and both surfaces sent it to the numbered one.
        A selector belongs to one row on the whole list or it belongs to none,
        so the named row goes by the time of its own event instead.
        """
        beta = uuid_for(20)
        for uuid in (CLOSED, beta):
            self.write_transcript(uuid)
        (self.state / "terminal-numbers.json").write_text(
            json.dumps({"schema_version": 1, "bindings": {f"ai:claude:{beta}": 2}}),
            encoding="utf-8",
        )
        self.ledger(
            [
                self.closed_record(CLOSED, "2", closed_at_unix_ms=self.at(13, 19)),
                self.closed_record(beta, "Beta", closed_at_unix_ms=self.at(13, 18)),
            ]
        )

        rows = {row["uuid"]: row for row in self.picker_entries()}
        self.assertEqual("2", rows[beta]["selector"])
        self.assertEqual("@19:00", rows[CLOSED]["selector"])

        for token, uuid in (("2", beta), ("@19:00", CLOSED)):
            with self.subTest(token=token):
                chosen = self.select(token)
                self.assertEqual(0, chosen.returncode, chosen.stderr)
                self.assertEqual([self.handle(rows[uuid])], chosen.stdout.split())
                code, restored = self.cli_screen(verb=f'restore_listed "{token}"')
                self.assertEqual(0, code, restored)
                self.assertIn(f"restore-exact claude {uuid}", restored)

    def two_alphas(self) -> None:
        """Two conversations one name, so each goes by its own clock face."""
        (self.state / "terminal-numbers.json").write_text(
            json.dumps({"schema_version": 1, "bindings": {}}), encoding="utf-8"
        )
        for uuid in (uuid_for(70), uuid_for(71)):
            self.write_transcript(uuid)
        self.ledger(
            [
                self.closed_record(
                    uuid_for(70), "Alpha", closed_at_unix_ms=self.at(13, 19)
                ),
                self.closed_record(
                    uuid_for(71), "Alpha", closed_at_unix_ms=self.at(13, 20)
                ),
            ]
        )

    def check(self, token: str, provider: str, uuid: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                os.fspath(REPO / "lib" / "session_inventory.py"),
                "recovery-selectors",
                "check",
                token,
                provider,
                uuid,
            ],
            env=self.env(),
            text=True,
            capture_output=True,
            check=False,
        )

    def test_sp_recover_writes_down_the_screen_it_prints(self) -> None:
        """The record is made from the bytes that were rendered, not a rebuild.

        `sp restore` has no frozen screen to act out of, so this is the only
        thing that can tell it the word it was handed has changed rows.
        """
        self.two_alphas()

        code, screen = self.cli_screen()
        self.assertEqual(0, code, screen)

        agreed = self.check("@19:00", "claude", uuid_for(70))
        self.assertEqual(0, agreed.returncode, agreed.stderr)
        self.assertEqual("agrees", json.loads(agreed.stdout)["verdict"])

        moved = self.check("@19:00", "claude", uuid_for(72))
        self.assertEqual(4, moved.returncode, moved.stdout)
        self.assertEqual("disagrees", json.loads(moved.stdout)["verdict"])

        # A word nobody has been shown is not evidence either way, and saying
        # so is not the same as saying it agrees.
        unseen = self.check("@04:00", "claude", uuid_for(72))
        self.assertEqual(0, unseen.returncode, unseen.stderr)
        self.assertEqual("unknown", json.loads(unseen.stdout)["verdict"])

    def test_the_picker_writes_down_the_screen_it_draws(self) -> None:
        """Both surfaces print the same words, so both record them.

        A word read off the picker and typed at `sp restore` is checked the
        same way as one read off `sp recover`.
        """
        self.two_alphas()
        drawn = subprocess.run(
            ["bash", "-c", f"""
set -u
SK_STATE_DIR={os.fspath(self.state)!r}
TEMP_FILES=()
new_temp() {{ NEW_TEMP=$(mktemp "$SK_STATE_DIR/$1.json.XXXXXX"); }}
STATUS_CMD={os.fspath(REPO / "bin" / "shpool_status")!r}
source {os.fspath(PICKER)!r}
recovery_list || exit 1
printf 'unrecorded=%s\\n' "${{RECOVERY_UNRECORDED:-}}"
"""],
            env=self.env(),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, drawn.returncode, drawn.stderr)
        self.assertIn("unrecorded=\n", drawn.stdout)

        agreed = self.check("@20:00", "claude", uuid_for(71))
        self.assertEqual(0, agreed.returncode, agreed.stderr)
        self.assertEqual("agrees", json.loads(agreed.stdout)["verdict"])

    def test_a_screen_that_could_not_be_written_down_says_so(self) -> None:
        """The check is worth having only if its absence is visible.

        A screen that quietly failed to record itself would leave `sp restore`
        unable to catch a word that has changed rows, while both of them went
        on looking exactly as they do when it can.
        """
        self.two_alphas()
        # Something is in the way of the one file this writes. What it is does
        # not matter; that the screen still prints, and still says the check
        # is missing, does.
        printed_selectors_path(self.state).mkdir()

        code, screen = self.cli_screen()

        self.assertEqual(0, code, screen)
        self.assertIn("Alpha", screen)
        self.assertIn("could not be written down", screen)

    def test_a_provider_that_exited_still_comes_back_under_its_real_name(
        self,
    ) -> None:
        """A close after the provider exited used to be filed as a shell.

        The conversation is still there and still restorable; what exited was
        a process. Recorded as the conversation it is, it comes back named.
        """
        (self.data / "closed-sessions.jsonl").write_text(
            json.dumps(
                {
                    "provider": "claude",
                    "uuid": CLOSED,
                    "title": "Count the great lakes",
                    "title_source": "native",
                    "cwd": "/srv/app",
                    "closed_at_unix_ms": 7_000,
                    "origin": "human",
                    "shpool_id": "s-gone",
                    "account_alias": "",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        entries = self.picker_entries()

        self.assertEqual(1, len(entries))
        self.assertEqual("claude", entries[0]["provider"])
        self.assertEqual("Count the great lakes", entries[0]["display_name"])
        self.assertTrue(entries[0]["restorable"])
        self.assertEqual("", entries[0]["history_only_reason"])


if __name__ == "__main__":
    unittest.main()
