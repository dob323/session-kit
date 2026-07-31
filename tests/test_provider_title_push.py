"""Provider title propagation: kit-assigned names land in provider surfaces."""

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


class ProviderTitlePushTest(unittest.TestCase):
    def _home(self, base: Path) -> Path:
        home = base / "home"
        (home / ".claude" / "sessions").mkdir(parents=True, mode=0o700)
        (home / ".codex").mkdir(mode=0o700)
        return home

    def _push(self, provider: str, uuid: str, title: str, home: Path) -> dict:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = inventory_core.propagate_provider_title(
                provider, uuid, title, environ={"HOME": os.fspath(home)}
            )
        result["stderr"] = stderr.getvalue()
        return result

    def test_claude_push_writes_nameintent_and_exact_record_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-push-", dir=REPO) as raw:
            home = self._home(Path(raw))
            sessions = home / ".claude" / "sessions"
            exact = uuid_for(11)
            other = uuid_for(12)
            matching = sessions / "123.json"
            matching.write_text(
                json.dumps({"pid": 123, "sessionId": exact, "name": "Old"}),
                encoding="utf-8",
            )
            unrelated = sessions / "456.json"
            unrelated_payload = json.dumps(
                {"pid": 456, "sessionId": other, "name": "Keep"}
            )
            unrelated.write_text(unrelated_payload, encoding="utf-8")
            result = self._push("claude", exact, "Launch Gate Test", home)
            self.assertEqual([], result["provider_title_warnings"])
            self.assertEqual(
                ["claude-nameintent", "claude-session-record"],
                result["provider_title_pushes"],
            )
            intent = sessions / f"{exact}.nameintent"
            self.assertEqual(
                "Launch Gate Test\n", intent.read_text(encoding="utf-8")
            )
            self.assertEqual(0o600, intent.stat().st_mode & 0o777)
            updated = json.loads(matching.read_text(encoding="utf-8"))
            self.assertEqual("Launch Gate Test", updated["name"])
            self.assertEqual(123, updated["pid"])
            self.assertEqual(
                unrelated_payload, unrelated.read_text(encoding="utf-8")
            )

    def test_codex_push_appends_and_preserves_existing_index(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-push-", dir=REPO) as raw:
            home = self._home(Path(raw))
            index = home / ".codex" / "session_index.jsonl"
            existing = json.dumps(
                {
                    "id": uuid_for(21),
                    "thread_name": "Existing Thread",
                    "updated_at": "2026-07-28T18:35:21Z",
                },
                separators=(",", ":"),
            )
            index.write_text(existing + "\n", encoding="utf-8")
            exact = uuid_for(22)
            result = self._push("codex", exact, "Prompt Audit Throughput", home)
            self.assertEqual([], result["provider_title_warnings"])
            self.assertEqual(
                ["codex-session-index"], result["provider_title_pushes"]
            )
            lines = index.read_text(encoding="utf-8").splitlines()
            self.assertEqual(2, len(lines))
            self.assertEqual(existing, lines[0])
            appended = json.loads(lines[1])
            self.assertEqual(
                ["id", "thread_name", "updated_at"], list(appended)
            )
            self.assertEqual(exact, appended["id"])
            self.assertEqual("Prompt Audit Throughput", appended["thread_name"])
            self.assertTrue(appended["updated_at"].endswith("Z"))
            names = inventory_core.read_codex_session_index(index)
            self.assertEqual("Prompt Audit Throughput", names[exact])

    def test_codex_push_creates_private_index_when_missing(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-push-", dir=REPO) as raw:
            home = self._home(Path(raw))
            index = home / ".codex" / "session_index.jsonl"
            self.assertFalse(index.exists())
            result = self._push("codex", uuid_for(23), "Fresh Thread Name", home)
            self.assertEqual(
                ["codex-session-index"], result["provider_title_pushes"]
            )
            self.assertEqual(0o600, index.stat().st_mode & 0o777)

    def test_push_fails_open_on_missing_surfaces_and_bad_input(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-push-", dir=REPO) as raw:
            base = Path(raw)
            bare = base / "bare-home"
            bare.mkdir(mode=0o700)
            for provider in ("claude", "codex"):
                with self.subTest(provider=provider):
                    result = self._push(
                        provider, uuid_for(31), "Valid Title Here", bare
                    )
                    self.assertEqual([], result["provider_title_pushes"])
                    self.assertEqual(1, len(result["provider_title_warnings"]))
                    self.assertIn("session inventory:", result["stderr"])
            result = self._push("claude", "not-a-uuid", "Valid Title", bare)
            self.assertEqual([], result["provider_title_pushes"])
            self.assertEqual(
                ["invalid provider title push request"],
                result["provider_title_warnings"],
            )
            oversized = self._home(base)
            index = oversized / ".codex" / "session_index.jsonl"
            with index.open("w", encoding="utf-8") as handle:
                handle.write("x" * (inventory_core.MAX_CODEX_SESSION_INDEX_BYTES + 1))
            result = self._push("codex", uuid_for(32), "Bounded Title", oversized)
            self.assertEqual([], result["provider_title_pushes"])
            self.assertIn(
                "bounded size", result["provider_title_warnings"][0]
            )

    def test_alias_set_propagates_and_delete_leaves_provider_alone(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-push-", dir=REPO) as raw:
            base = Path(raw)
            home = self._home(base)
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
            exact = uuid_for(41)
            index = home / ".codex" / "session_index.jsonl"
            environment = {
                "SESSION_KIT_CONFIG": os.fspath(config_file),
                "HOME": os.fspath(home),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    inventory_core._alias_command(
                        argparse.Namespace(
                            alias_action="set",
                            provider="codex",
                            uuid=exact,
                            title="Manual Alias Name",
                        ),
                        dict(config),
                    )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(
                    ["codex-session-index"], payload["provider_title_pushes"]
                )
                self.assertEqual(
                    "Manual Alias Name",
                    inventory_core.read_codex_session_index(index)[exact],
                )
                appended = index.read_text(encoding="utf-8")
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    inventory_core._alias_command(
                        argparse.Namespace(
                            alias_action="delete", provider="codex", uuid=exact
                        ),
                        dict(config),
                    )
                payload = json.loads(stdout.getvalue())
                self.assertNotIn("provider_title_pushes", payload)
                self.assertNotIn(f"codex:{exact}", payload["aliases"])
                self.assertEqual(
                    appended, index.read_text(encoding="utf-8")
                )

    def test_accepted_self_name_pushes_with_caller_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-push-", dir=REPO) as raw:
            base = Path(raw)
            home = self._home(base)
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
            exact = uuid_for(51)
            evidence = {
                "provider": "codex",
                "uuid": exact,
                "shpool_id": "main",
                "provider_pid": 2001,
                "provider_start_ticks": 7,
                "shell_pid": 1001,
                "shell_start_ticks": 5,
            }
            environment = {
                "SESSION_KIT_CONFIG": os.fspath(config_file),
                "HOME": os.fspath(home),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                with mock.patch.object(
                    inventory_core,
                    "prove_self_name_caller",
                    return_value=dict(evidence),
                ):
                    result = inventory_core.self_name_automatic_title(
                        config,
                        "Session Kit Updates",
                        inventory={"source": "live", "stale": False},
                        process_table={},
                        environ={"HOME": os.fspath(home)},
                        current_pid=3001,
                    )
            self.assertEqual("ready", result["automatic_name_state"])
            self.assertEqual(
                ["codex-session-index"], result["provider_title_pushes"]
            )
            index = home / ".codex" / "session_index.jsonl"
            self.assertEqual(
                "Session Kit Updates",
                inventory_core.read_codex_session_index(index)[exact],
            )


class ClaudeAiTitleTests(unittest.TestCase):
    def test_reader_returns_last_bounded_ai_title_for_exact_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".ai-title-", dir=REPO) as raw:
            home = Path(raw)
            project = home / ".claude" / "projects" / "-srv-project"
            project.mkdir(parents=True, mode=0o700)
            exact = uuid_for(71)
            other = uuid_for(72)
            transcript = project / f"{exact}.jsonl"
            lines = [
                json.dumps(
                    {
                        "type": "ai-title",
                        "aiTitle": "First Title",
                        "sessionId": exact,
                    }
                ),
                json.dumps(
                    {
                        "type": "ai-title",
                        "aiTitle": "Wrong Session",
                        "sessionId": other,
                    }
                ),
                json.dumps({"type": "user", "sessionId": exact}),
                json.dumps(
                    {
                        "type": "ai-title",
                        "aiTitle": "Find leap years this year",
                        "sessionId": exact,
                    }
                ),
            ]
            transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertEqual(
                "Find leap years this year",
                inventory_core.read_claude_ai_title(exact, home),
            )
            self.assertEqual(
                "", inventory_core.read_claude_ai_title(uuid_for(73), home)
            )
            self.assertEqual(
                "", inventory_core.read_claude_ai_title("not-a-uuid", home)
            )

    def test_parser_passes_title_evidence_and_derived_names_yield(self) -> None:
        exact = uuid_for(74)
        parsed = inventory_core._parse_claude_payload(
            [
                {
                    "pid": 4001,
                    "sessionId": exact,
                    "cwd": "/srv/project",
                    "kind": "interactive",
                    "name": "v2-b3",
                    "nameSource": "derived",
                    "aiTitle": "Find leap years this year",
                    "status": "idle",
                }
            ]
        )
        self.assertEqual("v2-b3", parsed[0]["title"])
        self.assertEqual(
            "Find leap years this year", parsed[0]["ai_title"]
        )
        self.assertEqual("derived", parsed[0]["name_source"])


if __name__ == "__main__":
    unittest.main()
