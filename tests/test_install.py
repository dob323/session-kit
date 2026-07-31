from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
RELEASE_A = "1" * 40
RELEASE_B = "2" * 40


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(
            tempfile.mkdtemp(
                prefix="session-kit-install-test.",
                dir=REPO.parent,
            )
        )
        self.home = self.temp / "home"
        self.home.mkdir()
        self.fake_bin = self.temp / "bin"
        self.fake_bin.mkdir()
        for command in ("shpool", "codex"):
            executable = self.fake_bin / command
            executable.write_text(
                "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
            )
            executable.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:{self.env['PATH']}",
                "SESSION_KIT_RELEASE_ID": RELEASE_A,
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

    def test_macos_install_update_and_rollback_fail_closed(self) -> None:
        self.env["SESSION_KIT_TEST_PLATFORM"] = "macos"
        install = self.run_installer("--non-interactive", check=False)
        self.assertNotEqual(install.returncode, 0)
        self.assertIn("macOS lifecycle operations are not supported", install.stdout)
        self.assertEqual(list(self.home.iterdir()), [])

        self.env["SESSION_KIT_TEST_PLATFORM"] = "linux"
        self.run_installer("--non-interactive")
        self.env["SESSION_KIT_TEST_PLATFORM"] = "macos"
        update = self.installed("update", "--source", str(REPO), check=False)
        rollback = self.installed("rollback", "--to", RELEASE_A, check=False)
        self.assertIn("update is supported only on Linux", update.stderr)
        self.assertIn("rollback is supported only on Linux", rollback.stderr)

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
