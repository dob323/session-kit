from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.support import REPO


CORE_PATH = REPO / "lib" / "session_inventory.py"
CORE_SPEC = importlib.util.spec_from_file_location("macos_inventory", CORE_PATH)
assert CORE_SPEC is not None and CORE_SPEC.loader is not None
inventory_core = importlib.util.module_from_spec(CORE_SPEC)
sys.modules[CORE_SPEC.name] = inventory_core
CORE_SPEC.loader.exec_module(inventory_core)


def procargs(*argv: str, environ: tuple[str, ...] = ()) -> bytes:
    executable = argv[0].encode()
    values = [executable, b"", *(value.encode() for value in argv)]
    values.extend(value.encode() for value in environ)
    return struct.pack("=i", len(argv)) + b"\0".join(values) + b"\0"


def bsd_info(pid: int, ppid: int, generation: int, name: bytes) -> object:
    info = inventory_core._DarwinBsdInfo()
    info.pbi_pid = pid
    info.pbi_ppid = ppid
    info.pbi_start_tvsec = generation // 1_000_000
    info.pbi_start_tvusec = generation % 1_000_000
    info.pbi_name = name
    return info


class DarwinParserTests(unittest.TestCase):
    def test_procargs_parser_separates_argv_and_environment(self) -> None:
        payload = procargs(
            "/opt/homebrew/bin/codex",
            "--no-alt-screen",
            environ=(
                "SHPOOL_SESSION_NAME=s20260729-120000-1",
                "CODEX_THREAD_ID=00000000-0000-4000-8000-000000000018",
            ),
        )
        argv, environ = inventory_core._parse_darwin_procargs2(payload)
        self.assertEqual(
            argv, ["/opt/homebrew/bin/codex", "--no-alt-screen"]
        )
        self.assertEqual(
            environ["SHPOOL_SESSION_NAME"], "s20260729-120000-1"
        )
        self.assertEqual(
            environ["CODEX_THREAD_ID"],
            "00000000-0000-4000-8000-000000000018",
        )

    def test_process_scan_binds_environment_to_exact_generation(self) -> None:
        generation = 1_752_000_000_123_456
        rows = {
            10: bsd_info(10, 1, generation, b"shpool"),
            20: bsd_info(20, 10, generation + 1, b"bash"),
            30: bsd_info(30, 20, generation + 2, b"codex"),
        }
        arguments = {
            10: procargs("/opt/homebrew/bin/shpool", "daemon"),
            20: procargs(
                "/bin/bash",
                environ=(
                    "SHPOOL_SESSION_NAME=s20260729-120000-1",
                    "PWD=/srv/session-kit",
                ),
            ),
            30: procargs(
                "/opt/homebrew/bin/codex",
                environ=(
                    "CODEX_THREAD_ID="
                    "00000000-0000-4000-8000-000000000018",
                ),
            ),
        }
        table = inventory_core.scan_darwin_process_table(
            32,
            pids=[10, 20, 30],
            bsd_reader=rows.__getitem__,
            args_reader=arguments.__getitem__,
        )
        self.assertEqual(table[20]["session_name"], "s20260729-120000-1")
        self.assertEqual(table[20]["cwd"], "/srv/session-kit")
        self.assertEqual(table[30]["start_ticks"], generation + 2)
        self.assertEqual(
            table[30]["generation_kind"], "darwin-start-usec"
        )
        self.assertEqual(
            table[30]["codex_thread_id"],
            "00000000-0000-4000-8000-000000000018",
        )

    def test_process_scan_discards_generation_that_changes_mid_read(self) -> None:
        first = bsd_info(30, 20, 1_752_000_000_000_001, b"codex")
        second = bsd_info(30, 20, 1_752_000_000_000_002, b"codex")
        sequence = iter((first, second))
        table = inventory_core.scan_darwin_process_table(
            4,
            pids=[30],
            bsd_reader=lambda _pid: next(sequence),
            args_reader=lambda _pid: procargs("/opt/homebrew/bin/codex"),
        )
        # The changing generation cannot be published as process identity, but
        # the process must remain in its known parent tree as unreadable.  A
        # missing row would let a reader mistake churn for proof of absence.
        self.assertEqual({30}, set(table))
        self.assertEqual(30, table[30]["pid"])
        self.assertEqual(20, table[30]["ppid"])
        self.assertEqual(-1, table[30]["start_ticks"])
        self.assertEqual([], table[30]["cmdline"])
        self.assertTrue(table[30]["argv_unreadable"])
        self.assertTrue(table[30]["environ_unreadable"])

    def test_codex_uuid_comes_only_from_exact_process_environment(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000018"
        table = {
            30: {
                "cmdline": ["/opt/homebrew/bin/codex"],
                "comm": "codex",
                "codex_thread_id": uuid,
            },
            31: {
                "cmdline": ["/opt/homebrew/bin/codex"],
                "comm": "codex",
                "codex_thread_id": "not-a-uuid",
            },
        }
        with mock.patch.object(
            inventory_core, "_runtime_platform", return_value="darwin"
        ), mock.patch.object(
            inventory_core,
            "codex_rollout_by_uuid",
            return_value=[
                {
                    "session_id": uuid,
                    "id": uuid,
                    "source": "cli",
                    "_turn_state": "idle",
                }
            ],
        ):
            indexed = inventory_core.index_codex_processes(
                table, Path("/proc"), Path("/unused")
            )
        self.assertEqual(indexed[30][0]["session_id"], uuid)
        self.assertEqual(indexed[30][0]["_turn_state"], "idle")
        self.assertEqual(indexed[31], [])

    def test_native_claude_session_argument_is_exact_identity(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000019"
        session_name = "s20260729-120000-1"
        process_table = {
            10: {
                "pid": 10,
                "ppid": 1,
                "start_ticks": 100,
                "cmdline": ["/opt/homebrew/bin/shpool", "daemon"],
                "comm": "shpool",
            },
            20: {
                "pid": 20,
                "ppid": 10,
                "start_ticks": 200,
                "cmdline": ["-bash"],
                "comm": "bash",
                "session_name": session_name,
            },
            30: {
                "pid": 30,
                "ppid": 20,
                "start_ticks": 300,
                "cmdline": ["claude", "--session-id", uuid],
                "comm": "claude.exe",
                "cwd": "/Users/test/project",
            },
        }
        result = inventory_core.build_inventory(
            {
                "sessions": [
                    {
                        "name": session_name,
                        "status": "Disconnected",
                        "started_at_unix_ms": 1_800_000_000_000,
                    }
                ]
            },
            [],
            process_table,
            {},
            ({}, {}),
            {},
            now=1_800_000_001,
        )
        row = result["sessions"][0]
        self.assertEqual(row["provider"], "claude")
        self.assertEqual(row["identity"]["uuid"], uuid)
        self.assertEqual(row["identity"]["confidence"], "exact")
        self.assertTrue(row["mutation_allowed"])

    def test_unused_native_codex_reports_provider_working_directory(self) -> None:
        session_name = "s20260729-120000-2"
        process_table = {
            10: {
                "pid": 10,
                "ppid": 1,
                "start_ticks": 100,
                "cmdline": ["/opt/homebrew/bin/shpool", "daemon"],
                "comm": "shpool",
            },
            20: {
                "pid": 20,
                "ppid": 10,
                "start_ticks": 200,
                "cmdline": ["-bash"],
                "comm": "bash",
                "session_name": session_name,
                "cwd": "",
            },
            30: {
                "pid": 30,
                "ppid": 20,
                "start_ticks": 300,
                "cmdline": ["codex", "--no-alt-screen"],
                "comm": "codex",
                "cwd": "/Users/test/project",
            },
        }
        result = inventory_core.build_inventory(
            {
                "sessions": [
                    {
                        "name": session_name,
                        "status": "Disconnected",
                        "started_at_unix_ms": 1_800_000_000_000,
                    }
                ]
            },
            [],
            process_table,
            {},
            ({}, {}),
            {},
            now=1_800_000_001,
        )
        row = result["sessions"][0]
        self.assertEqual(row["provider"], "unknown")
        self.assertEqual(row["display_provider"], "codex")
        self.assertEqual(row["cwd"], "/Users/test/project")
        self.assertEqual(row["title"], "Codex started, no messages yet")

    def test_darwin_age_uses_epoch_microseconds(self) -> None:
        now = time.time()
        table = {
            7: {
                "start_ticks": int((now - 42) * 1_000_000),
                "generation_kind": "darwin-start-usec",
            }
        }
        self.assertIn(inventory_core._process_age(7, table, now), {41, 42})

    def test_macos_is_a_supported_native_platform(self) -> None:
        with (
            mock.patch.object(
                inventory_core, "_runtime_platform", return_value="darwin"
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(inventory_core._require_supported_platform(), "darwin")


class DarwinSupportedCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".macos-preview-", dir=REPO)
        self.base = Path(self.temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        uname = self.bin / "uname"
        uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
        uname.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.base / "home"),
            "SESSION_KIT_TESTING": "1",
            "SESSION_KIT_TEST_PLATFORM": "Darwin",
            "SESSION_KIT_STATE_DIR": str(self.base / "state"),
        }
        self.bash = os.environ.get("SESSION_KIT_TEST_BASH", "bash")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_bash(self, path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.bash, str(path), *args],
            cwd=REPO,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_maintenance_commands_no_longer_have_preview_refusals(self) -> None:
        for relative, message in (
            ("bin/shpool_reaper", "reaper is unavailable"),
            ("bin/session_kit_watchdog", "watchdog repair is unavailable in the macOS preview"),
            ("bin/sp", "prune is unavailable"),
        ):
            with self.subTest(path=relative):
                self.assertNotIn(
                    message,
                    (REPO / relative).read_text(encoding="utf-8"),
                )

    def test_watchdog_repair_requires_linux_thread_evidence(self) -> None:
        env = {**self.env, "SESSION_KIT_WATCHDOG_MODE": "repair"}
        result = subprocess.run(
            [self.bash, str(REPO / "bin/session_kit_watchdog"), "--once"],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires Linux daemon-thread evidence", result.stderr)

    def test_darwin_mutex_is_file_backed_and_explicitly_released(self) -> None:
        lock = self.base / "create.lock"
        command = (
            f"source {REPO / 'bin/session_kit_common'}; "
            f"exec 9>{lock}; sk_lock_acquire 9 {lock}; "
            f"test -f {lock}; "
            "sk_lock_release 9; test ! -e " + str(lock) + ".darwin-lock"
        )
        result = subprocess.run(
            [self.bash, "-c", command],
            cwd=REPO,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
