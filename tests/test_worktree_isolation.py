"""Delegated work gets its own copy of the code, and gives it back on close.

Isolation used to be a flag somebody had to remember (`sp new --worktree`) and
cleanup a verb somebody had to type (`sp teardown`). Two workers in one
checkout edit the same files, and a worker that is never torn down leaves its
directory on the machine for good.

So: a machine session in a git repository is given a worktree without asking,
and the close gives it back. Everything below is about what that must never
do — remove unsaved work, remove unmerged commits, remove a directory somebody
is still standing in, remove the operator's own checkout, or remove any of the
copies that were already on this machine before the kit started managing them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from lib.sessionkit_inventory import worktrees
from tests.support import REPO, run
from tests.test_commands import CommandFixture, inventory_document, session_row

SP = REPO / "bin" / "sp"

# Every directory this suite makes -- fixture repositories, worktree roots, the
# worktrees themselves -- lives under here, and here is inside the checkout this
# agent owns. The suite creates and DELETES git worktrees, so "where can a path
# in this file point" is not a tidiness question: a sandbox that escapes is a
# sandbox that removes directories somewhere real. `sandbox()` is the only way
# this file makes a temporary directory, and `assert_contained` re-proves the
# containment at the moment each fixture is built.
SANDBOX_ROOT = Path(
    os.environ.get("SESSION_KIT_TEST_EXEC_ROOT", os.fspath(REPO))
).resolve()


def sandbox(prefix: str) -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix=prefix, dir=SANDBOX_ROOT)


def assert_contained(case: unittest.TestCase, *paths: Path | str) -> None:
    for path in paths:
        resolved = Path(path).resolve()
        case.assertTrue(
            resolved == SANDBOX_ROOT or SANDBOX_ROOT in resolved.parents,
            f"{resolved} is outside the suite's sandbox {SANDBOX_ROOT}",
        )

GIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    # Pinned so a replayed commit keeps its identity: the rebase case below
    # needs the commit it stops on to be the same object it started as, or the
    # test cannot tell "kept for the rebase" from "kept for being unmerged".
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **GIT_ENVIRONMENT},
    )
    return completed.stdout


class WorktreeReleaseTests(unittest.TestCase):
    """The core: what release removes, and everything it refuses to."""

    def setUp(self) -> None:
        self.temporary = sandbox("session-kit-worktrees.")
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.root = self.base / "worktree-root"
        self.environ = {"SESSION_KIT_WORKTREE_ROOT": os.fspath(self.root)}
        assert_contained(self, self.base, self.root)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main", "--quiet")
        (self.repo / "README").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "--quiet", "-m", "first")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def materialize(self, branch: str, *, auto: bool = True) -> dict:
        return worktrees.materialize(
            repo=self.repo,
            branch=branch,
            state_dir=self.state,
            environ=self.environ,
            auto=auto,
            origin="machine" if auto else "",
        )

    def release(self, **extra: object) -> dict:
        return worktrees.release(
            self.state, environ=self.environ, merged_into_ref="main", **extra
        )

    def fake_proc(self, pid: int, cwd: Path) -> Path:
        """A /proc with one process standing in ``cwd``."""
        proc = self.base / f"proc-{pid}"
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cwd").symlink_to(cwd)
        return proc

    # ---- the ordinary case ------------------------------------------------

    def test_a_clean_merged_copy_is_given_back_on_close(self) -> None:
        record = self.materialize("worker/one")
        worktrees.bind(state_dir=self.state, path=record["path"], shpool_id="7",
                       environ=self.environ)
        self.assertTrue(Path(record["path"]).is_dir())
        verdict = self.release(shpool_id="7")
        self.assertEqual("removed", verdict["action"], verdict)
        self.assertFalse(Path(record["path"]).exists())
        self.assertIsNone(
            worktrees.lookup(self.state, path=record["path"], environ=self.environ)
        )

    def test_the_branch_and_its_commits_outlive_the_directory(self) -> None:
        record = self.materialize("worker/keep-commits")
        worktrees.bind(state_dir=self.state, path=record["path"], shpool_id="8",
                       environ=self.environ)
        self.release(shpool_id="8")
        self.assertIn("worker/keep-commits", git(self.repo, "branch", "--list",
                                                 "worker/keep-commits"))

    # ---- the four refusals ------------------------------------------------

    def test_uncommitted_work_keeps_the_copy_and_says_so(self) -> None:
        record = self.materialize("worker/dirty")
        worktrees.bind(state_dir=self.state, path=record["path"], shpool_id="9",
                       environ=self.environ)
        (Path(record["path"]) / "unsaved.txt").write_text("half a thought\n",
                                                          encoding="utf-8")
        verdict = self.release(shpool_id="9")
        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("uncommitted or untracked", verdict["reason"])
        self.assertTrue((Path(record["path"]) / "unsaved.txt").is_file())

    def test_unmerged_commits_keep_the_copy_and_say_so(self) -> None:
        record = self.materialize("worker/unmerged")
        path = Path(record["path"])
        worktrees.bind(state_dir=self.state, path=path, shpool_id="10",
                       environ=self.environ)
        (path / "work.txt").write_text("finished\n", encoding="utf-8")
        git(path, "add", "work.txt")
        git(path, "commit", "--quiet", "-m", "the work")
        verdict = self.release(shpool_id="10")
        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("is not merged into main", verdict["reason"])
        self.assertTrue(path.is_dir())

    def test_a_live_session_in_the_copy_keeps_it_whatever_else_is_true(self) -> None:
        record = self.materialize("worker/busy")
        path = Path(record["path"])
        worktrees.bind(state_dir=self.state, path=path, shpool_id="11",
                       environ=self.environ)
        verdict = worktrees.release(
            self.state,
            shpool_id="11",
            environ=self.environ,
            merged_into_ref="main",
            proc=self.fake_proc(4242, path),
        )
        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("still working in it (pid 4242)", verdict["reason"])
        self.assertTrue(path.is_dir())

    def test_a_copy_nobody_asked_the_kit_for_is_never_removed(self) -> None:
        """The dozen worktrees already on a machine are not ours to collect."""
        record = self.materialize("worker/by-name", auto=False)
        worktrees.bind(state_dir=self.state, path=record["path"], shpool_id="12",
                       environ=self.environ)
        verdict = self.release(shpool_id="12")
        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("asked for by name", verdict["reason"])
        self.assertTrue(Path(record["path"]).is_dir())

    def test_a_session_with_no_worktree_is_a_quiet_no_op(self) -> None:
        verdict = self.release(shpool_id="404")
        self.assertEqual("none", verdict["action"], verdict)
        self.assertEqual("", worktrees.render_verdict(verdict))

    # ---- the operator's own checkout --------------------------------------

    def test_the_operators_own_checkout_is_refused_by_teardown(self) -> None:
        outside = self.base / "operators-own"
        outside.mkdir()
        (outside / "keep-me").write_text("mine\n", encoding="utf-8")
        record = self.materialize("worker/outside")
        registry = self.root / "registry" / f"{record['token']}.json"
        document = json.loads(registry.read_text(encoding="utf-8"))
        document["path"] = os.fspath(outside)
        registry.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.teardown(
                state_dir=self.state,
                path=outside,
                environ=self.environ,
                merged_into_ref="main",
                force=True,
            )
        self.assertIn("outside the kit's worktree root", str(refusal.exception))
        self.assertTrue((outside / "keep-me").is_file())
        self.assertEqual(
            "kept", self.release(path=outside)["action"]
        )

    def test_force_never_pulls_the_floor_out_from_a_live_session(self) -> None:
        record = self.materialize("worker/occupied")
        path = Path(record["path"])
        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.teardown(
                state_dir=self.state,
                path=path,
                environ=self.environ,
                merged_into_ref="main",
                force=True,
                proc=self.fake_proc(5150, path),
            )
        self.assertIn("still working in it", str(refusal.exception))
        self.assertTrue(path.is_dir())

    # ---- the sweep --------------------------------------------------------

    def test_the_sweep_releases_only_copies_whose_session_is_gone(self) -> None:
        live = self.materialize("worker/live")
        gone = self.materialize("worker/gone")
        by_name = self.materialize("worker/named", auto=False)
        for record, session in ((live, "20"), (gone, "21"), (by_name, "22")):
            worktrees.bind(state_dir=self.state, path=record["path"],
                           shpool_id=session, environ=self.environ)
        verdicts = worktrees.release_idle(
            self.state, ["20"], merged_into_ref="main", environ=self.environ
        )
        self.assertEqual(
            [("removed", gone["path"])],
            [(verdict["action"], verdict["path"]) for verdict in verdicts],
        )
        self.assertTrue(Path(live["path"]).is_dir())
        self.assertTrue(Path(by_name["path"]).is_dir())

    def test_a_copy_with_no_recorded_session_is_never_swept_on_age_alone(self) -> None:
        """Age is not ownership.

        A bind that failed leaves a *running* session in a copy that looks
        exactly like an abandoned one, and the launch path treats a failed bind
        as non-fatal on purpose. So an unowned copy is reported, never removed,
        however old it is.
        """
        fresh = self.materialize("worker/starting")
        for _ in range(2):
            verdicts = worktrees.release_idle(
                self.state, [], merged_into_ref="main", environ=self.environ
            )
            self.assertEqual(
                [("kept", fresh["path"])],
                [(verdict["action"], verdict["path"]) for verdict in verdicts],
            )
            self.assertIn("no session is recorded", verdicts[0]["reason"])
            self.assertTrue(Path(fresh["path"]).is_dir())

    def test_a_sweep_with_no_live_list_refuses_rather_than_assuming(self) -> None:
        """"I was not told what is alive" must never read as "nothing is"."""
        record = self.materialize("worker/live-elsewhere")
        worktrees.bind(state_dir=self.state, path=record["path"], shpool_id="30",
                       environ=self.environ)
        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.release_idle(
                self.state, None, merged_into_ref="main", environ=self.environ
            )
        self.assertIn("refusing to treat", str(refusal.exception))
        self.assertTrue(Path(record["path"]).is_dir())

    def test_every_copy_bound_to_one_session_is_answered_for(self) -> None:
        first = self.materialize("worker/twin-a")
        second = self.materialize("worker/twin-b")
        for record in (first, second):
            worktrees.bind(state_dir=self.state, path=record["path"],
                           shpool_id="31", environ=self.environ)
        verdict = self.release(shpool_id="31")
        self.assertEqual("many", verdict["action"], verdict)
        self.assertEqual(
            {"removed"}, {item["action"] for item in verdict["verdicts"]}
        )
        self.assertFalse(Path(first["path"]).exists())
        self.assertFalse(Path(second["path"]).exists())


class WorkIsMoreThanGitStatusTests(unittest.TestCase):
    """`git status` is not the question "is there work here".

    Every case below was clean to `git status --porcelain` and every one of
    them is somebody's work. The first two destroy it permanently: a commit
    that exists on no branch dies with the worktree's reflog, and a file the
    repository ignores — a report, a log, a screenshot, a `.env` — is exactly
    what an agent produces all day.
    """

    def setUp(self) -> None:
        self.temporary = sandbox("session-kit-work.")
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.root = self.base / "worktree-root"
        self.environ = {"SESSION_KIT_WORKTREE_ROOT": os.fspath(self.root)}
        assert_contained(self, self.base, self.root)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main", "--quiet")
        (self.repo / "README").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "--quiet", "-m", "first")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def copy_for(self, branch: str, session: str) -> Path:
        record = worktrees.materialize(
            repo=self.repo, branch=branch, state_dir=self.state,
            environ=self.environ, auto=True, origin="machine",
        )
        worktrees.bind(state_dir=self.state, path=record["path"],
                       shpool_id=session, environ=self.environ)
        return Path(record["path"])

    def release(self, session: str) -> dict:
        return worktrees.release(
            self.state, shpool_id=session, environ=self.environ,
            merged_into_ref="main",
        )

    def assert_clean_to_git(self, path: Path) -> None:
        self.assertEqual("", git(path, "status", "--porcelain").strip())

    def test_a_commit_on_a_detached_head_is_never_removed_with_its_copy(self) -> None:
        path = self.copy_for("worker/detached", "40")
        git(path, "checkout", "--quiet", "--detach")
        (path / "finished.txt").write_text("the work\n", encoding="utf-8")
        git(path, "add", "finished.txt")
        git(path, "commit", "--quiet", "-m", "work nobody else has")
        commit = git(path, "rev-parse", "HEAD").strip()
        self.assert_clean_to_git(path)
        self.assertEqual("", git(self.repo, "branch", "--contains", commit).strip())

        verdict = self.release("40")

        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn(commit[:12], verdict["reason"])
        self.assertIn("not merged into main", verdict["reason"])
        self.assertTrue(path.is_dir())
        self.assertEqual(commit, git(path, "rev-parse", "HEAD").strip())

    def test_files_the_repository_ignores_are_still_somebody_s_work(self) -> None:
        path = self.copy_for("worker/ignored", "41")
        (path / ".gitignore").write_text(
            "*.log\nproof/logs/\n.env\n/*-audit-report.md\n", encoding="utf-8"
        )
        git(path, "add", ".gitignore")
        git(path, "commit", "--quiet", "-m", "ignore rules")
        git(self.repo, "merge", "--quiet", "--ff-only", "worker/ignored")
        (path / "traffic-audit-report.md").write_text("six hours\n", encoding="utf-8")
        (path / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (path / "proof" / "logs").mkdir(parents=True)
        (path / "proof" / "logs" / "crawl.log").write_text("lines\n", encoding="utf-8")
        self.assert_clean_to_git(path)

        verdict = self.release("41")

        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("the repository ignores but somebody made", verdict["reason"])
        self.assertTrue((path / "traffic-audit-report.md").is_file())
        self.assertTrue((path / ".env").is_file())
        self.assertTrue((path / "proof" / "logs" / "crawl.log").is_file())

    def test_a_stashed_change_keeps_its_copy(self) -> None:
        path = self.copy_for("worker/stashed", "42")
        (path / "README").write_text("half a thought\n", encoding="utf-8")
        git(path, "stash", "push", "--quiet", "-m", "wip")
        self.assert_clean_to_git(path)

        verdict = self.release("42")

        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("stashed change", verdict["reason"])
        self.assertTrue(path.is_dir())

    def test_an_interrupted_merge_keeps_its_copy(self) -> None:
        path = self.copy_for("worker/merging", "43")
        git(self.repo, "branch", "side", "main")
        side = self.base / "side"
        git(self.repo, "worktree", "add", "--quiet", os.fspath(side), "side")
        (side / "side.txt").write_text("theirs\n", encoding="utf-8")
        git(side, "add", "side.txt")
        git(side, "commit", "--quiet", "-m", "side work")
        git(path, "merge", "--no-commit", "--no-ff", "-q", "side")
        self.assertTrue((Path(git(path, "rev-parse", "--absolute-git-dir").strip())
                         / "MERGE_HEAD").exists())

        verdict = self.release("43")

        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("a merge is in progress", verdict["reason"])
        self.assertTrue(path.is_dir())

    def test_an_interrupted_rebase_keeps_its_copy(self) -> None:
        path = self.copy_for("worker/rebasing", "44")
        (path / "one.txt").write_text("one\n", encoding="utf-8")
        git(path, "add", "one.txt")
        git(path, "commit", "--quiet", "-m", "one")
        git(self.repo, "merge", "--quiet", "--ff-only", "worker/rebasing")
        before = git(path, "rev-parse", "HEAD").strip()
        result = subprocess.run(
            ["git", "-C", os.fspath(path), "rebase", "--exec", "false", "HEAD~1"],
            capture_output=True, text=True, check=False,
            env={**os.environ, **GIT_ENVIRONMENT},
        )
        self.assertNotEqual(0, result.returncode, "the rebase must stop mid-way")
        self.assert_clean_to_git(path)
        # The replayed commit is the same object, so "unmerged" cannot be the
        # reason this copy survives, only the interrupted rebase can be.
        self.assertEqual(before, git(path, "rev-parse", "HEAD").strip())

        verdict = self.release("44")

        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("rebase is in progress", verdict["reason"])
        self.assertNotIn("not merged", verdict["reason"])
        self.assertTrue(path.is_dir())

    def test_a_checkout_that_is_not_the_recorded_one_is_never_removed(self) -> None:
        """A record outlives its directory; what is there now may be a person's."""
        path = self.copy_for("worker/original", "45")
        git(self.repo, "worktree", "remove", "--force", os.fspath(path))
        git(self.repo, "worktree", "add", "--quiet", "-b", "operator/manual",
            os.fspath(path), "main")
        (path / "mine.txt").write_text("the operator's\n", encoding="utf-8")
        git(path, "add", "mine.txt")
        git(path, "commit", "--quiet", "-m", "by hand")
        git(self.repo, "merge", "--quiet", "--ff-only", "operator/manual")

        verdict = self.release("45")

        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("not the recorded", verdict["reason"])
        self.assertTrue((path / "mine.txt").is_file())

    def test_force_does_not_override_a_commit_that_exists_nowhere_else(self) -> None:
        path = self.copy_for("worker/forced", "46")
        git(path, "checkout", "--quiet", "--detach")
        (path / "only.txt").write_text("only copy\n", encoding="utf-8")
        git(path, "add", "only.txt")
        git(path, "commit", "--quiet", "-m", "unreferenced")
        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.teardown(
                state_dir=self.state, path=path, environ=self.environ,
                merged_into_ref="main", force=True,
            )
        self.assertIn("whatever --force says", str(refusal.exception))
        self.assertTrue(path.is_dir())

    def test_every_directory_this_suite_can_name_is_inside_its_sandbox(self) -> None:
        """The suite deletes worktrees. Prove it can only delete its own.

        Read back, from the registry, every path this fixture has caused to
        exist -- the repository, the worktree root, the copies themselves --
        and require each one to be under the agent's own checkout. A fixture
        that escapes while these paths are being deleted is the work-loss bug
        happening tonight instead of after an install.
        """
        first = self.copy_for("worker/one", "90")
        second = self.copy_for("worker/two", "91")
        recorded = worktrees.records(self.state, self.environ)
        self.assertEqual(2, len(recorded), recorded)
        for record in recorded:
            assert_contained(self, record["path"], record["repo"])
        assert_contained(self, first, second, self.repo, self.state,
                         worktrees.worktree_root(self.state, self.environ))

    def test_a_copy_with_nothing_in_it_still_goes_back(self) -> None:
        """The guard has to stay narrow enough to actually collect a copy."""
        path = self.copy_for("worker/empty", "47")
        verdict = self.release("47")
        self.assertEqual("removed", verdict["action"], verdict)
        self.assertFalse(path.exists())

    # ---- stashes: three shapes, and git names two of them nothing ---------

    def test_a_stash_from_a_mixed_case_branch_keeps_its_copy(self) -> None:
        """Git writes the branch as spelled; a search must fold both sides.

        `On Feature/Work: …` against a search for `on Feature/Work:` in a
        line that has already been lower-cased matches nothing, so the copy
        was removed and the stash left with no directory that explains it.
        """
        path = self.copy_for("Feature/Work", "48")
        (path / "README").write_text("changed\n", encoding="utf-8")
        git(path, "stash", "push", "--quiet", "-m", "case-stash")
        self.assert_clean_to_git(path)
        self.assertIn("On Feature/Work:", git(self.repo, "stash", "list"))

        verdict = self.release("48")

        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("stashed change", verdict["reason"])
        self.assertTrue(path.is_dir())

    def test_a_stash_pushed_from_a_detached_head_keeps_its_copy(self) -> None:
        """Git records those as `On (no branch)`, which names nothing at all.

        The only thing that can attribute one is the commit it was made from,
        which is this copy's HEAD.
        """
        path = self.copy_for("worker/stash-detached", "49")
        git(path, "checkout", "--quiet", "--detach")
        head = git(path, "rev-parse", "HEAD").strip()
        (path / "README").write_text("mid-thought\n", encoding="utf-8")
        git(path, "stash", "push", "--quiet", "-m", "detached-stash")
        self.assert_clean_to_git(path)
        listing = git(self.repo, "stash", "list")
        self.assertIn("On (no branch):", listing)
        self.assertEqual(head, git(path, "rev-parse", "HEAD").strip())

        verdict = self.release("49")

        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("stashed change", verdict["reason"])
        self.assertTrue(path.is_dir())

    # ---- --force covers a decision, never an unrepeatable loss -----------

    def test_force_does_not_remove_a_detached_commit_the_check_could_not_place(
        self,
    ) -> None:
        """Detached and *unproved* is the rule, not detached and known-unmerged.

        A reference that does not exist makes `merge-base` exit 128, so the
        comparison produces an error rather than an answer. That error used to
        read as an ordinary refusal, which `--force` covers — and the forced
        removal takes the copy's reflog, the only thing that reaches the
        commit.
        """
        path = self.copy_for("worker/unplaceable", "50")
        git(path, "checkout", "--quiet", "--detach")
        (path / "only.txt").write_text("only copy\n", encoding="utf-8")
        git(path, "add", "only.txt")
        git(path, "commit", "--quiet", "-m", "unreferenced")
        commit = git(path, "rev-parse", "HEAD").strip()

        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.teardown(
                state_dir=self.state, path=path, environ=self.environ,
                merged_into_ref="refs/heads/no-such-reference", force=True,
            )

        self.assertIn("whatever --force says", str(refusal.exception))
        self.assertIn("not on a branch", str(refusal.exception))
        self.assertTrue(path.is_dir())
        self.assertEqual(
            commit, git(path, "rev-parse", "HEAD").strip(), "the commit is still there"
        )

    def test_force_does_not_destroy_a_half_finished_rebase(self) -> None:
        """The replay state lives only in the copy, and no branch holds it."""
        path = self.copy_for("worker/rebase-forced", "51")
        (path / "step.txt").write_text("one\n", encoding="utf-8")
        git(path, "add", "step.txt")
        git(path, "commit", "--quiet", "-m", "a step to replay")
        git(self.repo, "merge", "--quiet", "--ff-only", "worker/rebase-forced")
        before = git(path, "rev-parse", "HEAD").strip()
        completed = subprocess.run(
            ["git", "-C", os.fspath(path), "rebase", "--exec", "false", "HEAD~1"],
            capture_output=True, text=True, check=False,
            env={**os.environ, **GIT_ENVIRONMENT},
        )
        self.assertNotEqual(0, completed.returncode, "the rebase must stop mid-way")
        self.assert_clean_to_git(path)
        # The replayed commit is the same object, so the copy is still merged:
        # the interrupted rebase is the only thing that can be keeping it.
        self.assertEqual(before, git(path, "rev-parse", "HEAD").strip())

        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.teardown(
                state_dir=self.state, path=path, environ=self.environ,
                merged_into_ref="main", force=True,
            )

        self.assertIn("whatever --force says", str(refusal.exception))
        self.assertIn("rebase is in progress", str(refusal.exception))
        self.assertIn("rebase --abort", str(refusal.exception))
        self.assertTrue(path.is_dir())


