"""Session colors: stable identity hash, overrides, provider-native push."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO

CORE_PATH = REPO / "lib" / "session_inventory.py"
if "session_inventory" in sys.modules:
    inventory_core = sys.modules["session_inventory"]
else:
    CORE_SPEC = importlib.util.spec_from_file_location(
        "session_inventory", CORE_PATH
    )
    assert CORE_SPEC is not None and CORE_SPEC.loader is not None
    inventory_core = importlib.util.module_from_spec(CORE_SPEC)
    sys.modules[CORE_SPEC.name] = inventory_core
    CORE_SPEC.loader.exec_module(inventory_core)


def uuid_for(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


class SessionColorTests(unittest.TestCase):
    def test_session_color_is_stable_and_override_wins(self) -> None:
        exact = uuid_for(61)
        first = inventory_core.session_color("codex", exact)
        second = inventory_core.session_color("codex", exact)
        self.assertEqual(first, second)
        self.assertIn(first, inventory_core.SESSION_COLORS)
        self.assertNotEqual(
            inventory_core.session_color("claude", exact),
            None,
        )
        override = {"codex:" + exact: "pink"}
        self.assertEqual(
            "pink", inventory_core.session_color("codex", exact, override)
        )
        self.assertIsNone(inventory_core.session_color("shell", exact))
        self.assertIsNone(inventory_core.session_color("codex", "bad-uuid"))

    def test_valid_colors_rejects_malformed_entries(self) -> None:
        exact = uuid_for(62)
        cleaned = inventory_core._valid_colors(
            {
                f"codex:{exact}": "pink",
                f"claude:{exact}": "plaid",
                "codex:not-a-uuid": "red",
                "unknown:" + exact: "blue",
                42: "green",
            }
        )
        self.assertEqual({f"codex:{exact}": "pink"}, cleaned)

    def test_color_command_set_effective_delete_and_claude_push(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            base = Path(raw)
            home = base / "home"
            project_dir = home / ".claude" / "projects" / "-srv-project"
            project_dir.mkdir(parents=True, mode=0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_file = base / "inventory.json"
            config_file.write_text(
                json.dumps({"schema_version": 1, "aliases": {}}),
                encoding="utf-8",
            )
            config_file.chmod(0o600)
            config = {
                "state_dir": state,
                "max_proc_nodes": 8192,
                "max_proc_depth": 32,
            }
            exact = uuid_for(63)
            transcript = project_dir / f"{exact}.jsonl"
            transcript.write_text(
                '{"type":"user","sessionId":"%s"}\n' % exact, encoding="utf-8"
            )
            environment = {
                "SESSION_KIT_CONFIG": os.fspath(config_file),
                "HOME": os.fspath(home),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    inventory_core._color_command(
                        argparse.Namespace(
                            color_action="set",
                            provider="claude",
                            uuid=exact,
                            color="pink",
                        ),
                        dict(config),
                    )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(
                    {"claude:" + exact: "pink"}, payload["colors"]
                )
                self.assertEqual(
                    ["claude-transcript-color"],
                    payload["provider_color_pushes"],
                )
                lines = transcript.read_text(encoding="utf-8").splitlines()
                appended = json.loads(lines[-1])
                self.assertEqual(
                    {
                        "type": "agent-color",
                        "agentColor": "pink",
                        "sessionId": exact,
                    },
                    appended,
                )
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = inventory_core._color_command(
                        argparse.Namespace(
                            color_action="effective",
                            provider="claude",
                            uuid=exact,
                        ),
                        dict(config),
                    )
                self.assertEqual(0, code)
                self.assertEqual("pink", stdout.getvalue().strip())
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    inventory_core._color_command(
                        argparse.Namespace(
                            color_action="delete", provider="claude", uuid=exact
                        ),
                        dict(config),
                    )
                payload = json.loads(stdout.getvalue())
                self.assertEqual({}, payload["colors"])
                hash_color = inventory_core.session_color("claude", exact)
                self.assertEqual(
                    hash_color,
                    json.loads(
                        transcript.read_text(encoding="utf-8").splitlines()[-1]
                    )["agentColor"],
                )

    def test_codex_color_push_is_a_clean_no_op(self) -> None:
        result = inventory_core.propagate_provider_color(
            "codex", uuid_for(64), "pink", environ={"HOME": "/nonexistent"}
        )
        self.assertEqual([], result["provider_color_pushes"])
        self.assertEqual([], result["provider_color_warnings"])

    def test_color_push_fails_open_without_transcript(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            home = Path(raw) / "home"
            (home / ".claude" / "projects").mkdir(parents=True, mode=0o700)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = inventory_core.propagate_provider_color(
                    "claude",
                    uuid_for(65),
                    "red",
                    environ={"HOME": os.fspath(home)},
                )
            self.assertEqual([], result["provider_color_pushes"])
            self.assertEqual(1, len(result["provider_color_warnings"]))
            self.assertIn("session inventory:", stderr.getvalue())

    def test_inventory_sessions_carry_display_color(self) -> None:
        exact = uuid_for(66)
        overrides = {"codex:" + exact: "cyan"}
        self.assertEqual(
            "cyan",
            inventory_core.session_color("codex", exact, overrides),
        )
        self.assertEqual(
            inventory_core.session_color("codex", exact),
            inventory_core.session_color(
                "codex", exact, {"codex:" + uuid_for(67): "red"}
            ),
        )


if __name__ == "__main__":
    unittest.main()
