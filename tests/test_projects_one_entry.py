"""One directory is one project, and no project is locked to a provider.

The shortcut table used to answer two questions with one row: *where* a
project is, and *what opens it*. The second answer was binding, so a directory
worked on with Claude and with Codex needed two rows under two short names,
and adding a project asked a third question whose answer was already asked
again at every launch.

These tests hold the ruling: adding takes a directory (and a short name if you
want your own), a directory already listed is refused by name rather than
duplicated, the provider column is a default that a launch overrides, and a
project that keeps no default is never opened by guess.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from lib.sessionkit_inventory import projects
from lib.sessionkit_projects import identity, launch

TOOL = Path(__file__).resolve().parents[1] / "lib" / "sessionkit_inventory" / "projects.py"


class OneEntryPerDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-one-entry.")
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.file = self.root / "projects.tsv"
        self.work = self.root / "work"
        self.work.mkdir()
        self.other = self.root / "other"
        self.other.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def rows(self) -> list[list[str]]:
        if not self.file.exists():
            return []
        return [
            line.split("\t")
            for line in self.file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]

    def tool(self, *words: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                os.fspath(TOOL),
                "--projects-file",
                os.fspath(self.file),
                "--state-dir",
                os.fspath(self.state),
                *words,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    # ---- adding -----------------------------------------------------------

    def test_a_project_is_added_without_naming_a_provider(self) -> None:
        projects.add_project(self.file, self.state, "work", cwd=os.fspath(self.work))
        self.assertEqual([["work", "any", os.fspath(self.work)]], self.rows())

    def test_the_command_line_takes_a_short_name_and_a_directory(self) -> None:
        completed = self.tool("add", "work", os.fspath(self.work))
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([["work", "any", os.fspath(self.work)]], self.rows())
        self.assertIn("chosen when you start a session", completed.stdout)

    def test_a_directory_alone_is_enough_to_add_it(self) -> None:
        completed = self.tool("add", os.fspath(self.work))
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([["work", "any", os.fspath(self.work)]], self.rows())

    def test_the_old_three_word_form_still_records_its_provider(self) -> None:
        completed = self.tool("add", "work", "claude", os.fspath(self.work))
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([["work", "claude", os.fspath(self.work)]], self.rows())

    def test_a_second_entry_for_one_directory_is_refused_by_name(self) -> None:
        projects.add_project(self.file, self.state, "work", cwd=os.fspath(self.work))
        with self.assertRaises(projects.ProjectError) as refusal:
            projects.add_project(
                self.file, self.state, "work-codex", "codex", os.fspath(self.work)
            )
        self.assertIn("already on the list as work", str(refusal.exception))
        self.assertEqual([["work", "any", os.fspath(self.work)]], self.rows())

    # ---- the default ------------------------------------------------------

    def test_a_default_provider_is_set_and_cleared_without_re_adding(self) -> None:
        projects.add_project(self.file, self.state, "work", cwd=os.fspath(self.work))
        projects.set_default_provider(self.file, self.state, "work", "codex")
        self.assertEqual([["work", "codex", os.fspath(self.work)]], self.rows())
        projects.set_default_provider(self.file, self.state, "work", "any")
        self.assertEqual([["work", "any", os.fspath(self.work)]], self.rows())

    def test_a_default_never_prevents_the_other_provider(self) -> None:
        projects.add_project(
            self.file, self.state, "work", "claude", os.fspath(self.work)
        )
        resolver = identity.Resolver(self.file)
        project = resolver.resolve_alias("work")
        self.assertIsNotNone(project)
        plan = launch.launch_plan(project, requested_provider="codex")
        self.assertEqual("codex", plan["provider"])
        self.assertEqual("flag", plan["decisions"]["provider"])

    def test_a_project_with_no_default_is_never_opened_by_guess(self) -> None:
        projects.add_project(self.file, self.state, "work", cwd=os.fspath(self.work))
        resolver = identity.Resolver(self.file)
        project = resolver.resolve_alias("work")
        self.assertIsNotNone(project, "an `any` row must still resolve by alias")
        plan = launch.launch_plan(project)
        self.assertIsNone(plan["provider"])
        self.assertEqual("unset", plan["decisions"]["provider"])
        self.assertTrue(
            any("no default provider" in note for note in plan["notes"]),
            plan["notes"],
        )

    # ---- the directories already listed twice -----------------------------

    def write(self, text: str) -> None:
        self.file.write_text(text, encoding="utf-8")
        self.file.chmod(0o600)

    def test_a_directory_listed_twice_reads_as_one_project_with_no_default(
        self,
    ) -> None:
        self.write(
            f"work\tclaude\t{self.work}\n"
            f"work-codex\tcodex\t{self.work}\n"
            f"other\tshell\t{self.other}\n"
        )
        candidates = projects.project_candidates(self.file)
        listed = [(row["alias"], row["provider"]) for row in candidates["configured"]]
        self.assertEqual([("work", "any"), ("other", "shell")], listed)
        self.assertEqual(
            [("work-codex", "work")],
            [(row["alias"], row["kept_alias"]) for row in candidates["duplicates"]],
        )

    def test_normalize_folds_the_duplicate_and_keeps_every_other_line(self) -> None:
        self.write(
            "# hand written\n"
            f"work\tclaude\t{self.work}\n"
            f"work-codex\tcodex\t{self.work}\n"
            f"other\tshell\t{self.other}\n"
        )
        completed = self.tool("normalize")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("kept work and retired work-codex", completed.stdout)
        self.assertIn("# hand written", self.file.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted([["other", "shell", os.fspath(self.other)],
                    ["work", "any", os.fspath(self.work)]]),
            sorted(self.rows()),
        )

    def test_normalizing_a_file_with_no_duplicate_changes_nothing(self) -> None:
        self.write(f"work\tclaude\t{self.work}\nother\tshell\t{self.other}\n")
        before = self.file.read_bytes()
        completed = self.tool("normalize")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("exactly one entry", completed.stdout)
        self.assertEqual(before, self.file.read_bytes())

    def test_an_ignore_row_is_a_different_class_and_survives_untouched(self) -> None:
        self.write(
            f"work\tclaude\t{self.work}\n"
            f"work-codex\tcodex\t{self.work}\n"
            f"hidden\tignore\t{self.other}\n"
        )
        completed = self.tool("normalize")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(["hidden", "ignore", os.fspath(self.other)], self.rows())
        candidates = projects.project_candidates(self.file)
        self.assertEqual([os.fspath(self.other)], candidates["ignored"])


if __name__ == "__main__":
    unittest.main()