class AQuestionGitCouldNotAnswerKeepsTheCopyTests(unittest.TestCase):
    """A check that could not run has not passed.

    Every guard in the module asks git to *prove* the work is safe. Each of
    those questions was asked without `check`, and each empty answer was then
    read as "there is nothing there" — so one failed `git rev-parse` switched
    off the identity check, the half-finished-operation check and the
    unmerged-commit check together, and the copy was removed in silence.

    The failures below are injected through the `runner` seam the module
    already takes for exactly this, so the copy underneath is a real one with
    real work in it and only the *answer* is missing.
    """

    def setUp(self) -> None:
        self.temporary = sandbox("session-kit-blind.")
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.root = self.base / "worktree-root"
        self.environ = {"SESSION_KIT_WORKTREE_ROOT": os.fspath(self.root)}
        assert_contained(self, self.base, self.root)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main", "--quiet")
        (self.repo / "README").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "--quiet", "-m", "first")
        record = worktrees.materialize(
            repo=self.repo, branch="worker/blind", state_dir=self.state,
            environ=self.environ, auto=True, origin="machine",
        )
        worktrees.bind(state_dir=self.state, path=record["path"],
                       shpool_id="60", environ=self.environ)
        self.path = Path(record["path"])
        assert_contained(self, self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def runner_that_cannot_answer(self, *failing: str):
        """Real git, except the named subcommand always fails."""
        def run(argv, **kwargs):  # noqa: ANN001 - a subprocess.run stand-in
            # argv is ["git", "-C", <cwd>, <subcommand>, ...]
            words = [str(value) for value in argv]
            for name in failing:
                wanted = name.split(" ")
                if words[3 : 3 + len(wanted)] == wanted:
                    return subprocess.CompletedProcess(
                        words, 128, "", f"fatal: simulated failure of {name}\n"
                    )
            return subprocess.run(argv, **kwargs)
        return run

    def release_with(self, runner) -> dict:  # noqa: ANN001
        return worktrees.release(
            self.state, shpool_id="60", environ=self.environ,
            merged_into_ref="main", runner=runner,
        )

    def test_a_head_git_cannot_read_keeps_the_copy(self) -> None:
        verdict = self.release_with(self.runner_that_cannot_answer("rev-parse HEAD"))
        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("which commit this copy is on", verdict["reason"])
        self.assertTrue(self.path.is_dir())

    def test_a_git_directory_git_cannot_name_keeps_the_copy(self) -> None:
        """Without it, a half-finished rebase in the copy is invisible."""
        verdict = self.release_with(
            self.runner_that_cannot_answer("rev-parse --absolute-git-dir")
        )
        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("half-finished merge or rebase", verdict["reason"])
        self.assertTrue(self.path.is_dir())

    def test_a_stash_list_that_failed_keeps_the_copy(self) -> None:
        verdict = self.release_with(self.runner_that_cannot_answer("stash list"))
        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("stash list could not be read", verdict["reason"])
        self.assertTrue(self.path.is_dir())

    def test_a_comparison_that_errored_keeps_the_copy(self) -> None:
        verdict = self.release_with(self.runner_that_cannot_answer("merge-base"))
        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("could not be compared", verdict["reason"])
        self.assertTrue(self.path.is_dir())

    def test_an_unreadable_process_table_keeps_the_copy(self) -> None:
        """`[]` from a `/proc` that could not be listed is not "nobody is here".

        That empty list is the only thing standing between a live session and
        having its directory removed underneath it.
        """
        missing = self.base / "no-such-proc"
        self.assertFalse(missing.exists())
        verdict = worktrees.release(
            self.state, shpool_id="60", environ=self.environ,
            merged_into_ref="main", proc=missing,
        )
        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("process table", verdict["reason"])
        self.assertTrue(self.path.is_dir())

    def test_a_registry_record_that_cannot_be_read_stops_the_release(self) -> None:
        """One session, two copies, one unreadable record.

        Concluding "this session holds exactly one copy" from a listing that
        silently dropped the other is how the other becomes permanent — the
        exact failure the many-copies branch exists to prevent.
        """
        second = worktrees.materialize(
            repo=self.repo, branch="worker/blind-two", state_dir=self.state,
            environ=self.environ, auto=True, origin="machine",
        )
        worktrees.bind(state_dir=self.state, path=second["path"],
                       shpool_id="60", environ=self.environ)
        damaged = self.root / "registry" / f"{second['token']}.json"
        damaged.write_text("{not json at all", encoding="utf-8")

        verdict = worktrees.release(
            self.state, shpool_id="60", environ=self.environ, merged_into_ref="main"
        )

        self.assertEqual("kept", verdict["action"], verdict)
        self.assertIn("could not be read", verdict["reason"])
        self.assertTrue(self.path.is_dir())
        self.assertTrue(Path(second["path"]).is_dir())


class OnlyOurOwnRegistrationIsEverForgottenTests(unittest.TestCase):
    """`git worktree prune` is repository-wide, and that is somebody else's work.

    A worktree's `.git/worktrees/<name>` directory holds its index — its
    staged-but-uncommitted content — its HEAD, its reflog and its rebase state.
    `git worktree prune` deletes that directory for *every* worktree of the
    repository whose own directory it cannot reach at that moment: one on a
    volume that is not mounted, one on a share that is briefly down, one
    somebody renamed and means to move back. The kit ran it for its own
    housekeeping, with `check=False`, inside a repository it was told never to
    touch beyond its own copy.
    """

    def setUp(self) -> None:
        self.temporary = sandbox("session-kit-prune.")
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.root = self.base / "worktree-root"
        self.environ = {"SESSION_KIT_WORKTREE_ROOT": os.fspath(self.root)}
        assert_contained(self, self.base, self.root)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main", "--quiet")
        (self.repo / "README").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "--quiet", "-m", "first")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def an_unreachable_worktree_of_the_operators(self) -> tuple[str, Path, Path]:
        """A hand-made worktree with staged work, whose directory is moved away."""
        theirs = self.base / "theirs"
        parked = self.base / "theirs-on-an-unmounted-volume"
        git(self.repo, "worktree", "add", "--quiet", "-b", "operator/manual",
            os.fspath(theirs), "main")
        (theirs / "staged.txt").write_text("hours of it\n", encoding="utf-8")
        git(theirs, "add", "staged.txt")
        registrations = self.repo / ".git" / "worktrees"
        name = next(
            entry.name for entry in registrations.iterdir()
            if entry.name != "" and (entry / "gitdir").is_file()
            and os.fspath(theirs) in (entry / "gitdir").read_text(encoding="utf-8")
        )
        theirs.rename(parked)
        self.assertFalse(theirs.exists())
        self.assertTrue((registrations / name).is_dir())
        return name, parked, registrations / name

    def test_giving_a_copy_back_leaves_another_worktrees_registration_alone(
        self,
    ) -> None:
        name, parked, registration = self.an_unreachable_worktree_of_the_operators()
        record = worktrees.materialize(
            repo=self.repo, branch="worker/ours", state_dir=self.state,
            environ=self.environ, auto=True, origin="machine",
        )
        worktrees.bind(state_dir=self.state, path=record["path"],
                       shpool_id="70", environ=self.environ)

        verdict = worktrees.release(
            self.state, shpool_id="70", environ=self.environ, merged_into_ref="main"
        )

        self.assertEqual("removed", verdict["action"], verdict)
        self.assertFalse(Path(record["path"]).exists(), "ours went")
        self.assertTrue(
            registration.is_dir(),
            f"the operator's registration {name} was pruned with ours",
        )
        self.assertTrue((registration / "index").is_file(), "their staged work")
        # And it still works: put the volume back and the staged file is there.
        parked.rename(self.base / "theirs")
        self.assertIn(
            "staged.txt",
            git(self.base / "theirs", "diff", "--cached", "--name-only"),
        )

    def test_rebuilding_a_copy_whose_directory_vanished_prunes_nothing_else(
        self,
    ) -> None:
        """The same prune ran on an ordinary retried launch, with no teardown."""
        name, parked, registration = self.an_unreachable_worktree_of_the_operators()
        record = worktrees.materialize(
            repo=self.repo, branch="worker/retried", state_dir=self.state,
            environ=self.environ, auto=True, origin="machine",
        )
        shutil.rmtree(record["path"])

        again = worktrees.materialize(
            repo=self.repo, branch="worker/retried", state_dir=self.state,
            environ=self.environ, auto=True, origin="machine",
        )

        self.assertTrue(Path(again["path"]).is_dir())
        self.assertTrue(
            registration.is_dir(),
            f"the operator's registration {name} was pruned by a relaunch",
        )
        self.assertTrue((registration / "index").is_file())


