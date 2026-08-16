from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from sessionkit_inventory import reaper  # noqa: E402


SESSION_ID = "s20260730-220500-19"
UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
HOUR_NS = 3600 * 1_000_000_000
TEST_NOW_NS = 100 * HOUR_NS
REAPER = REPO / "bin" / "shpool_reaper"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def fixture() -> tuple[dict, dict, dict]:
    shpool = {
        "sessions": [
            {
                "name": SESSION_ID,
                "status": "Disconnected",
                "started_at_unix_ms": 1_800_000_000_000,
                "last_disconnected_at_unix_ms": 1_800_000_100_000,
            }
        ]
    }
    inventory = {
        "source": "live",
        "stale": False,
        "warnings": [],
        "daemon_generation": {
            "pid": 10,
            "process_start_ticks": 1_000,
        },
        "sessions": [
            {
                "shpool_id_raw": SESSION_ID,
                "started_at_unix_ms": 1_800_000_000_000,
                "shpool_status": "Disconnected",
                "availability": "ready",
                "provider": "shell",
                "exited_provider": "codex",
                "agent_status": "provider exited",
                "needs_you": False,
                "provider_exit_input_tracking": True,
                "user_input_after_provider_exit": False,
                "provider_exit_keep": False,
                "provider_exited_at_monotonic_ns": 10 * HOUR_NS,
                "shpool_shell": {
                    "pid": 100,
                    "process_start_ticks": 10_000,
                },
                "recovery": {
                    "available": True,
                    "provider": "codex",
                    "uuid": UUID,
                },
                "exited_identity": {
                    "confidence": "historical-exact",
                    "uuid": UUID,
                },
                "mutation_allowed": True,
            }
        ],
    }
    facts = {
        SESSION_ID: {
            "shell_pid": 100,
            "shell_start_ticks": 10_000,
            "empty": True,
        }
    }
    return shpool, inventory, facts


