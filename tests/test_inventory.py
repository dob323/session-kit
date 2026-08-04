from __future__ import annotations

import argparse
import copy
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import unicodedata

from tests.support import REPO, run


CORE_PATH = REPO / "lib" / "session_inventory.py"
CORE_SPEC = importlib.util.spec_from_file_location("session_inventory", CORE_PATH)
assert CORE_SPEC is not None and CORE_SPEC.loader is not None
inventory_core = importlib.util.module_from_spec(CORE_SPEC)
sys.modules[CORE_SPEC.name] = inventory_core
CORE_SPEC.loader.exec_module(inventory_core)
from sessionkit_inventory import lifecycle as lifecycle_state  # noqa: E402


def uuid_for(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def process(
    pid: int,
    ppid: int,
    comm: str,
    *,
    session_name: str = "",
    cwd: str = "/srv/project",
    cmdline: list[str] | None = None,
    start_ticks: int | None = None,
) -> dict:
    return {
        "pid": pid,
        "ppid": ppid,
        "comm": comm,
        "session_name": session_name,
        "cwd": cwd,
        "cmdline": cmdline if cmdline is not None else [f"/usr/bin/{comm}"],
        "start_ticks": start_ticks if start_ticks is not None else pid * 10,
    }


def inventory_fixture(
    count: int,
    *,
    providers: tuple[str, ...] = ("claude", "codex"),
) -> tuple[dict, list[dict], dict[int, dict], dict[int, list[dict]], tuple[dict, dict], dict]:
    shpool = {"sessions": []}
    claude: list[dict] = []
    table = {
        10: process(10, 1, "shpool", cmdline=["/usr/bin/shpool", "daemon"])
    }
    codex_index: dict[int, list[dict]] = {}
    threads: dict[str, dict] = {}
    for index in range(1, count + 1):
        name = f"main{index}" if index > 1 else "main"
        root_pid = 1000 + index
        agent_pid = 2000 + index
        provider = providers[(index - 1) % len(providers)]
        exact_uuid = uuid_for(index)
        cwd = f"/srv/project-{index}"
        shpool["sessions"].append(
            {
                "name": name,
                "status": "Disconnected" if index % 2 else "Attached",
                "started_at_unix_ms": 1_700_000_000_000 + index,
            }
        )
        table[root_pid] = process(
            root_pid, 10, "bash", session_name=name, cwd=cwd
        )
        table[agent_pid] = process(
            agent_pid,
            root_pid,
            provider,
            cwd=cwd,
            cmdline=[f"/usr/bin/{provider}"],
        )
        if provider == "claude":
            claude.append(
                {
                    "pid": agent_pid,
                    "sessionId": exact_uuid,
                    "cwd": cwd,
                    "kind": "interactive",
                    "name": f"Claude task {index}",
                    "status": "busy",
                    "startedAt": 1_700_000_000_000 + index,
                }
            )
        else:
            codex_index[agent_pid] = [
                {
                    "source": "cli",
                    "id": exact_uuid,
                    "session_id": exact_uuid,
                }
            ]
            threads[exact_uuid] = {
                "id": exact_uuid,
                "title": f"Codex task {index}",
                "cwd": cwd,
            }
    config = {
        "aliases": {},
        "max_proc_nodes": 8192,
        "max_proc_depth": 32,
    }
    return shpool, claude, table, codex_index, (threads, {}), config


class InventoryScaleTests(unittest.TestCase):
    def test_zero_one_and_100_plus_counts_have_no_ceiling(self) -> None:
        for count in (0, 1, 9, 10, 99, 125, 250):
            with self.subTest(count=count):
                fixture = inventory_fixture(count)
                result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
                self.assertEqual(count, len(result["sessions"]))
                self.assertEqual(
                    list(range(1, count + 1)),
                    sorted(row["row"] for row in result["sessions"]),
                )
                self.assertEqual(
                    count,
                    len({row["shpool_id"] for row in result["sessions"]}),
                )
                with mock.patch.dict(
                    os.environ,
                    {"COLUMNS": "60", "SESSION_KIT_NO_COLOR": "1"},
                    clear=False,
                ):
                    rendered = inventory_core.render_inventory(
                        result, rows_only=True
                    )
                session_word = "session" if count == 1 else "sessions"
                self.assertTrue(
                    rendered.startswith(f"  {count} {session_word}:"),
                    rendered[:100],
                )
                if count in (0, 1, 125):
                    self.assertTrue(
                        all(
                            inventory_core._display_width(line) <= 59
                            for line in rendered.splitlines()
                        ),
                        rendered,
                    )

    def test_action_provider_activity_sort_and_rows_are_deterministic(self) -> None:
        fixture = list(inventory_fixture(12, providers=("claude",)))
        for item in fixture[0]["sessions"]:
            item["status"] = "Disconnected"
        fixture[1][9]["status"] = "waiting"
        fixture[1][9]["waitingFor"] = "reply"
        recent = {
            "main2": 2_000,
            "main3": 3_000,
            "main4": 3_000,
            "main10": 1_000,
        }
        first = inventory_core.build_inventory(
            *fixture,
            now=1_800_000_000,
            recent_output_by_shpool_id=recent,
        )
        fixture[0] = {"sessions": list(reversed(fixture[0]["sessions"]))}
        fixture[1] = list(reversed(fixture[1]))
        second = inventory_core.build_inventory(
            *fixture,
            now=1_800_000_000,
            recent_output_by_shpool_id=dict(reversed(tuple(recent.items()))),
        )
        expected = [
            "main10",
            "main3",
            "main4",
            "main2",
            "main",
            "main5",
            "main6",
            "main7",
            "main8",
            "main9",
            "main11",
            "main12",
        ]
        self.assertEqual(expected, [item["shpool_id"] for item in first["sessions"]])
        self.assertEqual(expected, [item["shpool_id"] for item in second["sessions"]])
        first_rows = {item["shpool_id"]: item["row"] for item in first["sessions"]}
        second_rows = {item["shpool_id"]: item["row"] for item in second["sessions"]}
        self.assertEqual(first_rows, second_rows)
        self.assertEqual(list(range(1, 13)), [item["row"] for item in first["sessions"]])
        self.assertTrue(first["sessions"][0]["needs_you"])
        self.assertEqual(3_000, first["sessions"][1]["recent_output_at_unix_ms"])
        self.assertEqual(3_000, first["sessions"][2]["recent_output_at_unix_ms"])

    def test_availability_then_provider_precede_activity(self) -> None:
        fixture = list(inventory_fixture(4, providers=("codex", "claude")))
        fixture[0]["sessions"][1]["status"] = "Disconnected"
        fixture[0]["sessions"][3]["status"] = "Disconnected"
        result = inventory_core.build_inventory(
            *fixture,
            now=1_800_000_000,
            recent_output_by_shpool_id={
                "main": 4_000,
                "main2": 1_000,
                "main3": 3_000,
                "main4": 2_000,
            },
        )
        self.assertEqual(
            ["claude", "claude", "codex", "codex"],
            [item["provider"] for item in result["sessions"]],
        )
        self.assertEqual(
            ["main4", "main2", "main", "main3"],
            [item["shpool_id"] for item in result["sessions"]],
        )

    def test_recent_output_uses_exact_map_legacy_and_segmented_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".recent-output-", dir=REPO) as raw:
            base = Path(raw)
            journals = base / "journals"
            recovery = base / "recovery"
            journals.mkdir()
            recovery.mkdir()

            legacy = journals / "main.raw"
            legacy.write_text("legacy", encoding="utf-8")
            os.utime(legacy, ns=(1_000_000_000, 1_000_000_000))

            segment_dir = journals / "main2"
            segment_dir.mkdir()
            first_segment = segment_dir / "segment-000001.raw"
            latest_segment = segment_dir / "segment-000002.raw"
            ignored = segment_dir / "notes.txt"
            for path in (first_segment, latest_segment, ignored):
                path.write_text(path.name, encoding="utf-8")
            os.utime(first_segment, ns=(2_000_000_000, 2_000_000_000))
            os.utime(latest_segment, ns=(3_000_000_000, 3_000_000_000))
            os.utime(ignored, ns=(9_000_000_000, 9_000_000_000))

            active_but_mapped = journals / "main3.raw"
            active_but_mapped.write_text("active", encoding="utf-8")
            os.utime(active_but_mapped, ns=(8_000_000_000, 8_000_000_000))
            recovered = recovery / "main3-recovered.raw"
            recovered.write_text("mapped", encoding="utf-8")
            os.utime(recovered, ns=(4_000_000_000, 4_000_000_000))
            (recovery / "current-map.tsv").write_text(
                f"main3\t{recovered}\n"
                f"main4\t{recovered}\n"
                f"main4\t{active_but_mapped}\n",
                encoding="utf-8",
            )

            result = inventory_core.recent_output_times(
                ("main", "main2", "main3", "main4", "custom-session"),
                journal_dir=journals,
                recovery_dir=recovery,
            )
            self.assertEqual(
                {"main": 1_000, "main2": 3_000, "main3": 4_000},
                result,
            )


