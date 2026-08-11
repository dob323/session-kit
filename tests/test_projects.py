from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from lib.sessionkit_inventory import projects


class ProjectDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-projects.")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.claude = self.home / ".claude"
        self.codex = self.home / ".codex"
        self.claude.mkdir()
        self.codex.mkdir()
        self.environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "SESSION_KIT_CODEX_HOME": str(self.codex),
                "TMPDIR": str(self.root.parent),
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def directory(self, relative: str) -> Path:
        path = self.root / relative
        path.mkdir(parents=True)
        return path.resolve()

    def private_json(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)

    def codex_database(self, rows: list[str]) -> None:
        connection = sqlite3.connect(self.codex / "state_5.sqlite")
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT)")
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?)",
            [
                (f"00000000-0000-4000-8000-{number:012d}", cwd)
                for number, cwd in enumerate(rows)
            ],
        )
        connection.commit()
        connection.close()

    def test_discovers_both_providers_without_scanning_temp_projects(self) -> None:
        claude_project = self.directory("work/claude")
        shared = self.directory("work/shared")
        codex_thread = self.directory("work/codex-thread")
        temporary = self.directory("claude-tmp/scratchpad/test")
        self.private_json(
            self.claude / "settings.json",
            {"env": {"CLAUDE_CODE_TMPDIR": str(self.root / "claude-tmp")}},
        )
        self.private_json(
            self.home / ".claude.json",
            {
                "projects": {
                    str(claude_project): {},
                    str(shared): {},
                    str(temporary): {},
                    str(self.root / "missing"): {},
                }
            },
        )
        history = self.claude / "history.jsonl"
        history.write_text(
            "\n".join(
                (
                    json.dumps({"project": str(claude_project)}),
                    "not json",
                    json.dumps({"project": str(temporary)}),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        history.chmod(0o600)
        (self.codex / "config.toml").write_text(
            f'[projects."{shared}"]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        self.codex_database(
            [str(codex_thread), str(shared), str(self.root / "missing")]
        )

        result = projects.discover_projects(self.home)

        self.assertEqual(result["warnings"], [])
        self.assertEqual(
            {(row["provider"], row["cwd"]) for row in result["projects"]},
            {
                ("claude", str(claude_project)),
                ("claude", str(shared)),
                ("codex", str(shared)),
                ("codex", str(codex_thread)),
            },
        )

    def test_discovery_excludes_inaccessible_paths_but_keeps_shared_readable_repos(
        self,
    ) -> None:
        inaccessible = self.directory("private/root-copy")
        shared = self.directory("shared/team-repo")
        self.private_json(
            self.home / ".claude.json",
            {"projects": {str(inaccessible): {}, str(shared): {}}},
        )
        real_access = os.access

        def selective_access(path: os.PathLike[str], mode: int) -> bool:
            if Path(path) == inaccessible:
                return False
            return real_access(path, mode)

        with mock.patch.object(projects.os, "access", side_effect=selective_access):
            result = projects.discover_projects(self.home)

        self.assertEqual(
            result["projects"],
            [{"provider": "claude", "cwd": str(shared)}],
        )

    def test_codex_home_falls_back_to_provider_override(self) -> None:
        project = self.directory("work/provider-home")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex)}, clear=False):
            os.environ.pop("SESSION_KIT_CODEX_HOME", None)
            (self.codex / "config.toml").write_text(
                f'[projects."{project}"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            result = projects.discover_projects(self.home)
        self.assertIn(
            ("codex", str(project)),
            {(row["provider"], row["cwd"]) for row in result["projects"]},
        )

    def test_import_preserves_existing_rows_and_builds_unique_aliases(self) -> None:
        first = self.directory("work/one/app")
        second = self.directory("work/two/app")
        shared = self.directory("work/shared")
        self.private_json(
            self.home / ".claude.json",
            {"projects": {str(first): {}, str(second): {}, str(shared): {}}},
        )
        (self.codex / "config.toml").write_text(
            f'[projects."{shared}"]\ntrust_level = "trusted"\n', encoding="utf-8"
        )
        projects_file = self.root / "config/projects.tsv"
        projects_file.parent.mkdir()
        projects_file.write_text(
            "# keep this comment\nmanual\tshell\t" + str(self.root) + "\n",
            encoding="utf-8",
        )
        projects_file.chmod(0o600)
        state = self.root / "state"

        result = projects.import_projects(projects_file, state)

        rows = projects._parse_projects(projects_file.read_bytes())
        self.assertEqual(rows[0]["alias"], "manual")
        self.assertEqual(
            {(row["alias"], row["provider"], row["cwd"]) for row in result["added"]},
            {
                ("one-app", "claude", str(first)),
                ("two-app", "claude", str(second)),
                ("shared", "claude", str(shared)),
            },
        )
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertIn("# keep this comment", projects_file.read_text(encoding="utf-8"))
        self.assertEqual(projects_file.stat().st_mode & 0o777, 0o600)

        repeated = projects.import_projects(projects_file, state)
        self.assertEqual(repeated["added"], [])
        self.assertIsNone(repeated["backup"])

    def test_import_never_adds_a_second_row_for_a_configured_directory(self) -> None:
        configured = self.directory("work/app")
        self.private_json(
            self.home / ".claude.json", {"projects": {str(configured): {}}}
        )
        (self.codex / "config.toml").write_text(
            f'[projects."{configured}"]\ntrust_level = "trusted"\n', encoding="utf-8"
        )
        projects_file = self.root / "projects.tsv"
        projects_file.write_text(f"sl\tclaude\t{configured}\n", encoding="utf-8")
        projects_file.chmod(0o600)
        state = self.root / "state"

        result = projects.import_projects(projects_file, state)

        self.assertEqual(result["added"], [])
        self.assertEqual(
            projects._parse_projects(projects_file.read_bytes()),
            [{"alias": "sl", "provider": "claude", "cwd": str(configured)}],
        )

    def test_selection_grammar_is_shared_by_the_installer_and_the_picker(self) -> None:
        rows = [{"cwd": f"/w/{index}"} for index in range(1, 6)]
        pick = lambda answer: [  # noqa: E731
            row["cwd"] for row in projects.select_rows(rows, answer)
        ]
        every = ["/w/1", "/w/2", "/w/3", "/w/4", "/w/5"]
        self.assertEqual(pick("a"), every)
        # The picker takes the whole word; so does everything else now.
        self.assertEqual(pick("all"), every)
        self.assertEqual(pick("ALL"), every)
        self.assertEqual(pick("2"), ["/w/2"])
        self.assertEqual(pick(" 4 , 2-3 "), ["/w/2", "/w/3", "/w/4"])
        self.assertEqual(pick("4 2"), ["/w/2", "/w/4"])
        self.assertEqual(pick("3,3"), ["/w/3"])
        self.assertEqual(pick("2-2"), ["/w/2"])
        self.assertEqual(pick(""), [])
        for bad in (
            "0",
            "6",
            "2-1",
            "1-",
            "x",
            "1,,2",
            "one",
            "-3",
            "2,",
            "1-99999",  # a range no picker could show, refused before it is built
            "\u0663",  # an Arabic-Indic digit is not a number a picker printed
            "\u00b2",  # a superscript passes str.isdigit() and breaks int()
        ):
            with self.assertRaises(projects.ProjectError, msg=bad):
                projects.select_rows(rows, bad)

    def test_candidates_report_what_import_would_add_without_writing(self) -> None:
        fresh = self.directory("work/fresh")
        taken = self.directory("work/taken")
        self.private_json(
            self.home / ".claude.json",
            {"projects": {str(fresh): {}, str(taken): {}}},
        )
        projects_file = self.root / "projects.tsv"
        projects_file.write_text(f"sl\tclaude\t{taken}\n", encoding="utf-8")
        projects_file.chmod(0o600)
        before = projects_file.read_bytes()

        value = projects.project_candidates(projects_file)

        self.assertEqual(
            value["candidates"],
            [{"alias": "fresh", "provider": "claude", "cwd": str(fresh)}],
        )
        self.assertEqual(
            value["configured"],
            [{"alias": "sl", "provider": "claude", "cwd": str(taken)}],
        )
        self.assertEqual(value["ignored"], [])
        self.assertEqual(before, projects_file.read_bytes())

    def test_import_only_takes_the_directories_that_were_chosen(self) -> None:
        wanted = self.directory("work/wanted")
        skipped = self.directory("work/skipped")
        self.private_json(
            self.home / ".claude.json",
            {"projects": {str(wanted): {}, str(skipped): {}}},
        )
        projects_file = self.root / "projects.tsv"
        projects_file.write_text("", encoding="utf-8")
        projects_file.chmod(0o600)
        state = self.root / "state"

        result = projects.import_projects(projects_file, state, only=[str(wanted)])

        self.assertEqual(
            result["added"],
            [{"alias": "wanted", "provider": "claude", "cwd": str(wanted)}],
        )
        # The directory that was not chosen stays a candidate rather than
        # being silently ignored for good.
        self.assertEqual(
            [
                row["cwd"]
                for row in projects.project_candidates(projects_file)["candidates"]
            ],
            [str(skipped)],
        )

    def test_adding_a_directory_reverses_its_ignore_under_any_alias(self) -> None:
        target = self.directory("work/kit")
        projects_file = self.root / "projects.tsv"
        projects_file.write_text("", encoding="utf-8")
        projects_file.chmod(0o600)
        state = self.root / "state"
        projects.ignore_project(projects_file, state, str(target))

        # Reusing the ignore row's own alias must not collide with it.
        result = projects.add_project(projects_file, state, "kit", "codex", str(target))

        self.assertEqual(
            result["added"],
            [{"alias": "kit", "provider": "codex", "cwd": str(target)}],
        )
        self.assertEqual(
            projects._parse_projects(projects_file.read_bytes()),
            [{"alias": "kit", "provider": "codex", "cwd": str(target)}],
        )

    def test_add_derives_an_unused_alias_when_none_is_given(self) -> None:
        target = self.directory("work/newsite/app")
        projects_file = self.root / "projects.tsv"
        projects_file.write_text("", encoding="utf-8")
        projects_file.chmod(0o600)

        result = projects.add_project(
            projects_file, self.root / "state", None, "claude", str(target)
        )

        # "app" is a generic leaf, so the suggestion widens to the parent.
        self.assertEqual(
            result["added"],
            [{"alias": "newsite-app", "provider": "claude", "cwd": str(target)}],
        )

    def test_ignore_withdraws_a_shortcut_and_survives_rediscovery(self) -> None:
        ignored = self.directory("work/parent")
        kept = self.directory("work/parent/app")
        self.private_json(
            self.home / ".claude.json",
            {"projects": {str(ignored): {}, str(kept): {}}},
        )
        projects_file = self.root / "projects.tsv"
        projects_file.write_text(
            f"# keep this comment\nsl\tclaude\t{kept}\nparent\tclaude\t{ignored}\n",
            encoding="utf-8",
        )
        projects_file.chmod(0o600)
        state = self.root / "state"

        result = projects.ignore_project(projects_file, state, str(ignored))

        self.assertEqual(
            result["added"],
            [{"alias": "parent", "provider": "ignore", "cwd": str(ignored)}],
        )
        self.assertEqual([row["alias"] for row in result["removed"]], ["parent"])
        self.assertEqual(
            projects._parse_projects(projects_file.read_bytes()),
            [
                {"alias": "sl", "provider": "claude", "cwd": str(kept)},
                {"alias": "parent", "provider": "ignore", "cwd": str(ignored)},
            ],
        )
        self.assertIn("# keep this comment", projects_file.read_text(encoding="utf-8"))
        self.assertEqual(projects_file.stat().st_mode & 0o777, 0o600)

        # Discovery still reports the directory, and import must leave it alone.
        imported = projects.import_projects(projects_file, state)
        self.assertEqual(imported["added"], [])

        repeated = projects.ignore_project(projects_file, state, str(ignored))
        self.assertEqual(repeated["added"], [])
        self.assertEqual(repeated["removed"], [])
        self.assertIsNone(repeated["backup"])

    def test_ignore_names_a_directory_that_had_no_shortcut(self) -> None:
        target = self.directory("work/unlisted")
        projects_file = self.root / "projects.tsv"
        projects_file.write_text("", encoding="utf-8")
        projects_file.chmod(0o600)

        result = projects.ignore_project(
            projects_file, self.root / "state", str(target)
        )

        self.assertEqual(
            result["added"],
            [{"alias": "unlisted", "provider": "ignore", "cwd": str(target)}],
        )

    def test_add_validates_alias_and_never_replaces_an_existing_alias(self) -> None:
        cwd = self.directory("work/project")
        other = self.directory("work/other")
        projects_file = self.root / "projects.tsv"
        state = self.root / "state"

        added = projects.add_project(
            projects_file, state, "my-project", "codex", str(cwd)
        )
        self.assertEqual(added["added"][0]["cwd"], str(cwd))
        repeated = projects.add_project(
            projects_file, state, "my-project", "codex", str(cwd)
        )
        self.assertEqual(repeated["added"], [])
        with self.assertRaisesRegex(projects.ProjectError, "already exists"):
            projects.add_project(
                projects_file, state, "my-project", "claude", str(other)
            )
        with self.assertRaisesRegex(projects.ProjectError, "lowercase"):
            projects.add_project(
                projects_file, state, "Bad Alias", "claude", str(other)
            )

    def test_unsafe_provider_file_warns_and_other_provider_still_works(self) -> None:
        codex_project = self.directory("work/codex")
        target = self.root / "real-claude.json"
        self.private_json(target, {"projects": {str(codex_project): {}}})
        os.symlink(target, self.home / ".claude.json")
        (self.codex / "config.toml").write_text(
            f'[projects."{codex_project}"]\ntrust_level = "trusted"\n', encoding="utf-8"
        )

        result = projects.discover_projects(self.home)

        self.assertEqual(
            result["projects"], [{"provider": "codex", "cwd": str(codex_project)}]
        )
        self.assertTrue(
            any(
                "Claude Code project configuration" in warning
                for warning in result["warnings"]
            )
        )


if __name__ == "__main__":
    unittest.main()
