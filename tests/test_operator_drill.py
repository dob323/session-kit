"""The operator drill: isolated by construction, and the husk rides in it.

Two things are proved here, and they are the reasons the drill exists.

*Isolation.* The drill builds a sandbox with its own HOME, state directory and
a stub `shpool` that starts no daemon. A fixture daemon can answer its own
client with names that must never be combined with the operator daemon's
process tree, so the drill asserts every path it hands the kit is a fixture
path and refuses itself otherwise. These tests drive that refusal directly.

*The husk.* `IFS=$'\t' read` collapses a run of tabs and drops the empty fields
between them. The launch record's launch key is empty for every `sp new
--model` without `--launch-key`, so the generation fields shifted one place
left and the launch was refused as mismatched. Three sessions in one night
became shells with no provider. The last test here puts the bug back into a
throwaway copy of the release and proves the drill fails on it.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.support import REPO


DRILL = REPO / "tools" / "operator-drill"


def load_drill():
    """Import the drill, which ships without a .py suffix."""
    spec = importlib.util.spec_from_loader(
        "operator_drill",
        importlib.machinery.SourceFileLoader("operator_drill", os.fspath(DRILL)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DrillIsolationTests(unittest.TestCase):
    """The fence, driven from both sides."""

    def setUp(self) -> None:
        self.drill = load_drill()
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-drill-test.")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / self.drill.SANDBOX_MARK).touch()
        self.binaries = Path(self.drill.write_stub(os.fspath(self.root)))
        self.stub_state = self.root / "stub"
        self.stub_state.mkdir()
        for name in ("home", "state", "state/shpool-start", "run"):
            (self.root / name).mkdir(exist_ok=True)

    def environment(self, **overrides) -> dict:
        base = {
            "HOME": os.fspath(self.root / "home"),
            "XDG_STATE_HOME": os.fspath(self.root / "state"),
            "XDG_RUNTIME_DIR": os.fspath(self.root / "run"),
            "SESSION_KIT_STATE_DIR": os.fspath(self.root / "state"),
            "SESSION_KIT_START_DIR": os.fspath(self.root / "state/shpool-start"),
            "SESSION_KIT_JOURNAL_DIR": os.fspath(self.root / "state/journal"),
            "SESSION_KIT_SHPOOL_CMD": os.fspath(self.binaries / "shpool"),
            "SHPOOL_SOCKET": os.fspath(self.stub_state / "shpool.socket"),
            "DRILL_SANDBOX": os.fspath(self.root),
            "DRILL_STUB_STATE": os.fspath(self.stub_state),
            "PATH": os.fspath(self.binaries) + ":/usr/bin:/bin",
        }
        base.update(overrides)
        return base

    def test_a_fixture_environment_is_accepted(self) -> None:
        evidence = self.drill.assert_isolated(os.fspath(self.root), self.environment())
        self.assertTrue(any("stub" in line for line in evidence))

    def test_a_home_outside_the_sandbox_is_refused(self) -> None:
        with self.assertRaises(self.drill.IsolationError) as refusal:
            self.drill.assert_isolated(
                os.fspath(self.root), self.environment(HOME=os.path.expanduser("~"))
            )
        self.assertIn("HOME", str(refusal.exception))

    def test_a_socket_path_outside_the_sandbox_is_refused(self) -> None:
        with self.assertRaises(self.drill.IsolationError) as refusal:
            self.drill.assert_isolated(
                os.fspath(self.root),
                self.environment(
                    SHPOOL_SOCKET=os.path.expanduser("~/.local/state/shpool.socket")
                ),
            )
        self.assertIn("SHPOOL_SOCKET", str(refusal.exception))

    def test_a_state_directory_outside_the_sandbox_is_refused(self) -> None:
        with self.assertRaises(self.drill.IsolationError) as refusal:
            self.drill.assert_isolated(
                os.fspath(self.root),
                self.environment(SESSION_KIT_STATE_DIR="/tmp"),
            )
        self.assertIn("SESSION_KIT_STATE_DIR", str(refusal.exception))

    def test_a_real_shpool_on_the_path_is_refused(self) -> None:
        """The stub must be the only `shpool` the drill can reach."""
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        impostor = elsewhere / "shpool"
        impostor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        impostor.chmod(0o700)
        with self.assertRaises(self.drill.IsolationError) as refusal:
            self.drill.assert_isolated(
                os.fspath(self.root),
                self.environment(PATH=f"{elsewhere}:{self.binaries}:/usr/bin:/bin"),
            )
        self.assertIn("not the stub", str(refusal.exception))

    def test_an_unmarked_sandbox_is_refused(self) -> None:
        (self.root / self.drill.SANDBOX_MARK).unlink()
        with self.assertRaises(self.drill.IsolationError) as refusal:
            self.drill.assert_isolated(os.fspath(self.root), self.environment())
        self.assertIn("marker", str(refusal.exception))

    def test_the_stub_refuses_to_start_a_daemon(self) -> None:
        refused = subprocess.run(
            [os.fspath(self.binaries / "shpool"), "daemon"],
            env={**os.environ, **self.environment()},
            capture_output=True,
            text=True,
        )
        self.assertEqual(3, refused.returncode)
        self.assertIn("never starts a daemon", refused.stderr)
        self.assertFalse((self.stub_state / "shpool.socket").exists())

    def test_the_stub_refuses_a_state_directory_outside_its_sandbox(self) -> None:
        outside = self.root.parent / "outside-stub-state"
        outside.mkdir(exist_ok=True)
        self.addCleanup(lambda: outside.rmdir() if outside.exists() else None)
        refused = subprocess.run(
            [os.fspath(self.binaries / "shpool"), "list"],
            env={
                **os.environ,
                **self.environment(DRILL_STUB_STATE=os.fspath(outside)),
            },
            capture_output=True,
            text=True,
        )
        self.assertEqual(3, refused.returncode)
        self.assertIn("outside the sandbox", refused.stderr)


class DrillRunTests(unittest.TestCase):
    """The drill itself, run the way the release gate runs it."""

    def drive(self, *arguments: str, release: Path | None = None):
        completed = subprocess.run(
            [
                os.fspath(DRILL),
                "--json",
                "--release",
                os.fspath(release or REPO),
                *arguments,
            ],
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
        return completed, payload

    @unittest.skipUnless(
        os.path.exists("/proc/self/stat"), "the launch path reads process generations"
    )
    def test_every_stub_step_passes_against_this_tree(self) -> None:
        completed, payload = self.drive()
        verdicts = {step["step"]: step["verdict"] for step in payload.get("steps", [])}
        self.assertEqual(
            0, completed.returncode, f"{verdicts}\n{completed.stdout}\n{completed.stderr}"
        )
        self.assertEqual("PASS", verdicts.get("0 isolation"))
        self.assertEqual("PASS", verdicts.get("1 launch"))
        self.assertEqual("PASS", verdicts.get("2 model launch"))
        self.assertEqual("PASS", verdicts.get("3 stale record"))
        self.assertEqual("PASS", verdicts.get("4 no daemon"))
        self.assertFalse(payload["failed"])

    @unittest.skipUnless(
        os.path.exists("/proc/self/stat"), "the launch path reads process generations"
    )
    def test_the_run_names_no_path_of_the_operators(self) -> None:
        _, payload = self.drive("--steps", "1")
        home = os.path.realpath(os.path.expanduser("~"))
        rendered = json.dumps(payload)
        self.assertNotIn(f'"{home}', rendered)
        self.assertNotIn(f"{home}/.local/state/session-kit", rendered)

    def test_an_unknown_step_is_a_usage_error(self) -> None:
        completed, _ = self.drive("--steps", "9")
        self.assertEqual(2, completed.returncode)
        self.assertIn("no step 9", completed.stderr)

    @unittest.skipUnless(
        os.path.exists("/proc/self/stat"), "the launch path reads process generations"
    )
    def test_the_drill_fails_when_the_tab_collapse_husk_returns(self) -> None:
        """Put the bug back; the drill must refuse to call the release good.

        A regression case that cannot fail is decoration. This rewrites the
        launch record's read back to the collapsing form in a throwaway copy of
        the release, and expects step 2 to fail on it.
        """
        source = (REPO / "bashrc" / "shpool.bashrc").read_text(encoding="utf-8")
        translation = "__sk_launch_line=${__sk_launch_line//$'\\t'/$'\\034'}\n"
        read = "IFS=$'\\034' read -r __sk_launch_provider __sk_launch_cwd"
        self.assertIn(translation, source, "the launch record's \\034 translation moved")
        self.assertIn(read, source, "the launch record's read moved")
        husked = source.replace(translation, "", 1).replace(
            read, "IFS=$'\\t' read -r __sk_launch_provider __sk_launch_cwd", 1
        )

        with tempfile.TemporaryDirectory(prefix="session-kit-husk.") as scratch:
            release = Path(scratch) / "release"
            (release / "bashrc").mkdir(parents=True)
            (release / "bashrc" / "shpool.bashrc").write_text(husked, encoding="utf-8")
            (release / "lib").symlink_to(REPO / "lib")
            completed, payload = self.drive("--steps", "1,2", release=release)

        verdicts = {step["step"]: step["verdict"] for step in payload.get("steps", [])}
        self.assertEqual("PASS", verdicts.get("1 launch"), completed.stdout)
        self.assertEqual("FAIL", verdicts.get("2 model launch"), completed.stdout)
        self.assertEqual(1, completed.returncode)


if __name__ == "__main__":
    unittest.main()