class ACheckoutThatIsTheRunningThingIsNeverCopiedTests(unittest.TestCase):
    """Some directories are served live. A copy of one is not isolation.

    Editing a copy of a checkout whose files are being served is work that
    never takes effect — and the repository would collect a branch per
    delegated session besides. A repository says so for itself with a marker
    file, or the host names it; either way nothing is written into it and
    nothing is ever removed outside the kit's own root.
    """

    def setUp(self) -> None:
        self.temporary = sandbox("session-kit-shared.")
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.root = self.base / "worktree-root"
        self.environ = {"SESSION_KIT_WORKTREE_ROOT": os.fspath(self.root)}
        assert_contained(self, self.base, self.root)
        self.repo = self.base / "live-site"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main", "--quiet")
        (self.repo / "index.php").write_text("<?php\n", encoding="utf-8")
        git(self.repo, "add", "index.php")
        git(self.repo, "commit", "--quiet", "-m", "the live site")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_untouched(self) -> None:
        self.assertEqual(
            [], git(self.repo, "branch", "--list", "sk/*").strip().splitlines()
        )
        self.assertFalse((self.repo / ".git" / "worktrees").exists())

    def test_a_big_checkout_is_not_copied_without_being_asked(self) -> None:
        """Four gigabytes inside `sp new` is a decision, not a default."""
        reason = worktrees.auto_copy_refusal(
            self.repo, {"SESSION_KIT_AUTO_WORKTREE_MAX_FILES": "0"}
        )
        self.assertEqual("", reason, "a limit of 0 turns the size check off")
        reason = worktrees.auto_copy_refusal(
            self.repo, {"SESSION_KIT_AUTO_WORKTREE_MAX_FILES": "1"}
        )
        self.assertEqual("", reason, "one tracked file is not over a limit of one")
        (self.repo / "second.php").write_text("<?php\n", encoding="utf-8")
        git(self.repo, "add", "second.php")
        git(self.repo, "commit", "--quiet", "-m", "second")
        reason = worktrees.auto_copy_refusal(
            self.repo, {"SESSION_KIT_AUTO_WORKTREE_MAX_FILES": "1"}
        )
        self.assertIn("more than the 1 this copies without asking", reason)
        # An explicit branch is still a decision, and is never refused for size.
        record = worktrees.materialize(
            repo=self.repo, branch="fix/by-hand", state_dir=self.state,
            environ={**self.environ, "SESSION_KIT_AUTO_WORKTREE_MAX_FILES": "1"},
        )
        self.assertTrue(Path(record["path"]).is_dir())

    def test_a_repository_under_a_system_root_is_refused_by_path_alone(self) -> None:
        """The one answer that needs nobody to have configured anything.

        The marker file and `SESSION_KIT_SHARED_REPOS` are both opt-in, so on a
        host where nobody set either — which is every host until somebody does
        — a checkout that *is* the running site was copyable. Where a
        repository lives is a fact about it, and `/srv`, `/var`, `/opt` and
        `/usr` are where a machine keeps software it runs.
        """
        for candidate in (
            "/srv/site", "/var/www/app", "/opt/thing", "/usr/local/src/x",
            "/etc/config", "/root/deploy", "/",
        ):
            with self.subTest(candidate=candidate):
                reason = worktrees.shared_checkout(candidate, {})
                self.assertNotEqual("", reason, candidate)
                self.assertIn("software it runs", reason)
        # Scratch space sits inside those roots and is not software anything
        # runs; refusing it would refuse every temporary fixture on the machine.
        for candidate in ("/tmp/work", "/var/tmp/work", "/run/user/1000/x"):
            with self.subTest(candidate=candidate):
                self.assertEqual("", worktrees.shared_checkout(candidate, {}))
        # A rule with no way out is a rule somebody works around.
        self.assertEqual(
            "",
            worktrees.shared_checkout(
                "/srv/site", {"SESSION_KIT_COPYABLE_REPOS": "/srv/site"}
            ),
        )

    def test_a_working_copy_is_never_built_under_a_system_root(self) -> None:
        """The other direction: not where it comes from, where it would land."""
        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.materialize(
                repo=self.repo, branch="sk/w-1-1", state_dir=self.state,
                environ={"SESSION_KIT_WORKTREE_ROOT": "/srv/session-kit-worktrees"},
                auto=True,
            )
        self.assertIn("/srv", str(refusal.exception))
        self.assertIn("software it runs", str(refusal.exception))
        self.assert_untouched()
        self.assertFalse(Path("/srv/session-kit-worktrees").exists())

    def test_a_marker_file_in_the_repository_refuses_the_copy(self) -> None:
        (self.repo / worktrees.SHARED_MARKER).write_text("", encoding="utf-8")
        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.materialize(
                repo=self.repo, branch="sk/w-1-1", state_dir=self.state,
                environ=self.environ, auto=True,
            )
        self.assertIn("shared checkout", str(refusal.exception))
        self.assert_untouched()

    def test_the_host_can_name_one_without_editing_the_repository(self) -> None:
        environ = {
            **self.environ,
            "SESSION_KIT_SHARED_REPOS": f"/nowhere:{self.repo}",
        }
        with self.assertRaises(worktrees.WorktreeError) as refusal:
            worktrees.materialize(
                repo=self.repo, branch="sk/w-1-1", state_dir=self.state,
                environ=environ, auto=True,
            )
        self.assertIn("SESSION_KIT_SHARED_REPOS", str(refusal.exception))
        self.assert_untouched()

    def test_a_directory_inside_a_named_checkout_is_covered_too(self) -> None:
        inside = self.repo / "httpdocs"
        inside.mkdir()
        self.assertIn(
            "SESSION_KIT_SHARED_REPOS",
            worktrees.shared_checkout(
                inside, {"SESSION_KIT_SHARED_REPOS": os.fspath(self.repo)}
            ),
        )

    def test_nothing_outside_the_kits_root_is_ever_removed(self) -> None:
        """Whatever a record claims, removal stops at the root boundary."""
        source = self.base / "repo"
        source.mkdir()
        git(source, "init", "--initial-branch=main", "--quiet")
        (source / "README").write_text("one\n", encoding="utf-8")
        git(source, "add", "README")
        git(source, "commit", "--quiet", "-m", "first")
        record = worktrees.materialize(
            repo=source, branch="worker/one", state_dir=self.state,
            environ=self.environ, auto=True,
        )
        registry = self.root / "registry" / f"{record['token']}.json"
        keep = self.repo / "index.php"
        for target in (self.repo, self.repo / "httpdocs"):
            document = json.loads(registry.read_text(encoding="utf-8"))
            document["path"] = os.fspath(target)
            registry.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(worktrees.WorktreeError) as refusal:
                worktrees.teardown(
                    state_dir=self.state, path=target, environ=self.environ,
                    merged_into_ref="main", force=True,
                )
            self.assertIn("outside the kit's worktree root", str(refusal.exception))
            verdict = worktrees.release(
                self.state, path=target, environ=self.environ, merged_into_ref="main"
            )
            self.assertEqual("kept", verdict["action"], verdict)
            self.assertTrue(keep.is_file())


