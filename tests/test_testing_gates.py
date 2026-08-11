"""Production must never honour a test-only hook.

Every variable exercised here replaces evidence the kit would otherwise gather
from the machine: which platform this is, whether a provider process really
started, which sessions the provider reports, whether an install should abort.
Setting an environment variable is not a privilege, so each hook is armed only
when ``SESSION_KIT_TESTING=1`` is also set. These tests assert both directions
-- ignored without the gate, honoured with it -- because a gate that is only
tested in its armed state is a gate nobody has proven closed.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, run

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_inventory import common as inventory_common  # noqa: E402
from sessionkit_messages import claude_send  # noqa: E402


COMMON = REPO / "bin" / "session_kit_common"
LIFECYCLE = REPO / "lib" / "sh" / "session_kit_lifecycle.sh"
BASHRC = REPO / "bashrc" / "shpool.bashrc"
PROVIDER_HOOKS = REPO / "lib" / "sessionkit_supervisor" / "provider_hooks.py"

# A PID that exists (this process) paired with start ticks that cannot be its
# own, so the real evidence path always answers "not present". Only the
# override can turn this into a success, which is exactly what makes it a
# usable probe for whether the override was honoured.
UNPROVABLE_START_TICKS = "999999999999"


def sandbox_env(**extra: str) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    environment.update(extra)
    return environment


class ProviderPresenceOverrideGateTests(unittest.TestCase):
    """`sk_provider_process_present` is the entire proof that a launch worked."""

    def presence(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return run(
            [
                "bash",
                "-c",
                'source "$1"; '
                f'sk_provider_process_present "$$" {UNPROVABLE_START_TICKS} codex',
                "presence-gate-test",
                COMMON,
            ],
            env=sandbox_env(**extra),
            check=False,
        )

    def test_override_is_ignored_without_the_testing_gate(self) -> None:
        result = self.presence(SESSION_KIT_PROVIDER_PRESENCE_OVERRIDE="present")

        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "ignoring SESSION_KIT_PROVIDER_PRESENCE_OVERRIDE", result.stderr
        )
        self.assertIn("reserved for isolated tests", result.stderr)

    def test_override_is_honoured_under_the_testing_gate(self) -> None:
        result = self.presence(
            SESSION_KIT_TESTING="1",
            SESSION_KIT_PROVIDER_PRESENCE_OVERRIDE="present",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("ignoring", result.stderr)

    def test_an_absent_override_still_refuses_under_the_gate(self) -> None:
        """The gate decides whether the hook is read, not what it may say."""
        result = self.presence(
            SESSION_KIT_TESTING="1",
            SESSION_KIT_PROVIDER_PRESENCE_OVERRIDE="absent",
        )

        self.assertNotEqual(0, result.returncode)

    def test_an_unset_override_never_notes_anything(self) -> None:
        result = self.presence()

        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stderr)


class PlatformOverrideGateTests(unittest.TestCase):
    def platform_of(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return run(
            ["bash", "-c", 'source "$1"; sk_platform', "platform-gate-test", COMMON],
            env=sandbox_env(**extra),
            check=False,
        )

    def test_platform_override_is_ignored_without_the_testing_gate(self) -> None:
        result = self.platform_of(SESSION_KIT_TEST_PLATFORM="Darwin")

        self.assertEqual(platform.system(), result.stdout.strip())
        self.assertIn("ignoring SESSION_KIT_TEST_PLATFORM", result.stderr)

    def test_platform_override_is_honoured_under_the_testing_gate(self) -> None:
        result = self.platform_of(
            SESSION_KIT_TESTING="1", SESSION_KIT_TEST_PLATFORM="Darwin"
        )

        self.assertEqual("Darwin", result.stdout.strip())
        self.assertEqual("", result.stderr)


class LifecycleFailpointGateTests(unittest.TestCase):
    """An ungated failpoint lets any process abort every install."""

    def failpoint(self, name: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return run(
            [
                "bash",
                "-c",
                'die() { printf "die: %s\\n" "$*" >&2; exit 9; }; '
                'source "$1"; lifecycle_failpoint "$2"; echo SURVIVED',
                "failpoint-gate-test",
                LIFECYCLE,
                name,
            ],
            env=sandbox_env(**extra),
            check=False,
        )

    def test_failpoint_is_ignored_without_the_testing_gate(self) -> None:
        result = self.failpoint("themes", SESSION_KIT_TEST_FAILPOINT="themes")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("SURVIVED", result.stdout)

    def test_kill_mode_is_ignored_without_the_testing_gate(self) -> None:
        result = self.failpoint(
            "themes",
            SESSION_KIT_TEST_FAILPOINT="themes",
            SESSION_KIT_TEST_FAILPOINT_MODE="kill",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("SURVIVED", result.stdout)

    def test_failpoint_is_honoured_under_the_testing_gate(self) -> None:
        result = self.failpoint(
            "themes", SESSION_KIT_TESTING="1", SESSION_KIT_TEST_FAILPOINT="themes"
        )

        self.assertEqual(9, result.returncode)
        self.assertIn("isolated test failpoint after themes", result.stderr)
        self.assertNotIn("SURVIVED", result.stdout)

    def test_a_different_failpoint_name_never_fires(self) -> None:
        result = self.failpoint(
            "themes",
            SESSION_KIT_TESTING="1",
            SESSION_KIT_TEST_FAILPOINT="theme-copy",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("SURVIVED", result.stdout)

    def test_the_theme_copy_failpoint_shares_the_one_gate(self) -> None:
        """`session_kit_install.sh` asks the same helper, so it cannot drift."""
        source = (REPO / "lib/sh/session_kit_install.sh").read_text(encoding="utf-8")

        self.assertIn("lifecycle_failpoint_armed theme-copy", source)
        self.assertNotIn("SESSION_KIT_TEST_FAILPOINT", source)


class CodexHandoffFailpointGateTests(unittest.TestCase):
    def test_the_handoff_failpoint_reads_the_testing_gate(self) -> None:
        source = BASHRC.read_text(encoding="utf-8")

        self.assertIn('os.environ.get("SESSION_KIT_TESTING") == "1"', source)
        self.assertEqual(
            2, source.count('failpoint_armed("prompt-after-quarantine-move")')
        )
        # No caller may read the raw variable and skip the gate.
        self.assertEqual(
            1, source.count('os.environ.get("SESSION_KIT_TEST_FAILPOINT")')
        )


class ProviderHooksFailpointGateTests(unittest.TestCase):
    def test_both_provider_hook_failpoints_read_the_testing_gate(self) -> None:
        source = PROVIDER_HOOKS.read_text(encoding="utf-8")

        self.assertEqual(
            2, source.count('os.environ.get("SESSION_KIT_TEST_FAILPOINT")')
        )
        self.assertEqual(
            2, source.count('os.environ.get("SESSION_KIT_TESTING") == "1"')
        )


class ProviderSnapshotFixtureGateTests(unittest.TestCase):
    """The fixture hooks replace the provider's own answer about what exists."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Path(self.temporary.name) / "agents.json"
        self.fixture.write_text(
            '[{"sessionId": "00000000-0000-4000-8000-000000000001"}]',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command_json(self, environ: dict[str, str]) -> object:
        called: list[list[str]] = []

        def runner(argv, timeout):  # noqa: ANN001 - mirrors the injected Runner
            called.append(list(argv))
            return "[]"

        payload = inventory_common._command_json(
            fixture_env="SESSION_KIT_CLAUDE_JSON_FILE",
            command_env="SESSION_KIT_CLAUDE_CMD",
            default_command=("claude", "agents", "--json"),
            runner=runner,
            timeout=1.0,
            environ=environ,
            load_json_file=lambda path: {"fixture": str(path)},
            command_from_env=lambda name, default: [
                *environ.get(name, default).split()
            ],
        )
        return payload, called

    def test_snapshot_fixture_is_ignored_without_the_testing_gate(self) -> None:
        payload, called = self.command_json(
            {"SESSION_KIT_CLAUDE_JSON_FILE": str(self.fixture)}
        )

        self.assertEqual([], payload)
        self.assertEqual([["claude", "agents", "--json"]], called)

    def test_snapshot_fixture_is_honoured_under_the_testing_gate(self) -> None:
        payload, called = self.command_json(
            {
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_CLAUDE_JSON_FILE": str(self.fixture),
            }
        )

        self.assertEqual({"fixture": str(self.fixture)}, payload)
        self.assertEqual([], called)

    def test_registry_fixture_is_ignored_without_the_testing_gate(self) -> None:
        """Without the gate the real `claude` command decides, not the file."""
        missing = Path(self.temporary.name) / "no-such-binary"

        entries = claude_send.registry_entries(
            {
                "SESSION_KIT_CLAUDE_JSON_FILE": str(self.fixture),
                "SESSION_KIT_CLAUDE_CMD": str(missing),
            }
        )

        self.assertEqual([], entries)

    def test_registry_fixture_is_honoured_under_the_testing_gate(self) -> None:
        missing = Path(self.temporary.name) / "no-such-binary"

        entries = claude_send.registry_entries(
            {
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_CLAUDE_JSON_FILE": str(self.fixture),
                "SESSION_KIT_CLAUDE_CMD": str(missing),
            }
        )

        self.assertEqual(
            ["00000000-0000-4000-8000-000000000001"],
            [entry["sessionId"] for entry in entries],
        )


class ProcRootGateTests(unittest.TestCase):
    """`/proc` is where every identity proof in the kit gets its answer."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fake_proc = Path(self.temporary.name) / "proc"
        self.fake_proc.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fixture_root_is_ignored_without_the_testing_gate(self) -> None:
        chosen = inventory_common.proc_root(
            {"SESSION_KIT_PROC_ROOT": os.fspath(self.fake_proc)}
        )

        self.assertEqual(Path("/proc"), chosen)

    def test_fixture_root_is_honoured_under_the_testing_gate(self) -> None:
        chosen = inventory_common.proc_root(
            {
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_PROC_ROOT": os.fspath(self.fake_proc),
            }
        )

        self.assertEqual(self.fake_proc, chosen)

    def test_an_unset_fixture_root_is_the_real_one(self) -> None:
        self.assertEqual(Path("/proc"), inventory_common.proc_root({}))
        self.assertEqual(
            Path("/proc"), inventory_common.proc_root({"SESSION_KIT_TESTING": "1"})
        )

    def test_the_reaper_reads_the_real_proc_without_the_gate(self) -> None:
        result = run(
            [
                "bash",
                "-c",
                'set -a; source /dev/stdin <<<"$(sed -n "1,30p" "$1")"; '
                'printf "%s\\n" "$PROC_ROOT"',
                "reaper-proc-root-test",
                REPO / "bin" / "shpool_reaper",
            ],
            env=sandbox_env(SESSION_KIT_PROC_ROOT=os.fspath(self.fake_proc)),
            check=False,
        )

        self.assertEqual("/proc", result.stdout.strip(), result.stderr)

    def test_the_reaper_reads_the_fixture_proc_under_the_gate(self) -> None:
        result = run(
            [
                "bash",
                "-c",
                'set -a; source /dev/stdin <<<"$(sed -n "1,30p" "$1")"; '
                'printf "%s\\n" "$PROC_ROOT"',
                "reaper-proc-root-test",
                REPO / "bin" / "shpool_reaper",
            ],
            env=sandbox_env(
                SESSION_KIT_TESTING="1",
                SESSION_KIT_PROC_ROOT=os.fspath(self.fake_proc),
            ),
            check=False,
        )

        self.assertEqual(os.fspath(self.fake_proc), result.stdout.strip(), result.stderr)


class NoUngatedTestHookRemainsTests(unittest.TestCase):
    """A regression net: a new ungated hook fails here, not in production."""

    HOOKS = (
        "SESSION_KIT_PROVIDER_PRESENCE_OVERRIDE",
        "SESSION_KIT_TEST_FAILPOINT",
        "SESSION_KIT_TEST_FAILPOINT_MODE",
        "SESSION_KIT_TEST_MONOTONIC_NS",
        "SESSION_KIT_TEST_PLATFORM",
        "SESSION_KIT_PROC_ROOT",
        "SESSION_KIT_SHPOOL_JSON_FILE",
        "SESSION_KIT_CLAUDE_JSON_FILE",
    )
    # The files that own a gate. Every other reader has to sit beside one.
    GATEKEEPERS = {
        "bin/session_kit_common",
        "bin/shpool_reaper",
        "lib/sh/session_kit_lifecycle.sh",
        "lib/sessionkit_inventory/common.py",
    }

    def production_files(self) -> list[Path]:
        listed = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.split()
        keep = []
        for name in listed:
            if name.startswith(("tests/", "docs/", ".github/")):
                continue
            path = REPO / name
            if path.suffix in {".md", ".json", ".txt", ".patch", ".tmTheme"}:
                continue
            if path.is_file():
                keep.append(path)
        return keep

    def test_every_test_hook_read_sits_next_to_the_testing_gate(self) -> None:
        offenders = []
        for path in self.production_files():
            relative = path.relative_to(REPO).as_posix()
            if relative in self.GATEKEEPERS:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(lines, 1):
                if not any(hook in line for hook in self.HOOKS):
                    continue
                # The gate may sit on this line or within three lines either
                # side, which covers every multi-line condition in the tree.
                window = "\n".join(lines[max(0, number - 4) : number + 3])
                if "SESSION_KIT_TESTING" in window:
                    continue
                if any(
                    marker in line
                    for marker in (
                        "sk_test_hook",
                        "failpoint_armed",
                        "proc_root(",
                        # Names the variable for `_command_json`, which is
                        # where that hook's gate lives.
                        "fixture_env=",
                    )
                ):
                    continue
                if line.lstrip().startswith(("#", "``", '"')):
                    continue
                offenders.append(f"{relative}:{number}: {line.strip()}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
