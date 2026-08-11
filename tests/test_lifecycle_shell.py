from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest

from tests.support import REPO, run


BASHRC = REPO / "bashrc" / "shpool.bashrc"
CORE = REPO / "lib" / "session_inventory.py"
BOOT_ID = "11111111-2222-3333-4444-555555555555"
UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class TimeoutPipelineTests(unittest.TestCase):
    def test_timeout_preserves_pipeline_stdin(self) -> None:
        command = (
            'source "$1"; printf "native-color-input\\n" | '
            "sk_timeout 5 bash -c 'read -r value; printf \"%s\\n\" \"$value\"'"
        )
        completed = subprocess.run(
            [
                "bash",
                "-c",
                command,
                "timeout-test",
                str(REPO / "bin" / "session_kit_common"),
            ],
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("native-color-input\n", completed.stdout)


class PortableMktempTests(unittest.TestCase):
    def test_runtime_templates_end_with_replacement_characters(self) -> None:
        template_pattern = re.compile(
            r'mktemp(?:\s+-d)?\s+"([^"\n]*XXXXXX[^"\n]*)"'
        )
        runtime_files = [
            path
            for path in (REPO / "bin").iterdir()
            if path.is_file()
        ]
        runtime_files.append(REPO / "tests" / "run")
        invalid: list[str] = []
        for path in runtime_files:
            text = path.read_text(encoding="utf-8")
            for line_number, line in enumerate(text.splitlines(), start=1):
                for match in template_pattern.finditer(line):
                    if not match.group(1).endswith("XXXXXX"):
                        invalid.append(
                            f"{path.relative_to(REPO)}:{line_number}:"
                            f" {match.group(1)}"
                        )
        self.assertEqual([], invalid)


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class ProviderExitShellHarness(unittest.TestCase):
    """Fixture only: launches the bashrc against provider stubs. No tests."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix=".lifecycle-shell-", dir=REPO
        )
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.state = self.base / "state"
        self.start = self.base / "start"
        self.project = self.base / "project"
        # The bashrc prepends $HOME/.cargo/bin:$HOME/.local/bin:/opt/homebrew/bin
        # to PATH. On macOS a real provider in the Homebrew prefix would
        # therefore shadow a stub placed anywhere later, so the stub lives in
        # the fixture HOME, which that prepend puts first on both platforms.
        self.bin = self.home / ".local" / "bin"
        for path in (
            self.home,
            self.state,
            self.start,
            self.project,
        ):
            path.mkdir(mode=0o700)
        self.bin.mkdir(mode=0o700, parents=True)
        self.config = self.base / "inventory.json"
        self.config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "state_dir": str(self.state),
                    "aliases": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.config.chmod(0o600)
        self.boot = self.base / "boot-id"
        self.boot.write_text(BOOT_ID + "\n", encoding="utf-8")
        self.provider_log = self.base / "provider.log"
        self.account_environment_log = self.base / "account-environment.log"
        self.account_profile = self.base / "codex-profile"
        self.account_profile.mkdir(mode=0o700)
        self.core = CORE
        self.environment_overrides: dict[str, str] = {}
        write_executable(
            self.bin / "codex",
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$PROVIDER_LOG"\n'
            'if [[ -n ${ACCOUNT_ENV_LOG:-} ]]; then '
            'printf "%s\\t%s\\t%s\\n" "$CODEX_HOME" "$SESSION_KIT_ACCOUNT_ALIAS" '
            '"$SESSION_KIT_ACCOUNT_CAPABLE" >> "$ACCOUNT_ENV_LOG"; fi\n',
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:{environment['PATH']}",
                "SHPOOL_SESSION_NAME": "main2",
                "SHPOOL_JOURNAL": "disabled",
                "SESSION_KIT_BOOT_ID_FILE": str(self.boot),
                "SESSION_KIT_CONFIG": str(self.config),
                "SESSION_KIT_INVENTORY_CORE": str(self.core),
                "SESSION_KIT_START_DIR": str(self.start),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "PROVIDER_LOG": str(self.provider_log),
                "ACCOUNT_ENV_LOG": str(self.account_environment_log),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        environment.update(self.environment_overrides)
        return environment

    def launch(
        self, choices: str, *, account_alias: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        record = self.start / "main2"
        record.write_text(
            f"codex\t{self.project}\t{UUID}\tresume\n",
            encoding="utf-8",
        )
        if account_alias is not None:
            account = self.start / "main2.account"
            account.write_text(f"codex\t{account_alias}\n", encoding="utf-8")
            account.chmod(0o600)
        # The generation of a process is read from /proc on Linux and from the
        # native adapter on Darwin, exactly as the bashrc itself does, so this
        # harness exercises the real launch path on both platforms.
        command = r"""
bash --noprofile --norc -ic '
  # Captured out here on purpose: inside the function, $6 would refer to the
  # sixth argument of the function rather than of this script.
  inventory_core="$6"
  start_ticks() {
    if [ -r "/proc/$1/stat" ]; then
      awk "{print \$22}" "/proc/$1/stat"
    else
      python3 "$inventory_core" platform process-info "$1" | cut -f2
    fi
  }
  shell_start=$(start_ticks $$)
  parent_start=$(start_ticks $PPID)
  printf "%s\t%s\t%s\t1\t%s\t%s\t%s\t%s\t%s\tresume\n" \
    codex "$2" "$4" "$$" "$shell_start" "$PPID" "$parent_start" "$5" \
    > "$3/main2.expected"
  source "$1"
  printf "SOURCE_RETURNED\n"
' lifecycle-inner "$1" "$2" "$3" "$4" "$5" "$6"
"""
        return subprocess.run(
            [
                "bash",
                "-c",
                command,
                "lifecycle-outer",
                str(BASHRC),
                str(self.project),
                str(self.start),
                BOOT_ID,
                UUID,
                str(self.core),
            ],
            cwd=REPO,
            env=self.environment(),
            input=choices,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )

    def lifecycle_document(self) -> dict:
        lifecycle_dir = self.state / "lifecycle"
        candidates = sorted(
            path
            for path in lifecycle_dir.glob("*.json")
            if path.name != "key.json" and not path.name.endswith(".exact.json")
        )
        self.assertEqual(1, len(candidates))
        self.assertEqual(0o600, stat.S_IMODE(candidates[0].stat().st_mode))
        return json.loads(candidates[0].read_text(encoding="utf-8"))

    def autoclose_on_clean_exit(self) -> None:
        """Opt this terminal out of the recovery menu on a clean exit."""
        (self.home / ".sk_autoclose_on_clean_exit").write_text(
            "", encoding="utf-8"
        )


class ProviderExitShellTests(ProviderExitShellHarness):
    def test_clean_exit_with_the_autoclose_marker_closes_without_a_menu(
        self,
    ) -> None:
        # The opt-in marker restores the old behavior: /exit lands straight
        # back on the picker. Without the marker the menu is the default,
        # which tests/test_exit_stayalive.py covers.
        self.autoclose_on_clean_exit()
        completed = self.launch("")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("Provider exited:", completed.stdout)
        self.assertNotIn("exited with status", completed.stdout)
        # The shell closed rather than returning to the sourcing caller.
        self.assertNotIn("SOURCE_RETURNED", completed.stdout)
        state = self.lifecycle_document()
        self.assertTrue(state["user_input_after_exit"])

    def test_account_record_exports_exact_codex_profile_before_resume(self) -> None:
        wrapper = self.base / "inventory-wrapper.py"
        wrapper.write_text(
            """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:4] == ["account", "resume-profile", "codex"]:
    print(json.dumps({
        "provider": "codex",
        "alias": sys.argv[4],
        "email": "account@example.com",
        "profile_dir": os.environ["ACCOUNT_PROFILE"],
        "plan": "plus",
    }))
    raise SystemExit(0)
