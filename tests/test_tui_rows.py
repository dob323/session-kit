"""What the list contains and in what order."""

from __future__ import annotations

import unittest

from tests.tui_support import Clock, inventory, picker, row

from sessionkit_tui import rows as rowmod
from sessionkit_tui.model import format_age


class MarkingGrammarTests(unittest.TestCase):
    def test_digits_commas_and_ranges_mark_the_numbers_they_name(self) -> None:
        self.assertEqual(((1, 4, 7, 8, 9), ()), rowmod.parse_marks("1,4,7-9"))
        self.assertEqual(((5,), ()), rowmod.parse_marks("5"))
        self.assertEqual(((3, 2), ()), rowmod.parse_marks("3-2"))
        self.assertEqual(((2,), ()), rowmod.parse_marks("2,2"))

    def test_a_fragment_that_names_nothing_is_handed_back(self) -> None:
        self.assertEqual(((1,), ("x",)), rowmod.parse_marks("1,x"))

    def test_spaces_between_numbers_change_nothing(self) -> None:
        self.assertEqual(((1, 2), ()), rowmod.parse_marks("1, 2"))

    def test_one_letter_turns_the_whole_input_into_a_filter(self) -> None:
        self.assertFalse(rowmod.is_filter("1,4"))
        self.assertTrue(rowmod.is_filter("1a"))
        self.assertTrue(rowmod.is_filter("cache"))

    def test_a_marked_row_carries_the_mark(self) -> None:
        drawn = rowmod.build_rows(
            inventory(row("Alpha", number=1), row("Beta", number=2)).sessions,
            marks=(2,),
        )
        marked = {item.label: item.marked for item in drawn if item.kind == rowmod.SESSION}
        self.assertEqual({"Alpha": False, "Beta": True}, marked)


class FilterTests(unittest.TestCase):
    def test_the_filter_finds_names_out_of_order(self) -> None:
        self.assertTrue(rowmod.matches("orc", "orphaned record"))
        self.assertFalse(rowmod.matches("zz", "orphaned record"))

    def test_the_filter_reaches_providers_accounts_and_projects(self) -> None:
        sessions = inventory(
            row("Alpha", number=1, provider="claude", account_alias="primary"),
            row("Beta", number=2, provider="codex", account_alias="spare"),
        ).sessions
        by_provider = rowmod.build_rows(sessions, query="codex")
        self.assertEqual(
            ["Beta"], [item.label for item in by_provider if item.kind == rowmod.SESSION]
        )
        by_account = rowmod.build_rows(sessions, query="primary")
        self.assertEqual(
            ["Alpha"], [item.label for item in by_account if item.kind == rowmod.SESSION]
        )
        by_project = rowmod.build_rows(sessions, query="project")
        self.assertEqual(
            2, len([item for item in by_project if item.kind == rowmod.SESSION])
        )

    def test_the_filter_never_reaches_an_identifier(self) -> None:
        record = row("Alpha", number=1)
        sessions = inventory(record).sessions
        drawn = rowmod.build_rows(sessions, query=record["shpool_id"])
        self.assertEqual([], [item for item in drawn if item.kind == rowmod.SESSION])


class OrderTests(unittest.TestCase):
    def test_ready_stays_together_before_open_elsewhere(self) -> None:
        sessions = inventory(
            row("Elsewhere", number=1, availability="attached", agent_status="running"),
            row("Ready", number=2, agent_status="running"),
            row("Waiting", number=3, needs_you=True),
            row(
                "Elsewhere waiting",
                number=4,
                availability="attached",
                needs_you=True,
            ),
        ).sessions
        drawn = rowmod.build_rows(sessions)
        self.assertEqual(
            ["Waiting", "Ready", "Elsewhere waiting", "Elsewhere"],
            [item.label for item in drawn if item.kind == rowmod.SESSION],
        )

    def test_the_list_holds_still_while_the_cursor_is_moving(self) -> None:
        first = inventory(row("Alpha", number=1), row("Beta", number=2)).sessions
        order = [item.key for item in rowmod.order_sessions(first)]
        moved = inventory(
            row("Alpha", number=1), row("Beta", number=2, needs_you=True)
        ).sessions
        held = [item.key for item in rowmod.order_sessions(moved, pinned=order)]
        self.assertEqual(order, held)
        free = [item.title for item in rowmod.order_sessions(moved)]
        self.assertEqual("Beta", free[0])

    def test_a_new_session_lands_after_the_order_being_held(self) -> None:
        first = inventory(row("Alpha", number=1)).sessions
        order = [item.key for item in rowmod.order_sessions(first)]
        later = inventory(row("Alpha", number=1), row("Beta", number=2)).sessions
        held = rowmod.order_sessions(later, pinned=order)
        self.assertEqual(["Alpha", "Beta"], [item.title for item in held])


