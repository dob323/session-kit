"""What needs you is a fact about the estate, not about the current view.

Three ways the attention count used to disagree with reality, all proven on a
pseudo-terminal before they were fixed:

  * a search filter — including the preview drawn while the line was still
    being typed — emptied the count, and the Needs-you screen answered
    "Nothing needs you." while a session waited;
  * the home summary line de-duplicated a session against its own stall flag
    and the Needs-you screen did not, so the two screens printed different
    totals for the same estate. That summary line is gone (operator ruling,
    2026-08-15), so every count here is now asserted where the truth lives --
    on the Needs-you screen itself, which is the surface a person acts from;
  * a session whose shell generation could not be proven was dropped before
    any counter saw it, so the row most likely to need attention was the one
    row guaranteed not to be mentioned.
"""

from __future__ import annotations

import json
import time
import unittest

from tests.test_login import LoginFixture, inventory, row, run_pty


def waiting(shpool_id: str, *, number: int, provider: str = "codex") -> dict:
    return row(shpool_id, number=number, provider=provider, needs_you=True)


def quarantined(shpool_id: str, *, provider: str = "claude") -> dict:
    """A real, designed state: the shell generation could not be proven."""
    item = row(shpool_id, number=99, provider=provider, needs_you=True)
    item["terminal_number"] = None
    item["mutation_allowed"] = False
    item["mutation_rejection_reason"] = "unprovable-generation"
    item["recent_output_at_unix_ms"] = 1
    return item


def write_fleet(fixture: LoginFixture, *, stalls: list[dict]) -> None:
    fleet = fixture.home / ".local" / "state" / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / "inbox").mkdir(exist_ok=True)
    (fleet / "stalls.json").write_text(
        json.dumps({"generated_at": 4_000_000_000, "stalled": stalls}),
        encoding="utf-8",
    )


class AttentionSummaryLineTests(unittest.TestCase):
    """The home summary line is gone (operator ruling, 2026-08-15).

    It read `needs you: 56 · 6 sessions, 50 repair failures` on their screen. The
    50 were watchdog records that had never described a failed repair, so the
    loudest number on the login screen was the least true thing on it. They chose
    removal over a trimmed version. What a person can ACT on is unchanged: the
    rows still say "needs you" and the `a` screen still lists every item.
    """

    def test_the_home_screen_carries_no_attention_summary_line(self) -> None:
        fixture = LoginFixture(
            inventory(waiting("alpha", number=3), row("bravo", number=17))
        )
        try:
            code, output = run_pty(fixture, b"q\n", columns=120)
            self.assertEqual(0, code)
            # No summary line, at any count.
            self.assertNotIn("needs you: 1 ·", output)
            self.assertNotIn("needs you: 2 ·", output)
            self.assertNotIn("repair failure", output)
            # The row itself still says it, and the footer still offers the
            # screen that lists it.
            self.assertIn("needs you", output)
            self.assertIn("needs you a", output)
        finally:
            fixture.close()

    def test_the_line_is_gone_even_with_unresolved_watchdog_records(self) -> None:
        """The exact shape that produced their 56: sessions plus records."""
        fixture = LoginFixture(inventory(waiting("alpha", number=3)))
        (fixture.state / "watchdog-repairs.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "repairs": [
                        {
                            "at_unix_ms": 1,
                            "old_shpool_id": f"gone-{index}",
                            "new_shpool_id": "",
                            "title": f"Worker {index}",
                            "provider": "codex",
                            "outcome": "reported",
                            "reason": "no output for far longer than any normal pause",
                            "acknowledged": False,
                        }
                        for index in range(3)
                    ],
                }
            ),
            encoding="utf-8",
        )
        try:
            code, output = run_pty(fixture, b"a\nq\nq\n", columns=120)
            self.assertEqual(0, code)
            self.assertNotIn("needs you: 4 ·", output)
            self.assertNotIn("3 repair failures", output)
            # The review screen still shows every record, and now says what
            # actually happened rather than calling all three failed repairs.
            review = output[output.index("needs you: 4\n") :]
            self.assertIn("Watchdog reports", review)
            self.assertIn("reported quiet, no repair attempted", review)
            self.assertNotIn("automatic repair failed", review)
        finally:
            fixture.close()


