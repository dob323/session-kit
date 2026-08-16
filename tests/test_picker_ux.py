"""Picker navigation: live filtering, jump, grouping, compact rows, and the
single help table.

The picker is the surface a person lives in all day, so every test here asks
the same two questions of a new behaviour: does it show the truth, and can it
change anything by accident. The jump key marks rather than selects, grouping
and compact only reorder or redraw what the unfiltered list already contained,
and a filter previewed mid-typing is undone the moment the line turns out to
be a command.
"""

from __future__ import annotations

import json
import re
import unittest

from tests.support import REPO
from tests.test_login import LoginFixture, inventory, row, run_pty

LOGIN = REPO / "bin" / "shpool_login"


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


class PeekIsGoneTests(unittest.TestCase):
    """`i<n>` was documented in four places and never had a helper to run.

    It is not a key any more, so it answers like any other key the picker does
    not have, and nothing on any screen teaches it.
    """

    def test_i_is_an_unknown_key_and_changes_nothing(self) -> None:
        item = waiting_row("parser", number=1)
        fixture = LoginFixture(inventory(item))
        try:
            code, output = run_pty(fixture, b"i1\nq\n")
            self.assertEqual(0, code)
            self.assertIn("There is no such key on this screen.", output)
            self.assertNotIn("Peek", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_no_screen_teaches_peek_any_more(self) -> None:
        fixture = LoginFixture(inventory(row("only", number=1)))
        try:
            code, output = run_pty(fixture, b"?\n\nq\n", columns=110)
            self.assertEqual(0, code)
            self.assertNotIn("Peek", output)
            self.assertNotIn("i<n>", output)
            self.assertNotIn("peek", output)
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
            self.assertEqual(0, code)
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
            self.assertEqual(0, code)
            undone = output[output.rindex("Nothing needs you.") :]
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
                b"/alpha\nq\n",
                env_updates={"SESSION_KIT_PICKER_FILTER_LIVE": "0"},
            )
            self.assertEqual(0, code)
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
            code, output = run_pty(fixture, b"g\ng\ng\nq\n")
            self.assertEqual(0, code)
            announcements = re.findall(r"Session (\d+) needs you", output)
            self.assertEqual(["2", "3", "2"], announcements)
            # The row is marked, never selected: no command ran.
            self.assertEqual([], fixture.sp_entries())
            self.assertIn("▸", output)
        finally:
            fixture.close()

    def test_jump_says_so_when_nothing_is_waiting(self) -> None:
        fixture = LoginFixture(inventory(row("calm", number=1)))
        try:
            code, output = run_pty(fixture, b"g\nq\n")
            self.assertEqual(0, code)
            self.assertIn("Nothing needs you.", output)
            self.assertNotIn("▸", output)
        finally:
            fixture.close()

    def test_jump_repages_after_clearing_a_filter_and_draws_the_named_row(self) -> None:
        sessions = [
            row(f"ready-{number:02d}", number=number)
            for number in range(1, 42)
        ]
        # Setup-incomplete sessions are in the attention projection without
        # being sorted to the head of their availability group. Number 20 is
        # therefore on a middle page after the search is cleared.
        sessions[19]["setup_incomplete"] = True
        fixture = LoginFixture(inventory(*sessions))
        try:
            code, output = run_pty(
                fixture, b"/ready-01\ng\nq\n", lines=24, columns=110
            )
            self.assertEqual(0, code)
            after = output[output.index("Session 20 needs you.") :]
            self.assertIn("Codex ready-20", after)
            marked = next(line for line in after.splitlines() if "Codex ready-20" in line)
            self.assertIn("▸", marked)
            self.assertIn("Search cleared to show session 20.", output)
        finally:
            fixture.close()

    def test_jump_keeps_a_search_when_the_target_is_already_visible(self) -> None:
        fixture = LoginFixture(inventory(waiting_row("asking", number=2)))
        try:
            code, output = run_pty(fixture, b"/asking\ng\nq\n", columns=100)
            self.assertEqual(0, code)
            after = output[output.index("Session 2 needs you.") :]
            self.assertIn("Search: asking", after)
            self.assertNotIn("Search cleared", output)
            self.assertIn("▸", after)
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
            code, output = run_pty(fixture, b"q\n")
            self.assertEqual(0, code)
            self.assertIn("Ready", output)
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
            code, output = run_pty(fixture, b"group provider\nq\n")
            self.assertEqual(0, code)
            grouped = output[output.index("Grouping by provider.") :]
            self.assertIn("Claude", grouped)
            self.assertIn("Codex", grouped)
            self.assertNotIn("Open elsewhere", grouped)
            self.assertEqual(["1", "2"], row_numbers(grouped))
        finally:
            fixture.close()

    def test_bare_group_cycles_state_provider_project(self) -> None:
        fixture = LoginFixture(inventory(row("one", number=1)))
        try:
            code, output = run_pty(fixture, b"group\ngroup\ngroup\nq\n")
            self.assertEqual(0, code)
            self.assertEqual(
                ["provider", "project", "state"],
                re.findall(r"Grouping by (\w+)\.", output),
            )
        finally:
            fixture.close()

    def test_an_unknown_grouping_changes_nothing(self) -> None:
        fixture = LoginFixture(inventory(row("one", number=1)))
        try:
            code, output = run_pty(fixture, b"group sideways\nq\n")
            self.assertEqual(0, code)
            self.assertIn("Grouping is state, provider, or project", output)
            self.assertIn("Ready", output)
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
            code, output = run_pty(fixture, b"group project\nq\n")
            self.assertEqual(0, code)
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
            code, output = run_pty(fixture, b"group project\nq\n")
            self.assertEqual(0, code)
            self.assertIn("delegation core", output[output.index("Grouping by") :])
        finally:
            fixture.close()


