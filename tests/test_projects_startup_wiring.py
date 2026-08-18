"""A startup command a person can review and approve.

`sp new` reads a project's launch plan and refuses to run a startup command
nobody approved. The verbs that review and approve one existed, in
lib/sessionkit_projects/cli.py, but `session-kit projects` dispatched only to
the project-list tool, so no command a person could type reached them. An
unapproved command could therefore never become approved, and the notice `sp
new` printed pointed at nothing.

Both halves are tested here: the verbs reach the manifest tool, and the notice
names the commands that do the work.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.support import REPO, run

SESSION_KIT = REPO / "bin" / "session-kit"
SP = REPO / "bin" / "sp"

MANIFEST = """
name = "demo-api"
provider = "codex"
startup = "sp list"
"""


class ProjectsDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".projects-wiring-", dir=REPO)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.install = self.root / "lib" / "session-kit"
        self.install.mkdir(parents=True)
        # An installed release is a link to a tree; this checkout is that tree.
        (self.install / "current").symlink_to(REPO)
        self.config = self.root / "config"
        self.config.mkdir()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "session-kit.toml").write_text(MANIFEST, encoding="utf-8")
        (self.config / "projects.tsv").write_text(
            f"demo\tcodex\t{self.repo}\n", encoding="utf-8"
        )

    def session_kit(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [os.fspath(SESSION_KIT), *argv],
            env={
                **os.environ,
                "HOME": os.fspath(self.root),
                "SESSION_KIT_ROOT": os.fspath(self.install),
                "SESSION_KIT_CONFIG_ROOT": os.fspath(self.config),
                "SESSION_KIT_STATE_DIR": os.fspath(self.state),
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def test_launch_plan_is_reachable_and_says_what_would_start(self) -> None:
        shown = self.session_kit("projects", "launch-plan", "demo")
        self.assertEqual(0, shown.returncode, shown.stderr)
        plan = json.loads(shown.stdout)
        self.assertEqual(os.fspath(self.repo), plan["root"])
        self.assertEqual("codex", plan["provider"])
        self.assertEqual("unapproved", plan["startup_state"])

    def test_approving_a_startup_command_makes_the_plan_apply_it(self) -> None:
        approved = self.session_kit("projects", "approve-startup", "demo")
        self.assertEqual(0, approved.returncode, approved.stderr)
        self.assertTrue(json.loads(approved.stdout)["approved"])
        plan = json.loads(self.session_kit("projects", "launch-plan", "demo").stdout)
        self.assertTrue(plan["startup_applied"])
        self.assertEqual("approved", plan["startup_state"])

    def test_the_project_list_verbs_still_reach_the_list_tool(self) -> None:
        listed = self.session_kit("projects", "list")
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertIn("demo", listed.stdout)

    def test_a_manifest_can_be_checked_before_it_is_trusted(self) -> None:
        checked = self.session_kit("projects", "check", os.fspath(self.repo))
        self.assertEqual(0, checked.returncode, checked.stderr)

    def test_the_help_names_the_verbs_that_exist(self) -> None:
        helped = self.session_kit("help")
        self.assertIn("session-kit projects launch-plan", helped.stdout)
        self.assertIn("session-kit projects approve-startup", helped.stdout)


MODEL_ONLY_MANIFEST = """
name = "demo-api"
provider = "codex"
model = "gpt-5-1-codex"
"""


class ProjectPlanFieldsTests(unittest.TestCase):
    """A project that names a model but no account still launches.

    The plan arrives as one tab-separated line, and a tab is IFS whitespace: a
    run of them reads as ONE separator, so the empty account field vanished and
    every field after it shifted left. The model landed in the account
    variable and `sp new <project>` refused itself with "--account is available
    only for Claude or Codex with an enrolled alias", a flag nobody typed.
    """

    def setUp(self) -> None:
        from tests.test_commands import CommandFixture

        self.fixture = CommandFixture()
        self.repo = self.fixture.base / "model-only-project"
        self.repo.mkdir()
        (self.repo / "session-kit.toml").write_text(
            MODEL_ONLY_MANIFEST, encoding="utf-8"
        )
        self.fixture.projects.write_text(
            f"demo\tcodex\t{self.repo}\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.fixture.close()

    def env(self) -> dict[str, str]:
        return {
            **self.fixture.env(),
            "SESSION_KIT_PROJECTS_CORE": os.fspath(
                REPO / "lib" / "sessionkit_projects" / "cli.py"
            ),
            "STUB_DYNAMIC_PROVIDER": "codex",
            "STUB_DYNAMIC_CWD": os.fspath(self.repo),
            "STUB_DYNAMIC_UUID": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
            "SESSION_KIT_BACKGROUND": "1",
        }

    def test_a_model_without_an_account_is_not_read_as_an_account(self) -> None:
        started = run([SP, "new", "codex", "demo"], env=self.env(), check=False)
        self.assertEqual(0, started.returncode, started.stdout + started.stderr)
        self.assertNotIn("--account", started.stderr)
        self.assertNotIn("enrolled alias", started.stderr)

    def test_the_model_the_project_names_reaches_the_launch_record(self) -> None:
        run([SP, "new", "codex", "demo"], env=self.env(), check=False)
        launches = sorted(self.fixture.start.glob("*.launch"))
        self.assertEqual(1, len(launches), launches)
        fields = launches[0].read_text(encoding="utf-8").rstrip("\n").split("\t")
        self.assertEqual("codex", fields[0])
        self.assertEqual(os.fspath(self.repo), fields[1])
        self.assertEqual("gpt-5-1-codex", fields[2])
        # No account was named, so no account record was written.
        self.assertEqual([], sorted(self.fixture.start.glob("*.account")))


class StartupNoticeTests(unittest.TestCase):
    """The notice `sp new` prints names commands that work."""

    def setUp(self) -> None:
        from tests.test_commands import CommandFixture

        self.fixture = CommandFixture()
        self.repo = self.fixture.base / "manifest-project"
        self.repo.mkdir()
        (self.repo / "session-kit.toml").write_text(MANIFEST, encoding="utf-8")
        self.fixture.projects.write_text(
            f"demo\tcodex\t{self.repo}\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.fixture.close()

    def test_an_unapproved_startup_command_points_at_the_real_verbs(self) -> None:
        env = {
            **self.fixture.env(),
            "SESSION_KIT_PROJECTS_CORE": os.fspath(
                REPO / "lib" / "sessionkit_projects" / "cli.py"
            ),
            "STUB_DYNAMIC_PROVIDER": "codex",
            "STUB_DYNAMIC_CWD": os.fspath(self.repo),
            "STUB_DYNAMIC_UUID": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
            "SESSION_KIT_BACKGROUND": "1",
        }
        started = run([SP, "new", "codex", "demo"], env=env, check=False)
        self.assertIn(
            "This project's startup command is not approved here, so it was not run.",
            started.stdout,
        )
        self.assertIn(
            "Review it with: session-kit projects launch-plan demo", started.stdout
        )
        self.assertIn(
            "Approve it with: session-kit projects approve-startup demo",
            started.stdout,
        )


if __name__ == "__main__":
    unittest.main()
