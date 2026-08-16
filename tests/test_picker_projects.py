"""The picker's projects menu: hiding a directory happens on the keystroke,
says what it did, and is reversible from the same menu.

"Drop number" wrote an ignore row -- the directory left the list and no verb
anywhere in the picker brought it back. These tests hold the two halves of
that fix together: the honest wording, and the restore. Nothing here asks a
question: no screen in the kit does.
"""

from __future__ import annotations

import unittest

from tests.test_login import LoginFixture, display_cells, inventory, row, run_pty, strip_sgr


def projects_rows(fixture: LoginFixture) -> list[list[str]]:
    return [
        line.split("\t")
        for line in fixture.projects.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


class ProjectsMenuTests(unittest.TestCase):
    def fixture(self) -> LoginFixture:
        return LoginFixture(inventory(row("one", number=1)))

    def test_hiding_a_project_happens_on_the_key_and_says_what_it_did(self) -> None:
        fixture = self.fixture()
        try:
            # m: More · p: Projects · d 1: hide the first row. No question is
            # asked anywhere, and the line afterwards names the way back.
            code, output = run_pty(fixture, b"m\np\nd 1\n\n\nq\n")
            self.assertEqual(0, code)
            self.assertNotIn("[y/N]", output)
            self.assertIn("Restore it from Projects.", output)
            hidden = [entry for entry in projects_rows(fixture) if entry[1] == "ignore"]
            self.assertEqual(
                [str(fixture.primary_project)], [entry[2] for entry in hidden]
            )
        finally:
            fixture.close()

    def test_a_hidden_project_is_restorable_from_the_same_menu(self) -> None:
        fixture = self.fixture()
        try:
            code, _ = run_pty(fixture, b"m\np\nd 1\n\n\nq\n")
            self.assertEqual(0, code)
            hidden = [entry for entry in projects_rows(fixture) if entry[1] == "ignore"]
            self.assertEqual(
                [str(fixture.primary_project)], [entry[2] for entry in hidden]
            )

            # The menu now offers the way back, and takes it.
            code, output = run_pty(fixture, b"m\np\nu\n1\n\n\nq\n")
            self.assertEqual(0, code)
            self.assertIn("restore u", output)
            self.assertIn("Hidden directories", output)
            self.assertIn("No longer ignoring", output)
            self.assertEqual(
                [],
                [entry for entry in projects_rows(fixture) if entry[1] == "ignore"],
            )
        finally:
            fixture.close()

    def test_the_menu_offers_no_restore_when_nothing_is_hidden(self) -> None:
        fixture = self.fixture()
        try:
            code, output = run_pty(fixture, b"m\np\n\n\nq\n")
            self.assertEqual(0, code)
            self.assertIn("hide d number", output)
            self.assertNotIn("restore u", output)
        finally:
            fixture.close()

    def test_wide_project_aliases_keep_the_path_column_aligned(self) -> None:
        fixture = self.fixture()
        try:
            with fixture.projects.open("a", encoding="utf-8") as handle:
                handle.write(f"東京\tclaude\t{fixture.other}\n")
            code, output = run_pty(fixture, b"m\np\n\n\nq\n", columns=120)
            self.assertEqual(0, code)
            plain = strip_sgr(output)
            lines = [
                line
                for line in plain.splitlines()
                if str(fixture.primary_project) in line or str(fixture.other) in line
            ]
            self.assertGreaterEqual(len(lines), 2, plain)
            starts = {
                display_cells(line[: line.index(path)])
                for line in lines[-2:]
                for path in (str(fixture.primary_project), str(fixture.other))
                if path in line
            }
            self.assertEqual(1, len(starts), lines[-2:])
        finally:
            fixture.close()

    # The branch's test_an_unusable_provider_keeps_the_form_it_was_typed_into
    # asserted the add-time provider question, which the projects ruling
    # retired: the provider is chosen when a session starts. Dropped in the
    # merge; the two tests below carry the current contract.
    def test_adding_a_project_never_asks_which_provider_opens_it(self) -> None:
        """Two answers, and neither of them is a provider.

        The third question used to be binding: answering it is what made a
        directory also worked on with the other provider need a second row
        under a second short name. It is asked when a session starts instead,
        so the row that lands here carries no provider at all.
        """
        fixture = self.fixture()
        extra = fixture.base / "extra-project"
        extra.mkdir()
        try:
            code, output = run_pty(
                fixture,
                b"m\np\na\n" + str(extra).encode() + b"\nnewalias\n\n\nq\n",
            )
            self.assertEqual(0, code)
            self.assertNotIn("Opens as claude, codex, or shell", output)
            self.assertIn(
                "Claude, Codex, or a shell is chosen when you start a session", output
            )
            self.assertIn(["newalias", "any", str(extra)], projects_rows(fixture))
        finally:
            fixture.close()

    def test_a_directory_already_listed_is_refused_by_the_name_it_has(self) -> None:
        """One directory, one entry — and the refusal says which entry.

        Adding it again under a second short name is exactly how a directory
        ended up listed twice, once per provider.
        """
        fixture = self.fixture()
        try:
            code, output = run_pty(
                fixture,
                b"m\np\na\n" + str(fixture.other).encode() + b"\nsecondname\n\n\nq\n",
            )
            self.assertEqual(0, code)
            self.assertIn("already on the list as other", output)
            self.assertEqual(
                [["main", "claude", str(fixture.primary_project)],
                 ["other", "codex", str(fixture.other)]],
                projects_rows(fixture),
            )
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