class MachineRowTests(unittest.TestCase):
    def test_machine_sessions_stay_behind_one_counted_row(self) -> None:
        sessions = inventory(
            row("Mine", number=1),
            row("Drill", number=2, origin="machine"),
            row("Worker", number=3, origin="machine"),
        ).sessions
        drawn = rowmod.build_rows(sessions)
        labels = [item.label for item in drawn]
        self.assertIn("2 machine sessions", labels)
        self.assertNotIn("Drill", labels)
        self.assertNotIn("Worker", labels)

    def test_the_counted_row_expands_in_place(self) -> None:
        sessions = inventory(
            row("Mine", number=1),
            row("Drill", number=2, origin="machine"),
        ).sessions
        drawn = rowmod.build_rows(sessions, machine_expanded=True)
        labels = [item.label for item in drawn]
        self.assertEqual(
            ["Mine", "1 machine session", "Drill"], labels[: labels.index("New session")]
        )

    def test_the_counted_row_says_how_many_of_them_need_you(self) -> None:
        sessions = inventory(
            row("Drill", number=1, origin="machine", needs_you=True),
            row("Worker", number=2, origin="machine"),
        ).sessions
        drawn = rowmod.build_rows(sessions)
        self.assertIn("2 machine sessions · 1 needs you", [item.label for item in drawn])

    def test_a_session_with_no_origin_is_one_a_person_started(self) -> None:
        sessions = inventory(row("Mine", number=1)).sessions
        self.assertFalse(sessions[0].is_machine)
        drawn = rowmod.build_rows(sessions)
        self.assertIn("Mine", [item.label for item in drawn])

    def test_the_counted_row_is_gone_when_no_machine_session_exists(self) -> None:
        drawn = rowmod.build_rows(inventory(row("Mine", number=1)).sessions)
        self.assertEqual(
            [], [item for item in drawn if item.kind == rowmod.MACHINE_COUNT]
        )