class CompactTests(unittest.TestCase):
    def test_compact_removes_secondary_row_details_even_on_a_wide_screen(self) -> None:
        item = row("delegated", number=1, provider="codex")
        item["active_subagent_count"] = 2
        item["worktree"] = {"branch": "feature/compact-proof"}
        fixture = LoginFixture(inventory(item))
        try:
            code, output = run_pty(fixture, b"c\nq\n", columns=140)
            self.assertEqual(0, code)
            before, _, after = output.partition("Compact rows on.")
            self.assertIn("2 subagents", before)
            self.assertIn("worktree feature/compact-proof", before)
            self.assertIn("2 subagents", after)
            self.assertNotIn("worktree feature/compact-proof", after)
        finally:
            fixture.close()

    def test_compact_drops_headings_and_shows_more_sessions(self) -> None:
        sessions = [row(f"task-{number}", number=number) for number in range(1, 31)]
        fixture = LoginFixture(inventory(*sessions))
        try:
            code, output = run_pty(fixture, b"c\nq\n", lines=24, columns=100)
            self.assertEqual(0, code)
            before, _, after = output.partition("Compact rows on.")
            self.assertIn("Ready", before)
            self.assertNotIn("Open elsewhere", after)
            self.assertGreater(len(row_numbers(after)), len(row_numbers(before)))
        finally:
            fixture.close()

    def test_compact_can_be_turned_back_off(self) -> None:
        fixture = LoginFixture(inventory(row("one", number=1)))
        try:
            code, output = run_pty(fixture, b"c\nc\nq\n")
            self.assertEqual(0, code)
            self.assertIn("Compact rows on.", output)
            after = output[output.index("Compact rows off.") :]
            self.assertIn("Ready", after)
        finally:
            fixture.close()

    def test_compact_can_start_on(self) -> None:
        fixture = LoginFixture(inventory(row("one", number=1)))
        try:
            code, output = run_pty(
                fixture,
                b"q\n",
                env_updates={"SESSION_KIT_PICKER_COMPACT": "1"},
            )
            self.assertEqual(0, code)
            self.assertNotIn("Ready\n", output)
            self.assertEqual(["1"], row_numbers(output))
        finally:
            fixture.close()