class InventoryIdentityTests(unittest.TestCase):
    def test_default_proc_bound_covers_peak_and_remains_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".config-bound-", dir=REPO) as raw:
            missing = Path(raw) / "missing.json"
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(missing)},
                clear=False,
            ):
                default = inventory_core.load_config()
            self.assertEqual(16384, default["max_proc_nodes"])
            missing.write_text(
                json.dumps({"schema_version": 1, "max_proc_nodes": 100001}),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(missing)},
                clear=False,
            ):
                invalid = inventory_core.load_config()
            self.assertEqual(16384, invalid["max_proc_nodes"])
        partial_fixture = list(inventory_fixture(1))
        partial_fixture[5] = {"aliases": {}, "max_proc_depth": 32}
        with mock.patch.object(
            inventory_core,
            "descendants",
            wraps=inventory_core.descendants,
        ) as descend:
            inventory_core.build_inventory(
                *partial_fixture, now=1_800_000_000
            )
        self.assertEqual(
            inventory_core.DEFAULT_MAX_PROC_NODES,
            descend.call_args.kwargs["max_nodes"],
        )

        class ScanReached(RuntimeError):
            pass

        with (
            mock.patch.object(
                inventory_core,
                "_command_json",
                return_value={"sessions": []},
            ),
            mock.patch.object(
                inventory_core,
                "scan_process_table",
                side_effect=ScanReached,
            ) as scan,
        ):
            with self.assertRaises(ScanReached):
                inventory_core.collect_live({})
        self.assertEqual(
            inventory_core.DEFAULT_MAX_PROC_NODES,
            scan.call_args.args[1],
        )

    def test_duplicate_cwd_does_not_conflate_claude_and_codex(self) -> None:
        fixture = list(inventory_fixture(2))
        shared = "/srv/shared"
        fixture[1][0]["cwd"] = shared
        fixture[2][2001]["cwd"] = shared
        fixture[2][1001]["cwd"] = shared
        fixture[2][2002]["cwd"] = shared
        fixture[2][1002]["cwd"] = shared
        fixture[4][0][uuid_for(2)]["cwd"] = shared
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        by_provider = {row["provider"]: row for row in result["sessions"]}
        self.assertEqual(uuid_for(1), by_provider["claude"]["identity"]["uuid"])
        self.assertEqual(uuid_for(2), by_provider["codex"]["identity"]["uuid"])
        self.assertEqual(
            ["claude", "codex"], sorted(row["provider"] for row in result["sessions"])
        )

    def test_subagents_are_nested_and_not_promoted_to_sessions(self) -> None:
        fixture = list(inventory_fixture(1, providers=("claude",)))
        parent_uuid = uuid_for(1)
        fixture[2][3001] = process(
            3001,
            2001,
            "claude",
            cmdline=[
                "/usr/bin/claude",
                "--parent-session-id",
                parent_uuid,
                "--agent-name",
                "Verifier",
            ],
        )
        fixture[1].append(
            {
                "pid": 3001,
                "sessionId": uuid_for(9001),
                "cwd": "/srv/project-1",
                "kind": "subagent",
                "name": "Verifier",
                "status": "busy",
            }
        )
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        self.assertEqual(1, len(result["sessions"]))
        self.assertEqual("claude", result["sessions"][0]["provider"])
        self.assertEqual(parent_uuid, result["sessions"][0]["identity"]["uuid"])
        self.assertEqual(["Verifier"], [x["title"] for x in result["sessions"][0]["subagents"]])
        self.assertEqual([], result["outside_agents"])

    def test_provider_roots_outside_shpool_are_visible_separately(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        outside_pid = 9000
        outside_uuid = uuid_for(9000)
        fixture[2][outside_pid] = process(outside_pid, 1, "claude", cwd="/srv/outside")
        fixture[1].append(
            {
                "pid": outside_pid,
                "sessionId": outside_uuid,
                "cwd": "/srv/outside",
                "kind": "interactive",
                "name": "Outside task",
                "status": "waiting",
                "waitingFor": "user",
            }
        )
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        self.assertEqual(1, len(result["sessions"]))
        self.assertEqual(1, len(result["outside_agents"]))
        self.assertEqual(outside_uuid, result["outside_agents"][0]["identity"]["uuid"])
        self.assertTrue(result["outside_agents"][0]["needs_you"])

    def test_stale_claude_agent_without_live_process_is_not_outside(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        fixture[1].append(
            {
                "pid": 9000,
                "sessionId": uuid_for(9000),
                "cwd": "/srv/exiting",
                "kind": "interactive",
                "name": "Exiting task",
                "status": "idle",
            }
        )

        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)

        self.assertEqual([], result["outside_agents"])
        self.assertTrue(inventory_core.guard_live_inventory(result))

    def test_pid_loss_never_carries_an_old_identity_forward(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        exact = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        self.assertEqual(uuid_for(1), exact["sessions"][0]["identity"]["uuid"])
        fixture[3] = {}
        lost = inventory_core.build_inventory(*fixture, now=1_800_000_001)
        self.assertIsNone(lost["sessions"][0]["identity"]["uuid"])
        self.assertFalse(lost["sessions"][0]["recovery"]["available"])

    def test_pid_reuse_changes_exact_identity_and_start_ticks(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        first = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        new_uuid = uuid_for(777)
        fixture[2][2001]["start_ticks"] += 50_000
        fixture[3][2001] = [
            {"source": "cli", "id": new_uuid, "session_id": new_uuid}
        ]
        fixture[4] = ({new_uuid: {"id": new_uuid, "title": "Replacement", "cwd": "/srv/project-1"}}, {})
        second = inventory_core.build_inventory(*fixture, now=1_800_000_001)
        self.assertNotEqual(
            first["sessions"][0]["identity"]["process_start_ticks"],
            second["sessions"][0]["identity"]["process_start_ticks"],
        )
        self.assertEqual(new_uuid, second["sessions"][0]["identity"]["uuid"])

    def test_aliases_are_bound_to_provider_and_uuid_not_cwd_or_pid(self) -> None:
        fixture = list(inventory_fixture(2))
        fixture[5]["aliases"] = {
            f"claude:{uuid_for(1)}": "Lyrics audit",
            f"codex:{uuid_for(2)}": "Deploy review",
        }
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        titles = {(row["provider"], row["identity"]["uuid"]): row["title"] for row in result["sessions"]}
        self.assertEqual("Lyrics audit", titles[("claude", uuid_for(1))])
        self.assertEqual("Deploy review", titles[("codex", uuid_for(2))])

    def test_codex_session_index_latest_exact_name_precedes_database_title(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".codex-index-", dir=REPO) as raw:
            base = Path(raw)
            db = base / "state_5.sqlite"
            exact = uuid_for(2)
            connection = sqlite3.connect(db)
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, cwd TEXT)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?)",
                (exact, "Database title", "/srv/project-2"),
            )
            connection.commit()
            connection.close()
            index = base / "session_index.jsonl"
            index.write_text(
                "\n".join(
                    (
                        "{broken",
                        json.dumps({"id": exact, "thread_name": ""}),
                        json.dumps({"id": exact, "thread_name": "Earlier"}),
                        json.dumps({"id": exact, "thread_name": "Exact menu name"}),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            index.chmod(0o600)
            warnings: list[str] = []
            names = inventory_core.read_codex_session_index(index, warnings)
            threads, _ = inventory_core.read_codex_db(db, names)
        self.assertEqual("Exact menu name", names[exact])
        self.assertEqual("Exact menu name", threads[exact]["session_index_name"])
        self.assertEqual(1, len(warnings))
        fixture = list(inventory_fixture(2))
        fixture[5]["automatic_titles"] = {
            f"codex:{exact}": "Automatic Menu Name"
        }
        fixture[4] = (threads, {})
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        codex = next(row for row in result["sessions"] if row["provider"] == "codex")
        self.assertEqual("Exact menu name", codex["native_title"])
        self.assertEqual("native", codex["title_source"])

    def test_completed_codex_child_is_retained_but_not_active(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".codex-child-", dir=REPO) as raw:
            codex_home = Path(raw)
            db = codex_home / "state_5.sqlite"
            parent = uuid_for(1)
            child = uuid_for(2)
            rollout = codex_home / "sessions" / "rollout-child.jsonl"
            rollout.parent.mkdir()
            rollout.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"type": "task_started"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"type": "task_complete"},
                            }
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(db)
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, cwd TEXT, "
                "rollout_path TEXT, agent_nickname TEXT)"
            )
            connection.execute(
                "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, "
                "child_thread_id TEXT, status TEXT)"
            )
            connection.executemany(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                (
                    (parent, "Parent", "/srv/project-1", "", None),
                    (child, "Child", "/srv/project-1", str(rollout), "Verifier"),
                ),
            )
            connection.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
                (parent, child, "open"),
            )
            connection.commit()
            connection.close()

            threads, edges = inventory_core.read_codex_db(db)

        self.assertEqual("idle", edges[parent][0]["status"])
        fixture = list(inventory_fixture(1, providers=("codex",)))
        fixture[4] = (threads, edges)
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        row = result["sessions"][0]
        self.assertEqual(1, len(row["subagents"]))
        self.assertEqual(0, row["active_subagent_count"])

    def test_codex_database_prompt_never_outranks_automatic_or_context_title(
        self,
    ) -> None:
        fixture = list(inventory_fixture(2))
        exact = uuid_for(2)
        raw_prompt = (
            "i need you to open research on the session kit project and make "
            "several important changes " * 8
        )
        fixture[4] = (
            {
                exact: {
                    "id": exact,
                    "title": raw_prompt,
                    "cwd": "/srv/example-project",
                    "session_index_name": "",
                }
            },
            {},
        )
        fixture[5]["automatic_titles"] = {
            f"codex:{exact}": "Session Kit Updates"
        }
        automatic = inventory_core.build_inventory(
            *fixture, now=1_800_000_000
        )
        codex = next(
            row for row in automatic["sessions"] if row["provider"] == "codex"
        )
        self.assertEqual("Session Kit Updates", codex["title"])
        self.assertEqual("automatic", codex["title_source"])
        self.assertEqual("ready", codex["automatic_name_state"])
        fixture[5]["automatic_titles"] = {}
        pending = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        codex = next(
            row for row in pending["sessions"] if row["provider"] == "codex"
        )
        self.assertEqual("context", codex["title_source"])
        self.assertNotIn("i need you", codex["title"].casefold())
        self.assertEqual("pending", codex["automatic_name_state"])

    def test_codex_index_titles_survive_missing_or_corrupt_database(self) -> None:
        fixture = list(inventory_fixture(2))
        codex_uuid = uuid_for(2)
        for database_state in ("missing", "corrupt"):
            with self.subTest(database_state=database_state):
                with tempfile.TemporaryDirectory(
                    prefix=".codex-fallback-", dir=REPO
                ) as raw:
                    codex_home = Path(raw)
                    index = codex_home / "session_index.jsonl"
                    index.write_text(
                        json.dumps(
                            {
                                "id": codex_uuid,
                                "thread_name": "Indexed exact title",
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    index.chmod(0o600)
                    db = codex_home / "state_5.sqlite"
                    if database_state == "corrupt":
                        db.write_bytes(b"not sqlite")
                    names = inventory_core.read_codex_session_index(index)
                    if database_state == "missing":
                        threads, _ = inventory_core.read_codex_db(db, names)
                        self.assertEqual(
                            "Indexed exact title",
                            threads[codex_uuid]["session_index_name"],
                        )
                    with (
                        mock.patch.object(
                            inventory_core,
                            "_command_json",
                            side_effect=(fixture[0], fixture[1]),
                        ),
                        mock.patch.object(
                            inventory_core,
                            "scan_process_table",
                            return_value=fixture[2],
                        ),
                        mock.patch.object(
                            inventory_core,
                            "_codex_paths",
                            return_value=(codex_home, db),
                        ),
                        mock.patch.object(
                            inventory_core,
                            "index_codex_processes",
                            return_value=fixture[3],
                        ),
                        mock.patch.object(
                            inventory_core,
                            "recent_output_times",
                            return_value={},
                        ),
                    ):
                        collected = inventory_core.collect_live(fixture[5])
                    codex = next(
                        row
                        for row in collected["sessions"]
                        if row["provider"] == "codex"
                    )
                    self.assertEqual("Indexed exact title", codex["native_title"])
                    self.assertFalse(collected["_complete"])
                    self.assertTrue(
                        any(
                            "Codex metadata unavailable" in warning
                            for warning in collected["warnings"]
                        )
                    )

    def test_title_precedence_and_context_fallback_are_explicit(self) -> None:
        exact = uuid_for(70)
        timestamp = 1_785_220_000_000
        alias = {f"codex:{exact}": "Manual name"}
        self.assertEqual(
            ("Manual name", "alias"),
            inventory_core._provider_title_info(
                "codex", exact, "Native", alias, "/srv/example-project", timestamp
            ),
        )
        self.assertEqual(
            ("Native", "native"),
            inventory_core._provider_title_info(
                "codex", exact, "Native", {}, "/srv/example-project", timestamp
            ),
        )
        self.assertEqual(
            ("Automatic Name", "automatic"),
            inventory_core._provider_title_info(
                "codex",
                exact,
                "Raw prompt",
                {},
                "/srv/example-project",
                timestamp,
                {f"codex:{exact}": "Automatic Name"},
                provider_title_is_explicit=False,
            ),
        )
        title, source = inventory_core._provider_title_info(
            "codex", exact, "", {}, "/srv/example-project", timestamp
        )
        self.assertEqual("context", source)
        self.assertIn("Codex in example-project at ", title)
        self.assertNotIn("/srv/", title)
        self.assertNotIn(exact[:8], title)

    def test_operational_id_policy_preserves_raw_ids_and_bounds_display_only(self) -> None:
        safe = (
            "main",
            "main10",
            "s20260728-012345-4321",
            "s20260728-012345-4321-7",
        )
        for raw in safe:
            with self.subTest(raw=raw):
                self.assertEqual((True, None), inventory_core.shpool_id_mutation_policy(raw))

        rejected = {
            "main-template": "template",
            "control": "unmanaged",
            "custom-session": "unmanaged",
            "main\ncontrol": "control",
            "é" * 65: "oversize",
        }
        for raw, reason in rejected.items():
            with self.subTest(raw=repr(raw)):
                self.assertEqual(
                    (False, reason), inventory_core.shpool_id_mutation_policy(raw)
                )
                shpool = {
                    "sessions": [
                        {
                            "name": raw,
                            "status": "Disconnected",
                            "started_at_unix_ms": 1_700_000_000_000,
                        }
                    ]
                }
                table = {
                    10: process(
                        10,
                        1,
                        "shpool",
                        cmdline=["/usr/bin/shpool", "daemon"],
                    ),
                    100: process(100, 10, "bash", session_name=raw),
                }
                result = inventory_core.build_inventory(
                    shpool,
                    [],
                    table,
                    {},
                    ({}, {}),
                    {"aliases": {}, "max_proc_nodes": 8192, "max_proc_depth": 32},
                    now=1_800_000_000,
                )
                row = result["sessions"][0]
                self.assertEqual(raw, row["shpool_id"])
                self.assertEqual(raw, row["shpool_id_raw"])
                self.assertEqual(raw, json.loads(json.dumps(row))["shpool_id_raw"])
                self.assertFalse(row["mutation_allowed"])
                self.assertEqual(reason, row["mutation_rejection_reason"])
                self.assertLessEqual(len(row["display_shpool_id"]), 32)
                self.assertFalse(
                    any(
                        unicodedata.category(character).startswith("C")
                        for character in row["display_shpool_id"]
                    )
                )

    def test_process_scan_discards_pid_when_pre_and_post_stat_identity_differs(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".proc-race-", dir=REPO) as raw:
            proc_root = Path(raw)
            entry = proc_root / "123"
            entry.mkdir()
            (entry / "stat").write_text("fixture\n", encoding="utf-8")
            (entry / "cmdline").write_bytes(b"codex\0")
            (entry / "comm").write_text("codex\n", encoding="utf-8")
            (entry / "environ").write_bytes(b"")
            with mock.patch.object(
                inventory_core,
                "_proc_stat",
                side_effect=[(123, 10, 1000), (123, 10, 2000)],
            ):
                scanned = inventory_core.scan_process_table(proc_root, 64)
            self.assertEqual({}, scanned)

    def test_codex_rollout_read_discards_metadata_on_pid_identity_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".codex-race-", dir=REPO) as raw:
            base = Path(raw)
            proc_root = base / "proc"
            codex_home = base / "codex-home"
            rollout = codex_home / "sessions/rollout-fixture.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "source": "cli",
                            "id": uuid_for(1),
                            "session_id": uuid_for(1),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            pid_dir = proc_root / "321"
            (pid_dir / "fd").mkdir(parents=True)
            (pid_dir / "stat").write_text("fixture\n", encoding="utf-8")
            os.symlink(rollout, pid_dir / "fd/7")
            expected_process = {"pid": 321, "ppid": 10, "start_ticks": 1000}
            stable = (321, 10, 1000)
            reused = (321, 10, 2000)
            with mock.patch.object(
                inventory_core,
                "_expected_proc_identity",
                side_effect=[stable, stable, reused],
            ):
                metadata = inventory_core.codex_open_rollouts(
                    321, proc_root, codex_home, expected_process
                )
            self.assertEqual([], metadata)

    def test_codex_metadata_is_read_through_proc_fd_not_reopened_pathname(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".codex-fd-", dir=REPO) as raw:
            base = Path(raw)
            proc_root = base / "proc"
            codex_home = base / "codex-home"
            rollout = codex_home / "sessions/rollout-direct-fd.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "source": "cli",
                            "id": uuid_for(55),
                            "session_id": uuid_for(55),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            pid_dir = proc_root / "654"
            (pid_dir / "fd").mkdir(parents=True)
            (pid_dir / "stat").write_text("fixture\n", encoding="utf-8")
            proc_descriptor = pid_dir / "fd/7"
            os.symlink(rollout, proc_descriptor)
            expected_process = {"pid": 654, "ppid": 10, "start_ticks": 1000}
            stable = (654, 10, 1000)
            real_open = os.open
            real_fstat = os.fstat
            opened_paths: list[Path] = []

            def open_spy(path: object, flags: int, *args: object, **kwargs: object) -> int:
                candidate = Path(os.fspath(path))
                opened_paths.append(candidate)
                if candidate == rollout:
                    raise AssertionError("rollout pathname must never be reopened")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                inventory_core,
                "_expected_proc_identity",
                return_value=stable,
            ), mock.patch.object(
                inventory_core.os,
                "open",
                side_effect=open_spy,
            ), mock.patch.object(
                inventory_core.os,
                "fstat",
                wraps=real_fstat,
            ) as fstat_spy:
                metadata = inventory_core.codex_open_rollouts(
                    654, proc_root, codex_home, expected_process
                )
            self.assertEqual(uuid_for(55), metadata[0]["session_id"])
            self.assertEqual([proc_descriptor], opened_paths)
            self.assertGreaterEqual(fstat_spy.call_count, 2)

    def test_app_server_open_rollout_is_exact_managed_codex_identity(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        exact = uuid_for(1)
        app_pid = 2001
        fixture[2][app_pid]["cmdline"] = [
            "/usr/bin/codex",
            "-c",
            "check_for_update_on_startup=false",
            "app-server",
            "--listen",
            "unix:///run/user/1000/session-kit/app.sock",
        ]
        fixture[3][app_pid] = [
            {
                "source": "vscode",
                "originator": "example_coordination_broker",
                "id": exact,
                "session_id": exact,
                "_turn_state": "idle",
            }
        ]
        fixture[4][0][exact]["title"] = "How deep is deepest ocean"
        fixture[4][0][exact]["first_user_message"] = "how deep is deepest ocean"
        fixture[4][0][exact]["session_index_name"] = "How deep is deepest ocean"
        fixture[5]["colors"] = {f"codex:{exact}": "green"}

        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)

        row = result["sessions"][0]
        self.assertEqual("codex", row["provider"])
        self.assertEqual(exact, row["identity"]["uuid"])
        self.assertEqual(app_pid, row["identity"]["pid"])
        self.assertEqual(
            "native Codex PID open exact rollout",
            row["identity"]["provenance"],
        )
        self.assertEqual("How deep is deepest ocean", row["title"])
        self.assertEqual("green", row["display_color"])
        self.assertEqual("idle", row["agent_status"])

    def test_editor_rollout_requires_app_server_and_one_exact_thread(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        exact = uuid_for(1)
        app_pid = 2001
        editor_meta = {
            "source": "vscode",
            "id": exact,
            "session_id": exact,
        }
        fixture[3][app_pid] = [editor_meta]

        direct = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        self.assertEqual("unknown", direct["sessions"][0]["provider"])
        self.assertIsNone(direct["sessions"][0]["identity"]["uuid"])

        fixture[2][app_pid]["cmdline"] = [
            "/usr/bin/codex",
            "app-server",
            "--listen",
            "unix:///run/user/1000/session-kit/app.sock",
        ]
        second = uuid_for(2)
        fixture[3][app_pid].append(
            {"source": "vscode", "id": second, "session_id": second}
        )
        ambiguous = inventory_core.build_inventory(
            *fixture, now=1_800_000_000
        )
        self.assertEqual("unknown", ambiguous["sessions"][0]["provider"])
        self.assertIsNone(ambiguous["sessions"][0]["identity"]["uuid"])


class CodexTurnStateTests(unittest.TestCase):
    @staticmethod
    def _event(event_type: str, payload: dict) -> dict:
        return {"type": event_type, "payload": payload}

    def _state(self, events: list[dict]) -> str:
        with tempfile.TemporaryFile() as rollout:
            for event in events:
                rollout.write(json.dumps(event).encode("utf-8") + b"\n")
            rollout.flush()
            return inventory_core._rollout_turn_state(rollout.fileno())

    def test_pending_blocking_question_needs_reply_until_exact_output(self) -> None:
        started = self._event("event_msg", {"type": "task_started"})
        question = self._event(
            "response_item",
            {
                "type": "function_call",
                "name": "request_user_input",
                "call_id": "call-question",
                "arguments": json.dumps(
                    {"questions": [{"question": "task_complete is prose only"}]}
                ),
            },
        )
        wrong_output = self._event(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call-unrelated",
                "output": "ignored",
            },
        )
        exact_output = self._event(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call-question",
                "output": "answer text is not inspected",
            },
        )

        self.assertEqual(
            "needs your reply", self._state([started, question, wrong_output])
        )
        self.assertEqual(
            "working",
            self._state([started, question, wrong_output, exact_output]),
        )
        self.assertEqual(
            "working",
            self._state([question, exact_output]),
        )

    def test_new_structured_turn_retires_unanswered_prior_question(self) -> None:
        question = self._event(
            "response_item",
            {
                "type": "function_call",
                "name": "request_user_input",
                "call_id": "call-prior-turn",
                "arguments": json.dumps(
                    {"questions": [{"question": "Prior turn question"}]}
                ),
            },
        )
        new_turn = self._event(
            "event_msg",
            {"type": "task_started", "turn_id": "new-turn"},
        )
        self.assertEqual("working", self._state([question, new_turn]))

        completed = self._event("event_msg", {"type": "task_complete"})
        self.assertEqual("idle", self._state([question, completed]))

        aborted = self._event("event_msg", {"type": "turn_aborted"})
        self.assertEqual("idle", self._state([question, aborted]))

    def test_completed_turn_ending_in_a_prose_question_is_reply_optional(
        self,
    ) -> None:
        started = self._event("event_msg", {"type": "task_started"})
        asking = self._event(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "I checked both configs.\n\nWhich hostname should I use?",
                    }
                ],
            },
        )
        completed = self._event("event_msg", {"type": "task_complete"})
        # A finished turn whose last words ask the human something is a soft
        # wait, not idle — but never the hard "needs your reply".
        self.assertEqual(
            "reply optional", self._state([started, asking, completed])
        )
        # A statement ending the turn stays idle.
        stating = self._event(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Done. Both configs match."}
                ],
            },
        )
        self.assertEqual("idle", self._state([started, stating, completed]))
        # A NEW turn clears the stale question.
        self.assertEqual(
            "working", self._state([started, asking, completed, started])
        )

    def test_timed_question_is_optional_and_never_blocking(self) -> None:
        state = self._state(
            [
                self._event("event_msg", {"type": "task_started"}),
                self._event(
                    "response_item",
                    {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "call-timed",
                        "arguments": json.dumps(
                            {
                                "autoResolutionMs": 60000,
                                "questions": [{"question": "Optional context"}],
                            }
                        ),
                    },
                ),
            ]
        )
        self.assertEqual("reply optional", state)

    def test_lifecycle_events_define_working_idle_and_aborted(self) -> None:
        cases = (
            ([self._event("event_msg", {"type": "task_started"})], "working"),
            (
                [
                    self._event("event_msg", {"type": "task_started"}),
                    self._event("event_msg", {"type": "task_complete"}),
                    self._event(
                        "response_item",
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": "prose does not create a reply alert",
                        },
                    ),
                ],
                "idle",
            ),
            (
                [
                    self._event("event_msg", {"type": "task_started"}),
                    self._event("event_msg", {"type": "turn_aborted"}),
                ],
                "idle",
            ),
        )
        for events, expected in cases:
            with self.subTest(expected=expected, final=events[-1]["payload"]["type"]):
                self.assertEqual(expected, self._state(events))

    def test_malformed_or_incomplete_structured_evidence_is_unavailable(self) -> None:
        malformed_cases = (
            [
                self._event("event_msg", {"type": "task_started"}),
                self._event(
                    "response_item",
                    {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "call-bad-json",
                        "arguments": "{",
                    },
                ),
            ],
            [
                self._event(
                    "response_item",
                    {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "call-bad-timeout",
                        "arguments": json.dumps({"autoResolutionMs": True}),
                    },
                )
            ],
            [self._event("response_item", {"type": "function_call_output"})],
        )
        for events in malformed_cases:
            with self.subTest(events=events):
                self.assertEqual("state unavailable", self._state(events))

        with tempfile.TemporaryFile() as rollout:
            rollout.write(b'{"type":"event_msg","payload":{"type":"task_started"}}')
            rollout.flush()
            self.assertEqual(
                "state unavailable",
                inventory_core._rollout_turn_state(rollout.fileno()),
            )

    def test_bounded_backward_search_finds_lifecycle_beyond_normal_tail(self) -> None:
        started = self._event("event_msg", {"type": "task_started"})
        oversized = self._event(
            "event_msg",
            {"type": "token_count", "padding": "x" * inventory_core.ROLLOUT_TAIL_BYTES},
        )
        self.assertEqual(
            "working",
            self._state([started, oversized]),
        )
        self.assertEqual(
            "working",
            self._state([oversized, started]),
        )

    def test_state_propagates_to_managed_and_outside_inventory(self) -> None:
        states = (
            ("needs your reply", True),
            ("reply optional", False),
            ("working", False),
            ("idle", False),
            ("state unavailable", False),
        )
        for state, expected_needs_you in states:
            with self.subTest(state=state):
                fixture = list(inventory_fixture(1, providers=("codex",)))
                fixture[3][2001][0]["_turn_state"] = state
                managed = inventory_core.build_inventory(
                    *fixture, now=1_800_000_000
                )
                self.assertEqual(
                    state, managed["sessions"][0]["agent_status"]
                )
                self.assertEqual(
                    expected_needs_you, managed["sessions"][0]["needs_you"]
                )

                outside = inventory_core.build_inventory(
                    {"sessions": []},
                    fixture[1],
                    fixture[2],
                    fixture[3],
                    fixture[4],
                    fixture[5],
                    now=1_800_000_000,
                )
                self.assertEqual(
                    state, outside["outside_agents"][0]["agent_status"]
                )
                self.assertEqual(
                    expected_needs_you,
                    outside["outside_agents"][0]["needs_you"],
                )

    def test_waiting_count_includes_only_blocking_reply_state(self) -> None:
        snapshot = {
            "sessions": [
                {"agent_status": "needs your reply", "needs_you": True},
                {"agent_status": "reply optional", "needs_you": False},
                {"agent_status": "working", "needs_you": False},
            ]
        }
        output = io.StringIO()
        with mock.patch.object(
            inventory_core, "load_config", return_value={}
        ), mock.patch.object(
            inventory_core, "snapshot", return_value=snapshot
        ), contextlib.redirect_stdout(output):
            return_code = inventory_core.main(["waiting-count"])
        self.assertEqual(0, return_code)
        self.assertEqual("1\n", output.getvalue())