class AttentionCountTests(unittest.TestCase):
    def test_a_filter_never_empties_the_attention_count(self) -> None:
        fixture = LoginFixture(
            inventory(
                waiting("alpha", number=3),
                row("bravo", number=17),
            )
        )
        try:
            # Filter to the row that is NOT waiting, then open the review.
            code, output = run_pty(fixture, b"/bravo\na\nq\nq\n", columns=120)
            self.assertEqual(0, code)
            filtered = output[output.index("Search: bravo") :]
            # The review survives the filter: the waiting session is still
            # counted and still listed, though the filter hides its row.
            self.assertNotIn("Nothing needs you.", filtered)
            self.assertIn("needs you: 1\n", filtered)
            self.assertIn("Codex alpha", filtered[filtered.index("needs you: 1\n") :])
        finally:
            fixture.close()

    def test_the_home_line_and_the_review_report_the_same_total(self) -> None:
        session = waiting("alpha", number=3, provider="claude")
        uuid = session["identity"]["uuid"]
        fixture = LoginFixture(inventory(session))
        write_fleet(
            fixture,
            stalls=[{"key": uuid, "reason": "unsurfaced", "since": 4_000_000_000}],
        )
        try:
            code, output = run_pty(fixture, b"a\nq\nq\n", columns=120)
            self.assertEqual(0, code)
            # One session, one fact, one number: the stall flag and the row
            # are the same session and are counted once.
            self.assertIn("needs you: 1\n", output)
            review = output[output.index("needs you: 1\n") :]
            self.assertNotIn("Stalled ·", review)
            self.assertIn("Claude alpha", review)
        finally:
            fixture.close()

    def test_a_stall_flag_for_a_session_the_filter_hides_is_still_its_row(
        self,
    ) -> None:
        session = waiting("alpha", number=3, provider="claude")
        uuid = session["identity"]["uuid"]
        fixture = LoginFixture(inventory(session, row("bravo", number=17)))
        write_fleet(
            fixture,
            stalls=[{"key": uuid, "reason": "unsurfaced", "since": 4_000_000_000}],
        )
        try:
            # Filter to the OTHER row, then open the review behind the filter.
            code, output = run_pty(fixture, b"/bravo\na\nq\nq\n", columns=120)
            self.assertEqual(0, code)
            filtered = output[output.index("Search: bravo") :]
            # Re-filing the same session as a stalled session was the defect:
            # the category moved because the row was hidden.
            self.assertIn("needs you: 1\n", filtered)
            self.assertNotIn("Stalled ·", filtered[filtered.index("needs you: 1\n") :])
            self.assertIn("Claude alpha", filtered[filtered.index("needs you: 1\n") :])
        finally:
            fixture.close()

    def test_a_quarantined_session_that_needs_you_still_surfaces(self) -> None:
        fixture = LoginFixture(
            inventory(waiting("alpha", number=3), quarantined("bravo"))
        )
        try:
            code, output = run_pty(fixture, b"a\nq\nq\n", columns=120)
            self.assertEqual(0, code)
            # Two sessions need the operator, and the review says two.
            review = output[output.index("needs you: 2\n") :]
            self.assertIn("Claude bravo", review)
            # It is listed as unactionable rather than offered as a number.
            self.assertIn("--  Claude bravo", review)
            self.assertIn("unavailable", review)
            self.assertIn("needs you since", review)
            self.assertIn("sp help unavailable", review)
        finally:
            fixture.close()

    def test_a_quarantined_setup_row_keeps_setup_age_and_unavailable(self) -> None:
        item = quarantined("setup")
        item["setup_incomplete"] = True
        item["recent_output_at_unix_ms"] = int((time.time() - 7200) * 1000)
        fixture = LoginFixture(inventory(item))
        try:
            code, output = run_pty(fixture, b"a\nq\nq\n", columns=120)
            self.assertEqual(0, code)
            review = output[output.index("needs you: 1\n") :]
            self.assertIn("pending", review)
            self.assertIn("needs you 2 hr", review)
            self.assertIn("unavailable", review)
        finally:
            fixture.close()


