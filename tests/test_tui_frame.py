"""What a painted frame says, and what it must never say."""

from __future__ import annotations

import re
import unittest

from tests.tui_support import inventory, picker, row

from sessionkit_tui import rows as rowmod, voice
from sessionkit_tui.frame import Panel, PanelItem, Screen, build_frame
from sessionkit_tui.model import format_age, parse_session


def frame_of(*records, **keywords):
    screen = picker(*records, **keywords)
    return screen, screen.frame(width=100, height=20)


class FooterTests(unittest.TestCase):
    def test_every_frame_surface_has_no_decorated_key_at_probe_widths(self) -> None:
        decorated = re.compile(
            r"(?:<|‹|«)(?:n|number|numbers)(?:>|›|»)|[«»‹›]"
        )
        base = picker(row("Alpha", number=1))
        screens = [base.screen()]
        base.view = "help"
        screens.append(base.screen())
        screens.extend(
            (
                Screen(
                    panel=Panel(
                        "Alpha",
                        (PanelItem("open", "Open"), PanelItem("close", "Close")),
                    )
                ),
                Screen(
                    panel=Panel(
                        "Rename",
                        (),
                        prompt="New name (↵ back) ❯",
                        entry="draft",
                    )
                ),
                Screen(heading="Closed sessions", view_kind="closed"),
            )
        )
        for width in (50, 80, 120):
            for screen in screens:
                with self.subTest(width=width, screen=screen):
                    drawn = build_frame(screen, width=width, height=40).joined()
                    self.assertIsNone(decorated.search(drawn), drawn)

    def test_the_footer_is_the_one_footer(self) -> None:
        _, drawn = frame_of(row("Alpha", number=1))
        self.assertEqual(f"  {voice.FOOTER}", drawn.lines[-1].text)
        # Priority order, the same one the shell picker's footer drops from
        # the end (operator ruling, 2026-08-16): history is the first luxury
        # and `more` is a door, so `more m` outranks `history h #` here too.
        self.assertEqual(
            "↵ open · open # · kill k # · new n · more m · "
            "help ? · history h # · esc quit",
            voice.FOOTER,
        )

    def test_the_footer_keeps_the_old_verb_first_key_grammar(self) -> None:
        _, drawn = frame_of(row("Alpha", number=1))
        footer = drawn.lines[-1]
        self.assertNotIn("↑↓ move", footer.text)
        for phrase in (
            "open #",
            "kill k #",
            "history h #",
            "new n",
            "more m",
            "help ?",
            "esc quit",
        ):
            self.assertIn(phrase, footer.text)
        key_positions = (
            ("#", footer.text.index("open #") + len("open ")),
            ("k #", footer.text.index("kill k #") + len("kill ")),
            ("h #", footer.text.index("history h #") + len("history ")),
            ("n", footer.text.index("new n") + len("new ")),
            ("m", footer.text.index("more m") + len("more ")),
            ("?", footer.text.index("help ?") + len("help ")),
            ("esc", footer.text.index("esc quit")),
        )
        for token, start in key_positions:
            self.assertTrue(
                any(
                    span.style == "key"
                    and span.start <= start
                    and span.start + span.length >= start + len(token)
                    for span in footer.spans
                ),
                (token, footer),
            )

    def test_every_frame_ends_with_the_footer_at_the_same_line(self) -> None:
        screen, first = frame_of(row("Alpha", number=1), row("Beta", number=2))
        second = screen.frame(width=100, height=20)
        self.assertEqual(len(first.lines), len(second.lines))
        self.assertEqual(20, len(first.lines))
        self.assertEqual(first.lines[-1].text, second.lines[-1].text)

    def test_the_screen_carries_no_other_key_glossary(self) -> None:
        _, drawn = frame_of(row("Alpha", number=1))
        text = drawn.joined()
        for jargon in ("[y/N]", "press q", "action 5", "i<n>", "d1"):
            self.assertNotIn(jargon, text)


class IdentifierTests(unittest.TestCase):
    def test_no_frame_ever_prints_a_shpool_id_or_a_uuid(self) -> None:
        records = [
            row("Alpha", number=1),
            row("Beta", number=2, provider="codex"),
            row("Drill", number=3, origin="machine"),
        ]
        screen = picker(*records)
        screen.machine_expanded = True
        drawn = screen.frame(width=120, height=24)
        text = drawn.joined()
        for record in records:
            self.assertNotIn(record["shpool_id"], text)
            uuid = (record.get("identity") or {}).get("uuid")
            if uuid:
                self.assertNotIn(uuid, text)

    def test_the_action_panel_prints_no_identifier_either(self) -> None:
        record = row("Alpha", number=1)
        screen = picker(record)
        screen.type_text("1")
        screen.enter()
        text = screen.frame(width=100, height=20).joined()
        self.assertIn("Close", text)
        self.assertNotIn(record["shpool_id"], text)
        self.assertNotIn(record["identity"]["uuid"], text)


