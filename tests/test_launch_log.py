"""Every launch decision reaches the action log, named by session.

Three launches started nothing one night. Each one printed its reason to the
session's own screen and nowhere else, so the only way to learn why was to
attach to the session before somebody closed it, and by the time anyone
asked, all three were closed. Hours went into reconstructing an answer the
machine had already computed and thrown away.

The shell now records what it decided and which session it decided about, and
`sp new` records that it created one at all, a verb that makes state and
leaves no trace of having run cannot answer "what made this session".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from tests.support import REPO, run


BASHRC = REPO / "bashrc/shpool.bashrc"
CORE = REPO / "lib/session_inventory.py"
SESSION = "s20260812-000000-424242"


class LaunchLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".launch-log-", dir=REPO.parent
        )
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.start = self.root / "start"
        self.start.mkdir(mode=0o700)
        self.project = self.root / "project"
        self.project.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.boot = self.root / "boot-id"
        self.boot.write_text("fixture-boot\n", encoding="utf-8")
        self.provider_log = self.root / "provider.log"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.start / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def stub_claude(self) -> None:
        executable = self.bin / "claude"
        executable.write_text(
            "#!/usr/bin/env bash\nprintf 'launched %s\\n' \"$*\" > \"$PROVIDER_LOG\"\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)

    def environment(self) -> dict:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "PROVIDER_LOG": str(self.provider_log),
                "SESSION_KIT_BOOT_ID_FILE": str(self.boot),
                "SESSION_KIT_INVENTORY_CORE": str(CORE),
                "SESSION_KIT_START_DIR": str(self.start),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "XDG_STATE_HOME": str(self.state),
                "SHPOOL_JOURNAL": "disabled",
                "SHPOOL_SESSION_NAME": SESSION,
            }
        )
        return env

    def launch(self, inner: str) -> None:
        completed = run(
            [
                "bash",
                "-c",
                'bash --noprofile --norc -ic "$1" launch-inner "$2" "$3" "$4"',
                "launch-log-test",
                inner,
                str(BASHRC),
                str(self.project),
                str(self.start),
            ],
            env=self.environment(),
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def entries(self) -> list[dict]:
        # SESSION_KIT_STATE_DIR is the state root itself; without it the kit
        # falls back to $XDG_STATE_HOME/session-kit. Read whichever exists so
        # the test pins the records, not the layout.
        path = self.state / "action-events.jsonl"
        if not path.exists():
            path = self.state / "session-kit" / "action-events.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def launch_outcomes(self) -> list[tuple[str, str]]:
        return [
            (item["outcome"], item.get("session", ""))
            for item in self.entries()
            if item.get("action") == "launch"
        ]

    # -- the refusals -----------------------------------------------------
    def test_an_incomplete_record_says_so_on_the_log(self) -> None:
        self.write(SESSION, f"claude\t{self.project}\t\tnew\n")
        self.launch('source "$1"')
        self.assertIn(("refused_record_incomplete", SESSION), self.launch_outcomes())

    def test_a_shell_launch_is_not_reported_as_an_incomplete_record(self) -> None:
        """A plain shell is what `sp new shell` was asked for.

        It arms its record and clears it once the shell is open, so the
        sidecar is legitimately absent -- and every healthy shell session
        opened with a line calling itself broken.
        """
        self.write(SESSION, f"shell\t{self.project}\t\tnew\n")
        self.launch('source "$1"')
        self.assertEqual([], self.launch_outcomes())

    def test_a_generation_that_does_not_match_says_so_on_the_log(self) -> None:
        self.stub_claude()
        self.write(SESSION, f"claude\t{self.project}\t\tnew\n")
        # A shell pid that is not this shell: the exact husk shape.
        self.write(
            f"{SESSION}.expected",
            f"claude\t{self.project}\tfixture-boot\t1\t999999\t1\t999998\t1\t\tnew\n",
        )
        self.launch('source "$1"')
        self.assertIn(
            ("refused_generation_mismatch", SESSION), self.launch_outcomes()
        )
        self.assertFalse(self.provider_log.exists())

    def test_an_account_that_cannot_be_verified_says_so_on_the_log(self) -> None:
        self.stub_claude()
        inner = (
            'shell_start=$(awk "{print \\$22}" /proc/$$/stat); '
            'daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat); '
            'printf "claude\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t\\tnew\\n" '
            f'"$2" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/{SESSION}.expected"; '
            f'printf "claude\\tnosuch\\n" > "$3/{SESSION}.account"; '
            f'chmod 600 "$3/{SESSION}.expected" "$3/{SESSION}.account"; source "$1"'
        )
        self.write(SESSION, f"claude\t{self.project}\t\tnew\n")
        self.launch(inner)
        self.assertIn(("refused_account_unsafe", SESSION), self.launch_outcomes())
        self.assertFalse(self.provider_log.exists())

    def test_a_missing_provider_says_so_on_the_log(self) -> None:
        # No stub on PATH: `command -v claude` finds nothing.
        inner = (
            'shell_start=$(awk "{print \\$22}" /proc/$$/stat); '
            'daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat); '
            'printf "claude\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t\\tnew\\n" '
            f'"$2" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/{SESSION}.expected"; '
            f'chmod 600 "$3/{SESSION}.expected"; '
            'PATH=/usr/bin:/bin; source "$1"'
        )
        self.write(SESSION, f"claude\t{self.project}\t\tnew\n")
        self.launch(inner)
        self.assertIn(("refused_provider_missing", SESSION), self.launch_outcomes())

    # -- and the launch that works ---------------------------------------
    def test_a_launch_that_starts_records_that_it_started(self) -> None:
        self.stub_claude()
        inner = (
            'shell_start=$(awk "{print \\$22}" /proc/$$/stat); '
            'daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat); '
            'printf "claude\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t\\tnew\\n" '
            f'"$2" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/{SESSION}.expected"; '
            f'chmod 600 "$3/{SESSION}.expected"; source "$1"'
        )
        self.write(SESSION, f"claude\t{self.project}\t\tnew\n")
        self.launch(inner)
        self.assertIn(("started", SESSION), self.launch_outcomes())
        self.assertTrue(self.provider_log.exists())

    def test_the_log_keeps_records_that_name_a_session(self) -> None:
        """The shell logger rewrites the file; a named record must survive it."""
        from tests.support import run as run_command

        core_env = self.environment()
        run_command(
            [
                "python3",
                str(CORE),
                "action-log",
                "launch",
                "refused_generation_mismatch",
                "--session",
                SESSION,
            ],
            env=core_env,
        )
        # A second writer rewrites the whole file through the shell logger.
        run_command(
            [
                "bash",
                "-c",
                'source "$1"; SK_STATE_DIR="$2" SK_ACTION_LOG="$2/action-events.jsonl" '
                "sk_log_action picker_exit quit",
                "log-test",
                str(REPO / "bin/session_kit_common"),
                str(self.state),
            ],
            env=core_env,
        )
        outcomes = self.launch_outcomes()
        self.assertIn(("refused_generation_mismatch", SESSION), outcomes)


if __name__ == "__main__":
    unittest.main()