class ViewSurvivesAnActionTests(unittest.TestCase):
    """The filter, the page and the jump marker belong to the person."""

    def test_a_close_keeps_the_search_the_person_was_reading(self) -> None:
        first = row("bravo-one", number=17, provider="codex")
        second = row("bravo-two", number=18, provider="codex")
        fixture = LoginFixture(
            inventory(row("alpha", number=3), first, second),
            # The refresh after the action reports the estate without 17.
            refreshed_document=inventory(row("alpha", number=3), second),
        )
        try:
            code, output = run_pty(fixture, b"/bravo\nk 17\nq\n", columns=120)
            self.assertEqual(0, code)
            # The frame the close drew: one match of the two that are left,
            # still filtered. Before the fix this frame was page 1 of the
            # whole estate with no filter and no notice.
            after = [
                frame
                for frame in output.split("\x1b[2J")
                if "1 match of 2 sessions" in frame
            ]
            self.assertTrue(after, msg=output)
            self.assertIn("Search: bravo", after[0])
            self.assertNotIn("Codex alpha", after[0])
        finally:
            fixture.close()

    def test_the_r_key_still_clears_the_filter(self) -> None:
        fixture = LoginFixture(
            inventory(row("alpha", number=3), row("bravo", number=17))
        )
        try:
            code, output = run_pty(fixture, b"/bravo\nr\nq\n", columns=120)
            self.assertEqual(0, code)
            refreshed = output[output.rindex("2 sessions") :]
            self.assertNotIn("Search:", refreshed)
        finally:
            fixture.close()


class ActionReceiptTests(unittest.TestCase):
    """Every action says what it did, and says it before the refresh."""

    def test_a_close_names_what_it_closed(self) -> None:
        fixture = LoginFixture(
            inventory(row("alpha", number=3), row("bravo", number=17)),
            refreshed_document=inventory(row("alpha", number=3)),
        )
        try:
            code, output = run_pty(fixture, b"k 17\nq\n")
            self.assertEqual(0, code)
            self.assertIn("Closed session 17.", output)
        finally:
            fixture.close()

    def test_a_close_says_so_even_when_the_refresh_fails(self) -> None:
        """The receipt cannot depend on the refresh that follows it.

        A failed refresh prints "Showing the last confirmed list" -- and that
        list still holds the session just closed, with nothing on screen saying
        it was closed. The operator's only reasonable reading was that the close
        had not happened.
        """
        fixture = LoginFixture(inventory(row("alpha", number=3), row("bravo", number=17)))
        broken = fixture.base / "one-shot-status"
        broken.write_text(
            "#!/usr/bin/env bash\n"
            f"count={fixture.base / 'status-calls'}\n"
            'printf x >> "$count"\n'
            'if [[ ${1:-} == --json && $(wc -c < "$count") -gt 1 ]]; then exit 1; fi\n'
            f"exec {fixture.fake_status} \"$@\"\n",
            encoding="utf-8",
        )
        broken.chmod(0o755)
        try:
            code, output = run_pty(
                fixture,
                b"k 17\nq\n",
                env_updates={"SESSION_KIT_STATUS_CMD": str(broken)},
            )
            self.assertEqual(0, code)
            self.assertIn("Closed session 17.", output)
            self.assertIn("Live refresh failed", output)
            # The receipt comes first: the operator reads what happened, then
            # why the list behind it is the old one.
            self.assertLess(
                output.index("Closed session 17."),
                output.index("Live refresh failed"),
            )
        finally:
            fixture.close()

    def test_history_refuses_a_number_that_is_not_on_the_screen(self) -> None:
        sessions = [row(f"task-{number}", number=number) for number in range(1, 13)]
        fixture = LoginFixture(inventory(*sessions))
        try:
            # 12 rows on a 14-line terminal is two pages; every other row key
            # already refuses an off-page number, and history did not.
            code, output = run_pty(fixture, b"h 12\nq\n", lines=14)
            self.assertEqual(0, code)
            self.assertIn("There is no session 12 on this screen", output)
            self.assertEqual(
                [],
                [
                    entry
                    for entry in fixture.sp_entries()
                    if entry.get("args", [""])[0] == "picker-history"
                ],
            )
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
