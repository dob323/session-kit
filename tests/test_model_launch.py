"""Generation-bound requested models for managed provider launches."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from tests.support import REPO, run


BASHRC = REPO / "bashrc/shpool.bashrc"


class RequestedModelLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".model-launch-", dir=REPO.parent)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        codex_themes = self.home / ".codex" / "themes"
        codex_themes.mkdir(parents=True, mode=0o700)
        (codex_themes / "sk-lime.tmTheme").write_text(
            "fixture theme\n", encoding="utf-8"
        )
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.start = self.root / "start"
        self.start.mkdir(mode=0o700)
        self.project = self.root / "project"
        self.project.mkdir()
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.boot = self.root / "boot-id"
        self.boot.write_text("fixture-boot\n", encoding="utf-8")
        self.inventory = self.root / "inventory.py"
        self.inventory.write_text(
            "#!/usr/bin/env python3\n"
            "import re, sys\n"
            "if sys.argv[1] == 'validate-worker-model':\n"
            " provider, model = sys.argv[2:]\n"
            " safe = re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', model)\n"
            " ok = bool(safe) and ((provider == 'claude' and model.startswith('claude-')) or "
            "(provider == 'codex' and model.startswith(('gpt-', 'o3', 'o4', 'codex-'))))\n"
            " if ok: print(model)\n"
            " raise SystemExit(0 if ok else 1)\n"
            "if sys.argv[1:3] == ['color', 'launch-pick']:\n"
            " print('lime')\n"
            " raise SystemExit(0)\n"
            "if sys.argv[1:3] == ['lifecycle', 'provider-exited']: raise SystemExit(0)\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        self.inventory.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_model_and_launch_key_reach_each_provider(self) -> None:
        values = {
            "claude": ("claude-sonnet-4-5", "worker:review:1"),
            "codex": ("gpt-5.6-codex", "worker:implementation:2"),
        }
        inner = (
            'shell_start=$(awk "{print \\$22}" /proc/$$/stat); '
            'daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat); '
            'printf "%s\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t\\tnew\\n" '
            '"$4" "$2" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/modeled.expected"; '
            'printf "%s\\t%s\\t%s\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\n" '
            '"$4" "$2" "$5" "$6" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/modeled.launch"; '
            'chmod 600 "$3/modeled.expected" "$3/modeled.launch"; source "$1"'
        )
        for provider, (model, launch_key) in values.items():
            with self.subTest(provider=provider):
                for path in self.start.iterdir():
                    path.unlink()
                log = self.root / f"{provider}.log"
                executable = self.fake_bin / provider
                executable.write_text(
                    "#!/usr/bin/env bash\n"
                    "printf 'argv=' > \"$PROVIDER_LOG\"\n"
                    "printf '<%s>' \"$@\" >> \"$PROVIDER_LOG\"\n"
                    "printf '\\nmodel=%s\\nkey=%s\\n' \"$SESSION_KIT_REQUESTED_MODEL\" "
                    "\"$SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY\" >> \"$PROVIDER_LOG\"\n",
                    encoding="utf-8",
                )
                executable.chmod(0o755)
                start = self.start / "modeled"
                start.write_text(f"{provider}\t{self.project}\t\tnew\n", encoding="utf-8")
                start.chmod(0o600)
                environment = os.environ.copy()
                environment.update(
                    {
                        "HOME": str(self.home),
                        "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                        "PROVIDER_LOG": str(log),
                        "SESSION_KIT_BOOT_ID_FILE": str(self.boot),
                        "SESSION_KIT_INVENTORY_CORE": str(self.inventory),
                        "SESSION_KIT_START_DIR": str(self.start),
                        "SESSION_KIT_STATE_DIR": str(self.state),
                        "SHPOOL_JOURNAL": "disabled",
                        "SHPOOL_SESSION_NAME": "modeled",
                    }
                )
                launched = run(
                    [
                        "bash", "-c",
                        'bash --noprofile --norc -ic "$1" model-inner "$2" "$3" "$4" "$5" "$6" "$7"',
                        "model-launch-test", inner, BASHRC, self.project, self.start,
                        provider, model, launch_key,
                    ],
                    env=environment,
                )
                self.assertEqual(0, launched.returncode, launched.stderr)
                observed = log.read_text(encoding="utf-8")
                self.assertIn(f"<--model><{model}>", observed)
                if provider == "codex":
                    self.assertIn('<-c><tui.theme="sk-lime">', observed)
                self.assertIn(f"model={model}", observed)
                self.assertIn(f"key={launch_key}", observed)
                self.assertFalse(start.exists())
                self.assertFalse(Path(f"{start}.expected").exists())
                self.assertFalse(Path(f"{start}.launch").exists())

    def test_a_model_launch_with_no_launch_key_still_starts_the_provider(self) -> None:
        """The default `sp new --model` writes an EMPTY launch key.

        Tab is IFS whitespace, so `IFS=$'\\t' read` collapses the run of tabs
        around an empty field and shifts every generation value one place
        left: the boot id lands in the key, the daemon start lands nowhere,
        and the cross-check refuses a record that was armed perfectly. The
        session then sits as a shell with no provider — three of them in one
        night — and the test suite never saw it because every case here
        passed a key.
        """
        provider, model = "claude", "claude-opus-5"
        log = self.root / "nokey.log"
        executable = self.fake_bin / provider
        executable.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'argv=' > \"$PROVIDER_LOG\"\n"
            "printf '<%s>' \"$@\" >> \"$PROVIDER_LOG\"\n"
            "printf '\\nmodel=%s\\nkey=[%s]\\n' \"$SESSION_KIT_REQUESTED_MODEL\" "
            "\"$SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY\" >> \"$PROVIDER_LOG\"\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        # The fourth field is empty, exactly as sk_arm_launch_request writes it
        # when nobody passed --launch-key.
        inner = (
            'shell_start=$(awk "{print \\$22}" /proc/$$/stat); '
            'daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat); '
            'printf "%s\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t\\tnew\\n" '
            '"$4" "$2" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/nokey.expected"; '
            'printf "%s\\t%s\\t%s\\t\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\n" '
            '"$4" "$2" "$5" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/nokey.launch"; '
            'chmod 600 "$3/nokey.expected" "$3/nokey.launch"; source "$1"'
        )
        start = self.start / "nokey"
        start.write_text(f"{provider}\t{self.project}\t\tnew\n", encoding="utf-8")
        start.chmod(0o600)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "PROVIDER_LOG": str(log),
                "SESSION_KIT_BOOT_ID_FILE": str(self.boot),
                "SESSION_KIT_INVENTORY_CORE": str(self.inventory),
                "SESSION_KIT_START_DIR": str(self.start),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SHPOOL_JOURNAL": "disabled",
                "SHPOOL_SESSION_NAME": "nokey",
            }
        )
        launched = run(
            [
                "bash", "-c",
                'bash --noprofile --norc -ic "$1" model-inner "$2" "$3" "$4" "$5" "$6"',
                "model-launch-test", inner, BASHRC, self.project, self.start,
                provider, model,
            ],
            env=environment,
        )
        self.assertEqual(0, launched.returncode, launched.stderr)
        self.assertTrue(log.exists(), "the provider never started")
        observed = log.read_text(encoding="utf-8")
        self.assertIn(f"<--model><{model}>", observed)
        self.assertIn("key=[]", observed)
        # A launch consumes its records; a refusal leaves them behind.
        self.assertFalse(start.exists())
        self.assertFalse(Path(f"{start}.launch").exists())

    def test_unsafe_or_generation_mismatched_model_record_never_launches(self) -> None:
        provider_log = self.root / "refused.log"
        executable = self.fake_bin / "claude"
        executable.write_text(
            "#!/usr/bin/env bash\nprintf launched > \"$PROVIDER_LOG\"\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        start = self.start / "modeled"
        start.write_text(f"claude\t{self.project}\t\tnew\n", encoding="utf-8")
        start.chmod(0o600)
        inner = (
            'shell_start=$(awk "{print \\$22}" /proc/$$/stat); '
            'daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat); '
            'printf "claude\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t\\tnew\\n" '
            '"$2" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/modeled.expected"; '
            'printf "claude\\t%s\\t%s\\tworker:bad:1\\twrong-boot\\t1\\t%s\\t%s\\t%s\\t%s\\n" '
            '"$2" "$4" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/modeled.launch"; '
            'chmod 600 "$3/modeled.expected" "$3/modeled.launch"; source "$1"'
        )
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "PROVIDER_LOG": str(provider_log),
                "SESSION_KIT_BOOT_ID_FILE": str(self.boot),
                "SESSION_KIT_INVENTORY_CORE": str(self.inventory),
                "SESSION_KIT_START_DIR": str(self.start),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SHPOOL_JOURNAL": "disabled",
                "SHPOOL_SESSION_NAME": "modeled",
            }
        )
        refused = run(
            [
                "bash", "-c",
                'bash --noprofile --norc -ic "$1" model-inner "$2" "$3" "$4" "$5"',
                "model-launch-refusal", inner, BASHRC, self.project, self.start,
                "claude-sonnet-4-5",
            ],
            env=environment,
        )
        self.assertIn("stale or mismatched launch record retained", refused.stderr)
        self.assertFalse(provider_log.exists())
        self.assertTrue(start.exists())
        self.assertTrue(Path(f"{start}.launch").exists())

    def test_codex_seed_prompt_is_delivered_once_and_consumed(self) -> None:
        log = self.root / "seeded.log"
        executable = self.fake_bin / "codex"
        executable.write_text(
            "#!/usr/bin/env bash\n"
            "printf '<%s>' \"$@\" > \"$PROVIDER_LOG\"\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        start = self.start / "seeded"
        start.write_text(f"codex\t{self.project}\t\tnew\n", encoding="utf-8")
        start.chmod(0o600)
        handoff = self.start / "seeded.prompt"
        prompt = "Inspect both providers.\nReturn the exact parity gaps."
        handoff.write_text(prompt, encoding="utf-8")
        handoff.chmod(0o600)
        inner = (
            'shell_start=$(awk "{print \\$22}" /proc/$$/stat); '
            'daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat); '
            'printf "codex\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t\\tnew\\n" '
            '"$2" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/seeded.expected"; '
            'chmod 600 "$3/seeded.expected"; source "$1"'
        )
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "PROVIDER_LOG": str(log),
                "SESSION_KIT_BOOT_ID_FILE": str(self.boot),
                "SESSION_KIT_INVENTORY_CORE": str(self.inventory),
                "SESSION_KIT_START_DIR": str(self.start),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SHPOOL_JOURNAL": "disabled",
                "SHPOOL_SESSION_NAME": "seeded",
            }
        )
        launched = run(
            [
                "bash", "-c",
                'bash --noprofile --norc -ic "$1" seed-inner "$2" "$3" "$4"',
                "seed-prompt-test", inner, BASHRC, self.project, self.start,
            ],
            env=environment,
        )
        self.assertEqual(0, launched.returncode, launched.stderr)
        observed = log.read_text(encoding="utf-8")
        self.assertIn(f"<--><{prompt}>", observed)
        self.assertEqual(1, observed.count(prompt))
        self.assertFalse(handoff.exists())

    def test_codex_seed_prompt_never_follows_a_symlink(self) -> None:
        log = self.root / "symlinked.log"
        executable = self.fake_bin / "codex"
        executable.write_text(
            "#!/usr/bin/env bash\nprintf '<%s>' \"$@\" > \"$PROVIDER_LOG\"\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        start = self.start / "symlinked"
        start.write_text(f"codex\t{self.project}\t\tnew\n", encoding="utf-8")
        start.chmod(0o600)
        secret = self.root / "must-not-read"
        secret.write_text("sensitive fixture text\n", encoding="utf-8")
        handoff = self.start / "symlinked.prompt"
        handoff.symlink_to(secret)
        inner = (
            'shell_start=$(awk "{print \\$22}" /proc/$$/stat); '
            'daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat); '
            'printf "codex\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t\\tnew\\n" '
            '"$2" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/symlinked.expected"; '
            'chmod 600 "$3/symlinked.expected"; source "$1"'
        )
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "PROVIDER_LOG": str(log),
                "SESSION_KIT_BOOT_ID_FILE": str(self.boot),
                "SESSION_KIT_INVENTORY_CORE": str(self.inventory),
                "SESSION_KIT_START_DIR": str(self.start),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SHPOOL_JOURNAL": "disabled",
                "SHPOOL_SESSION_NAME": "symlinked",
            }
        )
        launched = run(
            [
                "bash", "-c",
                'bash --noprofile --norc -ic "$1" seed-inner "$2" "$3" "$4"',
                "seed-symlink-test", inner, BASHRC, self.project, self.start,
            ],
            env=environment,
        )
        self.assertEqual(0, launched.returncode, launched.stderr)
        self.assertNotIn("sensitive fixture text", log.read_text(encoding="utf-8"))
        self.assertTrue(handoff.is_symlink())


class PreBakedResumeFallbackTests(unittest.TestCase):
    """A resume that cannot succeed must still open a session.

    The pre-bake mints its throwaway conversation inside one profile and the
    session shell resolves its own; if those ever disagree the launch records
    have already been consumed, so `claude --resume` failed with "No
    conversation found", that was read as a crash, the SAME conversation was
    reopened once, it failed the same way, and the person was left with an
    open window containing no provider at all. A conversation nothing can
    find is nothing to lose: open a new one instead, which still takes its
    colour from the kit.
    """

    UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".resume-fallback-", dir=REPO.parent
        )
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        (self.home / ".claude" / "projects").mkdir(parents=True, mode=0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.start = self.root / "start"
        self.start.mkdir(mode=0o700)
        self.project = self.root / "project"
        self.project.mkdir()
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.boot = self.root / "boot-id"
        self.boot.write_text("fixture-boot\n", encoding="utf-8")
        self.inventory = self.root / "inventory.py"
        self.inventory.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if sys.argv[1:3] == ['lifecycle', 'provider-exited']: raise SystemExit(0)\n"
            "if sys.argv[1] == 'action-log': raise SystemExit(0)\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        self.inventory.chmod(0o755)
        self.log = self.root / "claude.log"
        executable = self.fake_bin / "claude"
        executable.write_text(
            "#!/usr/bin/env bash\n"
            "printf '<%s>' \"$@\" >> \"$PROVIDER_LOG\"\n"
            "printf '\\n' >> \"$PROVIDER_LOG\"\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        self.addCleanup(self.temporary.cleanup)

    def launch(self):
        start = self.start / "resumed"
        start.write_text(
            f"claude\t{self.project}\t{self.UUID}\tresume\n", encoding="utf-8"
        )
        start.chmod(0o600)
        inner = (
            'shell_start=$(awk "{print \\$22}" /proc/$$/stat); '
            'daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat); '
            'printf "claude\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t%s\\tresume\\n" '
            '"$2" "$$" "$shell_start" "$PPID" "$daemon_start" "$4" '
            '> "$3/resumed.expected"; '
            'chmod 600 "$3/resumed.expected"; source "$1"'
        )
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "PROVIDER_LOG": str(self.log),
                "SESSION_KIT_BOOT_ID_FILE": str(self.boot),
                "SESSION_KIT_INVENTORY_CORE": str(self.inventory),
                "SESSION_KIT_START_DIR": str(self.start),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "XDG_STATE_HOME": str(self.state),
                "SHPOOL_JOURNAL": "disabled",
                "SHPOOL_SESSION_NAME": "resumed",
            }
        )
        environment.pop("CLAUDE_CONFIG_DIR", None)
        launched = run(
            [
                "bash",
                "-c",
                'bash --noprofile --norc -ic "$1" resume-inner "$2" "$3" "$4" "$5"',
                "resume-launch-test",
                inner,
                str(BASHRC),
                str(self.project),
                str(self.start),
                self.UUID,
            ],
            env=environment,
        )
        self.assertEqual(0, launched.returncode, launched.stderr)
        return launched, self.log.read_text(encoding="utf-8")

    def test_a_conversation_this_profile_does_not_hold_opens_a_new_one(self) -> None:
        launched, observed = self.launch()

        self.assertNotIn("<--resume>", observed)
        self.assertIn("<--session-id>", observed)
        # A NEW conversation, never the one that could not be found.
        self.assertNotIn(self.UUID, observed)
        self.assertIn("opening a new one instead", launched.stderr)
        # Exactly one launch: a fallback, not a loop.
        self.assertEqual(1, observed.count("<--session-id>"))

    def test_a_conversation_that_is_there_is_still_resumed(self) -> None:
        """The fallback must not fire on a resume that can work."""
        project = self.home / ".claude" / "projects" / "-fixture"
        project.mkdir(parents=True)
        (project / f"{self.UUID}.jsonl").write_text(
            '{"type":"user"}\n', encoding="utf-8"
        )

        launched, observed = self.launch()

        self.assertIn(f"<--resume><{self.UUID}>", observed)
        self.assertNotIn("<--session-id>", observed)
        self.assertNotIn("opening a new one instead", launched.stderr)


if __name__ == "__main__":
    unittest.main()
