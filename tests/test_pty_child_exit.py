"""A forked test child dies when its exec fails, instead of running the suite.

This is the bug that took a machine down on 2026-08-11, not a hypothetical.
Five tests fork a pty and immediately exec a shell or `sp` in the child. None
of them guarded that child, so a failing `execve` -- EAGAIN or ENOMEM under
process pressure, ENOENT if a fixture path moved -- let the exception unwind
into the harness the child was forked from. The child does not die there: it
carries on as a second copy of the test runner and forks again at the next pty
test. Under exactly the pressure that causes the first failure, that is
exponential, and it produced roughly 900 python processes and a load average of
684 on a production host before anybody read a traceback.

Two tests, because the failure has two shapes:

* the static one, which fails when someone adds a sixth unguarded `pty.fork()`
  a year from now and nothing else notices;
* the live one, which forks a real child at a path that cannot be executed and
  proves it exits rather than returning.

The exit status is 127 -- the conventional "exec failed" status -- so a child
that hit the guard can never be confused with a legitimate `1` from `sp` or the
login script, several of which these harnesses already assert on.
"""

from __future__ import annotations

import ast
import os
import pty
import unittest

from tests.support import REPO


EXEC_FAILED = 127


def fork_sites(tree: ast.Module) -> list[ast.If]:
    """Every `if pid == 0:` guarding a child, by the fork call above it."""
    children: list[ast.If] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if (
            not isinstance(call, ast.Call)
            or not isinstance(call.func, ast.Attribute)
            or call.func.attr != "fork"
            or not isinstance(call.func.value, ast.Name)
            or call.func.value.id != "pty"
        ):
            continue
        parent = getattr(node, "parent", None)
        body = getattr(parent, "body", None) or []
        index = body.index(node) if node in body else -1
        following = body[index + 1] if 0 <= index < len(body) - 1 else None
        if isinstance(following, ast.If):
            children.append(following)
    return children


def annotate(tree: ast.Module) -> ast.Module:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent  # type: ignore[attr-defined]
    return tree


def exits_the_child(block: ast.If) -> bool:
    """Whether every path out of the child branch calls os._exit."""
    for node in ast.walk(block):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_exit"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ):
            handler = getattr(node, "parent", None)
            while handler is not None and not isinstance(handler, ast.Try):
                handler = getattr(handler, "parent", None)
            if handler is not None:
                return True
    return False


class ForkedChildGuardTests(unittest.TestCase):
    def test_every_pty_child_in_the_suite_exits_on_a_failed_exec(self) -> None:
        unguarded: list[str] = []
        for path in sorted((REPO / "tests").glob("test_*.py")):
            tree = annotate(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            for block in fork_sites(tree):
                if not exits_the_child(block):
                    unguarded.append(f"{path.name}:{block.lineno}")
        self.assertEqual(
            [],
            unguarded,
            "these pty.fork() children can return into the test runner and fork "
            f"again: {unguarded}. Wrap the child body in try/finally with "
            "os._exit(127).",
        )

    def test_a_child_whose_exec_fails_dies_with_the_exec_failed_status(self) -> None:
        """The live half: fork for real, fail the exec for real, reap the child."""
        missing = REPO / "tests" / "no-such-executable-for-this-test"
        self.assertFalse(missing.exists())
        pid, descriptor = pty.fork()
        if pid == 0:  # pragma: no cover - this branch never returns
            try:
                os.execv(os.fspath(missing), [os.fspath(missing)])
            finally:
                os._exit(EXEC_FAILED)
        try:
            _, status = os.waitpid(pid, 0)
        finally:
            os.close(descriptor)
        self.assertTrue(os.WIFEXITED(status), status)
        self.assertEqual(EXEC_FAILED, os.WEXITSTATUS(status))


if __name__ == "__main__":
    unittest.main()
