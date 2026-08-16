from __future__ import annotations

import base64
import grp
import hashlib
import json
import os
import pwd
from pathlib import Path
import plistlib
import re
import signal
import shutil
import sqlite3
import importlib.util
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(REPO / "lib"))

from tests.support import THEME_COLORS
from sessionkit_inventory.common import CollectionError
from sessionkit_inventory.state_io import StateLock


RELEASE_A = "1" * 40
RELEASE_B = "2" * 40
RELEASE_C = "3" * 40
# Launchers are compared byte for byte to prove which release a rollback took
# them from. A fixture release built out of this checkout would otherwise carry
# launchers identical to the release under test, and the comparison would hold
# no matter which one the code picked.
LEGACY_LAUNCHER_STAMP = "# Session Kit legacy generation fixture.\n"
# A unit name no release carries, so a fixture release that adds it reproduces
# the one state that breaks a rollback: a running kit whose unit list names a
# file the release it is activating never had.
FIXTURE_UNIT = "session-kit-fixture-sweep.service"


def host_group_is_private() -> bool:
    """Report whether this host gives the test account a private group.

    A group-writable provider directory is installable only where the group
    belongs to one account, which is how a private-group distribution creates
    it. Hosts with a shared primary group cannot stage that fixture.
    """
    try:
        account = pwd.getpwuid(os.geteuid())
        group = grp.getgrgid(os.getegid())
        accounts = pwd.getpwall()
    except (KeyError, OSError):  # pragma: no cover - directory service failure
        return False
    return bool(
        os.getegid() == account.pw_gid
        and group.gr_name == account.pw_name
        and not group.gr_mem
        and accounts
        and all(
            other.pw_gid != account.pw_gid or other.pw_name == account.pw_name
            for other in accounts
        )
    )


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        # GitHub's macOS workspace carries an inherited ACL that denies
        # renaming sealed 0555 directories. Real installs live under HOME, so
        # keep Darwin fixtures in the normal per-user temporary directory.
        # Managed worktrees may expose the checkout's parent read-only. /tmp is
        # writable in that environment and also avoids copying a source tree
        # into one of its own descendants in tests that stage a release.
        isolated_test_parent = os.environ.get("SESSION_KIT_TEST_TMPDIR")
        temp_parent = (
            Path(isolated_test_parent)
            if isolated_test_parent
            else None if sys.platform == "darwin" else Path("/tmp")
        )
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
                    "if [[ ${1:-} == version ]]; then echo 'shpool 0.11.0'; exit 0; fi\n"
                    "echo 'test shpool: refusing non-version command' >&2\n"
                    "exit 97\n"
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
                # A managed session exports the real profile here, and the
                # transcript checks honour it; the fixture must own its
                # ambient profile or foreground runs read the real estate.
                "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                "PATH": f"{self.fake_bin}:{self.env['PATH']}",
                "SESSION_KIT_RELEASE_ID": RELEASE_A,
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_DATA_HOME": str(self.home / ".local/share"),
                "XDG_STATE_HOME": str(self.home / ".local/state"),
                "SESSION_KIT_STATE_DIR": str(
                    self.home / ".local/state/session-kit"
                ),
                "SESSION_KIT_CONFIG": str(
                    self.home / ".config/session-kit/inventory.json"
                ),
                "SESSION_KIT_SYSTEMD_ROOT": str(self.temp / "systemd"),
                "SESSION_KIT_TESTING": "1",
            }
        )
        real_data_home = (Path.home() / ".local/share").resolve()
        isolated_paths = (
            Path(self.env["HOME"]),
            Path(self.env["XDG_CONFIG_HOME"]),
            Path(self.env["XDG_DATA_HOME"]),
            Path(self.env["XDG_STATE_HOME"]),
        )
        for path in isolated_paths:
            self.assertFalse(
                path.resolve().is_relative_to(real_data_home),
                f"installer test path escapes into operator data: {path}",
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

    def run_installer_on_a_terminal(
        self, keystrokes: str, *args: str
    ) -> subprocess.CompletedProcess[str]:
        """Run the installer with a terminal on stdin, the way a person runs
        it. It asks nothing there: it takes the marked defaults and reports
        them."""
        import pty

        parent, child = pty.openpty()
        try:
            process = subprocess.Popen(
                [str(REPO / "install.sh"), *args],
                cwd=REPO,
                env=self.env,
                stdin=child,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            os.close(child)
            child = -1
            os.write(parent, keystrokes.encode())
            output, _ = process.communicate(timeout=180)
        finally:
            if child >= 0:
                os.close(child)
            os.close(parent)
        return subprocess.CompletedProcess(
            args, process.returncode, output, ""
        )

    def test_installer_asks_nothing_and_reports_the_defaults_it_took(self) -> None:
        """The installer never asks. It takes the marked defaults on a
        terminal, installs, and states what it chose with the way back."""
        completed = self.run_installer_on_a_terminal("")
        self.assertEqual(0, completed.returncode, completed.stdout)
        for banned in ("[Y/n]", "[y/N]", "Answer y or n", "cancelled"):
            self.assertNotIn(banned, completed.stdout)
        self.assertIn(
            "Login integration is on. Turn it off with: session-kit disable-login",
            completed.stdout,
        )
        self.assertIn(
            "History recording is off. Turn it on with: "
            "session-kit update --journal on",
            completed.stdout,
        )
        self.assertTrue(
            (self.home / ".local/lib/session-kit/current").is_symlink(),
            completed.stdout,
        )
        self.assertIn(
            "session-kit managed integration",
            (self.home / ".bashrc").read_text(encoding="utf-8"),
        )
        self.assertTrue((self.home / ".no_shpool_journal").is_file())

    def test_fresh_install_seeds_collection_floor(self) -> None:
        self.run_installer("--non-interactive")

        floor = self.home / ".local/state/session-kit/collection-sequence-floor.json"
        self.assertEqual(
            {"schema_version": 1, "last_collection_start": 1},
            json.loads(floor.read_text(encoding="utf-8")),
        )
        self.assertEqual(0o600, stat.S_IMODE(floor.stat().st_mode))

    def test_install_never_overwrites_existing_collection_floor(self) -> None:
        state = self.home / ".local/state/session-kit"
        state.mkdir(parents=True, mode=0o700)
        floor = state / "collection-sequence-floor.json"
        original = b'{ "schema_version" : 1, "last_collection_start" : 41 }\n'
        floor.write_bytes(original)
        floor.chmod(0o600)

        self.run_installer("--non-interactive")

        self.assertEqual(original, floor.read_bytes())

    def test_install_refuses_underivable_collection_floor_with_exact_remedy(
        self,
    ) -> None:
        state = self.home / ".local/state/session-kit"
        state.mkdir(parents=True, mode=0o700)
        inventory = state / "inventory.json"
        inventory.write_text('{"sessions":[]}\n', encoding="utf-8")
        inventory.chmod(0o600)
        command = (
            f"env SESSION_KIT_STATE_DIR={state} python3 "
            f"{REPO / 'bin/reset-collection-order.py'}"
        )
        expected = (
            "session-kit-release: collection allocation floor cannot be derived "
            "from existing state; restart the machine and, before starting a "
            "picker, sp, shpool_status, or any Session Kit user service, run "
            f"exactly:\n  {command}\n"
        )

        refused = self.run_installer("--non-interactive", check=False)

        self.assertNotEqual(0, refused.returncode)
        self.assertEqual(expected, refused.stderr)
        self.assertFalse((state / "collection-sequence-floor.json").exists())
        self.assertFalse((self.home / ".local/lib/session-kit/current").exists())

    def test_interrupted_install_journals_provider_config_preimages_and_commits_backups(
        self,
    ) -> None:
        """The operator's provider config is preimaged before anything runs.

        The installer now owns exact Claude registrations, so an interrupted
        install must first restore what it found before the retry applies the
        release again. This proves the journal captured the real preimage --
        content for the file that was there, `absent` for the one that was not
        -- and that the committed backup carries the same record.
        """
        claude = self.home / ".claude"
        claude.mkdir()
        settings = claude / "settings.json"
        original = b'{"existing":"claude-setting"}\n'
        settings.write_bytes(original)
        settings.chmod(0o600)
        codex_hooks = self.home / ".codex" / "hooks.json"

        self.env["SESSION_KIT_TEST_FAILPOINT"] = "themes"
        self.env["SESSION_KIT_TEST_FAILPOINT_MODE"] = "kill"
        failed = self.run_installer("--non-interactive", check=False)
        self.assertEqual(-signal.SIGKILL, failed.returncode)
        journal_path = self.home / ".local/state/session-kit/lifecycle-transaction.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        entries = {item["path"]: item for item in journal["entries"]}
        self.assertEqual("file", entries[str(settings)]["kind"])
        self.assertEqual(original, base64.b64decode(entries[str(settings)]["content"]))
        self.assertEqual("absent", entries[str(codex_hooks)]["kind"])
        self.assertFalse(codex_hooks.exists())

        self.env.pop("SESSION_KIT_TEST_FAILPOINT")
        self.env.pop("SESSION_KIT_TEST_FAILPOINT_MODE")
        recovered = self.run_installer("--non-interactive")
        self.assertIn(
            "Recovered interrupted Session Kit install transaction", recovered.stdout
        )
        self.assertFalse(journal_path.exists())
        restored = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual("claude-setting", restored["existing"])
        self.assertEqual(
            "~/.claude/statusline.sh", restored["statusLine"]["command"]
        )
        self.assertFalse(codex_hooks.exists())

        backups = sorted(
            (self.home / ".local/state/session-kit/backups").glob("lifecycle-*.json")
        )
        committed = json.loads(backups[-1].read_text(encoding="utf-8"))
        committed_entries = {item["path"]: item for item in committed["entries"]}
        self.assertEqual(
            original, base64.b64decode(committed_entries[str(settings)]["content"])
        )
        self.assertEqual("absent", committed_entries[str(codex_hooks)]["kind"])

    def installed(
        self,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = self.home / ".local/bin/session-kit"
        result = subprocess.run(
            [str(command), *args],
            env=env if env is not None else self.env,
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

    def changed_claude_source_fixture(self) -> Path:
        source = self.clean_source_fixture()
        for relative in (
            "config/claude/nameintent_title.sh",
            "config/claude/statusline.sh",
        ):
            path = source / relative
            path.write_text(
                path.read_text(encoding="utf-8") + "\n# distinct fixture payload\n",
                encoding="utf-8",
            )
        subprocess.run(["git", "-C", source, "add", "."], check=True)
        subprocess.run(
            [
                "git", "-C", source,
                "-c", "user.name=Session Kit Tests",
                "-c", "user.email=session-kit-tests@invalid.example",
                "commit", "-q", "--amend", "--no-edit",
            ],
            check=True,
        )
        return source

    def stage_legacy_release(self, source_release: str, target_release: str) -> None:
        root = self.home / ".local/lib/session-kit/releases"
        target = root / target_release
        shutil.copytree(root / source_release, target)
        self.unseal_release(target)
        metadata = target / "RELEASE.json"
        metadata.chmod(0o644)
        value = json.loads(metadata.read_text(encoding="utf-8"))
        value["commit"] = target_release
        metadata.write_text(json.dumps(value) + "\n", encoding="utf-8")
        shutil.rmtree(target / "config/claude")
        self.seal_release(target)

    def make_release_fence_unaware(self, release_id: str) -> Path:
        """Give one retained release the real legacy lock contract.

        Its status command takes only inventory.lock and emits the retained
        inventory.  The target's StateLock source deliberately contains no
        generation-2 path, so rollback must decide from code evidence rather
        than from the synthetic release id.
        """
        release = self.home / ".local/lib/session-kit/releases" / release_id
        self.unseal_release(release)
        state_io = release / "lib/sessionkit_inventory/state_io.py"
        state_io.chmod(0o644)
        state_io.write_text(
            '"""Pre-generation-2 StateLock fixture."""\n\n'
            '_PUBLISHING_LOCK_NAME = "inventory.lock"\n\n'
            "class StateLock:\n"
            "    pass\n",
            encoding="utf-8",
        )
        status_command = release / "bin/shpool_status"
        status_command.chmod(0o755)
        status_command.write_text(
            """#!/usr/bin/env python3
import fcntl
import os
from pathlib import Path
import stat
import sys

if sys.argv[1:] != ["--json"]:
    raise SystemExit(2)
root = Path(os.environ["SESSION_KIT_STATE_DIR"])
lock = root / "inventory.lock"
flags = os.O_RDWR | os.O_CREAT
if hasattr(os, "O_CLOEXEC"):
    flags |= os.O_CLOEXEC
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(lock, flags, 0o600)
try:
    opened = os.fstat(descriptor)
    before = lock.lstat()
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_uid != os.geteuid()
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise SystemExit(1)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    after = lock.lstat()
    if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
        raise SystemExit(1)
    sys.stdout.write((root / "inventory.json").read_text(encoding="utf-8"))
finally:
    os.close(descriptor)
""",
            encoding="utf-8",
        )
        self.seal_release(release)
        return release

    def install_mixed_modern_legacy_history(self) -> None:
        self.run_installer("--non-interactive")
        self.stage_legacy_release(RELEASE_A, RELEASE_C)
        changed = self.changed_claude_source_fixture()
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(changed))
        self.installed("rollback", "--to", RELEASE_A)
        self.installed("rollback", "--to", RELEASE_C)

    def helper_launcher_names(self) -> list[str]:
        """The helper commands the installer keeps, read from the source."""
        source = (REPO / "bin/session-kit").read_text(encoding="utf-8")
        match = re.search(r"^helpers=\((?P<names>[^)]*)\)$", source, re.MULTILINE)
        assert match is not None, "bin/session-kit no longer declares helpers"
        return match.group("names").split()

    def unseal_release(self, release: Path) -> None:
        for directory in sorted(
            (path for path in release.rglob("*") if path.is_dir()), reverse=True
        ):
            directory.chmod(0o755)
        release.chmod(0o755)

    def seal_release(self, release: Path) -> None:
        """Restore the modes and manifest `write_release_manifest` leaves.

        `verify_release` recomputes every digest and refuses a release whose
        directories are writable, so a fixture that edits a sealed release has
        to leave it exactly as the installer would have.
        """
        manifest = release / "MANIFEST.sha256"
        manifest.chmod(0o644)
        lines = [
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
            f"{path.relative_to(release).as_posix()}"
            for path in sorted(release.rglob("*"))
            if path.is_file() and path != manifest
        ]
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for path in sorted(release.rglob("*")):
            if path.is_file():
                path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
        for directory in sorted(
            (path for path in release.rglob("*") if path.is_dir()), reverse=True
        ):
            directory.chmod(0o555)
        release.chmod(0o555)

    def install_legacy_generation(self) -> None:
        """Leave the installation an older release left behind.

        The rollback proofs below all begin from an installation created by a
        release older than the release-anchored manager. That generation
        differed in two ways this installer still has to cope with: nothing
        anchors management to a release, and the management command is a copy
        of the release's own `bin/session-kit` rather than the stable launcher.

        The fixture reproduces that state instead of installing the old source
        tree, which is reachable from no public clone, so the proofs run
        everywhere. Every property the later assertions depend on is checked
        here, so a future change to the installer cannot quietly turn these
        into tests of an ordinary modern installation.
        """
        self.run_installer("--non-interactive")
        root = self.home / ".local/lib/session-kit"
        release = root / "releases" / RELEASE_A

        self.unseal_release(release)
        # An older release also carries older launchers. Stamping them keeps
        # the byte comparisons that prove which release a launcher came from
        # able to tell the two releases apart.
        for relative in ("bin/session-kit", "deploy/session-kit-launcher"):
            stamped = release / relative
            stamped.chmod(0o755)
            shebang, _, body = stamped.read_text(encoding="utf-8").partition("\n")
            stamped.write_text(
                f"{shebang}\n{LEGACY_LAUNCHER_STAMP}{body}", encoding="utf-8"
            )
        self.seal_release(release)

        (root / "manager").unlink()
        launcher = self.home / ".local/bin/session-kit"
        launcher.write_bytes((release / "bin/session-kit").read_bytes())
        launcher.chmod(0o755)
        for helper in self.helper_launcher_names():
            helper_path = self.home / ".local/bin" / helper
            if not helper_path.exists():
                continue
            helper_path.write_bytes(
                (release / "deploy/session-kit-launcher").read_bytes()
            )
            helper_path.chmod(0o755)
        for directory in (self.home / ".claude", self.home / ".codex"):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()

        # The state the rollback proofs are written against, asserted rather
        # than assumed.
        self.assertFalse((root / "manager").exists())
        self.assertEqual(RELEASE_A, (root / "current").resolve().name)
        self.assertEqual(
            (release / "bin/session-kit").read_bytes(), launcher.read_bytes()
        )
        self.assertNotEqual(
            (release / "deploy/session-kit-launcher").read_bytes(),
            (REPO / "deploy/session-kit-launcher").read_bytes(),
        )

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
        self.assertIn("OK    source             installable", result.stdout)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_check_reports_direct_and_fallback_systemd_transports(self) -> None:
        source = self.clean_source_fixture()
        log = self.write_systemctl_probe_fixture()
        env = self.env.copy()
        env.pop("SESSION_KIT_TESTING")
        env.pop("SESSION_KIT_RELEASE_ID")
        env["SESSION_KIT_SYSTEMCTL_LOG"] = str(log)

        cases = (
            (
                "0",
                "1",
                0,
                "OK    services           systemd user manager is available",
                ["--user show-environment"],
            ),
            (
                "1",
                "0",
                0,
                "WARN  services           systemd user manager is available through "
                "local-machine transport; direct user socket is unavailable",
                ["--user show-environment", "--user --machine=@.host show-environment"],
            ),
            (
                "1",
                "1",
                1,
                "FAIL  services           systemd user manager is unavailable",
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
                self.assertIn(
                    f"FAIL  source-files       missing from the source: {relative}",
                    result.stdout,
                )
                path.write_bytes(payload)

    # The Codex version this branch's finding was measured against. If Codex
    # is upgraded, the test below fails ON PURPOSE: someone has to re-measure
    # rather than assume the finding still holds.
    MEASURED_CODEX_VERSION = "0.145.0"

    def test_codex_gets_no_kit_file_because_codex_cannot_use_one(self) -> None:
        """Codex has no `/kit`, and installing a file that pretends otherwise
        is worse than installing none.

        Measured 2026-08-15 against codex-cli 0.145.0, three ways:

          * its app-server protocol has 54 client methods and NO channel for a
            custom command -- `skills/list` is the only invoke-by-name surface;
          * `skills/list` against a home holding a full `prompts/` directory
            returned 28 skills, none of them from `prompts/`;
          * with a `skills/kit/SKILL.md` present and reported by `skills/list`
            as enabled, the real TUI driven in remote mode drew a slash menu of
            built-ins only -- `/model /fast /ide /permissions /keymap /vim
            /experimental /approve` -- and `/ki` rendered `no matches`.

        So `~/.codex/prompts/kit.md` was a file nothing ever read, and the docs
        repeated its existence as a capability. Ctrl-q is the Codex answer and
        needs no file at all. An older Codex did honour `prompts/`, which is
        why this used to be true.
        """
        claude = self.home / ".claude"
        claude.mkdir()
        codex = self.home / ".codex"
        codex.mkdir()
        accounts = self.home / ".local/share/session-kit/accounts"
        (accounts / "codex" / "wren").mkdir(parents=True)

        self.run_installer("--non-interactive")

        # Claude still gets the verb; Codex gets nothing, anywhere.
        self.assertTrue((claude / "commands" / "kit.md").is_file())
        self.assertFalse((codex / "prompts" / "kit.md").exists())
        self.assertFalse((codex / "prompts").exists())
        self.assertFalse(
            (accounts / "codex" / "wren" / "prompts" / "kit.md").exists()
        )

    def test_the_codex_finding_is_pinned_to_the_version_it_was_measured_on(
        self,
    ) -> None:
        """A version bump must force a re-measurement, not an assumption.

        The test above encodes a fact about codex-cli 0.145.0's behaviour, not
        a law. If Codex ever restores custom slash commands, the right response
        is to measure it again -- protocol methods, then the menu the real
        client draws -- and give the verb back. This is the tripwire that makes
        someone look.
        """
        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("no codex on this machine to measure")
        result = subprocess.run(
            [codex, "--version"], capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            self.skipTest(f"codex --version failed: {result.stderr.strip()}")
        version = result.stdout.strip()
        self.assertIn(
            self.MEASURED_CODEX_VERSION,
            version,
            "Codex changed version since the /kit finding was measured "
            f"({version!r} is not {self.MEASURED_CODEX_VERSION}). Re-measure "
            "before trusting test_codex_gets_no_kit_file_because_codex_cannot_"
            "use_one: check whether the app-server protocol has gained a "
            "custom-command channel, and drive the real TUI to read its slash "
            "menu. If /kit works again, restore the verb and this pin.",
        )

    def test_only_the_typed_kit_verb_may_ever_detach(self) -> None:
        """The bare word `kit` must do nothing.

        Claude Code merged custom commands into skills, so a file in
        `commands/` is model-invocable by default. This one's body opens with
        an injected `!`shpool detach``, which runs when the skill content is
        RENDERED -- before the model reads any of it -- and `allowed-tools`
        pre-approves the command, so nothing prompts. An operator typed the
        bare word `kit` as an ordinary message, the model matched it against
        the description, and their terminal detached mid-turn while output was
        still painting (2026-08-15).

        `disable-model-invocation: true` is the documented field that keeps
        `/kit` working while removing the description from the model's context
        entirely, so there is nothing left to match. Asserted on the INSTALLED
        file, because that is the one a provider reads.
        """
        claude = self.home / ".claude"
        claude.mkdir()
        accounts = self.home / ".local/share/session-kit/accounts"
        (accounts / "claude" / "work").mkdir(parents=True)

        self.run_installer("--non-interactive")

        for path in (
            claude / "commands" / "kit.md",
            accounts / "claude" / "work" / "commands" / "kit.md",
        ):
            with self.subTest(path=str(path)):
                body = path.read_text(encoding="utf-8")
                front = body.split("---")[1]
                self.assertIn("disable-model-invocation: true", front)
                # The injected command is still there -- the fix is the gate
                # on WHO may fire it, not the removal of what it does.
                self.assertIn("!`shpool detach`", body)
                # And the typed verb still works: the file is still a command
                # named kit, with its tool grant intact.
                self.assertEqual("kit.md", path.name)
                self.assertIn("allowed-tools: Bash(shpool detach:*)", front)

    def test_the_kit_verb_reaches_every_enrolled_account_profile(self) -> None:
        """A session the kit launches on an account reads that account's
        commands, not ~/.claude's: CLAUDE_CONFIG_DIR replaces the config root
        rather than adding to it. A verb installed only in the home directory
        is invisible in exactly the sessions it exists for — which is what the
        live drill found, with /kit unknown inside a managed session."""
        (self.home / ".claude").mkdir()
        accounts = self.home / ".local/share/session-kit/accounts"
        for alias in ("work", "spare"):
            (accounts / "claude" / alias).mkdir(parents=True)
        (accounts / "codex" / "wren").mkdir(parents=True)

        self.run_installer("--non-interactive")

        for alias in ("work", "spare"):
            path = accounts / "claude" / alias / "commands" / "kit.md"
            with self.subTest(alias=alias):
                self.assertTrue(path.is_file(), path)
                self.assertIn("shpool detach", path.read_text(encoding="utf-8"))
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
        # Codex profiles get nothing: measured 2026-08-15, codex-cli 0.145.0
        # never reads prompts/ and has no custom slash namespace, so a file
        # here would be a claim rather than a capability. See
        # test_codex_gets_no_kit_file_because_codex_cannot_use_one.
        self.assertFalse((accounts / "codex" / "wren" / "prompts").exists())

    def test_the_kit_verb_skips_a_provider_that_is_not_installed(self) -> None:
        # No provider home is not a failure to report: it is a provider that
        # is not on this machine. (The Codex home is not a fair test of this:
        # the kit creates it itself for the window themes.)
        self.run_installer("--non-interactive")
        self.assertFalse((self.home / ".claude" / "commands").exists())

    def test_the_kit_verb_ignores_the_account_profile_of_the_running_session(
        self,
    ) -> None:
        """An install run from inside a managed session inherits that
        session's account profile in CLAUDE_CONFIG_DIR. A machine-wide verb
        installed into one rotating profile is invisible to the next account,
        and writes into a directory the installer was never asked to touch."""
        claude = self.home / ".claude"
        claude.mkdir()
        profile = self.home / "account-profile"
        (profile / "commands").mkdir(parents=True)
        environment = dict(self.env, CLAUDE_CONFIG_DIR=str(profile))
        result = subprocess.run(
            [str(REPO / "install.sh"), "--non-interactive"],
            cwd=REPO,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((claude / "commands" / "kit.md").is_file())
        self.assertFalse((profile / "commands" / "kit.md").exists())

    def test_the_kit_verb_never_writes_through_a_symlink(self) -> None:
        claude = self.home / ".claude"
        claude.mkdir()
        commands = claude / "commands"
        commands.mkdir(mode=0o700)
        elsewhere = self.home / "not-a-command.md"
        elsewhere.write_text("untouched\n", encoding="utf-8")
        (commands / "kit.md").symlink_to(elsewhere)

        self.run_installer("--non-interactive")

        self.assertTrue((commands / "kit.md").is_symlink())
        self.assertEqual("untouched\n", elsewhere.read_text(encoding="utf-8"))

    @unittest.skipUnless(
        host_group_is_private(), "this host does not give the account a private group"
    )
    def test_install_accepts_a_group_writable_provider_directory(self) -> None:
        # A private-group distribution with a 002 umask leaves the provider
        # config directory at 0775, which exposes it to no other account. The
        # install still writes through it -- the /kit verb is the write that
        # remains -- and leaves the operator's own mode alone.
        claude = self.home / ".claude"
        claude.mkdir(mode=0o775)
        claude.chmod(0o775)

        checked = self.run_installer("--check")
        self.assertIn("OK    source             installable", checked.stdout)
        self.run_installer("--non-interactive")

        self.assertTrue((claude / "commands" / "kit.md").is_file())
        self.assertEqual(0o775, claude.stat().st_mode & 0o777)

    @unittest.skipUnless(
        host_group_is_private(), "this host does not give the account a private group"
    )
    def test_install_accepts_a_group_writable_codex_home(self) -> None:
        # The Codex CLI leaves ~/.codex group-writable on the same
        # private-group distributions, and the theme layout owns that path.
        codex = self.home / ".codex"
        codex.mkdir(mode=0o700)
        codex.chmod(0o775)

        checked = self.run_installer("--check")
        self.assertIn(
            "OK    codex-home         the theme layout is owner-controlled",
            checked.stdout,
        )
        self.run_installer("--non-interactive")

        self.assertTrue((codex / "themes/sk-red.tmTheme").is_file())
        self.assertFalse((codex / "prompts").exists())
        self.assertEqual(0o775, codex.stat().st_mode & 0o777)

    @unittest.skipUnless(
        host_group_is_private(), "this host does not give the account a private group"
    )
    def test_interrupted_install_recovers_through_group_writable_directories(
        self,
    ) -> None:
        # Recovery revalidates every recorded provider-config and theme
        # ancestor, so it has to accept the same directories the install
        # accepted or an interrupted install can never be finished.
        claude = self.home / ".claude"
        claude.mkdir(mode=0o700)
        claude.chmod(0o775)
        codex = self.home / ".codex"
        codex.mkdir(mode=0o700)
        codex.chmod(0o775)
        journal = self.home / ".local/state/session-kit/lifecycle-transaction.json"

        self.env["SESSION_KIT_TEST_FAILPOINT"] = "themes"
        self.env["SESSION_KIT_TEST_FAILPOINT_MODE"] = "kill"
        interrupted = self.run_installer("--non-interactive", check=False)
        self.assertEqual(-signal.SIGKILL, interrupted.returncode)
        self.assertTrue(journal.is_file())

        self.env.pop("SESSION_KIT_TEST_FAILPOINT")
        self.env.pop("SESSION_KIT_TEST_FAILPOINT_MODE")
        recovered = self.run_installer("--non-interactive")

        self.assertIn(
            "Recovered interrupted Session Kit install transaction", recovered.stdout
        )
        self.assertFalse(journal.exists())
        self.assertTrue((self.home / ".local/lib/session-kit/current").is_symlink())
        self.assertTrue((claude / "commands" / "kit.md").is_file())
        self.assertEqual(0o775, claude.stat().st_mode & 0o777)
        self.assertEqual(0o775, codex.stat().st_mode & 0o777)

    def test_world_writable_provider_directory_is_never_written_through(
        self,
    ) -> None:
        """A directory any account can write is never a write target.

        Provider commands still treat an unavailable provider as absent, but
        the required Claude status line and hook may not be silently skipped.
        The overall install therefore refuses and names the unsafe directory.
        """
        claude = self.home / ".claude"
        claude.mkdir(mode=0o700)
        claude.chmod(0o777)

        self.run_installer("--check")
        refused = self.run_installer("--non-interactive", check=False)

        self.assertNotEqual(0, refused.returncode)
        self.assertIn("Claude home is not an owner-controlled directory", refused.stderr)
        self.assertEqual(0o777, claude.stat().st_mode & 0o777)
        self.assertEqual([], sorted(path.name for path in claude.iterdir()))
        self.assertFalse((claude / "settings.json").exists())
        self.assertFalse((claude / "commands").exists())

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
        reset_tool = current / "bin/reset-collection-order.py"
        self.assertTrue(reset_tool.is_file())
        self.assertTrue(reset_tool.stat().st_mode & 0o111)
        self.assertTrue(
            (current / "extras/statusline-quota-refresh.example").is_file()
        )
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

    def test_install_registers_claude_integration_in_every_account_profile(self) -> None:
        accounts = self.home / ".local/share/session-kit/accounts/claude"
        for alias in ("one", "two"):
            profile = accounts / alias
            profile.mkdir(parents=True)
            (profile / "settings.json").write_text(
                json.dumps({"keep": alias}), encoding="utf-8"
            )

        self.run_installer("--non-interactive")

        for settings_path in (
            self.home / ".claude/settings.json",
            accounts / "one/settings.json",
            accounts / "two/settings.json",
        ):
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(
                settings["statusLine"],
                {
                    "type": "command",
                    "command": "~/.claude/statusline.sh",
                    "refreshInterval": 2,
                },
            )
            for event in ("SessionStart", "UserPromptSubmit", "Stop"):
                commands = [
                    hook.get("command")
                    for group in settings["hooks"][event]
                    for hook in group["hooks"]
                    if isinstance(hook, dict)
                ]
                self.assertIn("~/.claude/hooks/nameintent_title.sh", commands)

        result = self.installed("doctor", "--json")
        names = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual("ok", names["naming-hook"]["status"])
        self.assertEqual("ok", names["statusline"]["status"])

        (self.home / ".claude/statusline.sh").write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8"
        )
        drifted = self.installed("doctor", "--json")
        drifted_names = {
            row["name"]: row for row in json.loads(drifted.stdout)["checks"]
        }
        self.assertEqual("warn", drifted_names["statusline"]["status"])
        self.assertIn("content", drifted_names["statusline"]["detail"])

    def test_install_refuses_unrelated_claude_statusline_without_force(self) -> None:
        settings_path = self.home / ".claude/settings.json"
        settings_path.parent.mkdir()
        original_status = {
            "type": "command",
            "command": "/opt/operator/statusline",
            "refreshInterval": 9,
        }
        settings_path.write_text(
            json.dumps({"statusLine": original_status}) + "\n", encoding="utf-8"
        )
        settings_path.chmod(0o600)

        refused = self.run_installer("--non-interactive", check=False)

        self.assertNotEqual(0, refused.returncode)
        self.assertIn("refusing unrelated Claude statusLine", refused.stderr)
        self.assertIn("--force", refused.stderr)
        self.assertEqual(
            original_status,
            json.loads(settings_path.read_text(encoding="utf-8"))["statusLine"],
        )
        self.assertFalse((self.home / ".local/lib/session-kit/current").exists())
        self.assertFalse(
            (self.home / ".local/state/session-kit/lifecycle-transaction.json").exists()
        )

    def test_update_statusline_refusal_happens_before_release_transaction(self) -> None:
        self.run_installer("--non-interactive")
        root = self.home / ".local/lib/session-kit"
        current = root / "current"
        manager = root / "manager"
        receipt = self.home / ".local/state/session-kit/install.json"
        settings_path = self.home / ".claude/settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        custom_status = {
            "type": "command",
            "command": "/opt/operator/update-status",
        }
        settings["statusLine"] = custom_status
        settings_path.write_text(json.dumps(settings) + "\n", encoding="utf-8")
        receipt_before = receipt.read_bytes()
        releases_before = sorted(path.name for path in (root / "releases").iterdir())

        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        refused = self.installed(
            "update", "--source", str(REPO), check=False
        )

        self.assertNotEqual(0, refused.returncode)
        self.assertIn("refusing unrelated Claude statusLine", refused.stderr)
        self.assertEqual(RELEASE_A, current.resolve().name)
        self.assertEqual(RELEASE_A, manager.resolve().name)
        self.assertEqual(receipt_before, receipt.read_bytes())
        self.assertEqual(
            releases_before,
            sorted(path.name for path in (root / "releases").iterdir()),
        )
        self.assertEqual(
            custom_status,
            json.loads(settings_path.read_text(encoding="utf-8"))["statusLine"],
        )
        self.assertFalse(
            (self.home / ".local/state/session-kit/lifecycle-transaction.json").exists()
        )

    def test_update_rejects_malformed_backup_entry_before_release_flip(self) -> None:
        self.run_installer("--non-interactive")
        root = self.home / ".local/lib/session-kit"
        current = root / "current"
        manager = root / "manager"
        receipt = self.home / ".local/state/session-kit/install.json"
        backups = self.home / ".local/state/session-kit/claude-statusline-backups.json"
        settings = self.home / ".claude/settings.json"
        backups.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": {str(settings): {"present": "not-bool"}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        receipt_before = receipt.read_bytes()
        releases_before = sorted(path.name for path in (root / "releases").iterdir())

        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        refused = self.installed("update", "--source", str(REPO), check=False)

        self.assertNotEqual(0, refused.returncode)
        self.assertIn("invalid Claude status-line backup", refused.stderr)
        self.assertEqual(RELEASE_A, current.resolve().name)
        self.assertEqual(RELEASE_A, manager.resolve().name)
        self.assertEqual(receipt_before, receipt.read_bytes())
        self.assertEqual(
            releases_before,
            sorted(path.name for path in (root / "releases").iterdir()),
        )
        self.assertFalse(
            (self.home / ".local/state/session-kit/lifecycle-transaction.json").exists()
        )

    def test_enrolled_profile_statusline_is_preserved_without_blocking_install(self) -> None:
        profile = (
            self.home
            / ".local/share/session-kit/accounts/claude/independent"
        )
        profile.mkdir(parents=True)
        custom_status = {
            "type": "command",
            "command": "/opt/profile/statusline",
            "refreshInterval": 7,
        }
        settings_path = profile / "settings.json"
        settings_path.write_text(
            json.dumps({"keep": True, "statusLine": custom_status}) + "\n",
            encoding="utf-8",
        )

        installed = self.run_installer("--non-interactive")

        self.assertEqual(0, installed.returncode, installed.stderr)
        self.assertIn(
            f"skipping Claude statusLine rewrite in {settings_path}",
            installed.stderr,
        )
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(custom_status, settings["statusLine"])
        self.assertTrue(settings["keep"])
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            commands = [
                hook.get("command")
                for group in settings["hooks"][event]
                for hook in group["hooks"]
                if isinstance(hook, dict)
            ]
            self.assertIn("~/.claude/hooks/nameintent_title.sh", commands)

    def test_force_install_restores_preexisting_statusline_on_uninstall(self) -> None:
        settings_path = self.home / ".claude/settings.json"
        settings_path.parent.mkdir()
        original_status = {
            "type": "command",
            "command": "/opt/operator/statusline",
            "padding": {"left": 1},
        }
        settings_path.write_text(
            json.dumps({"keep": True, "statusLine": original_status}) + "\n",
            encoding="utf-8",
        )
        settings_path.chmod(0o600)

        self.run_installer("--non-interactive", "--force")
        installed = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual("~/.claude/statusline.sh", installed["statusLine"]["command"])

        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        self.installed("uninstall")

        restored = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertTrue(restored["keep"])
        self.assertEqual(original_status, restored["statusLine"])
        self.assertFalse(
            (self.home / ".local/state/session-kit/claude-statusline-backups.json").exists()
        )

    def test_force_update_preserves_statusline_set_after_install(self) -> None:
        settings_path = self.home / ".claude/settings.json"
        self.run_installer("--non-interactive")
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        operator_status = {
            "type": "command",
            "command": "/opt/operator/after-install",
        }
        settings["statusLine"] = operator_status
        settings_path.write_text(json.dumps(settings) + "\n", encoding="utf-8")

        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO), "--force")
        self.installed("uninstall")

        restored = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(operator_status, restored["statusLine"])

    def test_force_update_refreshes_changed_operator_statusline_backup(self) -> None:
        settings_path = self.home / ".claude/settings.json"
        settings_path.parent.mkdir()
        first_status = {
            "type": "command",
            "command": "/opt/operator/first",
        }
        settings_path.write_text(
            json.dumps({"statusLine": first_status}) + "\n", encoding="utf-8"
        )
        self.run_installer("--non-interactive", "--force")

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        current_status = {
            "type": "command",
            "command": "/opt/operator/current",
        }
        settings["statusLine"] = current_status
        settings_path.write_text(json.dumps(settings) + "\n", encoding="utf-8")

        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO), "--force")
        self.installed("uninstall")

        restored = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(current_status, restored["statusLine"])

    def test_force_update_preserves_enrolled_statusline_set_after_install(self) -> None:
        profile = self.home / ".local/share/session-kit/accounts/claude/independent"
        profile.mkdir(parents=True)
        settings_path = profile / "settings.json"
        settings_path.write_text('{"keep":true}\n', encoding="utf-8")
        self.run_installer("--non-interactive")

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        operator_status = {
            "type": "command",
            "command": "/opt/operator/enrolled-current",
        }
        settings["statusLine"] = operator_status
        settings_path.write_text(json.dumps(settings) + "\n", encoding="utf-8")

        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO), "--force")
        self.installed("uninstall")

        restored = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertTrue(restored["keep"])
        self.assertEqual(operator_status, restored["statusLine"])

    def _assert_unusable_claude_settings_do_not_block_lifecycle(
        self, *, enrolled: bool, bad_kind: str
    ) -> None:
        if enrolled:
            profile = self.home / ".local/share/session-kit/accounts/claude/broken"
        else:
            profile = self.home / ".claude"
        profile.mkdir(parents=True)
        settings_path = profile / "settings.json"
        fixtures = {
            "invalid-json": (b"{\n", "invalid JSON"),
            "non-object": (b"[]\n", "top level is not an object"),
            "bad-hooks-group": (
                b'{"hooks":{"SessionStart":{}}}\n',
                "SessionStart hooks setting is not a list",
            ),
            "oversize": (b"x" * 1_048_577, "larger than 1 MiB"),
            "foreign-owner": (
                b'{"operator":true}\n',
                "not owned by the current user",
            ),
        }
        original, reason = fixtures[bad_kind]
        settings_path.write_bytes(original)
        settings_path.chmod(0o600)
        if bad_kind == "foreign-owner":
            foreign_uid = next(
                entry.pw_uid for entry in pwd.getpwall() if entry.pw_uid != os.geteuid()
            )
            os.chown(settings_path, foreign_uid, -1)

        def assert_skipped(result: subprocess.CompletedProcess[str]) -> None:
            self.assertEqual(0, result.returncode, result.stderr)
            matching = [
                line
                for line in result.stderr.splitlines()
                if f"skipping unusable Claude settings file {settings_path}:" in line
            ]
            self.assertEqual(1, len(matching), result.stderr)
            self.assertIn(reason, matching[0])
            self.assertEqual(original, settings_path.read_bytes())

        assert_skipped(self.run_installer("--non-interactive", check=False))
        root = self.home / ".local/lib/session-kit"
        self.assertEqual(RELEASE_A, (root / "current").resolve().name)

        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        assert_skipped(
            self.installed("update", "--source", str(REPO), check=False)
        )
        self.assertEqual(RELEASE_B, (root / "current").resolve().name)

        assert_skipped(self.installed("rollback", "--to", RELEASE_A, check=False))
        self.assertEqual(RELEASE_A, (root / "current").resolve().name)

    def test_invalid_json_default_settings_do_not_block_lifecycle(self) -> None:
        self._assert_unusable_claude_settings_do_not_block_lifecycle(
            enrolled=False, bad_kind="invalid-json"
        )

    def test_invalid_json_enrolled_settings_do_not_block_lifecycle(self) -> None:
        self._assert_unusable_claude_settings_do_not_block_lifecycle(
            enrolled=True, bad_kind="invalid-json"
        )

    def test_non_object_default_settings_do_not_block_lifecycle(self) -> None:
        self._assert_unusable_claude_settings_do_not_block_lifecycle(
            enrolled=False, bad_kind="non-object"
        )

    def test_non_object_enrolled_settings_do_not_block_lifecycle(self) -> None:
        self._assert_unusable_claude_settings_do_not_block_lifecycle(
            enrolled=True, bad_kind="non-object"
        )

    def test_bad_hooks_default_settings_do_not_block_lifecycle(self) -> None:
        self._assert_unusable_claude_settings_do_not_block_lifecycle(
            enrolled=False, bad_kind="bad-hooks-group"
        )

    def test_bad_hooks_enrolled_settings_do_not_block_lifecycle(self) -> None:
        self._assert_unusable_claude_settings_do_not_block_lifecycle(
            enrolled=True, bad_kind="bad-hooks-group"
        )

    def test_oversize_default_settings_do_not_block_lifecycle(self) -> None:
        self._assert_unusable_claude_settings_do_not_block_lifecycle(
            enrolled=False, bad_kind="oversize"
        )

    def test_oversize_enrolled_settings_do_not_block_lifecycle(self) -> None:
        self._assert_unusable_claude_settings_do_not_block_lifecycle(
            enrolled=True, bad_kind="oversize"
        )

    @unittest.skipUnless(os.geteuid() == 0, "requires permission to create a foreign owner")
    def test_foreign_owned_default_settings_do_not_block_lifecycle(self) -> None:
        self._assert_unusable_claude_settings_do_not_block_lifecycle(
            enrolled=False, bad_kind="foreign-owner"
        )

    @unittest.skipUnless(os.geteuid() == 0, "requires permission to create a foreign owner")
    def test_foreign_owned_enrolled_settings_do_not_block_lifecycle(self) -> None:
        self._assert_unusable_claude_settings_do_not_block_lifecycle(
            enrolled=True, bad_kind="foreign-owner"
        )

    def test_preledger_kit_statusline_is_removed_on_uninstall(self) -> None:
        settings_path = self.home / ".claude/settings.json"
        settings_path.parent.mkdir()
        settings_path.write_text(
            json.dumps(
                {
                    "keep": True,
                    "statusLine": {
                        "type": "command",
                        "command": "~/.claude/statusline.sh",
                        "refreshInterval": 2,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        settings_path.chmod(0o600)

        self.run_installer("--non-interactive")
        self.installed("uninstall")

        restored = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertTrue(restored["keep"])
        self.assertNotIn("statusLine", restored)

    def test_install_fails_when_claude_integration_directory_is_unsafe(self) -> None:
        claude_home = self.home / ".claude"
        claude_home.mkdir(mode=0o700)
        hooks = claude_home / "hooks"
        hooks.mkdir(mode=0o777)
        hooks.chmod(0o777)

        refused = self.run_installer("--non-interactive", check=False)

        self.assertNotEqual(0, refused.returncode)
        self.assertIn("Claude hooks directory is not owner-controlled", refused.stderr)

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
        floor = self.home / ".local/state/session-kit/collection-sequence-floor.json"
        floor_bytes = floor.read_bytes()
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        current = self.home / ".local/lib/session-kit/current"
        self.assertEqual(current.resolve().name, RELEASE_B)
        self.installed("rollback")
        self.assertEqual(current.resolve().name, RELEASE_A)
        self.assertEqual(floor_bytes, floor.read_bytes())

    def test_rollback_unfences_a_code_proven_legacy_release_for_a_fresh_picker(
        self,
    ) -> None:
        self.run_installer("--non-interactive")
        legacy = self.make_release_fence_unaware(RELEASE_A)
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))

        state = Path(self.env["SESSION_KIT_STATE_DIR"])
        inventory = state / "inventory.json"
        inventory.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sessions": [
                        {"display_title": "Alpha", "shpool_id_raw": "human-alpha"},
                        {"display_title": "Beta", "shpool_id_raw": "human-beta"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        inventory.chmod(0o600)
        sentinel = state / "inventory.lock"
        sentinel.write_bytes(b"session-kit publishing lock generation 2\n")
        sentinel.chmod(0o400)

        refused = subprocess.run(
            [str(legacy / "bin/shpool_status"), "--json"],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertEqual("", refused.stdout)

        rolled_back = self.installed("rollback", "--to", RELEASE_A)

        self.assertIn("Rolled back Session Kit", rolled_back.stdout)
        self.assertEqual(0o600, stat.S_IMODE(sentinel.stat().st_mode))
        self.assertEqual(b"", sentinel.read_bytes())
        fresh = subprocess.run(
            [str(self.home / ".local/bin/shpool_status"), "--json"],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, fresh.returncode, fresh.stderr)
        self.assertEqual(
            ["human-alpha", "human-beta"],
            [row["shpool_id_raw"] for row in json.loads(fresh.stdout)["sessions"]],
        )

    def test_rollback_keeps_the_fence_for_a_code_proven_aware_release(self) -> None:
        self.run_installer("--non-interactive")
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        state = Path(self.env["SESSION_KIT_STATE_DIR"])
        sentinel = state / "inventory.lock"
        expected = b"session-kit publishing lock generation 2\n"
        sentinel.write_bytes(expected)
        sentinel.chmod(0o400)

        self.installed("rollback", "--to", RELEASE_A)

        current = self.home / ".local/lib/session-kit/current"
        self.assertEqual(RELEASE_A, current.resolve().name)
        self.assertEqual(0o400, stat.S_IMODE(sentinel.stat().st_mode))
        self.assertEqual(expected, sentinel.read_bytes())

    def test_crash_after_unfence_cannot_select_legacy_code_behind_the_fence(
        self,
    ) -> None:
        self.run_installer("--non-interactive")
        self.make_release_fence_unaware(RELEASE_A)
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        state = Path(self.env["SESSION_KIT_STATE_DIR"])
        sentinel = state / "inventory.lock"
        sentinel.write_bytes(b"session-kit publishing lock generation 2\n")
        sentinel.chmod(0o400)
        self.env["SESSION_KIT_TEST_FAILPOINT"] = "rollback-unfenced"
        self.env["SESSION_KIT_TEST_FAILPOINT_MODE"] = "kill"

        killed = self.installed("rollback", "--to", RELEASE_A, check=False)

        self.assertEqual(-signal.SIGKILL, killed.returncode)
        current = self.home / ".local/lib/session-kit/current"
        self.assertEqual(RELEASE_B, current.resolve().name)
        self.assertEqual(0o600, stat.S_IMODE(sentinel.stat().st_mode))
        self.env.pop("SESSION_KIT_TEST_FAILPOINT")
        self.env.pop("SESSION_KIT_TEST_FAILPOINT_MODE")
        recovered = self.installed("rollback", "--to", RELEASE_A)
        self.assertIn("Recovered interrupted Session Kit rollback", recovered.stdout)
        self.assertEqual(RELEASE_A, current.resolve().name)
        self.assertEqual(0o600, stat.S_IMODE(sentinel.stat().st_mode))

    def test_generation_two_publisher_cannot_refence_during_legacy_rollback(
        self,
    ) -> None:
        self.run_installer("--non-interactive")
        self.make_release_fence_unaware(RELEASE_A)
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        state = Path(self.env["SESSION_KIT_STATE_DIR"])
        sentinel = state / "inventory.lock"
        sentinel.write_bytes(b"session-kit publishing lock generation 2\n")
        sentinel.chmod(0o400)
        events: list[str] = []

        def publish_in_gap() -> None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if stat.S_IMODE(sentinel.stat().st_mode) == 0o600:
                        events.append("unfenced")
                        try:
                            with StateLock(state, state / "inventory-v2.lock"):
                                events.append("published")
                        except CollectionError as exc:
                            events.append(str(exc))
                        return
                except OSError:
                    pass
            events.append("unfence-not-observed")

        publisher = threading.Thread(
            target=publish_in_gap, name="generation-two-publisher"
        )
        publisher.start()
        rolled_back = self.installed("rollback", "--to", RELEASE_A)
        publisher.join(12)

        self.assertFalse(publisher.is_alive())
        self.assertIn("Rolled back Session Kit", rolled_back.stdout)
        self.assertEqual(
            RELEASE_A,
            (self.home / ".local/lib/session-kit/current").resolve().name,
        )
        self.assertEqual(0o600, stat.S_IMODE(sentinel.stat().st_mode))
        self.assertNotIn("published", events)
        self.assertTrue(
            any("legacy rollback selected" in event for event in events), events
        )

    def test_forward_update_retires_the_legacy_rollback_hold(self) -> None:
        self.run_installer("--non-interactive")
        self.make_release_fence_unaware(RELEASE_A)
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        state = Path(self.env["SESSION_KIT_STATE_DIR"])
        inventory = state / "inventory.json"
        inventory.write_text(
            '{"schema_version":1,"sessions":[]}\n', encoding="utf-8"
        )
        inventory.chmod(0o600)
        sentinel = state / "inventory.lock"
        sentinel.write_bytes(b"session-kit publishing lock generation 2\n")
        sentinel.chmod(0o400)

        self.installed("rollback", "--to", RELEASE_A)

        hold = state / "inventory-rollback-v2"
        self.assertTrue(hold.is_file())
        self.assertEqual(0o400, stat.S_IMODE(hold.stat().st_mode))
        self.assertEqual(
            b"session-kit legacy rollback selected\n", hold.read_bytes()
        )
        self.assertEqual(0o600, stat.S_IMODE(sentinel.stat().st_mode))

        self.installed("update", "--source", str(REPO))
        refreshed = subprocess.run(
            [str(self.home / ".local/bin/shpool_status"), "--json"],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(0, refreshed.returncode, refreshed.stderr)
        self.assertFalse(hold.exists())
        self.assertEqual(0o400, stat.S_IMODE(sentinel.stat().st_mode))

    def test_each_legacy_rollback_fence_kill_point_leaves_a_safe_pair(self) -> None:
        cases = (
            ("rollback-fence-marked", RELEASE_B, 0o400),
            ("rollback-unfenced", RELEASE_B, 0o600),
            ("current", RELEASE_A, 0o600),
        )
        for failpoint, selected, lock_mode in cases:
            with self.subTest(failpoint=failpoint):
                try:
                    self.run_installer("--non-interactive")
                    self.make_release_fence_unaware(RELEASE_A)
                    self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
                    self.installed("update", "--source", str(REPO))
                    state = Path(self.env["SESSION_KIT_STATE_DIR"])
                    inventory = state / "inventory.json"
                    inventory.write_text(
                        '{"schema_version":1,"sessions":[]}\n', encoding="utf-8"
                    )
                    inventory.chmod(0o600)
                    sentinel = state / "inventory.lock"
                    sentinel.write_bytes(
                        b"session-kit publishing lock generation 2\n"
                    )
                    sentinel.chmod(0o400)
                    self.env["SESSION_KIT_TEST_FAILPOINT"] = failpoint
                    self.env["SESSION_KIT_TEST_FAILPOINT_MODE"] = "kill"

                    killed = self.installed(
                        "rollback", "--to", RELEASE_A, check=False
                    )

                    self.assertEqual(-signal.SIGKILL, killed.returncode)
                    current = self.home / ".local/lib/session-kit/current"
                    self.assertEqual(selected, current.resolve().name)
                    self.assertEqual(
                        lock_mode, stat.S_IMODE(sentinel.stat().st_mode)
                    )
                    hold = state / "inventory-rollback-v2"
                    self.assertEqual(0o400, stat.S_IMODE(hold.stat().st_mode))
                    self.assertEqual(
                        b"session-kit legacy rollback selected\n",
                        hold.read_bytes(),
                    )
                    if selected == RELEASE_A:
                        fresh = subprocess.run(
                            [
                                str(self.home / ".local/bin/shpool_status"),
                                "--json",
                            ],
                            env=self.env,
                            text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            check=False,
                        )
                        self.assertEqual(0, fresh.returncode, fresh.stderr)
                finally:
                    self.env.pop("SESSION_KIT_TEST_FAILPOINT", None)
                    self.env.pop("SESSION_KIT_TEST_FAILPOINT_MODE", None)
                    self.tearDown()
                    self.setUp()

    def test_rollback_refuses_a_lookalike_fence_before_selecting_legacy_code(
        self,
    ) -> None:
        self.run_installer("--non-interactive")
        self.make_release_fence_unaware(RELEASE_A)
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        state = Path(self.env["SESSION_KIT_STATE_DIR"])
        sentinel = state / "inventory.lock"
        lookalike = b"session-kit publishing lock generation two\n"
        sentinel.write_bytes(lookalike)
        sentinel.chmod(0o400)

        refused = self.installed("rollback", "--to", RELEASE_A, check=False)

        self.assertNotEqual(0, refused.returncode)
        self.assertIn("not the exact generation-2 fence", refused.stderr)
        current = self.home / ".local/lib/session-kit/current"
        self.assertEqual(RELEASE_B, current.resolve().name)
        self.assertEqual(lookalike, sentinel.read_bytes())
        self.assertEqual(0o400, stat.S_IMODE(sentinel.stat().st_mode))

    def test_rollback_accepts_legacy_release_without_claude_payload(self) -> None:
        self.run_installer("--non-interactive")
        root = self.home / ".local/lib/session-kit"
        legacy = root / "releases" / RELEASE_A
        self.unseal_release(legacy)
        shutil.rmtree(legacy / "config/claude")
        self.seal_release(legacy)

        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        statusline = self.home / ".claude/statusline.sh"
        modern_statusline = statusline.read_bytes()

        rolled_back = self.installed("rollback", "--to", RELEASE_A)

        self.assertIn("Rolled back Session Kit", rolled_back.stdout)
        self.assertEqual(RELEASE_A, (root / "current").resolve().name)
        self.assertEqual(modern_statusline, statusline.read_bytes())

    def test_rollback_skips_statusline_drift_and_still_flips_release(self) -> None:
        self.run_installer("--non-interactive")
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        settings_path = self.home / ".claude/settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        custom_status = {
            "type": "command",
            "command": "/opt/operator/emergency-status",
        }
        settings["statusLine"] = custom_status
        settings_path.write_text(json.dumps(settings) + "\n", encoding="utf-8")

        rolled_back = self.installed("rollback", "--to", RELEASE_A)

        self.assertIn("Rolled back Session Kit", rolled_back.stdout)
        self.assertIn(
            f"skipping Claude statusLine rewrite in {settings_path}",
            rolled_back.stderr,
        )
        self.assertNotIn("--force", rolled_back.stderr)
        self.assertEqual(
            RELEASE_A,
            (self.home / ".local/lib/session-kit/current").resolve().name,
        )
        self.assertEqual(
            custom_status,
            json.loads(settings_path.read_text(encoding="utf-8"))["statusLine"],
        )

    def test_doctor_uses_integration_ledger_after_legacy_rollback(self) -> None:
        self.install_mixed_modern_legacy_history()

        doctor = self.installed("doctor", "--json")

        checks = {row["name"]: row for row in json.loads(doctor.stdout)["checks"]}
        self.assertEqual("ok", checks["naming-hook"]["status"])
        self.assertEqual("ok", checks["statusline"]["status"])
        self.assertNotIn("content", checks["naming-hook"]["detail"])
        self.assertNotIn("content", checks["statusline"]["detail"])

    def test_doctor_requires_owner_only_modes_for_claude_scripts(self) -> None:
        self.run_installer("--non-interactive")
        hook = self.home / ".claude/hooks/nameintent_title.sh"
        statusline = self.home / ".claude/statusline.sh"
        hook.chmod(0o755)
        statusline.chmod(0o755)

        doctor = self.installed("doctor", "--json")

        checks = {row["name"]: row for row in json.loads(doctor.stdout)["checks"]}
        self.assertEqual("warn", checks["naming-hook"]["status"])
        self.assertIn("hook file", checks["naming-hook"]["detail"])
        self.assertEqual("warn", checks["statusline"]["status"])
        self.assertIn("status line file", checks["statusline"]["detail"])

    def test_uninstall_removes_ledger_owned_scripts_after_legacy_rollback(self) -> None:
        self.install_mixed_modern_legacy_history()
        hook = self.home / ".claude/hooks/nameintent_title.sh"
        statusline = self.home / ".claude/statusline.sh"
        manager_statusline = (
            self.home
            / ".local/lib/session-kit/manager/config/claude/statusline.sh"
        )
        self.assertTrue(hook.is_file())
        self.assertTrue(statusline.is_file())
        self.assertNotEqual(statusline.read_bytes(), manager_statusline.read_bytes())

        self.installed("uninstall")

        self.assertFalse(hook.exists())
        self.assertFalse(statusline.exists())
        self.assertFalse(
            (self.home / ".local/state/session-kit/claude-integration.json").exists()
        )

    def test_uninstall_partial_ledger_removes_recorded_file_and_retains_ledger(self) -> None:
        self.run_installer("--non-interactive")
        hook = self.home / ".claude/hooks/nameintent_title.sh"
        statusline = self.home / ".claude/statusline.sh"
        ledger_path = self.home / ".local/state/session-kit/claude-integration.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["files"].pop(str(statusline))
        ledger_path.write_text(json.dumps(ledger) + "\n", encoding="utf-8")

        removed = self.installed("uninstall")

        self.assertFalse(hook.exists())
        self.assertTrue(statusline.is_file())
        self.assertTrue(ledger_path.is_file())
        self.assertIn("incomplete Claude integration ledger retained", removed.stderr)
        self.assertIn(str(statusline), removed.stderr)

    def test_uninstall_digest_drift_retains_only_unremoved_ledger_entry(self) -> None:
        self.run_installer("--non-interactive")
        hook = self.home / ".claude/hooks/nameintent_title.sh"
        statusline = self.home / ".claude/statusline.sh"
        ledger_path = self.home / ".local/state/session-kit/claude-integration.json"
        statusline.write_text(
            statusline.read_text(encoding="utf-8") + "\n# operator edit\n",
            encoding="utf-8",
        )

        removed = self.installed("uninstall")

        self.assertFalse(hook.exists())
        self.assertTrue(statusline.is_file())
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual({str(statusline)}, set(ledger["files"]))
        self.assertIn("Claude integration ledger retained", removed.stderr)
        self.assertIn(str(statusline), removed.stderr)

    def assert_uninstall_refusal_preserves_login(
        self, ledger_name: str, invalid_payload: object, expected_message: str
    ) -> None:
        self.run_installer("--non-interactive", "--enable-login")
        state = self.home / ".local/state/session-kit"
        (state / ledger_name).write_text(
            json.dumps(invalid_payload) + "\n", encoding="utf-8"
        )
        marker = state / "integration-ready-v1"
        bashrc = self.home / ".bashrc"
        self.assertTrue(marker.is_file())
        self.assertIn("session-kit managed integration", bashrc.read_text(encoding="utf-8"))

        refused = self.installed("uninstall", check=False)

        self.assertNotEqual(0, refused.returncode)
        self.assertIn(expected_message, refused.stderr)
        self.assertFalse((state / "lifecycle-transaction.json").exists())
        self.assertTrue(marker.is_file())
        self.assertIn("session-kit managed integration", bashrc.read_text(encoding="utf-8"))

    def test_uninstall_invalid_integration_ledger_preserves_login(self) -> None:
        self.assert_uninstall_refusal_preserves_login(
            "claude-integration.json",
            {"schema_version": 1, "release_id": RELEASE_A, "files": {"bad": "0" * 64}},
            "invalid Claude integration ledger",
        )

    def test_uninstall_invalid_statusline_backup_preserves_login(self) -> None:
        self.assert_uninstall_refusal_preserves_login(
            "claude-statusline-backups.json",
            {"schema_version": 1, "entries": {"relative": {"present": False}}},
            "invalid Claude status-line backup",
        )

    def test_uninstall_post_preflight_failure_recovers_login_and_journal(self) -> None:
        self.run_installer("--non-interactive", "--enable-login")
        failing_rm = self.fake_bin / "rm"
        failing_rm.write_text("#!/usr/bin/env bash\nexit 77\n", encoding="utf-8")
        failing_rm.chmod(0o755)
        state = self.home / ".local/state/session-kit"
        marker = state / "integration-ready-v1"
        bashrc = self.home / ".bashrc"

        refused = self.installed("uninstall", check=False)

        self.assertEqual(77, refused.returncode)
        self.assertIn("Recovered interrupted Session Kit uninstall", refused.stdout)
        self.assertFalse((state / "lifecycle-transaction.json").exists())
        self.assertTrue(marker.is_file())
        self.assertIn("session-kit managed integration", bashrc.read_text(encoding="utf-8"))
        self.assertTrue((self.home / ".claude/statusline.sh").is_file())
        self.assertTrue((self.home / ".claude/hooks/nameintent_title.sh").is_file())

    def test_doctor_warns_for_invalid_statusline_backup_ledger(self) -> None:
        self.run_installer("--non-interactive")
        backups = self.home / ".local/state/session-kit/claude-statusline-backups.json"
        backups.write_text(
            json.dumps({"schema_version": 1, "entries": {"relative": {"present": False}}})
            + "\n",
            encoding="utf-8",
        )

        doctor = self.installed("doctor", "--json")

        checks = {row["name"]: row for row in json.loads(doctor.stdout)["checks"]}
        self.assertEqual("warn", checks["claude-statusline-backups"]["status"])
        self.assertIn("invalid data", checks["claude-statusline-backups"]["detail"])

    def test_doctor_warns_for_existing_unsafe_integration_ledger(self) -> None:
        self.run_installer("--non-interactive")
        ledger_path = self.home / ".local/state/session-kit/claude-integration.json"
        target = ledger_path.with_name("unsafe-ledger-target.json")
        ledger_path.rename(target)
        ledger_path.symlink_to(target)

        doctor = self.installed("doctor", "--json")

        checks = {row["name"]: row for row in json.loads(doctor.stdout)["checks"]}
        self.assertEqual("warn", checks["claude-integration-ledger"]["status"])
        self.assertIn("symbolic link", checks["claude-integration-ledger"]["detail"])
        self.assertEqual("warn", checks["naming-hook"]["status"])
        self.assertEqual("warn", checks["statusline"]["status"])

    def test_uninstall_rejects_unexpected_integration_ledger_key(self) -> None:
        self.run_installer("--non-interactive")
        hook = self.home / ".claude/hooks/nameintent_title.sh"
        statusline = self.home / ".claude/statusline.sh"
        ledger_path = self.home / ".local/state/session-kit/claude-integration.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["files"][str(self.home / "unrelated-script")] = "0" * 64
        ledger_path.write_text(json.dumps(ledger) + "\n", encoding="utf-8")

        refused = self.installed("uninstall", check=False)

        self.assertNotEqual(0, refused.returncode)
        self.assertIn("invalid Claude integration ledger", refused.stderr)
        self.assertTrue(hook.is_file())
        self.assertTrue(statusline.is_file())
        self.assertTrue(ledger_path.is_file())

    def test_uninstall_removes_per_account_quota_cache_tree(self) -> None:
        self.run_installer("--non-interactive")
        account = self.home / ".local/share/session-kit/accounts/claude/work"
        account.mkdir(parents=True)
        credentials = account / ".credentials.json"
        credentials.write_text('{"token":"fixture-only"}\n', encoding="utf-8")
        cache = self.home / ".claude/cache/session-kit-quota/work-fixture"
        probe = cache / "probe-home/.claude"
        probe.mkdir(parents=True)
        (cache / "quota_headers").write_text("x-probe-account: fixture\n", encoding="utf-8")
        (probe / ".credentials.json").symlink_to(credentials)

        self.installed("uninstall")

        self.assertFalse((self.home / ".claude/cache/session-kit-quota").exists())
        self.assertTrue(credentials.is_file())

    def test_real_installed_release_layouts_are_legacy_or_complete(self) -> None:
        """Exercise every real rollback payload shape without changing it."""
        releases = Path.home() / ".local/lib/session-kit/releases"
        if not releases.is_dir():
            self.skipTest("no installed Session Kit release corpus")
        layouts: dict[str, int] = {"legacy": 0, "modern": 0}
        visited = 0
        for release in sorted(releases.iterdir()):
            if not release.is_dir():
                continue
            title = release / "config/claude/nameintent_title.sh"
            status = release / "config/claude/statusline.sh"
            shape = (
                title.is_file() and not title.is_symlink(),
                status.is_file() and not status.is_symlink(),
            )
            self.assertIn(
                shape,
                {(False, False), (True, True)},
                f"partial Claude integration payload in {release.name}",
            )
            layouts["modern" if shape == (True, True) else "legacy"] += 1
            visited += 1
        self.assertGreater(visited, 0)
        self.assertGreater(layouts["legacy"], 0)
        self.assertGreater(layouts["modern"], 0)

    def test_legacy_rollback_keeps_modern_manager_for_forward_update(self) -> None:
        self.install_legacy_generation()
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.run_installer("--non-interactive")
        root = self.home / ".local/lib/session-kit"
        current = root / "current"
        manager = root / "manager"
        launcher = self.home / ".local/bin/session-kit"
        release_b = root / "releases" / RELEASE_B
        self.assertEqual(RELEASE_B, manager.resolve().name)

        self.installed("rollback", "--to", RELEASE_A)

        self.assertEqual(RELEASE_A, current.resolve().name)
        self.assertEqual(RELEASE_B, manager.resolve().name)
        self.assertEqual(
            (release_b / "deploy/session-kit-launcher").read_bytes(),
            launcher.read_bytes(),
        )
        self.assertNotEqual(
            (root / "releases" / RELEASE_A / "deploy/session-kit-launcher").read_bytes(),
            launcher.read_bytes(),
        )

        updated = self.installed("update", "--source", str(REPO))

        self.assertIn("Installed Session Kit release", updated.stdout)
        self.assertEqual(RELEASE_B, current.resolve().name)
        self.assertEqual(RELEASE_B, manager.resolve().name)
        doctor = self.installed("doctor", "--json")
        checks = {row["name"]: row for row in json.loads(doctor.stdout)["checks"]}
        self.assertEqual("ok", checks["manager"]["status"])
        self.assertEqual("ok", checks["manager-launcher"]["status"])

    def test_direct_manager_rollback_pins_preflip_launcher_source(self) -> None:
        self.install_legacy_generation()
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.run_installer("--non-interactive")
        root = self.home / ".local/lib/session-kit"
        release_b = root / "releases" / RELEASE_B
        launcher = self.home / ".local/bin/session-kit"
        shutil.copy2(release_b / "bin/session-kit", launcher)

        rolled_back = self.installed("rollback", "--to", RELEASE_A)

        self.assertIn("Rolled back Session Kit", rolled_back.stdout)
        self.assertEqual(RELEASE_A, (root / "current").resolve().name)
        self.assertEqual(
            (release_b / "deploy/session-kit-launcher").read_bytes(),
            launcher.read_bytes(),
        )
        self.assertNotEqual(
            (root / "releases" / RELEASE_A / "deploy/session-kit-launcher").read_bytes(),
            launcher.read_bytes(),
        )
        self.installed("doctor", "--json")

    def test_management_launcher_fails_closed_without_manager_anchor(self) -> None:
        self.run_installer("--non-interactive")
        manager = self.home / ".local/lib/session-kit/manager"
        manager.unlink()

        result = self.installed("doctor", check=False)

        self.assertEqual(78, result.returncode)
        self.assertIn("unsafe manager link", result.stderr)

    def test_rollback_to_a_legacy_release_and_next_recovery_succeed(self) -> None:
        self.install_legacy_generation()

        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.run_installer("--non-interactive")
        current = self.home / ".local/lib/session-kit/current"
        self.assertEqual(RELEASE_B, current.resolve().name)

        rolled_back = self.installed("rollback", "--to", RELEASE_A)
        self.assertIn("Rolled back Session Kit", rolled_back.stdout)
        self.assertEqual(RELEASE_A, current.resolve().name)

        self.env["SESSION_KIT_TEST_FAILPOINT"] = "current"
        self.env["SESSION_KIT_TEST_FAILPOINT_MODE"] = "kill"
        interrupted = self.installed(
            "update", "--source", str(REPO), check=False
        )
        self.assertEqual(-signal.SIGKILL, interrupted.returncode)
        journal = self.home / ".local/state/session-kit/lifecycle-transaction.json"
        self.assertTrue(journal.is_file())
        self.env.pop("SESSION_KIT_TEST_FAILPOINT")
        self.env.pop("SESSION_KIT_TEST_FAILPOINT_MODE")
        recovered = self.installed("update", "--source", str(REPO))
        self.assertIn("Recovered interrupted Session Kit install", recovered.stdout)
        self.assertFalse(journal.exists())

    def test_sigkill_before_rollback_commit_uses_pinned_recovery_release(self) -> None:
        self.install_legacy_generation()
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.run_installer("--non-interactive")

        self.env["SESSION_KIT_TEST_FAILPOINT"] = "rollback-precommit"
        self.env["SESSION_KIT_TEST_FAILPOINT_MODE"] = "kill"
        killed = self.installed("rollback", "--to", RELEASE_A, check=False)
        self.assertEqual(-signal.SIGKILL, killed.returncode)
        current = self.home / ".local/lib/session-kit/current"
        journal = self.home / ".local/state/session-kit/lifecycle-transaction.json"
        launcher = self.home / ".local/bin/session-kit"
        self.assertEqual(RELEASE_A, current.resolve().name)
        self.assertTrue(journal.is_file())
        self.assertEqual(
            (self.home / ".local/lib/session-kit/releases" / RELEASE_B / "deploy/session-kit-launcher").read_bytes(),
            launcher.read_bytes(),
        )

        self.env.pop("SESSION_KIT_TEST_FAILPOINT")
        self.env.pop("SESSION_KIT_TEST_FAILPOINT_MODE")
        recovered = self.installed("update", "--source", str(REPO))
        self.assertIn("Recovered interrupted Session Kit rollback", recovered.stdout)
        self.assertEqual(RELEASE_B, current.resolve().name)
        self.assertFalse(journal.exists())

    def test_sigkill_immediately_after_rollback_commit_keeps_stable_launcher(self) -> None:
        self.install_legacy_generation()
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.run_installer("--non-interactive")

        self.env["SESSION_KIT_TEST_FAILPOINT"] = "rollback-postcommit"
        self.env["SESSION_KIT_TEST_FAILPOINT_MODE"] = "kill"
        killed = self.installed("rollback", "--to", RELEASE_A, check=False)
        self.assertEqual(-signal.SIGKILL, killed.returncode)
        current = self.home / ".local/lib/session-kit/current"
        journal = self.home / ".local/state/session-kit/lifecycle-transaction.json"
        launcher = self.home / ".local/bin/session-kit"
        self.assertEqual(RELEASE_A, current.resolve().name)
        self.assertFalse(journal.exists())
        self.assertEqual(
            (self.home / ".local/lib/session-kit/releases" / RELEASE_B / "deploy/session-kit-launcher").read_bytes(),
            launcher.read_bytes(),
        )

        self.env.pop("SESSION_KIT_TEST_FAILPOINT")
        self.env.pop("SESSION_KIT_TEST_FAILPOINT_MODE")
        recovered = self.installed("update", "--source", str(REPO))
        self.assertEqual(RELEASE_B, current.resolve().name)
        self.assertFalse(journal.exists())
        self.assertIn("Installed Session Kit release", recovered.stdout)

    def test_install_owns_claude_integration_and_preserves_other_hooks(self) -> None:
        claude_settings = self.home / ".claude/settings.json"
        codex_hooks = self.home / ".codex/hooks.json"
        claude_settings.parent.mkdir()
        codex_hooks.parent.mkdir()
        claude_original = (
            b'{ "hooks" : { "UserPromptSubmit" : [{"hooks":['
            b'{"command":"python3 ~/.claude/hooks/quota_human_session.py"},'
            b'{"command":"sh ~/.claude/hooks/nameintent_title.sh"}'
            b']}]}, "keep" : true }\n'
        )
        codex_original = (
            b'{"hooks":{"UserPromptSubmit":[{"hooks":['
            b'{"command":"user-codex-hook","type":"command"}'
            b']}]},"description":"mine"}\n'
        )
        for path, payload in (
            (claude_settings, claude_original),
            (codex_hooks, codex_original),
        ):
            path.write_bytes(payload)
            path.chmod(0o600)

        self.run_installer("--non-interactive")
        settings = json.loads(claude_settings.read_text(encoding="utf-8"))
        self.assertTrue(settings["keep"])
        self.assertEqual(
            settings["statusLine"],
            {
                "type": "command",
                "command": "~/.claude/statusline.sh",
                "refreshInterval": 2,
            },
        )
        prompt_commands = [
            hook["command"]
            for group in settings["hooks"]["UserPromptSubmit"]
            for hook in group["hooks"]
            if isinstance(hook, dict) and "command" in hook
        ]
        self.assertIn("python3 ~/.claude/hooks/quota_human_session.py", prompt_commands)
        self.assertIn("sh ~/.claude/hooks/nameintent_title.sh", prompt_commands)
        self.assertIn("~/.claude/hooks/nameintent_title.sh", prompt_commands)
        self.assertEqual(
            (self.home / ".claude/hooks/nameintent_title.sh").read_bytes(),
            (REPO / "config/claude/nameintent_title.sh").read_bytes(),
        )
        self.assertEqual(
            (self.home / ".claude/statusline.sh").read_bytes(),
            (REPO / "config/claude/statusline.sh").read_bytes(),
        )
        self.assertEqual(codex_original, codex_hooks.read_bytes())
        self.installed("uninstall")
        restored = json.loads(claude_settings.read_text(encoding="utf-8"))
        self.assertTrue(restored["keep"])
        self.assertNotIn("statusLine", restored)
        remaining_commands = [
            hook["command"]
            for group in restored["hooks"]["UserPromptSubmit"]
            for hook in group["hooks"]
            if isinstance(hook, dict) and "command" in hook
        ]
        self.assertEqual(
            remaining_commands,
            [
                "python3 ~/.claude/hooks/quota_human_session.py",
                "sh ~/.claude/hooks/nameintent_title.sh",
            ],
        )
        self.assertFalse((self.home / ".claude/hooks/nameintent_title.sh").exists())
        self.assertFalse((self.home / ".claude/statusline.sh").exists())
        self.assertEqual(codex_original, codex_hooks.read_bytes())

    def test_update_preserves_journal_choice_unless_explicitly_changed(self) -> None:
        # The old default flipped journals OFF on every update; that silent
        # flip erased all terminal recordings 2026-07-30..08-12. An update now
        # keeps the operator's choice in both directions unless --journal is
        # passed explicitly.
        self.run_installer("--journal", "on", "--non-interactive")
        sentinel = self.home / ".no_shpool_journal"
        self.assertFalse(sentinel.exists())
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(REPO))
        self.assertFalse(sentinel.exists())
        self.installed(
            "update", "--source", str(REPO), "--journal", "off"
        )
        self.assertTrue(sentinel.is_file())
        self.installed("update", "--source", str(REPO))
        self.assertTrue(sentinel.is_file())
        self.installed(
            "update", "--source", str(REPO), "--journal", "on"
        )
        self.assertFalse(sentinel.exists())

    def test_noninteractive_rollover_keeps_journals_on(self) -> None:
        # The exact 2026-08-12 regression: a --non-interactive reinstall over
        # an existing installation re-created the kill switch and silently
        # stopped all new-session recording.
        self.run_installer("--journal", "on", "--non-interactive")
        sentinel = self.home / ".no_shpool_journal"
        self.assertFalse(sentinel.exists())
        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.run_installer("--non-interactive")
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
        self.assertEqual(current.resolve().name, RELEASE_A)
        transaction = (
            self.home / ".local/state/session-kit/lifecycle-transaction.json"
        )
        self.assertFalse(transaction.exists())
        self.assertIn("Recovered interrupted Session Kit install", failed.stdout)

        del self.env["SESSION_KIT_TEST_FAILPOINT"]
        recovered = self.installed("update", "--source", str(REPO))
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
        installed_config = shpool_config.read_text(encoding="utf-8")
        self.assertIn(f'shell = "{expected_config_bash}"', installed_config)
        # Ctrl-q is what docs/usage.md tells people to press to leave a
        # conversation running, so a fresh install has to make it true. Stock
        # shpool 0.11 detaches on the two-key chord Ctrl-Space Ctrl-q and a
        # lone Ctrl-q does nothing at all; shipping the instruction without
        # the binding passed for the one machine whose private config already
        # had it, and was false for every new setup (found in review,
        # 2026-08-15). That is the worst shape of documentation bug: it
        # survives the only test anyone will actually run.
        self.assertIn("[[keybinding]]", installed_config)
        self.assertIn('binding = "Ctrl-q"', installed_config)
        self.assertIn('action = "detach"', installed_config)

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

    def test_the_release_ships_bytecode_caches_that_validate(self) -> None:
        """A cache that does not match its source is worse than none.

        `cp -R` renews every mtime, so a cache built while the source tree was
        in use records a timestamp the copy does not have. Python then compiled
        from source on every import -- 112-148 ms where a valid cache costs 41 --
        and could never repair it: the release is read-only and
        `shpool_status` exports PYTHONDONTWRITEBYTECODE=1 for that reason.
        """
        self.run_installer()
        release = (
            self.home / ".local/lib/session-kit/releases" / RELEASE_A / "lib"
        )
        caches = sorted(release.rglob("*.pyc"))
        self.assertTrue(caches, msg="no bytecode caches were shipped")
        stale = []
        for cache in caches:
            source = cache.parents[1] / (cache.name.split(".")[0] + ".py")
            self.assertTrue(source.exists(), msg=f"orphan cache: {cache}")
            data = cache.read_bytes()
            flags = int.from_bytes(data[4:8], "little")
            # Hash-based (PEP 552): valid whatever the mtimes are, which is the
            # only kind a copied, resealed tree can promise.
            if not flags & 0b1:
                stale.append(f"{cache.name}: mtime-based")
                continue
            if data[8:16] != importlib.util.source_hash(source.read_bytes()):
                stale.append(f"{cache.name}: hash does not match its source")
        self.assertEqual([], stale)

    def test_doctor_reports_the_recording_footprint_without_removing_any(
        self,
    ) -> None:
        """The largest thing the kit writes, and the only feed with no policy.

        Recordings go when their session is closed or pruned, and nothing ages
        them out -- 2.6 GB on a two-week-old box, 470 MB of it belonging to
        sessions that no longer existed. Doctor states the number and warns past
        a bound the operator sets. It deletes nothing: a recording is the history
        `sp find` and `sp history` read.
        """
        self.run_installer()
        journals = self.home / ".local/state/shpool-journal/s20260101-000000-1"
        journals.mkdir(parents=True)
        old = journals / "segment-000001.raw"
        old.write_bytes(b"x" * 4096)
        stale = time.time() - 40 * 86400
        os.utime(old, (stale, stale))

        result = self.installed("doctor", "--json", check=False)
        checks = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual("warn", checks["journals"]["status"])
        self.assertIn(
            "SESSION_KIT_JOURNAL_RETENTION_DAYS", checks["journals"]["detail"]
        )
        # Nothing was removed by looking.
        self.assertTrue(old.exists())

        # And the bound is the operator's: a wider one is satisfied.
        self.env["SESSION_KIT_JOURNAL_RETENTION_DAYS"] = "90"
        result = self.installed("doctor", "--json", check=False)
        checks = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual("ok", checks["journals"]["status"])
        self.assertTrue(old.exists())

    def test_doctor_names_a_disabled_live_channel_on_the_kill_switch_line(
        self,
    ) -> None:
        """A kill switch that doctor cannot see is a false all-clear.

        The picker's three live channels default on; each is turned off by
        0/off/no/false in any case. Doctor's own `kill-switches` row must name a
        disabled one instead of reporting that none is active -- otherwise the
        line meant to surface a turned-off feature lies about the three newest.
        """
        self.run_installer()

        clean = self.installed("doctor", "--json", check=False)
        row = {r["name"]: r for r in json.loads(clean.stdout)["checks"]}["kill-switches"]
        self.assertEqual("ok", row["status"])
        self.assertIn("no supported kill switch", row["detail"])

        for var, spelling in (
            ("SESSION_KIT_PICKER_EVENTS", "0"),
            ("SESSION_KIT_PICKER_PULSE", " Off "),
            ("SESSION_KIT_CLAUDE_SOCKET", "false"),
        ):
            self.env[var] = spelling
        result = self.installed("doctor", "--json", check=False)
        row = {r["name"]: r for r in json.loads(result.stdout)["checks"]}["kill-switches"]
        self.assertEqual("warn", row["status"])
        for var in (
            "SESSION_KIT_PICKER_EVENTS",
            "SESSION_KIT_PICKER_PULSE",
            "SESSION_KIT_CLAUDE_SOCKET",
        ):
            self.assertIn(var, row["detail"])
        for var in (
            "SESSION_KIT_PICKER_EVENTS",
            "SESSION_KIT_PICKER_PULSE",
            "SESSION_KIT_CLAUDE_SOCKET",
        ):
            self.env.pop(var)

    # ---- what is running vs what was shipped ------------------------------
    #
    # A release rolls by writing new unit files and moving `current`. systemd
    # keeps serving the previous unit definitions until it is told, and a
    # long-running kit process keeps executing the inode it started with. Ten
    # activations passed over one watchdog process on a live box: every fix
    # shipped that day ran nowhere, and doctor reported green throughout.

    def fake_systemctl(self, *, reload: str = "no", main_pid: str = "0") -> Path:
        """A systemctl that answers the two queries doctor asks and logs calls.

        The real one is never reached from a test: `SESSION_KIT_SYSTEMCTL_CMD`
        is the seam, and without it the kit skips this work entirely under the
        test flag rather than restarting the operator's own units.
        """
        log = self.temp / "systemctl.log"
        tool = self.fake_bin / "fake-systemctl"
        tool.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> {log}\n'
            "case \"$*\" in\n"
            f'  *NeedDaemonReload*) printf "NeedDaemonReload={reload}\\nMainPID={main_pid}\\n" ;;\n'
            '  *UnitFileState*) printf "UnitFileState=enabled\\nActiveState=active\\n" ;;\n'
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        tool.chmod(0o755)
        self.env["SESSION_KIT_SYSTEMCTL_CMD"] = str(tool)
        return log

    def start_release_process(
        self, release: str, *, evidence: str = "script"
    ) -> subprocess.Popen[str]:
        """A live process that belongs to `release`, the way the watchdog does.

        Two kinds of evidence, both of which doctor reads: `script` is the
        descriptor bash keeps open on the file it exec'd, which resolves through
        `current` to the release as it was at exec time; `environ` is the
        release directory the launcher exports, for a process whose descriptors
        cannot be read.
        """
        root = self.home / ".local/lib/session-kit/releases" / release
        if evidence == "environ":
            environment = dict(self.env)
            environment["SESSION_KIT_RELEASE_DIR"] = str(root)
            return subprocess.Popen(
                ["/usr/bin/env", "bash", "-c", "read -r _line"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        script = root / "bin/fixture_watchdog"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "#!/usr/bin/env bash\nread -r _line\n", encoding="utf-8"
        )
        script.chmod(0o755)
        return subprocess.Popen(
            ["/usr/bin/env", "bash", str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def test_activation_reloads_the_units_and_restarts_the_watchdog(self) -> None:
        log = self.fake_systemctl()
        self.run_installer()
        called = log.read_text(encoding="utf-8").splitlines()
        self.assertIn("--user daemon-reload", called)
        self.assertIn(
            "--user try-restart session-kit-watchdog.service", called
        )
        # The session daemon is never restarted by an activation: that would
        # end every managed session.
        for line in called:
            self.assertNotIn("shpool.service", line)
            self.assertNotIn("shpool.socket", line)

    def test_doctor_fails_when_the_running_watchdog_is_another_release(
        self,
    ) -> None:
        self.run_installer()
        stale = "b" * 40
        process = self.start_release_process(stale)
        try:
            self.fake_systemctl(main_pid=str(process.pid))
            result = self.installed("doctor", "--json", check=False)
            checks = {
                row["name"]: row for row in json.loads(result.stdout)["checks"]
            }
            self.assertEqual("fail", checks["release-running"]["status"])
            self.assertIn("not the installed", checks["release-running"]["detail"])
            self.assertIn(
                "systemctl --user restart session-kit-watchdog.service",
                checks["release-running"]["detail"],
            )
            self.assertNotEqual(0, result.returncode)
        finally:
            process.stdin.close()
            process.wait(timeout=10)

    def test_doctor_passes_when_the_running_watchdog_is_this_release(
        self,
    ) -> None:
        self.run_installer()
        process = self.start_release_process(RELEASE_A, evidence="environ")
        try:
            self.fake_systemctl(main_pid=str(process.pid))
            result = self.installed("doctor", "--json", check=False)
            checks = {
                row["name"]: row for row in json.loads(result.stdout)["checks"]
            }
            self.assertEqual("ok", checks["release-running"]["status"])
            self.assertEqual("ok", checks["units-loaded"]["status"])
        finally:
            process.stdin.close()
            process.wait(timeout=10)

    def test_doctor_fails_when_systemd_has_not_read_the_new_units(self) -> None:
        self.run_installer()
        self.fake_systemctl(reload="yes")
        result = self.installed("doctor", "--json", check=False)
        checks = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual("fail", checks["units-loaded"]["status"])
        self.assertIn(
            "systemctl --user daemon-reload", checks["units-loaded"]["detail"]
        )
        self.assertNotEqual(0, result.returncode)

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

    def test_every_entry_point_speaks_with_one_prefix_and_one_sentence(
        self,
    ) -> None:
        """One error prefix, and one prerequisite sentence in all three places.

        The launcher used to print `session-kit launcher:` on twelve lines, and
        the same macOS prerequisite was three different sentences."""
        launcher = (REPO / "deploy/session-kit-launcher").read_text(
            encoding="utf-8"
        )
        printed = re.findall(r'echo "([^"]+)" >&2', launcher)
        self.assertEqual(12, len(printed), printed)
        for message in printed:
            self.assertTrue(
                message.startswith("session-kit: "), message
            )
        sentence = (
            "session-kit: macOS needs Homebrew Bash 4 or newer. "
            "Install it with: brew install bash python rust"
        )
        for relative in ("install.sh", "bin/session-kit", "deploy/session-kit-launcher"):
            self.assertIn(
                sentence,
                (REPO / relative).read_text(encoding="utf-8"),
                relative,
            )

    def test_doctor_and_check_render_one_row_form_with_one_set_of_names(
        self,
    ) -> None:
        """The two reports a person runs when something is wrong agree.

        One renderer, one name per check: `check` used to print
        `OK    operating system: Linux` where `doctor` printed
        `OK    platform           linux`."""
        row = re.compile(r"^(OK|WARN|FAIL)\s{2,}(\S+)\s+(\S.*)$")
        self.run_installer("--non-interactive")
        doctor = self.installed("doctor")
        checked = self.run_installer("--check")
        names = {}
        for label, output in (("doctor", doctor.stdout), ("check", checked.stdout)):
            parsed = [row.match(line) for line in output.splitlines() if line.strip()]
            self.assertTrue(all(parsed), output)
            names[label] = {match.group(2) for match in parsed}
            for match in parsed:
                self.assertEqual(
                    f"{match.group(1):<5} {match.group(2):<18} {match.group(3)}",
                    match.group(0),
                    output,
                )
        for shared in ("platform", "prerequisites", "services", "provider", "process"):
            self.assertIn(shared, names["doctor"])
            self.assertIn(shared, names["check"])
        self.assertIn("OK    platform           linux", doctor.stdout)
        self.assertIn("OK    platform           linux", checked.stdout)

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

        result = self.installed("doctor", "--json", check=False)

        # Nothing here is a `fail`, so the run itself succeeds; the kill-switch
        # warning is reported without being turned into an exit code.
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("secret-color-value", result.stdout)
        names = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual(names["naming-instructions"]["status"], "ok")
        self.assertEqual(names["naming-hook"]["status"], "ok")
        self.assertEqual(names["acceptance"]["status"], "ok")
        self.assertEqual(names["kill-switches"]["status"], "warn")
        self.assertIn("SESSION_KIT_NO_COLOR", names["kill-switches"]["detail"])
        # Who owns the tab name (K3). Nothing is silencing it in this fixture.
        self.assertEqual(names["tab-title"]["status"], "ok")

        # The vendor's own off-switch silences the kit's title too (drill,
        # 2026-08-13), so doctor has to name it when it is set.
        silenced = self.installed(
            "doctor",
            "--json",
            check=False,
            env={**self.env, "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"},
        )
        silenced_names = {
            row["name"]: row for row in json.loads(silenced.stdout)["checks"]
        }
        self.assertEqual(silenced_names["tab-title"]["status"], "warn")
        self.assertIn(
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE", silenced_names["tab-title"]["detail"]
        )

        stale = json.loads(acceptance.read_text(encoding="utf-8"))
        stale["release_id"] = RELEASE_B
        acceptance.write_text(json.dumps(stale), encoding="utf-8")
        acceptance.chmod(0o600)
        stale_result = self.installed("doctor", "--json", check=False)
        stale_names = {
            row["name"]: row for row in json.loads(stale_result.stdout)["checks"]
        }
        self.assertEqual(stale_names["acceptance"]["status"], "warn")

    def test_doctor_requires_exact_typed_title_hook_command(self) -> None:
        self.run_installer("--non-interactive")
        claude = self.home / ".claude"
        hooks = claude / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
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

        result = self.installed("doctor", "--json", check=False)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        names = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual(names["naming-hook"]["status"], "warn")
        self.assertIn("hook content", names["naming-hook"]["detail"])
        self.assertIn("SessionStart", names["naming-hook"]["detail"])
        self.assertIn("UserPromptSubmit", names["naming-hook"]["detail"])

    def test_doctor_reports_a_wrong_shaped_title_template(self) -> None:
        self.run_installer("--non-interactive")
        template = self.home / ".codex/session-kit/terminal-title.toml"
        template.write_text('tui = "scalar"\n', encoding="utf-8")

        result = self.installed("doctor", "--json", check=False)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        names = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual("warn", names["tab-title"]["status"])
        self.assertIn("tui must be a table", names["tab-title"]["detail"])

    def test_doctor_reports_the_durable_codex_titler_failure(self) -> None:
        self.run_installer("--non-interactive")
        record = self.home / ".local/state/session-kit/codex-autotitle-error.json"
        record.write_text(
            json.dumps(
                {
                    "detail": "Codex thread store unreadable: page two broke",
                    "at": "2026-08-13T12:00:00Z",
                }
            ),
            encoding="utf-8",
        )

        result = self.installed("doctor", "--json", check=False)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        names = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertEqual("warn", names["codex-titles"]["status"])
        self.assertIn("page two broke", names["codex-titles"]["detail"])

    def test_doctor_uses_custom_codex_home_for_themes_and_instructions(self) -> None:
        codex_home = self.home / "private-codex"
        codex_home.mkdir(mode=0o700)
        self.env["CODEX_HOME"] = str(codex_home)
        self.run_installer("--non-interactive")
        (codex_home / "AGENTS.md").write_text(
            "run sp self-name once\n", encoding="utf-8"
        )
        claude = self.home / ".claude"
        claude.mkdir(exist_ok=True)
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
        # Preflight reports it on stdout; the transaction would repeat it on
        # stderr. Either layer refusing before any write is the contract.
        self.assertIn("absolute normalized path", relative.stdout + relative.stderr)
        self.assertFalse((self.home / ".local/lib/session-kit").exists())

        target = self.temp / "codex-target"
        target.mkdir(mode=0o700)
        link = self.temp / "codex-link"
        link.symlink_to(target, target_is_directory=True)
        self.env["CODEX_HOME"] = str(link)
        linked = self.run_installer("--non-interactive", check=False)
        self.assertNotEqual(linked.returncode, 0)
        self.assertIn("unsafe Codex path ancestor", linked.stdout + linked.stderr)
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
        self.assertIn("unsafe Codex path ancestor", rejected.stdout + rejected.stderr)
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
            self.env["SESSION_KIT_TEST_FAILPOINT_MODE"] = "kill"
            interrupted = self.installed(
                "update", "--source", str(REPO), check=False
            )
            self.assertEqual(-signal.SIGKILL, interrupted.returncode)
            self.env.pop("SESSION_KIT_TEST_FAILPOINT")
            self.env.pop("SESSION_KIT_TEST_FAILPOINT_MODE")
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
        for color in THEME_COLORS:
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
        self.env["SESSION_KIT_TEST_FAILPOINT_MODE"] = "kill"

        interrupted = self.installed(
            "update", "--source", str(REPO), check=False
        )

        self.assertEqual(-signal.SIGKILL, interrupted.returncode)
        transaction = json.loads(
            (self.home / ".local/state/session-kit/lifecycle-transaction.json").read_text(
                encoding="utf-8"
            )
        )
        captured = {entry["path"] for entry in transaction["entries"]}
        expected = {
            str(themes / f"sk-{color}.tmTheme") for color in THEME_COLORS
        }
        self.assertTrue(expected.issubset(captured))
        self.assertNotEqual(red.read_text(encoding="utf-8"), "pre-update-theme\n")

        self.env.pop("SESSION_KIT_TEST_FAILPOINT")
        self.env.pop("SESSION_KIT_TEST_FAILPOINT_MODE")
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

    def unit_adding_source_fixture(self) -> Path:
        """A copy of this checkout that ships one more systemd unit than it
        does, named in its own `systemd_units` list.

        That is the only shape in which the rollback defect appears: the list
        an activation reads belongs to the RUNNING kit, so a release that adds
        a unit hands the next rollback a name the older release never carried.
        A fixture that adds the file without adding the name, or the name
        without the file, reproduces nothing.
        """
        source = self.clean_source_fixture()
        (source / "systemd" / FIXTURE_UNIT).write_text(
            "[Unit]\n"
            "Description=Session Kit fixture unit\n"
            "\n"
            "[Service]\n"
            "Type=oneshot\n"
            "ExecStart=/bin/true\n",
            encoding="utf-8",
        )
        entry = source / "bin/session-kit"
        text = entry.read_text(encoding="utf-8")
        listed = "  session-kit-watchdog.service\n)"
        self.assertIn(listed, text, "bin/session-kit no longer declares systemd_units")
        entry.write_text(
            text.replace(
                listed, f"  session-kit-watchdog.service\n  {FIXTURE_UNIT}\n)", 1
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", source, "add", "."], check=True)
        subprocess.run(
            [
                "git", "-C", source,
                "-c", "user.name=Session Kit Tests",
                "-c", "user.email=session-kit-tests@invalid.example",
                "commit", "-q", "--amend", "--no-edit",
            ],
            check=True,
        )
        return source

    def test_rollback_to_a_release_that_predates_a_unit_completes(self) -> None:
        """Adding a systemd unit must not remove the undo for adding it.

        The rollback runs the release being left, so its unit list names a file
        the target release does not have. Copying that file used to fail and
        take the whole transaction with it, which left the installation on the
        release the operator was trying to leave -- with no way back to any
        release older than the one that added the unit.
        """
        self.run_installer("--non-interactive")
        service_root = self.temp / "systemd"
        carried = {
            path.name: path.read_bytes() for path in sorted(service_root.iterdir())
        }
        self.assertIn("shpool.service", carried)
        self.assertNotIn(FIXTURE_UNIT, carried)

        self.env["SESSION_KIT_RELEASE_ID"] = RELEASE_B
        self.installed("update", "--source", str(self.unit_adding_source_fixture()))
        stale = service_root / FIXTURE_UNIT
        self.assertTrue(stale.is_file(), "the fixture release installed no new unit")

        rolled_back = self.installed("rollback")

        current = self.home / ".local/lib/session-kit/current"
        self.assertEqual(RELEASE_A, current.resolve().name)
        self.assertIn(
            f"{FIXTURE_UNIT} is not part of this release", rolled_back.stderr
        )
        # The stale unit's program is the release that is now current, which
        # does not take the arguments the unit passes it, so leaving the file
        # would leave systemd running a failing job on a timer.
        self.assertFalse(stale.exists() or stale.is_symlink())
        # Everything the target release does carry survives, byte for byte.
        self.assertEqual(
            sorted(carried), sorted(path.name for path in service_root.iterdir())
        )
        for name, content in carried.items():
            self.assertEqual(content, (service_root / name).read_bytes(), name)
        receipt = json.loads(
            (self.home / ".local/state/session-kit/install.json").read_text()
        )
        self.assertEqual(RELEASE_A, receipt["installed_release"])

    def test_uninstall_removes_the_unit_files_it_installed(self) -> None:
        """An uninstall that leaves unit files behind leaves systemd pointing
        at code the same command just deleted."""
        self.run_installer("--non-interactive")
        service_root = self.temp / "systemd"
        self.assertTrue((service_root / "shpool.socket").is_file())

        self.installed("uninstall", "--purge-code", "--purge-config")

        self.assertEqual([], sorted(path.name for path in service_root.iterdir()))

    def write_systemctl_state_fixture(self) -> Path:
        """A systemctl that answers `is-enabled` and `is-active` from the
        environment, so a test can name the state systemd is in."""
        systemctl = self.fake_bin / "systemctl-state"
        systemctl.write_text(
            "#!/usr/bin/env bash\n"
            "case ${2:-} in\n"
            "  is-enabled) printf '%s\\n' \"${FIXTURE_ENABLED:-disabled}\" ;;\n"
            "  is-active) printf '%s\\n' \"${FIXTURE_ACTIVE:-inactive}\" ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        systemctl.chmod(0o755)
        return systemctl

    def test_uninstall_refuses_while_systemd_holds_an_enablement(self) -> None:
        """Removing the unit file of a unit systemd has enabled leaves the
        manager holding a job whose file is gone. The documented verb comes
        first, and nothing is removed until it has.

        `is-enabled` has four answers that mean systemd is holding this file,
        not one, and a check that reads only `enabled` deletes the file under
        the other three.
        """
        self.run_installer("--non-interactive")
        env = self.env.copy()
        env["SESSION_KIT_SYSTEMCTL_CMD"] = str(self.write_systemctl_state_fixture())

        for state in ("enabled", "enabled-runtime", "linked", "linked-runtime"):
            with self.subTest(is_enabled=state):
                env["FIXTURE_ENABLED"] = state
                refused = self.installed(
                    "uninstall",
                    "--purge-code",
                    "--purge-config",
                    check=False,
                    env=env,
                )
                self.assertNotEqual(0, refused.returncode)
                self.assertIn("session-kit services disable", refused.stderr)
                self.assertTrue((self.temp / "systemd/shpool.socket").is_file())
                self.assertTrue((self.home / ".local/bin/sp").exists())
                self.assertTrue(
                    (self.home / ".local/lib/session-kit/current").is_symlink()
                )

    def test_uninstall_proceeds_while_a_unit_runs_without_an_enablement(self) -> None:
        """A running daemon is holding live sessions. Uninstall says so and
        removes its files; it does not make the way out of an installation go
        through killing every session in it."""
        self.run_installer("--non-interactive")
        env = self.env.copy()
        env["SESSION_KIT_SYSTEMCTL_CMD"] = str(self.write_systemctl_state_fixture())
        env["FIXTURE_ENABLED"] = "disabled"
        env["FIXTURE_ACTIVE"] = "active"

        removed = self.installed(
            "uninstall", "--purge-code", "--purge-config", check=False, env=env
        )

        self.assertEqual(0, removed.returncode, removed.stderr)
        self.assertIn("is still running and is left running", removed.stderr)
        self.assertEqual([], sorted(path.name for path in (self.temp / "systemd").iterdir()))

    def test_uninstall_refuses_a_symlinked_unit_path_and_spares_its_target(
        self,
    ) -> None:
        """A unit path that is a symlink is not this installation's file to
        delete, and the removal must not reach through it."""
        self.run_installer("--non-interactive")
        decoy = self.temp / "decoy-outside-the-service-root"
        decoy.write_text("operator file\n", encoding="utf-8")
        planted = self.temp / "systemd/shpool.socket"
        planted.unlink()
        planted.symlink_to(decoy)

        refused = self.installed(
            "uninstall", "--purge-code", "--purge-config", check=False
        )

        self.assertNotEqual(0, refused.returncode)
        self.assertIn("refusing", refused.stderr)
        self.assertEqual("operator file\n", decoy.read_text(encoding="utf-8"))
        self.assertTrue(planted.is_symlink())
        self.assertTrue((self.home / ".local/bin/sp").exists())


class SystemdUnitLoopTests(unittest.TestCase):
    """Drive `install_systemd_units` directly, with a service root of the
    test's own making.

    The end-to-end proof above shows the loop reached from a real rollback.
    These pin what the loop does with the cases a release cannot be built to
    contain -- a symlinked unit path, a unit name carrying a path -- and the
    half that deletes, which is the half worth proving twice.
    """

    STALE = "session-kit-fixture-stale.service"
    TEMPLATE = "[Unit]\nDescription=fixture\n\n[Service]\nExecStart=@SHPOOL@ daemon\n"

    def setUp(self) -> None:
        self.temp = Path(
            tempfile.mkdtemp(prefix="session-kit-units-test.", dir=Path("/tmp"))
        ).resolve()
        self.home = self.temp / "home"
        self.install_root = self.home / ".local/lib/session-kit"
        self.service_root = self.temp / "systemd"
        self.fake_bin = self.temp / "bin"
        self.service_root.mkdir()
        self.fake_bin.mkdir()
        (self.install_root / "releases").mkdir(parents=True)
        shpool = self.fake_bin / "shpool"
        shpool.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        shpool.chmod(0o755)
        self.harness = self.temp / "harness.sh"
        self.harness.write_text(
            "#!/usr/bin/env bash\n"
            # The dispatcher runs under these options; the abort this branch
            # fixes only happens under them.
            "set -euo pipefail\n"
            "install_root=$1\n"
            "service_root=$2\n"
            "release_id=$3\n"
            "shift 3\n"
            "systemd_units=(\"$@\")\n"
            "die() { printf 'session-kit: %s\\n' \"$*\" >&2; exit 1; }\n"
            "platform() { printf 'linux\\n'; }\n"
            'source "$SESSION_KIT_MODULE"\n'
            'install_systemd_units "$release_id"\n',
            encoding="utf-8",
        )
        self.harness.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                "PATH": f"{self.fake_bin}:{self.env['PATH']}",
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_DATA_HOME": str(self.home / ".local/share"),
                "XDG_STATE_HOME": str(self.home / ".local/state"),
                "SESSION_KIT_ROOT": str(self.install_root),
                "SESSION_KIT_SYSTEMD_ROOT": str(self.service_root),
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_MODULE": str(REPO / "lib/sh/session_kit_install.sh"),
            }
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp)

    def stage_release(self, release_id: str, units: dict[str, str]) -> None:
        root = self.install_root / "releases" / release_id / "systemd"
        root.mkdir(parents=True)
        for name, content in units.items():
            (root / name).write_text(content, encoding="utf-8")

    def install_units(
        self, release_id: str, units: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(self.harness),
                str(self.install_root),
                str(self.service_root),
                release_id,
                *units,
            ],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_a_unit_the_release_does_not_carry_is_removed_and_named(self) -> None:
        self.stage_release(RELEASE_A, {"shpool.service": self.TEMPLATE})
        stale = self.service_root / self.STALE
        stale.write_text("stale unit\n", encoding="utf-8")

        result = self.install_units(RELEASE_A, ("shpool.service", self.STALE))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(stale.exists() or stale.is_symlink())
        self.assertIn(f"{self.STALE} is not part of this release", result.stderr)
        self.assertIn(f"systemctl --user disable {self.STALE}", result.stderr)
        installed = (self.service_root / "shpool.service").read_text(encoding="utf-8")
        self.assertIn(f"ExecStart={self.fake_bin}/shpool daemon", installed)

    def test_a_unit_the_release_carries_is_kept_and_rewritten(self) -> None:
        """The pruning half must not reach a unit the target release has."""
        self.stage_release(
            RELEASE_A,
            {"shpool.service": self.TEMPLATE, "shpool.socket": "[Socket]\n"},
        )
        kept = self.service_root / "shpool.socket"
        kept.write_text("foreign content\n", encoding="utf-8")

        result = self.install_units(RELEASE_A, ("shpool.service", "shpool.socket"))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(kept.is_file() and not kept.is_symlink())
        self.assertEqual("[Socket]\n", kept.read_text(encoding="utf-8"))
        self.assertNotIn("shpool.socket is not part of", result.stderr)

    def test_a_symlinked_stale_unit_is_neither_followed_nor_removed(self) -> None:
        """`rm` on a symlink would take the link, not its target -- but the
        link is not this installation's file either, and the guard that keeps
        the deletion off it is worth a test of its own."""
        self.stage_release(RELEASE_A, {"shpool.service": self.TEMPLATE})
        decoy = self.temp / "decoy-outside-the-service-root"
        decoy.write_text("operator file\n", encoding="utf-8")
        planted = self.service_root / self.STALE
        planted.symlink_to(decoy)

        result = self.install_units(RELEASE_A, ("shpool.service", self.STALE))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(decoy.is_file())
        self.assertEqual("operator file\n", decoy.read_text(encoding="utf-8"))
        self.assertTrue(planted.is_symlink())
        self.assertIn(f"{self.STALE} is not part of this release", result.stderr)
        self.assertNotIn("was removed", result.stderr)

    def test_a_symlinked_live_unit_is_replaced_without_writing_through(self) -> None:
        """The copying half installs over the link, never through it."""
        self.stage_release(
            RELEASE_A,
            {"shpool.service": self.TEMPLATE, "shpool.socket": "[Socket]\n"},
        )
        decoy = self.temp / "decoy-outside-the-service-root"
        decoy.write_text("operator file\n", encoding="utf-8")
        planted = self.service_root / "shpool.socket"
        planted.symlink_to(decoy)

        result = self.install_units(RELEASE_A, ("shpool.service", "shpool.socket"))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("operator file\n", decoy.read_text(encoding="utf-8"))
        self.assertFalse(planted.is_symlink())
        self.assertEqual("[Socket]\n", planted.read_text(encoding="utf-8"))

    def test_a_unit_name_that_leaves_the_service_root_is_refused(self) -> None:
        """Both halves join the name onto a directory. A name carrying a path
        would copy a file in from outside the release and delete one from
        outside the service root, so the name is refused before either."""
        escaping = "../escape.service"
        release = self.install_root / "releases" / RELEASE_A
        self.stage_release(RELEASE_A, {"shpool.service": self.TEMPLATE})
        (release / "escape.service").write_text("payload\n", encoding="utf-8")
        victim = self.temp / "escape.service"
        victim.write_text("operator file\n", encoding="utf-8")
        self.assertEqual(victim, (self.service_root / escaping).resolve())

        result = self.install_units(RELEASE_A, ("shpool.service", escaping))

        self.assertEqual("operator file\n", victim.read_text(encoding="utf-8"))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"refusing unsafe unit name: {escaping}", result.stderr)

    def test_every_malformed_unit_name_is_refused(self) -> None:
        self.stage_release(RELEASE_A, {"shpool.service": self.TEMPLATE})
        for name in ("../escape.service", "..", ".", "sub/dir.service", ""):
            with self.subTest(name=name):
                result = self.install_units(RELEASE_A, ("shpool.service", name))
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("refusing unsafe unit name", result.stderr)

    def test_a_release_without_the_templated_unit_does_not_abort(self) -> None:
        """The templating step reads shpool.service straight after the loop.
        A release that does not carry it leaves nothing to fill in, and
        reading it anyway would restore the abort the loop just removed."""
        self.stage_release(RELEASE_A, {"shpool.socket": "[Socket]\n"})
        (self.service_root / "shpool.service").write_text(
            "stale service\n", encoding="utf-8"
        )

        result = self.install_units(RELEASE_A, ("shpool.service", "shpool.socket"))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse((self.service_root / "shpool.service").exists())
        self.assertEqual(
            "[Socket]\n",
            (self.service_root / "shpool.socket").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