class TerminalNumberRegistryTests(unittest.TestCase):
    @staticmethod
    def _unknown(item: dict) -> None:
        item["provider"] = "unknown"
        item["display_provider"] = "unknown"
        item["identity"] = {
            "uuid": None,
            "pid": item["shpool_shell"]["pid"],
            "process_start_ticks": item["shpool_shell"]["process_start_ticks"],
            "provenance": "process tree",
            "confidence": "unknown",
        }

    @staticmethod
    def _orphan(item: dict) -> None:
        raw_id = item["shpool_id_raw"]
        item["provider"] = "unknown"
        item["display_provider"] = "unknown"
        item["availability"] = "ready"
        item["shpool_status"] = "Disconnected"
        item["identity"] = {
            "uuid": None,
            "pid": None,
            "process_start_ticks": None,
            "provenance": "none",
            "confidence": "unknown",
        }
        item["title"] = "Unresolved provider session"
        item["display_title"] = "Unresolved provider session"
        item["native_title"] = "Unresolved provider session"
        item["title_source"] = "provider"
        item["shpool_shell"] = None
        item["recovery"] = {
            "available": False,
            "provider": None,
            "uuid": None,
            "cwd": None,
            "argv": [],
            "command": None,
        }
        item["diagnostics"] = [
            f"expected one daemon child for {raw_id!r}, found 0",
            "identity candidates: Claude=0, Codex=0",
        ]
        item["terminal_number"] = None
        item["mutation_allowed"] = True
        item["mutation_rejection_reason"] = None
        item.pop("_terminal_identity_hint", None)

    def test_shell_less_orphan_is_quarantined_without_blocking_exact_rows(
        self,
    ) -> None:
        inventory = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        exact, orphan = inventory["sessions"]
        self._orphan(orphan)
        self.assertFalse(inventory_core.strict_live_inventory(inventory))
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        self.assertEqual(1, exact["terminal_number"])
        self.assertIsNone(orphan["terminal_number"])
        self.assertIs(orphan["mutation_allowed"], False)
        self.assertEqual(
            "missing-shell-generation",
            orphan["mutation_rejection_reason"],
        )
        self.assertEqual(2, registry["next_number"])
        self.assertEqual({1}, set(registry["bindings"].values()))
        self.assertTrue(inventory_core.guard_live_inventory(inventory))
        self.assertFalse(inventory_core.strict_live_inventory(inventory))
        unchanged = inventory_core.apply_terminal_numbers(
            inventory,
            registry,
            boot_id="boot-a",
            allocate=False,
        )
        self.assertEqual(registry, unchanged)
        self.assertIsNone(orphan["terminal_number"])
        self.assertIs(orphan["mutation_allowed"], False)

        self.assertIs(inventory_core.lookup(inventory, "1"), exact)
        self.assertIsNone(inventory_core.lookup(inventory, "2"))
        self.assertIs(
            inventory_core.lookup(inventory, orphan["shpool_id_raw"]),
            orphan,
        )
        rendered = inventory_core.render_inventory(inventory)
        self.assertRegex(rendered, r"(?m)^\s+-\s+Unresolved provider session")

        with tempfile.TemporaryDirectory(prefix=".mixed-input-", dir=REPO) as raw:
            path = Path(raw) / "inventory.json"
            path.write_text(json.dumps(inventory), encoding="utf-8")
            loaded = getattr(inventory_core, "load_inventory_input")(path)
        self.assertEqual(1, loaded["sessions"][0]["terminal_number"])
        self.assertIsNone(loaded["sessions"][1]["terminal_number"])

    def test_recycled_numbers_respect_the_quarantine_and_continuity(self) -> None:
        quarantine = inventory_core.TERMINAL_NUMBER_QUARANTINE_SECONDS
        now = 1_800_000_000.0
        # Pass 1: two live sessions take 1 and 2.
        inventory = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        retired: dict[int, float] = {}
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=now,
        )
        self.assertEqual(
            [1, 2], [item["terminal_number"] for item in inventory["sessions"]]
        )
        self.assertEqual({}, retired)

        # Pass 2: session 1 died — its number enters quarantine.
        survivor = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_100
        )
        survivor["sessions"] = [survivor["sessions"][1]]
        registry = inventory_core.apply_terminal_numbers(
            survivor,
            registry,
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=now + 100,
        )
        self.assertEqual(2, survivor["sessions"][0]["terminal_number"])
        self.assertEqual({1: now + 100}, retired)

        # Pass 3, inside the quarantine: a NEW session must not take 1.
        fresh = inventory_core.build_inventory(
            *inventory_fixture(3), now=1_800_000_200
        )
        fresh["sessions"] = fresh["sessions"][1:]
        registry = inventory_core.apply_terminal_numbers(
            fresh,
            registry,
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=now + 200,
        )
        numbers = {
            item["shpool_id_raw"]: item["terminal_number"]
            for item in fresh["sessions"]
        }
        self.assertEqual(2, numbers["main2"])
        self.assertEqual(3, numbers["main3"])
        self.assertIn(1, retired)

        # Pass 4: the dead conversation RECOVERS inside the window — its AI
        # binding hands its old number back and the retirement clears.
        revived = inventory_core.build_inventory(
            *inventory_fixture(3), now=1_800_000_300
        )
        registry = inventory_core.apply_terminal_numbers(
            revived,
            registry,
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=now + 300,
        )
        revived_numbers = {
            item["shpool_id_raw"]: item["terminal_number"]
            for item in revived["sessions"]
        }
        self.assertEqual(1, revived_numbers["main"])
        self.assertNotIn(1, retired)

        # Pass 5: dead again, and the quarantine EXPIRES — the number frees,
        # its bindings prune, and the next new session takes the lowest gap.
        after = inventory_core.build_inventory(
            *inventory_fixture(3), now=1_800_000_400
        )
        after["sessions"] = after["sessions"][1:]
        registry = inventory_core.apply_terminal_numbers(
            after,
            registry,
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=now + 400,
        )
        self.assertEqual({1: now + 400}, {n: retired[n] for n in retired if n == 1})
        expired_pass = inventory_core.build_inventory(
            *inventory_fixture(4), now=1_800_000_500
        )
        expired_pass["sessions"] = expired_pass["sessions"][1:]
        registry = inventory_core.apply_terminal_numbers(
            expired_pass,
            registry,
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=now + 400 + quarantine + 1,
        )
        expired_numbers = {
            item["shpool_id_raw"]: item["terminal_number"]
            for item in expired_pass["sessions"]
        }
        # main4 is new; 1 finished quarantine and is the lowest free number.
        self.assertEqual(1, expired_numbers["main4"])
        self.assertNotIn(1, retired)
        self.assertNotIn(
            f"ai:claude:{uuid_for(1)}", registry["bindings"]
        )
        # Legacy invariant for pinned releases stays intact.
        self.assertGreater(
            registry["next_number"], max(registry["bindings"].values())
        )

    def test_retirement_ledger_round_trip_and_boot_reset(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "terminal-numbers-retired.json"
            payload = inventory_core._terminal_retirement_payload(
                {3: 1_800_000_000.0, 1: 1_700_000_000.5}, "boot-a"
            )
            path.write_text(json.dumps(payload), encoding="utf-8")
            os.chmod(path, 0o600)
            self.assertEqual(
                {1: 1_700_000_000.5, 3: 1_800_000_000.0},
                inventory_core._read_terminal_retirements(path, "boot-a"),
            )
            # A different boot voids the ledger (numbers restart anyway).
            self.assertEqual(
                {}, inventory_core._read_terminal_retirements(path, "boot-b")
            )
            # Junk keys/values never surface.
            path.write_text(
                json.dumps(
                    {
                        "schema_version": payload["schema_version"],
                        "boot_id": "boot-a",
                        "retired": {"x": 5, "2": True, "4": -1, "5": 9.5},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                {5: 9.5},
                inventory_core._read_terminal_retirements(path, "boot-a"),
            )

    def test_only_exact_inert_orphan_shape_can_skip_numbering(self) -> None:
        base = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        self._orphan(base["sessions"][1])

        def known_provider(item: dict) -> None:
            item["provider"] = "shell"

        def attached(item: dict) -> None:
            item["availability"] = "attached"
            item["shpool_status"] = "Attached"

        def partial_shell(item: dict) -> None:
            item["shpool_shell"] = {
                "pid": 123,
                "process_start_ticks": None,
            }

        def partial_identity(item: dict) -> None:
            item["identity"]["pid"] = 123

        def retained_hint(item: dict) -> None:
            item["_terminal_identity_hint"] = {
                "provider": "codex",
                "uuid": uuid_for(99),
            }

        def invalid_start(item: dict) -> None:
            item["started_at_unix_ms"] = 0

        def recoverable(item: dict) -> None:
            item["recovery"] = inventory_core.recovery_spec(
                "codex",
                uuid_for(99),
                "/srv/project",
            )

        cases = (
            ("known-provider", known_provider),
            ("attached", attached),
            ("partial-shell", partial_shell),
            ("partial-identity", partial_identity),
            ("retained-hint", retained_hint),
            ("invalid-start", invalid_start),
            ("recoverable", recoverable),
        )
        for label, mutate in cases:
            with self.subTest(case=label):
                inventory = copy.deepcopy(base)
                mutate(inventory["sessions"][1])
                with self.assertRaisesRegex(
                    inventory_core.CollectionError,
                    "lacks an exact generation",
                ):
                    inventory_core.apply_terminal_numbers(
                        inventory,
                        inventory_core._empty_terminal_registry("boot-a"),
                        boot_id="boot-a",
                        allocate=True,
                    )

    def test_orphan_quarantine_marker_is_required_by_guard_and_frozen_input(
        self,
    ) -> None:
        inventory = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        self._orphan(inventory["sessions"][1])
        inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        for label, field, value in (
            ("numbered", "terminal_number", 99),
            ("mutable", "mutation_allowed", True),
            ("wrong-reason", "mutation_rejection_reason", "unsafe-id"),
        ):
            with self.subTest(case=label):
                changed = copy.deepcopy(inventory)
                changed["sessions"][1][field] = value
                self.assertFalse(inventory_core.guard_live_inventory(changed))
                with tempfile.TemporaryDirectory(
                    prefix=".mixed-invalid-",
                    dir=REPO,
                ) as raw:
                    path = Path(raw) / "inventory.json"
                    path.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaises(inventory_core.CollectionError):
                        getattr(inventory_core, "load_inventory_input")(path)

    def test_snapshot_writes_live_exact_rows_alongside_quarantined_orphan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".orphan-snapshot-",
            dir=REPO,
        ) as raw:
            state = Path(raw)
            state.chmod(0o700)
            config = {
                "state_dir": state,
                "aliases": {},
                "max_proc_nodes": 8192,
                "max_proc_depth": 32,
            }
            live = inventory_core.build_inventory(
                *inventory_fixture(2),
                now=1_800_000_000,
            )
            self._orphan(live["sessions"][1])
            with (
                mock.patch.object(
                    inventory_core,
                    "collect_live",
                    return_value=live,
                ),
                mock.patch.object(
                    inventory_core,
                    "_boot_id",
                    return_value="boot-a",
                ),
            ):
                result = inventory_core.snapshot(config=config)
            self.assertEqual("live", result["source"])
            self.assertIs(result["stale"], False)
            self.assertEqual([], result["warnings"])
            self.assertEqual(1, result["sessions"][0]["terminal_number"])
            self.assertIsNone(result["sessions"][1]["terminal_number"])
            stored = json.loads(
                (state / "inventory.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result, stored)

    def test_provisional_generation_promotes_without_renumbering(self) -> None:
        inventory = inventory_core.build_inventory(
            *inventory_fixture(1), now=1_800_000_000
        )
        item = inventory["sessions"][0]
        exact_identity = copy.deepcopy(item["identity"])
        self._unknown(item)
        registry = inventory_core._empty_terminal_registry("boot-a")
        registry = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual(1, item["terminal_number"])

        inventory["daemon_generation"]["process_start_ticks"] += 99
        unchanged = inventory_core.apply_terminal_numbers(
            inventory,
            registry,
            boot_id="boot-a",
            allocate=False,
        )
        self.assertEqual(1, item["terminal_number"])
        self.assertEqual(registry, unchanged)

        item["provider"] = "claude"
        item["identity"] = exact_identity
        promoted = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual(1, item["terminal_number"])
        self.assertEqual(
            1, promoted["bindings"][f"ai:claude:{exact_identity['uuid']}"]
        )

        item["shpool_id"] = item["shpool_id_raw"] = "main20"
        item["started_at_unix_ms"] += 100
        item["shpool_shell"]["pid"] += 100
        item["shpool_shell"]["process_start_ticks"] += 100
        recovered = inventory_core.apply_terminal_numbers(
            inventory, promoted, boot_id="boot-a", allocate=True
        )
        self.assertEqual(1, item["terminal_number"])
        self.assertEqual(2, recovered["next_number"])

    def test_exact_recovery_overrides_a_new_generation_provisional_number(
        self,
    ) -> None:
        inventory = inventory_core.build_inventory(
            *inventory_fixture(1), now=1_800_000_000
        )
        item = inventory["sessions"][0]
        exact_identity = copy.deepcopy(item["identity"])
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        self.assertEqual(1, item["terminal_number"])

        item["shpool_id"] = item["shpool_id_raw"] = "main20"
        item["started_at_unix_ms"] += 100
        item["shpool_shell"]["pid"] += 100
        item["shpool_shell"]["process_start_ticks"] += 100
        self._unknown(item)
        registry = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual(2, item["terminal_number"])

        item["provider"] = "claude"
        item["identity"] = exact_identity
        recovered = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual(1, item["terminal_number"])
        self.assertEqual(3, recovered["next_number"])
        generation = inventory_core._terminal_generation_key(
            inventory, item, "boot-a"
        )
        self.assertEqual(1, recovered["bindings"][generation])
        self.assertEqual(
            [generation],
            [
                key
                for key, value in recovered["bindings"].items()
                if key.startswith("generation:") and value == 1
            ],
        )

    def test_other_provider_or_uuid_never_inherits_an_exact_binding(self) -> None:
        inventory = inventory_core.build_inventory(
            *inventory_fixture(1), now=1_800_000_000
        )
        item = inventory["sessions"][0]
        first_uuid = item["identity"]["uuid"]
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        self.assertEqual(1, item["terminal_number"])

        item["shpool_id"] = item["shpool_id_raw"] = "main20"
        item["started_at_unix_ms"] += 100
        item["shpool_shell"]["pid"] += 100
        item["shpool_shell"]["process_start_ticks"] += 100
        item["provider"] = "codex"
        registry = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual(first_uuid, item["identity"]["uuid"])
        self.assertEqual(2, item["terminal_number"])

        item["shpool_id"] = item["shpool_id_raw"] = "main21"
        item["started_at_unix_ms"] += 100
        item["shpool_shell"]["pid"] += 100
        item["shpool_shell"]["process_start_ticks"] += 100
        item["provider"] = "claude"
        item["identity"]["uuid"] = uuid_for(99)
        inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual(3, item["terminal_number"])

    def test_unrelated_uuid_replaces_same_generation_with_a_new_number(
        self,
    ) -> None:
        inventory = inventory_core.build_inventory(
            *inventory_fixture(1), now=1_800_000_000
        )
        item = inventory["sessions"][0]
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        item["identity"]["uuid"] = uuid_for(98)
        updated = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        generation = inventory_core._terminal_generation_key(
            inventory, item, "boot-a"
        )
        self.assertEqual(2, item["terminal_number"])
        self.assertEqual(2, updated["bindings"][generation])

    def test_registry_rejects_duplicate_generations_and_active_exact_duplicates(
        self,
    ) -> None:
        corrupt = {
            "schema_version": 1,
            "boot_id": "boot-a",
            "next_number": 2,
            "bindings": {
                f"generation:{'1' * 64}": 1,
                f"generation:{'2' * 64}": 1,
            },
        }
        with self.assertRaisesRegex(
            inventory_core.CollectionError, "duplicate generation"
        ):
            inventory_core._validate_terminal_registry(corrupt, "boot-a")

        inventory = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        inventory["sessions"][1]["provider"] = inventory["sessions"][0][
            "provider"
        ]
        inventory["sessions"][1]["identity"] = copy.deepcopy(
            inventory["sessions"][0]["identity"]
        )
        with self.assertRaisesRegex(
            inventory_core.CollectionError, "two active sessions"
        ):
            inventory_core.apply_terminal_numbers(
                inventory, registry, boot_id="boot-a", allocate=False
            )

    def test_snapshot_writes_stable_numbers_and_no_write_leaves_new_rows_null(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".terminal-state-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            config = {
                "state_dir": state,
                "aliases": {},
                "max_proc_nodes": 8192,
                "max_proc_depth": 32,
            }
            first_live = inventory_core.build_inventory(
                *inventory_fixture(2), now=1_800_000_000
            )
            second_live = inventory_core.build_inventory(
                *inventory_fixture(2), now=1_800_000_001
            )
            second_live["sessions"].reverse()
            third_live = inventory_core.build_inventory(
                *inventory_fixture(3), now=1_800_000_002
            )
            with (
                mock.patch.object(inventory_core, "_boot_id", return_value="boot-a"),
                mock.patch.object(
                    inventory_core,
                    "collect_live",
                    side_effect=(
                        copy.deepcopy(first_live),
                        copy.deepcopy(second_live),
                        copy.deepcopy(third_live),
                    ),
                ),
            ):
                first = inventory_core.snapshot(write_state=True, config=config)
                second = inventory_core.snapshot(write_state=True, config=config)
                no_write = inventory_core.snapshot(write_state=False, config=config)
            first_map = {
                row["shpool_id_raw"]: row["terminal_number"]
                for row in first["sessions"]
            }
            second_map = {
                row["shpool_id_raw"]: row["terminal_number"]
                for row in second["sessions"]
            }
            no_write_map = {
                row["shpool_id_raw"]: row["terminal_number"]
                for row in no_write["sessions"]
            }
            self.assertEqual(first_map, second_map)
            self.assertEqual(first_map["main"], no_write_map["main"])
            self.assertEqual(first_map["main2"], no_write_map["main2"])
            self.assertIsNone(no_write_map["main3"])
            registry_path = state / "terminal-numbers.json"
            self.assertEqual(0o600, registry_path.stat().st_mode & 0o777)

    def test_registry_corruption_and_same_boot_disappearance_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".terminal-safe-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            registry = state / "terminal-numbers.json"
            epoch = state / "terminal-numbers.initialized.json"
            epoch.write_text(
                json.dumps({"schema_version": 1, "boot_id": "boot-a"}),
                encoding="utf-8",
            )
            epoch.chmod(0o600)
            with self.assertRaisesRegex(
                inventory_core.CollectionError, "disappeared"
            ):
                inventory_core._read_terminal_registry(
                    registry, "boot-a", epoch
                )
            registry.write_text("{broken", encoding="utf-8")
            registry.chmod(0o600)
            with self.assertRaisesRegex(
                inventory_core.CollectionError, "invalid JSON"
            ):
                inventory_core._read_terminal_registry(
                    registry, "boot-a", epoch
                )
            registry.unlink()
            os.symlink(epoch, registry)
            with self.assertRaises(inventory_core.CollectionError):
                inventory_core._read_terminal_registry(
                    registry, "boot-a", epoch
                )

    def test_registry_receipt_requires_exact_schema_but_allows_old_boot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".terminal-epoch-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            registry = state / "terminal-numbers.json"
            epoch = state / "terminal-numbers.initialized.json"
            cases = (
                {"schema_version": 2, "boot_id": "boot-old"},
                {
                    "schema_version": 1,
                    "boot_id": "boot-old",
                    "extra": True,
                },
                {"schema_version": 1, "boot_id": 7},
            )
            for receipt in cases:
                with self.subTest(receipt=receipt):
                    epoch.write_text(json.dumps(receipt), encoding="utf-8")
                    epoch.chmod(0o600)
                    with self.assertRaisesRegex(
                        inventory_core.CollectionError, "invalid schema"
                    ):
                        inventory_core._read_terminal_registry(
                            registry, "boot-a", epoch
                        )
            epoch.write_text(
                json.dumps({"schema_version": 1, "boot_id": "boot-old"}),
                encoding="utf-8",
            )
            epoch.chmod(0o600)
            self.assertEqual(
                inventory_core._empty_terminal_registry("boot-a"),
                inventory_core._read_terminal_registry(
                    registry, "boot-a", epoch
                ),
            )

    def test_verified_new_boot_resets_monotonic_registry(self) -> None:
        registry = {
            "schema_version": 1,
            "boot_id": "boot-a",
            "next_number": 12,
            "bindings": {
                f"ai:claude:{uuid_for(1)}": 11,
            },
        }
        reset = inventory_core._validate_terminal_registry(registry, "boot-b")
        self.assertEqual(
            inventory_core._empty_terminal_registry("boot-b"), reset
        )


class AutomaticTitleTests(unittest.TestCase):
    def _private_config(self, base: Path) -> tuple[Path, dict]:
        base.chmod(0o700)
        state = base / "state"
        state.mkdir(mode=0o700)
        config_path = base / "inventory.json"
        config_path.write_text(
            json.dumps({"schema_version": 1, "aliases": {}}),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        return config_path, {
            "state_dir": state,
            "max_proc_nodes": 8192,
            "max_proc_depth": 32,
        }

    def _caller_fixture(
        self, config: dict, exact: str
    ) -> tuple[dict, dict[int, dict], dict[str, str], int]:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        fixture[5].update(config)
        fixture[4] = (
            {
                exact: {
                    "id": exact,
                    "title": "raw first prompt " * 20,
                    "cwd": "/srv/project-1",
                    "session_index_name": "",
                }
            },
            {},
        )
        fixture[3] = {
            2001: [{"source": "cli", "id": exact, "session_id": exact}]
        }
        fixture[2][3001] = process(3001, 2001, "python3")
        inventory = inventory_core.build_inventory(
            *fixture, now=1_800_000_000
        )
        environment = {
            "SHPOOL_SESSION_NAME": "main",
            "CODEX_THREAD_ID": exact,
            # Sandboxed HOME: without it the title/color pushes inside
            # self-name land in the developer's real provider stores.
            "HOME": str(config.get("state_dir", "/nonexistent-home")),
        }
        return inventory, fixture[2], environment, 3001

    def test_title_validation_kill_switch_and_provenance_precedence(self) -> None:
        self.assertEqual(
            "Session Kit Updates",
            inventory_core.normalize_automatic_title(" Session Kit Updates "),
        )
        for rejected in (
            "One",
            "one lowercase",
            "Codex Session Work",
            "Six Word Automatic Session Name Here",
            "Unsafe / Path",
            "Alphaabcdefghijkl Betaabcdefghijkl Gammaabcdefghijkl Delta Epsilon Sixth",
        ):
            with self.subTest(rejected=rejected):
                with self.assertRaises(inventory_core.CollectionError):
                    inventory_core.normalize_automatic_title(rejected)
        self.assertFalse(
            inventory_core.automatic_naming_enabled(
                {"SESSION_KIT_AUTO_NAME": "0"}
            )
        )
        exact = uuid_for(81)
        automatic = {f"codex:{exact}": "Session Kit Updates"}
        self.assertEqual(
            ("Manual Override", "alias"),
            inventory_core._provider_title_info(
                "codex",
                exact,
                "Provider Rename",
                {f"codex:{exact}": "Manual Override"},
                automatic_titles=automatic,
            ),
        )
        self.assertEqual(
            ("Provider Rename", "native"),
            inventory_core._provider_title_info(
                "codex",
                exact,
                "Provider Rename",
                {},
                automatic_titles=automatic,
            ),
        )
        with mock.patch.dict(
            os.environ, {"SESSION_KIT_AUTO_NAME": "0"}, clear=False
        ):
            title, source = inventory_core._provider_title_info(
                "codex",
                exact,
                "Raw prompt",
                {},
                "/srv/project",
                1_700_000_000_000,
                automatic,
                provider_title_is_explicit=False,
            )
        self.assertEqual("context", source)
        self.assertNotEqual("Session Kit Updates", title)

    def test_atomic_write_manual_alias_race_and_mixed_release_retention(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".automatic-title-", dir=REPO) as raw:
            base = Path(raw)
            config_path, config = self._private_config(base)
            exact = uuid_for(82)
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": str(config_path)}, clear=False
            ):
                stored = inventory_core.mutate_canonical_automatic_title(
                    config, "codex", exact, "Session Kit Updates"
                )
                self.assertEqual("Session Kit Updates", stored["title"])
                # The pinned schema-v1 alias writer preserves the new keys,
                # which is the rollback/mixed-release compatibility contract.
                inventory_core.mutate_canonical_alias(
                    config, "codex", uuid_for(83), "Pinned Alias"
                )
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    "Session Kit Updates",
                    document["automatic_titles"][f"codex:{exact}"],
                )
                inventory_core.mutate_canonical_alias(
                    config, "codex", exact, "Manual Override"
                )
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "explicit local alias"
                ):
                    inventory_core.mutate_canonical_automatic_title(
                        config,
                        "codex",
                        exact,
                        "Different Automatic Name",
                        overwrite=True,
                    )
                title, source = inventory_core._provider_title_info(
                    "codex",
                    exact,
                    "Raw prompt",
                    inventory_core.canonical_aliases(config),
                    automatic_titles=inventory_core.canonical_automatic_titles(
                        config
                    ),
                    provider_title_is_explicit=False,
                )
                self.assertEqual(("Manual Override", "alias"), (title, source))
                # An explicit reset removes only the automatic layer, even
                # while a manual alias remains authoritative.
                inventory_core.mutate_canonical_automatic_title(
                    config, "codex", exact, None
                )
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertNotIn(
                    f"codex:{exact}", document.get("automatic_titles", {})
                )
                self.assertEqual(
                    "Manual Override", document["aliases"][f"codex:{exact}"]
                )
                raced = uuid_for(87)
                barrier = threading.Barrier(2)
                errors: list[BaseException] = []

                def automatic_writer() -> None:
                    barrier.wait()
                    try:
                        inventory_core.mutate_canonical_automatic_title(
                            config, "codex", raced, "Concurrent Task Name"
                        )
                    except inventory_core.CollectionError:
                        # If the manual writer wins the lock, refusing the
                        # lower-precedence automatic write is correct.
                        pass
                    except BaseException as exc:
                        errors.append(exc)

                def manual_writer() -> None:
                    barrier.wait()
                    try:
                        inventory_core.mutate_canonical_alias(
                            config, "codex", raced, "Concurrent Manual Name"
                        )
                    except BaseException as exc:
                        errors.append(exc)

                threads = [
                    threading.Thread(target=automatic_writer),
                    threading.Thread(target=manual_writer),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual([], errors)
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    "Concurrent Manual Name",
                    document["aliases"][f"codex:{raced}"],
                )
                title, source = inventory_core._provider_title_info(
                    "codex",
                    raced,
                    "Raw prompt",
                    inventory_core.canonical_aliases(config),
                    automatic_titles=inventory_core.canonical_automatic_titles(
                        config
                    ),
                    provider_title_is_explicit=False,
                )
                self.assertEqual(("Concurrent Manual Name", "alias"), (title, source))

    def test_self_name_proves_root_generation_refuses_bad_callers_and_tracks_failures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".self-name-", dir=REPO) as raw:
            base = Path(raw)
            config_path, config = self._private_config(base)
            exact = uuid_for(84)
            inventory, table, environment, current_pid = self._caller_fixture(
                config, exact
            )
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": str(config_path)}, clear=False
            ):
                for attempt in (1, 2):
                    with self.assertRaisesRegex(
                        inventory_core.CollectionError,
                        "name failed" if attempt == 2 else "name pending",
                    ):
                        inventory_core.self_name_automatic_title(
                            config,
                            "bad lowercase",
                            inventory=inventory,
                            process_table=table,
                            environ=environment,
                            current_pid=current_pid,
                        )
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    2,
                    document["automatic_title_failures"][f"codex:{exact}"],
                )
                failed_inventory, _, _, _ = self._caller_fixture(
                    {
                        **config,
                        "automatic_title_failures": document[
                            "automatic_title_failures"
                        ],
                    },
                    exact,
                )
                self.assertEqual(
                    "failed",
                    failed_inventory["sessions"][0]["automatic_name_state"],
                )
                result = inventory_core.self_name_automatic_title(
                    config,
                    "Session Kit Updates",
                    inventory=inventory,
                    process_table=table,
                    environ=environment,
                    current_pid=current_pid,
                )
                self.assertEqual("ready", result["automatic_name_state"])
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertNotIn("automatic_title_failures", document)
                # A self-name is an explicit rename: it must land in the alias
                # tier too, or a provider's own ai-title keeps outranking it
                # in every display.
                self.assertEqual(
                    "Session Kit Updates",
                    result["aliases"][f"codex:{exact}"],
                )
                self.assertEqual(
                    "Session Kit Updates",
                    document["aliases"][f"codex:{exact}"],
                )
                repeated = inventory_core.self_name_automatic_title(
                    config,
                    "Session Kit Release",
                    inventory=inventory,
                    process_table=table,
                    environ=environment,
                    current_pid=current_pid,
                )
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual("Session Kit Release", repeated["title"])
                self.assertEqual(
                    "Session Kit Release",
                    document["aliases"][f"codex:{exact}"],
                )
                self.assertEqual(
                    "Session Kit Release",
                    document["automatic_titles"][f"codex:{exact}"],
                )

                bad_thread = dict(environment)
                bad_thread["CODEX_THREAD_ID"] = uuid_for(99)
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "managed root conversation"
                ):
                    inventory_core.self_name_automatic_title(
                        config,
                        "Another Task Name",
                        inventory=inventory,
                        process_table=table,
                        environ=bad_thread,
                        current_pid=current_pid,
                    )
                child_uuid = uuid_for(98)
                child_inventory = copy.deepcopy(inventory)
                child_inventory["sessions"][0]["subagents"] = [
                    {
                        "provider": "codex",
                        "uuid": child_uuid,
                        "status": "running",
                    }
                ]
                child_environment = dict(environment)
                child_environment["CODEX_THREAD_ID"] = child_uuid
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "subagent"
                ):
                    inventory_core.self_name_automatic_title(
                        config,
                        "Another Task Name",
                        inventory=child_inventory,
                        process_table=table,
                        environ=child_environment,
                        current_pid=current_pid,
                    )
                outside = dict(environment)
                outside.pop("SHPOOL_SESSION_NAME")
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "outside a managed shell"
                ):
                    inventory_core.self_name_automatic_title(
                        config,
                        "Another Task Name",
                        inventory=inventory,
                        process_table=table,
                        environ=outside,
                        current_pid=current_pid,
                    )
                setup = copy.deepcopy(inventory)
                setup["sessions"][0]["setup_incomplete"] = True
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "setup is incomplete"
                ):
                    inventory_core.self_name_automatic_title(
                        config,
                        "Another Task Name",
                        inventory=setup,
                        process_table=table,
                        environ=environment,
                        current_pid=current_pid,
                    )
                subagent_table = copy.deepcopy(table)
                subagent_table[3001]["cmdline"] = [
                    "python3",
                    "--parent-session-id",
                    exact,
                ]
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "subagent"
                ):
                    inventory_core.prove_self_name_caller(
                        inventory,
                        subagent_table,
                        environment,
                        current_pid,
                    )

    def test_provider_title_pending_requires_a_private_regular_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-state-", dir=REPO) as raw:
            state = Path(raw)
            marker_root = state / "provider-untitled"
            marker_root.mkdir()
            live = {
                "sessions": [
                    {
                        "provider": "codex",
                        "shpool_id_raw": "main2",
                    },
                    {
                        "provider": "claude",
                        "shpool_id_raw": "main3",
                    },
                ]
            }
            (marker_root / "main2").touch()
            inventory_core.apply_provider_title_states(
                live, {"state_dir": state}
            )
            self.assertEqual(
                "pending", live["sessions"][0]["provider_title_state"]
            )
            self.assertNotIn("provider_title_state", live["sessions"][1])

            live["sessions"][0].update(
                {"availability": "attached", "agent_status": "working"}
            )
            inventory_core.apply_provider_title_states(
                live, {"state_dir": state}
            )
            self.assertEqual(
                "deferred", live["sessions"][0]["provider_title_state"]
            )

            (marker_root / "main2").unlink()
            (marker_root / "target").touch()
            (marker_root / "main2").symlink_to(marker_root / "target")
            inventory_core.apply_provider_title_states(
                live, {"state_dir": state}
            )
            self.assertEqual(
                "ready", live["sessions"][0]["provider_title_state"]
            )

    def test_retained_titles_audit_and_dry_run_token_gate_prune(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".automatic-prune-", dir=REPO) as raw:
            base = Path(raw)
            config_path, config = self._private_config(base)
            active = uuid_for(85)
            orphan = uuid_for(86)
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": str(config_path)}, clear=False
            ):
                inventory_core.mutate_canonical_automatic_title(
                    config, "codex", active, "Active Task Name"
                )
                inventory_core.mutate_canonical_automatic_title(
                    config, "claude", orphan, "Retained Closed Task"
                )
                live, _, _, _ = self._caller_fixture(config, active)
                audit = inventory_core.audit_automatic_titles(config, live)
                self.assertTrue(audit["dry_run"])
                self.assertEqual([f"claude:{orphan}"], audit["orphans"])
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "stale or does not match"
                ):
                    inventory_core.prune_automatic_titles(
                        config, live, "0" * 64
                    )
                pruned = inventory_core.prune_automatic_titles(
                    config, live, audit["prune_token"]
                )
                self.assertEqual([f"claude:{orphan}"], pruned["pruned"])
                self.assertEqual(
                    {f"codex:{active}": "Active Task Name"},
                    inventory_core.canonical_automatic_titles(config),
                )


