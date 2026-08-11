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
                self.assertIn(f"model={model}", observed)
                self.assertIn(f"key={launch_key}", observed)
                self.assertFalse(start.exists())
                self.assertFalse(Path(f"{start}.expected").exists())
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


if __name__ == "__main__":
    unittest.main()
