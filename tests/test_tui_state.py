"""What keys, clicks, and refreshes do to the screen."""

from __future__ import annotations

import unittest

from tests.tui_support import Clock, inventory, panel_to, picker, row, row_to

from sessionkit_tui import rows as rowmod, voice
from sessionkit_tui.model import ClosedSession


def session_labels(screen):
    return [item.label for item in screen.visible_rows() if item.kind == rowmod.SESSION]


class MovementTests(unittest.TestCase):
    def test_the_arrows_move_the_highlight(self) -> None:
        screen = picker(row("Alpha", number=1), row("Beta", number=2))
        ordered = session_labels(screen)
        self.assertEqual(ordered[0], screen.current_row().label)
        screen.move(1)
        self.assertEqual(ordered[1], screen.current_row().label)
        screen.move(-1)
        self.assertEqual(ordered[0], screen.current_row().label)

    def test_the_highlight_stops_at_both_ends(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.move(-5)
        self.assertEqual("Alpha", screen.current_row().label)
        screen.move(500)
        self.assertEqual(voice.HELP, screen.current_row().label)

    def test_the_highlight_follows_the_session_across_a_reordering_refresh(self) -> None:
        clock = Clock()
        screen = picker(
            row("Alpha", number=1, agent_status="running"),
            row("Beta", number=2, agent_status="running"),
            row("Gamma", number=3, agent_status="running"),
            clock=clock,
        )
        while screen.current_row().label != "Gamma":
            screen.move(1)
        self.assertEqual("Gamma", screen.current_row().label)
        clock.advance(5000)
        screen.set_inventory(
            inventory(
                row("Alpha", number=1, agent_status="running"),
                row("Beta", number=2, agent_status="running"),
                row("Gamma", number=3, needs_you=True),
            )
        )
        labels = session_labels(screen)
        self.assertEqual("Gamma", labels[0])
        self.assertEqual({"Alpha", "Beta"}, set(labels[1:]))
        self.assertEqual("Gamma", screen.current_row().label)

    def test_a_session_that_goes_away_leaves_the_highlight_in_place(self) -> None:
        screen = picker(row("Alpha", number=1), row("Beta", number=2))
        screen.move(1)
        screen.set_inventory(inventory(row("Alpha", number=1)))
        self.assertIsNotNone(screen.current_row())
        self.assertGreaterEqual(screen.cursor_index(), 0)


class MarkingTests(unittest.TestCase):
    def test_typed_digits_mark_the_rows_they_name(self) -> None:
        screen = picker(row("Alpha", number=1), row("Beta", number=2), row("Gamma", number=3))
        for character in "1,3":
            screen.type_text(character)
        self.assertEqual((1, 3), screen.marks)
        marked = [item.label for item in screen.visible_rows() if item.marked]
        self.assertEqual({"Alpha", "Gamma"}, set(marked))

    def test_a_range_marks_every_row_it_covers(self) -> None:
        screen = picker(*[row(f"S{n}", number=n) for n in range(1, 5)])
        for character in "2-4":
            screen.type_text(character)
        self.assertEqual((2, 3, 4), screen.marks)

    def test_a_number_that_is_not_on_the_screen_is_refused_in_one_line(self) -> None:
        screen = picker(row("Alpha", number=1))
        for character in "20":
            screen.type_text(character)
        self.assertEqual(
            "There is no session 20 on this screen. Numbers shown here work.",
            screen.status,
        )
        self.assertEqual((), screen.marks)

    def test_enter_on_a_refused_number_changes_nothing(self) -> None:
        screen = picker(row("Alpha", number=1))
        for character in "20":
            screen.type_text(character)
        self.assertIsNone(screen.enter())
        self.assertEqual(voice.no_such_session(20), screen.status)

    def test_a_letter_turns_the_marks_into_a_filter(self) -> None:
        screen = picker(row("Alpha", number=1), row("Beta", number=2))
        screen.type_text("1")
        self.assertEqual((1,), screen.marks)
        screen.type_text("b")
        self.assertEqual((), screen.marks)
        self.assertTrue(screen.is_filter)

    def test_escape_clears_what_was_typed(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.type_text("1")
        screen.escape()
        self.assertEqual("", screen.input_text)
        self.assertEqual("", screen.status)


class LeavingTests(unittest.TestCase):
    """The way out. A screen a person cannot leave is a trap, however good
    the list on it is."""

    def test_escape_with_nothing_typed_leaves(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.escape()
        self.assertTrue(screen.leaving)

    def test_escape_clears_the_input_before_it_leaves(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.type_text("a")
        screen.escape()
        self.assertFalse(screen.leaving)
        self.assertEqual("", screen.input_text)
        screen.escape()
        self.assertTrue(screen.leaving)

    def test_escape_closes_a_panel_before_it_leaves(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.type_text("1")
        screen.enter()
        screen.escape()
        self.assertFalse(screen.leaving)
        self.assertEqual(voice.NOTHING_CHANGED, screen.status)

    def test_escape_returns_from_help_before_it_leaves(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.view = "help"
        screen.escape()
        self.assertFalse(screen.leaving)
        self.assertEqual("list", screen.view)
        screen.escape()
        self.assertTrue(screen.leaving)

    def test_q_leaves_when_nothing_is_typed(self) -> None:
        screen = picker(row("Alpha", number=1))
        self.assertTrue(screen.quit_key())
        self.assertTrue(screen.leaving)

    def test_q_is_an_ordinary_letter_while_filtering(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.type_text("a")
        self.assertFalse(screen.quit_key())
        self.assertFalse(screen.leaving)

    def test_q_is_an_ordinary_letter_while_a_panel_is_open(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.type_text("1")
        screen.enter()
        self.assertFalse(screen.quit_key())

    def test_ctrl_d_leaves_from_anywhere(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.type_text("alpha")
        screen.leave()
        self.assertTrue(screen.leaving)

    def test_enter_never_leaves_because_it_always_means_open(self) -> None:
        # Even an estate with no sessions still lists New session, Projects,
        # Closed sessions, and Help, so there is always something under the
        # highlight for Enter to mean.
        screen = picker()
        self.assertIsNotNone(screen.current_row())
        screen.enter()
        self.assertFalse(screen.leaving)
        screen = picker(row("Alpha", number=1))
        screen.enter()
        self.assertFalse(screen.leaving)

    def test_enter_under_a_filter_that_matches_nothing_stays_put(self) -> None:
        screen = picker(row("Alpha", number=1))
        for character in "zzzz":
            screen.type_text(character)
        self.assertEqual([], list(screen.visible_rows()))
        self.assertIsNone(screen.enter())
        self.assertFalse(screen.leaving)

    def test_a_pasted_close_command_becomes_a_filter_and_runs_nothing(self) -> None:
        screen = picker(row("Alpha", number=1), row("Beta", number=17))
        for character in "k 17":
            screen.type_text(character)
        self.assertTrue(screen.is_filter)
        self.assertEqual((), screen.marks)
        self.assertIsNone(screen.enter())
        self.assertFalse(screen.leaving)


class FilterTests(unittest.TestCase):
    def test_the_filter_narrows_the_list_as_it_is_typed(self) -> None:
        screen = picker(row("Blueprint", number=1), row("Config Sweep", number=2))
        for character in "blu":
            screen.type_text(character)
        self.assertEqual(["Blueprint"], session_labels(screen))

    def test_backspace_widens_it_again(self) -> None:
        screen = picker(row("Blueprint", number=1), row("Config Sweep", number=2))
        for character in "blu":
            screen.type_text(character)
        for _ in range(3):
            screen.backspace()
        self.assertEqual(["Blueprint", "Config Sweep"], session_labels(screen))


class EnterTests(unittest.TestCase):
    def test_enter_on_a_row_opens_that_session(self) -> None:
        screen = picker(row("Alpha", number=1))
        batch = screen.enter()
        self.assertIsNotNone(batch)
        self.assertEqual(("sp", "picker-open", "{proof}"), batch.plans[0].argv)
        self.assertEqual("Opened Alpha.", batch.success)

    def test_enter_on_a_session_open_elsewhere_takes_it_over(self) -> None:
        screen = picker(row("Alpha", number=1, availability="attached"))
        batch = screen.enter()
        self.assertEqual("picker-takeover", batch.plans[0].argv[1])

    def test_enter_with_marks_opens_the_actions_with_close_highlighted(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.type_text("1")
        self.assertIsNone(screen.enter())
        self.assertIsNotNone(screen.panel)
        self.assertEqual("Close", screen.panel.current.label)
        self.assertEqual(
            ["Open", "History", "Close", "Change account", "Change model", "Rename", "Color"],
            [item.label for item in screen.panel.items],
        )

    def test_close_of_several_marked_sessions_reports_once(self) -> None:
        screen = picker(row("Alpha", number=1), row("Beta", number=2))
        for character in "1,2":
            screen.type_text(character)
        screen.enter()
        batch = screen.enter()
        self.assertEqual(2, len(batch.plans))
        self.assertEqual("Closed 2 sessions.", batch.success)
        self.assertTrue(all(plan.argv[1] == "picker-close" for plan in batch.plans))

    def test_closing_one_marked_session_names_it(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.type_text("1")
        screen.enter()
        batch = screen.enter()
        self.assertEqual("Closed Alpha.", batch.success)

    def test_several_marked_sessions_offer_only_what_fits_them_all(self) -> None:
        screen = picker(row("Alpha", number=1), row("Beta", number=2))
        for character in "1,2":
            screen.type_text(character)
        screen.enter()
        self.assertEqual(["Close"], [item.label for item in screen.panel.items])
        self.assertEqual("2 sessions marked", screen.panel.title)

    def test_the_counted_machine_row_opens_in_place(self) -> None:
        screen = picker(
            row("Mine", number=1),
            row("Drill", number=2, origin="machine", agent_status="working"),
        )
        screen.move(1)
        self.assertEqual("1 machine session", screen.current_row().label)
        self.assertIsNone(screen.enter())
        self.assertIn("Drill", session_labels(screen))
        screen.enter()
        self.assertNotIn("Drill", session_labels(screen))

    def test_the_help_row_opens_and_closes_the_help(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.move(500)
        screen.enter()
        self.assertEqual("help", screen.view)
        screen.enter()
        self.assertEqual("list", screen.view)

    def test_the_closed_row_opens_the_closed_list(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.set_closed(
            [
                ClosedSession(
                    key="u1",
                    title="Orphaned Record Audit",
                    provider="claude",
                    uuid="u1",
                    cwd="/srv/project",
                    account_alias="primary",
                    closed_at_unix_ms=1,
                )
            ]
        )
        row_to(screen, rowmod.CLOSED)
        screen.enter()
        self.assertEqual("closed", screen.view)
        self.assertEqual(["Orphaned Record Audit"], [r.label for r in screen.visible_rows()])

    def test_a_closed_conversation_offers_restore_first(self) -> None:
        screen = picker()
        screen.view = "closed"
        screen.set_closed(
            [
                ClosedSession(
                    key="u1",
                    title="Orphaned Record Audit",
                    provider="claude",
                    uuid="u1",
                    cwd="/srv/project",
                    account_alias=None,
                    closed_at_unix_ms=1,
                )
            ]
        )
        screen.enter()
        self.assertEqual("Restore", screen.panel.current.label)
        batch = screen.enter()
        self.assertEqual(
            ("sp", "restore-exact", "claude", "u1", "/srv/project"), batch.plans[0].argv
        )
        self.assertEqual("Restored Orphaned Record Audit.", batch.success)

    def test_a_closed_shell_session_offers_history_and_says_why(self) -> None:
        screen = picker()
        screen.view = "closed"
        screen.set_closed(
            [
                ClosedSession(
                    key="sk-1",
                    title="Build log",
                    provider="shell",
                    uuid="",
                    cwd="/srv/project",
                    account_alias=None,
                    closed_at_unix_ms=1,
                )
            ]
        )
        screen.enter()
        self.assertEqual(["History"], [item.label for item in screen.panel.items])
        self.assertIn("history", screen.panel.items[0].detail)


class PanelTests(unittest.TestCase):
    def _panel(self, **keywords):
        screen = picker(row("Alpha", number=1), **keywords)
        screen.type_text("1")
        screen.enter()
        return screen

    def test_escape_closes_the_panel_and_says_nothing_changed(self) -> None:
        screen = self._panel()
        screen.escape()
        self.assertIsNone(screen.panel)
        self.assertEqual("Nothing changed.", screen.status)

    def test_rename_takes_the_typed_name_without_asking_twice(self) -> None:
        screen = self._panel()
        panel_to(screen, "Rename")
        screen.enter()
        for character in "Blueprint":
            screen.type_text(character)
        batch = screen.enter()
        self.assertEqual(("sp", "picker-name", "{proof}", "Blueprint"), batch.plans[0].argv)
        self.assertEqual("Renamed Blueprint.", batch.success)

    def test_an_empty_rename_changes_nothing(self) -> None:
        screen = self._panel()
        panel_to(screen, "Rename")
        screen.enter()
        self.assertIsNone(screen.enter())
        self.assertEqual("Nothing changed.", screen.status)

    def test_change_account_offers_the_accounts_it_was_given(self) -> None:
        screen = self._panel(accounts=("primary", "spare"))
        panel_to(screen, "Change account")
        screen.enter()
        self.assertEqual(["primary", "spare"], [item.label for item in screen.panel.items])
        batch = screen.enter()
        self.assertEqual(
            ("sp", "picker-account-switch", "{proof}", "primary"), batch.plans[0].argv
        )

    def test_change_model_offers_the_models_it_was_given(self) -> None:
        screen = self._panel(models=("opus", "sonnet"))
        panel_to(screen, "Change model")
        screen.enter()
        batch = screen.enter()
        self.assertEqual(("sp", "change-model", "1", "opus"), batch.plans[0].argv)

    def test_change_model_with_nothing_to_offer_says_so(self) -> None:
        screen = self._panel()
        panel_to(screen, "Change model")
        self.assertIsNone(screen.enter())
        self.assertEqual("Models: none.", screen.status)

    def test_color_offers_the_palette_the_provider_accepts(self) -> None:
        screen = self._panel()
        panel_to(screen, "Color")
        screen.enter()
        self.assertEqual("red", screen.panel.items[0].label)
        batch = screen.enter()
        self.assertEqual(("sp", "color", "1", "red"), batch.plans[0].argv)
        self.assertEqual("Alpha is now red.", batch.success)

    def test_a_codex_session_is_offered_the_codex_palette(self) -> None:
        screen = picker(row("Beta", number=1, provider="codex"))
        screen.type_text("1")
        screen.enter()
        panel_to(screen, "Color")
        screen.enter()
        self.assertEqual("lime", screen.panel.items[0].label)

    def test_history_is_an_action_on_every_row(self) -> None:
        screen = self._panel()
        panel_to(screen, "History")
        batch = screen.enter()
        self.assertEqual(("sp", "picker-history", "{proof}"), batch.plans[0].argv)


class NewSessionTests(unittest.TestCase):
    def test_new_session_asks_provider_then_project_and_nothing_else(self) -> None:
        screen = picker(row("Alpha", number=1), projects=(("main", "/srv/project"),))
        row_to(screen, rowmod.NEW_SESSION)
        screen.enter()
        self.assertEqual(["Claude Code", "Codex", "Shell"], [i.label for i in screen.panel.items])
        screen.enter()
        self.assertEqual(["This directory", "main"], [i.label for i in screen.panel.items])
        screen.move(1)
        batch = screen.enter()
        self.assertEqual(("sp", "new", "claude", "main"), batch.plans[0].argv)

    def test_projects_asks_project_then_provider(self) -> None:
        screen = picker(row("Alpha", number=1), projects=(("main", "/srv/project"),))
        row_to(screen, rowmod.PROJECTS)
        screen.enter()
        self.assertEqual("Project", screen.panel.title)
        screen.move(1)
        screen.enter()
        self.assertEqual("New session", screen.panel.title)
        batch = screen.enter()
        self.assertEqual(("sp", "new", "claude", "main"), batch.plans[0].argv)


class MouseTests(unittest.TestCase):
    def test_a_click_highlights_and_a_second_click_opens(self) -> None:
        screen = picker(row("Alpha", number=1), row("Beta", number=2))
        drawn = screen.frame(width=100, height=20)
        line = next(
            number for number, key in drawn.row_lines.items()
            if key == screen.visible_rows()[1].key
        )
        wanted = screen.visible_rows()[1]
        self.assertIsNone(screen.click(line, 20, drawn))
        self.assertEqual(wanted.label, screen.current_row().label)
        batch = screen.click(line, 20, drawn)
        self.assertEqual(f"Opened {wanted.label}.", batch.success)

    def test_a_click_in_the_mark_column_ticks_the_row(self) -> None:
        screen = picker(row("Alpha", number=1), row("Beta", number=2))
        drawn = screen.frame(width=100, height=20)
        line = next(
            number for number, key in drawn.row_lines.items()
            if key == screen.visible_rows()[1].key
        )
        wanted = screen.visible_rows()[1]
        screen.click(line, 0, drawn)
        self.assertEqual((wanted.number,), screen.marks)
        screen.click(line, 0, drawn)
        self.assertEqual((), screen.marks)

    def test_the_wheel_scrolls_the_list(self) -> None:
        screen = picker(*[row(f"S{n}", number=n) for n in range(1, 30)])
        screen.frame(width=100, height=12)
        screen.wheel(3)
        self.assertEqual(3, screen.top)
        screen.wheel(-3)
        self.assertEqual(0, screen.top)

    def test_a_click_on_a_panel_row_takes_it(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.type_text("1")
        screen.enter()
        drawn = screen.frame(width=100, height=20)
        line = next(
            number for number, key in drawn.row_lines.items() if key == "panel:close"
        )
        batch = screen.click(line, 6, drawn)
        self.assertEqual("Closed Alpha.", batch.success)


class ResultTests(unittest.TestCase):
    def test_a_finished_action_reports_after_the_fact(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.type_text("1")
        screen.enter()
        batch = screen.enter()
        screen.action_finished(batch, ok=True)
        self.assertEqual("Closed Alpha.", screen.status)
        self.assertEqual("", screen.input_text)

    def test_a_refused_action_says_nothing_changed(self) -> None:
        screen = picker(row("Alpha", number=1))
        screen.type_text("1")
        screen.enter()
        batch = screen.enter()
        screen.action_finished(batch, ok=False)
        self.assertEqual("Close was refused. Nothing changed.", screen.status)

    def test_a_verb_that_answers_for_itself_is_quoted_as_it_answered(self) -> None:
        screen = picker(row("Alpha", number=1), models=("opus",))
        screen.type_text("1")
        screen.enter()
        panel_to(screen, "Change model")
        screen.enter()
        batch = screen.enter()
        screen.action_finished(
            batch, ok=False, note="session-kit: there is no command named change-model"
        )
        self.assertEqual(
            "session-kit: there is no command named change-model", screen.status
        )

    def test_a_cached_list_refuses_to_open_anything(self) -> None:
        screen = picker()
        screen.set_inventory(inventory(row("Alpha", number=1), stale=True))
        self.assertIsNone(screen.enter())
        self.assertEqual("Nothing changed.", screen.status)


class NoConfirmationTests(unittest.TestCase):
    def test_no_screen_in_any_action_path_asks_a_question(self) -> None:
        screen = picker(row("Alpha", number=1), accounts=("primary",), models=("opus",))
        seen = [screen.frame(width=100, height=20).joined()]
        screen.type_text("1")
        seen.append(screen.frame(width=100, height=20).joined())
        screen.enter()
        seen.append(screen.frame(width=100, height=20).joined())
        for _ in range(len(screen.panel.items)):
            seen.append(screen.frame(width=100, height=20).joined())
            screen.move(1)
        for text in seen:
            text = "\n".join(text.splitlines()[:-1])
            self.assertNotIn("[y/N]", text)
            self.assertNotIn("(y/n)", text)
            self.assertNotIn("Are you sure", text)
            self.assertNotIn("?", text)


if __name__ == "__main__":
    unittest.main()
