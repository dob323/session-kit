"""The sweep has to know every name the suite can leave behind.

`tests/sweep_sandboxes.py` deletes directories and kills processes, so it is
told what a sandbox looks like by two things and nothing else: a set of
prefixes read out of the test sources, and the random characters tempfile puts
after one. It used to be told by a list six entries long, written by hand
against a suite that hands tempfile over two hundred prefixes. The list was not
wrong when it was written. It went wrong the next time a fixture picked a name,
and it went wrong quietly: on 2026-08-19 the checkout held four stale
sandboxes and the list named two, so `.codex-fallback-` and `.watchdog-` stayed
where they were, and so would anything still running inside them.

This file is the part that cannot go quietly stale. It walks the test sources
itself, with its own parse rather than the sweep's, and fails when a fixture
starts making a sandbox under a name the sweep would not recognise. The other
tests hold the safety end: what the sweep must refuse to remove even when a
prefix says otherwise.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.support import REPO

from tests import sweep_sandboxes


TESTS = REPO / "tests"
# tempfile draws eight of these after the prefix. Any run of them is enough to
# assert on; the sweep does not depend on the count.
A_RANDOM_PART = "a1b2c3d4"


def sandbox_calls() -> list[tuple[Path, ast.Module, ast.Call]]:
    """Every call in the suite that makes a directory tempfile names.

    Deliberately a second, independent walk. A guard that asked the sweep what
    it found would agree with the sweep by construction and prove nothing. Each
    call is returned with the tree it came from, because the enclosing function
    is found by identity and only holds within one parse.
    """
    found = []
    for path in sorted(TESTS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = ""
            if isinstance(function, ast.Attribute):
                name = function.attr
            elif isinstance(function, ast.Name):
                name = function.id
            if name in {"TemporaryDirectory", "mkdtemp"}:
                found.append((path, tree, node))
    return found


def prefix_expression(node: ast.Call) -> ast.expr | None:
    for entry in node.keywords:
        if entry.arg == "prefix":
            return entry.value
    # suffix comes first in both signatures, so a positional prefix is second.
    return node.args[1] if len(node.args) > 1 else None


def enclosing_parameters(tree: ast.Module, node: ast.Call) -> list[str]:
    """The parameter names of the function `node` sits in."""
    for parent in ast.walk(tree):
        if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(child is node for child in ast.walk(parent)):
            return [argument.arg for argument in parent.args.args]
    return []


class TheSweepKnowsEveryNameTheSuiteCanMakeTests(unittest.TestCase):
    # Derived once: it parses every module under tests/, and nothing here
    # changes what it reads.
    prefixes: frozenset[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.prefixes = sweep_sandboxes.sandbox_prefixes()

    def test_every_literal_prefix_in_the_suite_reached_the_sweep(self) -> None:
        missing = []
        for path, _tree, node in sandbox_calls():
            expression = prefix_expression(node)
            if not isinstance(expression, ast.Constant):
                continue
            if not isinstance(expression.value, str) or not expression.value:
                continue
            if expression.value not in self.prefixes:
                missing.append(
                    f"{path.relative_to(REPO)}:{node.lineno} {expression.value!r}"
                )
        self.assertEqual(
            [],
            missing,
            "tests/sweep_sandboxes.py cannot see these sandbox prefixes, so an "
            "interrupted run leaves them and anything running inside them:\n  "
            + "\n  ".join(missing),
        )

    def test_no_fixture_hides_its_prefix_behind_an_expression(self) -> None:
        """A name the sources do not spell out is a name the sweep cannot learn.

        One forwarding helper is allowed and used -- `sandbox(prefix)` in
        tests/test_worktree_isolation.py -- because its callers still spell the
        literal. Anything else (an f-string, a module constant, a value built at
        runtime) has to change, not be added to a list here.

        A call with no prefix at all is fine only while it also has no `dir=`:
        tempfile then works in the system temp directory, outside the checkout
        and outside this sweep. Ask for a directory inside the checkout and the
        name has to be one the sweep can recognise, because `tmpXXXXXXXX` is not
        a name anything can safely claim at a repository root.
        """
        unreadable = []
        for path, tree, node in sandbox_calls():
            where = f"{path.relative_to(REPO)}:{node.lineno}"
            expression = prefix_expression(node)
            if expression is None:
                if any(entry.arg == "dir" for entry in node.keywords):
                    unreadable.append(f"{where} (asks for dir= with no prefix)")
                continue
            if isinstance(expression, ast.Constant):
                continue
            parameters = enclosing_parameters(tree, node)
            if isinstance(expression, ast.Name) and expression.id in parameters:
                continue
            unreadable.append(f"{where} (prefix is {ast.unparse(expression)})")
        self.assertEqual(
            [],
            unreadable,
            "these sandboxes are named by something no parse can read; give "
            "tempfile a string literal, or route the call through a helper that "
            "takes the literal from its callers:\n  " + "\n  ".join(unreadable),
        )

    def test_the_derived_set_is_the_whole_suite_and_not_a_remnant(self) -> None:
        """A scan that quietly returned almost nothing would look like success.

        The floor is far below the count today; it exists to fail loudly if the
        parse ever stops finding call sites, which is precisely the shape of the
        bug that made the hand-written list useless.
        """
        self.assertGreater(len(self.prefixes), 100)
        for known in (".session-kit-test-", ".sandbox-guard-", ".watchdog-"):
            self.assertIn(known, self.prefixes)

    def test_a_prefix_only_matches_with_tempfiles_random_part_attached(self) -> None:
        self.assertTrue(
            sweep_sandboxes.is_a_sandbox_name(
                ".watchdog-" + A_RANDOM_PART, self.prefixes
            )
        )
        # The bare prefix, a hand-written suffix, and a path separator are all
        # names tempfile cannot produce.
        for name in (".watchdog-", ".watchdog-NOTES", ".watchdog-my-notes"):
            self.assertFalse(sweep_sandboxes.is_a_sandbox_name(name, self.prefixes))


class TheSweepRefusesToTouchTheRepositoryTests(unittest.TestCase):
    def tracked_root_entries(self) -> set[str]:
        listed = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.split()
        return {name.split("/")[0] for name in listed}

    def test_no_derived_prefix_can_claim_source(self) -> None:
        prefixes = sweep_sandboxes.sandbox_prefixes()
        claimed = [
            f"{entry} claimed by {prefix!r}"
            for entry in self.tracked_root_entries() | {".git"}
            for prefix in prefixes
            if entry.startswith(prefix)
        ]
        self.assertEqual([], claimed, "\n  ".join(claimed))

    def test_protected_names_survive_a_prefix_that_would_claim_them(self) -> None:
        """The keep-list is checked before any prefix, so widening cannot reach it.

        `.gi` is not a prefix any fixture uses; it is the shortest thing that
        would swallow `.git` and `.gitignore` by name shape alone, which is what
        makes it the right probe for whether the keep-list is load-bearing.
        """
        poisoned = frozenset({".gi", ".shellcheckr", ".githu"})
        for protected in sorted(sweep_sandboxes.NEVER_REMOVE):
            self.assertFalse(
                sweep_sandboxes.is_a_sandbox_name(protected, poisoned),
                f"{protected} was not protected",
            )
        # The probe is only meaningful if it really would have matched.
        self.assertTrue(sweep_sandboxes.is_a_sandbox_name(".gitxyz", poisoned))

    def test_the_keep_list_is_not_the_copy_ignore_rule(self) -> None:
        """Two rules, opposite requirements for `.git`, kept apart on purpose.

        tests.support.SOURCE_ROOT_DOTTED omits `.git` because a COPY of the tree
        must not carry history. This sweep DELETES, so it has to protect the one
        entry that rule drops. Sharing a constant would put those a single edit
        from each other.
        """
        from tests.support import SOURCE_ROOT_DOTTED

        self.assertIn(".git", sweep_sandboxes.NEVER_REMOVE)
        self.assertNotIn(".git", SOURCE_ROOT_DOTTED)
        self.assertIsNot(sweep_sandboxes.NEVER_REMOVE, SOURCE_ROOT_DOTTED)


# A checkout the sweep can be pointed at without risking this one: the same
# layout (a `tests/` directory beside the root it will scan), a fixture module
# naming a prefix that appears nowhere in the real suite, and decoys. `.gi` is
# in there so the copied sweep really does derive a prefix that would swallow
# `.git`, and the keep-list is the only thing that stops it.
FIXTURE_MODULE = """
import tempfile

