"""Worktree isolation: a recorded branch becomes a directory, and goes again.

The git here is real. A worktree store that passes against a mocked git proves
nothing about the two answers that matter — whether the branch is checked out
somewhere already, and whether it is merged — so every case below runs against
a repository built in a temporary directory, outside the checkout: a sandbox
under the repo would dirty `git status` and trip the installer's clean-tree
gate mid-run.

Two boundaries are stated rather than faked. `sp new --worktree` would start a
real provider session, so the shpool it talks to is the fixture's recorder and
the inventory it reads is the fixture's stub — but the worktree it materializes,
the directory the session is started in, and the record binding the two are all
real, and those are the facts under test.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.support import REPO, run
from tests.test_commands import CommandFixture, SP, write_executable

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor import worktrees  # noqa: E402


CORE = REPO / "lib" / "session_inventory.py"


def load_core(name: str):
    spec = importlib.util.spec_from_file_location(name, CORE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(repo), *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )
    return completed.stdout


def build_repository(root: Path) -> Path:
    repo = root / "project"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "config", "user.email", "tester@invalid.example")
    git(repo, "config", "user.name", "Session Kit Tests")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "--quiet", "--message", "first")
    return repo


class WorktreeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="worktree-store-")
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.repo = build_repository(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_materialize_creates_the_branch_directory_once(self) -> None:
        record = worktrees.materialize(
            repo=self.repo, branch="feature/one", state_dir=self.state, environ={}
        )
        path = Path(record["path"])
        self.assertTrue(record["created"])
        self.assertTrue(path.is_dir())
        self.assertEqual("feature/one", record["branch"])
        self.assertEqual(
            "feature/one", git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
        )
        registry = self.state / "worktrees" / "registry" / f"{record['token']}.json"
        self.assertEqual(0o600, registry.stat().st_mode & 0o777)
        again = worktrees.materialize(
            repo=self.repo, branch="feature/one", state_dir=self.state, environ={}
        )
        self.assertFalse(again["created"])
        self.assertEqual(record["path"], again["path"])

    def test_materialize_checks_out_a_branch_that_already_exists(self) -> None:
        git(self.repo, "branch", "already/there")
        head = git(self.repo, "rev-parse", "already/there").strip()
        record = worktrees.materialize(
            repo=self.repo, branch="already/there", state_dir=self.state, environ={}
        )
        self.assertEqual(
            head, git(Path(record["path"]), "rev-parse", "HEAD").strip()
        )

    def test_a_branch_checked_out_elsewhere_is_refused(self) -> None:
        elsewhere = self.base / "elsewhere"
        git(self.repo, "worktree", "add", "--quiet", os.fspath(elsewhere), "-b", "taken")
        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.materialize(
                repo=self.repo, branch="taken", state_dir=self.state, environ={}
            )
        self.assertIn("already checked out", str(refusal.exception))

    def test_a_directory_outside_a_repository_is_refused(self) -> None:
        outside = self.base / "plain"
        outside.mkdir()
        self.assertIsNone(worktrees.repository_root(outside))
        with self.assertRaises(worktrees.WorktreeError):
            worktrees.materialize(
                repo=outside, branch="feature/one", state_dir=self.state, environ={}
            )

    def test_a_request_from_inside_a_worktree_keys_the_main_repository(self) -> None:
        first = worktrees.materialize(
            repo=self.repo, branch="feature/first", state_dir=self.state, environ={}
        )
        # Asked from inside the worktree, not the project: worktrees are
        # siblings, so this has to resolve back to the main working tree or the
        # second branch would be keyed under the first worktree's path.
        self.assertEqual(
            self.repo, worktrees.repository_root(Path(first["path"]))
        )
        second = worktrees.materialize(
            repo=first["path"],
            branch="feature/second",
            state_dir=self.state,
            environ={},
        )
        self.assertEqual(os.fspath(self.repo), second["repo"])
        self.assertEqual(
            os.fspath(Path(first["path"]).parent),
            os.fspath(Path(second["path"]).parent),
        )

    def test_bind_and_labels_name_the_session_and_branch(self) -> None:
        record = worktrees.materialize(
            repo=self.repo, branch="feature/two", state_dir=self.state, environ={}
        )
        bound = worktrees.bind(
            state_dir=self.state,
            path=record["path"],
            shpool_id="main7",
            launch_key="worker:review:1",
        )
        self.assertEqual("main7", bound["shpool_id"])
        self.assertEqual("worker:review:1", bound["launch_key"])
        self.assertEqual(
            {
                "branch": "feature/two",
                "repo": os.fspath(self.repo),
                "path": record["path"],
            },
            worktrees.labels(self.state, {})[record["path"]],
        )
        found = worktrees.lookup(self.state, path=record["path"])
        assert found is not None
        self.assertEqual("main7", found["shpool_id"])

    def test_teardown_refuses_uncommitted_work(self) -> None:
        record = worktrees.materialize(
            repo=self.repo, branch="feature/dirty", state_dir=self.state, environ={}
        )
        (Path(record["path"]) / "scratch.txt").write_text("unsaved\n", encoding="utf-8")
        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.teardown(
                state_dir=self.state, path=record["path"], merged_into_ref="main"
            )
        self.assertIn("uncommitted or untracked", str(refusal.exception))
        self.assertTrue(Path(record["path"]).is_dir())

    def test_teardown_refuses_an_unmerged_branch_and_accepts_a_merged_one(self) -> None:
        record = worktrees.materialize(
            repo=self.repo, branch="feature/merge", state_dir=self.state, environ={}
        )
        tree = Path(record["path"])
        (tree / "work.txt").write_text("done\n", encoding="utf-8")
        git(tree, "add", "work.txt")
        git(tree, "commit", "--quiet", "--message", "work")
        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.teardown(
                state_dir=self.state, path=record["path"], merged_into_ref="main"
            )
        self.assertIn("not merged into main", str(refusal.exception))
        self.assertTrue(tree.is_dir())

        git(self.repo, "merge", "--quiet", "--no-ff", "-m", "merge", "feature/merge")
        result = worktrees.teardown(
            state_dir=self.state, path=record["path"], merged_into_ref="main"
        )
        self.assertTrue(result["removed"])
        self.assertTrue(result["merged"])
        self.assertFalse(tree.exists())
        self.assertIsNone(worktrees.lookup(self.state, path=record["path"]))
        # The directory goes; the work never does.
        self.assertIn(
            "feature/merge", git(self.repo, "branch", "--list", "feature/merge")
        )

    def test_force_removes_an_unmerged_worktree_but_keeps_the_branch(self) -> None:
        record = worktrees.materialize(
            repo=self.repo, branch="feature/forced", state_dir=self.state, environ={}
        )
        tree = Path(record["path"])
        (tree / "work.txt").write_text("done\n", encoding="utf-8")
        git(tree, "add", "work.txt")
        git(tree, "commit", "--quiet", "--message", "work")
        result = worktrees.teardown(
            state_dir=self.state,
            path=record["path"],
            merged_into_ref="main",
            force=True,
        )
        self.assertTrue(result["forced"])
        self.assertFalse(tree.exists())
        self.assertIn(
            "feature/forced", git(self.repo, "branch", "--list", "feature/forced")
        )

    def test_a_record_pointing_outside_the_root_is_never_removed(self) -> None:
        record = worktrees.materialize(
            repo=self.repo, branch="feature/outside", state_dir=self.state, environ={}
        )
        stranger = self.base / "not-ours"
        stranger.mkdir()
        registry = self.state / "worktrees" / "registry" / f"{record['token']}.json"
        document = json.loads(registry.read_text(encoding="utf-8"))
        document["path"] = os.fspath(stranger)
        registry.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.teardown(
                state_dir=self.state, path=os.fspath(stranger), merged_into_ref="main"
            )
        self.assertIn("outside the kit's worktree root", str(refusal.exception))
        self.assertTrue(stranger.is_dir())


class WorktreeCommandTests(unittest.TestCase):
    """The verbs a person and the launcher both call, through the real CLI."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="worktree-cli-")
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.repo = build_repository(self.base)
        self.environment = {
            **os.environ,
            "SESSION_KIT_STATE_DIR": os.fspath(self.state),
            "HOME": os.fspath(self.base),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def core(self, *arguments: str, check: bool = True):
        return run(
            [sys.executable, os.fspath(CORE), *arguments],
            env=self.environment,
            check=check,
        )

    def test_materialize_list_lookup_and_teardown(self) -> None:
        created = json.loads(
            self.core(
                "worktree", "materialize", "--repo", os.fspath(self.repo),
                "--branch", "cli/one",
            ).stdout
        )
        self.assertTrue(Path(created["path"]).is_dir())
        listed = json.loads(self.core("worktree", "list", "--json").stdout)
        self.assertEqual([created["path"]], [row["path"] for row in listed["worktrees"]])
        # The human form is the default; JSON is what a caller asks for.
        readable = self.core("worktree", "list").stdout
        self.assertIn("cli/one", readable)
        self.assertIn(created["path"], readable)
        found = json.loads(self.core("worktree", "lookup", "--path", created["path"]).stdout)
        self.assertEqual("cli/one", found["branch"])
        missing = self.core(
            "worktree", "lookup", "--path", os.fspath(self.base / "nowhere"),
            check=False,
        )
        self.assertEqual(2, missing.returncode)
        removed = json.loads(
            self.core(
                "worktree", "teardown", "--path", created["path"],
                "--merged-into", "main",
            ).stdout
        )
        self.assertTrue(removed["removed"])
        self.assertFalse(Path(created["path"]).exists())

    def test_teardown_refusal_states_the_reason_and_keeps_the_directory(self) -> None:
        created = json.loads(
            self.core(
                "worktree", "materialize", "--repo", os.fspath(self.repo),
                "--branch", "cli/two",
            ).stdout
        )
        tree = Path(created["path"])
        (tree / "work.txt").write_text("done\n", encoding="utf-8")
        git(tree, "add", "work.txt")
        git(tree, "commit", "--quiet", "--message", "work")
        refused = self.core(
            "worktree", "teardown", "--path", created["path"],
            "--merged-into", "main", check=False,
        )
        self.assertEqual(1, refused.returncode)
        self.assertIn("not merged into main", refused.stderr)
        self.assertTrue(tree.is_dir())


class WorktreeRowTests(unittest.TestCase):
    """A worktree a session runs in is on its row, or nobody can tell them apart."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="worktree-rows-")
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.repo = build_repository(self.base)
        self.record = worktrees.materialize(
            repo=self.repo, branch="rows/one", state_dir=self.state, environ={}
        )
        self.inventory_core = load_core("session_inventory_worktree_rows")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def inventory(self) -> dict:
        return {
            "schema_version": 1,
            "sessions": [
                {
                    "row": 1,
                    "terminal_number": 1,
                    "shpool_id": "main1",
                    "provider": "claude",
                    "display_provider": "claude",
                    "availability": "ready",
                    "title": "isolated worker",
                    "cwd": self.record["path"],
                    "agent_status": "working",
                    "identity": {"uuid": None, "confidence": "unknown"},
                    "subagents": [],
                },
                {
                    "row": 2,
                    "terminal_number": 2,
                    "shpool_id": "main2",
                    "provider": "claude",
                    "display_provider": "claude",
                    "availability": "ready",
                    "title": "plain session",
                    "cwd": os.fspath(self.repo),
                    "agent_status": "working",
                    "identity": {"uuid": None, "confidence": "unknown"},
                    "subagents": [],
                },
            ],
            "outside_agents": [],
        }

    def test_rows_are_labelled_from_the_registry_and_rendered(self) -> None:
        annotated = self.inventory_core._apply_worktree_labels(
            self.inventory(), {"state_dir": os.fspath(self.state)}
        )
        self.assertEqual(
            {
                "branch": "rows/one",
                "repo": os.fspath(self.repo),
                "path": self.record["path"],
            },
            annotated["sessions"][0]["worktree"],
        )
        self.assertNotIn("worktree", annotated["sessions"][1])
        rendered = self.inventory_core.render_inventory(annotated)
        self.assertIn("worktree rows/one", rendered)
        detail = self.inventory_core.render_detail(annotated, "1")
        self.assertIn("Worktree", detail)
        self.assertIn("rows/one", detail)

    def test_a_missing_registry_leaves_every_row_untouched(self) -> None:
        empty = self.base / "empty-state"
        empty.mkdir(mode=0o700)
        annotated = self.inventory_core._apply_worktree_labels(
            self.inventory(), {"state_dir": os.fspath(empty)}
        )
        self.assertTrue(all("worktree" not in row for row in annotated["sessions"]))


class PickerRowTests(unittest.TestCase):
    """The interactive picker's own row, rendered by its own module."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="worktree-picker-")
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def render(self, *, worktree: dict[str, str] | None) -> str:
        session = {
            "row": 1,
            "terminal_number": 1,
            "shpool_id": "main1",
            "shpool_id_raw": "main1",
            "display_shpool_id": "main1",
            "mutation_allowed": True,
            "shpool_shell": {"pid": 1001, "process_start_ticks": 10001},
            "started_at_unix_ms": 1_700_000_000_000,
            "shpool_status": "Disconnected",
            "availability": "ready",
            "provider": "claude",
            "display_provider": "claude",
            "identity": {
                "uuid": "00000000-0000-4000-8000-000000000001",
                "confidence": "exact",
            },
            "title": "isolated worker",
            "display_title": "isolated worker",
            "cwd": "/tmp/tree",
            "agent_status": "working",
            "needs_you": False,
            "subagents": [],
            "recovery": {"available": False},
            "diagnostics": [],
        }
        if worktree is not None:
            session["worktree"] = worktree
        view = self.base / "view.json"
        view.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-08-11T00:00:00Z",
                    "source": "live",
                    "stale": False,
                    "warnings": [],
                    "sessions": [session],
                    "outside_agents": [],
                }
            ),
            encoding="utf-8",
        )
        modules = REPO / "lib" / "sh"
        return run(
            [
                "bash",
                "-c",
                f'source "{modules}/shpool_login_theme.sh" 2>/dev/null;'
                f' source "{modules}/shpool_login_view.sh" 2>/dev/null;'
                f' source "{modules}/shpool_login_render.sh";'
                " render_page",
            ],
            env={
                **os.environ,
                "VIEW": os.fspath(view),
                "PAGE": "1",
                "PAGE_SIZE": "10",
                "PICKER_STYLE": "plain",
            },
        ).stdout

    def test_the_row_names_the_branch_a_worker_is_isolated_on(self) -> None:
        self.assertIn(
            "worktree rows/one",
            self.render(worktree={"branch": "rows/one", "repo": "/tmp/project"}),
        )

    def test_a_session_with_no_worktree_renders_exactly_as_before(self) -> None:
        self.assertNotIn("worktree", self.render(worktree=None))