class MachineBucketTests(unittest.TestCase):
    def test_expansion_counts_and_draws_only_activatable_machine_roots(self) -> None:
        orphan = row("Orphan child", number=2, provider="codex")
        orphan["is_subagent"] = True
        machine = row("Machine root", number=3, provider="codex", origin="machine")
        for width in (50, 80, 120):
            with self.subTest(width=width):
                screen = picker(row("Parent", number=1), orphan, machine)
                screen.machine_expanded = True
                drawn = screen.frame(width=width, height=24).joined()
                self.assertIn("1 machine session", drawn)
                self.assertNotIn("2 machine sessions", drawn)
                self.assertIn("Machine root", drawn)
                self.assertNotIn("Orphan child", drawn)

    def test_activation_refuses_a_subagent_row_that_reaches_the_tui(self) -> None:
        orphan = row(
            "Orphan child", number=2, provider="codex", origin="machine"
        )
        orphan["is_subagent"] = True
        session = parse_session(orphan)
        self.assertIsNotNone(session)
        unsafe_row = next(
            item
            for item in rowmod.build_rows((session,), machine_expanded=True)
            if item.kind == rowmod.SESSION
        )
        screen = picker(row("Parent", number=1))

        self.assertIsNone(screen._activate(unsafe_row))
        self.assertEqual(
            "Subagent sessions stay with their parent. Nothing changed.",
            screen.status,
        )


class AttentionLineTests(unittest.TestCase):
    """There is no attention summary line above the footer, at any count.

    Operator ruling, 2026-08-15 -- the same ruling that removed the older
    picker's version. The count still exists as model state and the rows still
    say "needs you"; what is gone is the headline.
    """

    def test_no_attention_summary_line_however_many_need_you(self) -> None:
        screen, drawn = frame_of(
            row("Alpha", number=1, needs_you=True),
            row("Beta", number=2, needs_you=True),
            row("Gamma", number=3, agent_status="working"),
        )
        # joined(), not text(): the line was drawn in the tail, and asserting
        # its ABSENCE on text() passed at the parent commit as well -- a test
        # that proves nothing. joined() was verified to fail at 68fd53e8 and
        # pass here, which is what makes this a differential.
        text = drawn.joined()
        self.assertNotIn("needs you: 2", text)
        self.assertNotIn("needs you: 2 · 2 sessions", text)
        # The rows themselves still carry it, which is the surface a person
        # can actually see. (The count no longer exists as frame state; its
        # rule is tested directly in tests/test_tui_attention.py.)
        self.assertEqual(
            2, sum(1 for item in screen.screen().rows if getattr(item, "needs_you", False))
        )

    def test_no_attention_summary_line_for_a_single_session(self) -> None:
        screen, drawn = frame_of(row("Alpha", number=1, needs_you=True))
        self.assertNotIn("needs you: 1", drawn.joined())
        self.assertEqual(
            1, sum(1 for item in screen.screen().rows if getattr(item, "needs_you", False))
        )

    def test_nothing_is_printed_at_zero_either(self) -> None:
        _, drawn = frame_of(row("Alpha", number=1))
        text = drawn.joined()
        self.assertNotIn("need you", text)
        self.assertNotIn("0 session", text)


class SummaryAndGroupingTests(unittest.TestCase):
    def test_the_header_summarizes_the_same_estate_as_the_old_picker(self) -> None:
        _, drawn = frame_of(
            row("Ready one", number=1),
            row("Ready two", number=2, needs_you=True),
            row("Elsewhere", number=3, availability="attached"),
        )
        self.assertEqual(
            "  3 sessions · 2 ready · 1 open elsewhere",
            drawn.lines[0].text,
        )
        self.assertNotIn("Session Kit", drawn.joined())

    def test_ready_and_open_elsewhere_are_headings_not_row_details(self) -> None:
        _, drawn = frame_of(
            row("Ready one", number=1),
            row("Elsewhere", number=2, availability="attached"),
        )
        texts = list(drawn.text())
        ready_heading = texts.index("  Ready")
        ready_row = next(index for index, text in enumerate(texts) if "Ready one" in text)
        elsewhere_heading = texts.index("  Open elsewhere")
        elsewhere_row = next(index for index, text in enumerate(texts) if "Elsewhere" in text)
        self.assertLess(ready_heading, ready_row)
        self.assertLess(ready_row, elsewhere_heading)
        self.assertLess(elsewhere_heading, elsewhere_row)
        self.assertNotIn("open elsewhere", texts[elsewhere_row].casefold())


