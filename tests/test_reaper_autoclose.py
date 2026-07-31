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

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from sessionkit_inventory import reaper  # noqa: E402


SESSION_ID = "s20260730-220500-19"
UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
HOUR_NS = 3600 * 1_000_000_000
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
import json,os,pathlib,sys
state=pathlib.Path(os.environ["FAKE_SHPOOL_STATE"])
value=json.loads(state.read_text())
args=sys.argv[1:]
if args == ["list","--json"]:
    print(json.dumps(value))
    raise SystemExit(0)
if len(args) == 2 and args[0] == "kill":
    before=len(value["sessions"])
    value["sessions"]=[
        row for row in value["sessions"] if row.get("name") != args[1]
    ]
    if len(value["sessions"]) == before:
        raise SystemExit(4)
    state.write_text(json.dumps(value)+"\\n")
    pathlib.Path(os.environ["FAKE_KILL_LOG"]).write_text(args[1]+"\\n")
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
                "FAKE_SHPOOL_STATE": str(self.shpool_state),
                "FAKE_KILL_LOG": str(self.kill_log),
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
        ] = time.monotonic_ns() - 2 * HOUR_NS
        observations.write_text(
            json.dumps(value) + "\n", encoding="utf-8"
        )
        observations.chmod(0o600)

        closed = self.run_reaper()
        self.assertEqual(0, closed.returncode, closed.stderr)
        self.assertIn("auto_candidates=1 actions=1", closed.stdout)
        self.assertEqual(SESSION_ID + "\n", self.kill_log.read_text())
        self.assertEqual(
            [],
            json.loads(self.shpool_state.read_text())["sessions"],
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

    def test_second_verification_race_refuses_close(self) -> None:
        first = self.run_reaper()
        self.assertEqual(0, first.returncode, first.stderr)
        observations = self.state / "auto-close-observations.json"
        value = json.loads(observations.read_text(encoding="utf-8"))
        value["observations"][SESSION_ID][
            "first_verified_monotonic_ns"
        ] = time.monotonic_ns() - 2 * HOUR_NS
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


if __name__ == "__main__":
    unittest.main()
