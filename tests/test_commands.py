from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from tests.support import REPO, run


SP = REPO / "bin" / "sp"
REAPER = REPO / "bin" / "shpool_reaper"
COMMON = REPO / "bin" / "session_kit_common"
BASHRC = REPO / "bashrc" / "shpool.bashrc"
RESET = b"\x1b[?1049l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l\x1b[?1004l\x1b[0m"


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def session_row(
    shpool_id: str,
    *,
    row: int = 1,
    started: int = 1_700_000_000_001,
    provider: str = "codex",
    uuid: str = "00000000-0000-4000-8000-000000000001",
    status: str = "Disconnected",
) -> dict:
    return {
        "row": row,
        "shpool_id": shpool_id,
        "shpool_id_raw": shpool_id,
        "display_shpool_id": shpool_id,
        "mutation_allowed": True,
        "mutation_rejection_reason": None,
        "shpool_shell": {
            "pid": 1001,
            "process_start_ticks": 10010,
        },
        "started_at_unix_ms": started,
        "shpool_status": status,
        "availability": "ready" if status == "Disconnected" else "attached",
        "provider": provider,
        "identity": {
            "uuid": uuid,
            "pid": 2001,
            "process_start_ticks": 20010,
            "provenance": "fixture",
            "confidence": "exact",
        },
        "title": f"{provider.title()} fixture",
        "native_title": f"{provider.title()} fixture",
        "cwd": "/srv/project",
        "process_age_seconds": 60,
        "agent_status": "working",
        "needs_you": False,
        "subagents": [],
        "recovery": {
            "available": True,
            "provider": provider,
            "uuid": uuid,
            "cwd": "/srv/project",
            "argv": ["codex", "resume", uuid],
            "command": f"codex resume {uuid}",
        },
        "diagnostics": [],
    }


def inventory_document(*rows: dict) -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-07-28T00:00:00Z",
        "source": "live",
        "stale": False,
        "warnings": [],
        "daemon_generation": {
            "boot_id": "fixture",
            "pid": 10,
            "process_start_ticks": 100,
        },
        "sessions": list(rows),
        "outside_agents": [],
    }


def unknown_session_row(shpool_id: str = "main9", *, row: int = 9) -> dict:
    result = session_row(shpool_id, row=row, provider="unknown", uuid="")
    result["identity"] = {
        "uuid": None,
        "pid": 2909,
        "process_start_ticks": 29090,
        "provenance": "process-tree",
        "confidence": "partial",
    }
    result["title"] = "Unknown exact shell"
    result["native_title"] = None
    result["recovery"] = {"available": False}
    return result


