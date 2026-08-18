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
import time
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
            project = home / ".claude" / "projects" / "-srv-project"
            project.mkdir(parents=True, mode=0o700)
            exact = uuid_for(11)
            other = uuid_for(12)
            transcript = project / f"{exact}.jsonl"
            transcript.write_text(
                json.dumps({"type": "user", "sessionId": exact}) + "\n",
                encoding="utf-8",
            )
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
                [
                    "claude-nameintent",
                    "claude-transcript-name",
                    "claude-session-record",
                ],
                result["provider_title_pushes"],
            )
            appended = json.loads(
                transcript.read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(
                {
                    "type": "agent-name",
                    "agentName": "Launch Gate Test",
                    "sessionId": exact,
                },
                appended,
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
            config = json.loads(
                (home / ".config/session-kit/inventory.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn(
                f"claude:{exact}", config.get("pending_native_titles", {})
            )

    def test_claude_push_updates_account_record_and_clears_derived_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-push-", dir=REPO) as raw:
            home = self._home(Path(raw))
            exact = uuid_for(13)
            account = (
                home
                / ".local/share/session-kit/accounts/claude/duck/sessions"
            )
            account.mkdir(parents=True, mode=0o700)
            record = account / "789.json"
            record.write_text(
                json.dumps(
                    {
                        "pid": 789,
                        "sessionId": exact,
                        "name": "v2-5e",
                        "nameSource": "derived",
                        "nameSince": 100,
                    }
                ),
                encoding="utf-8",
            )

            result = self._push("claude", exact, "Session Kit Closeout", home)

            self.assertEqual([], result["provider_title_warnings"])
            self.assertIn("claude-session-record", result["provider_title_pushes"])
            updated = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual("Session Kit Closeout", updated["name"])
            self.assertNotIn("nameSource", updated)
            self.assertEqual(
                "Session Kit Closeout\n",
                (account / f"{exact}.nameintent").read_text(encoding="utf-8"),
            )
            config = json.loads(
                (home / ".config/session-kit/inventory.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                {
                    "title": "v2-5e",
                    "nameSince": 100,
                    "nameSource": "derived",
                },
                config["pending_native_titles"][f"claude:{exact}"],
            )

    def test_claude_record_without_name_since_gets_no_pending_tier(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-push-", dir=REPO) as raw:
            home = self._home(Path(raw))
            exact = uuid_for(16)
            record = home / ".claude" / "sessions" / "791.json"
            record.write_text(
                json.dumps(
                    {"pid": 791, "sessionId": exact, "name": "Legacy Name"}
                ),
                encoding="utf-8",
            )

            result = self._push("claude", exact, "New Kit Name", home)

            self.assertEqual([], result["provider_title_warnings"])
            config = json.loads(
                (home / ".config/session-kit/inventory.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("pending_native_titles", config)

    def test_claude_push_regression_guard_refuses_symlinked_intent_and_record(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-push-", dir=REPO) as raw:
            home = self._home(Path(raw))
            sessions = home / ".claude" / "sessions"
            exact = uuid_for(14)
            external_intent = Path(raw) / "external-intent"
            external_intent.write_text("keep\n", encoding="utf-8")
            (sessions / f"{exact}.nameintent").symlink_to(external_intent)
            external_record = Path(raw) / "external-record.json"
            external_payload = json.dumps(
                {"pid": 790, "sessionId": exact, "name": "Keep"}
            )
            external_record.write_text(external_payload, encoding="utf-8")
            (sessions / "790.json").symlink_to(external_record)

            result = self._push("claude", exact, "Must Not Land", home)

            self.assertIn("refusing symlinked name intent", result["stderr"])
            self.assertNotIn("claude-session-record", result["provider_title_pushes"])
            self.assertEqual("keep\n", external_intent.read_text(encoding="utf-8"))
            self.assertEqual(
                external_payload, external_record.read_text(encoding="utf-8")
            )

    def test_claude_push_refuses_symlinked_account_but_pushes_healthy_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-push-", dir=REPO) as raw:
            base = Path(raw)
            home = self._home(base)
            exact = uuid_for(15)
            ambient = home / ".claude" / "sessions"
            healthy = (
                home
                / ".local/share/session-kit/accounts/claude/healthy/sessions"
            )
            healthy.mkdir(parents=True, mode=0o700)
            external = base / "external-profile" / "sessions"
            external.mkdir(parents=True, mode=0o700)
            linked = home / ".local/share/session-kit/accounts/claude/linked"
            linked.parent.mkdir(parents=True, exist_ok=True)
            linked.symlink_to(external.parent, target_is_directory=True)

            records = []
            for sessions, pid, title in (
                (ambient, 801, "Ambient Old"),
                (healthy, 802, "Healthy Old"),
                (external, 803, "External Keep"),
            ):
                record = sessions / f"{pid}.json"
                record.write_text(
                    json.dumps(
                        {
                            "pid": pid,
                            "sessionId": exact,
                            "name": title,
                            "nameSince": 100,
                        }
                    ),
                    encoding="utf-8",
                )
                records.append(record)

            result = self._push("claude", exact, "Bounded Push", home)

            self.assertIn(str(linked / "sessions"), result["stderr"])
            self.assertIn("account profile is a symlink", result["stderr"])
            self.assertEqual(
                "Bounded Push", json.loads(records[0].read_text())["name"]
            )
            self.assertEqual(
                "Bounded Push", json.loads(records[1].read_text())["name"]
            )
            self.assertEqual(
                "External Keep", json.loads(records[2].read_text())["name"]
            )
            self.assertEqual(
                "Bounded Push\n", (ambient / f"{exact}.nameintent").read_text()
            )
            self.assertEqual(
                "Bounded Push\n", (healthy / f"{exact}.nameintent").read_text()
            )
            self.assertFalse((external / f"{exact}.nameintent").exists())

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
                "CODEX_HOME": os.fspath(home / ".codex"),
                "SESSION_KIT_CODEX_HOME": os.fspath(home / ".codex"),
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

    def test_a_title_in_an_account_profile_is_found(self) -> None:
        """The read side has to search the profile the session ran on.

        A session launched on an enrolled account runs with CLAUDE_CONFIG_DIR
        set, so its transcript is written under that profile and never under
        the default root. Reading only the default root returned "no title",
        every caller read that as "the provider never set one", and the name
        the kit could already see was never pushed anywhere the window shows.
        """
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            profile = home / ".local/share/session-kit/accounts/claude/primary"
            project = profile / "projects" / "-srv-app"
            project.mkdir(parents=True, mode=0o700)
            # The default root exists and holds nothing: the live shape.
            (home / ".claude" / "projects").mkdir(parents=True, mode=0o700)
            exact = uuid_for(91)
            (project / f"{exact}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "ai-title",
                        "aiTitle": "Count the great lakes",
                        "sessionId": exact,
                    }
                )
                + "\n"
                + json.dumps(
                    {"type": "agent-color", "agentColor": "blue", "sessionId": exact}
                )
                + "\n",
                encoding="utf-8",
            )

            signals = inventory_core.read_claude_transcript_signals(exact, home)

            self.assertEqual("Count the great lakes", signals["ai_title"])
            self.assertEqual("blue", signals["agent_color"])

    def test_transcript_signals_return_last_color_and_reject_junk(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            project = home / ".claude" / "projects" / "-srv-app"
            project.mkdir(parents=True, mode=0o700)
            exact = uuid_for(76)
            transcript = project / f"{exact}.jsonl"
            lines = [
                json.dumps(
                    {
                        "type": "agent-name",
                        "agentName": "Leap year audit",
                        "sessionId": exact,
                    }
                ),
                json.dumps(
                    {"type": "agent-color", "agentColor": "pink", "sessionId": exact}
                ),
                json.dumps(
                    {
                        "type": "agent-color",
                        "agentColor": "not-a-color",
                        "sessionId": exact,
                    }
                ),
                json.dumps(
                    {
                        "type": "ai-title",
                        "aiTitle": "Count leap years",
                        "sessionId": exact,
                    }
                ),
                json.dumps(
                    {"type": "agent-color", "agentColor": "green", "sessionId": exact}
                ),
            ]
            transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
            signals = inventory_core.read_claude_transcript_signals(exact, home)
            # Last valid record of each kind wins; junk colors never surface.
            self.assertEqual(
                {
                    "ai_title": "Count leap years",
                    "agent_name": "Leap year audit",
                    "agent_color": "green",
                    "pending_ask_user_question": False,
                    "pending_tool_use": False,
                    "pending_tool_use_at_unix_ms": None,
                },
                signals,
            )

    def test_pending_hydration_fills_only_absent_native_records(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            exact = uuid_for(77)
            project = home / ".claude" / "projects" / "-srv-app"
            sessions = home / ".claude" / "sessions"
            project.mkdir(parents=True, mode=0o700)
            sessions.mkdir(parents=True, mode=0o700)
            transcript = project / f"{exact}.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "ai-title",
                        "aiTitle": "Visible Claude metadata",
                        "sessionId": exact,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            live = {
                "source": "live",
                "stale": False,
                "sessions": [
                    {
                        "provider": "claude",
                        "identity": {"uuid": exact},
                        "native_title": "Visible Claude metadata",
                        "provider_title_state": "pending",
                    }
                ],
            }
            with (
                mock.patch.object(inventory_core, "snapshot", return_value=live),
                mock.patch.object(
                    inventory_core, "guard_live_inventory", return_value=True
                ),
                mock.patch.object(inventory_core, "canonical_colors", return_value={}),
            ):
                result = inventory_core.claude_pending_native_hydrations(
                    {"state_dir": str(home / "state")},
                    environ={"HOME": os.fspath(home)},
                )
            self.assertEqual(exact, result[0]["uuid"])
            signals = inventory_core.read_claude_transcript_signals(exact, home)
            self.assertEqual("Visible Claude metadata", signals["agent_name"])
            self.assertIn(signals["agent_color"], inventory_core.SESSION_COLORS)

            # A second pass is self-terminating and preserves both records.
            with (
                mock.patch.object(inventory_core, "snapshot", return_value=live),
                mock.patch.object(
                    inventory_core, "guard_live_inventory", return_value=True
                ),
                mock.patch.object(inventory_core, "canonical_colors", return_value={}),
            ):
                repeated = inventory_core.claude_pending_native_hydrations(
                    {"state_dir": str(home / "state")},
                    environ={"HOME": os.fspath(home)},
                )
            self.assertEqual([], repeated)

    def test_pending_hydration_never_replaces_native_explicit_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            exact = uuid_for(78)
            project = home / ".claude" / "projects" / "-srv-app"
            (home / ".claude" / "sessions").mkdir(parents=True, mode=0o700)
            project.mkdir(parents=True, mode=0o700)
            transcript = project / f"{exact}.jsonl"
            transcript.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "ai-title",
                                "aiTitle": "Generated title",
                                "sessionId": exact,
                            }
                        ),
                        json.dumps(
                            {
                                "type": "agent-color",
                                "agentColor": "pink",
                                "sessionId": exact,
                            }
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            live = {
                "source": "live",
                "stale": False,
                "sessions": [
                    {
                        "provider": "claude",
                        "identity": {"uuid": exact},
                        "native_title": "The operator's explicit name",
                        "provider_title_state": "ready",
                    }
                ],
            }
            with (
                mock.patch.object(inventory_core, "snapshot", return_value=live),
                mock.patch.object(
                    inventory_core, "guard_live_inventory", return_value=True
                ),
                mock.patch.object(inventory_core, "canonical_colors", return_value={}),
            ):
                result = inventory_core.claude_pending_native_hydrations(
                    {"state_dir": str(home / "state")},
                    environ={"HOME": os.fspath(home)},
                )
            self.assertEqual([], result)
            self.assertEqual(
                "",
                inventory_core.read_claude_transcript_signals(exact, home)[
                    "agent_name"
                ],
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
                    "agentName": "Find leap years this year",
                    "status": "idle",
                }
            ]
        )
        self.assertEqual("v2-b3", parsed[0]["title"])
        self.assertEqual(
            "Find leap years this year", parsed[0]["ai_title"]
        )
        self.assertEqual("derived", parsed[0]["name_source"])
        self.assertEqual(
            "Find leap years this year", parsed[0]["agent_name"]
        )


class DerivePromptTitleTests(unittest.TestCase):
    def test_trims_trailing_function_words(self) -> None:
        self.assertEqual(
            "Leap years are there in 2026",
            inventory_core.derive_prompt_title(
                "leap years are there in 2026 and what are the dates?"
            ),
        )

    def test_takes_first_sentence_only(self) -> None:
        self.assertEqual(
            "Fix the nginx 502 errors",
            inventory_core.derive_prompt_title(
                "fix the nginx 502 errors. they started last night"
            ),
        )

    def test_rejects_slash_commands_and_short_prompts(self) -> None:
        self.assertIsNone(inventory_core.derive_prompt_title("/color 3"))
        self.assertIsNone(inventory_core.derive_prompt_title("hi"))
        self.assertIsNone(inventory_core.derive_prompt_title(""))
        self.assertIsNone(inventory_core.derive_prompt_title(None))

    def test_rejects_session_kit_machine_transport_prompts(self) -> None:
        for prompt in (
            '<cross-session-message from="uds:/run/user/1000/example.sock">work</cross-session-message>',
            "[session-kit operator message abc123] work",
            "Session Kit initialized this managed worker. Wait for assignment.",
            "You are a Session Kit delivery runner. Deliver one message.",
            "RUNTIME FOR THIS WAKE: You are a fixed Session Kit delivery bot.",
        ):
            self.assertIsNone(inventory_core.derive_prompt_title(prompt))

    def test_caps_length_at_64(self) -> None:
        title = inventory_core.derive_prompt_title(
            "investigate " + "extraordinarily " * 8 + "long prompt"
        )
        assert title is not None
        self.assertLessEqual(len(title), 64)


class AutoTitleFromHookTests(unittest.TestCase):
    def test_codex_title_push_uses_one_override_precedence_for_all_writes(self) -> None:
        import sqlite3

        uuid = "00000000-0000-4000-8000-000000000033"
        with tempfile.TemporaryDirectory() as base:
            home = Path(base) / "home"
            provider_home = Path(base) / "provider"
            kit_home = Path(base) / "kit"
            home.mkdir()
            for root in (provider_home, kit_home):
                root.mkdir()
                connection = sqlite3.connect(root / "state_5.sqlite")
                connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
                connection.execute("INSERT INTO threads VALUES (?, NULL)", (uuid,))
                connection.commit()
                connection.close()
            result = inventory_core.propagate_provider_title(
                "codex",
                uuid,
                "Override Title",
                environ={
                    "HOME": str(home),
                    "CODEX_HOME": str(provider_home),
                    "SESSION_KIT_CODEX_HOME": str(kit_home),
                },
            )
            self.assertIn("codex-thread-title", result["provider_title_pushes"])
            for root, expected in ((kit_home, "Override Title"), (provider_home, None)):
                connection = sqlite3.connect(root / "state_5.sqlite")
                actual = connection.execute(
                    "SELECT title FROM threads WHERE id = ?", (uuid,)
                ).fetchone()[0]
                connection.close()
                self.assertEqual(expected, actual)

    def test_codex_title_push_live_renames_app_server_threads(self) -> None:
        import base64
        import hashlib
        import socket as socketmod
        import sqlite3
        import struct
        import threading

        uuid = "00000000-0000-4000-8000-000000000044"
        with tempfile.TemporaryDirectory() as base:
            home = Path(base) / "home"
            codex = Path(base) / "codex"
            home.mkdir()
            codex.mkdir()
            connection = sqlite3.connect(codex / "state_5.sqlite")
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)"
            )
            connection.execute("INSERT INTO threads VALUES (?, NULL)", (uuid,))
            connection.commit()
            connection.close()
            app_dir = (
                home / ".local" / "state" / "session-kit" / "app-server" / "s1"
            )
            app_dir.mkdir(parents=True)
            socket_path = app_dir / "app.sock"
            server = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
            server.bind(os.fspath(socket_path))
            server.listen(1)
            server.settimeout(5)
            seen: list[dict] = []

            def recv_client_frame(link: socketmod.socket) -> dict:
                def read_exact(count: int) -> bytes:
                    data = b""
                    while len(data) < count:
                        chunk = link.recv(count - len(data))
                        if not chunk:
                            raise OSError("client closed")
                        data += chunk
                    return data

                while True:
                    first, second = read_exact(2)
                    length = second & 0x7F
                    if length == 126:
                        length = struct.unpack("!H", read_exact(2))[0]
                    mask = read_exact(4) if second & 0x80 else b""
                    payload = read_exact(length)
                    if mask:
                        payload = bytes(
                            value ^ mask[index % 4]
                            for index, value in enumerate(payload)
                        )
                    if first & 0x0F != 1:
                        continue
                    return json.loads(payload)

            def serve() -> None:
                link, _ = server.accept()
                link.settimeout(5)
                request = b""
                while not request.endswith(b"\r\n\r\n"):
                    request += link.recv(1)
                key = next(
                    line.split(b":", 1)[1].strip()
                    for line in request.split(b"\r\n")
                    if line.lower().startswith(b"sec-websocket-key:")
                )
                accept = base64.b64encode(
                    hashlib.sha1(
                        key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                    ).digest()
                )
                link.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
                )
                while True:
                    message = recv_client_frame(link)
                    seen.append(message)
                    if message.get("id") is not None:
                        reply = json.dumps(
                            {"id": message["id"], "result": {}},
                            separators=(",", ":"),
                        ).encode()
                        link.sendall(
                            bytes([0x81, len(reply)]) + reply
                        )
                    if message.get("method") == "thread/name/set":
                        break
                link.close()

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                result = inventory_core.propagate_provider_title(
                    "codex",
                    uuid,
                    "Live Rename",
                    environ={
                        "HOME": str(home),
                        "SESSION_KIT_CODEX_HOME": str(codex),
                    },
                )
            finally:
                worker.join(timeout=5)
                server.close()
            self.assertIn(
                "codex-live-rename", result["provider_title_pushes"]
            )
            renamed = next(
                message
                for message in seen
                if message.get("method") == "thread/name/set"
            )
            self.assertEqual(
                {"threadId": uuid, "name": "Live Rename"},
                renamed.get("params"),
            )

    def test_codex_live_rename_kill_switch_and_absent_sockets_fail_open(self) -> None:
        import sqlite3

        uuid = "00000000-0000-4000-8000-000000000045"
        with tempfile.TemporaryDirectory() as base:
            home = Path(base) / "home"
            codex = Path(base) / "codex"
            home.mkdir()
            codex.mkdir()
            connection = sqlite3.connect(codex / "state_5.sqlite")
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)"
            )
            connection.execute("INSERT INTO threads VALUES (?, NULL)", (uuid,))
            connection.commit()
            connection.close()
            for extra in (
                {},
                {"SESSION_KIT_CODEX_LIVE_RENAME": "0"},
            ):
                result = inventory_core.propagate_provider_title(
                    "codex",
                    uuid,
                    "Quiet Title",
                    environ={
                        "HOME": str(home),
                        "SESSION_KIT_CODEX_HOME": str(codex),
                        **extra,
                    },
                )
                self.assertIn(
                    "codex-thread-title", result["provider_title_pushes"]
                )
                self.assertNotIn(
                    "codex-live-rename", result["provider_title_pushes"]
                )
                self.assertEqual([], result["provider_title_warnings"])

    def test_app_server_logs_are_private_precreated_and_symlink_refused(self) -> None:
        shell = (REPO / "bashrc" / "shpool.bashrc").read_text(encoding="utf-8")
        app_definition = shell.index('__sk_app_log="$__sk_app_dir/app-server.log"')
        broker_definition = shell.index('__sk_broker_log="$__sk_app_dir/broker.log"')
        first_redirect = shell.index('>>"$__sk_app_log"')
        self.assertLess(app_definition, first_redirect)
        self.assertLess(broker_definition, first_redirect)
        self.assertIn("O_NOFOLLOW", shell[app_definition:first_redirect])
        self.assertIn("os.fchmod(descriptor, 0o600)", shell[app_definition:first_redirect])
        self.assertIn("[[ -L $__sk_app_log || -L $__sk_broker_log ]]", shell)

    def test_app_server_directory_chain_is_private_real_and_owner_controlled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            state = base / "state"
            state.mkdir(mode=0o700)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = inventory_core._platform_command(
                    argparse.Namespace(
                        platform_action="app-server-dir",
                        state_root=str(state),
                        session_id="main",
                    )
                )
            self.assertEqual(0, code)
            created = Path(output.getvalue().strip())
            self.assertEqual(state / "session-kit" / "app-server" / "main", created)
            for path in (state / "session-kit", state / "session-kit" / "app-server", created):
                self.assertFalse(path.is_symlink())
                self.assertEqual(0o700, path.stat().st_mode & 0o777)

            unsafe = base / "unsafe-state"
            unsafe.mkdir(mode=0o700)
            external = base / "external"
            external.mkdir(mode=0o700)
            (unsafe / "session-kit").symlink_to(external, target_is_directory=True)
            with self.assertRaises(inventory_core.CollectionError):
                inventory_core._platform_command(
                    argparse.Namespace(
                        platform_action="app-server-dir",
                        state_root=str(unsafe),
                        session_id="main",
                    )
                )
            weak = base / "weak-state"
            weak.mkdir(mode=0o755)
            weak.chmod(0o755)
            with self.assertRaises(inventory_core.CollectionError):
                inventory_core._platform_command(
                    argparse.Namespace(
                        platform_action="app-server-dir",
                        state_root=str(weak),
                        session_id="main",
                    )
                )

    def test_pending_self_name_yields_to_native_claude_rename(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000034"
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            state = base / "state"
            state.mkdir(mode=0o700)
            home = base / "home"
            transcript_root = home / ".claude" / "projects" / "-srv-project"
            transcript_root.mkdir(parents=True)
            transcript = transcript_root / f"{uuid}.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "agent-name",
                        "agentName": "Manual Native Name",
                        "sessionId": uuid,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            retry = state / "provider-title-retries.json"
            retry.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "entries": {
                            f"claude:{uuid}": {
                                "provider": "claude",
                                "uuid": uuid,
                                "title": "Old Kit Name",
                                "attempts": 0,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            retry.chmod(0o600)
            before = transcript.read_bytes()
            with mock.patch.object(
                inventory_core,
                "snapshot",
                side_effect=inventory_core.CollectionError("no live inventory"),
            ):
                result = inventory_core.claude_pending_native_hydrations(
                    {"state_dir": state}, {"HOME": str(home)}
                )
            self.assertEqual("superseded", result[0]["status"])
            self.assertEqual(before, transcript.read_bytes())
            self.assertEqual({}, json.loads(retry.read_text())["entries"])
    UUID = "00000000-0000-4000-8000-000000000077"

    def _prebaked_home(self, base: Path) -> tuple[Path, Path]:
        home = base / "home"
        (home / ".claude" / "sessions").mkdir(parents=True, mode=0o700)
        project = home / ".claude" / "projects" / "-srv-project"
        project.mkdir(parents=True, mode=0o700)
        transcript = project / f"{self.UUID}.jsonl"
        records = [
            {"type": "agent-color", "agentColor": "green", "sessionId": self.UUID},
            {
                "type": "user",
                "isMeta": True,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Continue from where you left off.",
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "No response requested."}
                    ],
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return home, transcript

    def _payload(self, transcript: Path, prompt: str) -> str:
        return json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": self.UUID,
                "transcript_path": str(transcript),
                "prompt": prompt,
            }
        )

    def test_titles_prebaked_conversation_once(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            home, transcript = self._prebaked_home(Path(base))
            env = {"HOME": str(home)}
            result = inventory_core.auto_title_from_hook(
                self._payload(
                    transcript,
                    "leap years are there in 2026 and what are the dates?",
                ),
                environ=env,
            )
            self.assertEqual("Leap years are there in 2026", result["title"])
            self.assertIn("claude-transcript-ai-title", result["pushes"])
            self.assertIn("claude-transcript-name", result["pushes"])
            self.assertIn("claude-nameintent", result["pushes"])
            records = [
                json.loads(line)
                for line in transcript.read_text().splitlines()
            ]
            self.assertIn("ai-title", [r.get("type") for r in records])
            self.assertIn("agent-name", [r.get("type") for r in records])
            again = inventory_core.auto_title_from_hook(
                self._payload(transcript, "another prompt entirely here"),
                environ=env,
            )
            self.assertIsNone(again["title"])

    def test_refuses_without_prebake_signature(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            home, transcript = self._prebaked_home(Path(base))
            lines = [
                line
                for line in transcript.read_text().splitlines()
                if '"isMeta"' not in line
            ]
            transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = inventory_core.auto_title_from_hook(
                self._payload(transcript, "name this session please now"),
                environ={"HOME": str(home)},
            )
            self.assertIsNone(result["title"])
            self.assertEqual("no pre-bake resume signature", result["reason"])

    def test_refuses_existing_titles_and_busy_conversations(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            home, transcript = self._prebaked_home(Path(base))
            env = {"HOME": str(home)}
            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "ai-title",
                            "aiTitle": "Existing title",
                            "sessionId": self.UUID,
                        }
                    )
                    + "\n"
                )
            result = inventory_core.auto_title_from_hook(
                self._payload(transcript, "name this session please now"),
                environ=env,
            )
            self.assertIsNone(result["title"])
            home2 = Path(base) / "second"
            home2.mkdir()
            home3, transcript3 = self._prebaked_home(home2)
            with open(transcript3, "a", encoding="utf-8") as handle:
                for text in ("first question", "second question"):
                    handle.write(
                        json.dumps(
                            {
                                "type": "user",
                                "message": {"role": "user", "content": text},
                            }
                        )
                        + "\n"
                    )
            busy = inventory_core.auto_title_from_hook(
                self._payload(transcript3, "third question arrives here"),
                environ={"HOME": str(home3)},
            )
            self.assertIsNone(busy["title"])
            self.assertEqual(
                "conversation already has prior prompts", busy["reason"]
            )

    def test_tool_results_do_not_count_as_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            home, transcript = self._prebaked_home(Path(base))
            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "toolu_x",
                                        "content": "ok",
                                    }
                                ],
                            },
                        }
                    )
                    + "\n"
                )
                handle.write(
                    json.dumps(
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": "leap years are there in 2026 and what?",
                            },
                        }
                    )
                    + "\n"
                )
            result = inventory_core.auto_title_from_hook(
                self._payload(
                    transcript,
                    "leap years are there in 2026 and what are the dates?",
                ),
                environ={"HOME": str(home)},
            )
            self.assertEqual("Leap years are there in 2026", result["title"])

    def test_refuses_foreign_transcript_paths(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            home, transcript = self._prebaked_home(Path(base))
            foreign = Path(base) / "elsewhere.jsonl"
            foreign.write_text(transcript.read_text(), encoding="utf-8")
            result = inventory_core.auto_title_from_hook(
                self._payload(foreign, "name this session please now"),
                environ={"HOME": str(home)},
            )
            self.assertIsNone(result["title"])

    def test_codex_push_sets_thread_title_in_state_database(self) -> None:
        import sqlite3

        uuid = "00000000-0000-4000-8000-000000000031"
        with tempfile.TemporaryDirectory() as base:
            home = Path(base)
            codex = home / ".codex"
            codex.mkdir(parents=True)
            database = codex / "state_5.sqlite"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)"
            )
            connection.execute("INSERT INTO threads VALUES (?, NULL)", (uuid,))
            connection.commit()
            connection.close()
            result = inventory_core.propagate_provider_title(
                "codex", uuid, "Named thread", environ={"HOME": str(home)}
            )
            self.assertIn("codex-thread-title", result["provider_title_pushes"])
            connection = sqlite3.connect(database)
            self.assertEqual(
                ("Named thread",),
                connection.execute(
                    "SELECT title FROM threads WHERE id = ?", (uuid,)
                ).fetchone(),
            )
            connection.close()
            missing = inventory_core.propagate_provider_title(
                "codex",
                "00000000-0000-4000-8000-000000000032",
                "Other thread",
                environ={"HOME": str(home)},
            )
            self.assertNotIn(
                "codex-thread-title", missing["provider_title_pushes"]
            )
            self.assertTrue(
                any(
                    "thread row not found" in warning
                    for warning in missing["provider_title_warnings"]
                )
            )

    def _bounce_codex_root(
        self,
        base: Path,
        uuid: str,
        title: str | None,
        first_message: str | None,
        *,
        database_name: str = "state_5.sqlite",
        split_schema: bool = True,
        updated_at: int | None = None,
    ) -> Path:
        import sqlite3

        codex = base / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(codex / database_name)
        if split_schema:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT,"
                " first_user_message TEXT, updated_at INTEGER)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?)",
                (uuid, title, first_message, updated_at),
            )
        else:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?)", (uuid, title)
            )
        connection.commit()
        connection.close()
        return codex

    def test_bounce_defers_while_title_only_echoes_the_prompt(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000041"
        with tempfile.TemporaryDirectory() as base:
            codex = self._bounce_codex_root(
                Path(base),
                uuid,
                "who was the first person on the moon?",
                "who was the first person on the moon? and the second?",
                updated_at=1_800_000_000,
            )
            self.assertEqual(
                "",
                inventory_core.codex_bounce_prepare(
                    uuid, codex, now=1_800_000_030
                ),
            )
            self.assertFalse((codex / "session_index.jsonl").exists())

    def test_bounce_accepts_an_echo_title_once_the_thread_settles(
        self,
    ) -> None:
        uuid = "00000000-0000-4000-8000-000000000047"
        with tempfile.TemporaryDirectory() as base:
            codex = self._bounce_codex_root(
                Path(base),
                uuid,
                "at lollapalooza chicago in 2016",
                "at lollapalooza chicago in 2016",
                updated_at=1_800_000_000,
            )
            self.assertEqual(
                "at lollapalooza chicago in 2016",
                inventory_core.codex_bounce_prepare(
                    uuid, codex, now=1_800_000_301
                ),
            )
            last = json.loads(
                (codex / "session_index.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.assertEqual(
                "at lollapalooza chicago in 2016", last["thread_name"]
            )

    def test_bounce_uses_real_title_and_mirrors_it_to_the_index(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000042"
        with tempfile.TemporaryDirectory() as base:
            codex = self._bounce_codex_root(
                Path(base),
                uuid,
                "Session Naming Timing",
                "who was the first person on the moon?",
            )
            self.assertEqual(
                "Session Naming Timing",
                inventory_core.codex_bounce_prepare(uuid, codex),
            )
            entries = [
                json.loads(line)
                for line in (codex / "session_index.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [{"id": uuid, "thread_name": "Session Naming Timing"}],
                [
                    {"id": e["id"], "thread_name": e["thread_name"]}
                    for e in entries
                ],
            )

    def test_read_only_bounce_lookup_never_appends_to_the_session_index(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000049"
        with tempfile.TemporaryDirectory() as base:
            codex = self._bounce_codex_root(
                Path(base),
                uuid,
                "Read Only Launch Lookup",
                "an unrelated first prompt",
            )

            self.assertEqual(
                "Read Only Launch Lookup",
                inventory_core.codex_bounce_prepare(
                    uuid, codex, mirror_index=False
                ),
            )
            self.assertFalse((codex / "session_index.jsonl").exists())

    def test_bounce_honors_an_echo_shaped_index_entry_as_deliberate(
        self,
    ) -> None:
        # Codex never writes the index itself; an echo-shaped entry is a
        # kit auto-title and counts as the real name.
        uuid = "00000000-0000-4000-8000-000000000043"
        with tempfile.TemporaryDirectory() as base:
            codex = self._bounce_codex_root(
                Path(base),
                uuid,
                "what band does billy corgan play for?",
                "what band does billy corgan play for?",
            )
            index = codex / "session_index.jsonl"
            index.write_text(
                json.dumps(
                    {
                        "id": uuid,
                        "thread_name": "What band does billy corgan play",
                        "updated_at": "2026-08-01T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                "What band does billy corgan play",
                inventory_core.codex_bounce_prepare(uuid, codex),
            )
            self.assertEqual(
                1, len(index.read_text(encoding="utf-8").splitlines())
            )

    def test_bounce_prefers_an_explicit_index_rename(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000044"
        with tempfile.TemporaryDirectory() as base:
            codex = self._bounce_codex_root(
                Path(base),
                uuid,
                "Stale Database Title",
                "who was the first person on the moon?",
            )
            index = codex / "session_index.jsonl"
            index.write_text(
                json.dumps({"id": uuid, "thread_name": "Renamed By The Operator"})
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                "Renamed By The Operator",
                inventory_core.codex_bounce_prepare(uuid, codex),
            )
            self.assertEqual(
                1, len(index.read_text(encoding="utf-8").splitlines())
            )

    def test_bounce_trusts_any_title_on_a_pre_split_schema(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000045"
        with tempfile.TemporaryDirectory() as base:
            codex = self._bounce_codex_root(
                Path(base),
                uuid,
                "kit pushed name",
                None,
                split_schema=False,
            )
            self.assertEqual(
                "kit pushed name",
                inventory_core.codex_bounce_prepare(uuid, codex),
            )

    def test_bounce_reads_the_numerically_newest_state_database(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000046"
        with tempfile.TemporaryDirectory() as base:
            codex = self._bounce_codex_root(
                Path(base),
                uuid,
                "Old Schema Name",
                "unrelated prompt",
                database_name="state_5.sqlite",
            )
            self._bounce_codex_root(
                Path(base),
                uuid,
                "New Schema Name",
                "unrelated prompt",
                database_name="state_10.sqlite",
            )
            (codex / "state_12.sqlite").write_bytes(b"")
            self.assertEqual(
                "New Schema Name",
                inventory_core.codex_bounce_prepare(uuid, codex),
            )

    def test_placeholder_uuid_is_never_pushed(self) -> None:
        result = inventory_core.propagate_provider_title(
            "codex",
            "00000000-0000-0000-0000-000000000000",
            "Ghost thread",
            environ={"HOME": "/nonexistent"},
        )
        self.assertEqual([], result["provider_title_pushes"])

    def test_explicit_environ_without_home_never_escapes_the_sandbox(
        self,
    ) -> None:
        result = inventory_core.propagate_provider_title(
            "codex",
            "00000000-0000-4000-8000-000000000048",
            "Escapee thread",
            environ={"SHPOOL_SESSION_NAME": "main"},
        )
        self.assertEqual([], result["provider_title_pushes"])
        self.assertTrue(
            any(
                "no HOME" in warning
                for warning in result["provider_title_warnings"]
            )
        )

    def test_stop_event_titles_from_transcript_first_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            home, transcript = self._prebaked_home(Path(base))
            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": "fix the nginx 502 errors please",
                            },
                        }
                    )
                    + "\n"
                )
            payload = json.dumps(
                {
                    "hook_event_name": "Stop",
                    "session_id": self.UUID,
                    "transcript_path": str(transcript),
                }
            )
            result = inventory_core.auto_title_from_hook(
                payload, environ={"HOME": str(home)}
            )
            self.assertEqual("Fix the nginx 502 errors please", result["title"])

    def test_first_prompt_outranks_payload_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            home, transcript = self._prebaked_home(Path(base))
            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": "fix the nginx 502 errors please",
                            },
                        }
                    )
                    + "\n"
                )
            result = inventory_core.auto_title_from_hook(
                self._payload(transcript, "unrelated second question here"),
                environ={"HOME": str(home)},
            )
            self.assertEqual("Fix the nginx 502 errors please", result["title"])


class ClaudeBouncePrepareTests(unittest.TestCase):
    def _home(
        self,
        base: Path,
        uuid: str,
        intent: str | None,
        prompt_offset: float,
    ) -> Path:
        import datetime as dt

        home = base / "home"
        sessions = home / ".claude" / "sessions"
        project = home / ".claude" / "projects" / "-srv-project"
        sessions.mkdir(parents=True)
        project.mkdir(parents=True)
        intent_path = sessions / f"{uuid}.nameintent"
        if intent is not None:
            intent_path.write_text(intent + "\n", encoding="utf-8")
        intent_mtime = (
            intent_path.stat().st_mtime if intent is not None else 1_700_000_000
        )
        stamp = dt.datetime.fromtimestamp(
            intent_mtime + prompt_offset, dt.timezone.utc
        ).isoformat().replace("+00:00", "Z")
        records = [
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}, "timestamp": stamp},
            {"type": "user", "message": {"role": "user", "content": ["tool result"]}, "timestamp": stamp},
            {"type": "user", "message": {"role": "user", "content": "who are the last three headliners"}, "timestamp": stamp},
        ]
        (project / f"{uuid}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        return home

    def test_bounces_when_the_name_arrived_after_the_last_prompt(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000061"
        with tempfile.TemporaryDirectory() as raw:
            home = self._home(Path(raw), uuid, "Headliner Question", -120)
            self.assertEqual(
                ("Headliner Question", False),
                inventory_core.claude_bounce_prepare(uuid, home=home),
            )

    def test_clears_when_a_real_prompt_followed_the_intent(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000062"
        with tempfile.TemporaryDirectory() as raw:
            home = self._home(Path(raw), uuid, "Headliner Question", 120)
            self.assertEqual(
                ("", True),
                inventory_core.claude_bounce_prepare(uuid, home=home),
            )

    def test_defers_without_an_intent_and_ignores_meta_and_tool_records(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000063"
        with tempfile.TemporaryDirectory() as raw:
            home = self._home(Path(raw), uuid, None, 0)
            self.assertEqual(
                ("", False),
                inventory_core.claude_bounce_prepare(uuid, home=home),
            )


class CodexPendingAutoTitleTests(unittest.TestCase):
    def _codex_root(self, base: Path, rows: list[tuple]) -> Path:
        import sqlite3

        codex = base / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(codex / "state_5.sqlite")
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT,"
            " first_user_message TEXT, updated_at INTEGER)"
        )
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?, ?, ?)", rows
        )
        connection.commit()
        connection.close()
        return codex

    def _run(self, base: Path, codex: Path) -> list[dict[str, str]]:
        with mock.patch.dict(
            os.environ,
            {"SESSION_KIT_CODEX_HOME": str(codex)},
            clear=False,
        ):
            os.environ.pop("SESSION_KIT_CODEX_DB", None)
            return inventory_core.codex_pending_auto_titles(
                environ={"HOME": str(base)}
            )

    def test_auto_title_offers_live_rename_from_the_caller_sandbox(self) -> None:
        import time

        uuid = "00000000-0000-4000-8000-000000000058"
        now = int(time.time())
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            codex = self._codex_root(
                base,
                [(uuid, "trace the cdn purge failure", "trace the cdn purge failure", now - 60)],
            )
            with mock.patch.object(
                inventory_core, "_push_codex_live_rename", return_value=([], [])
            ) as live:
                titled = self._run(base, codex)
            self.assertEqual(1, len(titled))
            live.assert_called_once()
            call = live.call_args
            self.assertEqual(
                (
                    Path(base) / ".local" / "state" / "session-kit",
                    uuid,
                    titled[0]["title"],
                ),
                call.args,
            )
            self.assertGreater(call.kwargs["timeout_seconds"], 0)
            self.assertTrue(call.kwargs["still_automatic"]())

    def test_auto_title_live_rename_honors_kill_switch(self) -> None:
        import time

        uuid = "00000000-0000-4000-8000-000000000059"
        now = int(time.time())
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            codex = self._codex_root(
                base,
                [(uuid, "trace the cdn purge failure", "trace the cdn purge failure", now - 60)],
            )
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CODEX_HOME": str(codex)},
                clear=False,
            ):
                os.environ.pop("SESSION_KIT_CODEX_DB", None)
                with mock.patch.object(
                    inventory_core, "_push_codex_live_rename", return_value=([], [])
                ) as live:
                    titled = inventory_core.codex_pending_auto_titles(
                        environ={
                            "HOME": str(base),
                            "SESSION_KIT_CODEX_LIVE_RENAME": "0",
                        }
                    )
            self.assertEqual(1, len(titled))
            live.assert_not_called()

    def test_titles_a_recent_echo_thread_into_both_stores(self) -> None:
        import sqlite3
        import time

        uuid = "00000000-0000-4000-8000-000000000051"
        now = int(time.time())
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            codex = self._codex_root(
                base,
                [
                    (
                        uuid,
                        "what band does billy corgan play for?",
                        "what band does billy corgan play for?",
                        now - 60,
                    )
                ],
            )
            titled = self._run(base, codex)
            self.assertEqual(
                [{"uuid": uuid, "title": "What band does billy corgan play"}],
                titled,
            )
            connection = sqlite3.connect(codex / "state_5.sqlite")
            self.assertEqual(
                ("What band does billy corgan play",),
                connection.execute(
                    "SELECT title FROM threads WHERE id = ?", (uuid,)
                ).fetchone(),
            )
            connection.close()
            entries = [
                json.loads(line)
                for line in (codex / "session_index.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [(uuid, "What band does billy corgan play")],
                [(e["id"], e["thread_name"]) for e in entries],
            )
            # Self-terminating: the index entry excludes it next pass.
            self.assertEqual([], self._run(base, codex))

    def test_never_touches_named_or_indexed_threads(self) -> None:
        """Age is not evidence of anything; a name is.

        The titler used to skip threads older than a week, which left them
        nameless forever (ledger B6). Age no longer decides: what protects a
        thread is a real name in the database or a deliberate entry in the
        session index. An old thread whose title is still the prompt it
        started with is exactly the thread nobody has named.
        """
        import time

        now = int(time.time())
        named = "00000000-0000-4000-8000-000000000052"
        old_echo = "00000000-0000-4000-8000-000000000053"
        indexed = "00000000-0000-4000-8000-000000000054"
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            codex = self._codex_root(
                base,
                [
                    (named, "Real Name", "unrelated prompt", now - 60),
                    (old_echo, "old echo", "old echo", now - 8 * 86400),
                    (indexed, "echo text", "echo text", now - 60),
                ],
            )
            (codex / "session_index.jsonl").write_text(
                json.dumps({"id": indexed, "thread_name": "Kept Name"})
                + "\n",
                encoding="utf-8",
            )
            titled = self._run(base, codex)
            self.assertEqual([old_echo], [item["uuid"] for item in titled])
            entries = [
                json.loads(line)
                for line in (codex / "session_index.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                "Kept Name",
                [e["thread_name"] for e in entries if e["id"] == indexed][-1],
            )
            self.assertEqual([], [e for e in entries if e["id"] == named])

    def test_heals_database_title_from_curated_index_entry(self) -> None:
        import sqlite3
        import time

        uuid = "00000000-0000-4000-8000-000000000056"
        now = int(time.time())
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            codex = self._codex_root(
                base,
                [(uuid, "what is the capital of peru", "what is the capital of peru", now - 60)],
            )
            (codex / "session_index.jsonl").write_text(
                json.dumps({"id": uuid, "thread_name": "prompt echo stub"})
                + "\n"
                + json.dumps({"id": uuid, "thread_name": "Peru Capital Check"})
                + "\n",
                encoding="utf-8",
            )
            # Not reported as a fresh title: the heal converges stores for a
            # thread that already carries its deliberate name in the index.
            self.assertEqual([], self._run(base, codex))
            connection = sqlite3.connect(codex / "state_5.sqlite")
            self.assertEqual(
                ("Peru Capital Check",),
                connection.execute(
                    "SELECT title FROM threads WHERE id = ?", (uuid,)
                ).fetchone(),
            )
            connection.close()
            # The index itself is untouched by the heal (no new entries).
            self.assertEqual(
                2,
                len(
                    (codex / "session_index.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ),
            )

    def test_heal_never_replaces_a_real_database_name(self) -> None:
        import sqlite3
        import time

        uuid = "00000000-0000-4000-8000-000000000057"
        now = int(time.time())
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            codex = self._codex_root(
                base,
                [(uuid, "Deliberate Db Name", "some first prompt", now - 60)],
            )
            (codex / "session_index.jsonl").write_text(
                json.dumps({"id": uuid, "thread_name": "Different Index Name"})
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual([], self._run(base, codex))
            connection = sqlite3.connect(codex / "state_5.sqlite")
            self.assertEqual(
                ("Deliberate Db Name",),
                connection.execute(
                    "SELECT title FROM threads WHERE id = ?", (uuid,)
                ).fetchone(),
            )
            connection.close()

    def test_kill_switch_disables_the_titler(self) -> None:
        import time

        uuid = "00000000-0000-4000-8000-000000000055"
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            codex = self._codex_root(
                base, [(uuid, "echo", "echo", int(time.time()) - 60)]
            )
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CODEX_HOME": str(codex)},
                clear=False,
            ):
                for kill in (
                    {"SESSION_KIT_AUTO_NAME": "0"},
                    {"SESSION_KIT_CODEX_AUTOTITLE": "0"},
                ):
                    self.assertEqual(
                        [],
                        inventory_core.codex_pending_auto_titles(
                            environ={"HOME": str(base), **kill}
                        ),
                    )


class NameOwnershipTests(unittest.TestCase):
    """Who owns a name, and what an automatic pass may do about it."""

    UUID = "00000000-0000-4000-8000-000000000201"

    def _sandbox(self, base: Path) -> tuple[Path, dict[str, str]]:
        home = base / "home"
        home.mkdir(mode=0o700)
        return home, {"HOME": str(home)}

    def _document(self, home: Path) -> dict:
        path = home / ".config" / "session-kit" / "inventory.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _human_rename(
        self, home: Path, provider: str, uuid: str, title: str | None
    ) -> None:
        """Rename the way `sp name` and the picker do: through the alias tier."""
        state = home / "state"
        state.mkdir(mode=0o700, exist_ok=True)
        path = home / ".config" / "session-kit" / "inventory.json"
        inventory_core._private_alias_parent(path)
        with mock.patch.dict(
            os.environ, {"SESSION_KIT_CONFIG": str(path)}, clear=False
        ):
            inventory_core.mutate_canonical_alias(
                {"state_dir": state}, provider, uuid, title
            )

    def test_an_automatic_claim_is_taken_once_and_then_reported_as_owned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home, env = self._sandbox(Path(raw))
            self.assertEqual("", inventory_core.name_owner("codex", self.UUID, environ=env))
            self.assertEqual(
                "", inventory_core.claim_automatic_name("codex", self.UUID, environ=env)
            )
            self.assertEqual(
                "automatic",
                inventory_core.name_owner("codex", self.UUID, environ=env),
            )
            self.assertIn(
                "already owns",
                inventory_core.claim_automatic_name("codex", self.UUID, environ=env),
            )
            record = self._document(home)["name_ownership"][f"codex:{self.UUID}"]
            self.assertEqual("automatic", record["owner"])
            self.assertTrue(record["at"])

    def test_only_an_untouched_automatic_claim_can_be_released(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home, env = self._sandbox(Path(raw))
            self.assertEqual(
                "", inventory_core.claim_automatic_name("codex", self.UUID, environ=env)
            )
            self.assertTrue(
                inventory_core.release_automatic_name_claim(
                    "codex", self.UUID, environ=env
                )
            )
            self.assertEqual("", inventory_core.name_owner("codex", self.UUID, environ=env))

            self._human_rename(home, "codex", self.UUID, "the operator named this")
            self.assertFalse(
                inventory_core.release_automatic_name_claim(
                    "codex", self.UUID, environ=env
                )
            )
            self.assertEqual(
                "human", inventory_core.name_owner("codex", self.UUID, environ=env)
            )

    def test_a_human_rename_takes_ownership_and_never_gives_it_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home, env = self._sandbox(Path(raw))
            self.assertEqual(
                "", inventory_core.claim_automatic_name("claude", self.UUID, environ=env)
            )
            self._human_rename(home, "claude", self.UUID, "the operator named this")
            self.assertEqual(
                "human", inventory_core.name_owner("claude", self.UUID, environ=env)
            )
            self.assertEqual(
                frozenset({f"claude:{self.UUID}"}),
                inventory_core.human_named_keys(env),
            )
            stamped = self._document(home)["name_ownership"][f"claude:{self.UUID}"]
            self.assertIn(
                "already owns",
                inventory_core.claim_automatic_name("claude", self.UUID, environ=env),
            )
            # `sp name reset` drops the alias. The override outlives it, so no
            # later automatic pass can resurrect a name over the reset.
            self._human_rename(home, "claude", self.UUID, None)
            self.assertEqual(
                {}, self._document(home)["aliases"]
            )
            self.assertEqual(
                "human", inventory_core.name_owner("claude", self.UUID, environ=env)
            )
            self.assertIn(
                "already owns",
                inventory_core.claim_automatic_name("claude", self.UUID, environ=env),
            )
            # A second rename does not re-stamp the moment the override began.
            self._human_rename(home, "claude", self.UUID, "the operator named it again")
            self.assertEqual(
                stamped,
                self._document(home)["name_ownership"][f"claude:{self.UUID}"],
            )


    def test_a_legacy_document_is_read_from_the_evidence_it_kept(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home, env = self._sandbox(Path(raw))
            path = home / ".config" / "session-kit" / "inventory.json"
            inventory_core._private_alias_parent(path)
            renamed = uuid_for(202)
            self_named = uuid_for(203)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"claude:{renamed}": "A person wrote this",
                            f"codex:{self_named}": "Session Kit Updates",
                        },
                        "automatic_titles": {
                            f"codex:{self_named}": "Session Kit Updates"
                        },
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)
            # No ownership record exists yet: an alias is a human rename unless
            # the automatic tier holds the same text, which only a self-name
            # ever writes.
            self.assertEqual(
                "human", inventory_core.name_owner("claude", renamed, environ=env)
            )
            self.assertEqual(
                "automatic", inventory_core.name_owner("codex", self_named, environ=env)
            )
            self.assertEqual(
                frozenset({f"claude:{renamed}"}),
                inventory_core.human_named_keys(env),
            )

    def test_an_unreadable_document_owns_nothing(self) -> None:
        self.assertEqual("", inventory_core.name_owner("codex", self.UUID, environ={}))
        self.assertEqual(frozenset(), inventory_core.human_named_keys({}))
        self.assertIn(
            "no home",
            inventory_core.claim_automatic_name("codex", self.UUID, environ={}),
        )
        self.assertEqual(
            "", inventory_core.name_owner("codex", "not-a-uuid", environ={"HOME": "/"})
        )


class ClaudeFirstPromptOwnershipTests(unittest.TestCase):
    """The Claude hook claims a thread's name at its first prompt, once."""

    UUID = "00000000-0000-4000-8000-000000000211"

    def _prebaked_home(self, base: Path) -> tuple[Path, Path]:
        home = base / "home"
        (home / ".claude" / "sessions").mkdir(parents=True, mode=0o700)
        project = home / ".claude" / "projects" / "-srv-project"
        project.mkdir(parents=True, mode=0o700)
        transcript = project / f"{self.UUID}.jsonl"
        records = [
            {
                "type": "user",
                "isMeta": True,
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Continue from where you left off."}
                    ],
                },
            }
        ]
        transcript.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return home, transcript

    def _payload(self, transcript: Path, prompt: str, event: str) -> str:
        return json.dumps(
            {
                "hook_event_name": event,
                "session_id": self.UUID,
                "transcript_path": str(transcript),
                "prompt": prompt,
            }
        )

    def test_the_first_prompt_claims_the_name_and_the_stop_firing_does_not(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home, transcript = self._prebaked_home(Path(raw))
            env = {"HOME": str(home)}
            first = inventory_core.auto_title_from_hook(
                self._payload(
                    transcript,
                    "trace the cdn purge failure across both edges",
                    "UserPromptSubmit",
                ),
                environ=env,
            )
            self.assertEqual(
                "Trace the cdn purge failure across both", first["title"]
            )
            self.assertEqual(
                "automatic",
                inventory_core.name_owner("claude", self.UUID, environ=env),
            )
            # session_start fires twice for a brand-new session and the hook
            # fires again at Stop. The claim is what keeps this a no-op.
            before = transcript.read_bytes()
            again = inventory_core.auto_title_from_hook(
                self._payload(transcript, "a different second prompt here", "Stop"),
                environ=env,
            )
            self.assertIsNone(again["title"])
            self.assertEqual(
                "an automatic name already owns this session", again["reason"]
            )
            self.assertEqual(before, transcript.read_bytes())

    def test_a_human_rename_stops_the_hook_before_it_writes_anything(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home, transcript = self._prebaked_home(Path(raw))
            env = {"HOME": str(home)}
            state = home / "state"
            state.mkdir(mode=0o700)
            path = home / ".config" / "session-kit" / "inventory.json"
            inventory_core._private_alias_parent(path)
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": str(path)}, clear=False
            ):
                inventory_core.mutate_canonical_alias(
                    {"state_dir": state}, "claude", self.UUID, "the operator named this"
                )
            before = transcript.read_bytes()
            result = inventory_core.auto_title_from_hook(
                self._payload(
                    transcript, "trace the cdn purge failure now", "UserPromptSubmit"
                ),
                environ=env,
            )
            self.assertIsNone(result["title"])
            self.assertEqual("a human name owns this session", result["reason"])
            self.assertEqual([], result["pushes"])
            self.assertEqual(before, transcript.read_bytes())


class CodexFirstTurnOwnershipTests(unittest.TestCase):
    """A Codex thread is claimed at its first turn, the first moment it exists."""

    def _codex_root(self, base: Path, rows: list[tuple]) -> Path:
        import sqlite3

        codex = base / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(codex / "state_5.sqlite")
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT,"
            " first_user_message TEXT, updated_at INTEGER)"
        )
        connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?)", rows)
        connection.commit()
        connection.close()
        return codex

    def _run(self, base: Path, codex: Path) -> list[dict[str, str]]:
        with mock.patch.dict(
            os.environ, {"SESSION_KIT_CODEX_HOME": str(codex)}, clear=False
        ):
            os.environ.pop("SESSION_KIT_CODEX_DB", None)
            return inventory_core.codex_pending_auto_titles(
                environ={"HOME": str(base)}
            )

    def _thread_title(self, codex: Path, uuid: str) -> str:
        import sqlite3

        connection = sqlite3.connect(codex / "state_5.sqlite")
        try:
            return connection.execute(
                "SELECT title FROM threads WHERE id = ?", (uuid,)
            ).fetchone()[0]
        finally:
            connection.close()

    def test_the_first_turn_claims_the_thread_and_a_later_pass_leaves_it_alone(
        self,
    ) -> None:
        import time

        uuid = uuid_for(221)
        now = int(time.time())
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            base.chmod(0o700)
            codex = self._codex_root(
                base,
                [(uuid, "trace the cdn purge failure", "trace the cdn purge failure", now - 60)],
            )
            titled = self._run(base, codex)
            self.assertEqual(1, len(titled))
            env = {"HOME": str(base)}
            self.assertEqual(
                "automatic", inventory_core.name_owner("codex", uuid, environ=env)
            )
            named = self._thread_title(codex, uuid)
            # Codex re-stamps the raw prompt over the title and the index entry
            # is gone; only the claim keeps the second pass from renaming.
            import sqlite3

            connection = sqlite3.connect(codex / "state_5.sqlite")
            connection.execute(
                "UPDATE threads SET title = ? WHERE id = ?",
                ("trace the cdn purge failure", uuid),
            )
            connection.commit()
            connection.close()
            (codex / "session_index.jsonl").unlink()
            self.assertEqual([], self._run(base, codex))
            self.assertTrue(named)
            self.assertFalse((codex / "session_index.jsonl").is_file())

    def test_a_human_rename_is_never_titled_or_healed(self) -> None:
        import time

        uuid = uuid_for(222)
        now = int(time.time())
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            base.chmod(0o700)
            codex = self._codex_root(
                base,
                [(uuid, "trace the cdn purge failure", "trace the cdn purge failure", now - 60)],
            )
            state = base / "state"
            state.mkdir(mode=0o700)
            config = base / ".config" / "session-kit" / "inventory.json"
            inventory_core._private_alias_parent(config)
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": str(config)}, clear=False
            ):
                inventory_core.mutate_canonical_alias(
                    {"state_dir": state}, "codex", uuid, "the operator named this"
                )
            # A curated index entry plus a prompt-echo title is exactly the
            # shape the healer re-asserts. It must not touch a human's name.
            (codex / "session_index.jsonl").write_text(
                json.dumps({"id": uuid, "thread_name": "An older automatic name"})
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual([], self._run(base, codex))
            self.assertEqual(
                "trace the cdn purge failure", self._thread_title(codex, uuid)
            )

    def test_a_queued_retry_never_replays_over_a_human_rename_after_a_restart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            base.chmod(0o700)
            uuid = uuid_for(223)
            state = base / ".local" / "state" / "session-kit"
            state.mkdir(mode=0o700, parents=True)
            # The retry queue is on disk from before the reboot.
            (state / "provider-title-retries.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "entries": {
                            f"codex:{uuid}": {
                                "provider": "codex",
                                "uuid": uuid,
                                "title": "An Older Automatic Name",
                                "attempts": 0,
                                "updated_at": "2026-08-09T00:00:00Z",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            (state / "provider-title-retries.json").chmod(0o600)
            config = base / ".config" / "session-kit" / "inventory.json"
            inventory_core._private_alias_parent(config)
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": str(config)}, clear=False
            ):
                inventory_core.mutate_canonical_alias(
                    {"state_dir": base / "scratch"}, "codex", uuid, "the operator named this"
                )
            with mock.patch.object(
                inventory_core, "propagate_provider_title"
            ) as push:
                replayed = inventory_core._reconcile_pending_provider_titles(
                    {"state_dir": state}, "codex", {"HOME": str(base)}
                )
            self.assertEqual([], replayed)
            push.assert_not_called()


class NativeRenameOwnershipTests(unittest.TestCase):
    """A /rename typed into a provider is a person naming their own work."""

    def _sandbox(self, base: Path) -> tuple[Path, Path, dict[str, str]]:
        home = base / "home"
        home.mkdir(mode=0o700)
        config = home / ".config" / "session-kit" / "inventory.json"
        inventory_core._private_alias_parent(config)
        return home, config, {"HOME": str(home)}

    def _seed(self, config: Path, document: dict) -> None:
        config.write_text(json.dumps(document), encoding="utf-8")
        config.chmod(0o600)

    def _document(self, config: Path) -> dict:
        return json.loads(config.read_text(encoding="utf-8"))

    def _title(self, document: dict, provider: str, uuid: str, native: str) -> tuple:
        """What a row would show, with the document's own evidence."""
        return inventory_core._provider_title_info(
            provider,
            uuid,
            native,
            document.get("aliases", {}),
            "/srv/project",
            1_700_000_000_000,
            document.get("automatic_titles", {}),
            provider_title_is_explicit=True,
            pushed_titles=document.get("pushed_titles", {}),
            name_ownership=document.get("name_ownership", {}),
        )

    def test_the_live_stale_room_self_corrects_and_never_reverts(self) -> None:
        """Seeded exactly as observed: alias and automatic both stale.

        The room was self-named "Session Kit Audit"; the automatic title was
        written as an alias too, so the alias masked the native store. The
        operator typed /rename in Codex. Both Codex stores read "kit test"; the picker
        still read "Session Kit Audit".
        """
        uuid = uuid_for(301)
        key = f"codex:{uuid}"
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            base.chmod(0o700)
            home, config, env = self._sandbox(base)
            self._seed(
                config,
                {
                    "schema_version": 1,
                    "aliases": {key: "Session Kit Audit"},
                    "automatic_titles": {key: "Session Kit Audit"},
                },
            )
            # The row is honest immediately: the alias is kit-authored (it
            # equals the automatic title), so the rename outranks it before
            # anything has been written.
            self.assertEqual(
                ("kit test", "native"),
                self._title(self._document(config), "codex", uuid, "kit test"),
            )
            adopted = inventory_core.adopt_native_rename(
                "codex", uuid, "kit test", environ=env
            )
            # Byte for byte, including the lower case actually typed. A
            # human title is an opaque string: no Title Case, no folding.
            self.assertEqual("kit test", adopted)
            document = self._document(config)
            # After: one name, owned by a person, on every tier at once.
            self.assertEqual("kit test", document["aliases"][key])
            self.assertEqual("kit test", document["pushed_titles"][key])
            self.assertEqual("human", document["name_ownership"][key]["owner"])
            self.assertNotIn(key, document.get("automatic_titles", {}))
            self.assertEqual(
                ("kit test", "alias"),
                self._title(document, "codex", uuid, "kit test"),
            )
            # Every surface, byte-exact: the row, the detail view, the alias
            # list. Nothing along the way re-cases a name a person chose.
            rendered = inventory_core.render_inventory(
                {
                    "schema_version": 1,
                    "generated_at": "2026-08-09T00:00:00Z",
                    "source": "live",
                    "stale": False,
                    "warnings": [],
                    "sessions": [
                        {
                            "row": 1,
                            "terminal_number": 4,
                            "shpool_id": "s-1",
                            "provider": "codex",
                            "display_provider": "codex",
                            "identity": {"uuid": uuid, "confidence": "exact"},
                            "title": document["aliases"][key],
                            "display_title": document["aliases"][key],
                            "agent_status": "idle",
                            "availability": "ready",
                            "shpool_status": "Disconnected",
                            "cwd": "/srv/project",
                            "subagents": [],
                        }
                    ],
                    "outside_agents": [],
                }
            )
            self.assertIn("kit test", rendered)
            self.assertNotIn("Kit Test", rendered)
            self.assertNotIn("Session Kit Audit", rendered)
            self.assertEqual(
                "human", inventory_core.name_owner("codex", uuid, environ=env)
            )
            # A reconciliation pass is idempotent, nothing left to adopt.
            self.assertEqual(
                "", inventory_core.adopt_native_rename("codex", uuid, "kit test", environ=env)
            )
            # And no automatic path may put the old name back, ever.
            self.assertIn(
                "already owns",
                inventory_core.claim_automatic_name("codex", uuid, environ=env),
            )
            with self.assertRaisesRegex(
                inventory_core.CollectionError, "already owns"
            ):
                self._refuse_automatic(config, base, uuid)
            self.assertEqual(
                "kit test", self._document(config)["aliases"][key]
            )

    def test_codex_rename_survives_titler_and_restore_on_both_surfaces(self) -> None:
        """A settled Codex /rename is adopted before restore can overwrite it."""
        import sqlite3
        import time

        uuid = uuid_for(302)
        key = f"codex:{uuid}"
        automatic = "Crawl Budget Report Review"
        chosen = "A Hand Chosen Title"
        chosen_again = "A Hand Chosen Title Again"
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            base.chmod(0o700)
            home, config, env = self._sandbox(base)
            state = base / "state"
            state.mkdir(mode=0o700)
            codex = home / ".codex"
            codex.mkdir(mode=0o700)
            database = codex / "state_5.sqlite"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT,"
                " first_user_message TEXT, updated_at INTEGER)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?)",
                (
                    uuid,
                    "trace the cdn purge failure",
                    "trace the cdn purge failure",
                    int(time.time()),
                ),
            )
            connection.commit()
            connection.close()
            env.update(
                {
                    "SESSION_KIT_CONFIG": str(config),
                    "SESSION_KIT_STATE_DIR": str(state),
                    "SESSION_KIT_CODEX_HOME": str(codex),
                    "SESSION_KIT_CODEX_LIVE_RENAME": "0",
                }
            )
            with mock.patch.dict(os.environ, env, clear=False):
                inventory_core.mutate_canonical_automatic_title(
                    {"state_dir": state},
                    "codex",
                    uuid,
                    automatic,
                    overwrite=True,
                )
                seeded = inventory_core.propagate_provider_title(
                    "codex", uuid, automatic, environ=env
                )
                self.assertEqual([], seeded["provider_title_warnings"])

                # This is the exact store shape Codex /rename leaves: the
                # index and threads.title carry the same operator-typed name.
                with (codex / "session_index.jsonl").open(
                    "a", encoding="utf-8"
                ) as index:
                    index.write(
                        json.dumps({"id": uuid, "thread_name": chosen}) + "\n"
                    )
                connection = sqlite3.connect(database)
                connection.execute(
                    "UPDATE threads SET title = ? WHERE id = ?", (chosen, uuid)
                )
                connection.commit()
                connection.close()

                inventory_core.codex_pending_auto_titles(environ=env)
                document = self._document(config)
                self.assertEqual(chosen, document["aliases"][key])
                self.assertEqual("human", document["name_ownership"][key]["owner"])
                self.assertNotIn(key, document.get("automatic_titles", {}))
                self.assertEqual(
                    "human",
                    inventory_core.name_owner("codex", uuid, environ=env),
                )

                # Restore runs this exact alias-push path before launching the
                # provider. It must re-assert the adopted name, never the old
                # automatic title.
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    status = inventory_core._alias_command(
                        argparse.Namespace(
                            alias_action="push", provider="codex", uuid=uuid
                        ),
                        inventory_core.load_config(),
                    )
                self.assertEqual(0, status, output.getvalue())

                # Human ownership is not a reason to ignore later native
                # news. Rename the restored thread again, reconcile again,
                # and restore again through the real alias-push path.
                with (codex / "session_index.jsonl").open(
                    "a", encoding="utf-8"
                ) as index:
                    index.write(
                        json.dumps({"id": uuid, "thread_name": chosen_again}) + "\n"
                    )
                connection = sqlite3.connect(database)
                connection.execute(
                    "UPDATE threads SET title = ? WHERE id = ?",
                    (chosen_again, uuid),
                )
                connection.commit()
                connection.close()

                inventory_core.codex_pending_auto_titles(environ=env)
                document = self._document(config)
                self.assertEqual(chosen_again, document["aliases"][key])
                self.assertEqual(chosen_again, document["pushed_titles"][key])
                self.assertEqual("human", document["name_ownership"][key]["owner"])
                self.assertNotIn(key, document.get("automatic_titles", {}))

                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    status = inventory_core._alias_command(
                        argparse.Namespace(
                            alias_action="push", provider="codex", uuid=uuid
                        ),
                        inventory_core.load_config(),
                    )
                self.assertEqual(0, status, output.getvalue())

            latest = inventory_core.read_codex_session_index(
                codex / "session_index.jsonl"
            )
            connection = sqlite3.connect(database)
            stored = connection.execute(
                "SELECT title FROM threads WHERE id = ?", (uuid,)
            ).fetchone()[0]
            connection.close()
            self.assertEqual(chosen_again, latest[uuid])
            self.assertEqual(chosen_again, stored)

    def _refuse_automatic(self, config: Path, base: Path, uuid: str) -> None:
        state = base / "state"
        state.mkdir(mode=0o700, exist_ok=True)
        with mock.patch.dict(
            os.environ, {"SESSION_KIT_CONFIG": str(config)}, clear=False
        ):
            inventory_core.mutate_canonical_automatic_title(
                {"state_dir": state}, "codex", uuid, "Session Kit Audit", overwrite=True
            )

    def test_a_rename_after_the_kit_pushed_its_own_title_is_adopted(self) -> None:
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                uuid = uuid_for(310 if provider == "claude" else 311)
                key = f"{provider}:{uuid}"
                with tempfile.TemporaryDirectory() as raw:
                    base = Path(raw)
                    base.chmod(0o700)
                    home, config, env = self._sandbox(base)
                    self._seed(
                        config,
                        {
                            "schema_version": 1,
                            "aliases": {},
                            "automatic_titles": {key: "Session Kit Audit"},
                            "pushed_titles": {key: "Session Kit Audit"},
                        },
                    )
                    # The kit's own echo is not a rename.
                    self.assertEqual(
                        "",
                        inventory_core.adopt_native_rename(
                            provider, uuid, "Session Kit Audit", environ=env
                        ),
                    )
                    self.assertEqual(
                        "Kit Test",
                        inventory_core.adopt_native_rename(
                            provider, uuid, "Kit Test", environ=env
                        ),
                    )
                    document = self._document(config)
                    self.assertEqual("Kit Test", document["aliases"][key])
                    self.assertEqual(
                        "human", document["name_ownership"][key]["owner"]
                    )
                    self.assertEqual(
                        ("Kit Test", "alias"),
                        self._title(document, provider, uuid, "Kit Test"),
                    )

    def test_derived_placeholder_is_never_adopted_but_missing_source_is(self) -> None:
        uuid = uuid_for(312)
        key = f"claude:{uuid}"
        for source, expected in (("derived", ""), ("", "v2-5e")):
            with self.subTest(source=source or "missing"):
                with tempfile.TemporaryDirectory() as raw:
                    base = Path(raw)
                    base.chmod(0o700)
                    _home, config, env = self._sandbox(base)
                    self._seed(
                        config,
                        {
                            "schema_version": 1,
                            "aliases": {key: "Session Kit Closeout"},
                            "automatic_titles": {key: "Session Kit Closeout"},
                            "pushed_titles": {key: "Session Kit Closeout"},
                        },
                    )
                    adopted = inventory_core.adopt_native_rename(
                        "claude",
                        uuid,
                        "v2-5e",
                        native_name_source=source,
                        environ=env,
                    )
                    self.assertEqual(expected, adopted)
                    document = self._document(config)
                    if source == "derived":
                        self.assertEqual("Session Kit Closeout", document["aliases"][key])
                    else:
                        self.assertEqual("v2-5e", document["aliases"][key])

    def test_pre_push_native_value_is_pending_but_a_third_value_is_human(self) -> None:
        uuid = uuid_for(317)
        key = f"claude:{uuid}"
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            base.chmod(0o700)
            _home, config, env = self._sandbox(base)
            self._seed(
                config,
                {
                    "schema_version": 1,
                    "aliases": {key: "New Kit Name"},
                    "automatic_titles": {key: "New Kit Name"},
                    "pushed_titles": {key: "New Kit Name"},
                    "pending_native_titles": {
                        key: {
                            "title": "Old Registry Name",
                            "nameSince": 100,
                            "nameSource": "",
                        }
                    },
                },
            )
            self.assertEqual(
                "",
                inventory_core.adopt_native_rename(
                    "claude",
                    uuid,
                    "Old Registry Name",
                    native_name_since=100,
                    environ=env,
                ),
            )
            self.assertEqual(
                "A Third Human Name",
                inventory_core.adopt_native_rename(
                    "claude",
                    uuid,
                    "A Third Human Name",
                    native_name_since=200,
                    environ=env,
                ),
            )
            document = self._document(config)
            self.assertEqual("A Third Human Name", document["aliases"][key])
            self.assertNotIn(key, document.get("pending_native_titles", {}))

    def test_catch_up_then_rename_back_to_old_text_is_adopted_immediately(self) -> None:
        """Exact blocking lane sequence: push, catch up, then rename back."""
        uuid = uuid_for(318)
        key = f"claude:{uuid}"
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            base.chmod(0o700)
            _home, config, env = self._sandbox(base)
            self._seed(
                config,
                {
                    "schema_version": 1,
                    "aliases": {key: "New Kit Name"},
                    "automatic_titles": {key: "New Kit Name"},
                    "pushed_titles": {key: "New Kit Name"},
                    "pending_native_titles": {
                        key: {
                            "title": "Old Human Name",
                            "nameSince": 100,
                            "nameSource": "user",
                        }
                    },
                },
            )

            self.assertEqual(
                "",
                inventory_core.adopt_native_rename(
                    "claude",
                    uuid,
                    "New Kit Name",
                    native_name_source="",
                    native_name_since=100,
                    environ=env,
                ),
            )
            self.assertNotIn(
                key, self._document(config).get("pending_native_titles", {})
            )

            self.assertEqual(
                "Old Human Name",
                inventory_core.adopt_native_rename(
                    "claude",
                    uuid,
                    "Old Human Name",
                    native_name_source="user",
                    native_name_since=200,
                    environ=env,
                ),
            )
            document = self._document(config)
            self.assertEqual("Old Human Name", document["aliases"][key])
            self.assertEqual("human", document["name_ownership"][key]["owner"])

    def test_a_native_rename_after_sp_name_replaces_it_for_good(self) -> None:
        """`sp name` is not a shield. The newest human act is the name.

        No timestamp is needed: `pushed_titles` holds the last value the kit
        wrote into the provider's store, and `sp name` writes through that
        same push. A native store that disagrees with it can only have been
        typed afterwards, so the divergence itself dates the rename.
        """
        for provider, number in (("claude", 315), ("codex", 316)):
            with self.subTest(provider=provider):
                uuid = uuid_for(number)
                key = f"{provider}:{uuid}"
                with tempfile.TemporaryDirectory() as raw:
                    base = Path(raw)
                    base.chmod(0o700)
                    home, config, env = self._sandbox(base)
                    # Exactly what `sp name Old Name` leaves behind: the alias,
                    # human ownership, and the value it pushed to the provider.
                    self._seed(
                        config,
                        {
                            "schema_version": 1,
                            "aliases": {key: "Old Name"},
                            "pushed_titles": {key: "Old Name"},
                            "name_ownership": {
                                key: {"owner": "human", "at": "2026-08-09T00:00:00Z"}
                            },
                        },
                    )
                    # The row shows it before anything is written.
                    self.assertEqual(
                        ("New Name", "native"),
                        self._title(self._document(config), provider, uuid, "New Name"),
                    )
                    self.assertEqual(
                        "New Name",
                        inventory_core.adopt_native_rename(
                            provider, uuid, "New Name", environ=env
                        ),
                    )
                    document = self._document(config)
                    self.assertEqual("New Name", document["aliases"][key])
                    self.assertEqual("New Name", document["pushed_titles"][key])
                    self.assertEqual(
                        "human", document["name_ownership"][key]["owner"]
                    )
                    self.assertEqual(
                        ("New Name", "alias"),
                        self._title(document, provider, uuid, "New Name"),
                    )
                    # Ownership stays human; the stale value cannot come back.
                    self.assertEqual(
                        "human",
                        inventory_core.name_owner(provider, uuid, environ=env),
                    )
                    self.assertIn(
                        "already owns",
                        inventory_core.claim_automatic_name(
                            provider, uuid, environ=env
                        ),
                    )
                    self.assertEqual(
                        "",
                        inventory_core.adopt_native_rename(
                            provider, uuid, "New Name", environ=env
                        ),
                    )
                    self.assertNotIn(
                        "Old Name", json.dumps(self._document(config))
                    )

    def test_a_capitalisation_only_rename_sticks_byte_exact(self) -> None:
        """Changing only the capitals is still a person renaming their work.

        The kit has no standing to rule that it did not count, and no reason
        to believe a store that disagrees about case is the store echoing
        itself. Exact comparison, exact storage, exact display.
        """
        uuid = uuid_for(314)
        key = f"codex:{uuid}"
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            base.chmod(0o700)
            home, config, env = self._sandbox(base)
            self._seed(
                config,
                {
                    "schema_version": 1,
                    "aliases": {},
                    "automatic_titles": {key: "Session Kit Audit"},
                    "pushed_titles": {key: "Session Kit Audit"},
                },
            )
            adopted = inventory_core.adopt_native_rename(
                "codex", uuid, "session kit audit", environ=env
            )
            self.assertEqual("session kit audit", adopted)
            document = self._document(config)
            self.assertEqual("session kit audit", document["aliases"][key])
            self.assertEqual("session kit audit", document["pushed_titles"][key])
            self.assertEqual("human", document["name_ownership"][key]["owner"])
            self.assertNotIn(key, document.get("automatic_titles", {}))
            self.assertEqual(
                ("session kit audit", "alias"),
                self._title(document, "codex", uuid, "session kit audit"),
            )
            self.assertEqual(
                "human", inventory_core.name_owner("codex", uuid, environ=env)
            )
            # And the automatic tier can never put the capitals back.
            self.assertIn(
                "already owns",
                inventory_core.claim_automatic_name("codex", uuid, environ=env),
            )

    def test_a_thread_the_kit_never_named_keeps_its_ordinary_precedence(self) -> None:
        """No kit value means no rename to detect: a seeded prompt is not one."""
        uuid = uuid_for(313)
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            base.chmod(0o700)
            home, config, env = self._sandbox(base)
            self._seed(config, {"schema_version": 1, "aliases": {}})
            self.assertEqual(
                "",
                inventory_core.adopt_native_rename(
                    "codex", uuid, "trace the cdn purge failure", environ=env
                ),
            )
            self.assertEqual({}, self._document(config).get("name_ownership", {}))


class AccountProfileReadersTests(unittest.TestCase):
    """A session on an enrolled account keeps its transcript in that
    account's profile, not under ``~/.claude``. Three readers answered this
    question and only one of them had been widened, so the window's own name
    and every queued title retry stayed blind to account sessions."""

    def profile(self, home: Path, alias: str) -> Path:
        profile = (
            home / ".local" / "share" / "session-kit" / "accounts" / "claude" / alias
        )
        (profile / "projects" / "-srv-project").mkdir(parents=True)
        return profile

    def test_a_bounce_is_requested_for_a_session_on_an_account(self) -> None:
        """"" with clear=False means "defer, a name may still arrive", so the
        picker never asked for the one safe provider bounce and the window
        title stayed stale until the person typed something. That is the
        second half of "it didn't rename the session"."""
        import datetime as dt

        uuid = uuid_for(120)
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            sessions = home / ".claude" / "sessions"
            sessions.mkdir(parents=True)
            (home / ".claude" / "projects").mkdir(parents=True)
            intent = sessions / f"{uuid}.nameintent"
            intent.write_text("Count The Great Lakes\n", encoding="utf-8")
            stamp = (
                dt.datetime.fromtimestamp(
                    intent.stat().st_mtime - 120, dt.timezone.utc
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
            profile = self.profile(home, "fixture")
            (profile / "projects" / "-srv-project" / f"{uuid}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "who plays tonight"},
                        "timestamp": stamp,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                ("Count The Great Lakes", False),
                inventory_core.claude_bounce_prepare(uuid, home=home),
            )

    def test_a_title_retry_is_judged_for_a_session_on_an_account(self) -> None:
        """Every queued retry answered "defer" on every pass for an account
        session, so a title push that failed once was never retried and its
        entry never left the ledger."""
        names = sys.modules["sessionkit_inventory.names"]
        uuid = uuid_for(121)
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            (home / ".claude" / "projects").mkdir(parents=True)
            profile = self.profile(home, "fixture")
            (profile / "projects" / "-srv-project" / f"{uuid}.jsonl").write_text(
                json.dumps({"type": "user", "sessionId": uuid}) + "\n",
                encoding="utf-8",
            )

            verdict = names._provider_title_retry_disposition(
                "claude",
                uuid,
                "Some Title",
                {"HOME": os.fspath(home)},
                codex_home=lambda *a, **k: home / ".codex",
                codex_title_echoes_prompt=lambda *a, **k: False,
                home_resolver=lambda: home,
                transcript_signals=inventory_core.read_claude_transcript_signals,
                read_session_index=lambda *a, **k: {},
            )

            self.assertEqual("retry", verdict)


class TwoProfilesHoldOneConversationTests(unittest.TestCase):
    """An account switch leaves the same conversation id in two profiles.

    Which one describes the session has to be decided by evidence. Sorting
    the roots by alias and letting "the last record win" handed the answer to
    whichever profile sorted last, so a stale copy could out-rank the file the
    live session is writing -- and an unrecognised name from a stale copy is
    adopted as a person's rename, which wins for good.
    """

    def profile(self, home: Path, alias: str) -> Path:
        profile = (
            home / ".local" / "share" / "session-kit" / "accounts" / "claude" / alias
        )
        (profile / "projects" / "-srv-project").mkdir(parents=True)
        return profile

    def write(self, path: Path, uuid: str, title: str, color: str) -> None:
        path.write_text(
            json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": uuid})
            + "\n"
            + json.dumps(
                {"type": "agent-color", "agentColor": color, "sessionId": uuid}
            )
            + "\n",
            encoding="utf-8",
        )

    def test_the_newest_copy_wins_not_the_alphabetically_last(self) -> None:
        uuid = uuid_for(122)
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            (home / ".claude" / "projects").mkdir(parents=True)
            live = (
                self.profile(home, "aaa-live")
                / "projects"
                / "-srv-project"
                / f"{uuid}.jsonl"
            )
            stale = (
                self.profile(home, "zzz-stale")
                / "projects"
                / "-srv-project"
                / f"{uuid}.jsonl"
            )
            self.write(live, uuid, "The live title", "cyan")
            self.write(stale, uuid, "A stale copy", "red")
            old = time.time() - 86_400
            os.utime(stale, (old, old))

            signals = inventory_core.read_claude_transcript_signals(uuid, home)

            self.assertEqual("The live title", signals["ai_title"])
            self.assertEqual("cyan", signals["agent_color"])

    def test_a_fifth_profile_cannot_hide_the_current_one(self) -> None:
        """The read stops after a few files. Taken in alias order that cut-off
        could exclude the profile actually in use; taken newest-first it
        cannot."""
        uuid = uuid_for(123)
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            (home / ".claude" / "projects").mkdir(parents=True)
            old = time.time() - 86_400
            for alias in ("aaa", "bbb", "ccc", "ddd"):
                path = (
                    self.profile(home, alias)
                    / "projects"
                    / "-srv-project"
                    / f"{uuid}.jsonl"
                )
                path.write_text(
                    json.dumps({"type": "user", "sessionId": uuid}) + "\n",
                    encoding="utf-8",
                )
                os.utime(path, (old, old))
            current = (
                self.profile(home, "zzz-current")
                / "projects"
                / "-srv-project"
                / f"{uuid}.jsonl"
            )
            self.write(current, uuid, "The fifth is current", "green")

            signals = inventory_core.read_claude_transcript_signals(uuid, home)

            self.assertEqual("The fifth is current", signals["ai_title"])


if __name__ == "__main__":
    unittest.main()
