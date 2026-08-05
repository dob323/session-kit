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


class ProviderExitShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix=".lifecycle-shell-", dir=REPO
        )
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.state = self.base / "state"
        self.start = self.base / "start"
        self.project = self.base / "project"
        self.bin = self.base / "bin"
        for path in (
            self.home,
            self.state,
            self.start,
            self.project,
            self.bin,
        ):
            path.mkdir(mode=0o700)
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
        write_executable(
            self.bin / "codex",
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$PROVIDER_LOG"\n',
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
                "SESSION_KIT_INVENTORY_CORE": str(CORE),
                "SESSION_KIT_START_DIR": str(self.start),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "PROVIDER_LOG": str(self.provider_log),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return environment

    def launch(self, choices: str) -> subprocess.CompletedProcess[str]:
        record = self.start / "main2"
        record.write_text(
            f"codex\t{self.project}\t{UUID}\tresume\n",
            encoding="utf-8",
        )
        command = r"""
bash --noprofile --norc -ic '
  shell_start=$(awk "{print \$22}" /proc/$$/stat)
  parent_start=$(awk "{print \$22}" /proc/$PPID/stat)
  printf "%s\t%s\t%s\t1\t%s\t%s\t%s\t%s\t%s\tresume\n" \
    codex "$2" "$4" "$$" "$shell_start" "$PPID" "$parent_start" "$5" \
    > "$3/main2.expected"
  source "$1"
  printf "SOURCE_RETURNED\n"
' lifecycle-inner "$1" "$2" "$3" "$4" "$5"
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

    def keep_exit_menu(self) -> None:
        """Opt this terminal back into the recovery menu on a clean exit."""
        (self.home / ".sk_keep_exit_menu").write_text("", encoding="utf-8")

    def test_clean_provider_exit_closes_without_a_menu_or_a_keypress(self) -> None:
        # Typing /exit should land straight back on the picker.
        completed = self.launch("")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("Provider exited:", completed.stdout)
        self.assertNotIn("exited with status", completed.stdout)
        # The shell closed rather than returning to the sourcing caller.
        self.assertNotIn("SOURCE_RETURNED", completed.stdout)
        state = self.lifecycle_document()
        self.assertTrue(state["user_input_after_exit"])

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
        self.keep_exit_menu()
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
        self.keep_exit_menu()
        completed = self.launch("not-a-choice\nc\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Unknown choice", completed.stdout)
        self.assertNotIn("SOURCE_RETURNED", completed.stdout)
        self.assertTrue(
            self.lifecycle_document()["user_input_after_exit"]
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