class RowContentTests(unittest.TestCase):
    def test_a_row_carries_old_provider_and_age_words_in_column_order(self) -> None:
        sessions = inventory(
            row(
                "Alpha",
                number=1,
                provider="codex",
                account_alias="primary",
                model="gpt-5-codex",
                agent_status="running",
                subagents=2,
                age_seconds=3600,
                quiet_seconds=300,
            )
        ).sessions
        drawn = rowmod.build_rows(sessions, age_text=format_age)[0]
        self.assertEqual(
            "CDX | primary | gpt-5-codex | working | 2 subagents | last active 5m ago",
            drawn.detail,
        )

    def test_availability_is_a_heading_fact_not_a_row_detail(self) -> None:
        sessions = inventory(
            row("Alpha", number=1, availability="attached", agent_status="running"),
            row("Beta", number=2),
        ).sessions
        drawn = rowmod.build_rows(sessions)
        details = {item.label: item.detail for item in drawn if item.kind == rowmod.SESSION}
        self.assertIn("working", details["Alpha"])
        self.assertNotIn("open elsewhere", details["Alpha"])
        self.assertNotIn("open elsewhere", details["Beta"])

    def test_an_absent_model_says_why_rather_than_showing_a_dash(self) -> None:
        sessions = inventory(row("Alpha", number=1)).sessions
        drawn = rowmod.build_rows(sessions)[0]
        # Both unpulled sections say pending — the operator rule: until a
        # section has a valid read, it says pending, never a dash.
        self.assertIn("CLD | pending | pending |", drawn.detail)

    def test_a_shell_row_says_it_has_no_model(self) -> None:
        sessions = inventory(row("Alpha", number=1, provider="shell")).sessions
        drawn = rowmod.build_rows(sessions)[0]
        self.assertIn("SHL | pending | no model |", drawn.detail)

    def test_every_provider_uses_the_old_three_letter_form(self) -> None:
        sessions = inventory(
            row("Claude", number=1, provider="claude"),
            row("Codex", number=2, provider="codex"),
            row("Shell", number=3, provider="shell"),
            row("Unknown", number=4, provider="other"),
        ).sessions
        details = {
            item.label: item.detail
            for item in rowmod.build_rows(sessions)
            if item.kind == rowmod.SESSION
        }
        self.assertTrue(details["Claude"].startswith("CLD |"))
        self.assertTrue(details["Codex"].startswith("CDX |"))
        self.assertTrue(details["Shell"].startswith("SHL |"))
        self.assertTrue(details["Unknown"].startswith("UNK |"))

    def test_a_waiting_row_uses_the_old_under_one_minute_wording(self) -> None:
        sessions = inventory(
            row(
                "Alpha",
                number=1,
                provider="claude",
                needs_you=True,
                quiet_seconds=30,
                age_seconds=3600,
            )
        ).sessions
        detail = rowmod.build_rows(sessions, age_text=format_age)[0].detail
        # The state cell says whose turn it is; the time cell says when the
        # session last did something. They are two facts and two columns.
        self.assertIn("| needs you | last active just now", detail)
        self.assertEqual(1, detail.count("needs you"))

    def test_a_quiet_working_session_reads_as_waiting_on_you(self) -> None:
        sessions = inventory(
            row("Alpha", number=1, agent_status="running", quiet_seconds=3000)
        ).sessions
        self.assertEqual("needs you", sessions[0].state_word(stall=2700))

    def test_both_providers_say_one_word_for_one_situation(self) -> None:
        """A Claude session at its prompt and a Codex session at its prompt.

        The vendors describe that one situation in two vocabularies -- Claude
        raises `idle_prompt` and the collector records `needs your reply`,
        Codex writes `task_complete` and the collector records `idle` -- and
        the screen used to print both words. There is one situation, so there
        is one word.
        """
        sessions = inventory(
            row(
                "Claude at its prompt",
                number=1,
                provider="claude",
                agent_status="needs your reply",
                needs_you=True,
            ),
            row(
                "Codex at its prompt",
                number=2,
                provider="codex",
                agent_status="idle",
                needs_you=False,
            ),
        ).sessions
        self.assertEqual(
            ["needs you", "needs you"],
            [session.state_word() for session in sessions],
        )

    def test_ordinary_rows_close_the_list(self) -> None:
        drawn = rowmod.build_rows(inventory(row("Alpha", number=1)).sessions)
        self.assertEqual(
            ["New session", "Projects", "Closed sessions", "Help"],
            [item.label for item in drawn[-4:]],
        )


class PinnedThroughStateTests(unittest.TestCase):
    def test_the_hold_expires_three_seconds_after_the_cursor_stops(self) -> None:
        clock = Clock()
        screen = picker(
            row("Alpha", number=1), row("Beta", number=2), clock=clock
        )
        held_order = [
            item.label for item in screen.visible_rows() if item.kind == rowmod.SESSION
        ]
        screen.move(1)
        screen.set_inventory(
            inventory(row("Alpha", number=1), row("Beta", number=2, needs_you=True))
        )
        self.assertEqual(
            held_order,
            [item.label for item in screen.visible_rows() if item.kind == rowmod.SESSION],
        )
        clock.advance(3000)
        expected = [
            item.title for item in rowmod.order_sessions(screen.inventory.sessions)
        ]
        self.assertEqual(
            expected,
            [item.label for item in screen.visible_rows() if item.kind == rowmod.SESSION],
        )


if __name__ == "__main__":
    unittest.main()