os.execv(sys.executable, [sys.executable, os.environ["REAL_CORE"], *sys.argv[1:]])
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        self.core = wrapper
        self.environment_overrides = {
            "ACCOUNT_PROFILE": str(self.account_profile),
            "REAL_CORE": str(CORE),
        }
        completed = self.launch("", account_alias="work")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(
            self.account_environment_log.exists(),
            completed.stdout + completed.stderr,
        )
        self.assertEqual(
            f"{self.account_profile}\twork\t1\n",
            self.account_environment_log.read_text(encoding="utf-8"),
        )
        self.assertFalse((self.start / "main2.account").exists())

    def test_crashed_provider_still_stops_at_the_recovery_menu(self) -> None:
        write_executable(
            self.bin / "codex",
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$PROVIDER_LOG"\nexit 3\n',
        )
        completed = self.launch("c\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited with status 3", completed.stdout)
        self.assertIn("Provider exited: [r] reopen conversation", completed.stdout)

    def test_provider_exit_stays_in_controlled_menu_until_input_is_recorded(
        self,
    ) -> None:
        completed = self.launch("s\nexit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited with status 0", completed.stdout)
        self.assertIn("Provider exited: [r] reopen conversation", completed.stdout)
        self.assertIn("Shell opened", completed.stdout)
        self.assertIn("SOURCE_RETURNED", completed.stdout)
        self.assertEqual(
            (
                "-c check_for_update_on_startup=false "
                f"--no-alt-screen resume {UUID}\n"
            ),
            self.provider_log.read_text(encoding="utf-8"),
        )
        state = self.lifecycle_document()
        self.assertTrue(state["input_tracking"])
        self.assertTrue(state["user_input_after_exit"])
        self.assertNotIn("user_input_after_exit_at", state)
        self.assertFalse((self.start / "main2").exists())
        self.assertFalse((self.start / "main2.expected").exists())

    def test_unknown_choice_is_permanent_input_before_close(self) -> None:
        completed = self.launch("not-a-choice\nc\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Unknown choice", completed.stdout)
        self.assertNotIn("SOURCE_RETURNED", completed.stdout)
        self.assertTrue(
            self.lifecycle_document()["user_input_after_exit"]
        )

    @unittest.skipUnless(
        Path("/proc/1/stat").exists(), "reads a foreign process generation from /proc"
    )
    def test_unrelated_process_cannot_forge_lifecycle_generation(self) -> None:
        pid_one_fields = Path("/proc/1/stat").read_text(encoding="utf-8")
        pid_one_start = int(pid_one_fields[pid_one_fields.rfind(")") + 2 :].split()[19])
        environment = self.environment()
        environment.update(
            {
                "SESSION_KIT_LIFECYCLE_SESSION_ID": "main2",
                "SESSION_KIT_LIFECYCLE_BOOT_ID": BOOT_ID,
                "SESSION_KIT_LIFECYCLE_SHELL_PID": "1",
                "SESSION_KIT_LIFECYCLE_SHELL_START_TICKS": str(pid_one_start),
                "SESSION_KIT_LIFECYCLE_PROVIDER": "codex",
                "SESSION_KIT_LIFECYCLE_EXIT_CODE": "0",
            }
        )
        forged = run(
            [CORE, "lifecycle", "provider-exited"],
            env=environment,
            check=False,
        )
        self.assertNotEqual(0, forged.returncode)
        self.assertIn("outside the exact", forged.stderr)
        lifecycle_dir = self.state / "lifecycle"
        self.assertFalse(lifecycle_dir.exists())


if __name__ == "__main__":
    unittest.main()
