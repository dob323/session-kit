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
import threading
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

    def test_launch_color_is_deterministic_and_valid(self) -> None:
        first = inventory_core.launch_color_for("s20260731-172651-3345413")
        second = inventory_core.launch_color_for("s20260731-172651-3345413")
        self.assertEqual(first, second)
        self.assertIn(first, inventory_core.SESSION_COLORS)
        self.assertIsNone(inventory_core.record_launch_color({}, "../evil"))
        self.assertIsNone(inventory_core.record_launch_color({}, ""))

    def test_launch_reservations_serialize_and_fall_back_after_eight(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".color-race-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            config = {"state_dir": state}
            barrier = threading.Barrier(2)
            colors: list[str | None] = []

            def reserve(name: str) -> None:
                barrier.wait()
                colors.append(inventory_core.record_launch_color(config, name))

            threads = [
                threading.Thread(target=reserve, args=("new-one",)),
                threading.Thread(target=reserve, args=("new-two",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(2, len(set(colors)))
            first = inventory_core.record_launch_color(config, "repeat", {"red"})
            repeated = inventory_core.record_launch_color(
                config, "repeat", inventory_core.SESSION_COLORS
            )
            self.assertEqual(first, repeated)
            preferred = inventory_core.launch_color_for("full-palette")
            self.assertEqual(
                preferred,
                inventory_core.launch_color_for(
                    "full-palette", inventory_core.SESSION_COLORS
                ),
            )

    def test_conversation_pick_persists_the_prebaked_claude_override(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".conversation-color-", dir=REPO) as raw:
            base = Path(raw)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_file = base / "inventory.json"
            config_file.write_text('{"schema_version":1,"aliases":{}}\n')
            config_file.chmod(0o600)
            config = {"state_dir": state}
            exact = uuid_for(72)
            live = {"source": "live", "stale": False, "sessions": [], "outside_agents": []}
            with (
                mock.patch.dict(os.environ, {"SESSION_KIT_CONFIG": str(config_file)}),
                mock.patch.object(inventory_core, "snapshot", return_value=live),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                code = inventory_core._color_command(
                    argparse.Namespace(
                        color_action="conversation-pick",
                        provider="claude",
                        uuid=exact,
                    ),
                    config,
                )
            payload = json.loads(output.getvalue())
            document = json.loads(config_file.read_text())
            self.assertEqual(0, code)
            self.assertEqual(payload["color"], document["colors"][f"claude:{exact}"])

            with (
                mock.patch.dict(os.environ, {"SESSION_KIT_CONFIG": str(config_file)}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                mismatch = inventory_core._color_command(
                    argparse.Namespace(
                        color_action="conversation-release",
                        provider="claude",
                        uuid=exact,
                        color=next(
                            item
                            for item in inventory_core.SESSION_COLORS
                            if item != payload["color"]
                        ),
                    ),
                    config,
                )
            self.assertEqual(1, mismatch)
            self.assertEqual(
                payload["color"],
                json.loads(config_file.read_text())["colors"][f"claude:{exact}"],
            )
            with (
                mock.patch.dict(os.environ, {"SESSION_KIT_CONFIG": str(config_file)}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                released = inventory_core._color_command(
                    argparse.Namespace(
                        color_action="conversation-release",
                        provider="claude",
                        uuid=exact,
                        color=payload["color"],
                    ),
                    config,
                )
            self.assertEqual(0, released)
            self.assertNotIn(
                f"claude:{exact}",
                json.loads(config_file.read_text()).get("colors", {}),
            )

    def test_launch_color_marker_is_adopted_into_override(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".launch-", dir=REPO) as raw:
            base = Path(raw)
            home = base / "home"
            home.mkdir()
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
            shpool_id = "s20260731-170000-1234567"
            exact = uuid_for(71)
            environment = {
                "SESSION_KIT_CONFIG": os.fspath(config_file),
                "HOME": os.fspath(home),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                color = inventory_core.record_launch_color(config, shpool_id)
                self.assertIn(color, inventory_core.SESSION_COLORS)
                marker = state / "launch-color" / shpool_id
                self.assertEqual(color, marker.read_text().strip())
                sessions = [
                    {
                        "provider": "codex",
                        "shpool_id": shpool_id,
                        "identity": {"uuid": exact},
                    }
                ]
                adopted = inventory_core._adopt_launch_colors(
                    config, sessions, {}
                )
                self.assertEqual(color, adopted.get(f"codex:{exact}"))
                self.assertFalse(marker.exists())
                # A second run with the override in place changes nothing.
                again = inventory_core._adopt_launch_colors(
                    config, sessions, adopted
                )
                self.assertEqual(adopted, again)
                # An existing explicit override outranks a fresh marker.
                inventory_core.record_launch_color(config, shpool_id)
                kept = inventory_core._adopt_launch_colors(
                    config,
                    sessions,
                    {f"codex:{exact}": "purple"},
                )
                self.assertEqual("purple", kept[f"codex:{exact}"])
                self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