class AReleasedCopyStillLeadsBackToItsRepositoryTests(unittest.TestCase):
    """A delegated session that closes cleanly must stay restorable.

    Its recorded directory is the copy, and a clean close is exactly the case
    where the copy is collected — so the most ordinary delegated session was
    also the one whose conversation could never be reopened: restore refuses a
    directory that does not exist, and no screen offers another one. The
    release leaves a tombstone naming the repository, and the restore reopens
    there instead of dead-ending.
    """

    UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    def setUp(self) -> None:
        self.fixture = CommandFixture()
        self.addCleanup(self.fixture.close)
        self.environ = {
            "SESSION_KIT_WORKTREE_ROOT": str(self.fixture.base / "worktrees")
        }
        self.repo = self.fixture.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main", "--quiet")
        (self.repo / "README").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "--quiet", "-m", "first")

    def released_copy(self) -> str:
        record = worktrees.materialize(
            repo=self.repo, branch="sk/w-1-1", state_dir=self.fixture.state,
            environ=self.environ, auto=True, origin="machine",
        )
        worktrees.bind(state_dir=self.fixture.state, path=record["path"],
                       shpool_id="worker", environ=self.environ)
        verdict = worktrees.release(
            self.fixture.state, shpool_id="worker", environ=self.environ,
            merged_into_ref="main",
        )
        self.assertEqual("removed", verdict["action"], verdict)
        self.assertFalse(Path(record["path"]).exists())
        return str(record["path"])

    def test_the_conversation_reopens_in_the_repository_the_copy_came_from(
        self,
    ) -> None:
        gone = self.released_copy()
        result = run(
            [SP, "restore-exact", "codex", self.UUID, gone],
            env={
                **self.fixture.env(),
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "2",
                "STUB_DYNAMIC_PROVIDER": "codex",
                "STUB_DYNAMIC_CWD": str(self.repo),
                "STUB_DYNAMIC_UUID": self.UUID,
            },
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("was given back when it closed", result.stdout)
        self.assertIn(str(self.repo), result.stdout)
        self.assertNotIn("not an existing absolute directory", result.stderr)

    def test_a_directory_that_was_never_a_copy_still_refuses(self) -> None:
        """The fallback is for a copy this kit gave back, not for any typo."""
        result = run(
            [SP, "restore-exact", "codex", self.UUID,
             str(self.fixture.base / "never-existed")],
            env={**self.fixture.env(), "SESSION_KIT_BACKGROUND": "1"},
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("not an existing absolute directory", result.stderr)


class TheSweepIsToldWhatIsAliveTests(unittest.TestCase):
    """The list of live sessions is read where it can be got wrong once.

    "Nothing is alive" and "I could not find out what is alive" produce the
    same empty list, and one of those two means remove every copy. The shell
    that used to turn a `shpool list --json` payload into `--active` flags had
    no way to tell them apart: an enumerator that failed printed nothing, and
    the next line substituted "there are none alive". The payload is now read
    by the verb that acts on it, which is a place a test can reach.
    """

    CORE = REPO / "lib" / "session_inventory.py"

    def setUp(self) -> None:
        self.temporary = sandbox("session-kit-sweep-list.")
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.root = self.base / "worktree-root"
        assert_contained(self, self.base, self.root)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main", "--quiet")
        (self.repo / "README").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "--quiet", "-m", "first")
        record = worktrees.materialize(
            repo=self.repo, branch="worker/swept", state_dir=self.state,
            environ={"SESSION_KIT_WORKTREE_ROOT": os.fspath(self.root)},
            auto=True, origin="machine",
        )
        worktrees.bind(
            state_dir=self.state, path=record["path"], shpool_id="live-one",
            environ={"SESSION_KIT_WORKTREE_ROOT": os.fspath(self.root)},
        )
        self.copy = Path(record["path"])
        assert_contained(self, self.copy)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def sweep(self, payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, os.fspath(self.CORE), "worktree", "sweep",
                "--active-stdin", "--merged-into", "main",
            ],
            input=payload, capture_output=True, text=True, check=False,
            cwd=os.fspath(REPO),
            env={
                **os.environ,
                "SESSION_KIT_STATE_DIR": os.fspath(self.state),
                "SESSION_KIT_WORKTREE_ROOT": os.fspath(self.root),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )

    def test_a_payload_that_cannot_be_read_sweeps_nothing(self) -> None:
        for payload in (
            "",
            "{not json",
            json.dumps({"sessions": "not a list"}),
            json.dumps({"sessions": [{"name": "live-one"}, "a stray row"]}),
            json.dumps(17),
        ):
            with self.subTest(payload=payload):
                result = self.sweep(payload)
                self.assertEqual(2, result.returncode, result.stdout)
                self.assertIn("could not be read", result.stderr)
                self.assertTrue(self.copy.is_dir(), "the copy was removed anyway")

    def test_a_payload_that_names_the_session_keeps_its_copy(self) -> None:
        result = self.sweep(json.dumps({"sessions": [{"name": "live-one"}]}))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout.strip())
        self.assertTrue(self.copy.is_dir())

    def test_a_payload_that_read_cleanly_and_held_nothing_is_none_alive(self) -> None:
        """The one case where an empty list really does mean nothing is alive."""
        result = self.sweep(json.dumps({"sessions": []}))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Removed the worker/swept working copy", result.stdout)
        self.assertFalse(self.copy.exists())