from somewhere import REPO


def build():
    return tempfile.TemporaryDirectory(prefix=".brand-new-fixture-", dir=REPO)


def poison():
    return tempfile.mkdtemp(prefix=".gi", dir=REPO)
"""

THREE_HOURS = 3 * 3600


class TheSweepLearnsANewPrefixWithoutBeingToldTests(unittest.TestCase):
    """End to end, against a copy of the sweep in a checkout of its own."""

    def setUp(self) -> None:
        # Same filesystem as the code under test, like every other fixture here.
        self.temporary = tempfile.TemporaryDirectory(prefix=".sweep-fixture-", dir=REPO)
        self.root = Path(self.temporary.name)
        (self.root / "tests").mkdir()
        (self.root / "tests" / "sweep_sandboxes.py").write_text(
            (REPO / "tests" / "sweep_sandboxes.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (self.root / "tests" / "test_fixture.py").write_text(
            FIXTURE_MODULE, encoding="utf-8"
        )
        self.addCleanup(self.temporary.cleanup)

    def make(self, name: str, *, age: int = 0) -> Path:
        path = self.root / name
        path.mkdir()
        (path / "contents").write_text("evidence\n", encoding="utf-8")
        if age:
            stamp = path.stat().st_mtime - age
            os.utime(path, (stamp, stamp))
        return path

    def sweep(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                os.fspath(self.root / "tests" / "sweep_sandboxes.py"),
                *arguments,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def test_a_prefix_no_one_added_to_the_sweep_is_still_swept(self) -> None:
        stale = self.make(".brand-new-fixture-7x2qab19", age=THREE_HOURS)
        fresh = self.make(".brand-new-fixture-0000zzzz")
        unknown = self.make(".not-a-fixture-7x2qab19", age=THREE_HOURS)

        listed = self.sweep("--list")

        self.assertIn(".brand-new-fixture-7x2qab19", listed.stdout)
        self.assertNotIn(".not-a-fixture", listed.stdout)
        self.assertTrue(stale.is_dir(), "--list must remove nothing")

        removed = self.sweep()

        self.assertIn(
            "removed stale sandbox .brand-new-fixture-7x2qab19", removed.stdout
        )
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.is_dir(), "a sandbox inside the grace period is live")
        self.assertTrue(unknown.is_dir(), "an unknown name is somebody else's")

    def test_the_repository_survives_a_prefix_that_would_claim_it(self) -> None:
        git_dir = self.make(".git", age=THREE_HOURS)
        ignore = self.make(".gitignore", age=THREE_HOURS)
        claimed = self.make(".gitxyz1234", age=THREE_HOURS)

        removed = self.sweep()

        self.assertTrue(git_dir.is_dir())
        self.assertTrue(ignore.is_dir())
        # The decoy proves the fixture's `.gi` prefix really did reach the
        # sweep, so the two survivals above are the keep-list and not a miss.
        self.assertFalse(claimed.exists())
        self.assertIn(".gitxyz1234", removed.stdout)


if __name__ == "__main__":
    unittest.main()
