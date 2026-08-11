"""Phase 3 picker navigation: peek-and-reply, live filtering, jump, grouping,
compact rows, and the single help table.

The picker is the surface a person lives in all day, so every test here asks
the same two questions of a new behaviour: does it show the truth, and can it
change anything by accident. Peek is read-only until a reply is typed, the
jump key marks rather than selects, grouping and compact only reorder or
redraw what the unfiltered list already contained, and a filter previewed
mid-typing is undone the moment the line turns out to be a command.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest

from tests.support import REPO
from tests.test_login import LoginFixture, inventory, row, run_pty

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_events import peek as peek_module  # noqa: E402

LOGIN = REPO / "bin" / "shpool_login"


def thread_key(item: dict) -> str:
    return f"{item['provider']}:{item['identity']['uuid']}"


def write_event(
    fixture: LoginFixture,
    item: dict,
    *,
    event: str = "needs_input",
    question: str | None = None,
    ts_unix_ms: int,
) -> None:
    root = fixture.state / "events"
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / f"{thread_key(item)}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event": event,
                    "question": question,
                    "source": "hook",
                    "ts_unix_ms": ts_unix_ms,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
    path.chmod(0o600)


def write_exchange(fixture: LoginFixture, item: dict, entries: list[dict]) -> None:
    threads = fixture.state / "messages" / "threads"
    threads.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = threads / f"{thread_key(item)}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
    path.chmod(0o600)


def waiting_row(shpool_id: str, *, number: int, provider: str = "codex") -> dict:
    item = row(shpool_id, number=number, provider=provider, needs_you=True)
    item["agent_status"] = "needs your reply"
    return item


def rewrite_inventory(fixture: LoginFixture, document: dict) -> None:
    """Point both fixture snapshots at a document built after construction.

    Project grouping is keyed on real directories, and the fixture only knows
    where those are once it exists.
    """
    payload = json.dumps(document)
    fixture.inventory.write_text(payload, encoding="utf-8")
    fixture.refreshed_inventory.write_text(payload, encoding="utf-8")


def row_numbers(text: str) -> list[str]:
    return re.findall(r"(?m)^\s+(\d+)\s+\S.* \| (?:CLD|CDX|SHL|UNK) \|", text)


class PeekTests(unittest.TestCase):
    def test_peek_shows_the_question_and_the_last_exchange(self) -> None:
        item = waiting_row("parser", number=1)
        fixture = LoginFixture(inventory(item))
        try:
            write_event(
                fixture,
                item,
                question="Should I also update the changelog?",
                ts_unix_ms=1_700_000_000_000,
            )
            write_exchange(
                fixture,
                item,
                [
                    {
                        "ts_unix_ms": 1_699_999_000_000,
                        "dir": "out",
                        "msg_id": "0a0a0a0a",
                        "text": "Refactor the parser please",
                        "via": "test",
                    },
                    {
                        "ts_unix_ms": 1_700_000_000_000,
                        "dir": "in",
                        "msg_id": "0a0a0a0a",
                        "text": "Should I also update the changelog?",
                        "via": "test",
                    },
                ],
            )
            code, output = run_pty(fixture, b"i1\n\n\n")
            self.assertEqual(2, code)
            self.assertIn("It asked", output)
            self.assertIn("Should I also update the changelog?", output)
            self.assertIn("Latest messages", output)
            self.assertIn("Refactor the parser please", output)
            self.assertIn("Type a reply and press Enter", output)
            # Looking is not acting: no session command ran at all.
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_peek_reply_goes_out_through_sp_msg(self) -> None:
        item = waiting_row("parser", number=4)
        fixture = LoginFixture(inventory(item))
        try:
            write_event(
                fixture, item, question="Which branch?", ts_unix_ms=1_700_000_000_000
            )
            code, output = run_pty(fixture, b"i4\nuse main\n\n\n")
            self.assertEqual(2, code)
            self.assertIn("Sent to session 4.", output)
            self.assertEqual(
                [["msg", "4", "use main"]],
                [entry["args"] for entry in fixture.sp_entries()],
            )
        finally:
            fixture.close()

    def test_peek_open_hands_the_row_to_the_ordinary_open_path(self) -> None:
        item = waiting_row("parser", number=2)
        fixture = LoginFixture(inventory(item))
        try:
            code, _ = run_pty(fixture, b"i2\no\n\n\n")
            self.assertEqual(2, code)
            commands = [entry["args"][0] for entry in fixture.sp_entries()]
            self.assertIn("picker-open", commands)
        finally:
            fixture.close()

    def test_peek_refuses_a_number_that_is_not_on_the_page(self) -> None:
        fixture = LoginFixture(inventory(row("only", number=1)))
        try:
            code, output = run_pty(fixture, b"i9\n\n")
            self.assertEqual(2, code)
            self.assertIn("Choose a number shown here", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_bare_i_explains_itself_and_changes_nothing(self) -> None:
        fixture = LoginFixture(inventory(row("only", number=1)))
        try:
            code, output = run_pty(fixture, b"i\n\n")
            self.assertEqual(2, code)
            self.assertIn("Use i followed by a session number", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_a_session_with_nothing_recorded_says_so(self) -> None:
        fixture = LoginFixture(inventory(row("quiet", number=3)))
        try:
            code, output = run_pty(fixture, b"i3\n\n\n")
            self.assertEqual(2, code)
            self.assertIn("No question and no messages are recorded", output)
        finally:
            fixture.close()


class LiveFilterTests(unittest.TestCase):
    def test_a_half_typed_search_previews_its_own_result(self) -> None:
        first = row("alpha", number=1)
        second = row("bravo", number=2)
        fixture = LoginFixture(inventory(first, second))
        try:
            # "Search: alp" can only appear from the preview: nothing has been
            # submitted yet, and the first frame carries no search line.
            code, output = run_pty(
                fixture,
                b"/alp",
                deferred=("Search: alp", b"ha\nq\n"),
            )
            self.assertEqual(2, code)
            preview = output[output.index("Search: alp") :]
            self.assertIn("Codex alpha", preview)
            self.assertNotIn("Codex bravo", preview)
            # The line being typed is still the line being typed.
            self.assertIn("❯ /alp", preview)
            self.assertNotIn("Unknown choice", preview)
        finally:
            fixture.close()

    def test_a_preview_is_undone_when_the_line_turns_out_to_be_a_command(
        self,
    ) -> None:
        first = row("alpha", number=1)
        second = row("bravo", number=2)
        fixture = LoginFixture(inventory(first, second))
        try:
            # Type a search, let it preview, then rub it out and press a key
            # that is not a search. The list that key acts on is the whole
            # list, not the one the half-typed line was showing.
            code, output = run_pty(
                fixture,
                b"/alp",
                deferred=("Search: alp", b"\x7f\x7f\x7f\x7fg\nq\n"),
            )
            self.assertEqual(2, code)
            undone = output[output.rindex("No listed session is waiting on you.") :]
            self.assertIn("Codex bravo", undone)
            self.assertNotIn("Search:", undone)
        finally:
            fixture.close()

    def test_the_preview_can_be_switched_off(self) -> None:
        first = row("alpha", number=1)
        second = row("bravo", number=2)
        fixture = LoginFixture(inventory(first, second))
        try:
            code, output = run_pty(
                fixture,
                b"/alpha\n\n",
                env_updates={"SESSION_KIT_PICKER_FILTER_LIVE": "0"},
            )
            self.assertEqual(2, code)
            # Submitting still searches exactly as it always did.
            submitted = output[output.index("Search: alpha") :]
            self.assertIn("Codex alpha", submitted)
            self.assertNotIn("Codex bravo", submitted)
        finally:
            fixture.close()


class JumpTests(unittest.TestCase):
    def test_jump_marks_each_waiting_session_in_turn_and_wraps(self) -> None:
        fixture = LoginFixture(
            inventory(
                row("calm", number=1),
                waiting_row("asking", number=2),
                waiting_row("blocked", number=3),
            )
        )
        try:
            code, output = run_pty(fixture, b"g\ng\ng\n\n")
            self.assertEqual(2, code)
            announcements = re.findall(r"Session (\d+) wants you", output)
            self.assertEqual(["2", "3", "2"], announcements)
            # The row is marked, never selected: no command ran.
            self.assertEqual([], fixture.sp_entries())
            self.assertIn("▸", output)
        finally:
            fixture.close()

    def test_jump_says_so_when_nothing_is_waiting(self) -> None:
        fixture = LoginFixture(inventory(row("calm", number=1)))
        try:
            code, output = run_pty(fixture, b"g\n\n")
            self.assertEqual(2, code)
            self.assertIn("No listed session is waiting on you.", output)
            self.assertNotIn("▸", output)
        finally:
            fixture.close()


class GroupingTests(unittest.TestCase):
    def test_default_grouping_is_the_list_the_picker_always_drew(self) -> None:
        fixture = LoginFixture(
            inventory(
                row("here", number=1, provider="claude"),
                row("elsewhere", number=2, provider="codex", availability="attached"),
            )
        )
        try:
            code, output = run_pty(fixture, b"\n")
            self.assertEqual(2, code)
            self.assertIn("Ready to open", output)
            self.assertIn("Open elsewhere", output)
            self.assertNotIn("Grouping by", output)
        finally:
            fixture.close()

    def test_grouping_by_provider_heads_each_provider(self) -> None:
        fixture = LoginFixture(
            inventory(
                row("one", number=1, provider="claude"),
                row("two", number=2, provider="codex"),
            )
        )
        try:
            code, output = run_pty(fixture, b"group provider\n\n")
            self.assertEqual(2, code)
            grouped = output[output.index("Grouping by provider.") :]
            self.assertIn("Claude", grouped)
            self.assertIn("Codex", grouped)
            self.assertNotIn("Ready to open", grouped)
            self.assertEqual(["1", "2"], row_numbers(grouped))
        finally:
            fixture.close()

    def test_bare_group_cycles_state_provider_project(self) -> None:
        fixture = LoginFixture(inventory(row("one", number=1)))
        try:
            code, output = run_pty(fixture, b"group\ngroup\ngroup\n\n")
            self.assertEqual(2, code)
            self.assertEqual(
                ["provider", "project", "state"],
                re.findall(r"Grouping by (\w+)\.", output),
            )
        finally:
            fixture.close()

    def test_an_unknown_grouping_changes_nothing(self) -> None:
        fixture = LoginFixture(inventory(row("one", number=1)))
        try:
            code, output = run_pty(fixture, b"group sideways\n\n")
            self.assertEqual(2, code)
            self.assertIn("Grouping is state, provider, or project", output)
            self.assertIn("Ready to open", output)
        finally:
            fixture.close()

    def test_project_grouping_names_the_project_a_directory_belongs_to(self) -> None:
        inside = row("known", number=1, provider="claude")
        outside = row("stray", number=2, provider="codex")
        fixture = LoginFixture(inventory(inside, outside))
        try:
            inside["cwd"] = f"{fixture.primary_project}/lib/deep"
            outside["cwd"] = "/var/tmp/scratchpad"
            rewrite_inventory(fixture, inventory(inside, outside))
            code, output = run_pty(fixture, b"group project\n\n")
            self.assertEqual(2, code)
            grouped = output[output.index("Grouping by project.") :]
            # "main" is the alias the fixture's projects.tsv gives that root,
            # and the row three directories inside it still belongs to it.
            self.assertIn("main", grouped)
            self.assertIn("scratchpad", grouped)
            self.assertEqual(["1", "2"], row_numbers(grouped))
        finally:
            fixture.close()

    def test_a_row_that_names_its_own_project_wins(self) -> None:
        # The seam project identity plugs into: when the inventory row carries
        # the project, nothing is derived from a path.
        item = row("delegated", number=1, provider="claude")
        item["project_name"] = "delegation core"
        fixture = LoginFixture(inventory(item))
        try:
            code, output = run_pty(fixture, b"group project\n\n")
            self.assertEqual(2, code)
            self.assertIn("delegation core", output[output.index("Grouping by") :])
        finally:
            fixture.close()


class CompactTests(unittest.TestCase):
    def test_compact_drops_headings_and_shows_more_sessions(self) -> None:
        sessions = [row(f"task-{number}", number=number) for number in range(1, 31)]
        fixture = LoginFixture(inventory(*sessions))
        try:
            code, output = run_pty(fixture, b"c\n\n", lines=24, columns=100)
            self.assertEqual(2, code)
            before, _, after = output.partition("Compact rows on.")
            self.assertIn("Ready to open", before)
            self.assertNotIn("Ready to open", after)
            self.assertGreater(len(row_numbers(after)), len(row_numbers(before)))
        finally:
            fixture.close()

    def test_compact_can_be_turned_back_off(self) -> None:
        fixture = LoginFixture(inventory(row("one", number=1)))
        try:
            code, output = run_pty(fixture, b"c\nc\n\n")
            self.assertEqual(2, code)
            self.assertIn("Compact rows on.", output)
            after = output[output.index("Compact rows off.") :]
            self.assertIn("Ready to open", after)
        finally:
            fixture.close()

    def test_compact_can_start_on(self) -> None:
        fixture = LoginFixture(inventory(row("one", number=1)))
        try:
            code, output = run_pty(
                fixture,
                b"\n",
                env_updates={"SESSION_KIT_PICKER_COMPACT": "1"},
            )
            self.assertEqual(2, code)
            self.assertNotIn("Ready to open", output)
            self.assertEqual(["1"], row_numbers(output))
        finally:
            fixture.close()


class HelpTests(unittest.TestCase):
    """The help screen and the menu must describe the same picker.

    They did not: `a` (Needs you) and `m` (More) were on the footer of every
    screen and in no help text anywhere. One key table now feeds the help, and
    this test fails if a key is dispatched without being documented.
    """

    #: Patterns the dispatcher matches that are documented under another name.
    DOCUMENTED_ELSEWHERE = {
        '""': "Enter",
        "[0-9]*": "number",
        "*": "unknown input",
        "\\>": "next",
        "\\<": "prev",
    }

    def dispatched_keys(self) -> set[str]:
        source = LOGIN.read_text(encoding="utf-8")
        body = source[source.index('case "$choice" in') :]
        keys: set[str] = set()
        for match in re.finditer(r"(?m)^    ([^\s)][^)]*)\)$", body):
            for alternative in match.group(1).split("|"):
                alternative = alternative.strip()
                if alternative in self.DOCUMENTED_ELSEWHERE or not alternative:
                    continue
                # Only the lower-case spelling of each key is documented; the
                # upper-case and word aliases follow it in the same arm.
                if alternative != alternative.lower():
                    continue
                keys.add(alternative)
        return keys

    def test_every_dispatched_key_is_in_the_help_table(self) -> None:
        fixture = LoginFixture(inventory(row("one", number=1)))
        try:
            code, output = run_pty(fixture, b"?\n\n\n", columns=110)
            self.assertEqual(2, code)
            help_text = output[output.index("Session picker help") :]
            expected = {
                "q": "q / p",
                "p": "q / p",
                "r": "r",
                "m": "m",
                "a": "a",
                "g": "g",
                "c": "c",
                "v": "v",
                "u": "u",
                "o": "o",
                "n": "n",
                "s": "s",
                "i": "i<n>",
                "\\?": "?",
                "next": "next",
                "prev": "prev",
                "group": "group",
                "group\\ *": "group",
                "q\\ prune": "qp",
                "compact": "c",
                "qp": "qp",
                "i[0-9]*": "i<n>",
                "r[0-9]*": "r<n>",
                "q[0-9]*": "q<n>",
                "i\\ *": "i<n>",
                "k\\ *": "k <numbers>",
                "x\\ *": "x <number>",
                "name\\ *": "name <number>",
                "name\\ reset\\ *": "name reset #",
                "fork\\ *": "fork <number>",
                "/*": "/text",
            }
            missing = sorted(self.dispatched_keys() - set(expected))
            self.assertEqual(
                [],
                missing,
                "new picker keys must be added to this map and to the help table",
            )
            for pattern, documented in expected.items():
                if pattern not in self.dispatched_keys():
                    continue
                self.assertIn(
                    documented,
                    help_text,
                    f"{pattern} is dispatched but {documented!r} is not in the help",
                )
        finally:
            fixture.close()

    def test_help_is_reachable_from_more_and_from_needs_you(self) -> None:
        for label, payload in (
            ("more", b"m\n?\n\n\n\n"),
            ("needs you", b"a\n?\n\n\n\n"),
        ):
            with self.subTest(label=label):
                fixture = LoginFixture(inventory(row("one", number=1)))
                try:
                    code, output = run_pty(fixture, payload)
                    self.assertEqual(2, code)
                    self.assertIn("Session picker help", output)
                finally:
                    fixture.close()


class PeekProjectionTests(unittest.TestCase):
    """The peek card itself, without a terminal in the way."""

    peek = peek_module

    def test_a_missing_row_has_no_card(self) -> None:
        view = {"sessions": []}
        self.assertIsNone(self.peek.build_peek(view, None, "/nonexistent", 3))

    def test_control_characters_never_reach_the_card(self) -> None:
        view = {
            "sessions": [
                {
                    "terminal_number": 7,
                    "title": "danger\x1b]0;evil\x07",
                    "provider": "codex",
                    "identity": {"uuid": "00000000-0000-4000-8000-000000000009"},
                    "availability": "ready",
                    "agent_status": "working",
                }
            ]
        }
        card = self.peek.build_peek(view, None, "/nonexistent", 7)
        assert card is not None
        self.assertNotIn("\x1b", card["title"])
        self.assertNotIn("\x07", card["title"])
        for line in self.peek.render_peek(card):
            self.assertNotIn("\x1b", line)

    def test_a_shell_session_offers_no_reply(self) -> None:
        view = {
            "sessions": [
                {
                    "terminal_number": 2,
                    "title": "a plain shell",
                    "provider": "shell",
                    "identity": {"uuid": None},
                    "availability": "ready",
                    "agent_status": "idle",
                }
            ]
        }
        card = self.peek.build_peek(view, None, "/nonexistent", 2)
        assert card is not None
        self.assertFalse(card["can_reply"])
        self.assertEqual("", card["thread_key"])


if __name__ == "__main__":
    unittest.main()