class CanonicalAliasTests(unittest.TestCase):
    def test_new_alias_writer_serializes_with_pinned_config_lock_writer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".alias-lock-", dir=REPO) as raw:
            base = Path(raw)
            base.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "inventory.json"
            config_path.write_text(
                json.dumps({"schema_version": 1, "aliases": {}}),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            config = {"state_dir": state}
            old_key = f"claude:{uuid_for(31)}"
            new_uuid = uuid_for(32)
            config_lock = state / "config.lock"
            config_lock.touch(mode=0o600)
            config_lock.chmod(0o600)
            held = config_lock.open("r+")
            fcntl.flock(held, fcntl.LOCK_EX)
            started = threading.Event()
            finished = threading.Event()
            errors: list[BaseException] = []

            def new_writer() -> None:
                started.set()
                try:
                    inventory_core.mutate_canonical_alias(
                        config, "codex", new_uuid, "New writer"
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(config_path)},
                clear=False,
            ):
                thread = threading.Thread(target=new_writer, daemon=True)
                try:
                    thread.start()
                    self.assertTrue(started.wait(1))
                    self.assertFalse(finished.wait(0.1))
                    old_document = json.loads(
                        config_path.read_text(encoding="utf-8")
                    )
                    old_document["aliases"][old_key] = "Pinned writer"
                    inventory_core.atomic_write_json(config_path, old_document)
                finally:
                    fcntl.flock(held, fcntl.LOCK_UN)
                    held.close()
                self.assertTrue(finished.wait(2))
                thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            stored = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual("Pinned writer", stored["aliases"][old_key])
            self.assertEqual(
                "New writer", stored["aliases"][f"codex:{new_uuid}"]
            )

    def test_explicit_runtime_migration_preserves_runtime_wins_then_retires_overlay(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".alias-migrate-", dir=REPO) as raw:
            base = Path(raw)
            base.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "inventory.json"
            exact = uuid_for(5)
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state_dir": str(state),
                        "command_timeout_seconds": 7,
                        "aliases": {f"claude:{exact}": "Config value"},
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            runtime = state / "aliases.json"
            runtime.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"claude:{exact}": "Visible runtime value",
                            f"codex:{uuid_for(6)}": "Runtime only",
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            config_preimage = config_path.read_bytes()
            runtime_preimage = runtime.read_bytes()
            config = {"state_dir": state}
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(config_path)},
                clear=False,
            ):
                migrated = inventory_core.migrate_runtime_aliases(config)
                repeated = inventory_core.migrate_runtime_aliases(config)
            stored = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(migrated["migrated"])
            self.assertTrue(repeated["already_migrated"])
            self.assertEqual(7, stored["command_timeout_seconds"])
            self.assertEqual(
                "Visible runtime value", stored["aliases"][f"claude:{exact}"]
            )
            self.assertFalse(runtime.exists())
            archive = state / "aliases.json.migrated-v1"
            source_backup = state / "aliases.json.pre-migration-v1"
            config_backup = config_path.with_name(
                "inventory.json.pre-runtime-alias-migration-v1"
            )
            self.assertEqual(runtime_preimage, archive.read_bytes())
            self.assertEqual(runtime_preimage, source_backup.read_bytes())
            self.assertEqual(config_preimage, config_backup.read_bytes())
            self.assertEqual(0o600, archive.stat().st_mode & 0o777)
            self.assertEqual(0o600, source_backup.stat().st_mode & 0o777)
            self.assertEqual(0o600, config_backup.stat().st_mode & 0o777)

    def test_runtime_migration_resumes_after_config_publish_crash(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".alias-resume-", dir=REPO) as raw:
            base = Path(raw)
            base.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "inventory.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state_dir": str(state),
                        "aliases": {f"claude:{uuid_for(40)}": "Config"},
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            runtime = state / "aliases.json"
            runtime.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"claude:{uuid_for(40)}": "Runtime",
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            config_preimage = config_path.read_bytes()
            runtime_preimage = runtime.read_bytes()
            archive = state / "aliases.json.migrated-v1"
            real_replace = os.replace

            def fail_archive_publish(source: str, target: str) -> None:
                if Path(source) == runtime and Path(target) == archive:
                    raise OSError("injected archive publish failure")
                real_replace(source, target)

            config = {"state_dir": state}
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(config_path)},
                clear=False,
            ):
                with mock.patch.object(
                    inventory_core.os,
                    "replace",
                    side_effect=fail_archive_publish,
                ):
                    with self.assertRaisesRegex(
                        OSError, "injected archive publish failure"
                    ):
                        inventory_core.migrate_runtime_aliases(config)
                resumed = inventory_core.migrate_runtime_aliases(config)
            self.assertTrue(resumed["migrated"])
            self.assertFalse(runtime.exists())
            self.assertEqual(runtime_preimage, archive.read_bytes())
            self.assertEqual(
                runtime_preimage,
                (state / "aliases.json.pre-migration-v1").read_bytes(),
            )
            self.assertEqual(
                config_preimage,
                config_path.with_name(
                    "inventory.json.pre-runtime-alias-migration-v1"
                ).read_bytes(),
            )
            self.assertEqual(
                "Runtime",
                json.loads(config_path.read_text(encoding="utf-8"))["aliases"][
                    f"claude:{uuid_for(40)}"
                ],
            )

    def test_runtime_migration_accepts_only_byte_exact_rollback_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".alias-rollback-", dir=REPO) as raw:
            base = Path(raw)
            base.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "inventory.json"
            config_path.write_text(
                json.dumps({"schema_version": 1, "aliases": {}}),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            runtime = state / "aliases.json"
            runtime.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"codex:{uuid_for(41)}": "Rollback value",
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            config = {"state_dir": state}
            archive = state / "aliases.json.migrated-v1"
            config_backup = config_path.with_name(
                "inventory.json.pre-runtime-alias-migration-v1"
            )
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(config_path)},
                clear=False,
            ):
                inventory_core.migrate_runtime_aliases(config)
                config_path.write_bytes(config_backup.read_bytes())
                config_path.chmod(0o600)
                runtime.write_bytes(archive.read_bytes())
                runtime.chmod(0o600)
                retried = inventory_core.migrate_runtime_aliases(config)
                self.assertTrue(retried["migrated"])
                self.assertFalse(runtime.exists())
                self.assertEqual(
                    "Rollback value",
                    json.loads(config_path.read_text(encoding="utf-8"))[
                        "aliases"
                    ][f"codex:{uuid_for(41)}"],
                )
                archive_preimage = archive.read_bytes()
                runtime.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "aliases": {
                                f"codex:{uuid_for(41)}": "Diverged",
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                runtime.chmod(0o600)
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "differ"
                ):
                    inventory_core.migrate_runtime_aliases(config)
            self.assertEqual(archive_preimage, archive.read_bytes())
            self.assertTrue(runtime.exists())

    def test_runtime_migration_repairs_an_exact_partial_config_rollback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".alias-partial-", dir=REPO) as raw:
            base = Path(raw)
            base.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "inventory.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"claude:{uuid_for(43)}": "Config value",
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            runtime = state / "aliases.json"
            runtime.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"claude:{uuid_for(43)}": "Runtime value",
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            config = {"state_dir": state}
            archive = state / "aliases.json.migrated-v1"
            config_backup = config_path.with_name(
                "inventory.json.pre-runtime-alias-migration-v1"
            )
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(config_path)},
                clear=False,
            ):
                inventory_core.migrate_runtime_aliases(config)
                archive_preimage = archive.read_bytes()
                config_path.write_bytes(config_backup.read_bytes())
                config_path.chmod(0o600)
                repaired = inventory_core.migrate_runtime_aliases(config)
            self.assertTrue(repaired["migrated"])
            self.assertFalse(repaired["already_migrated"])
            self.assertFalse(runtime.exists())
            self.assertEqual(archive_preimage, archive.read_bytes())
            self.assertEqual(
                "Runtime value",
                json.loads(config_path.read_text(encoding="utf-8"))["aliases"][
                    f"claude:{uuid_for(43)}"
                ],
            )

    def test_absent_config_backup_is_a_non_publishable_rollback_sentinel(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".alias-absent-", dir=REPO) as raw:
            base = Path(raw)
            base.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "inventory.json"
            runtime = state / "aliases.json"
            runtime.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"claude:{uuid_for(42)}": "Only runtime",
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            config = {"state_dir": state}
            archive = state / "aliases.json.migrated-v1"
            backup = config_path.with_name(
                "inventory.json.pre-runtime-alias-migration-v1"
            )
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(config_path)},
                clear=False,
            ):
                inventory_core.migrate_runtime_aliases(config)
                self.assertEqual(
                    inventory_core.ABSENT_ALIAS_CONFIG_BACKUP,
                    backup.read_bytes(),
                )
                config_path.unlink()
                runtime.write_bytes(archive.read_bytes())
                runtime.chmod(0o600)
                retried = inventory_core.migrate_runtime_aliases(config)
                self.assertTrue(retried["migrated"])
                self.assertEqual(
                    "Only runtime",
                    json.loads(config_path.read_text(encoding="utf-8"))[
                        "aliases"
                    ][f"claude:{uuid_for(42)}"],
                )
                config_path.write_bytes(
                    inventory_core.ABSENT_ALIAS_CONFIG_BACKUP
                )
                config_path.chmod(0o600)
                runtime.write_bytes(archive.read_bytes())
                runtime.chmod(0o600)
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "diverged"
                ):
                    inventory_core.migrate_runtime_aliases(config)
            self.assertEqual(
                inventory_core.ABSENT_ALIAS_CONFIG_BACKUP,
                config_path.read_bytes(),
            )


