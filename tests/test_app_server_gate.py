"""The Codex App Server launch gate: what arms it, and what it says when it cannot.

Both checks run the exact program the login shell runs: the gate and the
warning are extracted from bashrc/shpool.bashrc so a drifted heredoc fails
here instead of silently on a real login.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.support import REPO

SHELL_PATH = REPO / "bashrc" / "shpool.bashrc"
SHELL = SHELL_PATH.read_text(encoding="utf-8")
GATE_ANCHOR = "__sk_coord_gate=$(python3 - \"$__sk_coord_config\" \"$__sk_cwd\" <<'PY'\n"
WARNING_ANCHOR = (
    "python3 - \"$__sk_state_root\" \"$SHPOOL_SESSION_NAME\" <<'PY' || true\n"
)
WEAK_STATE_WARNING = (
    "[session-kit: Codex App Server disabled — ~/.local/state must be mode 0700]"
)


def heredoc(anchor: str) -> str:
    """The exact shell-embedded program that follows an anchor line."""
    start = SHELL.index(anchor) + len(anchor)
    return SHELL[start : SHELL.index("\nPY\n", start)] + "\n"


class GateProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".gate-test-")
        self.base = Path(self.temporary.name)
        self.script = self.base / "gate.py"
        self.script.write_text(heredoc(GATE_ANCHOR), encoding="utf-8")
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.elsewhere = self.base / "elsewhere"
        self.elsewhere.mkdir()
        self.broker = self.repo / "provider_broker.py"
        self.broker.write_text("# fixture broker\n", encoding="utf-8")
        self.broker.chmod(0o644)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def config(self, name: str = "coordination", **overrides: object) -> Path:
        payload: dict[str, object] = {
            "codex_app_server": True,
            "codex_broker": os.fspath(self.broker),
            "repo_root": os.fspath(self.repo),
        }
        payload.update(overrides)
        path = self.base / f"{name}.json"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def gate(self, config: Path, cwd: Path) -> str:
        completed = subprocess.run(
            [sys.executable, os.fspath(self.script), os.fspath(config), os.fspath(cwd)],
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()

    def test_repo_cwd_still_arms_the_broker_byte_for_byte(self) -> None:
        for config in (self.config(), self.config(codex_app_server_all=True)):
            self.assertEqual(os.fspath(self.broker), self.gate(config, self.repo))

    def test_foreign_cwd_stays_dark_without_the_wider_key(self) -> None:
        for extra in ({}, {"codex_app_server_all": False}, {"codex_app_server_all": 1}):
            config = self.config(**extra)
            self.assertEqual("", self.gate(config, self.elsewhere))

    def test_wider_key_arms_the_server_alone_outside_the_repo(self) -> None:
        config = self.config(codex_app_server_all=True)
        self.assertEqual("-", self.gate(config, self.elsewhere))

    def dropped_key_config(self, key: str) -> Path:
        config = self.config(name=f"without-{key}", codex_app_server_all=True)
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload.pop(key)
        config.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        config.chmod(0o600)
        return config

    def test_every_existing_validation_still_refuses_the_wider_key(self) -> None:
        linked = self.repo / "linked_broker.py"
        linked.symlink_to(self.broker)
        cases: dict[str, Path] = {
            "app-server off": self.config(
                name="off", codex_app_server=False, codex_app_server_all=True
            ),
            "app-server key absent": self.dropped_key_config("codex_app_server"),
            "repo root key absent": self.dropped_key_config("repo_root"),
            "relative broker": self.config(
                name="relative",
                codex_broker="provider_broker.py",
                codex_app_server_all=True,
            ),
            "absent broker": self.config(
                name="absent",
                codex_broker=os.fspath(self.repo / "gone.py"),
                codex_app_server_all=True,
            ),
            "symlinked broker": self.config(
                name="linked",
                codex_broker=os.fspath(linked),
                codex_app_server_all=True,
            ),
        }
        for label, config in cases.items():
            for cwd in (self.repo, self.elsewhere):
                self.assertEqual("", self.gate(config, cwd), msg=label)

        # A group-writable config, an oversized one, and a writable broker are
        # each refused on their own.
        writable = self.config(name="writable", codex_app_server_all=True)
        writable.chmod(0o660)
        self.assertEqual("", self.gate(writable, self.repo))

        oversized = self.config(
            name="oversized", codex_app_server_all=True, filler="x" * 9000
        )
        self.assertEqual("", self.gate(oversized, self.repo))

        self.broker.chmod(0o666)
        config = self.config(name="writable-broker", codex_app_server_all=True)
        self.assertEqual("", self.gate(config, self.elsewhere))
        self.broker.chmod(0o644)

    def test_malformed_config_is_silent(self) -> None:
        path = self.base / "coordination.json"
        path.write_text("{not json", encoding="utf-8")
        path.chmod(0o600)
        self.assertEqual("", self.gate(path, self.repo))


class GateWiringTests(unittest.TestCase):
    def test_gate_arms_the_server_and_the_broker_stays_repo_scoped(self) -> None:
        # The config file itself is still read only when it is a real,
        # non-symlinked regular file.
        self.assertIn(
            "[[ -f $__sk_coord_config && ! -L $__sk_coord_config ]]", SHELL
        )
        # Only an absolute path or the bare "-" ever arms anything.
        self.assertIn("case $__sk_coord_gate in", SHELL)
        self.assertIn("/*) __sk_coord_broker=$__sk_coord_gate ;;", SHELL)
        self.assertIn("*) __sk_coord_gate= ;;", SHELL)
        launch = SHELL.index("if [[ -n $__sk_coord_gate ]]; then")
        remote = SHELL.index('__sk_codex_remote=(--remote "unix://$__sk_app_socket")')
        broker_guard = SHELL.index("if [[ -n $__sk_coord_broker ]]; then")
        spawn = SHELL.index('python3 "$__sk_coord_broker" "${__sk_broker_args[@]}"')
        self.assertLess(launch, remote)
        self.assertLess(remote, broker_guard)
        self.assertLess(broker_guard, spawn)


class PermissionWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".gate-warn-")
        self.base = Path(self.temporary.name)
        self.script = self.base / "warn.py"
        self.script.write_text(heredoc(WARNING_ANCHOR), encoding="utf-8")
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.session = "s20260807-093118-829518"

    def tearDown(self) -> None:
        self.state.chmod(0o700)
        self.temporary.cleanup()

    def warn(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, os.fspath(self.script), os.fspath(self.state), self.session],
            text=True,
            capture_output=True,
            check=True,
        )

    def log_path(self) -> Path:
        return (
            self.state / "session-kit" / "app-server" / self.session / "app-server.log"
        )

    def test_weak_state_root_names_the_exact_reason(self) -> None:
        self.state.chmod(0o755)
        completed = self.warn()
        self.assertEqual(WEAK_STATE_WARNING, completed.stderr.strip())
        self.assertEqual("", completed.stdout)

    def test_private_state_root_reports_the_other_failure_instead(self) -> None:
        completed = self.warn()
        self.assertIn("Codex App Server disabled", completed.stderr)
        self.assertNotIn("mode 0700", completed.stderr)

    def test_the_line_is_appended_to_an_existing_log_only(self) -> None:
        self.state.chmod(0o755)
        self.warn()
        self.assertFalse(self.log_path().exists())

        self.state.chmod(0o700)
        self.log_path().parent.mkdir(parents=True, mode=0o700)
        self.log_path().write_text("earlier server output\n", encoding="utf-8")
        self.log_path().chmod(0o600)
        self.state.chmod(0o755)
        self.warn()
        self.assertEqual(
            ["earlier server output", WEAK_STATE_WARNING],
            self.log_path().read_text(encoding="utf-8").splitlines(),
        )

    def test_a_symlinked_log_is_refused(self) -> None:
        self.log_path().parent.mkdir(parents=True, mode=0o700)
        target = self.base / "outside.log"
        target.write_text("untouched\n", encoding="utf-8")
        self.log_path().symlink_to(target)
        self.state.chmod(0o755)
        completed = self.warn()
        self.assertEqual(WEAK_STATE_WARNING, completed.stderr.strip())
        self.assertEqual("untouched\n", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