class RowPaintTests(unittest.TestCase):
    def test_a_marked_row_carries_a_visible_tick(self) -> None:
        screen = picker(row("Alpha", number=1), row("Beta", number=2))
        screen.type_text("2")
        drawn = screen.frame(width=100, height=20)
        marked = [line.text for line in drawn.lines if "Beta" in line.text][0]
        unmarked = [line.text for line in drawn.lines if "Alpha" in line.text][0]
        self.assertTrue(marked.startswith("✓"))
        self.assertFalse(unmarked.startswith("✓"))

    def test_the_typed_input_is_labelled_for_what_it_does(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.type_text("1")
        self.assertIn("  Mark: 1", screen.frame(width=100, height=20).text())
        screen.escape()
        screen.type_text("al")
        self.assertIn("  Filter: al", screen.frame(width=100, height=20).text())

    def test_the_highlight_is_on_the_first_row_to_start_with(self) -> None:
        screen, drawn = frame_of(row("Alpha", number=1), row("Beta", number=2))
        self.assertEqual(screen.visible_rows()[0].label, screen.current_row().label)
        self.assertEqual(screen.visible_rows()[0].key, drawn.row_lines[drawn.cursor_line])

    def test_a_narrow_terminal_truncates_rather_than_wraps(self) -> None:
        screen = picker(row("A very long session title indeed", number=1))
        drawn = screen.frame(width=30, height=12)
        for line in drawn.lines:
            self.assertLessEqual(len(line.text), 30)

    def test_structural_rows_are_dim_so_sessions_dominate(self) -> None:
        _, drawn = frame_of(row("Alpha", number=1))
        for label in ("New session", "Projects", "Closed sessions", "Help"):
            line = next(line for line in drawn.lines if label in line.text)
            start = line.text.index(label)
            self.assertTrue(
                any(
                    span.style == "dim"
                    and span.start <= start
                    and span.start + span.length >= start + len(label)
                    for span in line.spans
                ),
                (label, line),
            )


class ColumnTests(unittest.TestCase):
    def test_the_title_column_never_pushes_the_detail_off_the_line(self) -> None:
        long_title = "x" * 200
        screen = picker(row(long_title, number=1), row("Short", number=2))
        drawn = screen.frame(width=100, height=16)
        starts = set()
        for line in drawn.lines:
            if "CLD" in line.text:
                starts.add(line.text.index("CLD"))
        self.assertEqual(1, len(starts), drawn.joined())
        long_line = next(line.text for line in drawn.lines if "xxx" in line.text)
        self.assertIn("…", long_line[: long_line.index(" | CLD")])
        for line in drawn.lines:
            self.assertLessEqual(len(line.text), 100)

    def test_every_detail_field_starts_in_one_shared_column(self) -> None:
        screen = picker(
            row(
                "A",
                number=1,
                provider="claude",
                account_alias="main",
                model="opus",
                agent_status="idle",
                age_seconds=300,
            ),
            row(
                "A medium title",
                number=2,
                provider="codex",
                account_alias="secondary",
                model="gpt-5-codex",
                agent_status="running",
                age_seconds=7200,
            ),
            row(
                "A title that is much longer than either of the others",
                number=3,
                provider="shell",
                agent_status="idle",
                age_seconds=90000,
            ),
        )
        drawn = screen.frame(width=140, height=20)
        session_lines = [
            line.text
            for line in drawn.lines
            if any(provider in line.text for provider in ("CLD", "CDX", "SHL"))
        ]
        self.assertEqual(3, len(session_lines), drawn.joined())
        separators = [
            tuple(index for index, char in enumerate(line) if char == "|")
            for line in session_lines
        ]
        self.assertTrue(all(len(points) >= 5 for points in separators), session_lines)
        self.assertEqual(1, len({points[:5] for points in separators}), session_lines)
        for line in session_lines:
            self.assertNotIn(" · ", line)
            self.assertLessEqual(len(line), 140)

    def test_a_long_title_keeps_at_least_a_third_without_stealing_details(self) -> None:
        width = 90
        screen = picker(
            row(
                "x" * 200,
                number=1,
                provider="codex",
                account_alias="primary",
                model="gpt-5-codex",
                agent_status="running",
                age_seconds=7200,
            ),
            row(
                "Short",
                number=2,
                provider="claude",
                account_alias="p",
                model="opus",
                agent_status="idle",
                age_seconds=300,
            ),
        )
        from sessionkit_tui.frame import MIN_LABEL

        drawn = screen.frame(width=width, height=16)
        session_lines = [line.text for line in drawn.lines if " | CLD" in line.text or " | CDX" in line.text]
        self.assertEqual(2, len(session_lines), drawn.joined())
        first_pipes = {line.index("|") for line in session_lines}
        self.assertEqual(1, len(first_pipes), session_lines)
        # The title no longer claims a third of the line whatever the details
        # need. It keeps a floor, and the details -- which is where the state
        # and the one time live -- get the room they ask for.
        title_cells = first_pipes.pop() - 1 - 7
        self.assertGreaterEqual(title_cells, MIN_LABEL)
        self.assertTrue(
            any("…" in line[: line.index("|")] for line in session_lines),
            session_lines,
        )
        for line in session_lines:
            self.assertRegex(line, r"\| (?:CLD|CDX)\s+\|")
            self.assertLessEqual(len(line), width)
            # Every row still carries its identifiable compact time, uncut.
            self.assertTrue(line.rstrip().endswith("| pending"), line)

    def test_the_details_keep_the_room_they_need(self) -> None:
        from sessionkit_tui.frame import label_width
        from sessionkit_tui.rows import build_rows
        from sessionkit_tui.model import format_age

        sessions = inventory(
            row("x" * 200, number=1, account_alias="primary", model="opus-4.6"),
            row("Short", number=2, account_alias="primary", model="opus-4.6"),
        ).sessions
        drawn = build_rows(sessions, age_text=format_age)
        column = label_width(drawn, 120)
        longest_detail = max(len(item.detail) for item in drawn if item.detail)
        self.assertLessEqual(7 + column + 3 + longest_detail, 120)

    def test_a_narrow_terminal_truncates_the_title_not_the_details(self) -> None:
        """The title used to keep a third of the line whatever the details
        needed, so the row was assembled wider than the terminal and the frame
        hard-cut the tail -- the state and the time, which is what the row is
        for. It keeps a floor now and no more."""
        from sessionkit_tui.frame import label_width, detail_widths, MIN_LABEL
        from sessionkit_tui.rows import build_rows
        from sessionkit_tui.model import format_age

        sessions = inventory(
            row("x" * 200, number=1, account_alias="primary", model="opus-4.6")
        ).sessions
        drawn = build_rows(sessions, age_text=format_age)
        # Where there is room for both, the details get theirs and the title
        # takes what is left -- not a third of the line regardless.
        for width in (100, 120, 160):
            with self.subTest(width=width):
                column = label_width(drawn, width)
                self.assertGreaterEqual(column, MIN_LABEL)
                self.assertLessEqual(7 + column + detail_widths(drawn).fixed, width)
        # And where there is not, the title is still only its floor: it never
        # grows into room the state and the time need.
        self.assertEqual(MIN_LABEL, label_width(drawn, 80))


class EmptyStateTests(unittest.TestCase):
    def test_an_empty_list_says_so_once(self) -> None:
        screen = picker()
        drawn = screen.frame(width=80, height=16)
        self.assertIn("New session", drawn.joined())
        screen.type_text("zzzz")
        drawn = screen.frame(width=80, height=16)
        self.assertIn("  Sessions: none.", drawn.text())

    def test_closed_sessions_say_none_when_the_ledger_is_empty(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.view = "closed"
        drawn = screen.frame(width=80, height=16)
        self.assertIn("  Closed sessions: none.", drawn.text())


class PanelPaintTests(unittest.TestCase):
    def test_a_panel_draws_its_rows_and_highlights_the_default(self) -> None:
        panel = Panel(
            "Alpha",
            (PanelItem("open", "Open"), PanelItem("close", "Close")),
            index=1,
        )
        drawn = build_frame(Screen(panel=panel), width=60, height=12)
        self.assertIn("   2  Close", drawn.text())
        self.assertEqual("   2  Close", drawn.lines[drawn.cursor_line].text)

    def test_panel_details_share_one_pipe_column_and_name_the_way_back(self) -> None:
        panel = Panel(
            "Alpha",
            (
                PanelItem("close", "Close", "everything inside will end"),
                PanelItem("account", "Change account", "keeps this conversation"),
            ),
            identity_color="red",
        )
        drawn = build_frame(Screen(panel=panel), width=80, height=12)
        rows = [line.text for line in drawn.lines if " | " in line.text]
        self.assertEqual(1, len({line.index("|") for line in rows}), rows)
        self.assertEqual(
            "  ↵ choose · b back · esc back · ctrl-d quit", drawn.lines[-1].text
        )
        self.assertTrue(any(span.style == "title:red" for span in drawn.lines[0].spans))


class StaleTests(unittest.TestCase):
    def test_a_cached_list_says_it_is_cached(self) -> None:
        screen = picker()
        screen.set_inventory(inventory(row("Alpha", number=1), stale=True))
        drawn = screen.frame(width=100, height=16)
        self.assertIn(
            "  Showing the last confirmed list. Actions wait for a refresh.", drawn.text()
        )


class HelpTests(unittest.TestCase):
    def test_help_mirrors_the_old_picker_key_table(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.view = "help"
        drawn = screen.frame(width=200, height=40)
        text = drawn.joined()
        self.assertEqual("  Picker help", drawn.lines[0].text)
        for section in ("Sessions", "Needs you", "The list", "Quitting"):
            self.assertIn(f"  {section}", text)
        rows = [
            line
            for line in drawn.lines
            if any(line.text.startswith(f"  {key.ljust(13)} ") for _, key, _ in voice.HELP_ROWS)
        ]
        self.assertEqual(len(voice.HELP_ROWS), len(rows))
        self.assertTrue(all(any(span.style == "key" for span in line.spans) for line in rows))
        self.assertTrue(all(line.text[15] == " " for line in rows))
        self.assertIn("  ↵ back · b back · esc back · ctrl-d quit", drawn.text())


class ClosedPagePaintTests(unittest.TestCase):
    def test_closed_rows_mirror_the_old_bracketed_provider_layout(self) -> None:
        from sessionkit_tui.model import ClosedSession

        screen = picker(row("Alpha", number=1))
        screen.view = "closed"
        screen.set_closed((ClosedSession("u", "Old Alpha", "codex", "u", "/srv", "primary", 1),))
        drawn = screen.frame(width=100, height=16)
        line = next(line for line in drawn.lines if "Old Alpha" in line.text)
        self.assertIn("[CDX] Old Alpha [login time unknown]", line.text)
        self.assertTrue(any(span.style == "provider:codex" for span in line.spans))
        self.assertEqual("  ↵ actions · esc back · ctrl-d quit", drawn.lines[-1].text)


class ScrollTests(unittest.TestCase):
    def test_the_window_follows_the_highlight(self) -> None:
        records = [row(f"Session {number}", number=number) for number in range(1, 21)]
        screen = picker(*records)
        for _ in range(15):
            screen.move(1)
        drawn = screen.frame(width=100, height=12)
        self.assertIsNotNone(drawn.cursor_line)
        self.assertIn(screen.current_row().label, drawn.joined())


class AgeTests(unittest.TestCase):
    def test_one_time_phrase_with_one_shape_on_every_row(self) -> None:
        self.assertEqual("last active just now", format_age(5))
        self.assertEqual("last active 3m ago", format_age(200))
        self.assertEqual("last active 1h 0m ago", format_age(3600))
        self.assertEqual("last active 1d 1h ago", format_age(90000))
        self.assertEqual("last active pending", format_age(None))
        # Every answer begins the same way, which is what lets the column line
        # up: `opened 3 hr ago` beside `3 hr ago` never could.
        for seconds in (None, 0, 5, 200, 3600, 90000, 8 * 86400):
            self.assertTrue(format_age(seconds).startswith("last active "))


class RowKindTests(unittest.TestCase):
    def test_every_painted_row_maps_back_to_a_row(self) -> None:
        screen = picker(row("Alpha", number=1))
        drawn = screen.frame(width=100, height=20)
        keys = set(drawn.row_lines.values())
        self.assertEqual(
            {item.key for item in screen.visible_rows()} & keys,
            {item.key for item in screen.visible_rows()},
        )
        self.assertIn(rowmod.HELP, keys)


if __name__ == "__main__":
    unittest.main()