class InventoryInputAndRecoveryTests(unittest.TestCase):
    def test_retained_launch_modes_accept_legacy_resume_and_fork_without_aliasing_source(
        self,
    ) -> None:
        def unknown_fixture() -> dict:
            fixture = inventory_core.build_inventory(
                *inventory_fixture(1), now=1_800_000_000
            )
            item = fixture["sessions"][0]
            item["provider"] = "unknown"
            item["display_provider"] = "unknown"
            item["identity"] = {
                "uuid": None,
                "pid": item["shpool_shell"]["pid"],
                "process_start_ticks": item["shpool_shell"][
                    "process_start_ticks"
                ],
                "provenance": "process tree",
                "confidence": "unknown",
            }
            return fixture

        with tempfile.TemporaryDirectory(prefix=".launch-modes-", dir=REPO) as raw:
            start_dir = Path(raw)
            start_dir.chmod(0o700)
            for mode in ("resume", "fork", "unknown"):
                with self.subTest(mode=mode):
                    for path in start_dir.iterdir():
                        path.unlink()
                    fixture = unknown_fixture()
                    item = fixture["sessions"][0]
                    shell = item["shpool_shell"]
                    daemon = fixture["daemon_generation"]
                    source_uuid = uuid_for(91)
                    (start_dir / "main").write_text(
                        f"claude\t{item['cwd']}\t{source_uuid}\t{mode}\n",
                        encoding="utf-8",
                    )
                    (start_dir / "main.expected").write_text(
                        f"claude\t{item['cwd']}\tboot-a\t"
                        f"{item['started_at_unix_ms']}\t{shell['pid']}\t"
                        f"{shell['process_start_ticks']}\t{daemon['pid']}\t"
                        f"{daemon['process_start_ticks']}\t{source_uuid}\t{mode}\n",
                        encoding="utf-8",
                    )
                    (start_dir / "main").chmod(0o600)
                    (start_dir / "main.expected").chmod(0o600)
                    inventory_core.apply_retained_setup_attributions(
                        fixture, start_dir=start_dir, boot_id="boot-a"
                    )
                    if mode == "unknown":
                        self.assertEqual("unknown", item["display_provider"])
                        self.assertNotIn("_terminal_identity_hint", item)
                    else:
                        self.assertEqual("claude", item["display_provider"])
                        self.assertTrue(item["setup_incomplete"])
                        if mode == "resume":
                            self.assertEqual(
                                {
                                    "provider": "claude",
                                    "uuid": source_uuid,
                                },
                                item["_terminal_identity_hint"],
                            )
                        else:
                            self.assertNotIn("_terminal_identity_hint", item)

    def test_exact_retained_start_proofs_add_display_only_attribution(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        expected = (
            ("s20260728-022604-447706", "claude"),
            ("s20260728-022232-387175", "codex"),
        )
        for item, (shpool_id, _) in zip(fixture["sessions"], expected):
            item["shpool_id"] = shpool_id
            item["shpool_id_raw"] = shpool_id
            item["display_shpool_id"] = shpool_id
            item["provider"] = "unknown"
            item["display_provider"] = "unknown"
            item["identity"] = {
                "uuid": None,
                "pid": item["shpool_shell"]["pid"],
                "process_start_ticks": item["shpool_shell"][
                    "process_start_ticks"
                ],
                "provenance": "process tree",
                "confidence": "unknown",
            }
            item["title"] = "Unresolved provider session"
            item["display_title"] = item["title"]

        with tempfile.TemporaryDirectory(
            prefix=".retained-setup-", dir=REPO
        ) as raw:
            start_dir = Path(raw)
            start_dir.chmod(0o700)
            for item, (_, provider) in zip(fixture["sessions"], expected):
                shpool_id = item["shpool_id_raw"]
                cwd = item["cwd"]
                shell = item["shpool_shell"]
                generation = fixture["daemon_generation"]
                start = start_dir / shpool_id
                sidecar = start_dir / f"{shpool_id}.expected"
                start.write_text(f"{provider}\t{cwd}\t\n", encoding="utf-8")
                sidecar.write_text(
                    f"{provider}\t{cwd}\tboot-a\t"
                    f"{item['started_at_unix_ms']}\t{shell['pid']}\t"
                    f"{shell['process_start_ticks']}\t{generation['pid']}\t"
                    f"{generation['process_start_ticks']}\t\n",
                    encoding="utf-8",
                )
                start.chmod(0o600)
                sidecar.chmod(0o600)

            inventory_core.apply_retained_setup_attributions(
                fixture, start_dir=start_dir, boot_id="boot-a"
            )

        for item, (_, provider) in zip(fixture["sessions"], expected):
            self.assertEqual("unknown", item["provider"])
            self.assertEqual("unknown", item["identity"]["confidence"])
            self.assertIsNone(item["identity"]["uuid"])
            self.assertEqual("Unresolved provider session", item["title"])
            self.assertEqual(provider, item["display_provider"])
            self.assertTrue(item["setup_incomplete"])
            self.assertEqual(
                f"{provider.title()} setup incomplete",
                item["display_title"],
            )

    def test_retained_setup_attribution_rejects_unsafe_and_stale_records(
        self,
    ) -> None:
        def unknown_fixture() -> dict:
            fixture = inventory_core.build_inventory(
                *inventory_fixture(1), now=1_800_000_000
            )
            item = fixture["sessions"][0]
            item["provider"] = "unknown"
            item["display_provider"] = "unknown"
            item["identity"] = {
                "uuid": None,
                "pid": item["shpool_shell"]["pid"],
                "process_start_ticks": item["shpool_shell"][
                    "process_start_ticks"
                ],
                "provenance": "process tree",
                "confidence": "unknown",
            }
            return fixture

        with tempfile.TemporaryDirectory(
            prefix=".retained-reject-", dir=REPO
        ) as raw:
            start_dir = Path(raw)
            start_dir.chmod(0o700)

            def write_records(fixture: dict, *, boot: str = "boot-a") -> None:
                item = fixture["sessions"][0]
                generation = fixture["daemon_generation"]
                start = start_dir / "main"
                sidecar = start_dir / "main.expected"
                start.write_text(
                    f"claude\t{item['cwd']}\t\n", encoding="utf-8"
                )
                sidecar.write_text(
                    f"claude\t{item['cwd']}\t{boot}\t"
                    f"{item['started_at_unix_ms']}\t"
                    f"{item['shpool_shell']['pid']}\t"
                    f"{item['shpool_shell']['process_start_ticks']}\t"
                    f"{generation['pid']}\t"
                    f"{generation['process_start_ticks']}\t\n",
                    encoding="utf-8",
                )
                start.chmod(0o600)
                sidecar.chmod(0o600)

            for label in (
                "stale boot",
                "mismatched shell",
                "mismatched identity",
                "mismatched cwd",
                "mismatched start time",
                "mismatched daemon",
                "mismatched side provider",
                "mismatched uuid",
                "wrong file mode",
                "symlink",
                "wrong owner",
            ):
                with self.subTest(label=label):
                    for path in start_dir.iterdir():
                        path.unlink()
                    fixture = unknown_fixture()
                    write_records(
                        fixture,
                        boot="old-boot" if label == "stale boot" else "boot-a",
                    )
                    if label == "mismatched shell":
                        fixture["sessions"][0]["shpool_shell"]["pid"] += 1
                    elif label == "mismatched identity":
                        fixture["sessions"][0]["identity"]["confidence"] = "exact"
                    elif label == "mismatched cwd":
                        fixture["sessions"][0]["cwd"] += "-changed"
                    elif label == "mismatched start time":
                        fixture["sessions"][0]["started_at_unix_ms"] += 1
                    elif label == "mismatched daemon":
                        fixture["daemon_generation"]["process_start_ticks"] += 1
                    elif label == "mismatched side provider":
                        sidecar = start_dir / "main.expected"
                        sidecar.write_text(
                            sidecar.read_text(encoding="utf-8").replace(
                                "claude\t", "codex\t", 1
                            ),
                            encoding="utf-8",
                        )
                    elif label == "mismatched uuid":
                        sidecar = start_dir / "main.expected"
                        sidecar.write_text(
                            sidecar.read_text(encoding="utf-8").replace(
                                "\t\n",
                                f"\t{uuid_for(99)}\n",
                            ),
                            encoding="utf-8",
                        )
                    elif label == "wrong file mode":
                        (start_dir / "main.expected").chmod(0o644)
                    elif label == "symlink":
                        target = start_dir / "target"
                        target.write_text(
                            (start_dir / "main.expected").read_text(),
                            encoding="utf-8",
                        )
                        target.chmod(0o600)
                        (start_dir / "main.expected").unlink()
                        os.symlink(target, start_dir / "main.expected")
                    owner = (
                        mock.patch.object(
                            inventory_core.os,
                            "geteuid",
                            return_value=os.geteuid() + 1,
                        )
                        if label == "wrong owner"
                        else contextlib.nullcontext()
                    )
                    with owner:
                        inventory_core.apply_retained_setup_attributions(
                            fixture,
                            start_dir=start_dir,
                            boot_id="boot-a",
                        )
                    item = fixture["sessions"][0]
                    self.assertEqual("unknown", item["display_provider"])
                    self.assertFalse(item["setup_incomplete"])

    def test_guard_live_permits_unrelated_unknown_provider_row(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        self.assertTrue(inventory_core.strict_live_inventory(fixture))
        unknown = copy.deepcopy(fixture["sessions"][0])
        unknown_id = "s20260728-022232-387175"
        unknown["row"] = 3
        unknown["shpool_id"] = unknown_id
        unknown["shpool_id_raw"] = unknown_id
        unknown["display_shpool_id"] = unknown_id
        unknown["provider"] = "unknown"
        unknown["identity"] = {
            "uuid": None,
            "pid": unknown["shpool_shell"]["pid"],
            "process_start_ticks": unknown["shpool_shell"][
                "process_start_ticks"
            ],
            "provenance": "process tree",
            "confidence": "unknown",
        }
        fixture["sessions"].append(unknown)

        self.assertFalse(inventory_core.strict_live_inventory(fixture))
        self.assertTrue(inventory_core.guard_live_inventory(fixture))
        known = inventory_core.lookup(fixture, "main")
        self.assertIsNotNone(known)
        self.assertEqual("claude", known["provider"])
        self.assertEqual("exact", known["identity"]["confidence"])

    def test_guard_rejects_invalid_outside_identity_and_managed_duplicate(
        self,
    ) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        managed = fixture["sessions"][0]
        valid_outside = {
            "provider": "claude",
            "identity": {
                "uuid": uuid_for(90),
                "pid": 9000,
                "process_start_ticks": 90000,
                "provenance": "claude agents --json",
                "confidence": "exact",
            },
        }
        fixture["outside_agents"] = [valid_outside]
        self.assertTrue(inventory_core.guard_live_inventory(fixture))
        self.assertTrue(inventory_core.strict_live_inventory(fixture))

        duplicate = copy.deepcopy(fixture)
        duplicate["outside_agents"][0]["provider"] = managed["provider"]
        duplicate["outside_agents"][0]["identity"] = copy.deepcopy(
            managed["identity"]
        )
        self.assertFalse(inventory_core.guard_live_inventory(duplicate))
        self.assertFalse(inventory_core.strict_live_inventory(duplicate))

        mutations = (
            lambda item: item.update(provider="shell"),
            lambda item: item["identity"].update(uuid="bad"),
            lambda item: item["identity"].update(pid=0),
            lambda item: item["identity"].update(process_start_ticks=True),
            lambda item: item["identity"].update(confidence="unknown"),
        )
        for mutate in mutations:
            candidate = copy.deepcopy(fixture)
            mutate(candidate["outside_agents"][0])
            self.assertFalse(inventory_core.guard_live_inventory(candidate))
            self.assertFalse(inventory_core.strict_live_inventory(candidate))

    def test_guard_live_rejects_stale_partial_and_malformed_snapshots(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        malformed: list[tuple[str, object]] = []

        def changed(label: str, mutate: object) -> None:
            candidate = copy.deepcopy(fixture)
            mutate(candidate)
            malformed.append((label, candidate))

        changed("cache source", lambda value: value.update(source="cache"))
        changed("stale", lambda value: value.update(stale=True))
        changed("warning", lambda value: value["warnings"].append("partial"))
        changed("warnings type", lambda value: value.update(warnings=None))
        changed("schema version", lambda value: value.update(schema_version=999))
        changed("missing sessions", lambda value: value.pop("sessions"))
        changed(
            "daemon boolean pid",
            lambda value: value["daemon_generation"].update(pid=True),
        )
        changed(
            "daemon generation",
            lambda value: value["daemon_generation"].update(
                process_start_ticks=0
            ),
        )
        changed(
            "shell generation",
            lambda value: value["sessions"][0]["shpool_shell"].update(pid=True),
        )
        changed(
            "known confidence",
            lambda value: value["sessions"][0]["identity"].update(
                confidence="unknown"
            ),
        )
        changed(
            "known uuid",
            lambda value: value["sessions"][0]["identity"].update(uuid="bad"),
        )
        changed(
            "duplicate row",
            lambda value: value["sessions"][1].update(
                row=value["sessions"][0]["row"]
            ),
        )
        changed(
            "duplicate raw id",
            lambda value: value["sessions"][1].update(
                shpool_id_raw=value["sessions"][0]["shpool_id_raw"],
                shpool_id=value["sessions"][0]["shpool_id"],
            ),
        )
        changed(
            "unknown provider",
            lambda value: value["sessions"][0].update(provider="other"),
        )
        changed("sessions type", lambda value: value.update(sessions={}))

        for label, candidate in malformed:
            with self.subTest(label=label):
                self.assertFalse(
                    inventory_core.guard_live_inventory(candidate)
                )
                self.assertFalse(
                    inventory_core.strict_live_inventory(candidate)
                )

    def test_malformed_json_and_duplicate_selectors_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".inventory-input-", dir=REPO) as raw:
            base = Path(raw)
            malformed = base / "malformed.json"
            malformed.write_text("{broken", encoding="utf-8")
            proc = run(
                ["python3", CORE_PATH, "render", "--input", malformed], check=False
            )
            self.assertNotEqual(0, proc.returncode)
            self.assertIn("cannot read inventory input", proc.stderr)

            fixture = inventory_core.build_inventory(
                *inventory_fixture(2), now=1_800_000_000
            )
            fixture["sessions"][1]["row"] = fixture["sessions"][0]["row"]
            duplicate = base / "duplicate.json"
            duplicate.write_text(json.dumps(fixture), encoding="utf-8")
            proc = run(
                ["python3", CORE_PATH, "lookup", "--input", duplicate, "1"],
                check=False,
            )
            self.assertNotEqual(0, proc.returncode)
            self.assertIn("invalid or duplicate selectors", proc.stderr)

    def test_frozen_lookup_targets_exact_row_and_shpool_id(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(10), now=1_800_000_000
        )
        expected_tenth = fixture["sessions"][9]
        by_row = inventory_core.lookup(fixture, "10")
        self.assertIsNotNone(by_row)
        self.assertEqual(expected_tenth["shpool_id"], by_row["shpool_id"])
        by_id = inventory_core.lookup(fixture, "main2")
        self.assertIsNotNone(by_id)
        self.assertEqual(
            next(
                item["row"]
                for item in fixture["sessions"]
                if item["shpool_id"] == "main2"
            ),
            by_id["row"],
        )
        self.assertIsNone(inventory_core.lookup(fixture, "missing"))

    def test_recovery_commands_require_and_preserve_exact_uuid(self) -> None:
        exact = uuid_for(42)
        claude = inventory_core.recovery_spec("claude", exact, "/srv/a b")
        codex = inventory_core.recovery_spec("codex", exact)
        self.assertEqual(["claude", "--resume", exact], claude["argv"])
        self.assertEqual(
            ["codex", "--no-alt-screen", "resume", exact], codex["argv"]
        )
        joined = json.dumps([claude, codex]).casefold()
        for fallback in ("continue", "latest", "newest", "--last"):
            self.assertNotIn(fallback, joined)
        with self.assertRaises(ValueError):
            inventory_core.recovery_spec("codex", "not-an-id")

    def test_codex_print_mode_never_launches_and_has_no_fallback(self) -> None:
        helper = REPO / "bin" / "codex_resume_here"
        exact = uuid_for(88)
        proc = run([helper, "--print", "--cwd", "/srv/a b", exact])
        self.assertEqual(
            f"cd -- /srv/a\\ b && codex --no-alt-screen resume {exact}\n",
            proc.stdout,
        )
        invalid = run([helper, "--print", "latest"], check=False)
        self.assertEqual(2, invalid.returncode)

    def test_strict_live_refuses_a_last_known_good_cache_without_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".strict-live-", dir=REPO) as raw:
            base = Path(raw)
            state = base / "state"
            state.mkdir()
            cached = inventory_core.build_inventory(
                *inventory_fixture(1), now=1_800_000_000
            )
            (state / "inventory.json").write_text(
                json.dumps(cached), encoding="utf-8"
            )
            config = base / "inventory.json"
            config.write_text(
                json.dumps({"schema_version": 1, "state_dir": str(state)}),
                encoding="utf-8",
            )
            env = {
                "SESSION_KIT_CONFIG": str(config),
                "SESSION_KIT_SHPOOL_CMD": str(base / "missing-shpool"),
                "SESSION_KIT_STATE_DIR": str(state),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            direct = run(
                [
                    "python3",
                    CORE_PATH,
                    "snapshot",
                    "--strict-live",
                    "--no-write",
                ],
                env=env,
                check=False,
            )
            self.assertEqual(3, direct.returncode)
            self.assertEqual("", direct.stdout)
            self.assertIn("strict live snapshot unavailable", direct.stderr)
            wrapper = run(
                [REPO / "bin/shpool_status", "--strict-json"],
                env=env,
                check=False,
            )
            self.assertEqual(3, wrapper.returncode)
            self.assertEqual("", wrapper.stdout)
            guard = run(
                [REPO / "bin/shpool_status", "--guard-json"],
                env=env,
                check=False,
            )
            self.assertEqual(3, guard.returncode)
            self.assertEqual("", guard.stdout)
            self.assertIn("guard live snapshot unavailable", guard.stderr)


class InventoryRenderingTests(unittest.TestCase):
    def test_rows_hide_operational_ids_and_use_semantic_colors(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        fixture["sessions"][0]["needs_you"] = True
        fixture["sessions"][0]["agent_status"] = "needs your reply"
        with mock.patch.object(
            inventory_core, "_color_enabled", return_value=True
        ):
            rendered = inventory_core.render_inventory(fixture)
        visible = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
        for item in fixture["sessions"]:
            self.assertNotIn(f"[{item['display_shpool_id']}]", visible)
        self.assertIn("\x1b[36mClaude", rendered)
        self.assertIn("\x1b[36mCodex", rendered)
        self.assertIn("\x1b[32m 1\x1b[0m", rendered)
        self.assertIn("\x1b[32m 2\x1b[0m", rendered)
        self.assertIn("\x1b[33mneeds your reply", rendered)

    def test_renderer_used_by_picker_strips_csi_osc_and_all_control_bytes(self) -> None:
        malicious = {
            "stale": False,
            "sessions": [
                {
                    "row": 1,
                    "shpool_id": "main",
                    "display_shpool_id": "main",
                    "availability": "ready",
                    "provider": "codex",
                    "title": "safe\x1b[31mred\x1b]0;owned\x07title",
                    "cwd": "/srv/\x1b[2Jproject\tline\nbreak",
                    "process_age_seconds": 1,
                    "recent_output_age_seconds": 65,
                    "agent_status": "working",
                    "needs_you": False,
                    "subagents": [],
                }
            ],
            "outside_agents": [],
        }
        with mock.patch.dict(os.environ, {"SESSION_KIT_NO_COLOR": "1"}, clear=False):
            rendered = inventory_core.render_inventory(malicious)
        self.assertIn("safe", rendered)
        self.assertIn("owned", rendered)
        self.assertIn("recent output 1m ago", rendered)
        self.assertIn("process age 0m", rendered)
        self.assertNotIn("project", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertFalse(
            any(
                unicodedata.category(character).startswith("C")
                for character in rendered
                if character != "\n"
            ),
            repr(rendered),
        )

    def test_non_tty_no_color_rendering_at_supported_widths(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(10), now=1_800_000_000
        )
        fixture["sessions"][0]["title"] = "Wide 漢字 title 👋 with accents e\u0301"
        for width in (60, 80, 100, 160):
            with self.subTest(width=width):
                old_columns = os.environ.get("COLUMNS")
                old_no_color = os.environ.get("SESSION_KIT_NO_COLOR")
                try:
                    os.environ["COLUMNS"] = str(width)
                    os.environ["SESSION_KIT_NO_COLOR"] = "1"
                    rendered = inventory_core.render_inventory(fixture)
                finally:
                    if old_columns is None:
                        os.environ.pop("COLUMNS", None)
                    else:
                        os.environ["COLUMNS"] = old_columns
                    if old_no_color is None:
                        os.environ.pop("SESSION_KIT_NO_COLOR", None)
                    else:
                        os.environ["SESSION_KIT_NO_COLOR"] = old_no_color
                self.assertNotIn("\x1b", rendered)
                self.assertIn("Available to open", rendered)
                self.assertIn("Already open in another window", rendered)
                self.assertIn("Claude", rendered)
                self.assertIn("Codex", rendered)
                self.assertNotIn("open elsewhere", rendered)
                self.assertNotIn("in /srv/", rendered)
                self.assertTrue(
                    all(
                        inventory_core._display_width(line) <= width - 1
                        for line in rendered.splitlines()
                    ),
                    rendered,
                )

    def test_terminal_width_is_used_when_columns_is_not_exported(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        fixture["sessions"][0]["title"] = (
            "A screenshot-sized title that must not wrap in the SSH picker"
        )
        with (
            mock.patch.dict(
                os.environ, {"SESSION_KIT_NO_COLOR": "1"}, clear=False
            ),
            mock.patch.object(
                inventory_core.shutil,
                "get_terminal_size",
                return_value=os.terminal_size((72, 24)),
            ),
        ):
            os.environ.pop("COLUMNS", None)
            rendered = inventory_core.render_inventory(fixture)
        self.assertTrue(
            all(
                inventory_core._display_width(line) <= 71
                for line in rendered.splitlines()
            ),
            rendered,
        )

    def test_color_sequences_do_not_consume_the_display_cell_budget(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(4), now=1_800_000_000
        )
        fixture["sessions"][0]["title"] = (
            "Styled wide 漢字 title that reaches the terminal boundary"
        )
        for width in (60, 80, 100, 160):
            with self.subTest(width=width), mock.patch.dict(
                os.environ, {"COLUMNS": str(width)}, clear=False
            ), mock.patch.object(
                inventory_core, "_color_enabled", return_value=True
            ):
                rendered = inventory_core.render_inventory(fixture)
            self.assertIn("\x1b[", rendered)
            visible = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
            self.assertTrue(
                all(
                    inventory_core._display_width(line) <= width - 1
                    for line in visible.splitlines()
                ),
                visible,
            )


class ProviderLifecycleStateTests(unittest.TestCase):
    def test_lifecycle_caller_must_descend_from_exact_managed_shell(self) -> None:
        current_pid = os.getpid()
        shell_pid = current_pid + 10_000
        table = {
            current_pid: process(current_pid, shell_pid, "python3"),
            shell_pid: process(
                shell_pid,
                1,
                "bash",
                session_name="main2",
                start_ticks=987_654,
            ),
        }
        with (
            mock.patch.object(
                inventory_core, "_require_supported_platform", return_value="linux"
            ),
            mock.patch.object(
                inventory_core, "scan_process_table", return_value=table
            ),
        ):
            inventory_core._prove_lifecycle_caller(
                "main2", shell_pid, 987_654
            )
            with self.assertRaisesRegex(
                inventory_core.CollectionError, "outside the exact"
            ):
                inventory_core._prove_lifecycle_caller(
                    "main2", shell_pid, 987_655
                )
            table[shell_pid]["session_name"] = "main3"
            with self.assertRaisesRegex(
                inventory_core.CollectionError, "outside the exact"
            ):
                inventory_core._prove_lifecycle_caller(
                    "main2", shell_pid, 987_654
                )

    def test_state_is_private_minimal_and_first_input_is_permanent(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-state-", dir=REPO
        ) as raw:
            state_dir = Path(raw)
            session_id = "s20260730-220500-19"
            boot_id = "11111111-2222-3333-4444-555555555555"
            created = lifecycle_state.record_provider_exit(
                state_dir,
                session_id=session_id,
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                provider="codex",
                exit_code=0,
                input_tracking=True,
                now_monotonic_ns=1_800_000_000_000,
            )
            path = lifecycle_state.lifecycle_path(state_dir, session_id)
            self.assertIsNotNone(path)
            assert path is not None
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertNotIn(session_id, path.name)
            self.assertNotEqual(
                hashlib.sha256(session_id.encode()).hexdigest(),
                path.stem,
            )
            payload = path.read_text(encoding="utf-8")
            self.assertNotIn(session_id, payload)
            self.assertNotIn("/srv/", payload)
            self.assertNotIn("00000000-0000-4000", payload)
            self.assertTrue(created["input_tracking"])
            first = lifecycle_state.update_state(
                state_dir,
                session_id=session_id,
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                event="user-input",
            )
            second = lifecycle_state.update_state(
                state_dir,
                session_id=session_id,
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                event="user-input",
            )
            self.assertTrue(first["user_input_after_exit"])
            self.assertTrue(second["user_input_after_exit"])
            kept = lifecycle_state.update_state(
                state_dir,
                session_id=session_id,
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                event="keep",
                keep=True,
            )
            self.assertTrue(kept["keep"])
            with self.assertRaisesRegex(
                inventory_core.CollectionError, "generation changed"
            ):
                lifecycle_state.update_state(
                    state_dir,
                    session_id=session_id,
                    boot_id=boot_id,
                    shell_pid=999,
                    shell_start_ticks=456,
                    event="user-input",
                )
            self.assertEqual(
                0,
                lifecycle_state.prune_inactive_state(
                    state_dir, [session_id]
                ),
            )
            self.assertEqual(
                1,
                lifecycle_state.prune_inactive_state(state_dir, []),
            )
            self.assertFalse(path.exists())

    def test_exact_exit_overlay_keeps_shell_live_and_recovery_available(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        active = inventory_core.build_inventory(
            *fixture, now=1_800_000_000
        )
        expected_uuid = uuid_for(1)
        root_pid = 1001
        provider_pid = 2001
        del fixture[2][provider_pid]
        fixture[3] = {}
        idle = inventory_core.build_inventory(
            *fixture, now=1_800_000_001
        )
        self.assertEqual("shell", idle["sessions"][0]["provider"])
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-overlay-", dir=REPO
        ) as raw:
            state_dir = Path(raw)
            lifecycle_state.persist_last_exact(
                idle,
                active,
                state_dir=state_dir,
                boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            )
            cache_after_race = copy.deepcopy(idle)
            lifecycle_state.record_provider_exit(
                state_dir,
                session_id="main",
                boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                shell_pid=root_pid,
                shell_start_ticks=fixture[2][root_pid]["start_ticks"],
                provider="codex",
                exit_code=0,
                input_tracking=True,
                now_monotonic_ns=1_800_000_000_500,
            )
            lifecycle_state.apply_provider_exit_states(
                idle,
                cache_after_race,
                state_dir=state_dir,
                boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            )
        item = idle["sessions"][0]
        self.assertEqual("shell", item["provider"])
        self.assertEqual("codex", item["display_provider"])
        self.assertEqual("provider exited", item["agent_status"])
        self.assertEqual(active["sessions"][0]["title"], item["title"])
        self.assertEqual(expected_uuid, item["recovery"]["uuid"])
        self.assertEqual(
            {"provider": "codex", "uuid": expected_uuid},
            item["_terminal_identity_hint"],
        )
        self.assertFalse(item["needs_you"])

    def test_reopen_executes_only_generation_bound_exact_recovery(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-reopen-", dir=REPO
        ) as raw:
            state_dir = Path(raw)
            boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            lifecycle_state.record_provider_exit(
                state_dir,
                session_id="main2",
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                provider="codex",
                exit_code=0,
                input_tracking=True,
                now_monotonic_ns=500,
            )
            lifecycle_state.update_state(
                state_dir,
                session_id="main2",
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                event="user-input",
            )
            recovery = inventory_core.recovery_spec(
                "codex", uuid_for(2), str(state_dir)
            )
            item = {
                "provider": "shell",
                "exited_provider": "codex",
                "shpool_shell": {
                    "pid": 123,
                    "process_start_ticks": 456,
                },
                "recovery": recovery,
            }
            args = argparse.Namespace(lifecycle_action="reopen")
            completed = subprocess.CompletedProcess(
                recovery["argv"], 0
            )
            with (
                mock.patch.object(
                    inventory_core,
                    "_lifecycle_environment",
                    return_value=(state_dir, "main2", boot_id, 123, 456),
                ),
                mock.patch.object(
                    inventory_core, "_prove_lifecycle_caller"
                ),
                mock.patch.object(
                    inventory_core, "load_config", return_value={"state_dir": state_dir}
                ),
                mock.patch.object(
                    inventory_core, "snapshot", return_value={"sessions": [item]}
                ),
                mock.patch.object(
                    inventory_core, "guard_live_inventory", return_value=True
                ),
                mock.patch.object(
                    inventory_core, "lookup", return_value=item
                ),
                mock.patch.object(
                    inventory_core.subprocess,
                    "run",
                    return_value=completed,
                ) as launched,
            ):
                self.assertEqual(0, inventory_core._lifecycle_command(args))
            # The session's launch theme rides on every reopen so the window
            # keeps its color identity; the color is the stable identity hash
            # for this uuid.
            expected_theme = inventory_core.session_color("codex", uuid_for(2), {})
            launched.assert_called_once_with(
                [
                    "codex",
                    "-c",
                    f'tui.theme="sk-{expected_theme}"',
                    "-c",
                    "check_for_update_on_startup=false",
                    "--no-alt-screen",
                    "resume",
                    uuid_for(2),
                ],
                cwd=str(state_dir),
                check=False,
            )
            retained = lifecycle_state.load_state(state_dir, "main2")
            assert retained is not None
            self.assertTrue(retained["user_input_after_exit"])

    def test_claude_reopen_passes_hydrated_name_to_native_cli(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-claude-reopen-", dir=REPO
        ) as raw:
            base = Path(raw)
            state_dir = base / "state"
            home = base / "home"
            transcript_dir = home / ".claude" / "projects" / "-srv-project"
            state_dir.mkdir()
            transcript_dir.mkdir(parents=True)
            exact = uuid_for(3)
            (transcript_dir / f"{exact}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "agent-name",
                        "agentName": "Egypt Presidents",
                        "sessionId": exact,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            lifecycle_state.record_provider_exit(
                state_dir,
                session_id="main3",
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                provider="claude",
                exit_code=0,
                input_tracking=True,
                now_monotonic_ns=500,
            )
            recovery = inventory_core.recovery_spec(
                "claude", exact, str(state_dir)
            )
            item = {
                "provider": "shell",
                "exited_provider": "claude",
                "shpool_shell": {
                    "pid": 123,
                    "process_start_ticks": 456,
                },
                "recovery": recovery,
            }
            args = argparse.Namespace(lifecycle_action="reopen")
            completed = subprocess.CompletedProcess(recovery["argv"], 0)
            with (
                mock.patch.dict(os.environ, {"HOME": os.fspath(home)}),
                mock.patch.object(
                    inventory_core,
                    "_lifecycle_environment",
                    return_value=(state_dir, "main3", boot_id, 123, 456),
                ),
                mock.patch.object(inventory_core, "_prove_lifecycle_caller"),
                mock.patch.object(
                    inventory_core, "load_config", return_value={"state_dir": state_dir}
                ),
                mock.patch.object(
                    inventory_core, "snapshot", return_value={"sessions": [item]}
                ),
                mock.patch.object(
                    inventory_core, "guard_live_inventory", return_value=True
                ),
                mock.patch.object(inventory_core, "lookup", return_value=item),
                mock.patch.object(
                    inventory_core.subprocess,
                    "run",
                    return_value=completed,
                ) as launched,
            ):
                self.assertEqual(0, inventory_core._lifecycle_command(args))
            launched.assert_called_once_with(
                [
                    "claude",
                    "--name",
                    "Egypt Presidents",
                    "--resume",
                    exact,
                ],
                cwd=str(state_dir),
                check=False,
            )


if __name__ == "__main__":
    unittest.main()