def write_picker_proof(path: Path, row: dict, *, daemon_pid: int = 10) -> Path:
    identity = row["identity"]
    shell = row["shpool_shell"]
    proof = {
        "schema_version": 1,
        "proof_type": "session-kit-picker-session-v1",
        "shpool_id": row["shpool_id_raw"],
        "started_at_unix_ms": row["started_at_unix_ms"],
        "provider": row["provider"],
        "uuid": identity.get("uuid") or "",
        "provider_pid": identity["pid"],
        "provider_process_start_ticks": identity["process_start_ticks"],
        "shell_pid": shell["pid"],
        "shell_process_start_ticks": shell["process_start_ticks"],
        "daemon_pid": daemon_pid,
        "daemon_process_start_ticks": 100,
    }
    path.write_text(
        json.dumps(
            proof, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def run_queued_creator_after_marker_switch(
    fixture: "CommandFixture",
    command: list[object],
    env: dict[str, str],
) -> tuple[subprocess.Popen[str], str, str]:
    real_flock = shutil.which("flock")
    if real_flock is None:
        raise AssertionError("flock is required for the concurrency test")
    fake_bin = fixture.base / "marker-switch-bin"
    fake_bin.mkdir()
    entered = fixture.base / "marker-switch-entered"
    write_executable(
        fake_bin / "flock",
        """#!/usr/bin/env bash
if [[ ${1:-} == -x && ${2:-} == 9 ]]; then
  : > "$TEST_FLOCK_ENTERED"
fi
exec "$TEST_REAL_FLOCK" "$@"
""",
    )
    process_env = {
        **os.environ,
        **env,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEST_FLOCK_ENTERED": str(entered),
        "TEST_REAL_FLOCK": real_flock,
    }
    lock_path = fixture.state / "create.lock"
    marker = fixture.state / "integration-ready-v1"
    with lock_path.open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = subprocess.Popen(
            [str(part) for part in command],
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while not entered.exists() and proc.poll() is None:
            if time.monotonic() >= deadline:
                proc.kill()
                raise AssertionError("creator did not reach the held creation lock")
            time.sleep(0.01)
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise AssertionError(
                f"creator exited before queuing on creation lock: {(stdout, stderr)}"
            )
        marker.write_text(
            f"session-kit-integration-v1 {'b' * 40}\n",
            encoding="utf-8",
        )
        marker.chmod(0o600)
        fcntl.flock(held, fcntl.LOCK_UN)
    stdout, stderr = proc.communicate(timeout=5)
    return proc, stdout, stderr


class CommandFixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".commands-", dir=REPO)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.boot_id = self.base / "boot-id"
        self.boot_id.write_text("fixture-boot\n", encoding="utf-8")
        self.release_id = "a" * 40
        integration_marker = self.state / "integration-ready-v1"
        integration_marker.write_text(
            f"session-kit-integration-v1 {self.release_id}\n", encoding="utf-8"
        )
        integration_marker.chmod(0o600)
        self.start = self.base / "shpool-start"
        self.journals = self.base / "journals"
        self.archives = self.base / "archives"
        self.recovery = self.base / "recovery"
        self.project = self.base / "project"
        self.project.mkdir()
        self.shpool_state = self.base / "shpool.json"
        self.shpool_state.write_text('{"sessions":[]}\n', encoding="utf-8")
        self.shpool_log = self.base / "shpool.log"
        self.inventory = self.base / "inventory.json"
        self.inventory.write_text(
            json.dumps(inventory_document()), encoding="utf-8"
        )
        self.snapshot_count = self.base / "snapshot-count"
        self.projects = self.base / "projects.tsv"
        self.projects.write_text(
            f"fixture\tshell\t{self.project}\n", encoding="utf-8"
        )
        self.config = self.base / "session-kit.json"
        self.config.write_text(
            '{"schema_version":1,"aliases":{}}\n', encoding="utf-8"
        )
        self.config.chmod(0o600)
        self.fake_shpool = self.base / "fake-shpool"
        self.fake_core = self.base / "fake-inventory"
        write_executable(
            self.fake_shpool,
            """#!/usr/bin/env python3
import fcntl, json, os, pathlib, sys, tempfile, time
state=pathlib.Path(os.environ["FAKE_SHPOOL_STATE"])
log=pathlib.Path(os.environ["FAKE_SHPOOL_LOG"])
lock=state.with_suffix(".lock")
lock.parent.mkdir(parents=True, exist_ok=True)
with lock.open("a+") as held:
    fcntl.flock(held, fcntl.LOCK_EX)
    try: data=json.loads(state.read_text())
    except (OSError, ValueError): data={"sessions":[]}
    args=sys.argv[1:]
    if args == ["list", "--json"]:
        print(json.dumps(data))
        raise SystemExit(0)
    if args and args[0] == "attach":
        expected_lock=os.environ.get("FAKE_EXPECT_CREATE_LOCK")
        if expected_lock:
            create_lock=pathlib.Path(os.environ["SESSION_KIT_STATE_DIR"])/"create.lock"
            with create_lock.open("a+") as create_held:
                try:
                    fcntl.flock(create_held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    observed_lock="unlocked"
                except BlockingIOError:
                    observed_lock="locked"
            if observed_lock != expected_lock:
                raise SystemExit(88)
        if os.environ.get("FAKE_ATTACH_FAIL") == "1":
            raise SystemExit(5)
        name=args[-1]
        target=next(
            (row for row in data["sessions"] if row.get("name") == name),
            None,
        )
        force="--force" in args
        if os.environ.get("FAKE_ATTACH_BECOMES_BUSY") == "1" and not force:
            if target is not None:
                target["status"]="Attached"
            action="busy"
        elif (
            os.environ.get("FAKE_BUSY_IF_ATTACHED") == "1"
            and target is not None
            and target.get("status") == "Attached"
            and not force
        ):
            action="busy"
        elif os.environ.get("FAKE_BUSY_IF_ATTACHED") == "1" and force:
            if target is not None:
                target["status"]="Attached"
            action="force-attach"
        else:
            action="attach"
        if os.environ.get("FAKE_DROP_BEFORE_ATTACH") == "1":
            data["sessions"]=[row for row in data["sessions"] if row.get("name") != name]
        exits_immediately="--cmd" in args and args[args.index("--cmd")+1] == "/bin/false"
        if not exits_immediately and not any(row.get("name") == name for row in data["sessions"]):
            data["sessions"].append({"name":name,"status":"Disconnected","started_at_unix_ms":int(time.time()*1000)})
    elif len(args) == 2 and args[0] == "kill":
        expected_lock=os.environ.get(
            "FAKE_EXPECT_KILL_LOCK",
            os.environ.get("FAKE_EXPECT_CREATE_LOCK"),
        )
        if expected_lock:
            create_lock=pathlib.Path(os.environ["SESSION_KIT_STATE_DIR"])/"create.lock"
            with create_lock.open("a+") as create_held:
                try:
                    fcntl.flock(create_held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    observed_lock="unlocked"
                except BlockingIOError:
                    observed_lock="locked"
            if observed_lock != expected_lock:
                raise SystemExit(88)
        name=args[1]
        before=len(data["sessions"])
        data["sessions"]=[row for row in data["sessions"] if row.get("name") != name]
        if len(data["sessions"]) == before:
            raise SystemExit(4)
        action="kill"
    else:
        raise SystemExit(2)
    fd,tmp=tempfile.mkstemp(prefix=".state.",dir=state.parent)
    with os.fdopen(fd,"w") as out: json.dump(data,out)
    os.replace(tmp,state)
    with log.open("a") as out: out.write(action+" "+name+"\\n")
""",
        )
        write_executable(
            self.fake_core,
            """#!/usr/bin/env python3
import json, os, pathlib, sys, tempfile, unicodedata
args=sys.argv[1:]
if args[:2] == ["platform", "codex-refresh-target"] and len(args) == 6:
    print("\t".join((
        os.environ.get("STUB_REFRESH_PID", args[4]),
        os.environ.get("STUB_REFRESH_START", args[5]),
    )))
    raise SystemExit(0)
if args[:2] == ["platform", "process-is"] and len(args) == 5:
    pid=int(args[2]); generation=int(args[3]); executable=args[4]
    try:
        stat_text=pathlib.Path(f"/proc/{pid}/stat").read_text()
        current=int(stat_text.rsplit(")",1)[1].split()[19])
        argv=pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\\0")
        actual=pathlib.Path(argv[0].decode("utf-8","replace")).name
    except (OSError,ValueError,IndexError):
        raise SystemExit(1)
    raise SystemExit(0 if current == generation and actual == executable else 1)
if args[:1] == ["codex-bounce-title"] and len(args) == 2:
    title=os.environ.get("STUB_CODEX_BOUNCE_TITLE", "")
    if title:
        print(title)
        raise SystemExit(0)
    raise SystemExit(1)
if args[:2] == ["automatic-title", "self-name"] and len(args) == 3:
    print(json.dumps({
        "schema_version":1,
        "automatic_name_state":"ready",
        "title":args[2],
    },sort_keys=True))
    raise SystemExit(0)
if args[:2] == ["alias", "set"] or args[:2] == ["alias", "delete"]:
    action=args[1]
    tail=args[2:]
    if action == "set" and len(tail) == 4 and tail[2] == "--":
        provider,uuid,title=tail[0],tail[1],tail[3]
    elif action == "delete" and len(tail) == 2:
        provider,uuid=tail
        title=None
    else:
        raise SystemExit(2)
    if provider not in {"claude","codex"}:
        raise SystemExit(2)
    config=pathlib.Path(os.environ["SESSION_KIT_CONFIG"])
    data=json.loads(config.read_text())
    aliases=dict(data.get("aliases",{}))
    key=f"{provider}:{uuid.lower()}"
    if action == "set":
        safe="".join(
            " " if unicodedata.category(character).startswith("C") else character
            for character in title
        )
        safe=" ".join(safe.split())[:100]
        if not safe:
            raise SystemExit(2)
        aliases[key]=safe
    else:
        aliases.pop(key,None)
    data["aliases"]=aliases
    descriptor,temporary=tempfile.mkstemp(prefix=".config.",dir=config.parent)
    with os.fdopen(descriptor,"w",encoding="utf-8") as handle:
        json.dump(data,handle,sort_keys=True)
        handle.write("\\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary,0o600)
    os.replace(temporary,config)
    alias_log=os.environ.get("STUB_ALIAS_LOG")
    if alias_log:
        with pathlib.Path(alias_log).open("a",encoding="utf-8") as handle:
            handle.write(json.dumps(args)+"\\n")
    print(json.dumps({"schema_version":1,"aliases":aliases},sort_keys=True))
    raise SystemExit(0)
if args and args[0] == "snapshot":
    dynamic_provider=os.environ.get("STUB_DYNAMIC_PROVIDER")
    if dynamic_provider and os.environ.get("STUB_DYNAMIC_AFTER_SESSIONS"):
        current_state=json.loads(pathlib.Path(os.environ["FAKE_SHPOOL_STATE"]).read_text())
        if len(current_state.get("sessions",[])) <= int(os.environ["STUB_DYNAMIC_AFTER_SESSIONS"]):
            dynamic_provider=None
    if dynamic_provider:
        state=json.loads(pathlib.Path(os.environ["FAKE_SHPOOL_STATE"]).read_text())
        rows=[]
        for index,item in enumerate(state.get("sessions",[]),1):
            name=item["name"]
            if os.environ.get("STUB_CONSUME_ARMED_LAUNCH") == "1":
                start=pathlib.Path(os.environ["SESSION_KIT_START_DIR"])/name
                expected=start.with_name(start.name+".expected")
                if start.is_file() and expected.is_file():
                    start.unlink()
                    expected.unlink()
            uuid_overrides=json.loads(os.environ.get("STUB_DYNAMIC_UUID_OVERRIDES","{}"))
            rows.append({
                "row":index,
                "shpool_id":name,
                "shpool_id_raw":name,
                "display_shpool_id":name,
                "mutation_allowed":True,
                "mutation_rejection_reason":None,
                "shpool_shell":{"pid":1000+index,"process_start_ticks":10000+index},
                "started_at_unix_ms":int(os.environ.get("STUB_DYNAMIC_STARTED",item["started_at_unix_ms"])),
                "shpool_status":item["status"],
                "availability":"ready",
                "provider":dynamic_provider,
                "identity":{"uuid":uuid_overrides.get(name,os.environ.get("STUB_DYNAMIC_UUID")),"pid":2000+index,"process_start_ticks":20000+index,"confidence":"exact"},
                "title":"dynamic proof",
                "native_title":"dynamic proof",
                "cwd":os.environ.get("STUB_DYNAMIC_CWD","/srv/project"),
                "process_age_seconds":1,
                "agent_status":"working",
                "needs_you":False,
                "subagents":[],
                "recovery":{"available":False},
                "diagnostics":[],
            })
        if os.environ.get("STUB_DYNAMIC_INCLUDE_UNKNOWN") == "1":
            rows.append({
                "row":len(rows)+1,
                "shpool_id":"main9",
                "shpool_id_raw":"main9",
                "display_shpool_id":"main9",
                "mutation_allowed":True,
                "mutation_rejection_reason":None,
                "shpool_shell":{"pid":9009,"process_start_ticks":99009},
                "started_at_unix_ms":1700000009000,
                "shpool_status":"Disconnected",
                "availability":"ready",
                "provider":"unknown",
                "identity":{"uuid":None,"pid":None,"process_start_ticks":None,"confidence":"none"},
                "title":"unknown exact shell",
                "native_title":None,
                "cwd":"/srv/unknown",
                "process_age_seconds":1,
                "agent_status":"unknown",
                "needs_you":False,
                "subagents":[],
                "recovery":{"available":False},
                "diagnostics":[],
            })
        outside_agents=json.loads(os.environ.get("STUB_DYNAMIC_OUTSIDE_AGENTS","[]"))
        document={"schema_version":1,"generated_at":"2026-07-28T00:00:00Z","source":"live","stale":False,"warnings":[],"daemon_generation":{"pid":10,"process_start_ticks":100},"sessions":rows,"outside_agents":outside_agents}
        if "--strict-live" in args and any(row.get("provider") == "unknown" for row in rows):
            raise SystemExit(3)
        if "--guard-live" in args and os.environ.get("STUB_GUARD_FAIL") == "1":
            raise SystemExit(3)
        print(json.dumps(document))
        raise SystemExit(0)
    counter=pathlib.Path(os.environ["STUB_SNAPSHOT_COUNT"])
    try: count=int(counter.read_text())
    except (OSError, ValueError): count=0
    count += 1
    counter.write_text(str(count))
    source=os.environ.get("STUB_SECOND_INVENTORY") if count > 1 else None
    source=source or os.environ["STUB_INVENTORY"]
    document=json.loads(pathlib.Path(source).read_text())
    if "--strict-live" in args and any(row.get("provider") == "unknown" for row in document.get("sessions",[])):
        raise SystemExit(3)
    if "--guard-live" in args and os.environ.get("STUB_GUARD_FAIL") == "1":
        raise SystemExit(3)
    print(json.dumps(document))
elif args and args[0] == "render":
    print("fixture renderer")
elif args and args[0] == "waiting-count":
    print("0")
else:
    raise SystemExit(2)
""",
        )

    def close(self) -> None:
        self.temp.cleanup()

    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "SESSION_KIT_STATE_DIR": str(self.state),
            "SESSION_KIT_JOURNAL_DIR": str(self.journals),
            "SESSION_KIT_ARCHIVE_DIR": str(self.archives),
            "SESSION_KIT_JOURNAL_RECOVERY_DIR": str(self.recovery),
            "SESSION_KIT_START_DIR": str(self.start),
            "SESSION_KIT_PROJECTS_FILE": str(self.projects),
            "SESSION_KIT_CONFIG": str(self.config),
            "SESSION_KIT_SHPOOL_CMD": str(self.fake_shpool),
            "SESSION_KIT_INVENTORY_CORE": str(self.fake_core),
            "SESSION_KIT_NONINTERACTIVE": "1",
            "SESSION_KIT_NO_COLOR": "1",
            "SESSION_KIT_RELEASE_ID": self.release_id,
            "SESSION_KIT_BOOT_ID_FILE": str(self.boot_id),
            "FAKE_SHPOOL_STATE": str(self.shpool_state),
            "FAKE_SHPOOL_LOG": str(self.shpool_log),
            "STUB_INVENTORY": str(self.inventory),
            "STUB_SNAPSHOT_COUNT": str(self.snapshot_count),
            "PYTHONDONTWRITEBYTECODE": "1",
        }


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CommandFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_concurrent_create_allocates_unique_unbounded_ids_and_list_is_safe(self) -> None:
        env = {**os.environ, **self.fixture.env()}
        env.update(
            {
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
            }
        )
        processes = [
            subprocess.Popen(
                [SP, "new", "shell", "fixture"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(10)
        ]
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=20)
            self.assertEqual(0, proc.returncode, (stdout, stderr))
        state = json.loads(self.fixture.shpool_state.read_text())
        names = [row["name"] for row in state["sessions"]]
        self.assertEqual(10, len(names))
        self.assertEqual(10, len(set(names)))
        listed = run([SP, "list"], env=self.fixture.env())
        self.assertEqual("fixture renderer\n", listed.stdout)

    def test_release_bound_integration_marker_fails_closed_until_exact_match(self) -> None:
        marker = self.fixture.state / "integration-ready-v1"
        env = self.fixture.env()
        env["SESSION_KIT_BACKGROUND"] = "1"
        env["STUB_DYNAMIC_PROVIDER"] = "shell"
        env["STUB_DYNAMIC_CWD"] = str(self.fixture.project)
        marker.unlink()
        absent = run([SP, "new", "shell", "fixture"], env=env, check=False)
        self.assertNotEqual(0, absent.returncode)
        self.assertIn("validated for this release", absent.stderr)
        marker.write_text(
            f"session-kit-integration-v1 {'b' * 40}\n", encoding="utf-8"
        )
        marker.chmod(0o600)
        wrong = run([SP, "new", "shell", "fixture"], env=env, check=False)
        self.assertNotEqual(0, wrong.returncode)
        self.assertIn("validated for this release", wrong.stderr)
        marker.write_text(
            f"session-kit-integration-v1 {self.fixture.release_id}\n",
            encoding="utf-8",
        )
        marker.chmod(0o600)
        matching = run([SP, "new", "shell", "fixture"], env=env)
        self.assertTrue(matching.stdout.strip().splitlines()[-1].startswith("s"))

    def test_marker_self_heal_repairs_only_with_managed_login_block(self) -> None:
        marker = self.fixture.state / "integration-ready-v1"
        env = self.fixture.env()
        env["SESSION_KIT_BACKGROUND"] = "1"
        env["STUB_DYNAMIC_PROVIDER"] = "shell"
        env["STUB_DYNAMIC_CWD"] = str(self.fixture.project)
        bashrc = self.fixture.home / ".bashrc"
        marker.unlink()
        refused = run([SP, "new", "shell", "fixture"], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("validated for this release", refused.stderr)
        self.assertFalse(marker.exists())
        bashrc.write_text(
            "# >>> session-kit managed integration >>>\n"
            "# <<< session-kit managed integration <<<\n",
            encoding="utf-8",
        )
        healed = run([SP, "new", "shell", "fixture"], env=env)
        self.assertTrue(healed.stdout.strip().splitlines()[-1].startswith("s"))
        self.assertEqual(
            f"session-kit-integration-v1 {self.fixture.release_id}\n",
            marker.read_text(encoding="utf-8"),
        )
        self.assertEqual(0o600, marker.stat().st_mode & 0o777)
        log = self.fixture.state / "action-events.jsonl"
        self.assertIn("marker_self_heal", log.read_text(encoding="utf-8"))
        marker.write_text(
            f"session-kit-integration-v1 {'b' * 40}\n", encoding="utf-8"
        )
        marker.chmod(0o600)
        stale = run([SP, "new", "shell", "fixture"], env=env)
        self.assertTrue(stale.stdout.strip().splitlines()[-1].startswith("s"))
        self.assertEqual(
            f"session-kit-integration-v1 {self.fixture.release_id}\n",
            marker.read_text(encoding="utf-8"),
        )
        marker.unlink()
        (self.fixture.state / "self-heal-off").write_text("", encoding="utf-8")
        off = run([SP, "new", "shell", "fixture"], env=env, check=False)
        self.assertNotEqual(0, off.returncode)
        self.assertIn("validated for this release", off.stderr)
        self.assertFalse(marker.exists())
        (self.fixture.state / "self-heal-off").unlink()
        env["SESSION_KIT_NO_SELF_HEAL"] = "1"
        disabled = run([SP, "new", "shell", "fixture"], env=env, check=False)
        self.assertNotEqual(0, disabled.returncode)
        self.assertFalse(marker.exists())

    def test_queued_new_rechecks_release_marker_under_creation_lock(self) -> None:
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
            }
        )
        proc, stdout, stderr = run_queued_creator_after_marker_switch(
            self.fixture,
            [SP, "new", "shell", "fixture"],
            env,
        )
        self.assertNotEqual(0, proc.returncode, (stdout, stderr))
        self.assertIn("validated for this release", stderr)
        self.assertEqual(
            [], json.loads(self.fixture.shpool_state.read_text())["sessions"]
        )
        self.assertFalse(self.fixture.shpool_log.exists())
        self.assertFalse(self.fixture.start.exists())

    def test_queued_restore_rechecks_release_marker_under_creation_lock(self) -> None:
        exact_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "codex",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_DYNAMIC_UUID": exact_uuid,
            }
        )
        proc, stdout, stderr = run_queued_creator_after_marker_switch(
            self.fixture,
            [SP, "restore-exact", "codex", exact_uuid, self.fixture.project],
            env,
        )
        self.assertNotEqual(0, proc.returncode, (stdout, stderr))
        self.assertIn("validated for this release", stderr)
        self.assertEqual(
            [], json.loads(self.fixture.shpool_state.read_text())["sessions"]
        )
        self.assertFalse(self.fixture.shpool_log.exists())
        self.assertFalse(self.fixture.start.exists())

    def test_integration_marker_rejects_trailing_bytes(self) -> None:
        marker = self.fixture.state / "integration-ready-v1"
        marker.write_text(
            f"session-kit-integration-v1 {self.fixture.release_id}\nextra\n",
            encoding="utf-8",
        )
        marker.chmod(0o600)
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
            }
        )
        refused = run([SP, "new", "shell", "fixture"], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("validated for this release", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_ai_startup_proof_failure_keeps_session_and_record_without_kill(self) -> None:
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
            }
        )
        failed = run([SP, "new", "claude", "fixture"], env=env, check=False)
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("startup was not proven", failed.stderr)
        state = json.loads(self.fixture.shpool_state.read_text())
        self.assertEqual(1, len(state["sessions"]))
        shpool_id = state["sessions"][0]["name"]
        record = self.fixture.start / shpool_id
        self.assertTrue(record.is_file())
        self.assertTrue(record.with_name(record.name + ".expected").is_file())
        self.assertTrue(record.read_text().startswith(f"claude\t{self.fixture.project}\t"))
        actions = self.fixture.shpool_log.read_text().splitlines()
        self.assertTrue(any(line.startswith("attach ") for line in actions))
        self.assertFalse(any(line.startswith("kill ") for line in actions))

    def test_exact_ai_startup_proof_clears_only_matching_record(self) -> None:
        start_dir = self.fixture.start
        start_dir.mkdir()
        decoy = start_dir / "decoy"
        decoy_content = f"codex\t{self.fixture.project}\t00000000-0000-4000-8000-000000000999\n"
        decoy.write_text(decoy_content, encoding="utf-8")
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
            }
        )
        proven = run([SP, "new", "claude", "fixture"], env=env)
        shpool_id = proven.stdout.strip().splitlines()[-1]
        self.assertTrue(shpool_id.startswith("s"))
        self.assertFalse((start_dir / shpool_id).exists())
        self.assertFalse((start_dir / f"{shpool_id}.expected").exists())
        self.assertEqual(decoy_content, decoy.read_text())
        actions = self.fixture.shpool_log.read_text().splitlines()
        self.assertFalse(any(line.startswith("kill ") for line in actions))

    def test_exact_ai_startup_accepts_launch_records_consumed_by_shell(self) -> None:
        """The launcher shell removes its armed request before starting Claude.

        Provider proof must accept that normal handoff after independently
        matching the exact shpool generation and the live provider.
        """
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "2",
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_CONSUME_ARMED_LAUNCH": "1",
            }
        )
        proven = run([SP, "new", "claude", "fixture"], env=env)
        shpool_id = proven.stdout.strip().splitlines()[-1]
        self.assertRegex(shpool_id, r"^s[0-9]{8}-[0-9]{6}-[0-9]+$")
        self.assertFalse((self.fixture.start / shpool_id).exists())
        self.assertFalse((self.fixture.start / f"{shpool_id}.expected").exists())
        actions = self.fixture.shpool_log.read_text().splitlines()
        self.assertFalse(any(line.startswith("kill ") for line in actions))

    def test_provider_startup_proof_uses_guard_with_unrelated_unknown_session(self) -> None:
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_DYNAMIC_INCLUDE_UNKNOWN": "1",
            }
        )
        proven = run([SP, "new", "claude", "fixture"], env=env)
        shpool_id = proven.stdout.strip().splitlines()[-1]
        self.assertTrue(shpool_id.startswith("s"))
        self.assertFalse((self.fixture.start / shpool_id).exists())
        self.assertFalse((self.fixture.start / f"{shpool_id}.expected").exists())

    def test_verify_start_reproves_and_clears_a_retained_exact_record(self) -> None:
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
            }
        )
        failed = run([SP, "new", "claude", "fixture"], env=env, check=False)
        self.assertNotEqual(0, failed.returncode)
        shpool_id = json.loads(self.fixture.shpool_state.read_text())["sessions"][0]["name"]
        record = self.fixture.start / shpool_id
        expected = self.fixture.start / f"{shpool_id}.expected"
        self.assertTrue(record.is_file())
        self.assertTrue(expected.is_file())

        env["STUB_DYNAMIC_PROVIDER"] = "claude"
        verified = run([SP, "verify-start", shpool_id], env=env)
        self.assertIn(f"cleared retained launch record for {shpool_id}", verified.stdout)
        self.assertFalse(record.exists())
        self.assertFalse(expected.exists())
        self.assertFalse(any(
            line.startswith("kill ")
            for line in self.fixture.shpool_log.read_text().splitlines()
        ))

    def test_verify_start_refuses_sidecar_from_previous_boot(self) -> None:
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
            }
        )
        failed = run([SP, "new", "claude", "fixture"], env=env, check=False)
        self.assertNotEqual(0, failed.returncode)
        shpool_id = json.loads(self.fixture.shpool_state.read_text())["sessions"][0]["name"]
        record = self.fixture.start / shpool_id
        expected = self.fixture.start / f"{shpool_id}.expected"
        self.assertTrue(record.is_file())
        self.assertTrue(expected.is_file())

        self.fixture.boot_id.write_text("next-boot\n", encoding="utf-8")
        env["STUB_DYNAMIC_PROVIDER"] = "claude"
        refused = run([SP, "verify-start", shpool_id], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("exact provider is not active yet", refused.stderr)
        self.assertTrue(record.is_file())
        self.assertTrue(expected.is_file())

    def test_restore_same_provider_duplicate_skips_null_shell_then_refuses_outside_uuid(self) -> None:
        exact_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        shell = session_row("shell-root", provider="shell", uuid="")
        shell["identity"]["uuid"] = None
        shell["recovery"] = {"available": False}
        document = inventory_document(shell)
        document["outside_agents"] = [
            {
                "provider": "claude",
                "identity": {"uuid": exact_uuid},
                "title": "already active outside shpool",
                "cwd": str(self.fixture.project),
            }
        ]
        self.fixture.inventory.write_text(json.dumps(document), encoding="utf-8")
        refused = run(
            [SP, "restore-exact", "claude", exact_uuid, self.fixture.project],
            env=self.fixture.env(),
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("is already active", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())
        self.assertFalse(self.fixture.start.exists())

    def test_restore_allows_same_uuid_active_under_other_provider(self) -> None:
        exact_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        document = inventory_document()
        document["outside_agents"] = [
            {
                "provider": "claude",
                "identity": {"uuid": exact_uuid},
                "title": "Claude identity with same UUID bytes",
                "cwd": str(self.fixture.project),
            }
        ]
        self.fixture.inventory.write_text(json.dumps(document), encoding="utf-8")
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "codex",
                "STUB_DYNAMIC_AFTER_SESSIONS": "0",
                "STUB_DYNAMIC_UUID": exact_uuid,
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
            }
        )
        restored = run(
            [SP, "restore-exact", "codex", exact_uuid, self.fixture.project],
            env=env,
        )
        self.assertNotIn("already active", restored.stderr)
        shpool_id = restored.stdout.strip().splitlines()[-1]
        self.assertRegex(shpool_id, r"^s[0-9]{8}-[0-9]{6}-[0-9]+$")
        self.assertFalse((self.fixture.start / shpool_id).exists())
        self.assertFalse((self.fixture.start / f"{shpool_id}.expected").exists())

    def test_new_codex_session_is_proven_by_its_process_not_a_rollout(self) -> None:
        """The bug that started all of this.

        Codex publishes its identity only once it has a rollout file, and it
        does not write one until the human sends a first message. `sp new codex`
        waited for that identity, timed out, and returned without ever
        attaching -- leaving a live Codex session orphaned and labelled "setup
        incomplete". Claude was unaffected because `claude agents --json` lists
        a new session immediately. Startup is proven from the process tree.
        """
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "2",
                "STUB_DYNAMIC_PROVIDER": "unknown",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "SESSION_KIT_PROVIDER_PRESENCE_OVERRIDE": "present",
            }
        )
        started = run([SP, "new", "codex"], env=env, cwd=self.fixture.project)
        shpool_id = started.stdout.strip().splitlines()[-1]
        self.assertRegex(shpool_id, r"^s[0-9]{8}-[0-9]{6}-[0-9]+$")
        # A proven launch clears both records.
        self.assertFalse((self.fixture.start / shpool_id).exists())
        self.assertFalse((self.fixture.start / f"{shpool_id}.expected").exists())

    def test_new_session_is_refused_when_the_provider_never_started(self) -> None:
        """Relaxing the identity check must not accept an empty shell."""
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "2",
                "STUB_DYNAMIC_PROVIDER": "unknown",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "SESSION_KIT_PROVIDER_PRESENCE_OVERRIDE": "absent",
            }
        )
        refused = run(
            [SP, "new", "codex"], env=env, check=False, cwd=self.fixture.project
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("startup was not proven", refused.stderr)

    def test_resume_still_requires_its_exact_conversation(self) -> None:
        """The relaxation is for new sessions only.

        A resume that accepted a process instead of its exact UUID could bring
        back the wrong conversation.
        """
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "2",
                "STUB_DYNAMIC_PROVIDER": "unknown",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "SESSION_KIT_PROVIDER_PRESENCE_OVERRIDE": "present",
            }
        )
        refused = run(
            [
                SP,
                "restore-exact",
                "codex",
                "00000000-0000-4000-8000-000000000009",
                self.fixture.project,
            ],
            env=env,
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("recovery was not proven", refused.stderr)

    def test_repair_closes_the_dead_session_then_restores_its_conversation(self) -> None:
        """`sp repair` is the unattended half of the wedge fix.

        shpool can lose the thread that serves a session's terminal. The
        session then cannot be attached again and whatever runs inside it
        blocks on its next write for ever. Repair ends that session and brings
        the same conversation up in a new one.
        """
        exact_uuid = "00000000-0000-4000-8000-000000000001"
        row = session_row("wedged")
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(row)), encoding="utf-8"
        )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "wedged",
                            "status": "Disconnected",
                            "started_at_unix_ms": row["started_at_unix_ms"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "codex",
                "STUB_DYNAMIC_UUID": exact_uuid,
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "FAKE_EXPECT_KILL_LOCK": "locked",
            }
        )
        repaired = run([SP, "repair", "wedged"], env=env)
        log = self.fixture.shpool_log.read_text()
        # The dead session is ended before anything is launched, and the
        # replacement is created detached so it waits in the session list.
        self.assertIn("kill wedged\n", log)
        self.assertLess(log.index("kill wedged"), log.index("attach"))
        new_id = repaired.stdout.strip().splitlines()[-1]
        self.assertRegex(new_id, r"^s[0-9]{8}-[0-9]{6}-[0-9]+$")
        self.assertNotEqual("wedged", new_id)

    def test_repair_refuses_a_session_with_no_conversation_to_move(self) -> None:
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(unknown_session_row("halfstarted"))),
            encoding="utf-8",
        )
        refused = run([SP, "repair", "halfstarted"], env=self.fixture.env(), check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_restore_is_not_blocked_by_an_unrelated_unknown_session(self) -> None:
        """A half-started session must not be able to block recovery.

        Recovery previously demanded a perfect inventory, so any session that
        had not yet published a provider identity -- a shell opened seconds
        ago, or one sitting on a provider's startup prompt -- made every
        restore refuse. That turned an unrelated row into an outage during the
        exact moment recovery is needed. The duplicate-conversation guard is
        what protects this operation, and it is covered separately by
        test_restore_same_provider_duplicate_skips_null_shell_then_refuses_outside_uuid.
        """
        exact_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(unknown_session_row())),
            encoding="utf-8",
        )
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "codex",
                "STUB_DYNAMIC_AFTER_SESSIONS": "0",
                "STUB_DYNAMIC_UUID": exact_uuid,
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
            }
        )
        restored = run(
            [SP, "restore-exact", "codex", exact_uuid, self.fixture.project],
            env=env,
        )
        self.assertNotIn("strict live snapshot unavailable", restored.stderr)
        self.assertTrue(self.fixture.shpool_log.exists())
        shpool_id = restored.stdout.strip().splitlines()[-1]
        self.assertRegex(shpool_id, r"^s[0-9]{8}-[0-9]{6}-[0-9]+$")

    def test_stale_expected_namespace_forces_allocator_to_next_exact_id(self) -> None:
        fake_bin = self.fixture.base / "allocator-bin"
        fake_bin.mkdir()
        stale_content = b"stale-sidecar-reservation\n"
        write_executable(
            fake_bin / "date",
            """#!/usr/bin/env bash
if [[ ${1:-} == +%Y%m%d-%H%M%S ]]; then
  mkdir -p "$SESSION_KIT_START_DIR"
  owner_pid=$(awk '{print $4}' "/proc/$PPID/stat")
  printf 'stale-sidecar-reservation\\n' > "$SESSION_KIT_START_DIR/s20260728-010203-$owner_pid.expected"
  printf '20260728-010203\\n'
  exit 0
fi
exec /bin/date "$@"
""",
        )
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            }
        )
        created = run([SP, "new", "shell", "fixture"], env=env)
        shpool_id = created.stdout.strip().splitlines()[-1]
        self.assertRegex(shpool_id, r"^s20260728-010203-[0-9]+-1$")
        reserved = self.fixture.start / f"{shpool_id.removesuffix('-1')}.expected"
        self.assertEqual(stale_content, reserved.read_bytes())
        self.assertFalse((self.fixture.start / shpool_id).exists())
        self.assertFalse((self.fixture.start / f"{shpool_id}.expected").exists())

    def test_start_writer_refuses_existing_expected_namespace(self) -> None:
        self.fixture.start.mkdir()
        expected = self.fixture.start / "collision.expected"
        expected.write_bytes(b"existing-sidecar\n")
        refused = run(
            [
                "bash",
                "-c",
                'source "$1"; sk_write_start_record collision shell "$2"',
                "writer-collision-test",
                COMMON,
                self.fixture.project,
            ],
            env=self.fixture.env(),
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertFalse((self.fixture.start / "collision").exists())
        self.assertEqual(b"existing-sidecar\n", expected.read_bytes())

    def test_launch_records_accept_legacy_and_reject_multiline_or_extra_fields(self) -> None:
        self.fixture.start.mkdir()
        source_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        start = self.fixture.start / "launch"
        expected = self.fixture.start / "launch.expected"
        legacy_start = f"claude\t{self.fixture.project}\t{source_uuid}\n"
        legacy_expected = (
            f"claude\t{self.fixture.project}\tfixture-boot\t1700000000001\t"
            f"1001\t10010\t10\t100\t{source_uuid}\n"
        )
        fork_start = legacy_start.rstrip("\n") + "\tfork\n"
        fork_expected = legacy_expected.rstrip("\n") + "\tfork\n"

        def read_record() -> subprocess.CompletedProcess[str]:
            return run(
                [
                    "bash",
                    "-c",
                    'source "$1"; sk_read_start_expectation launch && '
                    'printf "%s\\n" "$SK_EXPECT_LAUNCH_MODE"',
                    "launch-record-test",
                    COMMON,
                ],
                env=self.fixture.env(),
                check=False,
            )

        start.write_text(legacy_start, encoding="utf-8")
        expected.write_text(legacy_expected, encoding="utf-8")
        self.assertEqual("resume\n", read_record().stdout)

        start.write_text(fork_start, encoding="utf-8")
        expected.write_text(fork_expected, encoding="utf-8")
        self.assertEqual("fork\n", read_record().stdout)

        invalid_pairs = [
            (fork_start.rstrip("\n") + "\textra\n", fork_expected),
            (fork_start, fork_expected.rstrip("\n") + "\textra\n"),
            (fork_start + "second-line\n", fork_expected),
            (fork_start, fork_expected + "second-line\n"),
        ]
        for index, (main_value, side_value) in enumerate(invalid_pairs):
            with self.subTest(index=index):
                start.write_text(main_value, encoding="utf-8")
                expected.write_text(side_value, encoding="utf-8")
                rejected = read_record()
                self.assertNotEqual(0, rejected.returncode)

    def test_attach_race_cannot_leave_a_replacement_shell(self) -> None:
        row = session_row("target")
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(row)), encoding="utf-8"
        )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "target",
                            "status": "Disconnected",
                            "started_at_unix_ms": row["started_at_unix_ms"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        env = self.fixture.env()
        env["FAKE_DROP_BEFORE_ATTACH"] = "1"
        self.fixture.journals.mkdir()
        (self.fixture.journals / "target.raw").write_text(
            "old terminal output must not replay automatically\n", encoding="utf-8"
        )
        attached = run([SP, "go", "target"], env=env)
        self.assertEqual(0, attached.returncode)
        self.assertNotIn("old terminal output", attached.stdout)
        self.assertEqual([], json.loads(self.fixture.shpool_state.read_text())["sessions"])
        self.assertEqual("attach target\n", self.fixture.shpool_log.read_text())

    def test_no_journal_sentinel_disables_wrapper_and_consumes_shell_launch(self) -> None:
        (self.fixture.home / ".no_shpool_journal").write_text("", encoding="utf-8")
        self.fixture.start.mkdir()
        record = self.fixture.start / "sentinel-session"
        record.write_text(
            f"shell\t{self.fixture.project}\t\n", encoding="utf-8"
        )
        fake_bin = self.fixture.base / "fake-bin"
        fake_bin.mkdir()
        script_log = self.fixture.base / "script-wrapper.log"
        write_executable(
            fake_bin / "script",
            "#!/usr/bin/env bash\nprintf called > \"$SCRIPT_WRAPPER_LOG\"\nexit 99\n",
        )
        env = self.fixture.env()
        env.update(
            {
                "SHPOOL_SESSION_NAME": "sentinel-session",
                "SHPOOL_JOURNAL": "",
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "SCRIPT_WRAPPER_LOG": str(script_log),
            }
        )
        sourced = run(
            [
                "bash",
                "-c",
                """
bash --noprofile --norc -ic '
  shell_start=$(awk "{print \\$22}" /proc/$$/stat)
  daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat)
  printf "shell\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t\\n" \
    "$2" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/sentinel-session.expected"
  source "$1"
  printf "journal=%s\\n" "$SHPOOL_JOURNAL"
' journal-disabled-inner "$1" "$2" "$3"
""",
                "journal-disabled-test",
                REPO / "bashrc/shpool.bashrc",
                self.fixture.project,
                self.fixture.start,
            ],
            env=env,
        )
        self.assertIn("journal=disabled", sourced.stdout)
        self.assertFalse(script_log.exists())
        self.assertFalse(record.exists())
        self.assertFalse((self.fixture.start / "sentinel-session.expected").exists())

    def test_previous_boot_armed_record_never_executes_provider(self) -> None:
        (self.fixture.home / ".no_shpool_journal").write_text("", encoding="utf-8")
        self.fixture.start.mkdir()
        record = self.fixture.start / "stale-session"
        record.write_text(
            f"claude\t{self.fixture.project}\t\n", encoding="utf-8"
        )
        fake_bin = self.fixture.base / "stale-bin"
        fake_bin.mkdir()
        provider_log = self.fixture.base / "provider-launch.log"
        write_executable(
            fake_bin / "claude",
            "#!/usr/bin/env bash\nprintf launched > \"$PROVIDER_LAUNCH_LOG\"\n",
        )
        env = self.fixture.env()
        env.update(
            {
                "SHPOOL_SESSION_NAME": "stale-session",
                "SHPOOL_JOURNAL": "",
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "PROVIDER_LAUNCH_LOG": str(provider_log),
            }
        )
        sourced = run(
            [
                "bash",
                "-c",
                """
bash --noprofile --norc -ic '
  shell_start=$(awk "{print \\$22}" /proc/$$/stat)
  daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat)
  printf "claude\\t%s\\tprevious-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t\\n" \
    "$2" "$$" "$shell_start" "$PPID" "$daemon_start" > "$3/stale-session.expected"
  source "$1"
' stale-inner "$1" "$2" "$3"
""",
                "stale-launch-test",
                REPO / "bashrc/shpool.bashrc",
                self.fixture.project,
                self.fixture.start,
            ],
            env=env,
        )
        self.assertIn("stale or mismatched launch record retained", sourced.stderr)
        self.assertFalse(provider_log.exists())
        self.assertTrue(record.exists())
        self.assertTrue((self.fixture.start / "stale-session.expected").exists())

    def test_fork_launch_mode_executes_only_provider_fork_primitives(self) -> None:
        (self.fixture.home / ".no_shpool_journal").write_text("", encoding="utf-8")
        source_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        expected_args = {
            "claude": f"--resume {source_uuid} --fork-session\n",
            # Codex is launched with its startup upgrade prompt suppressed: a
            # managed session has nobody to press a key, and the prompt would
            # hold the session at "setup incomplete" forever.
            "codex": (
                "-c check_for_update_on_startup=false "
                f"--no-alt-screen fork {source_uuid}\n"
            ),
        }
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                if self.fixture.start.exists():
                    shutil.rmtree(self.fixture.start)
                self.fixture.start.mkdir()
                record = self.fixture.start / "fork-session"
                record.write_text(
                    f"{provider}\t{self.fixture.project}\t{source_uuid}\tfork\n",
                    encoding="utf-8",
                )
                fake_bin = self.fixture.base / f"{provider}-fork-bin"
                fake_bin.mkdir()
                provider_log = self.fixture.base / f"{provider}-fork.log"
                write_executable(
                    fake_bin / provider,
                    '#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "$PROVIDER_LAUNCH_LOG"\n',
                )
                env = self.fixture.env()
                env.update(
                    {
                        "SHPOOL_SESSION_NAME": "fork-session",
                        "SHPOOL_JOURNAL": "",
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "PROVIDER_LAUNCH_LOG": str(provider_log),
                    }
                )
                launched = run(
                    [
                        "bash",
                        "-c",
                        """
bash --noprofile --norc -ic '
  shell_start=$(awk "{print \\$22}" /proc/$$/stat)
  daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat)
  printf "%s\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t%s\\tfork\\n" \
    "$4" "$2" "$$" "$shell_start" "$PPID" "$daemon_start" "$5" > "$3/fork-session.expected"
  source "$1"
' fork-launch-inner "$1" "$2" "$3" "$4" "$5"
""",
                        "fork-launch-test",
                        BASHRC,
                        self.fixture.project,
                        self.fixture.start,
                        provider,
                        source_uuid,
                    ],
                    env=env,
                )
                self.assertEqual(0, launched.returncode, launched.stderr)
                self.assertEqual(expected_args[provider], provider_log.read_text())
                self.assertEqual(
                    record.exists(),
                    (self.fixture.start / "fork-session.expected").exists(),
                )

    def test_unarmed_creation_failures_are_quarantined(self) -> None:
        attach_env = self.fixture.env()
        attach_env["SESSION_KIT_BACKGROUND"] = "1"
        attach_env["FAKE_ATTACH_FAIL"] = "1"
        failed_attach = run(
            [SP, "new", "shell", "fixture"], env=attach_env, check=False
        )
        self.assertNotEqual(0, failed_attach.returncode)
        self.assertIn("quarantined", failed_attach.stderr)
        failed_dir = self.fixture.start / "failed"
        self.assertEqual(1, len(list(failed_dir.glob("*.attach-failed.start"))))
        self.assertFalse(
            any(
                path.is_file()
                for path in self.fixture.start.iterdir()
                if path.name != "failed"
            )
        )

        generation_env = self.fixture.env()
        generation_env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_DYNAMIC_STARTED": "1",
            }
        )
        failed_generation = run(
            [SP, "new", "shell", "fixture"], env=generation_env, check=False
        )
        self.assertNotEqual(0, failed_generation.returncode)
        self.assertIn("unarmed launch record quarantined", failed_generation.stderr)
        self.assertEqual(1, len(list(failed_dir.glob("*.generation-unproven.start"))))

    def test_generation_sidecar_publish_failure_quarantines_main_record(self) -> None:
        fake_bin = self.fixture.base / "arming-bin"
        fake_bin.mkdir()
        write_executable(
            fake_bin / "mv",
            """#!/usr/bin/env bash
destination=${@: -1}
if [[ $destination == *.expected ]]; then exit 9; fi
exec /bin/mv "$@"
""",
        )
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            }
        )
        failed = run([SP, "new", "shell", "fixture"], env=env, check=False)
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("arming-failed", failed.stderr)
        failed_dir = self.fixture.start / "failed"
        self.assertEqual(1, len(list(failed_dir.glob("*.arming-failed.start"))))
        self.assertFalse(any(self.fixture.start.glob("s*")))

    def test_restore_uuid_requires_canonical_shape_and_normalizes_case(self) -> None:
        invalid = run(
            [SP, "restore-exact", "claude", "not-a-uuid", self.fixture.project],
            env=self.fixture.env(),
            check=False,
        )
        self.assertNotEqual(0, invalid.returncode)
        self.assertIn("exact conversation UUID", invalid.stderr)

        uppercase = "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
            }
        )
        failed = run(
            [SP, "restore-exact", "claude", uppercase, self.fixture.project],
            env=env,
            check=False,
        )
        self.assertNotEqual(0, failed.returncode)
        state = json.loads(self.fixture.shpool_state.read_text())
        shpool_id = state["sessions"][0]["name"]
        self.assertTrue(
            (self.fixture.start / shpool_id)
            .read_text()
            .endswith(uppercase.lower() + "\tresume\n")
        )
        self.assertTrue(
            (self.fixture.start / f"{shpool_id}.expected")
            .read_text()
            .endswith("\t" + uppercase.lower() + "\tresume\n")
        )

    def test_close_revalidates_exact_identity_and_refuses_changed_target(self) -> None:
        row = session_row("target")
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(row)), encoding="utf-8"
        )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "target",
                            "status": "Disconnected",
                            "started_at_unix_ms": row["started_at_unix_ms"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        changed = self.fixture.base / "changed.json"
        changed.write_text(
            json.dumps(
                inventory_document(
                    session_row(
                        "target",
                        uuid="00000000-0000-4000-8000-000000000999",
                    )
                )
            ),
            encoding="utf-8",
        )
        env = self.fixture.env()
        env["SESSION_KIT_CONFIRM_ID"] = "target"
        env["STUB_SECOND_INVENTORY"] = str(changed)
        refused = run([SP, "close", "1"], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("target changed", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())

        self.fixture.snapshot_count.unlink()
        env.pop("STUB_SECOND_INVENTORY")
        self.fixture.start.mkdir(exist_ok=True)
        retained = self.fixture.start / "target"
        retained_expected = self.fixture.start / "target.expected"
        retained.write_text("retained-start\n", encoding="utf-8")
        retained_expected.write_text("retained-expected\n", encoding="utf-8")
        closed = run([SP, "close", "target"], env=env)
        self.assertIn("Closed exact session target", closed.stdout)
        self.assertEqual("kill target\n", self.fixture.shpool_log.read_text())
        self.assertFalse(retained.exists())
        self.assertFalse(retained_expected.exists())
        archived = list((self.fixture.start / "failed").glob("target.*.closed.*"))
        self.assertEqual(2, len(archived))

    def test_target_revalidation_uses_guard_with_unrelated_unknown_session(self) -> None:
        target = session_row("target")
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(target, unknown_session_row())),
            encoding="utf-8",
        )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "target",
                            "status": "Disconnected",
                            "started_at_unix_ms": target["started_at_unix_ms"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        env = self.fixture.env()
        env["SESSION_KIT_CONFIRM_ID"] = "target"
        closed = run([SP, "close", "target"], env=env)
        self.assertIn("Closed exact session target", closed.stdout)
        self.assertEqual("kill target\n", self.fixture.shpool_log.read_text())

    def test_guard_failure_refuses_target_without_kill(self) -> None:
        target = session_row("target")
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(target)), encoding="utf-8"
        )
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_CONFIRM_ID": "target",
                "STUB_GUARD_FAIL": "1",
            }
        )
        refused = run([SP, "close", "target"], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("target changed", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_prompt_waiting_cache_recomputes_state_root_after_source_cleanup(self) -> None:
        xdg_state = self.fixture.base / "prompt-state"
        cache = xdg_state / "session-kit" / "waiting-count"
        cache.parent.mkdir(parents=True)
        cache.write_text("2\n", encoding="utf-8")
        env = {
            **os.environ,
            "HOME": str(self.fixture.home),
            "XDG_STATE_HOME": str(xdg_state),
            "SHPOOL_SESSION_NAME": "main",
            "SHPOOL_JOURNAL": "disabled",
        }
        prompted = run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-ic",
                'source "$1"; stat() { date +%s; }; __sk_waiting',
                "prompt-state-test",
                BASHRC,
            ],
            env=env,
        )
        self.assertIn("●2", prompted.stdout)

    def test_unsafe_raw_id_is_visible_but_refused_before_mutation(self) -> None:
        unsafe = session_row("main-template")
        unsafe["mutation_allowed"] = False
        unsafe["mutation_rejection_reason"] = "template"
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(unsafe)), encoding="utf-8"
        )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "main-template",
                            "status": "Disconnected",
                            "started_at_unix_ms": unsafe["started_at_unix_ms"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        env = self.fixture.env()
        env["SESSION_KIT_CONFIRM_ID"] = "main-template"
        refused = run([SP, "close", "1"], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("display-only and cannot be mutated", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_reaper_requires_two_observations_never_kills_and_preserves_active_inode(self) -> None:
        proc_root = self.fixture.base / "proc"
        proc_root.mkdir()
        self._write_proc(proc_root, 10, 1, "shpool", b"shpool\0daemon\0")
        self._write_proc(
            proc_root,
            100,
            10,
            "bash",
            b"bash\0",
            b"SHPOOL_SESSION_NAME=old\0",
        )
        self._write_proc(
            proc_root,
            101,
            10,
            "bash",
            b"bash\0",
            b"SHPOOL_SESSION_NAME=active\0",
        )
        old_ms = int((time.time() - 9 * 86400) * 1000)
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "old",
                            "status": "Disconnected",
                            "started_at_unix_ms": old_ms - 10_000,
                            "last_disconnected_at_unix_ms": old_ms,
                        },
                        {
                            "name": "active",
                            "status": "Attached",
                            "started_at_unix_ms": old_ms,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.fixture.journals.mkdir()
        active = self.fixture.journals / "active.raw"
        active.write_bytes(b"\x00control\x1b[31m snowman=\xe2\x98\x83\n")
        before = (active.stat().st_ino, hashlib.sha256(active.read_bytes()).hexdigest())
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_PROC_ROOT": str(proc_root),
                "SESSION_KIT_DAEMON_PID": "10",
                "SESSION_KIT_REAPER_SENTINEL": str(self.fixture.base / "not-disabled"),
            }
        )
        first = run([REAPER], env=env)
        self.assertIn("candidates=0 actions=0", first.stdout)
        second = run([REAPER], env=env)
        self.assertIn("candidates=1 actions=0", second.stdout)
        candidates = json.loads(
            (self.fixture.state / "prune-candidates.json").read_text()
        )
        self.assertEqual(["old"], [x["shpool_id"] for x in candidates["candidates"]])
        after = (active.stat().st_ino, hashlib.sha256(active.read_bytes()).hexdigest())
        self.assertEqual(before, after)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_verify_candidate_requires_exact_tuple_and_unchanged_empty_tree(self) -> None:
        proc_root = self.fixture.base / "verify-proc"
        proc_root.mkdir()
        self._write_proc(proc_root, 10, 1, "shpool", b"shpool\0daemon\0")
        self._write_proc(
            proc_root,
            100,
            10,
            "bash",
            b"bash\0",
            b"SHPOOL_SESSION_NAME=main\0",
        )
        started = 1_700_000_000_000
        old_disconnected = int((time.time() - 8 * 86400) * 1000)
        candidate = self.fixture.base / "candidate.json"
        candidate.write_text(
            json.dumps(
                {
                    "shpool_id": "main",
                    "started_at_unix_ms": started,
                    "last_disconnected_at_unix_ms": old_disconnected,
                    "shell_pid": 100,
                    "shell_start_ticks": 10_000,
                }
            ),
            encoding="utf-8",
        )
        candidate.chmod(0o600)
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_PROC_ROOT": str(proc_root),
                "SESSION_KIT_DAEMON_PID": "10",
                "SESSION_KIT_REAPER_SENTINEL": str(self.fixture.base / "enabled"),
            }
        )

        def set_state(
            status: str,
            exact_started: int,
            disconnected: int = old_disconnected,
        ) -> None:
            self.fixture.shpool_state.write_text(
                json.dumps(
                    {
                        "sessions": [
                            {
                                "name": "main",
                                "status": status,
                                "started_at_unix_ms": exact_started,
                                "last_disconnected_at_unix_ms": disconnected,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

        set_state("Disconnected", started)
        verified = run([REAPER, "--verify-candidate", candidate], env=env)
        self.assertIn("candidate verified exact", verified.stdout)

        set_state("Attached", started)
        attached = run(
            [REAPER, "--verify-candidate", candidate], env=env, check=False
        )
        self.assertNotEqual(0, attached.returncode)

        set_state("Disconnected", started + 1)
        replaced = run(
            [REAPER, "--verify-candidate", candidate], env=env, check=False
        )
        self.assertNotEqual(0, replaced.returncode)

        set_state(
            "Disconnected",
            started,
            int((time.time() - 60) * 1000),
        )
        recently_reconnected = run(
            [REAPER, "--verify-candidate", candidate], env=env, check=False
        )
        self.assertNotEqual(0, recently_reconnected.returncode)

        set_state("Disconnected", started)
        self._write_proc(proc_root, 200, 100, "sleep", b"sleep\0100\0")
        child_added = run(
            [REAPER, "--verify-candidate", candidate], env=env, check=False
        )
        self.assertNotEqual(0, child_added.returncode)

        sentinel = Path(env["SESSION_KIT_REAPER_SENTINEL"])
        sentinel.write_text("", encoding="utf-8")
        disabled = run(
            [REAPER, "--verify-candidate", candidate], env=env, check=False
        )
        self.assertEqual(3, disabled.returncode)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_stale_prune_candidate_fails_final_verifier_and_never_kills(self) -> None:
        proc_root = self.fixture.base / "stale-proc"
        proc_root.mkdir()
        self._write_proc(proc_root, 10, 1, "shpool", b"shpool\0daemon\0")
        self._write_proc(
            proc_root,
            100,
            10,
            "bash",
            b"bash\0",
            b"SHPOOL_SESSION_NAME=main\0",
        )
        old_ms = int((time.time() - 9 * 86400) * 1000)
        state_a = self.fixture.base / "state-a.json"
        state_b = self.fixture.base / "state-b.json"
        state_a.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "main",
                            "status": "Disconnected",
                            "started_at_unix_ms": old_ms - 1000,
                            "last_disconnected_at_unix_ms": old_ms,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        state_b.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "main",
                            "status": "Attached",
                            "started_at_unix_ms": old_ms - 1000,
                            "last_disconnected_at_unix_ms": old_ms,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        shutil.copyfile(state_a, self.fixture.shpool_state)
        self.fixture.inventory.write_text(
            json.dumps(
                inventory_document(
                    session_row(
                        "main",
                        started=old_ms - 1000,
                        provider="codex",
                    )
                )
            ),
            encoding="utf-8",
        )
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_PROC_ROOT": str(proc_root),
                "SESSION_KIT_DAEMON_PID": "10",
                "SESSION_KIT_REAPER_SENTINEL": str(self.fixture.base / "enabled"),
            }
        )
        run([REAPER], env=env)
        run([REAPER], env=env)
        self.assertEqual(
            1,
            len(
                json.loads(
                    (self.fixture.state / "prune-candidates.json").read_text()
                )["candidates"]
            ),
        )

        sequence = self.fixture.base / "sequence-shpool"
        counter = self.fixture.base / "sequence-count"
        kill_log = self.fixture.base / "sequence-kill.log"
        write_executable(
            sequence,
            """#!/usr/bin/env python3
import json, os, pathlib, sys
counter=pathlib.Path(os.environ["SEQUENCE_COUNT"])
try: count=int(counter.read_text())
except (OSError, ValueError): count=0
args=sys.argv[1:]
if args == ["list", "--json"]:
    count += 1
    counter.write_text(str(count))
    source=os.environ["SEQUENCE_A"] if count == 1 else os.environ["SEQUENCE_B"]
    print(pathlib.Path(source).read_text(), end="")
    raise SystemExit(0)
if len(args) == 2 and args[0] == "kill":
    pathlib.Path(os.environ["SEQUENCE_KILL_LOG"]).write_text(args[1])
    raise SystemExit(0)
raise SystemExit(2)
""",
        )
        env.update(
            {
                "SESSION_KIT_SHPOOL_CMD": str(sequence),
                "SESSION_KIT_CONFIRM_ID": "main",
                "SEQUENCE_COUNT": str(counter),
                "SEQUENCE_A": str(state_a),
                "SEQUENCE_B": str(state_b),
                "SEQUENCE_KILL_LOG": str(kill_log),
            }
        )
        stale = run([SP, "prune"], env=env, check=False)
        self.assertNotEqual(0, stale.returncode)
        self.assertIn("changed or is no longer empty", stale.stderr)
        self.assertFalse(kill_log.exists())

    def _write_proc(
        self,
        root: Path,
        pid: int,
        ppid: int,
        comm: str,
        cmdline: bytes,
        environ: bytes = b"",
    ) -> None:
        directory = root / str(pid)
        directory.mkdir()
        fields = ["S", str(ppid), *(["0"] * 17), str(pid * 100)]
        (directory / "stat").write_text(
            f"{pid} ({comm}) {' '.join(fields)}\n", encoding="utf-8"
        )
        (directory / "comm").write_text(comm + "\n", encoding="utf-8")
        (directory / "cmdline").write_bytes(cmdline)
        (directory / "environ").write_bytes(environ)


class PickerProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CommandFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def _prime(self, row: dict) -> Path:
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(row)), encoding="utf-8"
        )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": row["shpool_id"],
                            "status": row["shpool_status"],
                            "started_at_unix_ms": row["started_at_unix_ms"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return write_picker_proof(self.fixture.base / "picker-proof.json", row)

    def _live_idle_codex(self, status: str) -> tuple[dict, subprocess.Popen]:
        executable = self.fixture.base / "codex"
        executable.symlink_to("/bin/sleep")
        process = subprocess.Popen([executable, "60"])
        deadline = time.monotonic() + 2
        stat_text = ""
        while time.monotonic() < deadline:
            try:
                stat_text = Path(f"/proc/{process.pid}/stat").read_text()
                break
            except OSError:
                time.sleep(0.01)
        self.assertTrue(stat_text)
        start = int(stat_text.rsplit(")", 1)[1].split()[19])
        live = session_row("main2", status=status)
        live["identity"]["pid"] = process.pid
        live["identity"]["process_start_ticks"] = start
        live["agent_status"] = "idle"
        live["recent_output_age_seconds"] = 300
        return live, process

    def _mark_title_pending(self) -> Path:
        marker_root = self.fixture.state / "provider-untitled"
        marker_root.mkdir(exist_ok=True)
        marker = marker_root / "main2"
        marker.touch()
        return marker

    def test_picker_open_uses_exact_proof_and_releases_lock_before_attach(self) -> None:
        proof = self._prime(session_row("main2"))
        env = self.fixture.env()
        env["FAKE_EXPECT_CREATE_LOCK"] = "unlocked"
        opened = run([SP, "picker-open", proof], env=env)
        self.assertEqual("", opened.stdout)
        self.assertEqual("attach main2\n", self.fixture.shpool_log.read_text())

    def test_picker_takeover_confirms_exact_id_and_uses_force_attach(self) -> None:
        proof = self._prime(session_row("main2", status="Attached"))
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_CONFIRM_ID": "main2",
                "FAKE_EXPECT_CREATE_LOCK": "unlocked",
            }
        )
        moved = run([SP, "picker-takeover", proof], env=env)
        self.assertIn("Move session to this window", moved.stdout)
        # The confirmation names the session instead of demanding that its ID
        # be retyped.
        self.assertIn("main2", moved.stdout)
        self.assertNotIn("Type the exact ID", moved.stdout)
        self.assertEqual("attach main2\n", self.fixture.shpool_log.read_text())

    def test_attached_takeover_never_restarts_pending_title_automatically(self) -> None:
        row, process = self._live_idle_codex("Attached")
        proof = self._prime(row)
        marker = self._mark_title_pending()
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_CONFIRM_ID": "main2",
                "STUB_CODEX_BOUNCE_TITLE": "Release Notes",
            }
        )
        try:
            moved = run([SP, "picker-takeover", proof], env=env)
            self.assertEqual(0, moved.returncode)
            self.assertIsNone(process.poll())
            self.assertTrue(marker.exists())
        finally:
            process.terminate()
            process.wait(timeout=2)

    def test_explicit_pending_title_refresh_restarts_only_idle_exact_codex(self) -> None:
        row, process = self._live_idle_codex("Attached")
        proof = self._prime(row)
        marker = self._mark_title_pending()
        env = self.fixture.env()
        env["STUB_CODEX_BOUNCE_TITLE"] = "Release Notes"
        try:
            refreshed = run([SP, "picker-title-refresh", proof], env=env)
            process.wait(timeout=2)
            self.assertEqual(0, refreshed.returncode)
            self.assertIn("Restarted the idle Codex provider", refreshed.stdout)
            self.assertFalse(marker.exists())
            bounce = self.fixture.state / "provider-bounce" / "main2"
            self.assertEqual(row["identity"]["uuid"], bounce.read_text().strip())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)

    def test_app_server_title_refresh_restarts_only_remote_tui(self) -> None:
        row, app_server = self._live_idle_codex("Attached")
        executable = self.fixture.base / "codex"
        remote_tui = subprocess.Popen([executable, "60"])
        deadline = time.monotonic() + 2
        remote_start = None
        while time.monotonic() < deadline:
            try:
                stat_text = Path(f"/proc/{remote_tui.pid}/stat").read_text()
                remote_start = int(stat_text.rsplit(")", 1)[1].split()[19])
                break
            except OSError:
                time.sleep(0.01)
        self.assertIsNotNone(remote_start)
        proof = self._prime(row)
        marker = self._mark_title_pending()
        env = self.fixture.env()
        env.update(
            {
                "STUB_CODEX_BOUNCE_TITLE": "Release Notes",
                "STUB_REFRESH_PID": str(remote_tui.pid),
                "STUB_REFRESH_START": str(remote_start),
            }
        )
        try:
            refreshed = run([SP, "picker-title-refresh", proof], env=env)
            remote_tui.wait(timeout=2)
            self.assertEqual(0, refreshed.returncode)
            self.assertIsNone(app_server.poll())
            self.assertFalse(marker.exists())
        finally:
            for process in (remote_tui, app_server):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2)

    def test_explicit_pending_title_refresh_refuses_working_provider(self) -> None:
        row, process = self._live_idle_codex("Attached")
        row["agent_status"] = "working"
        proof = self._prime(row)
        marker = self._mark_title_pending()
        env = self.fixture.env()
        env["STUB_CODEX_BOUNCE_TITLE"] = "Release Notes"
        try:
            refused = run(
                [SP, "picker-title-refresh", proof], env=env, check=False
            )
            self.assertEqual(74, refused.returncode)
            self.assertIsNone(process.poll())
            self.assertTrue(marker.exists())
            self.assertIn("not proven idle", refused.stderr)
        finally:
            process.terminate()
            process.wait(timeout=2)

    def test_picker_open_maps_attach_failure_without_calling_session_dead(self) -> None:
        proof = self._prime(session_row("main2"))
        env = self.fixture.env()
        env["FAKE_ATTACH_FAIL"] = "1"
        refused = run([SP, "picker-open", proof], env=env, check=False)
        self.assertEqual(75, refused.returncode)
        self.assertIn("could not connect", refused.stderr)
        self.assertIn("nothing was called dead", refused.stderr)
        self.assertNotIn("terminal died", refused.stderr)
        events = [
            json.loads(line)
            for line in (self.fixture.state / "action-events.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(
            ["requested", "attach_failed"],
            [event["outcome"] for event in events],
        )

    def test_other_client_winning_open_race_is_a_busy_return_not_a_kill(self) -> None:
        proof = self._prime(session_row("main2"))
        env = self.fixture.env()
        env["FAKE_ATTACH_BECOMES_BUSY"] = "1"
        opened = run([SP, "picker-open", proof], env=env)
        self.assertEqual(0, opened.returncode)
        self.assertEqual("busy main2\n", self.fixture.shpool_log.read_text())
        state = json.loads(self.fixture.shpool_state.read_text())
        self.assertEqual("Attached", state["sessions"][0]["status"])
        self.assertNotIn("kill", self.fixture.shpool_log.read_text())

    def test_takeover_uses_force_against_one_busy_session(self) -> None:
        proof = self._prime(session_row("main2", status="Attached"))
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_CONFIRM_ID": "main2",
                "FAKE_BUSY_IF_ATTACHED": "1",
            }
        )
        moved = run([SP, "picker-takeover", proof], env=env)
        self.assertEqual(0, moved.returncode)
        self.assertEqual(
            "force-attach main2\n", self.fixture.shpool_log.read_text()
        )
        state = json.loads(self.fixture.shpool_state.read_text())
        self.assertEqual(1, len(state["sessions"]))
        self.assertEqual("Attached", state["sessions"][0]["status"])

    def test_picker_recovery_refuses_same_generation_now_attached(self) -> None:
        row = session_row("main2")
        row["cwd"] = str(self.fixture.project)
        proof = self._prime(row)
        attached = dict(row)
        attached["shpool_status"] = "Attached"
        attached["availability"] = "attached"
        changed = self.fixture.base / "attached.json"
        changed.write_text(
            json.dumps(inventory_document(attached)), encoding="utf-8"
        )
        env = self.fixture.env()
        env["STUB_SECOND_INVENTORY"] = str(changed)
        refused = run([SP, "picker-recover", proof], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("open in another window", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_repair_refuses_same_generation_now_attached(self) -> None:
        row = session_row("wedged")
        row["cwd"] = str(self.fixture.project)
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(row)), encoding="utf-8"
        )
        attached = dict(row)
        attached["shpool_status"] = "Attached"
        attached["availability"] = "attached"
        changed = self.fixture.base / "repair-attached.json"
        changed.write_text(
            json.dumps(inventory_document(attached)), encoding="utf-8"
        )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "wedged",
                            "status": "Attached",
                            "started_at_unix_ms": row["started_at_unix_ms"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        env = self.fixture.env()
        env["STUB_SECOND_INVENTORY"] = str(changed)
        refused = run([SP, "repair", "wedged"], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("open in another window", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_action_log_is_private_minimal_capped_and_seven_day_bounded(
        self,
    ) -> None:
        now_ms = time.time_ns() // 1_000_000
        old_ms = now_ms - 8 * 24 * 60 * 60 * 1000
        log = self.fixture.state / "action-events.jsonl"
        records = [
            {
                "action": "picker_open",
                "at_unix_ms": old_ms,
                "outcome": "returned",
                "schema_version": 1,
            },
            {
                "action": "/private/project/path",
                "at_unix_ms": now_ms,
                "outcome": "00000000-0000-4000-8000-000000000001",
                "schema_version": 1,
            }
        ]
        records.extend(
            {
                "action": "picker_open",
                "at_unix_ms": now_ms,
                "outcome": "returned",
                "schema_version": 1,
            }
            for _ in range(1_005)
        )
        log.write_text(
            "".join(
                json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
                for item in records
            ),
            encoding="utf-8",
        )
        log.chmod(0o600)
        run(
            [
                "bash",
                "-c",
                'source "$1"; sk_prepare_state; sk_log_action picker_close closed',
                "action-log-test",
                COMMON,
            ],
            env=self.fixture.env(),
        )
        kept = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertLessEqual(len(kept), 1_000)
        self.assertTrue(all(item["at_unix_ms"] >= now_ms for item in kept))
        self.assertEqual(0o600, stat.S_IMODE(log.stat().st_mode))
        self.assertLessEqual(log.stat().st_size, 256 * 1024)
        self.assertEqual(
            {
                "action",
                "at_unix_ms",
                "outcome",
                "schema_version",
            },
            set().union(*(item.keys() for item in kept)),
        )
        serialized = log.read_text()
        self.assertNotIn("/private/project/path", serialized)
        self.assertNotIn(
            "00000000-0000-4000-8000-000000000001", serialized
        )
        for forbidden in (
            "title",
            "uuid",
            "shpool_id",
            "path",
            "prompt",
            "output",
            "ip",
        ):
            self.assertNotIn(f'"{forbidden}"', serialized.casefold())

    def test_unknown_uuidless_picker_open_and_history_use_exact_guard_proof(self) -> None:
        row = unknown_session_row("main9")
        proof = self._prime(row)
        self.fixture.journals.mkdir()
        (self.fixture.journals / "main9.raw").write_text(
            "startup prompt output\n", encoding="utf-8"
        )

        history = run([SP, "picker-history", proof], env=self.fixture.env())
        self.assertEqual("startup prompt output\n", history.stdout)

        opened = run([SP, "picker-open", proof], env=self.fixture.env())
        self.assertEqual(0, opened.returncode)
        self.assertEqual("attach main9\n", self.fixture.shpool_log.read_text())

        refused_name = run(
            [SP, "picker-name", proof, "Not an AI identity"],
            env=self.fixture.env(),
            check=False,
        )
        self.assertNotEqual(0, refused_name.returncode)
        self.assertIn("derived and cannot be renamed", refused_name.stderr)

    def test_unknown_uuidless_picker_takeover_and_close_are_proof_bound(self) -> None:
        row = unknown_session_row("main9")
        row["shpool_status"] = "Attached"
        row["availability"] = "attached"
        proof = self._prime(row)
        env = self.fixture.env()
        env["SESSION_KIT_CONFIRM_ID"] = "main9"

        moved = run([SP, "picker-takeover", proof], env=env)
        self.assertEqual(0, moved.returncode)
        self.assertEqual("attach main9\n", self.fixture.shpool_log.read_text())

        self.fixture.shpool_log.unlink()
        closed = run([SP, "picker-close", proof], env=env)
        self.assertIn("Closed exact session main9", closed.stdout)
        self.assertEqual("kill main9\n", self.fixture.shpool_log.read_text())

    def test_unknown_provider_resolution_between_proofs_refuses_close(self) -> None:
        row = unknown_session_row("main9")
        row["shpool_status"] = "Attached"
        row["availability"] = "attached"
        proof = self._prime(row)
        resolved = session_row(
            "main9",
            row=9,
            status="Attached",
            provider="codex",
            uuid="00000000-0000-4000-8000-000000000999",
        )
        resolved["shpool_shell"] = dict(row["shpool_shell"])
        changed = self.fixture.base / "resolved.json"
        changed.write_text(
            json.dumps(inventory_document(resolved)), encoding="utf-8"
        )
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_CONFIRM_ID": "main9",
                "STUB_SECOND_INVENTORY": str(changed),
            }
        )
        refused = run([SP, "picker-close", proof], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("nothing closed", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_picker_close_holds_lock_through_exact_name_kill(self) -> None:
        proof = self._prime(session_row("main2", status="Attached"))
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_CONFIRM_ID": "main2",
                "FAKE_EXPECT_CREATE_LOCK": "locked",
            }
        )
        closed = run([SP, "picker-close", proof], env=env)
        self.assertIn("Closed exact session main2", closed.stdout)
        self.assertEqual("kill main2\n", self.fixture.shpool_log.read_text())

    def test_picker_close_without_exact_confirmation_never_kills(self) -> None:
        proof = self._prime(session_row("main2", status="Attached"))
        refused = run(
            [SP, "picker-close", proof],
            env=self.fixture.env(),
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("Close cancelled", refused.stdout)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_proof_file_and_schema_fail_closed(self) -> None:
        valid = self._prime(session_row("main2"))
        original = valid.read_bytes()
        variants: list[tuple[str, Path]] = []

        mode = self.fixture.base / "mode.json"
        mode.write_bytes(original)
        mode.chmod(0o644)
        variants.append(("mode", mode))

        symlink = self.fixture.base / "symlink.json"
        symlink.symlink_to(valid)
        variants.append(("symlink", symlink))

        hardlink = self.fixture.base / "hardlink.json"
        os.link(valid, hardlink)
        variants.append(("hardlink", hardlink))

        noncanonical = self.fixture.base / "noncanonical.json"
        noncanonical.write_text(
            json.dumps(json.loads(original), indent=2) + "\n", encoding="utf-8"
        )
        noncanonical.chmod(0o600)
        variants.append(("noncanonical", noncanonical))

        extra = self.fixture.base / "extra.json"
        extra_value = json.loads(original)
        extra_value["unexpected"] = True
        extra.write_text(
            json.dumps(extra_value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        extra.chmod(0o600)
        variants.append(("schema", extra))

        for label, proof in variants:
            with self.subTest(label=label):
                refused = run(
                    [SP, "picker-open", proof],
                    env=self.fixture.env(),
                    check=False,
                )
                self.assertNotEqual(0, refused.returncode)
                self.assertIn("invalid private picker proof", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_changed_fingerprint_before_or_after_confirmation_never_kills(self) -> None:
        row = session_row("main2", status="Attached")
        proof = self._prime(row)
        changed = session_row(
            "main2",
            status="Attached",
            uuid="00000000-0000-4000-8000-000000000999",
        )
        changed_file = self.fixture.base / "changed.json"
        changed_file.write_text(
            json.dumps(inventory_document(changed)), encoding="utf-8"
        )
        env = self.fixture.env()
        env["SESSION_KIT_CONFIRM_ID"] = "main2"

        self.fixture.inventory.write_text(
            json.dumps(inventory_document(changed)), encoding="utf-8"
        )
        before = run([SP, "picker-close", proof], env=env, check=False)
        self.assertNotEqual(0, before.returncode)
        self.assertIn("displayed session changed", before.stderr)

        self.fixture.inventory.write_text(
            json.dumps(inventory_document(row)), encoding="utf-8"
        )
        if self.fixture.snapshot_count.exists():
            self.fixture.snapshot_count.unlink()
        env["STUB_SECOND_INVENTORY"] = str(changed_file)
        after = run([SP, "picker-close", proof], env=env, check=False)
        self.assertNotEqual(0, after.returncode)
        self.assertIn("nothing closed", after.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_close_waits_for_creation_lock_before_final_proof_and_kill(self) -> None:
        proof = self._prime(session_row("main2", status="Attached"))
        env = {**os.environ, **self.fixture.env()}
        env["SESSION_KIT_CONFIRM_ID"] = "main2"
        lock_path = self.fixture.state / "create.lock"
        with lock_path.open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            proc = subprocess.Popen(
                [SP, "picker-close", proof],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.1)
            self.assertIsNone(proc.poll())
            self.assertFalse(self.fixture.shpool_log.exists())
            fcntl.flock(held, fcntl.LOCK_UN)
        stdout, stderr = proc.communicate(timeout=5)
        self.assertEqual(0, proc.returncode, (stdout, stderr))
        self.assertEqual("kill main2\n", self.fixture.shpool_log.read_text())

    def test_picker_history_requires_current_proof_and_preserves_journal(self) -> None:
        row = session_row("main2")
        proof = self._prime(row)
        self.fixture.journals.mkdir()
        journal = self.fixture.journals / "main2.raw"
        journal.write_text("retained explicit history\n", encoding="utf-8")
        shown = run([SP, "picker-history", proof], env=self.fixture.env())
        self.assertEqual("retained explicit history\n", shown.stdout)
        self.assertEqual(
            b"retained explicit history\n",
            journal.read_bytes(),
        )
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_picker_name_uses_shared_alias_api_and_reset_preserves_config(self) -> None:
        old_uuid = "11111111-1111-4111-8111-111111111111"
        self.fixture.config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "command_timeout_seconds": 7,
                    "aliases": {f"claude:{old_uuid}": "Existing"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        row = session_row("main2", provider="codex")
        proof = self._prime(row)
        alias_log = self.fixture.base / "alias-api.log"
        env = self.fixture.env()
        env["STUB_ALIAS_LOG"] = str(alias_log)
        named = run(
            [SP, "picker-name", proof, "  New\u001b title  "],
            env=env,
        )
        self.assertIn("Named exact codex session main2", named.stdout)
        self.assertNotIn('"aliases"', named.stdout)
        stored = json.loads(self.fixture.config.read_text())
        self.assertEqual(1, stored["schema_version"])
        self.assertEqual(7, stored["command_timeout_seconds"])
        self.assertEqual("Existing", stored["aliases"][f"claude:{old_uuid}"])
        self.assertEqual(
            "New title",
            stored["aliases"][f"codex:{row['identity']['uuid']}"],
        )
        self.assertEqual(0o600, stat.S_IMODE(self.fixture.config.stat().st_mode))
        self.assertEqual(
            [
                "alias",
                "set",
                "codex",
                row["identity"]["uuid"],
                "--",
                "  New\u001b title  ",
            ],
            json.loads(alias_log.read_text().splitlines()[0]),
        )

        reset = run([SP, "picker-name-reset", proof], env=env)
        self.assertIn("Reset local name for exact codex session main2", reset.stdout)
        self.assertNotIn('"aliases"', reset.stdout)
        stored = json.loads(self.fixture.config.read_text())
        self.assertNotIn(f"codex:{row['identity']['uuid']}", stored["aliases"])
        self.assertEqual("Existing", stored["aliases"][f"claude:{old_uuid}"])
        self.assertEqual(7, stored["command_timeout_seconds"])
        self.assertEqual(
            ["alias", "delete", "codex", row["identity"]["uuid"]],
            json.loads(alias_log.read_text().splitlines()[1]),
        )

        shell = session_row("main5", provider="shell", uuid="")
        shell_proof = write_picker_proof(
            self.fixture.base / "shell-proof.json", shell
        )
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(shell)), encoding="utf-8"
        )
        before = self.fixture.config.read_bytes()
        refused = run(
            [SP, "picker-name", shell_proof, "Shell alias"],
            env=self.fixture.env(),
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("derived and cannot be renamed", refused.stderr)
        self.assertEqual(before, self.fixture.config.read_bytes())

    def test_provider_neutral_self_name_forwards_one_exact_title_argument(
        self,
    ) -> None:
        result = run(
            [SP, "self-name", "Session Kit Updates"],
            env=self.fixture.env(),
        )
        payload = json.loads(result.stdout)
        self.assertEqual("ready", payload["automatic_name_state"])
        self.assertEqual("Session Kit Updates", payload["title"])
        refused = run(
            [SP, "self-name", "Too", "Many"],
            env=self.fixture.env(),
            check=False,
        )
        self.assertEqual(2, refused.returncode)

    def test_public_name_and_reset_revalidate_exact_identity(self) -> None:
        row = session_row("main2", provider="claude")
        self._prime(row)
        env = self.fixture.env()
        named = run([SP, "name", "main2", "Lyrics audit"], env=env)
        self.assertIn("Named exact claude session main2", named.stdout)
        stored = json.loads(self.fixture.config.read_text())
        self.assertEqual(
            "Lyrics audit", stored["aliases"][f"claude:{row['identity']['uuid']}"]
        )

        reset = run([SP, "name", "reset", "main2"], env=env)
        self.assertIn("Reset local name for exact claude session main2", reset.stdout)
        self.assertNotIn(
            f"claude:{row['identity']['uuid']}",
            json.loads(self.fixture.config.read_text())["aliases"],
        )

        changed = session_row(
            "main2",
            provider="claude",
            uuid="00000000-0000-4000-8000-000000000999",
        )
        changed_file = self.fixture.base / "changed-name.json"
        changed_file.write_text(
            json.dumps(inventory_document(changed)), encoding="utf-8"
        )
        if self.fixture.snapshot_count.exists():
            self.fixture.snapshot_count.unlink()
        env["STUB_SECOND_INVENTORY"] = str(changed_file)
        refused = run([SP, "name", "main2", "Wrong target"], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("target changed", refused.stderr)

    def test_public_name_uses_generation_bound_exited_provider_identity(self) -> None:
        historical_uuid = "00000000-0000-4000-8000-000000000111"
        row = session_row("main2", provider="shell", uuid="")
        row.update(
            {
                "agent_status": "provider exited",
                "display_provider": "claude",
                "exited_provider": "claude",
                "exited_identity": {
                    "confidence": "historical-exact",
                    "uuid": historical_uuid,
                },
                "recovery": {
                    "available": True,
                    "provider": "claude",
                    "uuid": historical_uuid,
                },
            }
        )
        self._prime(row)
        env = self.fixture.env()
        named = run([SP, "name", "main2", "Exited Claude"], env=env)
        self.assertIn("Named exact claude session main2", named.stdout)
        stored = json.loads(self.fixture.config.read_text())
        self.assertEqual(
            "Exited Claude", stored["aliases"][f"claude:{historical_uuid}"]
        )

        changed_uuid = "00000000-0000-4000-8000-000000000222"
        changed = json.loads(json.dumps(row))
        changed["exited_identity"]["uuid"] = changed_uuid
        changed["recovery"]["uuid"] = changed_uuid
        changed_file = self.fixture.base / "changed-exited-name.json"
        changed_file.write_text(
            json.dumps(inventory_document(changed)), encoding="utf-8"
        )
        if self.fixture.snapshot_count.exists():
            self.fixture.snapshot_count.unlink()
        env["STUB_SECOND_INVENTORY"] = str(changed_file)
        before = self.fixture.config.read_bytes()
        refused = run(
            [SP, "name", "main2", "Wrong exited target"],
            env=env,
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("target changed", refused.stderr)
        self.assertEqual(before, self.fixture.config.read_bytes())

    def test_public_numeric_selector_uses_terminal_number_not_internal_row(self) -> None:
        row = session_row("main2", row=1, provider="claude")
        row["terminal_number"] = 27
        self._prime(row)
        env = self.fixture.env()
        named = run([SP, "name", "27", "Stable selector"], env=env)
        self.assertIn("Named exact claude session main2", named.stdout)
        stored = json.loads(self.fixture.config.read_text())
        self.assertEqual(
            "Stable selector",
            stored["aliases"][f"claude:{row['identity']['uuid']}"],
        )

        refused = run(
            [SP, "name", "reset", "1"],
            env=env,
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("no unique session matches that selection", refused.stderr)

    def test_picker_fork_proves_a_distinct_uuid_and_keeps_source_active(self) -> None:
        source_uuid = "00000000-0000-4000-8000-000000000001"
        fork_uuid = "00000000-0000-4000-8000-000000000002"
        row = session_row("main2", provider="claude", uuid=source_uuid)
        row["cwd"] = str(self.fixture.project)
        proof = self._prime(row)
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_AFTER_SESSIONS": "1",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_DYNAMIC_UUID": fork_uuid,
                "STUB_DYNAMIC_UUID_OVERRIDES": json.dumps({"main2": source_uuid}),
            }
        )
        forked = run([SP, "picker-fork", proof], env=env)
        self.assertIn(f"as {fork_uuid}", forked.stdout)
        state = json.loads(self.fixture.shpool_state.read_text())
        self.assertEqual(2, len(state["sessions"]))
        self.assertTrue(any(item["name"] == "main2" for item in state["sessions"]))
        generated = [item["name"] for item in state["sessions"] if item["name"] != "main2"]
        self.assertEqual(1, len(generated))
        self.assertRegex(generated[0], r"^s[0-9]{8}-[0-9]{6}-[0-9]+")
        self.assertEqual(f"attach {generated[0]}\n", self.fixture.shpool_log.read_text())
        self.assertFalse(any(self.fixture.start.glob(f"{generated[0]}*")))

    def test_queued_picker_fork_rechecks_release_marker_under_creation_lock(self) -> None:
        source_uuid = "00000000-0000-4000-8000-000000000001"
        fork_uuid = "00000000-0000-4000-8000-000000000002"
        row = session_row("main2", provider="claude", uuid=source_uuid)
        row["cwd"] = str(self.fixture.project)
        proof = self._prime(row)
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_AFTER_SESSIONS": "1",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_DYNAMIC_UUID": fork_uuid,
                "STUB_DYNAMIC_UUID_OVERRIDES": json.dumps(
                    {"main2": source_uuid}
                ),
            }
        )
        proc, stdout, stderr = run_queued_creator_after_marker_switch(
            self.fixture,
            [SP, "picker-fork", proof],
            env,
        )
        self.assertNotEqual(0, proc.returncode, (stdout, stderr))
        self.assertIn("validated for this release", stderr)
        self.assertEqual(
            ["main2"],
            [
                item["name"]
                for item in json.loads(self.fixture.shpool_state.read_text())[
                    "sessions"
                ]
            ],
        )
        self.assertFalse(self.fixture.shpool_log.exists())
        self.assertFalse(self.fixture.start.exists())

    def test_picker_fork_allows_same_uuid_active_under_other_provider(self) -> None:
        source_uuid = "00000000-0000-4000-8000-000000000001"
        fork_uuid = "00000000-0000-4000-8000-000000000002"
        row = session_row("main2", provider="claude", uuid=source_uuid)
        row["cwd"] = str(self.fixture.project)
        proof = self._prime(row)
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_AFTER_SESSIONS": "1",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_DYNAMIC_UUID": fork_uuid,
                "STUB_DYNAMIC_UUID_OVERRIDES": json.dumps(
                    {"main2": source_uuid}
                ),
                "STUB_DYNAMIC_OUTSIDE_AGENTS": json.dumps(
                    [
                        {
                            "provider": "codex",
                            "identity": {
                                "uuid": fork_uuid,
                                "confidence": "exact",
                            },
                        }
                    ]
                ),
            }
        )
        forked = run([SP, "picker-fork", proof], env=env)
        self.assertIn(f"as {fork_uuid}", forked.stdout)
        state = json.loads(self.fixture.shpool_state.read_text())
        self.assertEqual(2, len(state["sessions"]))

    def test_picker_fork_refuses_same_provider_duplicate_fork_uuid(self) -> None:
        source_uuid = "00000000-0000-4000-8000-000000000001"
        fork_uuid = "00000000-0000-4000-8000-000000000002"
        row = session_row("main2", provider="claude", uuid=source_uuid)
        row["cwd"] = str(self.fixture.project)
        proof = self._prime(row)
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_AFTER_SESSIONS": "1",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_DYNAMIC_UUID": fork_uuid,
                "STUB_DYNAMIC_UUID_OVERRIDES": json.dumps(
                    {"main2": source_uuid}
                ),
                "STUB_DYNAMIC_OUTSIDE_AGENTS": json.dumps(
                    [
                        {
                            "provider": "claude",
                            "identity": {
                                "uuid": fork_uuid,
                                "confidence": "exact",
                            },
                        }
                    ]
                ),
            }
        )
        refused = run([SP, "picker-fork", proof], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("distinct exact claude fork was not proven", refused.stderr)
        state = json.loads(self.fixture.shpool_state.read_text())
        self.assertEqual(2, len(state["sessions"]))
        generated = [
            item["name"] for item in state["sessions"] if item["name"] != "main2"
        ]
        self.assertEqual(1, len(generated))
        self.assertTrue((self.fixture.start / generated[0]).is_file())
        self.assertTrue(
            (self.fixture.start / f"{generated[0]}.expected").is_file()
        )

    def test_picker_fork_refuses_stale_source_proof_before_launch(self) -> None:
        source_uuid = "00000000-0000-4000-8000-000000000001"
        row = session_row("main2", provider="codex", uuid=source_uuid)
        row["cwd"] = str(self.fixture.project)
        proof = self._prime(row)
        changed = session_row(
            "main2",
            provider="codex",
            uuid="00000000-0000-4000-8000-000000000999",
        )
        changed["cwd"] = str(self.fixture.project)
        changed_file = self.fixture.base / "changed-fork.json"
        changed_file.write_text(
            json.dumps(inventory_document(changed)), encoding="utf-8"
        )
        env = self.fixture.env()
        env["STUB_SECOND_INVENTORY"] = str(changed_file)
        refused = run([SP, "picker-fork", proof], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("changed before fork", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())
        self.assertEqual(
            ["main2"],
            [
                item["name"]
                for item in json.loads(self.fixture.shpool_state.read_text())["sessions"]
            ],
        )

    def test_picker_fork_uuid_collision_retains_source_and_new_session(self) -> None:
        source_uuid = "00000000-0000-4000-8000-000000000001"
        row = session_row("main2", provider="codex", uuid=source_uuid)
        row["cwd"] = str(self.fixture.project)
        proof = self._prime(row)
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "codex",
                "STUB_DYNAMIC_AFTER_SESSIONS": "1",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_DYNAMIC_UUID": source_uuid,
                "STUB_DYNAMIC_UUID_OVERRIDES": json.dumps({"main2": source_uuid}),
            }
        )
        refused = run([SP, "picker-fork", proof], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("distinct exact codex fork was not proven", refused.stderr)
        state = json.loads(self.fixture.shpool_state.read_text())
        self.assertEqual(2, len(state["sessions"]))
        generated = [item["name"] for item in state["sessions"] if item["name"] != "main2"]
        self.assertEqual(1, len(generated))
        record = self.fixture.start / generated[0]
        sidecar = self.fixture.start / f"{generated[0]}.expected"
        self.assertTrue(record.is_file())
        self.assertTrue(sidecar.is_file())
        self.assertEqual(
            f"codex\t{self.fixture.project}\t{source_uuid}\tfork\n",
            record.read_text(),
        )
        self.assertTrue(sidecar.read_text().endswith(f"\t{source_uuid}\tfork\n"))
        self.assertFalse(
            any(
                line.startswith("kill ")
                for line in self.fixture.shpool_log.read_text().splitlines()
            )
        )

    def test_picker_open_arms_bracketed_paste_on_a_real_terminal(self) -> None:
        """The attach handover re-arms bracketed paste before shpool runs.

        shpool's restore replays screen contents only, never input modes, so
        a reopened window lost bracketed paste: terminal paste protection
        prompted on every multi-line paste and each newline submitted the
        composer early. The picker path must emit the mode when stdout is a
        terminal; the piped runs elsewhere in this file prove it stays silent
        without one.
        """
        import pty
        import select
        import time

        proof = self._prime(session_row("main2"))
        env = os.environ.copy()
        env.update(self.fixture.env())
        env["FAKE_EXPECT_CREATE_LOCK"] = "unlocked"
        pid, descriptor = pty.fork()
        if pid == 0:
            os.execvpe(str(SP), [str(SP), "picker-open", str(proof)], env)
        output = bytearray()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = select.select([descriptor], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                while True:
                    ready, _, _ = select.select([descriptor], [], [], 0)
                    if not ready:
                        break
                    try:
                        chunk = os.read(descriptor, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                break
        os.close(descriptor)
        self.assertIn(b"\x1b[?2004h", bytes(output))
        self.assertEqual("attach main2\n", self.fixture.shpool_log.read_text())


class JournalHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CommandFixture()
        self.fixture.journals.mkdir()
        row = session_row("history")
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(row)), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.fixture.close()

    def test_zero_two_twenty_and_150mb_journals_remain_byte_exact(self) -> None:
        for megabytes in (0, 2, 20, 150):
            with self.subTest(megabytes=megabytes):
                journal = self.fixture.journals / "history.raw"
                with journal.open("wb") as handle:
                    handle.truncate(megabytes * 1024 * 1024)
                expected = hashlib.sha256()
                remaining = megabytes * 1024 * 1024
                zeroes = b"\0" * (1024 * 1024)
                while remaining:
                    block = zeroes[: min(remaining, len(zeroes))]
                    expected.update(block)
                    remaining -= len(block)
                actual = self._history_hash()
                self.assertEqual(expected.hexdigest(), actual)

    def test_control_sequences_and_multibyte_history_remain_byte_exact(self) -> None:
        payload = (
            b"\x00\x01\x1b[2J\x1b[?1049h"
            + "Lyrics: café, 東京, ☃\n".encode("utf-8")
            + bytes(range(32))
        )
        journal = self.fixture.journals / "history.raw"
        journal.write_bytes(payload)
        expected = hashlib.sha256(payload).hexdigest()
        self.assertEqual(expected, self._history_hash())

    def test_segment_order_and_recovered_journal_precedence_are_preserved(self) -> None:
        segmented = self.fixture.journals / "history"
        segmented.mkdir()
        (segmented / "segment-000002.raw").write_bytes(b"second\n")
        (segmented / "segment-000001.raw").write_bytes(b"first\n")
        shown = run([SP, "history", "history"], env=self.fixture.env())
        self.assertEqual("first\nsecond\n", shown.stdout)

        self.fixture.recovery.mkdir()
        recovered = self.fixture.recovery / "recovered.raw"
        recovered.write_bytes(b"recovered\n")
        (self.fixture.recovery / "current-map.tsv").write_text(
            f"history\t{recovered}\n", encoding="utf-8"
        )
        preferred = run([SP, "history", "history"], env=self.fixture.env())
        self.assertEqual("recovered\n", preferred.stdout)
        self.assertEqual(b"first\n", (segmented / "segment-000001.raw").read_bytes())
        self.assertEqual(b"second\n", (segmented / "segment-000002.raw").read_bytes())
        self.assertEqual(b"recovered\n", recovered.read_bytes())

    def _history_hash(self) -> str:
        proc = run(
            [
                "bash",
                "-c",
                '"$1" history history | sha256sum',
                "history-test",
                SP,
            ],
            env=self.fixture.env(),
        )
        return proc.stdout.split()[0]


class ConfirmExactDrainTests(unittest.TestCase):
    def test_confirm_is_promptless_and_consumes_no_input(self) -> None:
        """Interactive confirms are gone (Dan 2026-08-02): the action header
        is the safety display, and nothing typed afterwards may be eaten —
        the very next read must see the human's own input untouched.
        """
        import pty
        import select
        import time

        script = (
            "source bin/session_kit_common\n"
            'sk_confirm_exact "Close" "id1" "Title" "codex" && echo CONFIRMED\n'
            "IFS= read -r next\n"
            'echo "NEXT=[$next]"\n'
        )
        pid, descriptor = pty.fork()
        if pid == 0:
            os.chdir(REPO)
            os.execvp("bash", ["bash", "-c", script])
        os.write(descriptor, b"PROBE\n")
        output = bytearray()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = select.select([descriptor], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                while True:
                    ready, _, _ = select.select([descriptor], [], [], 0)
                    if not ready:
                        break
                    try:
                        chunk = os.read(descriptor, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                break
        os.close(descriptor)
        text = output.decode("utf-8", "replace")
        self.assertIn("CONFIRMED", text)
        self.assertIn("NEXT=[PROBE]", text)


if __name__ == "__main__":
    unittest.main()
