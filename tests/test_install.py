from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
RELEASE_A = "1" * 40
RELEASE_B = "2" * 40


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        # GitHub's macOS workspace carries an inherited ACL that denies
        # renaming sealed 0555 directories. Real installs live under HOME, so
        # keep Darwin fixtures in the normal per-user temporary directory.
        temp_parent = None if sys.platform == "darwin" else REPO.parent
        self.temp = Path(
            tempfile.mkdtemp(
                prefix="session-kit-install-test.",
                dir=temp_parent,
            )
        ).resolve()
        self.home = self.temp / "home"
        self.home.mkdir()
        self.fake_bin = self.temp / "bin"
        self.fake_bin.mkdir()
        for command in ("shpool", "codex", "launchctl", "plutil", "sw_vers"):
            executable = self.fake_bin / command
            body = "#!/usr/bin/env bash\nexit 0\n"
            if command == "shpool":
                body = (
                    "#!/usr/bin/env bash\n"
                    "if [[ ${1:-} == version ]]; then echo 'shpool 0.11.0'; fi\n"
                    "exit 0\n"
                )
            executable.write_text(
                body, encoding="utf-8"
            )
            executable.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:{self.env['PATH']}",
                "SESSION_KIT_RELEASE_ID": RELEASE_A,
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_STATE_HOME": str(self.home / ".local/state"),
                "SESSION_KIT_SYSTEMD_ROOT": str(self.temp / "systemd"),
                "SESSION_KIT_TESTING": "1",
            }
        )

    def tearDown(self) -> None:
        for path in self.temp.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o700)
        shutil.rmtree(self.temp)

    def run_installer(
        self, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(REPO / "install.sh"), *args],
            cwd=REPO,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode:
            self.fail(
                f"command failed ({result.returncode}): {args}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def installed(
        self, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        command = self.home / ".local/bin/session-kit"
        result = subprocess.run(
            [str(command), *args],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode:
            self.fail(
                f"installed command failed ({result.returncode}): {args}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def test_check_is_read_only(self) -> None:
        result = self.run_installer("--check")
        self.assertIn("OK    source: installable", result.stdout)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_check_rejects_partial_inventory_package(self) -> None:
        source = self.temp / "source"
        shutil.copytree(
            REPO,
            source,
            ignore=shutil.ignore_patterns(
                ".git",
                ".mypy_cache",
                ".ruff_cache",
                "__pycache__",
                "*.pyc",
            ),
        )
        subprocess.run(["git", "init", "-q", source], check=True)
        for relative in (
            "lib/sessionkit_inventory/__init__.py",
            "lib/sessionkit_inventory/common.py",
            "lib/sessionkit_inventory/lifecycle.py",
            "lib/sessionkit_inventory/providers.py",
            "lib/sessionkit_inventory/reaper.py",
            "lib/sessionkit_inventory/state_io.py",
        ):
            with self.subTest(relative=relative):
                path = source / relative
                payload = path.read_bytes()
                path.unlink()
                result = subprocess.run(
                    [str(source / "install.sh"), "--check"],
                    cwd=source,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"source file missing: {relative}", result.stdout)
                path.write_bytes(payload)

    def test_noninteractive_install_is_local_and_login_opt_in(self) -> None:
        self.run_installer("--non-interactive")
        current = self.home / ".local/lib/session-kit/current"
        self.assertTrue(current.is_symlink())
        self.assertEqual(current.resolve().name, RELEASE_A)
        bashrc = self.home / ".bashrc"
        self.assertFalse(bashrc.exists())
        self.assertTrue((self.home / ".no_shpool_journal").is_file())
        receipt = json.loads(
            (self.home / ".local/state/session-kit/install.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["installed_release"], RELEASE_A)
        self.assertTrue((self.home / ".local/bin/sp").is_file())
        self.assertTrue(
            (current / "lib/sessionkit_inventory/__init__.py").is_file()
        )
        self.assertTrue((current / "lib/sessionkit_inventory/common.py").is_file())
        self.assertTrue(
            (current / "lib/sessionkit_inventory/lifecycle.py").is_file()
        )
        self.assertTrue(
            (current / "lib/sessionkit_inventory/providers.py").is_file()
        )
        self.assertTrue(
            (current / "lib/sessionkit_inventory/reaper.py").is_file()
        )
        self.assertTrue(
            (current / "lib/sessionkit_inventory/state_io.py").is_file()
        )
        self.assertTrue((self.temp / "systemd/shpool.socket").is_file())
        service = (self.temp / "systemd/shpool.service").read_text(encoding="utf-8")
        self.assertIn(f"ExecStart={self.fake_bin / 'shpool'} daemon", service)
        self.assertNotIn("@SHPOOL@", service)

    def test_enable_disable_login_and_private_marker(self) -> None:
        self.run_installer("--enable-login", "--journal", "off")
        bashrc = self.home / ".bashrc"
        self.assertIn("session-kit managed integration", bashrc.read_text())
        marker = self.home / ".local/state/session-kit/integration-ready-v1"
        self.assertEqual(
            marker.read_text(encoding="utf-8"),
            f"session-kit-integration-v1 {RELEASE_A}\n",
        )
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.home / ".no_shpool_journal").is_file())
        self.installed("disable-login")
        self.assertNotIn("session-kit managed integration", bashrc.read_text())
        self.assertFalse(marker.exists())

    def test_rollover_install_preserves_validated_login_state(self) -> None:
        self.run_installer("--enable-login", "--journal", "off")
        marker = self.home / ".local/state/session-kit/integration-ready-v1"
        self.assertTrue(marker.is_file())
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.run_installer("--non-interactive")
        current = self.home / ".local/lib/session-kit/current"
        self.assertEqual(current.resolve().name, RELEASE_B)
        self.assertEqual(
            marker.read_text(encoding="utf-8"),
            f"session-kit-integration-v1 {RELEASE_B}\n",
        )
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        bashrc = self.home / ".bashrc"
        self.assertIn("session-kit managed integration", bashrc.read_text())
        self.assertEqual(
            1, bashrc.read_text().count(">>> session-kit managed integration >>>")
        )
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_A
        self.run_installer("--non-interactive", "--disable-login")
        self.assertFalse(marker.exists())

    def test_update_and_rollback_switch_exact_releases(self) -> None:
        self.run_installer("--non-interactive")
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        current = self.home / ".local/lib/session-kit/current"
        self.assertEqual(current.resolve().name, RELEASE_B)
        self.installed("rollback")
        self.assertEqual(current.resolve().name, RELEASE_A)

    def test_update_defaults_journals_off_and_allows_explicit_opt_in(self) -> None:
        self.run_installer("--journal", "on", "--non-interactive")
        sentinel = self.home / ".no_shpool_journal"
        self.assertFalse(sentinel.exists())
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        self.assertTrue(sentinel.is_file())
        self.installed(
            "update", "--source", str(REPO), "--journal", "on"
        )
        self.assertFalse(sentinel.exists())

    def test_interrupted_update_recovers_before_retry(self) -> None:
        self.run_installer("--non-interactive")
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.env["SESSION_KIT_TEST_FAILPOINT"] = "current"
        failed = self.installed(
            "update", "--source", str(REPO), check=False
        )
        self.assertNotEqual(failed.returncode, 0)
        current = self.home / ".local/lib/session-kit/current"
        self.assertEqual(current.resolve().name, RELEASE_B)
        transaction = (
            self.home / ".local/state/session-kit/lifecycle-transaction.json"
        )
        self.assertTrue(transaction.is_file())

        del self.env["SESSION_KIT_TEST_FAILPOINT"]
        recovered = self.installed("update", "--source", str(REPO))
        self.assertIn("Recovered interrupted Session Kit install", recovered.stdout)
        self.assertEqual(current.resolve().name, RELEASE_B)
        self.assertFalse(transaction.exists())
        backups = list(
            (self.home / ".local/state/session-kit/backups").glob(
                "lifecycle-*.json"
            )
        )
        self.assertGreaterEqual(len(backups), 2)

    def test_reused_release_is_verified_before_mutation(self) -> None:
        self.run_installer("--non-interactive")
        current = self.home / ".local/lib/session-kit/current"
        target = current / "bin/sp"
        target.chmod(0o755)
        target.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        target.chmod(0o555)
        result = self.installed(
            "update", "--source", str(REPO), check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release verification failed", result.stderr)
        self.assertFalse(
            (
                self.home
                / ".local/state/session-kit/lifecycle-transaction.json"
            ).exists()
        )

    def test_purge_requires_matching_ownership_before_login_mutation(self) -> None:
        self.run_installer("--enable-login", "--journal", "off")
        marker = (
            self.home
            / ".local/lib/session-kit/.session-kit-owned.json"
        )
        marker.write_text('{"owner":"someone-else"}\n', encoding="utf-8")
        bashrc = self.home / ".bashrc"
        before = bashrc.read_bytes()
        result = self.installed("uninstall", "--purge-code", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(bashrc.read_bytes(), before)
        self.assertTrue((self.home / ".local/bin/sp").is_file())

    def test_broad_install_root_is_rejected_without_mutation(self) -> None:
        self.env["SESSION_KIT_ROOT"] = str(self.home)
        result = self.run_installer("--non-interactive", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing broad install root", result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    @unittest.skipIf(
        sys.version_info < (3, 11),
        "the supported macOS lifecycle requires Python 3.11 or newer",
    )
    def test_macos_install_update_and_rollback_are_transactional(self) -> None:
        self.env["SESSION_KIT_TEST_PLATFORM"] = "macos"
        self.env["SESSION_KIT_SERVICE_ROOT"] = str(self.temp / "LaunchAgents")
        self.run_installer("--enable-login", "--journal", "off")
        current = self.home / ".local/lib/session-kit/current"
        self.assertEqual(current.resolve().name, RELEASE_A)
        self.assertEqual(current.resolve().stat().st_mode & 0o777, 0o555)
        templates = self.home / ".config/session-kit/launchd"
        self.assertEqual(
            {path.name for path in templates.glob("*.plist")},
            {
                "com.session-kit.shpool.plist",
                "com.session-kit.reaper.plist",
                "com.session-kit.watchdog.plist",
            },
        )
        self.assertFalse((self.temp / "LaunchAgents").exists())
        shpool_plist = plistlib.loads(
            (templates / "com.session-kit.shpool.plist").read_bytes()
        )
        expected_bash = shutil.which("bash", path=self.env["PATH"])
        self.assertIsNotNone(expected_bash)
        expected_config_bash = str(Path(expected_bash).resolve())
        self.assertEqual(
            shpool_plist["EnvironmentVariables"]["SHELL"],
            expected_bash,
        )
        self.assertEqual(
            shpool_plist["ProgramArguments"],
            [
                str(self.fake_bin / "shpool"),
                "--config-file",
                str(self.home / ".config/shpool/config.toml"),
                "daemon",
            ],
        )
        zshrc = self.home / ".zshrc"
        self.assertIn("$HOME/.local/bin", zshrc.read_text(encoding="utf-8"))
        self.assertNotIn("exec", zshrc.read_text(encoding="utf-8"))
        bash_profile = self.home / ".bash_profile"
        self.assertIn(
            ".local/lib/session-kit/current/bashrc/shpool.bashrc",
            bash_profile.read_text(encoding="utf-8"),
        )
        self.installed("disable-login")
        self.assertNotIn(
            "session-kit managed integration",
            bash_profile.read_text(encoding="utf-8"),
        )
        self.installed("enable-login")
        self.assertTrue((self.home / ".local/bin/kit").is_file())
        shpool_config = self.home / ".config/shpool/config.toml"
        self.assertIn(
            f'shell = "{expected_config_bash}"',
            shpool_config.read_text(encoding="utf-8"),
        )

        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        self.assertEqual(current.resolve().name, RELEASE_B)
        self.installed("rollback")
        self.assertEqual(current.resolve().name, RELEASE_A)

    def test_rollback_restores_helpers_units_pointer_and_receipt(self) -> None:
        self.run_installer("--non-interactive")
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        unit = self.temp / "systemd/shpool.socket"
        unit.write_text("foreign unit\n", encoding="utf-8")
        helper = self.home / ".local/bin/sp"
        helper.write_text("#!/bin/sh\nexit 88\n", encoding="utf-8")
        helper.chmod(0o755)

        self.installed("rollback")
        current = self.home / ".local/lib/session-kit/current"
        self.assertEqual(current.resolve().name, RELEASE_A)
        self.assertNotEqual(unit.read_text(encoding="utf-8"), "foreign unit\n")
        self.assertEqual(
            helper.read_bytes(),
            (current / "deploy/session-kit-launcher").read_bytes(),
        )
        receipt = json.loads(
            (self.home / ".local/state/session-kit/install.json").read_text()
        )
        self.assertEqual(receipt["installed_release"], RELEASE_A)
        self.assertEqual(receipt["previous_release"], RELEASE_B)
        backups = sorted(
            (self.home / ".local/state/session-kit/backups").glob(
                "lifecycle-*.json"
            ),
            key=lambda path: path.stat().st_mtime_ns,
        )
        payload = json.loads(backups[-1].read_text(encoding="utf-8"))
        saved = {
            entry["path"]: base64.b64decode(entry["content"])
            for entry in payload["entries"]
            if entry["kind"] == "file"
        }
        self.assertEqual(saved[str(unit)], b"foreign unit\n")
        self.assertEqual(saved[str(helper)], b"#!/bin/sh\nexit 88\n")

    def test_uninstall_retains_journals_and_does_not_manage_services(self) -> None:
        self.run_installer("--enable-login")
        journal = self.home / ".local/state/shpool-journal/example/segment.raw"
        journal.parent.mkdir(parents=True)
        journal.write_text("private transcript", encoding="utf-8")
        result = self.installed("uninstall", "--purge-code", "--purge-config")
        self.assertIn("No service was stopped or restarted", result.stdout)
        self.assertEqual(journal.read_text(encoding="utf-8"), "private transcript")
        self.assertFalse((self.home / ".local/lib/session-kit").exists())
        self.assertFalse((self.home / ".local/bin/sp").exists())

    def test_doctor_json_reports_install(self) -> None:
        self.run_installer("--enable-login")
        result = self.installed("doctor", "--json")
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        names = {row["name"]: row for row in payload["checks"]}
        self.assertEqual(names["release"]["detail"], RELEASE_A)
        self.assertEqual(names["login"]["status"], "ok")

    def test_doctor_rejects_partial_inventory_package(self) -> None:
        self.run_installer("--non-interactive")
        current = self.home / ".local/lib/session-kit/current"
        package = current / "lib/sessionkit_inventory"
        package.chmod(0o755)
        (current / "lib/sessionkit_inventory/common.py").chmod(0o644)
        (current / "lib/sessionkit_inventory/common.py").unlink()
        package.chmod(0o555)
        result = self.installed("doctor", "--json", check=False)
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        names = {row["name"]: row for row in payload["checks"]}
        self.assertEqual(names["release"]["status"], "fail")
        self.assertIn("lib/sessionkit_inventory/common.py", names["release"]["detail"])


if __name__ == "__main__":
    unittest.main()