class DelegatedWorkerIsolationTests(unittest.TestCase):
    """Delegated workers are isolated by default, and say so when they are not."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="worktree-delegate-")
        self.base = Path(self.temporary.name)
        self.repo = build_repository(self.base)
        self.inventory_core = load_core("session_inventory_worktree_delegate")
        self.launched = self.base / "launched.json"
        self.sp = self.base / "fake-sp"
        write_executable(
            self.sp,
            "#!/bin/sh\n"
            'printf \'{"cwd": "%s", "argv": "%s"}\\n\' "$(pwd -P)" "$*"'
            ' > "$SK_LAUNCH_MARKER"\n',
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assignment(self) -> dict:
        return {
            "branch": "worker/one",
            "provider": "claude",
            "requested_model": "claude-opus-test",
            "idempotency_key": "worker:one:1",
        }

    def test_branch_choice_states_every_answer_it_gives(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        self.assertEqual(
            "worker/one",
            self.inventory_core._worker_worktree_branch(
                self.assignment(), cwd=self.repo, environ={}
            ),
        )
        self.assertEqual(
            "",
            self.inventory_core._worker_worktree_branch(
                self.assignment(), cwd=plain, environ={}
            ),
        )
        self.assertEqual(
            "",
            self.inventory_core._worker_worktree_branch(
                self.assignment(),
                cwd=self.repo,
                environ={"SESSION_KIT_WORKER_WORKTREE": "0"},
            ),
        )
        self.assertEqual(
            "",
            self.inventory_core._worker_worktree_branch(
                {**self.assignment(), "branch": ""}, cwd=self.repo, environ={}
            ),
        )

    def test_an_impossible_delegation_is_refused_before_anything_is_reserved(
        self,
    ) -> None:
        state = self.base / "state"
        state.mkdir(mode=0o700)
        # Nothing recorded yet: the plan is launchable.
        self.inventory_core._refuse_unlaunchable_delegation(
            "a1b2c3d4", branches=["worker/one"], cwd=self.repo, state_dir=state
        )
        sys.path.insert(0, os.fspath(REPO / "lib"))
        from sessionkit_supervisor import receipts, worktrees

        elsewhere = self.base / "elsewhere"
        git(self.repo, "worktree", "add", "--quiet", os.fspath(elsewhere), "-b",
            "worker/one")
        planned = worktrees.preflight(
            repo=self.repo, branch="worker/one", state_dir=state, environ={}
        )
        self.assertEqual(os.fspath(elsewhere), planned["blocked_by"])
        with self.assertRaises(self.inventory_core.CollectionError) as blocked:
            self.inventory_core._refuse_unlaunchable_delegation(
                "a1b2c3d4", branches=["worker/one"], cwd=self.repo, state_dir=state
            )
        self.assertIn("already checked out", str(blocked.exception))

        receipts.set_cap(state_dir=state, msg_id="a1b2c3d4", max_usd_est=1.0)
        record = receipts.open_run(state_dir=state, msg_id="a1b2c3d4")
        receipts.record_spend(
            state_dir=state,
            receipt_id=record["receipt_id"],
            usd_est=1.0,
            source="supervisor lane",
        )
        with self.assertRaises(self.inventory_core.CollectionError) as capped:
            self.inventory_core._refuse_unlaunchable_delegation(
                "a1b2c3d4", branches=["worker/two"], cwd=self.repo, state_dir=state
            )
        self.assertIn("reached its cost cap", str(capped.exception))

    def test_the_launcher_hands_sp_new_the_worker_branch(self) -> None:
        environment = {
            **os.environ,
            "SESSION_KIT_SP_CMD": os.fspath(self.sp),
            "SK_LAUNCH_MARKER": os.fspath(self.launched),
        }
        result = self.inventory_core._launch_intake_worker(
            self.assignment(), cwd=self.repo, environ=environment
        )
        self.assertTrue(result["dispatched"])
        observed = json.loads(self.launched.read_text(encoding="utf-8"))
        self.assertIn("--worktree worker/one", observed["argv"])
        self.assertEqual(os.fspath(self.repo), observed["cwd"])

    def test_an_operator_can_turn_worker_isolation_off(self) -> None:
        environment = {
            **os.environ,
            "SESSION_KIT_SP_CMD": os.fspath(self.sp),
            "SK_LAUNCH_MARKER": os.fspath(self.launched),
            "SESSION_KIT_WORKER_WORKTREE": "0",
        }
        self.inventory_core._launch_intake_worker(
            self.assignment(), cwd=self.repo, environ=environment
        )
        observed = json.loads(self.launched.read_text(encoding="utf-8"))
        self.assertNotIn("--worktree", observed["argv"])


class SpNewWorktreeTests(unittest.TestCase):
    """`sp new --worktree` starts the session in the branch's own directory."""

    def setUp(self) -> None:
        self.fixture = CommandFixture()
        # The fixture's stub core answers the inventory verbs; the worktree
        # verbs under test go to the installed core unchanged.
        self.stub_core = self.fixture.base / "stub-inventory"
        self.fixture.fake_core.replace(self.stub_core)
        write_executable(
            self.fixture.fake_core,
            "#!/usr/bin/env python3\n"
            "import os, subprocess, sys\n"
            "arguments = sys.argv[1:]\n"
            "core = (\n"
            "    os.environ['REAL_INVENTORY_CORE']\n"
            "    if arguments and arguments[0] in ('worktree', 'receipt')\n"
            "    else os.environ['STUB_INVENTORY_CORE']\n"
            ")\n"
            "raise SystemExit(\n"
            "    subprocess.run([sys.executable, core, *arguments], check=False).returncode\n"
            ")\n",
        )
        git(self.fixture.project, "init", "--quiet", "--initial-branch=main")
        git(self.fixture.project, "config", "user.email", "tester@invalid.example")
        git(self.fixture.project, "config", "user.name", "Session Kit Tests")
        git(self.fixture.project, "config", "commit.gpgsign", "false")
        (self.fixture.project / "README.md").write_text("start\n", encoding="utf-8")
        git(self.fixture.project, "add", "README.md")
        git(self.fixture.project, "commit", "--quiet", "--message", "first")

    def tearDown(self) -> None:
        self.fixture.close()

    def environment(self) -> dict[str, str]:
        return {
            **os.environ,
            **self.fixture.env(),
            "REAL_INVENTORY_CORE": os.fspath(CORE),
            "STUB_INVENTORY_CORE": os.fspath(self.stub_core),
            "SESSION_KIT_BACKGROUND": "1",
            "STUB_DYNAMIC_PROVIDER": "shell",
        }

    def test_the_session_starts_in_the_worktree_and_the_record_binds_it(self) -> None:
        started = run(
            [SP, "new", "shell", "--worktree", "feature/isolated"],
            cwd=self.fixture.project,
            env=self.environment(),
        )
        self.assertIn("Isolated on branch feature/isolated", started.stdout)
        listed = json.loads(
            run(
                [sys.executable, os.fspath(CORE), "worktree", "list", "--json"],
                env=self.environment(),
            ).stdout
        )
        self.assertEqual(1, len(listed["worktrees"]))
        record = listed["worktrees"][0]
        self.assertEqual("feature/isolated", record["branch"])
        self.assertTrue(Path(record["path"]).is_dir())
        self.assertTrue(record["shpool_id"])
        # The session runs inside the worktree, not the project: this is the
        # whole promise of the flag, and the kit's own start record states it.
        self.assertIn(f"Starting a shell session in {record['path']}", started.stdout)
        # The bound id is the session shpool actually created, so the row the
        # picker labels is this worker's row and not some other session's.
        state = json.loads(self.fixture.shpool_state.read_text(encoding="utf-8"))
        self.assertEqual(
            [record["shpool_id"]], [row["name"] for row in state["sessions"]]
        )

    def test_teardown_closes_the_worker_and_prunes_the_merged_worktree(self) -> None:
        run(
            [SP, "new", "shell", "--worktree", "feature/done"],
            cwd=self.fixture.project,
            env=self.environment(),
        )
        record = json.loads(
            run(
                [sys.executable, os.fspath(CORE), "worktree", "list", "--json"],
                env=self.environment(),
            ).stdout
        )["worktrees"][0]
        tree = Path(record["path"])
        (tree / "work.txt").write_text("done\n", encoding="utf-8")
        git(tree, "add", "work.txt")
        git(tree, "commit", "--quiet", "--message", "work")
        git(self.fixture.project, "merge", "--quiet", "--no-ff", "-m", "merge",
            "feature/done")
        environment = {
            **self.environment(),
            "STUB_DYNAMIC_CWD": record["path"],
            "SESSION_KIT_CONFIRM_ID": record["shpool_id"],
        }
        torn = run([SP, "teardown", record["shpool_id"]], env=environment)
        self.assertIn("pruned the feature/done worktree", torn.stdout)
        self.assertFalse(tree.exists())
        state = json.loads(self.fixture.shpool_state.read_text(encoding="utf-8"))
        self.assertEqual([], state["sessions"])
        listed = json.loads(
            run(
                [sys.executable, os.fspath(CORE), "worktree", "list", "--json"],
                env=self.environment(),
            ).stdout
        )
        self.assertEqual([], listed["worktrees"])

    def test_teardown_keeps_an_unmerged_worktree_after_closing_the_worker(self) -> None:
        run(
            [SP, "new", "shell", "--worktree", "feature/open"],
            cwd=self.fixture.project,
            env=self.environment(),
        )
        record = json.loads(
            run(
                [sys.executable, os.fspath(CORE), "worktree", "list", "--json"],
                env=self.environment(),
            ).stdout
        )["worktrees"][0]
        tree = Path(record["path"])
        (tree / "work.txt").write_text("in progress\n", encoding="utf-8")
        git(tree, "add", "work.txt")
        git(tree, "commit", "--quiet", "--message", "work")
        environment = {
            **self.environment(),
            "STUB_DYNAMIC_CWD": record["path"],
            "SESSION_KIT_CONFIRM_ID": record["shpool_id"],
        }
        kept = run(
            [SP, "teardown", record["shpool_id"]], env=environment, check=False
        )
        self.assertNotEqual(0, kept.returncode)
        self.assertIn("not merged into HEAD", kept.stderr)
        self.assertTrue(tree.is_dir())

    def test_teardown_refuses_a_session_that_is_not_isolated(self) -> None:
        run([SP, "new", "shell", "fixture"], env=self.environment())
        state = json.loads(self.fixture.shpool_state.read_text(encoding="utf-8"))
        identifier = state["sessions"][0]["name"]
        environment = {
            **self.environment(),
            "STUB_DYNAMIC_CWD": os.fspath(self.fixture.project),
            "SESSION_KIT_CONFIRM_ID": identifier,
        }
        refused = run([SP, "teardown", identifier], env=environment, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("does not run in a Session Kit worktree", refused.stderr)
        state = json.loads(self.fixture.shpool_state.read_text(encoding="utf-8"))
        self.assertEqual(1, len(state["sessions"]))

    def test_a_project_outside_a_repository_creates_no_session(self) -> None:
        # Outside the checkout on purpose: a directory under it belongs to this
        # repository, and the launcher would isolate against Session Kit itself.
        with tempfile.TemporaryDirectory(prefix="worktree-plain-") as plain:
            refused = run(
                [SP, "new", "shell", "--worktree", "feature/nope"],
                cwd=Path(plain),
                env=self.environment(),
                check=False,
            )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("no worktree for branch feature/nope", refused.stderr)
        state = json.loads(self.fixture.shpool_state.read_text(encoding="utf-8"))
        self.assertEqual([], state["sessions"])


if __name__ == "__main__":
    unittest.main()