class DelegatedSessionsAreIsolatedByDefaultTests(unittest.TestCase):
    """`sp new` itself: who gets a copy without asking for one."""

    def setUp(self) -> None:
        self.fixture = CommandFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def worktree_calls(self) -> list[list[str]]:
        # Derived from the fixture's own directory rather than an attribute the
        # fixture only grew on this branch: a test whose *absence* of calls is
        # the point has to be able to run where the feature does not exist.
        log = Path(
            self.fixture.env().get(
                "STUB_WORKTREE_LOG", str(self.fixture.base / "worktree.log")
            )
        )
        if not log.exists():
            return []
        return [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def new_session(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return run(
            [SP, "new", "shell", "fixture", *argv],
            env={
                **self.fixture.env(),
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
            },
        )

    def test_a_delegated_session_is_given_its_own_copy_without_asking(self) -> None:
        started = self.new_session("--origin", "machine")
        materialize = [call for call in self.worktree_calls()
                       if call[1:2] == ["materialize"]]
        self.assertEqual(1, len(materialize), self.worktree_calls())
        self.assertIn("--auto", materialize[0])
        self.assertIn("--origin", materialize[0])
        self.assertIn("Its own copy of the code", started.stdout)
        self.assertIn("goes back when this session closes", started.stdout)

    def test_the_copy_is_bound_to_the_session_that_got_it(self) -> None:
        self.new_session("--origin", "machine")
        bound = [call for call in self.worktree_calls() if call[1:2] == ["bind"]]
        self.assertEqual(1, len(bound), self.worktree_calls())
        self.assertIn("--shpool-id", bound[0])

    def test_a_persons_own_session_is_left_in_the_directory_they_chose(self) -> None:
        self.new_session()
        self.assertEqual([], self.worktree_calls())

    def test_a_delegated_session_can_still_say_it_means_the_checkout(self) -> None:
        self.new_session("--origin", "machine", "--no-worktree")
        self.assertEqual([], self.worktree_calls())

    def test_a_shared_checkout_keeps_the_session_in_it_and_says_why(self) -> None:
        """A checkout that is the running thing is never copied."""
        started = run(
            [SP, "new", "shell", "fixture", "--origin", "machine"],
            env={
                **self.fixture.env(),
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_SHARED_REPO": str(self.fixture.project),
            },
        )
        self.assertIn("works in", started.stdout)
        self.assertIn(".session-kit-shared marker", started.stdout)
        self.assertEqual(
            [],
            [call for call in self.worktree_calls() if call[1:2] == ["materialize"]],
            self.worktree_calls(),
        )

    def test_a_copy_check_that_broke_keeps_the_session_in_the_checkout(self) -> None:
        """A guard that failed is not a guard that passed.

        `copy-check` answers in exit codes: 0 with a reason means do not copy
        this one, 1 means copying it is fine. Every other exit is the check
        itself failing, and reading "non-zero" as "go ahead" put a delegated
        session in a copy of a checkout that might be the running site — where
        its edits take effect nowhere and the verification reads the old page.
        """
        started = run(
            [SP, "new", "shell", "fixture", "--origin", "machine"],
            env={
                **self.fixture.env(),
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_COPY_CHECK_BROKEN": "1",
            },
        )
        self.assertIn("could not check whether", started.stdout)
        self.assertIn("works in it directly", started.stdout)
        self.assertIn("--worktree BRANCH", started.stdout)
        self.assertEqual(
            [],
            [call for call in self.worktree_calls() if call[1:2] == ["materialize"]],
            self.worktree_calls(),
        )

    def test_closing_a_session_gives_its_copy_back(self) -> None:
        """The close itself releases it. Nobody has to remember a teardown."""
        row = session_row("worker")
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(row)), encoding="utf-8"
        )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "worker",
                            "status": "Disconnected",
                            "started_at_unix_ms": row["started_at_unix_ms"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        closed = run(
            [SP, "close", "worker"],
            env={**self.fixture.env(), "SESSION_KIT_CONFIRM_ID": "worker"},
            check=False,
        )
        self.assertEqual(0, closed.returncode, closed.stderr)
        released = [call for call in self.worktree_calls()
                    if call[1:2] == ["release"]]
        self.assertEqual(1, len(released), self.worktree_calls())
        self.assertIn("--shpool-id", released[0])
        self.assertIn("worker", released[0])


if __name__ == "__main__":
    unittest.main()
