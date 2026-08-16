"""Attention truth: what the count means, and when it is allowed to exist.

Three rules, all measured before they were written down. A session the person
is attached to and looking at is seen, never counted as waiting. Every counted
session is reachable from the count. Zero is reachable.

The count is model state; it no longer prints a summary line above the footer
(operator ruling, 2026-08-15), so these assert the count itself.
"""

from __future__ import annotations

import unittest

from tests.tui_support import Clock, inventory, picker, row

from sessionkit_tui import rows as rowmod


def attention_of(screen) -> int:
    """The count itself, from the helper that computes it.

    It used to be read off Screen.attention. Nothing in production read that
    field once the summary line went, so it was recomputed on every frame for
    the tests alone (found in review, 2026-08-15). The rule it encodes is real
    and still tested -- here, against rows.attention_count directly.
    """
    return rowmod.attention_count(
        [item for item in screen.screen().rows if item.kind == "session"]
    )


def needs_you_rows(screen):
    return [item.label for item in screen.visible_rows() if item.needs_you]


class AttachedIsSeenTests(unittest.TestCase):
    def test_the_session_attached_in_this_window_is_never_counted(self) -> None:
        here = row("Here", number=1, needs_you=True)
        screen = picker(
            here,
            row("Elsewhere", number=2, needs_you=True),
            self_session_id=here["shpool_id"],
        )
        self.assertEqual(1, attention_of(screen))
        self.assertEqual(["Elsewhere"], needs_you_rows(screen))

    def test_the_attached_session_still_appears_in_the_list(self) -> None:
        here = row("Here", number=1, needs_you=True)
        screen = picker(here, self_session_id=here["shpool_id"])
        labels = [item.label for item in screen.visible_rows()]
        self.assertIn("Here", labels)

    def test_a_seen_session_no_longer_counts_as_needing_you(self) -> None:
        # One word for one state: the row still SAYS needs you, because that
        # is its state. Being seen removes the attention treatment — the
        # count and the needs-you list — not the state word.
        here = row("Here", number=1, needs_you=True)
        screen = picker(here, self_session_id=here["shpool_id"])
        self.assertEqual(0, attention_of(screen))
        self.assertEqual([], needs_you_rows(screen))
        detail = screen.visible_rows()[0].detail
        self.assertIn("needs you", detail)


class OpenedFromHereTests(unittest.TestCase):
    def test_an_open_that_was_refused_leaves_the_session_counted(self) -> None:
        clock = Clock()
        screen = picker(row("Alpha", number=1, needs_you=True, quiet_seconds=10), clock=clock)
        screen.action_finished(screen.enter(), ok=False)
        self.assertEqual(1, attention_of(screen))

    def test_a_session_opened_from_here_stops_being_counted(self) -> None:
        clock = Clock()
        screen = picker(row("Alpha", number=1, needs_you=True, quiet_seconds=10), clock=clock)
        self.assertEqual(1, attention_of(screen))
        screen.action_finished(screen.enter(), ok=True)
        self.assertEqual(0, attention_of(screen))

    def test_it_counts_again_once_the_session_says_something_new(self) -> None:
        clock = Clock()
        screen = picker(row("Alpha", number=1, needs_you=True, quiet_seconds=10), clock=clock)
        screen.action_finished(screen.enter(), ok=True)
        clock.advance(60_000)
        screen.set_inventory(
            inventory(row("Alpha", number=1, needs_you=True, quiet_seconds=5))
        )
        self.assertEqual(1, attention_of(screen))

    def test_it_stays_seen_while_the_session_stays_quiet(self) -> None:
        clock = Clock()
        screen = picker(row("Alpha", number=1, needs_you=True, quiet_seconds=10), clock=clock)
        screen.action_finished(screen.enter(), ok=True)
        clock.advance(60_000)
        screen.set_inventory(
            inventory(row("Alpha", number=1, needs_you=True, quiet_seconds=70))
        )
        self.assertEqual(0, attention_of(screen))


class DrillableTests(unittest.TestCase):
    def test_every_counted_session_is_the_first_thing_the_list_shows(self) -> None:
        screen = picker(
            row("Quiet", number=1, agent_status="running"),
            row("Waiting", number=2, needs_you=True),
            row("Also waiting", number=3, needs_you=True),
        )
        count = attention_of(screen)
        self.assertEqual(2, count)
        sessions = [item for item in screen.visible_rows() if item.kind == rowmod.SESSION]
        self.assertTrue(all(item.needs_you for item in sessions[:count]))
        self.assertEqual({"Waiting", "Also waiting"}, {item.label for item in sessions[:count]})

    def test_moving_to_the_count_lands_on_the_sessions_it_counted(self) -> None:
        screen = picker(
            row("Quiet", number=1, agent_status="running"),
            row("Waiting", number=2, needs_you=True),
        )
        self.assertEqual("Waiting", screen.current_row().label)
        self.assertTrue(screen.current_row().needs_you)

    def test_a_machine_session_that_needs_you_is_counted_where_it_is_shown(self) -> None:
        screen = picker(
            row("Mine", number=1),
            row("Drill", number=2, origin="machine", needs_you=True),
        )
        self.assertEqual(0, attention_of(screen))
        self.assertIn(
            "1 machine session · 1 needs you",
            [item.label for item in screen.visible_rows()],
        )
        screen.machine_expanded = True
        self.assertEqual(1, attention_of(screen))
        self.assertEqual(["Drill"], needs_you_rows(screen))


class ZeroTests(unittest.TestCase):
    def test_at_zero_the_line_disappears_entirely(self) -> None:
        screen = picker(row("Alpha", number=1))
        text = screen.frame(width=100, height=20).joined()
        self.assertNotIn("need you", text)
        self.assertNotIn("0 sessions", text)

    def test_the_count_goes_away_the_moment_the_last_one_is_seen(self) -> None:
        clock = Clock()
        screen = picker(row("Alpha", number=1, needs_you=True, quiet_seconds=10), clock=clock)
        self.assertEqual(1, attention_of(screen))
        # No summary line at any count, before or after.
        self.assertNotIn("needs you: 1", screen.frame(width=100, height=20).joined())
        screen.action_finished(screen.enter(), ok=True)
        self.assertEqual(0, attention_of(screen))
        self.assertNotIn("need you", screen.frame(width=100, height=20).joined())

    def test_a_filtered_list_counts_only_what_it_is_showing(self) -> None:
        screen = picker(
            row("Blueprint", number=1, needs_you=True),
            row("Config Sweep", number=2, needs_you=True),
        )
        self.assertEqual(2, attention_of(screen))
        for character in "blue":
            screen.type_text(character)
        self.assertEqual(1, attention_of(screen))
        self.assertEqual(["Blueprint"], needs_you_rows(screen))


if __name__ == "__main__":
    unittest.main()