class AutoClosePlanningTests(unittest.TestCase):
    def test_environment_planner_refuses_daemon_identity_outside_guard(self) -> None:
        shpool, inventory, _ = fixture()
        inventory["daemon_generation"] = {
            "pid": 20,
            "process_start_ticks": 2_000,
        }
        with tempfile.TemporaryDirectory(prefix=".reaper-bound-", dir=REPO) as raw:
            root = Path(raw)
            state = root / "state"
            proc = root / "proc"
            state.mkdir()
            proc.mkdir()
            environment = {
                "HOME": os.fspath(root / "home"),
                "XDG_STATE_HOME": os.fspath(root / "xdg-state"),
                "XDG_DATA_HOME": os.fspath(root / "xdg-data"),
                "XDG_CONFIG_HOME": os.fspath(root / "xdg-config"),
                "XDG_CACHE_HOME": os.fspath(root / "xdg-cache"),
                "XDG_RUNTIME_DIR": os.fspath(root / "xdg-runtime"),
                "SESSION_KIT_CONFIG": os.fspath(
                    root / "xdg-config/session-kit.json"
                ),
                "SK_REAPER_SHPOOL_JSON": json.dumps(shpool),
                "SK_REAPER_INVENTORY_JSON": json.dumps(inventory),
                "SK_REAPER_STATE_DIR": os.fspath(state),
                "SK_REAPER_PROC_ROOT": os.fspath(proc),
                # The retired first-name result must not choose PID 10.
                "SK_REAPER_DAEMON_PID": "10",
                "SK_REAPER_AUTO_CLOSE_HOURS": "72",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(
                    reaper.CollectionError,
                    "bound shpool daemon generation changed",
                ):
                    reaper.plan_from_environment(write_state=False)

    def test_requires_continuous_exact_proof_for_full_72_hours(self) -> None:
        shpool, inventory, facts = fixture()
        first_at = 20 * HOUR_NS
        observations, candidates = reaper.plan_auto_close(
            shpool,
            inventory,
            facts,
            None,
            now_monotonic_ns=first_at,
            minimum_age_ns=72 * HOUR_NS,
        )
        self.assertEqual([], candidates["candidates"])
        before = reaper.plan_auto_close(
            shpool,
            inventory,
            facts,
            observations,
            now_monotonic_ns=first_at + 72 * HOUR_NS - 1,
            minimum_age_ns=72 * HOUR_NS,
        )[1]
        self.assertEqual([], before["candidates"])
        ready = reaper.plan_auto_close(
            shpool,
            inventory,
            facts,
            observations,
            now_monotonic_ns=first_at + 72 * HOUR_NS,
            minimum_age_ns=72 * HOUR_NS,
        )[1]
        self.assertEqual(1, len(ready["candidates"]))
        candidate = ready["candidates"][0]
        self.assertEqual(SESSION_ID, candidate["shpool_id"])
        self.assertEqual(100, candidate["shell_pid"])
        self.assertEqual(10_000, candidate["shell_start_ticks"])
        self.assertEqual(UUID, candidate["provider_uuid"])

    def test_unreadable_daemon_child_environment_refuses_the_whole_scan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="reaper-environ-") as raw:
            proc_root = Path(raw)
            for pid, ppid, comm in ((10, 1, "shpool"), (100, 10, "bash")):
                entry = proc_root / str(pid)
                entry.mkdir()
                fields = ["S", str(ppid), *(["0"] * 17), str(pid * 100)]
                (entry / "stat").write_text(
                    f"{pid} ({comm}) {' '.join(fields)}\n", encoding="utf-8"
                )
                (entry / "comm").write_text(f"{comm}\n", encoding="utf-8")
                (entry / "cmdline").write_bytes(b"bash\0")
                (entry / "environ").write_bytes(b"SHPOOL_SESSION_NAME=main\0")
            # The daemon's own child answers nothing: a live shell that cannot
            # name itself must never be read as a session with no shell.
            child_environ = proc_root / "100" / "environ"
            child_environ.unlink()
            child_environ.mkdir()
            with self.assertRaises(reaper.CollectionError) as refused:
                reaper.scan_shell_facts(proc_root, 10)
        self.assertIn("unreadable environment", str(refused.exception))

    def test_shell_less_phantom_clears_first_pass_and_requires_exact_shape(
        self,
    ) -> None:
        phantom_id = "s20260730-012406-838224"
        shpool = {
            "sessions": [
                {
                    "name": phantom_id,
                    "status": "Disconnected",
                    "started_at_unix_ms": 1_800_000_000_000,
                    "last_disconnected_at_unix_ms": 1_800_000_100_000,
                }
            ]
        }
        item = {
            "shpool_id_raw": phantom_id,
            "provider": "unknown",
            "mutation_allowed": False,
            "mutation_rejection_reason": "missing-shell-generation",
            "shpool_shell": None,
            "subagents": [],
            "identity": {"uuid": None, "pid": None},
            "diagnostics": [
                f"expected one daemon child for {phantom_id!r}, found 0",
                "identity candidates: Claude=0, Codex=0",
            ],
        }
        inventory = {
            "source": "live",
            "stale": False,
            "warnings": [],
            "sessions": [item],
        }
        first_at = 20 * HOUR_NS
        observations, candidates = reaper.plan_auto_close(
            shpool,
            inventory,
            {},
            None,
            now_monotonic_ns=first_at,
            minimum_age_ns=72 * HOUR_NS,
        )
        # Exact process absence needs no aging window: one complete pass is
        # enough, unlike a live empty shell waiting on provider-exit policy.
        self.assertEqual(1, len(candidates["candidates"]))
        self.assertEqual(
            "shell-less-phantom", candidates["candidates"][0]["class"]
        )
        self.assertIn(phantom_id, observations["observations"])
        ready = reaper.plan_auto_close(
            shpool,
            inventory,
            {},
            observations,
            now_monotonic_ns=first_at + 72 * HOUR_NS,
            minimum_age_ns=72 * HOUR_NS,
        )[1]
        self.assertEqual(1, len(ready["candidates"]))
        candidate = ready["candidates"][0]
        self.assertEqual("shell-less-phantom", candidate["class"])
        self.assertEqual(phantom_id, candidate["shpool_id"])
        # ANY surviving shell fact, live pid, or different quarantine reason
        # disqualifies the phantom shape entirely.
        for breaker in (
            lambda: {"facts": {phantom_id: {"shell_pid": 1, "shell_start_ticks": 2, "empty": True}}},
            lambda: {"item": {"identity": {"uuid": None, "pid": 4242}}},
            lambda: {"item": {"mutation_rejection_reason": "outside-shpool"}},
            lambda: {"item": {"mutation_allowed": True}},
            lambda: {"item": {"diagnostics": ["one daemon child present"]}},
            # An unreadable environment means the shell may be alive and
            # merely unable to name itself.
            lambda: {
                "item": {
                    "diagnostics": [
                        f"expected one daemon child for {phantom_id!r}, found 0",
                        "1 daemon child process(es) have an unreadable "
                        "environment; session names cannot be proven",
                    ]
                }
            },
            lambda: {"item": {"subagents": [{"pid": 7}]}},
        ):
            change = breaker()
            broken_item = {**item, **change.get("item", {})}
            broken_inventory = {**inventory, "sessions": [broken_item]}
            result = reaper.plan_auto_close(
                shpool,
                broken_inventory,
                change.get("facts", {}),
                observations,
                now_monotonic_ns=first_at + 72 * HOUR_NS,
                minimum_age_ns=72 * HOUR_NS,
            )[1]
            self.assertEqual(
                [], result["candidates"], msg=f"breaker leaked: {change}"
            )

    def test_every_authorized_safety_predicate_fails_closed(self) -> None:
        cases = {
            "attached": lambda s, i, f: (
                s["sessions"][0].update(status="Attached"),
                i["sessions"][0].update(
                    shpool_status="Attached", availability="attached"
                ),
            ),
            "needs-reply": lambda s, i, f: i["sessions"][0].update(
                needs_you=True
            ),
            "input-untracked": lambda s, i, f: i["sessions"][0].update(
                provider_exit_input_tracking=False
            ),
            "human-returned": lambda s, i, f: i["sessions"][0].update(
                user_input_after_provider_exit=True
            ),
            "keep": lambda s, i, f: i["sessions"][0].update(
                provider_exit_keep=True
            ),
            "provider-active": lambda s, i, f: i["sessions"][0].update(
                provider="codex"
            ),
            "identity-unknown": lambda s, i, f: i["sessions"][0].update(
                exited_identity={"confidence": "unknown", "uuid": UUID}
            ),
            "recovery-unavailable": lambda s, i, f: i["sessions"][0].update(
                recovery={"available": False}
            ),
            "shell-not-empty": lambda s, i, f: f[SESSION_ID].update(
                empty=False
            ),
            "shell-replaced": lambda s, i, f: f[SESSION_ID].update(
                shell_start_ticks=10_001
            ),
            "legacy-name": lambda s, i, f: (
                s["sessions"][0].update(name="main2"),
                i["sessions"][0].update(shpool_id_raw="main2"),
                f.__setitem__("main2", f.pop(SESSION_ID)),
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                shpool, inventory, facts = fixture()
                mutate(shpool, inventory, facts)
                observations, candidates = reaper.plan_auto_close(
                    shpool,
                    inventory,
                    facts,
                    None,
                    now_monotonic_ns=100 * HOUR_NS,
                    minimum_age_ns=72 * HOUR_NS,
                )
                self.assertEqual({}, observations["observations"])
                self.assertEqual([], candidates["candidates"])

        for source, warnings in (("cache", []), ("live", ["blind"])):
            with self.subTest(inventory_source=source, warnings=warnings):
                shpool, inventory, facts = fixture()
                inventory["source"] = source
                inventory["warnings"] = warnings
                with self.assertRaisesRegex(
                    reaper.CollectionError, "guard-live"
                ):
                    reaper.plan_auto_close(
                        shpool,
                        inventory,
                        facts,
                        None,
                        now_monotonic_ns=100 * HOUR_NS,
                        minimum_age_ns=72 * HOUR_NS,
                    )

    def test_generation_or_disconnect_change_restarts_observation(self) -> None:
        shpool, inventory, facts = fixture()
        first, _ = reaper.plan_auto_close(
            shpool,
            inventory,
            facts,
            None,
            now_monotonic_ns=20 * HOUR_NS,
            minimum_age_ns=72 * HOUR_NS,
        )
        shpool["sessions"][0]["last_disconnected_at_unix_ms"] += 1
        restarted, candidates = reaper.plan_auto_close(
            shpool,
            inventory,
            facts,
            first,
            now_monotonic_ns=100 * HOUR_NS,
            minimum_age_ns=72 * HOUR_NS,
        )
        self.assertEqual([], candidates["candidates"])
        self.assertEqual(
            100 * HOUR_NS,
            restarted["observations"][SESSION_ID][
                "first_verified_monotonic_ns"
            ],
        )

    def test_process_scan_accepts_only_one_exact_empty_daemon_child(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".reaper-proc-", dir=REPO
        ) as raw:
            proc = Path(raw)

            def write_process(
                pid: int,
                ppid: int,
                comm: str,
                environ: bytes = b"",
            ) -> None:
                root = proc / str(pid)
                root.mkdir()
                fields = ["S", str(ppid)] + ["0"] * 17 + [str(pid * 100)]
                (root / "stat").write_text(
                    f"{pid} ({comm}) {' '.join(fields)}\n",
                    encoding="utf-8",
                )
                (root / "comm").write_text(comm + "\n", encoding="utf-8")
                (root / "cmdline").write_bytes(comm.encode() + b"\0")
                (root / "environ").write_bytes(environ)

            write_process(10, 1, "shpool")
            write_process(
                100,
                10,
                "bash",
                f"SHPOOL_SESSION_NAME={SESSION_ID}\0".encode(),
            )
            facts = reaper.scan_shell_facts(proc, 10)
            self.assertEqual(
                {
                    "shell_pid": 100,
                    "shell_start_ticks": 10_000,
                    "empty": True,
                },
                facts[SESSION_ID],
            )
            write_process(200, 100, "sleep")
            self.assertFalse(
                reaper.scan_shell_facts(proc, 10)[SESSION_ID]["empty"]
            )


class AutoCloseCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix=".reaper-auto-command-", dir=REPO
        )
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.state = self.base / "state"
        self.proc = self.base / "proc"
        self.bin = self.base / "bin"
        for path in (self.home, self.state, self.proc, self.bin):
            path.mkdir(mode=0o700)
        self.shpool_state = self.base / "shpool.json"
        self.shpool_state.write_text(
            json.dumps(fixture()[0]) + "\n", encoding="utf-8"
        )
        self.kill_log = self.base / "kill.log"
        self.inventory = self.base / "inventory.json"
        self.inventory.write_text(
            json.dumps(fixture()[1]) + "\n", encoding="utf-8"
        )
        write_executable(
            self.bin / "shpool",
            """#!/usr/bin/env python3
import json,os,pathlib,shutil,sys
state=pathlib.Path(os.environ["FAKE_SHPOOL_STATE"])
value=json.loads(state.read_text())
args=sys.argv[1:]
if args == ["list","--json"]:
    print(json.dumps(value))
    raise SystemExit(0)
if args == ["attach","--cmd","/bin/true","--dir","/",os.environ.get("FAKE_TARGET", "")]:
    target=args[-1]
    before=len(value["sessions"])
    value["sessions"]=[
        row for row in value["sessions"] if row.get("name") != target
    ]
    if len(value["sessions"]) == before:
        raise SystemExit(4)
    state.write_text(json.dumps(value)+"\\n")
    pathlib.Path(os.environ["FAKE_KILL_LOG"]).write_text("attach-exit "+target+"\\n")
    raise SystemExit(0)
if len(args) == 2 and args[0] == "kill":
    if not pathlib.Path(os.environ["SESSION_KIT_TEST_EXACT_SIGNAL_LOG"]).is_file():
        raise SystemExit(89)
    before=len(value["sessions"])
    value["sessions"]=[
        row for row in value["sessions"] if row.get("name") != args[1]
    ]
    if len(value["sessions"]) == before:
        raise SystemExit(4)
    state.write_text(json.dumps(value)+"\\n")
    pathlib.Path(os.environ["FAKE_KILL_LOG"]).write_text(args[1]+"\\n")
    shutil.rmtree(
        pathlib.Path(os.environ["SESSION_KIT_PROC_ROOT"])/"100",
        ignore_errors=True,
    )
    raise SystemExit(0)
raise SystemExit(2)
""",
        )
        write_executable(
            self.bin / "status",
            """#!/usr/bin/env python3
import pathlib,sys
if sys.argv[1:] != ["--guard-json"]:
    raise SystemExit(2)
print(pathlib.Path(__import__("os").environ["FAKE_INVENTORY"]).read_text(),end="")
""",
        )
        self._write_process(10, 1, "shpool")
        self._write_process(
            100,
            10,
            "bash",
            f"SHPOOL_SESSION_NAME={SESSION_ID}\0".encode(),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_process(
        self,
        pid: int,
        ppid: int,
        comm: str,
        environ: bytes = b"",
    ) -> None:
        root = self.proc / str(pid)
        root.mkdir()
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(pid * 100)]
        (root / "stat").write_text(
            f"{pid} ({comm}) {' '.join(fields)}\n",
            encoding="utf-8",
        )
        (root / "comm").write_text(comm + "\n", encoding="utf-8")
        (root / "cmdline").write_bytes(comm.encode() + b"\0")
        (root / "environ").write_bytes(environ)

    def environment(self) -> dict[str, str]:
        value = os.environ.copy()
        value.update(
            {
                "HOME": str(self.home),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SESSION_KIT_JOURNAL_DIR": str(self.base / "journals"),
                "SESSION_KIT_ARCHIVE_DIR": str(self.base / "archives"),
                "SESSION_KIT_SHPOOL_CMD": str(self.bin / "shpool"),
                "SESSION_KIT_STATUS_CMD": str(self.bin / "status"),
                "SESSION_KIT_PROC_ROOT": str(self.proc),
                "SESSION_KIT_DAEMON_PID": "10",
                "SESSION_KIT_REAPER_SENTINEL": str(self.base / "enabled"),
                "SESSION_KIT_AUTO_CLOSE_HOURS": "1",
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_TEST_EXACT_SIGNAL": "remove",
                "SESSION_KIT_TEST_EXACT_SIGNAL_LOG": str(
                    self.base / "exact-signal.log"
                ),
                "SESSION_KIT_TEST_MONOTONIC_NS": str(TEST_NOW_NS),
                "FAKE_SHPOOL_STATE": str(self.shpool_state),
                "FAKE_KILL_LOG": str(self.kill_log),
                "FAKE_TARGET": SESSION_ID,
                "FAKE_INVENTORY": str(self.inventory),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return value

    def run_reaper(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [REAPER, "--auto-close"],
            cwd=REPO,
            env=self.environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )

    def test_two_fresh_verifications_then_exact_close_and_minimal_log(
        self,
    ) -> None:
        first = self.run_reaper()
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertIn("auto_candidates=0 actions=0", first.stdout)
        observations = self.state / "auto-close-observations.json"
        value = json.loads(observations.read_text(encoding="utf-8"))
        value["observations"][SESSION_ID][
            "first_verified_monotonic_ns"
        ] = TEST_NOW_NS - 2 * HOUR_NS
        observations.write_text(
            json.dumps(value) + "\n", encoding="utf-8"
        )
        observations.chmod(0o600)

        closed = self.run_reaper()
        self.assertEqual(0, closed.returncode, closed.stderr)
        self.assertIn("auto_candidates=1 actions=1", closed.stdout)
        self.assertEqual(
            f"attach-exit {SESSION_ID}\n", self.kill_log.read_text()
        )
        self.assertEqual(
            "100\t10000\t15\n",
            (self.base / "exact-signal.log").read_text(),
        )
        self.assertEqual(
            [],
            [
                row["name"]
                for row in json.loads(self.shpool_state.read_text())["sessions"]
            ],
        )
        action_log = self.state / "action-events.jsonl"
        self.assertEqual(0o600, stat.S_IMODE(action_log.stat().st_mode))
        serialized = action_log.read_text(encoding="utf-8")
        self.assertNotIn(SESSION_ID, serialized)
        self.assertNotIn(UUID, serialized)
        actions = [json.loads(line) for line in serialized.splitlines()]
        self.assertEqual(
            [("auto_close", "requested"), ("auto_close", "closed")],
            [(item["action"], item["outcome"]) for item in actions],
        )

    def test_one_pass_clears_a_preexisting_phantom_and_keeps_live_shell(self) -> None:
        phantom = "s20260816-040100-77"
        shpool = json.loads(self.shpool_state.read_text())
        shpool["sessions"].append(
            {
                "name": phantom,
                "status": "Disconnected",
                "started_at_unix_ms": 1_800_000_200_000,
                "last_disconnected_at_unix_ms": 1_800_000_300_000,
            }
        )
        self.shpool_state.write_text(json.dumps(shpool) + "\n")
        inventory = json.loads(self.inventory.read_text())
        inventory["sessions"].append(
            {
                "shpool_id_raw": phantom,
                "started_at_unix_ms": 1_800_000_200_000,
                "shpool_status": "Disconnected",
                "availability": "ready",
                "provider": "unknown",
                "mutation_allowed": False,
                "mutation_rejection_reason": "missing-shell-generation",
                "shpool_shell": None,
                "subagents": [],
                "identity": {"uuid": None, "pid": None},
                "diagnostics": [
                    f"expected one daemon child for {phantom!r}, found 0",
                    "identity candidates: Claude=0, Codex=0",
                ],
            }
        )
        self.inventory.write_text(json.dumps(inventory) + "\n")
        environment = self.environment()
        environment["FAKE_TARGET"] = phantom

        swept = subprocess.run(
            [REAPER, "--auto-close"],
            cwd=REPO,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )

        self.assertEqual(0, swept.returncode, swept.stderr)
        self.assertIn("auto_candidates=1 actions=1", swept.stdout)
        remaining = json.loads(self.shpool_state.read_text())["sessions"]
        self.assertEqual([SESSION_ID], [row["name"] for row in remaining])
        self.assertTrue((self.proc / "100").is_dir(), "live shell was touched")
        self.assertFalse((self.base / "exact-signal.log").exists())
        self.assertEqual(f"attach-exit {phantom}\n", self.kill_log.read_text())

    def test_reparented_same_session_process_blocks_phantom_sweep(self) -> None:
        phantom = "s20260816-040200-78"
        self._write_process(
            200,
            1,
            "codex",
            f"SHPOOL_SESSION_NAME={phantom}\0".encode(),
        )
        facts = reaper.scan_shell_facts(self.proc, 10, daemon_start_ticks=1_000)
        self.assertEqual(
            {"unbound_session_processes": [200]}, facts[phantom]
        )

    def test_scheduled_run_appends_a_versioned_private_file_log_line(self) -> None:
        result = self.run_reaper()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("auto_candidates=0 actions=0", result.stdout)

        log = self.state / "reaper.log"
        self.assertEqual(0o600, stat.S_IMODE(log.stat().st_mode))
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(lines))
        self.assertRegex(
            lines[0],
            # The pass with nothing to close still runs the working-copy sweep,
            # and a copy it KEPT for unmerged work is the line worth finding
            # later, so both counts are on every summary.
            r"^schema_version=1 timestamp=\S+ mode=auto "
            r"auto_candidates=0 actions=0 "
            r"worktrees_released=0 worktrees_kept=0$",
        )

    def test_zero_candidate_summary_survives_a_reaper_log_write_failure(self) -> None:
        (self.state / "reaper.log").mkdir()

        result = self.run_reaper()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("auto_candidates=0 actions=0", result.stdout)
        self.assertIn("reaper log write", result.stderr)

    def test_human_input_proof_never_creates_a_candidate_or_kill(self) -> None:
        inventory = json.loads(self.inventory.read_text(encoding="utf-8"))
        inventory["sessions"][0]["user_input_after_provider_exit"] = True
        self.inventory.write_text(
            json.dumps(inventory) + "\n", encoding="utf-8"
        )
        for _ in range(2):
            refused = self.run_reaper()
            self.assertEqual(0, refused.returncode, refused.stderr)
            self.assertIn("auto_candidates=0 actions=0", refused.stdout)
        self.assertFalse(self.kill_log.exists())

    def test_dry_run_is_read_only(self) -> None:
        environment = self.environment()
        environment["SESSION_KIT_REAPER_DRY_RUN"] = "1"
        result = subprocess.run(
            [REAPER, "--auto-close"],
            cwd=REPO,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("auto_candidates=0 actions=0", result.stdout)
        self.assertFalse(
            (self.state / "auto-close-observations.json").exists()
        )
        self.assertFalse(
            (self.state / "auto-close-candidates.json").exists()
        )
        self.assertFalse(self.kill_log.exists())

    def test_old_picker_temp_files_expire_in_both_spellings(self) -> None:
        # mktemp's real form is <label>.json.SUFFIX; the legacy inverted
        # spelling is still collected so stragglers from old releases go too.
        old = self.state / "login-snapshot.ABC123.json"
        old_guard = self.state / "sp-guard.GHI789.json"
        old_recovery = self.state / "login-recovery.JKL012.json"
        old_view = self.state / "login-view.json.a1B2c3"
        old_live = self.state / "login-live.json.d4E5f6"
        old_done = self.state / "login-live-done.json.g7H8i9"
        old_tui = self.state / "tui-stderr.a1B2c3D4"
        old_tui.mkdir(mode=0o700)
        (old_tui / "tail").write_text("captured\n", encoding="utf-8")
        (old_tui / "tail").chmod(0o600)
        os.mkfifo(old_tui / "stream", mode=0o600)
        recent = self.state / "login-view.DEF456.json"
        unrelated = self.state / "inventory.json"
        aged = (old, old_guard, old_recovery, old_view, old_live, old_done)
        for path in aged + (recent, unrelated):
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o600)
        expired_at = time.time() - 25 * 60 * 60
        for path in aged + (unrelated,):
            os.utime(path, (expired_at, expired_at))
        for path in (old_tui / "tail", old_tui / "stream", old_tui):
            os.utime(path, (expired_at, expired_at), follow_symlinks=False)

        expired = self.run_reaper()
        self.assertEqual(0, expired.returncode, expired.stderr)
        for path in aged:
            self.assertFalse(path.exists(), path)
        self.assertFalse(old_tui.exists())
        self.assertTrue(recent.is_file())
        self.assertTrue(unrelated.is_file())
        self.assertIn("expired_picker_temp_files=7", expired.stderr)

    def test_a_live_picker_no_longer_blocks_temp_expiry(self) -> None:
        """A live picker's temps refresh every few seconds; the 24-hour age
        cutoff alone protects them. The old whole-run abort made expiry
        permanently dead on any box that always has a picker open."""
        expired_at = time.time() - 25 * 60 * 60
        blocked = self.state / "picker-proof.json.abc123"
        fresh = self.state / "login-view.json.zz9yy8"
        for path in (blocked, fresh):
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o600)
        os.utime(blocked, (expired_at, expired_at))
        self._write_process(200, 1, "shpool_login")
        retained = self.run_reaper()
        self.assertEqual(0, retained.returncode, retained.stderr)
        self.assertFalse(blocked.exists())
        self.assertTrue(fresh.is_file())
        self.assertIn("expired_picker_temp_files=1", retained.stderr)

    def test_reaper_keeps_an_aged_tui_capture_held_by_a_live_process(self) -> None:
        capture = self.state / "tui-stderr.z9Y8x7W6"
        capture.mkdir(mode=0o700)
        (capture / "tail").write_text("still live\n", encoding="utf-8")
        (capture / "tail").chmod(0o600)
        os.mkfifo(capture / "stream", mode=0o600)
        expired_at = time.time() - 25 * 60 * 60
        for path in (capture / "tail", capture / "stream", capture):
            os.utime(path, (expired_at, expired_at), follow_symlinks=False)
        self._write_process(200, 1, "shpool_login_launcher")
        descriptors = self.proc / "200/fd"
        descriptors.mkdir()
        (descriptors / "2").symlink_to(capture / "stream")

        retained = self.run_reaper()

        self.assertEqual(0, retained.returncode, retained.stderr)
        self.assertTrue(capture.is_dir())

    def test_second_verification_race_refuses_close(self) -> None:
        first = self.run_reaper()
        self.assertEqual(0, first.returncode, first.stderr)
        observations = self.state / "auto-close-observations.json"
        value = json.loads(observations.read_text(encoding="utf-8"))
        value["observations"][SESSION_ID][
            "first_verified_monotonic_ns"
        ] = TEST_NOW_NS - 2 * HOUR_NS
        observations.write_text(
            json.dumps(value) + "\n", encoding="utf-8"
        )
        observations.chmod(0o600)
        unsafe_inventory = self.base / "unsafe-inventory.json"
        unsafe = json.loads(self.inventory.read_text(encoding="utf-8"))
        unsafe["sessions"][0].update(
            shpool_status="Attached",
            availability="attached",
        )
        unsafe_inventory.write_text(
            json.dumps(unsafe) + "\n", encoding="utf-8"
        )
        counter = self.base / "status-count"
        write_executable(
            self.bin / "status",
            """#!/usr/bin/env python3
import os,pathlib,sys
if sys.argv[1:] != ["--guard-json"]:
    raise SystemExit(2)
counter=pathlib.Path(os.environ["FAKE_STATUS_COUNT"])
try: count=int(counter.read_text())
except (OSError,ValueError): count=0
count += 1
counter.write_text(str(count))
source=(
    os.environ["FAKE_INVENTORY"]
    if count < 3
    else os.environ["FAKE_UNSAFE_INVENTORY"]
)
print(pathlib.Path(source).read_text(),end="")
""",
        )
        environment = self.environment()
        environment.update(
            {
                "FAKE_STATUS_COUNT": str(counter),
                "FAKE_UNSAFE_INVENTORY": str(unsafe_inventory),
            }
        )
        raced = subprocess.run(
            [REAPER, "--auto-close"],
            cwd=REPO,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
        self.assertNotEqual(0, raced.returncode)
        self.assertIn("changed or is no longer safe", raced.stderr)
        self.assertFalse(self.kill_log.exists())

    def test_the_hourly_pass_sweeps_a_zombie_subagent_worker(self) -> None:
        # The trigger, not the code (operator rule): the timer's own
        # `shpool_reaper --auto-close` pass must close an idle sub-agent
        # worker end-to-end. The victim is a real process of ours whose pid
        # wears a claude-worker cmdline in the fake /proc, so the sweep's
        # TERM is a real signal landing on a real pid -- never a bystander,
        # because the pid is our own child.
        victim = subprocess.Popen(["sleep", "300"])
        self.addCleanup(victim.wait)
        self.addCleanup(
            lambda: victim.poll() is None and victim.kill()  # type: ignore[func-returns-value]
        )
        root = self.proc / str(victim.pid)
        root.mkdir()
        fields = ["S", "1"] + ["0"] * 17 + ["12345"]
        (root / "stat").write_text(
            f"{victim.pid} (claude) {' '.join(fields)}\n", encoding="utf-8"
        )
        worker = [
            "/home/user/.local/share/claude/versions/2.1.231",
            "--agent-id",
            "zombie@session-12345678",
            "--parent-session-id",
            "abcd-1234",
        ]
        (root / "cmdline").write_bytes("\0".join(worker).encode() + b"\0")
        (root / "comm").write_text("claude\n", encoding="utf-8")
        worker_session = "12345678-1234-4234-8234-123456789abc"
        (root / "environ").write_bytes(b"")
        session_record = self.home / ".claude" / "sessions" / f"{victim.pid}.json"
        session_record.parent.mkdir(parents=True)
        session_record.write_text(
            json.dumps(
                {
                    "pid": victim.pid,
                    "procStart": "12345",
                    "sessionId": worker_session,
                }
            ),
            encoding="utf-8",
        )
        transcript = (
            self.home / ".claude" / "projects" / "fixture"
            / f"{worker_session}.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        transcript.write_text("finished output\n", encoding="utf-8")
        environment = self.environment()
        # Ten seconds, with passes about a second apart. The window has to be
        # WIDER than the gap between passes: nothing is closed on evidence
        # older than the window, so a sub-second window with a pass that takes
        # a second to run would restart its own clock every time (round 2,
        # lane A F2). This is the real cadence in miniature.
        environment["SESSION_KIT_SUBAGENT_IDLE_HOURS"] = "0.0028"  # ~10s
        run = lambda: subprocess.run(  # noqa: E731
            [REAPER, "--auto-close"],
            cwd=REPO,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
        first = run()
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertIsNone(victim.poll())  # first sighting only arms the clock
        # Keep passing at that cadence until the worker has been WATCHED idle
        # for a full window, which is the promise the timer actually makes.
        deadline = time.time() + 40
        while victim.poll() is None and time.time() < deadline:
            time.sleep(1.0)
            later = run()
            self.assertEqual(0, later.returncode, later.stderr)
        self.assertEqual(-15, victim.poll())  # SIGTERM landed
        log_lines = (
            (self.state / "subagent-sweep.log").read_text().strip().splitlines()
        )
        records = [json.loads(line) for line in log_lines]
        self.assertEqual(["SIGTERM"], [r["signal"] for r in records])
        self.assertEqual(victim.pid, records[0]["pid"])
        self.assertEqual("zombie@session-12345678", records[0]["agent_id"])

    # ---- the working-copy sweep ------------------------------------------

    def a_delegated_copy(self, branch: str, session: str) -> tuple[Path, Path]:
        """A repository and one automatic working copy bound to ``session``."""
        sys.path.insert(0, str(REPO / "lib"))
        from sessionkit_inventory import worktrees

        repo = self.base / f"repo-{session}"
        repo.mkdir()
        environment = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }

        def run(*arguments: str) -> None:
            subprocess.run(
                ["git", "-C", str(repo), *arguments],
                check=True, capture_output=True, text=True,
                env={**os.environ, **environment},
            )

        run("init", "--initial-branch=main", "--quiet")
        (repo / "README").write_text("one\n", encoding="utf-8")
        run("add", "README")
        run("commit", "--quiet", "-m", "first")
        record = worktrees.materialize(
            repo=repo, branch=branch, state_dir=self.state,
            environ={"SESSION_KIT_WORKTREE_ROOT": str(self.worktree_root)},
            auto=True, origin="machine",
        )
        worktrees.bind(
            state_dir=self.state, path=record["path"], shpool_id=session,
            environ={"SESSION_KIT_WORKTREE_ROOT": str(self.worktree_root)},
        )
        return repo, Path(record["path"])

    @property
    def worktree_root(self) -> Path:
        return self.base / "worktree-root"

    def sweep_environment(self) -> dict[str, str]:
        value = self.environment()
        value["SESSION_KIT_WORKTREE_ROOT"] = str(self.worktree_root)
        return value

    def test_the_pass_with_nothing_to_close_still_sweeps(self) -> None:
        """Nothing else in the kit runs the sweep, and this is the usual pass.

        A machine with no auto-close candidate is the ordinary case. With the
        sweep below the zero-candidate exit, the scheduled half of "a copy
        comes back" almost never ran, so a copy kept because a worker was still
        writing was kept for good.
        """
        _repo, copy = self.a_delegated_copy("worker/gone", "s-not-live")
        self.assertTrue(copy.is_dir())

        result = subprocess.run(
            [REAPER, "--auto-close"], cwd=REPO, env=self.sweep_environment(),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=30,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("auto_candidates=0", result.stdout)
        self.assertIn("worktrees_released=1", result.stdout)
        self.assertFalse(copy.exists(), "the copy of a dead session came back")

    def test_a_live_sessions_copy_survives_the_same_pass(self) -> None:
        _repo, copy = self.a_delegated_copy("worker/live", SESSION_ID)

        result = subprocess.run(
            [REAPER, "--auto-close"], cwd=REPO, env=self.sweep_environment(),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=30,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("worktrees_released=0", result.stdout)
        self.assertTrue(copy.is_dir(), "a live session's copy was removed")

    def test_a_session_list_it_cannot_read_removes_no_copy(self) -> None:
        """An unreadable list is never "nothing is alive", on any path out.

        A payload this malformed makes the pass refuse during auto-close
        planning, before the sweep is reached — so the property under test is
        not which message appears, it is that **no copy is removed**. The sweep
        arriving at its own version of this question is proved at the verb, in
        `tests.test_worktree_isolation.TheSweepIsToldWhatIsAliveTests`, where a
        payload can be handed straight to the thing that acts on it: that is
        the reader that used to turn "I could not understand this" into an
        empty `--active` list and then into "nothing is alive".
        """
        _repo, copy = self.a_delegated_copy("worker/live", SESSION_ID)
        self.shpool_state.write_text(
            json.dumps({"sessions": "not a list at all"}) + "\n", encoding="utf-8"
        )

        result = subprocess.run(
            [REAPER, "--auto-close"], cwd=REPO, env=self.sweep_environment(),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=30,
        )

        self.assertNotEqual(0, result.returncode, "the pass must refuse, not proceed")
        self.assertNotIn("worktrees_released=1", result.stdout)
        self.assertTrue(copy.is_dir(), "a live session's copy was removed")

    def test_the_sweep_never_blocks_the_terminal_reaper(self) -> None:
        # A GENUINE sweep crash must leave the terminal-close machinery
        # running: the state scratch path is a directory, so the sweep's
        # state save raises IsADirectoryError and the module dies mid-pass
        # (corrupt JSON and a bad idle-hours value alone are handled inputs,
        # not crashes -- lane finding X20-F8). The sweep is additive, never
        # a new failure mode for the pass that shares its timer.
        environment = self.environment()
        environment["SESSION_KIT_SUBAGENT_IDLE_HOURS"] = "not-a-number"
        (self.state / "subagent-sweep.state.json").write_text(
            "{corrupt", encoding="utf-8"
        )
        (self.state / "subagent-sweep.state.json.tmp").mkdir()
        result = subprocess.run(
            [REAPER, "--auto-close"],
            cwd=REPO,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("auto_candidates=0 actions=0", result.stdout)

    def test_darwin_never_runs_the_sweep(self) -> None:
        # The Darwin proc tree is a regenerated process snapshot. Its mtimes
        # describe snapshot creation rather than worker output, and it cannot
        # re-read a live PID generation when delivery happens (re-derived
        # X20-F1). The platform gate must keep the sweep out of the
        # pass entirely: no signal, no state, no log — on a pass that RUNS
        # THE DARWIN PATH TO COMPLETION. A deterministic process table is
        # served through the reaper's own SESSION_KIT_INVENTORY_CORE seam,
        # because an aborted pass would satisfy the absence assertions
        # vacuously (X20 round-2 lane catch: the first version of this test
        # died in the libproc adapter before ever reaching the gate, at both
        # commits).
        victim = subprocess.Popen(["sleep", "300"])
        self.addCleanup(victim.wait)
        self.addCleanup(
            lambda: victim.poll() is None and victim.kill()  # type: ignore[func-returns-value]
        )
        table = {
            "processes": [
                {
                    "pid": 10,
                    "ppid": 1,
                    "comm": "shpool",
                    "cmdline": ["/opt/homebrew/bin/shpool", "daemon"],
                    "args_available": True,
                    "session_name": None,
                    "start_ticks": 1000,
                },
                {
                    "pid": 100,
                    "ppid": 10,
                    "comm": "bash",
                    "cmdline": ["bash"],
                    "args_available": True,
                    "session_name": SESSION_ID,
                    "start_ticks": 10_000,
                },
                {
                    "pid": victim.pid,
                    "ppid": 1,
                    "comm": "claude",
                    "cmdline": [
                        "/Users/user/.local/share/claude/versions/2.1.231",
                        "--agent-id",
                        "mac@team",
                        "--parent-session-id",
                        "abcd-1234",
                    ],
                    "args_available": True,
                    "session_name": None,
                    "start_ticks": 12_345,
                },
            ]
        }
        fake_core = self.bin / "darwin_inventory_core.py"
        write_executable(
            fake_core,
            "#!/usr/bin/env python3\n"
            "import json,sys\n"
            "if sys.argv[1:] == ['platform', 'process-table']:\n"
            f"    print(json.dumps({table!r}))\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(2)\n",
        )
        environment = self.environment()
        environment["SESSION_KIT_TEST_PLATFORM"] = "Darwin"
        environment["SESSION_KIT_INVENTORY_CORE"] = str(fake_core)
        environment["SESSION_KIT_SUBAGENT_IDLE_HOURS"] = "0.0001"
        for attempt in range(2):
            result = subprocess.run(
                [REAPER, "--auto-close"],
                cwd=REPO,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=20,
            )
            # The pass must COMPLETE on the Darwin path: an aborted run
            # would pass the absence checks below without testing the gate.
            self.assertEqual(0, result.returncode, (attempt, result.stderr))
            self.assertIn("auto_candidates=0 actions=0", result.stdout)
            time.sleep(0.6)
        self.assertIsNone(victim.poll())  # the worker was never signalled
        self.assertFalse((self.state / "subagent-sweep.state.json").exists())
        self.assertFalse((self.state / "subagent-sweep.log").exists())


if __name__ == "__main__":
    unittest.main()
