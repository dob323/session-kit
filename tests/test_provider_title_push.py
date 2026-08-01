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

    def test_caps_length_at_64(self) -> None:
        title = inventory_core.derive_prompt_title(
            "investigate " + "extraordinarily " * 8 + "long prompt"
        )
        assert title is not None
        self.assertLessEqual(len(title), 64)


class AutoTitleFromHookTests(unittest.TestCase):
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

    def test_bounce_heals_an_index_entry_that_echoes_the_prompt(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000043"
        with tempfile.TemporaryDirectory() as base:
            codex = self._bounce_codex_root(
                Path(base),
                uuid,
                "Session Naming Timing",
                "who was the first person on the moon?",
            )
            index = codex / "session_index.jsonl"
            index.write_text(
                json.dumps(
                    {
                        "id": uuid,
                        "thread_name": "who was the first person on the moon?",
                        "updated_at": "2026-07-31T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                "Session Naming Timing",
                inventory_core.codex_bounce_prepare(uuid, codex),
            )
            last = json.loads(
                index.read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual("Session Naming Timing", last["thread_name"])

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
                json.dumps({"id": uuid, "thread_name": "Renamed By Dan"})
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                "Renamed By Dan",
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


if __name__ == "__main__":
    unittest.main()