class BackOnlyScreenTests(unittest.TestCase):
    """Read-only screens say so instead of eating what was typed at them."""

    def test_help_refuses_a_line_it_cannot_act_on(self) -> None:
        fixture = LoginFixture(inventory(row("one", number=1)))
        try:
            # A row number typed on the help screen used to be swallowed: the
            # screen went back and nothing opened, with nothing said.
            code, output = run_pty(fixture, b"?\n1\n\nq\n")
            self.assertEqual(0, code)
            self.assertIn("There is nothing to choose on this screen", output)
            self.assertEqual([], fixture.sp_entries())
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
            code, output = run_pty(fixture, b"?\n\nq\n", columns=110)
            self.assertEqual(0, code)
            help_text = output[output.index("Picker help") :]
            expected = {
                "q": "q",
                "p": "Projects:",
                "r": "r",
                "m": "m",
                "a": "a",
                "g": "g",
                "x": "x",
                "c": "c",
                "u": "u",
                "o": "o",
                "n": "n",
                "\\?": "?",
                "next": "next",
                "prev": "prev",
                "group": "group",
                "group\\ *": "group",
                "compact": "c",
                "h": "h number",
                "h[0-9]*": "h number",
                "h\\ *": "h number",
                "k\\ *": "k numbers",
                "k[0-9]*": "k numbers",
                "name\\ *": "name number",
                "name[0-9]*": "name number",
                "name\\ reset\\ *": "name reset number",
                "name\\ reset[0-9]*": "name reset number",
                "fork\\ *": "fork number",
                "fork[0-9]*": "fork number",
                "model\\ *": "model number",
                "model[0-9]*": "model number",
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

    def test_a_key_takes_its_number_with_or_without_the_space(self) -> None:
        """k3, name3, and fork3 used to be unknown keys."""
        for typed, command in (
            (b"k9\nq\n", "picker-close"),
            (b"k 9\nq\n", "picker-close"),
            (b"name9\nBetter name\nq\n", "picker-name"),
            (b"fork9\nq\n", "picker-fork"),
        ):
            with self.subTest(typed=typed):
                fixture = LoginFixture(
                    inventory(row("open1", number=9, provider="codex"))
                )
                try:
                    code, output = run_pty(fixture, typed)
                    self.assertEqual(0, code)
                    self.assertNotIn("There is no such key", output)
                    self.assertIn(
                        command,
                        [entry["args"][0] for entry in fixture.sp_entries()],
                    )
                finally:
                    fixture.close()

    def test_model_action_wires_the_displayed_row_to_the_existing_verb(self) -> None:
        item = row("modeled", number=9, provider="codex")
        item["model"] = "gpt-old"
        fixture = LoginFixture(inventory(item))
        models = fixture.base / "models.tsv"
        models.write_text("codex\tgpt-new\n", encoding="utf-8")
        try:
            code, output = run_pty(
                fixture,
                b"model9\n1\nq\n",
                columns=120,
                env_updates={"SESSION_KIT_MODELS_FILE": str(models)},
            )
            self.assertEqual(0, code)
            self.assertIn("gpt-old", output)
            self.assertIn("1  gpt-new", output)
            entries = fixture.sp_entries()
            self.assertEqual("picker-change-model", entries[0]["args"][0])
            self.assertEqual("gpt-new", entries[0]["args"][2])
            self.assertEqual("modeled", entries[0]["proof"]["shpool_id"])
        finally:
            fixture.close()

    def test_model_action_refuses_text_outside_the_configured_list(self) -> None:
        item = row("modeled", number=9, provider="codex")
        fixture = LoginFixture(inventory(item))
        models = fixture.base / "models.tsv"
        models.write_text("codex\tgpt-approved\n", encoding="utf-8")
        try:
            code, output = run_pty(
                fixture,
                b"model9\ngpt-made-up\nq\n",
                env_updates={"SESSION_KIT_MODELS_FILE": str(models)},
            )
            self.assertEqual(0, code)
            self.assertIn("not in this provider's configured list", output)
            self.assertFalse(
                any(
                    entry["args"][0] == "picker-change-model"
                    for entry in fixture.sp_entries()
                )
            )
        finally:
            fixture.close()

    def test_model_menu_offers_only_models_the_provider_verb_accepts(self) -> None:
        item = row("modeled", number=9, provider="claude")
        fixture = LoginFixture(inventory(item))
        models = fixture.base / "models.tsv"
        models.write_text(
            "*\tclaude-opus-5\n*\tgpt-5-codex\n*\topus-4.6\n",
            encoding="utf-8",
        )
        try:
            code, output = run_pty(
                fixture,
                b"model9\n1\nq\n",
                env_updates={"SESSION_KIT_MODELS_FILE": str(models)},
            )
            self.assertEqual(0, code)
            menu = output[output.index("Models") : output.index("Model number")]
            self.assertIn("claude-opus-5", menu)
            self.assertNotIn("gpt-5-codex", menu)
            self.assertNotIn("opus-4.6", menu)
            changed = [
                entry
                for entry in fixture.sp_entries()
                if entry["args"][0] == "picker-change-model"
            ]
            self.assertEqual("claude-opus-5", changed[0]["args"][2])
        finally:
            fixture.close()

    def test_zero_and_out_of_range_model_numbers_get_only_a_plain_refusal(self) -> None:
        for requested in ("0", "9"):
            with self.subTest(requested=requested):
                item = row("modeled", number=9, provider="codex")
                fixture = LoginFixture(inventory(item))
                models = fixture.base / "models.tsv"
                models.write_text("codex\tgpt-approved\n", encoding="utf-8")
                try:
                    code, output = run_pty(
                        fixture,
                        f"model9\n{requested}\nq\n".encode(),
                        env_updates={"SESSION_KIT_MODELS_FILE": str(models)},
                    )
                    self.assertEqual(0, code)
                    self.assertIn(
                        "That model is not in this provider's configured list. Nothing changed.",
                        output,
                    )
                    self.assertNotIn("sed:", output)
                    self.assertFalse(
                        any(
                            entry["args"][0] == "picker-change-model"
                            for entry in fixture.sp_entries()
                        )
                    )
                finally:
                    fixture.close()

    def test_help_is_reachable_from_more_and_from_needs_you(self) -> None:
        for label, payload in (
            ("more", b"m\n?\n\n\nq\n"),
            ("needs you", b"a\n?\n\n\nq\n"),
        ):
            with self.subTest(label=label):
                fixture = LoginFixture(inventory(row("one", number=1)))
                try:
                    code, output = run_pty(fixture, payload)
                    self.assertEqual(0, code)
                    self.assertIn("Picker help", output)
                finally:
                    fixture.close()




if __name__ == "__main__":
    unittest.main()
