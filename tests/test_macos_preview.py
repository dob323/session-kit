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
        self.assertEqual(table, {})

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
        ):
            indexed = inventory_core.index_codex_processes(
                table, Path("/proc"), Path("/unused")
            )
        self.assertEqual(indexed[30][0]["session_id"], uuid)
        self.assertEqual(indexed[30][0]["_turn_state"], "state unavailable")
        self.assertEqual(indexed[31], [])

    def test_darwin_age_uses_epoch_microseconds(self) -> None:
        now = time.time()
        table = {
            7: {
                "start_ticks": int((now - 42) * 1_000_000),
                "generation_kind": "darwin-start-usec",
            }
        }
        self.assertIn(inventory_core._process_age(7, table, now), {41, 42})

    def test_macos_requires_explicit_preview_opt_in(self) -> None:
        with (
            mock.patch.object(
                inventory_core, "_runtime_platform", return_value="darwin"
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            with self.assertRaisesRegex(
                inventory_core.CollectionError, "experimental preview"
            ):
                inventory_core._require_supported_platform()


class DarwinFailClosedCommandTests(unittest.TestCase):
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
            "SESSION_KIT_MACOS_PREVIEW": "1",
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

    def test_reaper_watchdog_and_prune_refuse_darwin(self) -> None:
        cases = (
            (REPO / "bin/shpool_reaper", (), "reaper is unavailable"),
            (REPO / "bin/session_kit_watchdog", (), "watchdog repair is unavailable"),
            (REPO / "bin/sp", ("prune",), "prune is unavailable"),
        )
        for path, args, message in cases:
            with self.subTest(path=path.name):
                result = self.run_bash(path, *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        self.assertFalse((self.base / "state").exists())

    def test_darwin_mutex_is_atomic_and_explicitly_released(self) -> None:
        lock = self.base / "create.lock"
        command = (
            f"source {REPO / 'bin/session_kit_common'}; "
            f"exec 9>{lock}; sk_lock_acquire 9 {lock}; "
            "test -d " + str(lock) + ".darwin-lock; "
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
