from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
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
        for command in ("shpool", "claude", "codex", "launchctl", "plutil", "sw_vers"):
            executable = self.fake_bin / command
            body = "#!/usr/bin/env bash\nexit 0\n"
            if command == "shpool":
                body = (
                    "#!/usr/bin/env bash\n"
                    "if [[ ${1:-} == version ]]; then echo 'shpool 0.11.0'; fi\n"
                    "exit 0\n"
                )
            elif command == "claude":
                body = "#!/usr/bin/env bash\necho '2.1.221 (Claude Code)'\n"
            elif command == "codex":
                body = "#!/usr/bin/env bash\necho 'codex-cli 0.145.0'\n"
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

    def clean_source_fixture(self) -> Path:
        source = self.temp / "clean-source"
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
        subprocess.run(["git", "-C", source, "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                source,
                "-c",
                "user.name=Session Kit Tests",
                "-c",
                "user.email=session-kit-tests@invalid.example",
                "commit",
                "-q",
                "-m",
                "fixture",
            ],
            check=True,
        )
        return source

    def write_systemctl_probe_fixture(self) -> Path:
        log = self.temp / "systemctl-probes.log"
        systemctl = self.fake_bin / "systemctl"
        systemctl.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$SESSION_KIT_SYSTEMCTL_LOG\"\n"
            "case \"$*\" in\n"
            "  '--user show-environment') exit \"${SESSION_KIT_DIRECT_RC:-0}\" ;;\n"
            "  '--user --machine=@.host show-environment') exit \"${SESSION_KIT_MACHINE_RC:-1}\" ;;\n"
            "  *) exit 64 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        systemctl.chmod(0o755)
        return log

    def test_check_is_read_only(self) -> None:
        result = self.run_installer("--check")
        self.assertIn("OK    source: installable", result.stdout)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_check_reports_direct_and_fallback_systemd_transports(self) -> None:
        source = self.clean_source_fixture()
        log = self.write_systemctl_probe_fixture()
        env = self.env.copy()
        env.pop("SESSION_KIT_TESTING")
        env.pop("SESSION_KIT_RELEASE_ID")
        env["SESSION_KIT_SYSTEMCTL_LOG"] = str(log)

        cases = (
            ("0", "1", 0, "OK    user service manager: systemd", ["--user show-environment"]),
            (
                "1",
                "0",
                0,
                "WARN  user service manager: available through local-machine transport; direct user socket is unavailable",
                ["--user show-environment", "--user --machine=@.host show-environment"],
            ),
            (
                "1",
                "1",
                1,
                "FAIL  systemd user manager is unavailable",
                ["--user show-environment", "--user --machine=@.host show-environment"],
            ),
        )
        for direct_rc, machine_rc, expected_rc, message, calls in cases:
            with self.subTest(direct_rc=direct_rc, machine_rc=machine_rc):
                log.unlink(missing_ok=True)
                env["SESSION_KIT_DIRECT_RC"] = direct_rc
                env["SESSION_KIT_MACHINE_RC"] = machine_rc
                result = subprocess.run(
                    [str(source / "install.sh"), "--check"],
                    cwd=source,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(result.returncode, expected_rc, result.stdout + result.stderr)
                self.assertIn(message, result.stdout)
                self.assertEqual(log.read_text(encoding="utf-8").splitlines(), calls)

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
            "lib/sessionkit_inventory/projects.py",
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
            (current / "lib/sessionkit_inventory/projects.py").is_file()
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

    def test_initial_install_can_import_existing_provider_projects(self) -> None:
        claude_project = self.home / "work/claude-project"
        codex_project = self.home / "work/codex-project"
        shared = self.home / "work/shared"
        for path in (claude_project, codex_project, shared):
            path.mkdir(parents=True)
        claude_config = self.home / ".claude.json"
        claude_config.write_text(
            json.dumps({"projects": {str(claude_project): {}, str(shared): {}}}),
            encoding="utf-8",
        )
        claude_config.chmod(0o600)
        codex_home = self.home / ".codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            f'[projects."{shared}"]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        connection = sqlite3.connect(codex_home / "state_5.sqlite")
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT)")
        connection.execute(
            "INSERT INTO threads VALUES (?, ?)",
            ("00000000-0000-4000-8000-000000000001", str(codex_project)),
        )
        connection.commit()
        connection.close()

        result = self.run_installer("--non-interactive", "--import-projects")

        # The shared directory is discovered under both providers and still
        # earns exactly one shortcut.
        self.assertIn("Imported 3 new project shortcut(s)", result.stdout)
        projects_file = self.home / ".config/session-kit/projects.tsv"
        rows = {
            tuple(line.split("\t")[:3])
            for line in projects_file.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        }
        self.assertEqual(
            rows,
            {
                ("claude-project", "claude", str(claude_project)),
                ("codex-project", "codex", str(codex_project)),
                ("shared", "claude", str(shared)),
            },
        )
        listed = self.installed("projects", "list")
        self.assertIn(f"shared\tclaude\t{shared}", listed.stdout)

    def test_noninteractive_install_defers_project_import_and_command_can_rerun_it(self) -> None:
        project = self.home / "work/existing"
        project.mkdir(parents=True)
        config = self.home / ".claude.json"
        config.write_text(
            json.dumps({"projects": {str(project): {}}}), encoding="utf-8"
        )
        config.chmod(0o600)

        installed = self.run_installer("--non-interactive")

        self.assertIn("Project import skipped", installed.stdout)
        projects_file = self.home / ".config/session-kit/projects.tsv"
        self.assertFalse(
            any(
                line and not line.startswith("#")
                for line in projects_file.read_text(encoding="utf-8").splitlines()
            )
        )
        imported = self.installed("projects", "import")
        self.assertIn("Imported 1 new project shortcut(s)", imported.stdout)
        self.assertIn(f"existing\tclaude\t{project}", projects_file.read_text())

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
            Path(shpool_plist["EnvironmentVariables"]["SHELL"]).resolve(),
            Path(expected_bash).resolve(),
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
        self.assertEqual(names["codex-themes"]["status"], "ok")
        self.assertEqual(names["acceptance"]["status"], "warn")

    def test_doctor_warns_when_only_systemd_machine_transport_works(self) -> None:
        self.run_installer("--non-interactive")
        log = self.write_systemctl_probe_fixture()
        env = self.env.copy()
        env.pop("SESSION_KIT_TESTING")
        env.pop("SESSION_KIT_RELEASE_ID")
        env.update(
            {
                "SESSION_KIT_SYSTEMCTL_LOG": str(log),
                "SESSION_KIT_DIRECT_RC": "1",
                "SESSION_KIT_MACHINE_RC": "0",
            }
        )

        result = subprocess.run(
            [str(self.home / ".local/bin/session-kit"), "doctor", "--json"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        names = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual(names["services"]["status"], "warn")
        self.assertEqual(
            names["services"]["detail"],
            "systemd user manager is available through local-machine transport; direct user socket is unavailable",
        )
        self.assertEqual(
            log.read_text(encoding="utf-8").splitlines(),
            ["--user show-environment", "--user --machine=@.host show-environment"],
        )

    def test_doctor_audits_naming_and_redacts_kill_switch_values(self) -> None:
        self.run_installer("--enable-login")
        codex = self.home / ".codex"
        claude = self.home / ".claude"
        (codex / "AGENTS.md").write_text("run sp self-name once\n", encoding="utf-8")
        claude.mkdir(exist_ok=True)
        (claude / "CLAUDE.md").write_text("run sp self-name once\n", encoding="utf-8")
        hooks = claude / "hooks"
        hooks.mkdir()
        title_hook = hooks / "nameintent_title.sh"
        title_hook.write_text(
            "#!/bin/sh\n"
            "cat >/dev/null\n"
            "printf '%s\\n' '{\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",\"sessionTitle\":\"Session Kit Doctor Fixture\"}}'\n",
            encoding="utf-8",
        )
        title_hook.chmod(0o700)
        hook = {
            "hooks": [
                {
                    "type": "command",
                    "command": "~/.claude/hooks/nameintent_title.sh",
                }
            ]
        }
        (claude / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [hook]
                        for event in ("SessionStart", "UserPromptSubmit", "Stop")
                    }
                }
            ),
            encoding="utf-8",
        )
        acceptance = self.home / ".config/session-kit/release-acceptance.json"
        acceptance.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "release_id": RELEASE_A,
                    "platform": "linux" if sys.platform != "darwin" else "macos",
                    "provider_versions": {
                        "claude": "2.1.221 (Claude Code)",
                        "codex": "codex-cli 0.145.0",
                    },
                    "accepted_on": "2026-08-04",
                    "evidence": {
                        "unique_colors": "fixture-colors",
                        "thread_titles": "fixture-titles",
                        "resume_roundtrip": "fixture-resume",
                    },
                }
            ),
            encoding="utf-8",
        )
        acceptance.chmod(0o600)
        self.env["SESSION_KIT_NO_COLOR"] = "secret-color-value"

        result = self.installed("doctor", "--json")

        self.assertNotIn("secret-color-value", result.stdout)
        names = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual(names["naming-instructions"]["status"], "ok")
        self.assertEqual(names["naming-hook"]["status"], "ok")
        self.assertEqual(names["acceptance"]["status"], "ok")
        self.assertEqual(names["kill-switches"]["status"], "warn")
        self.assertIn("SESSION_KIT_NO_COLOR", names["kill-switches"]["detail"])

        stale = json.loads(acceptance.read_text(encoding="utf-8"))
        stale["release_id"] = RELEASE_B
        acceptance.write_text(json.dumps(stale), encoding="utf-8")
        acceptance.chmod(0o600)
        stale_result = self.installed("doctor", "--json")
        stale_names = {
            row["name"]: row for row in json.loads(stale_result.stdout)["checks"]
        }
        self.assertEqual(stale_names["acceptance"]["status"], "warn")

    def test_doctor_requires_exact_typed_title_hook_command(self) -> None:
        self.run_installer("--non-interactive")
        claude = self.home / ".claude"
        hooks = claude / "hooks"
        hooks.mkdir(parents=True)
        title_hook = hooks / "nameintent_title.sh"
        title_hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        title_hook.chmod(0o700)
        bad_hook = {
            "hooks": [
                {
                    "type": "command",
                    "command": "sh ~/.claude/hooks/nameintent_title.sh",
                }
            ]
        }
        (claude / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [bad_hook],
                        "UserPromptSubmit": {"hooks": []},
                        "Stop": [bad_hook],
                    }
                }
            ),
            encoding="utf-8",
        )

        result = self.installed("doctor", "--json")

        names = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual(names["naming-hook"]["status"], "warn")
        self.assertIn("SessionStart", names["naming-hook"]["detail"])
        self.assertIn("UserPromptSubmit", names["naming-hook"]["detail"])

    def test_doctor_uses_custom_codex_home_for_themes_and_instructions(self) -> None:
        codex_home = self.temp / "private-codex"
        codex_home.mkdir(mode=0o700)
        self.env["CODEX_HOME"] = str(codex_home)
        self.run_installer("--non-interactive")
        (codex_home / "AGENTS.md").write_text(
            "run sp self-name once\n", encoding="utf-8"
        )
        claude = self.home / ".claude"
        claude.mkdir()
        (claude / "CLAUDE.md").write_text(
            "run sp self-name once\n", encoding="utf-8"
        )

        result = self.installed("doctor", "--json")

        names = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual(names["codex-themes"]["status"], "ok")
        self.assertEqual(names["naming-instructions"]["status"], "ok")
        self.assertFalse((self.home / ".codex").exists())

    def test_doctor_warns_for_unsafe_theme_without_failing(self) -> None:
        self.run_installer("--non-interactive")
        theme = self.home / ".codex/themes/sk-red.tmTheme"
        theme.chmod(0o644)

        result = self.installed("doctor", "--json")

        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        names = {row["name"]: row for row in payload["checks"]}
        self.assertEqual(names["codex-themes"]["status"], "warn")
        self.assertIn("red", names["codex-themes"]["detail"])

    def test_doctor_bounds_provider_version_output(self) -> None:
        self.run_installer("--non-interactive")
        codex = self.fake_bin / "codex"
        codex.write_text(
            "#!/usr/bin/env bash\nprintf '%0300d' 0\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)

        result = self.installed("doctor", "--json")

        self.assertNotIn("0" * 257, result.stdout)
        names = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual(names["codex-version"]["status"], "warn")
        self.assertEqual(
            names["codex-version"]["detail"],
            "codex --version exceeded 256 bytes",
        )

    def test_doctor_never_echoes_unrecognized_provider_stderr(self) -> None:
        self.run_installer("--non-interactive")
        codex = self.fake_bin / "codex"
        codex.write_text(
            "#!/usr/bin/env bash\nprintf 'provider-secret-value\\n' >&2\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)

        result = self.installed("doctor", "--json")

        self.assertNotIn("provider-secret-value", result.stdout)
        names = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual(names["codex-version"]["status"], "warn")
        self.assertEqual(
            names["codex-version"]["detail"],
            "codex reported an unrecognized version",
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "uses Linux /proc")
    def test_doctor_kills_provider_group_after_leader_exits(self) -> None:
        self.run_installer("--non-interactive")
        descendant_pid = self.temp / "provider-descendant.pid"
        codex = self.fake_bin / "codex"
        codex.write_text(
            "#!/usr/bin/env bash\n"
            "( trap '' TERM; while :; do sleep 1; done ) &\n"
            f"printf '%s\\n' \"$!\" > {descendant_pid!s}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)

        self.installed("doctor", "--json")

        pid = int(descendant_pid.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status = Path(f"/proc/{pid}/stat")
            if not status.exists() or status.read_text().split()[2] == "Z":
                break
            time.sleep(0.02)
        else:
            self.fail(f"provider descendant {pid} survived doctor cleanup")

    def test_install_rejects_relative_or_symlinked_custom_codex_home(self) -> None:
        self.env["CODEX_HOME"] = "relative-codex-home"
        relative = self.run_installer("--non-interactive", check=False)
        self.assertNotEqual(relative.returncode, 0)
        self.assertIn("absolute normalized path", relative.stderr)
        self.assertFalse((self.home / ".local/lib/session-kit").exists())

        target = self.temp / "codex-target"
        target.mkdir(mode=0o700)
        link = self.temp / "codex-link"
        link.symlink_to(target, target_is_directory=True)
        self.env["CODEX_HOME"] = str(link)
        linked = self.run_installer("--non-interactive", check=False)
        self.assertNotEqual(linked.returncode, 0)
        self.assertIn("unsafe Codex path ancestor", linked.stderr)
        self.assertFalse((target / "themes").exists())

    def test_install_and_doctor_reject_unsafe_codex_ancestors(self) -> None:
        unsafe = self.home / "unsafe"
        unsafe.mkdir(mode=0o700)
        codex_home = unsafe / "codex"
        codex_home.mkdir(mode=0o700)
        unsafe.chmod(0o777)
        self.env["CODEX_HOME"] = str(codex_home)

        rejected = self.run_installer("--non-interactive", check=False)

        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unsafe Codex path ancestor", rejected.stderr)
        self.assertFalse((codex_home / "themes").exists())

        self.env.pop("CODEX_HOME")
        unsafe.chmod(0o700)
        self.run_installer("--non-interactive")
        installed_root = self.home / ".codex"
        installed_root.chmod(0o777)
        result = self.installed("doctor", "--json")
        names = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual(names["codex-themes"]["status"], "warn")
        self.assertEqual(names["naming-instructions"]["status"], "warn")

    def test_empty_codex_home_uses_runtime_default(self) -> None:
        self.env["CODEX_HOME"] = ""

        self.run_installer("--non-interactive")

        self.assertTrue((self.home / ".codex/themes/sk-red.tmTheme").is_file())

    def test_theme_recovery_uses_recorded_home_when_environment_changes(self) -> None:
        codex_a = self.home / "codex-a"
        codex_a.mkdir(mode=0o700)
        self.env["CODEX_HOME"] = str(codex_a)
        self.run_installer("--non-interactive")
        red = codex_a / "themes/sk-red.tmTheme"
        red.write_text("recorded-a\n", encoding="utf-8")
        red.chmod(0o600)
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B

        for changed_home in ("", str(self.home / "unsafe-b")):
            self.env["CODEX_HOME"] = str(codex_a)
            self.env["SESSION_KIT_TEST_FAILPOINT"] = "themes"
            interrupted = self.installed(
                "update", "--source", str(REPO), check=False
            )
            self.assertNotEqual(interrupted.returncode, 0)
            self.env.pop("SESSION_KIT_TEST_FAILPOINT")
            if changed_home:
                unsafe_b = Path(changed_home)
                unsafe_b.mkdir(mode=0o700)
                unsafe_b.chmod(0o777)
            self.env["CODEX_HOME"] = changed_home

            recovered = self.installed(
                "update", "--source", str(self.temp / "missing-source"), check=False
            )

            self.assertNotEqual(recovered.returncode, 0)
            self.assertIn("Recovered interrupted Session Kit", recovered.stdout)
            self.assertEqual(red.read_text(encoding="utf-8"), "recorded-a\n")
            if changed_home:
                Path(changed_home).chmod(0o700)

    def test_no_theme_release_removes_only_captured_kit_themes(self) -> None:
        source = self.temp / "pre-theme-source"
        shutil.copytree(
            REPO,
            source,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        shutil.rmtree(source / "config/codex-themes")
        subprocess.run(["git", "init", "-q", source], check=True)
        subprocess.run(["git", "-C", source, "add", "."], check=True)
        subprocess.run(
            [
                "git", "-C", source, "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture",
            ],
            check=True,
        )
        self.run_installer("--source", str(source), "--non-interactive")
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        themes = self.home / ".codex/themes"
        unrelated = themes / "personal.tmTheme"
        unrelated.write_text("personal\n", encoding="utf-8")

        self.installed("rollback", "--to", RELEASE_A)

        self.assertTrue(unrelated.is_file())
        for color in (
            "red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan"
        ):
            self.assertFalse((themes / f"sk-{color}.tmTheme").exists())

    def test_mid_copy_failure_cleans_theme_temporary(self) -> None:
        self.run_installer("--non-interactive")
        themes = self.home / ".codex/themes"
        red_before = (themes / "sk-red.tmTheme").read_bytes()
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.env["SESSION_KIT_TEST_FAILPOINT"] = "theme-copy"

        interrupted = self.installed(
            "update", "--source", str(REPO), check=False
        )

        self.assertNotEqual(interrupted.returncode, 0)
        self.assertEqual(list(themes.glob(".sk-*.tmTheme.*")), [])
        self.assertEqual((themes / "sk-red.tmTheme").read_bytes(), red_before)

    def test_theme_targets_recover_after_interrupted_update(self) -> None:
        self.run_installer("--non-interactive")
        themes = self.home / ".codex/themes"
        red = themes / "sk-red.tmTheme"
        red.write_text("pre-update-theme\n", encoding="utf-8")
        red.chmod(0o600)
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.env["SESSION_KIT_TEST_FAILPOINT"] = "themes"

        interrupted = self.installed(
            "update", "--source", str(REPO), check=False
        )

        self.assertNotEqual(interrupted.returncode, 0)
        transaction = json.loads(
            (self.home / ".local/state/session-kit/lifecycle-transaction.json").read_text(
                encoding="utf-8"
            )
        )
        captured = {entry["path"] for entry in transaction["entries"]}
        expected = {
            str(themes / f"sk-{color}.tmTheme")
            for color in (
                "red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan"
            )
        }
        self.assertTrue(expected.issubset(captured))
        self.assertNotEqual(red.read_text(encoding="utf-8"), "pre-update-theme\n")

        self.env.pop("SESSION_KIT_TEST_FAILPOINT")
        recovered = self.installed(
            "update", "--source", str(self.temp / "missing-source"), check=False
        )

        self.assertNotEqual(recovered.returncode, 0)
        self.assertIn("Recovered interrupted Session Kit", recovered.stdout)
        self.assertEqual(red.read_text(encoding="utf-8"), "pre-update-theme\n")
        self.assertFalse(
            (self.home / ".local/state/session-kit/lifecycle-transaction.json").exists()
        )

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
