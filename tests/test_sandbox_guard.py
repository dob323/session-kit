"""The suite may not reach the machine's real session manager. Proof.

Every test here fails on a tree without ``tests/sandbox_guard.py`` armed, and
that is the point: the first one drives the real provider-exit path from the
real bashrc and measures where ``shpool`` resolved. Without the guard it
resolves to the real binary and the fixture's ``shpool detach`` -- carrying
``SHPOOL_SESSION_NAME=main2`` -- goes to a live daemon on its default socket.

Read ``tests/sandbox_guard.py`` for why PATH is the only lever: the hand-back
in ``bashrc/shpool.bashrc`` runs ``command shpool detach``, hardcoded on
purpose, so ``SESSION_KIT_SHPOOL_CMD`` cannot redirect it.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests import sandbox_guard
from tests.support import REPO
from tests.test_lifecycle_shell import BASHRC, ProviderExitShellHarness


def bashrc_path_line() -> str:
    """The bashrc's own PATH prepend, quoted rather than modelled.

    A test that rebuilt this line by hand would prove something about the copy.
    Reading it out of the file under test keeps the measurement honest, and
    :meth:`PathLineTests.test_the_bashrc_still_prepends_the_home_directories`
    fails if the line ever moves.
    """
    for line in BASHRC.read_text(encoding="utf-8").splitlines():
        if line.startswith("export PATH="):
            return line
    raise AssertionError(f"no `export PATH=` line in {BASHRC}")


def inside_the_test_sandbox(path: str) -> bool:
    real = os.path.realpath(path)
    root = os.path.realpath(os.fspath(REPO))
    return real == root or real.startswith(root + os.sep)


class PathLineTests(unittest.TestCase):
    def test_the_bashrc_still_prepends_the_home_directories(self) -> None:
        # If this line stops prepending $HOME directories, PATH stops being a
        # lever a fixture can use and the guard needs rethinking, not patching.
        line = bashrc_path_line()
        self.assertIn('$HOME/.cargo/bin', line)
        self.assertIn('$HOME/.local/bin', line)
        self.assertTrue(line.rstrip('"').endswith("$PATH"), line)


class RealSessionManagerIsOutOfReachTests(ProviderExitShellHarness):
    """The differential. Fails on an unguarded tree, passes on a guarded one."""

    def resolved_shpool(self) -> str:
        """Where ``shpool`` resolves for the session shell this fixture builds.

        Same environment the harness hands the bashrc, same PATH prepend the
        bashrc performs, same lookup ``command shpool`` performs. What comes
        back is the file the hand-back would have executed.
        """
        completed = subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                f"{bashrc_path_line()}\ncommand -v shpool || true",
            ],
            env=self.environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
        return completed.stdout.strip()

    def test_the_session_shell_resolves_shpool_inside_the_sandbox(self) -> None:
        resolved = self.resolved_shpool()
        self.assertTrue(
            resolved and inside_the_test_sandbox(resolved),
            "this fixture's session shell would run a shpool outside the test "
            f"sandbox: {resolved!r}. That binary talks to the live daemon on "
            "its default socket, and the fixture carries SHPOOL_SESSION_NAME="
            "main2.",
        )

    def test_the_hand_back_to_the_picker_is_refused_not_delivered(self) -> None:
        """The real path, end to end: crash, refused reopen, hand back.

        This is the shape of ``test_a_reopen_that_is_refused_lands_in_a_shell``
        in tests/test_lifecycle_shell.py -- the one path that reaches
        ``__sk_detach_to_picker`` -- with the shpool call read back afterwards.
        """
        log = sandbox_guard.refusal_log()
        before = log.stat().st_size if log.exists() else 0

        self.crashing_provider()
        completed = self.launch("exit\n")

        self.assertEqual(0, completed.returncode, completed.stderr)
        # The fallback line only prints when the detach FAILED, which is what a
        # refusal looks like from the shell's side. On an unguarded tree this
        # line prints only because no live session happens to be called main2:
        # detach a session that does exist and it succeeds, and an operator's
        # window is thrown back to the picker mid-work.
        self.assertIn("Shell opened", completed.stdout)

        refused: list[str] = []
        if log.exists():
            with log.open(encoding="utf-8") as handle:
                handle.seek(before)
                refused = [line for line in handle.read().splitlines() if line]
        self.assertIn(
            "shpool detach",
            refused,
            "the hand-back's `command shpool detach` was not intercepted; "
            f"the guard recorded {refused!r} for this test. It went somewhere "
            "else, and the only somewhere else is the real binary.",
        )


class HandBuiltEnvironmentsTests(unittest.TestCase):
    """A suite that builds its own PATH cannot step outside the guard."""

    def setUp(self) -> None:
        self.real = shutil.which("shpool", path=sandbox_guard.INHERITED_PATH)
        if self.real is None:
            self.skipTest("no real shpool on this machine's inherited PATH")
        self.real_dir = os.path.dirname(self.real)

    def hand_built(self) -> dict[str, str]:
        # The shape several suites already use: a directory of their own, then
        # a hard-coded tail. Here the tail is the directory the real binary
        # actually lives in, which is the worst case the guard has to survive.
        return {
            "PATH": f"{self.real_dir}:/usr/bin:/bin",
            "HOME": os.environ.get("HOME", "/tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def test_subprocess_with_a_hand_built_path_lands_on_the_stub(self) -> None:
        completed = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", "command -v shpool || true"],
            env=self.hand_built(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(
            os.fspath(sandbox_guard.guard_dir() / "shpool"),
            completed.stdout.strip(),
            "a hand-built PATH reached past the guard",
        )

    def test_os_execve_with_a_hand_built_path_lands_on_the_stub(self) -> None:
        # Nine suites fork and exec with a hand-built environment rather than
        # going through subprocess. The fork shares this interpreter, so the
        # patched os.execve reaches the child.
        read_end, write_end = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the child never returns
            try:
                os.dup2(write_end, 1)
                os.close(read_end)
                os.execve(
                    "/bin/bash",
                    ["bash", "--noprofile", "--norc", "-c", "command -v shpool"],
                    self.hand_built(),
                )
            finally:
                os._exit(127)
        os.close(write_end)
        with os.fdopen(read_end, encoding="utf-8") as handle:
            output = handle.read().strip()
        os.waitpid(pid, 0)
        self.assertEqual(
            os.fspath(sandbox_guard.guard_dir() / "shpool"),
            output,
            "an os.execve with a hand-built PATH reached past the guard",
        )

    def test_asking_for_absence_gives_absence_not_the_real_binary(self) -> None:
        # tests/test_cli_help.py proves `sp help` answers on a machine with no
        # session manager. The guard's stub would make that pass for the wrong
        # reason, so the guard honours a request for absence -- and answers it
        # by dropping the real binary's directory too. This PATH starts with
        # that directory, which is the case that has to come back empty.
        completed = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", "command -v shpool || true"],
            env=sandbox_guard.without_shpool(self.hand_built()),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(
            "",
            completed.stdout.strip(),
            "asking for no session manager produced one",
        )

    def test_the_stub_refuses_loudly_and_nonzero(self) -> None:
        completed = subprocess.run(
            ["shpool", "detach"],
            env=self.hand_built(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(sandbox_guard.REFUSAL_EXIT_CODE, completed.returncode)
        self.assertIn("refused `shpool detach`", completed.stderr)
        self.assertIn("sandboxed", completed.stderr)


class StateHomePinTests(unittest.TestCase):
    """`__sk_state_root` is ${XDG_STATE_HOME:-$HOME/.local/state}."""

    def test_an_inherited_state_home_is_replaced_by_the_fixture_one(self) -> None:
        # The escape: a fixture copies os.environ, overrides HOME, and carries
        # the operator's XDG_STATE_HOME into the child, which then writes every
        # provider-bounce and session-color record outside the fixture.
        home = "/nonexistent-fixture-home"
        environment = {
            "HOME": home,
            "XDG_STATE_HOME": sandbox_guard._INHERITED_STATE_HOME or "",
        }
        if not environment["XDG_STATE_HOME"]:
            environment["XDG_STATE_HOME"] = "/nonexistent-real-state"
            sandbox_guard._INHERITED_STATE_HOME = "/nonexistent-real-state"
            self.addCleanup(
                setattr, sandbox_guard, "_INHERITED_STATE_HOME", None
            )
        guarded = sandbox_guard.guarded_environment(environment)
        self.assertEqual(
            os.path.join(home, ".local", "state"), guarded["XDG_STATE_HOME"]
        )

    def test_an_absent_state_home_is_pinned_to_the_fixture_home(self) -> None:
        home = "/nonexistent-fixture-home"
        guarded = sandbox_guard.guarded_environment({"HOME": home})
        self.assertEqual(
            os.path.join(home, ".local", "state"), guarded["XDG_STATE_HOME"]
        )

    def test_a_state_home_the_test_chose_is_left_alone(self) -> None:
        chosen = "/nonexistent-fixture-home/elsewhere/state"
        guarded = sandbox_guard.guarded_environment(
            {"HOME": "/nonexistent-fixture-home", "XDG_STATE_HOME": chosen}
        )
        self.assertEqual(chosen, guarded["XDG_STATE_HOME"])

    def test_the_process_no_longer_carries_an_inherited_state_home(self) -> None:
        if sandbox_guard._INHERITED_STATE_HOME is None:
            self.assertNotIn("XDG_STATE_HOME", os.environ)
        else:
            self.assertNotEqual(
                sandbox_guard._INHERITED_STATE_HOME,
                os.environ.get("XDG_STATE_HOME"),
            )


class FixtureStubsStillWinTests(unittest.TestCase):
    """The guard must not shadow a stub a fixture installed on purpose."""

    def test_a_recording_stub_in_the_sandbox_takes_precedence(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix=".sandbox-guard-", dir=REPO) as raw:
            base = Path(raw)
            binaries = base / "home" / ".local" / "bin"
            log = base / "shpool.log"
            stub = sandbox_guard.install_recording_shpool(binaries, log)
            completed = subprocess.run(
                ["bash", "--noprofile", "--norc", "-c", "command -v shpool"],
                env={
                    "PATH": f"{binaries}:{os.environ['PATH']}",
                    "SHPOOL_LOG": os.fspath(log),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(os.fspath(stub), completed.stdout.strip())

            subprocess.run(
                ["shpool", "list", "--json"],
                env={
                    "PATH": f"{binaries}:{os.environ['PATH']}",
                    "SHPOOL_LOG": os.fspath(log),
                },
                text=True,
                stdout=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(["list --json"], sandbox_guard.recorded_shpool_calls(log))
            self.assertEqual(
                [], sandbox_guard.unexpected_shpool_calls(log, ("list --json",))
            )
            self.assertEqual(
                ["list --json"],
                sandbox_guard.unexpected_shpool_calls(log, ("detach",)),
            )


class PrivateDaemonOptOutTests(unittest.TestCase):
    """The one legitimate escape, and it has to be spelled out to be taken."""

    def test_a_private_basename_is_allowed(self) -> None:
        self.assertEqual(
            "sk-exitproof-pool",
            sandbox_guard.private_daemon_name("sk-exitproof-pool"),
        )

    def test_the_guarded_basename_is_refused(self) -> None:
        with self.assertRaises(AssertionError) as raised:
            sandbox_guard.private_daemon_name("shpool")
        self.assertIn("own basename", str(raised.exception))

    def test_there_is_no_environment_switch_that_disables_the_guard(self) -> None:
        # A switch is the accidental escape this module exists to prevent, so
        # its absence is a contract, not an omission. The one marker the guard
        # does read (ABSENT_MARKER) makes it stricter, and the test above
        # proves it cannot produce the real binary.
        source = (REPO / "tests" / "sandbox_guard.py").read_text(encoding="utf-8")
        for name in ('environ.get("SESSION_KIT_TEST_REAL', "DISABLE_SANDBOX_GUARD"):
            self.assertNotIn(name, source)

    def test_the_only_marker_the_guard_reads_can_only_remove_access(self) -> None:
        markers = [
            name
            for name in dir(sandbox_guard)
            if name.endswith("_MARKER") and not name.startswith("_")
        ]
        self.assertEqual(["ABSENT_MARKER"], markers)
        real_dir = os.path.dirname(
            shutil.which("shpool", path=sandbox_guard.INHERITED_PATH) or "/nowhere"
        )
        guarded = sandbox_guard.guarded_environment(
            sandbox_guard.without_shpool({"PATH": f"{real_dir}:/usr/bin:/bin"})
        )
        self.assertNotIn(real_dir, guarded["PATH"].split(os.pathsep))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
