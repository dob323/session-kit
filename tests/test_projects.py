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
                ("shared-claude", "claude", str(shared)),
                ("shared-codex", "codex", str(shared)),
            },
        )
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertIn("# keep this comment", projects_file.read_text(encoding="utf-8"))
        self.assertEqual(projects_file.stat().st_mode & 0o777, 0o600)

        repeated = projects.import_projects(projects_file, state)
        self.assertEqual(repeated["added"], [])
        self.assertIsNone(repeated["backup"])

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
