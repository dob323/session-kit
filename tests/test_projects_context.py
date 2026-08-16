"""Launch plans, startup approval, resume context, and the machine surface.

``sp new <project>`` is supposed to reproduce a setup, and entering a project
is supposed to show what is already going on there. These tests pin both, and
they pin the rule that keeps the first from becoming a way to run a command
out of a repository nobody read.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from lib.sessionkit_projects import cli, context, identity, launch

REPO = Path(__file__).resolve().parents[1]
MANIFEST = """
name = "demo-api"
description = "Demo API service"
provider = "codex"
account = "work"
model = "gpt-5-1-codex"
startup = "sp msg main demo-up"

[[team]]
role = "reviewer"
provider = "claude"
model = "claude-opus-5"
expertise = "review"
scope = "Review the diff."
"""


class ProjectFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-context.")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        self.repo = self.root / "repo"
        (self.repo / "sub").mkdir(parents=True)
        self.projects_file = self.root / "projects.tsv"
        self.projects_file.write_text(f"demo\tcodex\t{self.repo}\n", encoding="utf-8")

    def manifest(self, text: str = MANIFEST) -> None:
        (self.repo / identity.MANIFEST_NAME).write_text(text, encoding="utf-8")

    def project(self, alias: str = "demo") -> identity.Project:
        project = identity.Resolver(self.projects_file, environ={}).resolve_alias(alias)
        assert project is not None
        return project


class LaunchPlanTests(ProjectFixture):
    def test_a_trusted_manifest_supplies_provider_account_and_model(self) -> None:
        self.manifest()
        plan = launch.launch_plan(self.project(), state_dir=self.state)
        self.assertEqual(plan["provider"], "codex")
        self.assertEqual(plan["account"], "work")
        self.assertEqual(plan["model"], "gpt-5-1-codex")
        self.assertEqual(plan["decisions"]["model"], "manifest")
        # The only note is the unapproved startup command, covered below.
        self.assertEqual([note for note in plan["notes"] if "startup" not in note], [])

    def test_an_explicit_flag_beats_the_manifest(self) -> None:
        self.manifest()
        plan = launch.launch_plan(
            self.project(),
            requested_provider="claude",
            requested_model="claude-opus-5",
            state_dir=self.state,
        )
        self.assertEqual(plan["provider"], "claude")
        self.assertEqual(plan["model"], "claude-opus-5")
        self.assertEqual(plan["decisions"]["provider"], "flag")
        # Not overridden, so the manifest still supplies it.
        self.assertEqual(plan["account"], "work")
        self.assertEqual(plan["decisions"]["account"], "manifest")

    def test_without_a_manifest_the_shortcut_row_still_decides_the_provider(
        self,
    ) -> None:
        plan = launch.launch_plan(self.project(), state_dir=self.state)
        self.assertEqual(plan["provider"], "codex")
        self.assertEqual(plan["decisions"]["provider"], "shortcut")
        self.assertIsNone(plan["model"])
        self.assertEqual(plan["team"], [])

    def test_an_untrusted_manifest_is_listed_but_never_applied(self) -> None:
        self.manifest()
        self.projects_file.write_text("", encoding="utf-8")
        resolver = identity.Resolver(self.projects_file, environ={})
        project = resolver.resolve(self.repo)
        assert project is not None
        plan = launch.launch_plan(project, state_dir=self.state)
        self.assertFalse(plan["trusted"])
        self.assertIsNone(plan["model"])
        self.assertIsNone(plan["account"])
        self.assertEqual(plan["provider"], "shell")
        self.assertEqual(plan["decisions"]["provider"], "default")
        # The roles are still shown: a person should see what the repository
        # proposes even while the host declines to act on it.
        self.assertEqual([role["role"] for role in plan["team"]], ["reviewer"])
        self.assertFalse(plan["team_launchable"])
        self.assertTrue(
            any("not on the host's project list" in note for note in plan["notes"])
        )

    def test_a_shell_session_takes_neither_account_nor_model(self) -> None:
        self.manifest(MANIFEST.replace('provider = "codex"', 'provider = "shell"'))
        plan = launch.launch_plan(self.project(), state_dir=self.state)
        self.assertEqual(plan["provider"], "shell")
        self.assertIsNone(plan["account"])
        self.assertIsNone(plan["model"])
        self.assertEqual(
            [note for note in plan["notes"] if "startup" not in note],
            [
                "a shell session takes no account; the account was not applied",
                "a shell session takes no model; the model was not applied",
            ],
        )

    def test_an_unknown_requested_provider_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            launch.launch_plan(self.project(), requested_provider="gemini")

    def test_a_worktree_plan_names_the_repository_it_groups_under(self) -> None:
        worktree = self.root / "wt-feature"
        (worktree / "sub").mkdir(parents=True)
        (self.repo / ".git" / "worktrees" / "feature").mkdir(parents=True)
        (worktree / ".git").write_text(
            f"gitdir: {self.repo}/.git/worktrees/feature\n", encoding="utf-8"
        )
        (worktree / identity.MANIFEST_NAME).write_text(MANIFEST, encoding="utf-8")
        project = identity.Resolver(self.projects_file, environ={}).resolve(worktree)
        assert project is not None
        plan = launch.launch_plan(project, state_dir=self.state)
        self.assertTrue(plan["is_worktree"])
        self.assertEqual(plan["group_root"], os.fspath(self.repo))
        self.assertTrue(plan["trusted"])
        self.assertEqual(plan["model"], "gpt-5-1-codex")


class StartupApprovalTests(ProjectFixture):
    def test_an_unapproved_startup_command_never_runs(self) -> None:
        self.manifest()
        plan = launch.launch_plan(self.project(), state_dir=self.state)
        self.assertEqual(plan["startup"], "sp msg main demo-up")
        self.assertEqual(plan["startup_state"], launch.UNAPPROVED)
        self.assertFalse(plan["startup_applied"])

    def test_a_non_interactive_launch_says_why_it_skipped_the_command(self) -> None:
        self.manifest()
        plan = launch.launch_plan(
            self.project(), state_dir=self.state, interactive=False
        )
        self.assertTrue(any("not interactive" in note for note in plan["notes"]))

    def test_approval_is_by_exact_command_and_survives(self) -> None:
        self.manifest()
        project = self.project()
        launch.approve_startup(project.root, "sp msg main demo-up", self.state)
        plan = launch.launch_plan(self.project(), state_dir=self.state)
        self.assertEqual(plan["startup_state"], launch.APPROVED)
        self.assertTrue(plan["startup_applied"])
        self.assertEqual(plan["decisions"]["startup"], "manifest")

    def test_editing_the_command_withdraws_the_approval(self) -> None:
        self.manifest()
        project = self.project()
        launch.approve_startup(project.root, "sp msg main demo-up", self.state)
        self.manifest(MANIFEST.replace("sp msg main demo-up", "curl example.invalid"))
        plan = launch.launch_plan(self.project(), state_dir=self.state)
        self.assertEqual(plan["startup_state"], launch.CHANGED)
        self.assertFalse(plan["startup_applied"])

    def test_the_approval_record_is_owner_only(self) -> None:
        self.manifest()
        launch.approve_startup(self.project().root, "sp msg main demo-up", self.state)
        path = self.state / "projects" / launch.APPROVAL_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_group_or_world_writable_record_is_not_believed(self) -> None:
        self.manifest()
        project = self.project()
        launch.approve_startup(project.root, "sp msg main demo-up", self.state)
        path = self.state / "projects" / launch.APPROVAL_FILE
        path.chmod(0o666)
        self.assertEqual(
            launch.startup_state(project.root, "sp msg main demo-up", self.state),
            launch.UNAPPROVED,
        )

    def test_approving_a_second_project_keeps_the_first(self) -> None:
        other = self.root / "other"
        other.mkdir()
        launch.approve_startup("/a/one", "one", self.state)
        launch.approve_startup(os.fspath(other), "two", self.state)
        self.assertEqual(
            launch.startup_state("/a/one", "one", self.state), launch.APPROVED
        )
        self.assertEqual(
            launch.startup_state(os.fspath(other), "two", self.state), launch.APPROVED
        )

    def test_a_manifest_with_no_startup_has_no_startup_state(self) -> None:
        self.manifest(MANIFEST.replace('startup = "sp msg main demo-up"\n', ""))
        plan = launch.launch_plan(self.project(), state_dir=self.state)
        self.assertIsNone(plan["startup"])
        self.assertIsNone(plan["startup_state"])
        self.assertNotIn("startup", plan["decisions"])


def session(cwd: str, **overrides: object) -> dict[str, object]:
    row = {
        "shpool_id": "s1",
        "display_shpool_id": "s1",
        "provider": "claude",
        "cwd": cwd,
        "display_title": "a session",
        "agent_status": "idle",
        "needs_you": False,
        "started_at_unix_ms": 1,
    }
    row.update(overrides)
    return row


class ResumeContextTests(ProjectFixture):
    def test_context_gathers_sessions_under_the_project(self) -> None:
        self.manifest()
        project = self.project()
        result = context.project_context(
            project,
            sessions=[
                session(os.fspath(self.repo)),
                session(os.fspath(self.repo / "sub"), shpool_id="s2"),
                session(os.fspath(self.root / "elsewhere"), shpool_id="s3"),
            ],
        )
        self.assertEqual(result["counts"]["sessions"], 2)
        self.assertEqual(result["counts"]["team_roles"], 1)
        self.assertEqual(
            [row["shpool_id"] for row in result["sessions"]], ["s1", "s2"]
        )

    def test_a_context_view_carries_only_the_fields_it_declares(self) -> None:
        self.manifest()
        result = context.project_context(
            self.project(),
            sessions=[session(os.fspath(self.repo), secret="do not copy this")],
        )
        self.assertNotIn("secret", result["sessions"][0])
        self.assertEqual(result["sessions"][0]["project_root"], os.fspath(self.repo))

    def test_an_unreadable_store_is_named_rather_than_read_as_empty(self) -> None:
        self.manifest()
        result = context.project_context(
            self.project(), unavailable=["the session snapshot is unreadable"]
        )
        self.assertEqual(result["counts"]["sessions"], 0)
        self.assertEqual(result["unavailable"], ["the session snapshot is unreadable"])

    def test_a_worktree_counts_as_the_project_it_was_cut_from(self) -> None:
        worktree = self.root / "wt-feature"
        worktree.mkdir()
        (self.repo / ".git" / "worktrees" / "feature").mkdir(parents=True)
        (worktree / ".git").write_text(
            f"gitdir: {self.repo}/.git/worktrees/feature\n", encoding="utf-8"
        )
        self.manifest()
        (worktree / identity.MANIFEST_NAME).write_text(MANIFEST, encoding="utf-8")
        self.projects_file.write_text(
            f"demo\tcodex\t{self.repo}\ndemo-wt\tcodex\t{worktree}\n", encoding="utf-8"
        )
        resolver = identity.Resolver(self.projects_file, environ={})
        project = resolver.resolve_alias("demo")
        assert project is not None
        result = context.project_context(
            project,
            sessions=[session(os.fspath(worktree), shpool_id="s9")],
            group=resolver.projects(),
        )
        self.assertEqual(result["counts"]["worktrees"], 1)
        self.assertEqual(result["counts"]["sessions"], 1)
        self.assertEqual(result["sessions"][0]["project_root"], os.fspath(worktree))


class SessionGroupingTests(ProjectFixture):
    def test_sessions_bucket_by_project_and_keep_the_unplaceable_ones(self) -> None:
        self.manifest()
        loose = self.root / "loose"
        loose.mkdir()
        resolver = identity.Resolver(self.projects_file, environ={})
        rows = [
            session(os.fspath(self.repo)),
            session(os.fspath(self.repo / "sub"), shpool_id="s2"),
            session(os.fspath(loose), shpool_id="s3"),
            session("", shpool_id="s4"),
        ]
        assignments = resolver.assign(
            str(row["cwd"]) for row in rows if isinstance(row["cwd"], str)
        )
        grouped = context.group_sessions_by_project(rows, assignments)
        self.assertEqual(len(grouped["groups"]), 1)
        self.assertEqual(grouped["groups"][0]["name"], "demo-api")
        self.assertEqual(len(grouped["groups"][0]["sessions"]), 2)
        self.assertEqual(
            [row["shpool_id"] for row in grouped["ungrouped"]], ["s3", "s4"]
        )


class WorktreeLabelTests(ProjectFixture):
    """A materialised worktree is placed by the registry when its path cannot.

    `sp new --worktree` creates a directory that is on no shortcut list and may
    carry no manifest, so path resolution alone would drop its sessions into
    the unplaceable pile. The worktree registry annotates the row with the main
    repository it was cut from — the same path this module groups by.
    """

    def worktree_session(self, cwd: str, repo: str, **overrides: object) -> dict:
        return session(
            cwd, worktree={"branch": "feature/one", "repo": repo}, **overrides
        )

    def test_the_worktree_label_survives_the_field_whitelist(self) -> None:
        self.manifest()
        result = context.project_context(
            self.project(),
            sessions=[
                self.worktree_session(os.fspath(self.repo), os.fspath(self.repo))
            ],
        )
        self.assertEqual(
            result["sessions"][0]["worktree"],
            {"branch": "feature/one", "repo": os.fspath(self.repo)},
        )

    def test_a_worktree_session_off_the_project_path_still_counts(self) -> None:
        self.manifest()
        outside = self.root / "wt-elsewhere"
        outside.mkdir()
        result = context.project_context(
            self.project(),
            sessions=[self.worktree_session(os.fspath(outside), os.fspath(self.repo))],
        )
        self.assertEqual(result["counts"]["sessions"], 1)
        self.assertEqual(result["sessions"][0]["project_root"], os.fspath(self.repo))

    def test_a_worktree_of_another_repository_is_not_borrowed(self) -> None:
        self.manifest()
        outside = self.root / "wt-elsewhere"
        outside.mkdir()
        result = context.project_context(
            self.project(),
            sessions=[self.worktree_session(os.fspath(outside), "/some/other/repo")],
        )
        self.assertEqual(result["counts"]["sessions"], 0)

    def test_grouping_places_a_worktree_session_under_its_project(self) -> None:
        self.manifest()
        outside = self.root / "wt-elsewhere"
        outside.mkdir()
        resolver = identity.Resolver(self.projects_file, environ={})
        rows = [
            session(os.fspath(self.repo)),
            self.worktree_session(
                os.fspath(outside), os.fspath(self.repo), shpool_id="s2"
            ),
        ]
        assignments = resolver.assign(str(row["cwd"]) for row in rows)
        grouped = context.group_sessions_by_project(rows, assignments)
        self.assertEqual(len(grouped["groups"]), 1)
        self.assertEqual(grouped["ungrouped"], [])
        self.assertEqual(len(grouped["groups"][0]["sessions"]), 2)
        # The worktree's own root is not on the row, so it is attributed to the
        # project rather than to a root this cannot name.
        self.assertEqual(grouped["groups"][0]["roots"], [os.fspath(self.repo)])
        self.assertEqual(
            grouped["groups"][0]["sessions"][1]["project_root"], os.fspath(self.repo)
        )

    def test_a_project_worked_only_in_worktrees_still_groups(self) -> None:
        # The delegated case: every worker is in a worktree and no session runs
        # in the project directory itself. Placing these needs the host's
        # project list, not the projects that happened to resolve from the
        # session list — which here is none of them.
        self.manifest()
        resolver = identity.Resolver(self.projects_file, environ={})
        rows = []
        for index in range(3):
            worktree = self.root / f"wt-{index}"
            worktree.mkdir()
            rows.append(
                self.worktree_session(
                    os.fspath(worktree), os.fspath(self.repo), shpool_id=f"s{index}"
                )
            )
        assignments = resolver.assign(str(row["cwd"]) for row in rows)
        self.assertEqual(set(assignments.values()), {None})

        without_list = context.group_sessions_by_project(rows, assignments)
        self.assertEqual(len(without_list["ungrouped"]), 3)

        grouped = context.group_sessions_by_project(
            rows, assignments, projects=resolver.projects()
        )
        self.assertEqual(grouped["ungrouped"], [])
        self.assertEqual(len(grouped["groups"]), 1)
        self.assertEqual(len(grouped["groups"][0]["sessions"]), 3)
        self.assertEqual(grouped["groups"][0]["name"], "demo-api")

    def test_a_malformed_worktree_label_is_ignored_not_trusted(self) -> None:
        self.manifest()
        outside = self.root / "wt-elsewhere"
        outside.mkdir()
        for label in ({"repo": "relative/path"}, {"repo": ""}, {}, "not-a-table", None):
            with self.subTest(label=label):
                result = context.project_context(
                    self.project(),
                    sessions=[session(os.fspath(outside), worktree=label)],
                )
                self.assertEqual(result["counts"]["sessions"], 0)


class CommandLineTests(ProjectFixture):
    def run_cli(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                os.fspath(REPO / "lib" / "sessionkit_projects" / "cli.py"),
                "--projects-file",
                os.fspath(self.projects_file),
                "--state-dir",
                os.fspath(self.state),
                *argv,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_resolve_prints_the_project_as_json(self) -> None:
        self.manifest()
        completed = self.run_cli("resolve", os.fspath(self.repo / "sub"))
        self.assertEqual(completed.returncode, cli.EXIT_OK, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["project"]["root"], os.fspath(self.repo))
        self.assertEqual(value["project"]["name"], "demo-api")

    def test_resolve_reports_a_directory_in_no_project(self) -> None:
        completed = self.run_cli("resolve", os.fspath(self.root))
        self.assertEqual(completed.returncode, cli.EXIT_NO_PROJECT)
        self.assertIn("no project", completed.stderr)

    def test_launch_plan_by_alias_carries_the_manifest(self) -> None:
        self.manifest()
        completed = self.run_cli("launch-plan", "demo")
        self.assertEqual(completed.returncode, cli.EXIT_OK, completed.stderr)
        plan = json.loads(completed.stdout)
        self.assertEqual(plan["provider"], "codex")
        self.assertEqual(plan["model"], "gpt-5-1-codex")
        self.assertEqual(plan["startup_state"], launch.UNAPPROVED)

    def test_approve_startup_records_and_then_the_plan_applies_it(self) -> None:
        self.manifest()
        completed = self.run_cli("approve-startup", "demo")
        self.assertEqual(completed.returncode, cli.EXIT_OK, completed.stderr)
        plan = json.loads(self.run_cli("launch-plan", "demo").stdout)
        self.assertTrue(plan["startup_applied"])

    def test_approve_startup_refuses_a_digest_that_no_longer_matches(self) -> None:
        self.manifest()
        completed = self.run_cli("approve-startup", "demo", "--expect", "0" * 64)
        self.assertEqual(completed.returncode, cli.EXIT_USAGE)
        self.assertIn("changed since it was shown", completed.stderr)

    def test_approve_startup_refuses_an_untrusted_project(self) -> None:
        self.manifest()
        self.projects_file.write_text("", encoding="utf-8")
        completed = self.run_cli("approve-startup", os.fspath(self.repo))
        self.assertEqual(completed.returncode, cli.EXIT_USAGE)
        self.assertIn("projects add", completed.stderr)

    def test_check_validates_a_manifest_and_names_what_is_wrong(self) -> None:
        self.manifest()
        completed = self.run_cli("check", os.fspath(self.repo))
        self.assertEqual(completed.returncode, cli.EXIT_OK, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["valid"])
        self.manifest('name = "demo"\nbogus = "x"\n')
        completed = self.run_cli("check", os.fspath(self.repo))
        self.assertEqual(completed.returncode, cli.EXIT_MANIFEST)
        self.assertIn("bogus", completed.stderr)

    def test_list_reports_projects_and_their_groups(self) -> None:
        self.manifest()
        completed = self.run_cli("list")
        self.assertEqual(completed.returncode, cli.EXIT_OK, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual([row["name"] for row in value["projects"]], ["demo-api"])
        self.assertEqual(len(value["groups"]), 1)

    def test_context_reads_sessions_from_a_snapshot_file(self) -> None:
        self.manifest()
        snapshot = self.root / "snapshot.json"
        snapshot.write_text(
            json.dumps({"sessions": [session(os.fspath(self.repo / "sub"))]}),
            encoding="utf-8",
        )
        completed = self.run_cli(
            "context", "demo", "--snapshot", os.fspath(snapshot)
        )
        self.assertEqual(completed.returncode, cli.EXIT_OK, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["counts"]["sessions"], 1)
        self.assertEqual(value["project"]["name"], "demo-api")

    def test_context_names_an_unreadable_snapshot_instead_of_reporting_none(
        self,
    ) -> None:
        self.manifest()
        completed = self.run_cli(
            "context",
            "demo",
            "--snapshot",
            os.fspath(self.root / "absent.json"),
        )
        self.assertEqual(completed.returncode, cli.EXIT_OK, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["counts"]["sessions"], 0)
        self.assertTrue(any("unreadable" in note for note in value["unavailable"]))

    def test_group_sessions_places_a_worktree_session_from_the_host_list(self) -> None:
        self.manifest()
        worktree = self.root / "wt-one"
        worktree.mkdir()
        snapshot = self.root / "snapshot.json"
        snapshot.write_text(
            json.dumps(
                {
                    "sessions": [
                        session(
                            os.fspath(worktree),
                            worktree={"branch": "f/one", "repo": os.fspath(self.repo)},
                        )
                    ]
                }
            ),
            encoding="utf-8",
        )
        completed = self.run_cli("group-sessions", "--snapshot", os.fspath(snapshot))
        self.assertEqual(completed.returncode, cli.EXIT_OK, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["ungrouped"], [])
        self.assertEqual(len(value["groups"]), 1)
        self.assertEqual(value["groups"][0]["name"], "demo-api")

    def test_group_sessions_buckets_a_snapshot(self) -> None:
        self.manifest()
        snapshot = self.root / "snapshot.json"
        snapshot.write_text(
            json.dumps(
                {
                    "sessions": [
                        session(os.fspath(self.repo)),
                        session(os.fspath(self.root), shpool_id="s2"),
                    ]
                }
            ),
            encoding="utf-8",
        )
        completed = self.run_cli("group-sessions", "--snapshot", os.fspath(snapshot))
        self.assertEqual(completed.returncode, cli.EXIT_OK, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(len(value["groups"]), 1)
        self.assertEqual(len(value["ungrouped"]), 1)

    def test_a_bad_verb_is_a_usage_error(self) -> None:
        self.assertEqual(self.run_cli("nonsense").returncode, cli.EXIT_USAGE)


class ShellSeamTests(ProjectFixture):
    """`sp new <project>` reads its plan through one shell helper.

    These drive `sk_project_plan` exactly as `start_new` does, so the contract
    between the resolver and the shell is pinned on both sides.
    """

    def plan_line(self, *arguments: str) -> tuple[int, str]:
        script = (
            "set -uo pipefail\n"
            f"source {os.fspath(REPO / 'bin' / 'session_kit_common')}\n"
            'sk_project_plan "$@"\n'
        )
        completed = subprocess.run(
            ["bash", "-c", script, "sk_project_plan", *arguments],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "SESSION_KIT_PROJECTS_FILE": os.fspath(self.projects_file),
                "SESSION_KIT_STATE_DIR": os.fspath(self.state),
                "SESSION_KIT_RELEASE_DIR": os.fspath(REPO),
                # The seam never starts a daemon; this keeps the sourced file
                # from hunting for a real shpool on the developer's machine.
                "SESSION_KIT_SHPOOL_CMD": "/bin/true",
            },
        )
        return completed.returncode, completed.stdout.rstrip("\n")

    def test_the_manifest_decides_over_the_shortcut_row(self) -> None:
        self.manifest()
        code, line = self.plan_line("demo", "", "", "")
        self.assertEqual(code, 0)
        self.assertEqual(
            line.split("\t"),
            [os.fspath(self.repo), "codex", "work", "gpt-5-1-codex", launch.UNAPPROVED],
        )

    def test_flags_decide_over_the_manifest(self) -> None:
        self.manifest()
        code, line = self.plan_line("demo", "claude", "", "claude-opus-5")
        self.assertEqual(code, 0)
        fields = line.split("\t")
        self.assertEqual(fields[1], "claude")
        self.assertEqual(fields[3], "claude-opus-5")
        self.assertEqual(fields[2], "work")

    def test_an_untrusted_manifest_applies_nothing_through_the_seam(self) -> None:
        self.manifest()
        self.projects_file.write_text("", encoding="utf-8")
        code, line = self.plan_line(os.fspath(self.repo / "sub"), "", "", "")
        self.assertEqual(code, 0)
        self.assertEqual(line.split("\t"), [os.fspath(self.repo), "shell", "", "", ""])

    def test_a_missing_resolver_is_named_rather_than_blamed_on_the_alias(self) -> None:
        # Otherwise `sp new demo` reports a perfectly good alias as unknown
        # when what is actually missing is a file.
        script = (
            "set -uo pipefail\n"
            f"source {os.fspath(REPO / 'bin' / 'session_kit_common')}\n"
            'sk_project_plan "$@"\n'
        )
        completed = subprocess.run(
            ["bash", "-c", script, "sk_project_plan", "demo", "", "", ""],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "SESSION_KIT_PROJECTS_FILE": os.fspath(self.projects_file),
                "SESSION_KIT_STATE_DIR": os.fspath(self.state),
                "SESSION_KIT_RELEASE_DIR": os.fspath(REPO),
                "SESSION_KIT_PROJECTS_CORE": os.fspath(self.root / "absent.py"),
                "SESSION_KIT_SHPOOL_CMD": "/bin/true",
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("project resolver is missing", completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_an_unknown_project_fails_so_the_caller_stops(self) -> None:
        code, line = self.plan_line("absent", "", "", "")
        self.assertNotEqual(code, 0)
        self.assertEqual(line, "")

    def test_a_project_without_a_manifest_still_resolves_its_row(self) -> None:
        code, line = self.plan_line("demo", "", "", "")
        self.assertEqual(code, 0)
        self.assertEqual(line.split("\t")[:2], [os.fspath(self.repo), "codex"])


if __name__ == "__main__":
    unittest.main()
