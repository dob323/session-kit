from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import REPO, run

sys.path.insert(0, str(REPO / "lib"))
from sessionkit_inventory import lifecycle  # noqa: E402


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
if [[ ${1:-} == -x && ${*: -1} == 9 ]]; then
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
        # The real core refuses a state directory that is not owner-private,
        # and the recovery list is served by it, so the fixture has to hold
        # the same contract a real install does.
        self.state.mkdir(mode=0o700)
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
        home_bin = self.home / ".local" / "bin"
        home_bin.mkdir(parents=True, mode=0o700)
        write_executable(
            home_bin / "shpool",
            "#!/usr/bin/env bash\nexec \"$SESSION_KIT_SHPOOL_CMD\" \"$@\"\n",
        )
        self.close_intent_log = self.base / "close-intent.log"
        self.worktree_log = self.base / "worktree.log"
        self.origin_log = self.base / "origin.log"
        self.origin_instance_log = self.base / "origin-instance.log"
        self.closed_log = self.base / "closed-sessions.log"
        self.name_push_log = self.base / "name-push.log"
        write_executable(
            self.fake_shpool,
            """#!/usr/bin/env python3
import fcntl, json, os, pathlib, shutil, sys, tempfile, time
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
        collision_marker=os.environ.get("FAKE_CREATE_COLLISION_ONCE")
        if collision_marker and not pathlib.Path(collision_marker).exists():
            pathlib.Path(collision_marker).write_text(name)
            if not any(row.get("name") == name for row in data["sessions"]):
                data["sessions"].append({
                    "name":name,
                    "status":"Disconnected",
                    "started_at_unix_ms":int(time.time()*1000)-1000,
                })
            fd,tmp=tempfile.mkstemp(prefix=".state.",dir=state.parent)
            with os.fdopen(fd,"w") as out: json.dump(data,out)
            os.replace(tmp,state)
            with log.open("a") as out: out.write("collision "+name+"\\n")
            raise SystemExit(17)
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
        immediate_command=(
            args[args.index("--cmd")+1] if "--cmd" in args else ""
        )
        exits_immediately=immediate_command in {"/bin/false", "/bin/true"}
        if immediate_command == "/bin/true":
            data["sessions"]=[
                row for row in data["sessions"] if row.get("name") != name
            ]
            action="attach-exit"
        if not exits_immediately and not any(row.get("name") == name for row in data["sessions"]):
            data["sessions"].append({"name":name,"status":"Disconnected","started_at_unix_ms":int(time.time()*1000)})
    elif len(args) == 2 and args[0] == "kill":
        required_signal=os.environ.get("FAKE_REQUIRE_EXACT_SIGNAL")
        if required_signal and not pathlib.Path(required_signal).is_file():
            raise SystemExit(89)
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
        proc_pid=os.environ.get("FAKE_KILL_PROC_PID","")
        proc_root=os.environ.get("SESSION_KIT_PROC_ROOT","")
        if proc_pid.isdigit() and proc_root:
            shutil.rmtree(pathlib.Path(proc_root)/proc_pid, ignore_errors=True)
        inventory_path=os.environ.get("FAKE_DROP_INVENTORY_ROW")
        if inventory_path:
            inventory_file=pathlib.Path(inventory_path)
            inventory=json.loads(inventory_file.read_text())
            inventory["sessions"]=[
                row for row in inventory.get("sessions",[])
                if row.get("shpool_id_raw") != name and row.get("shpool_id") != name
            ]
            inventory_file.write_text(json.dumps(inventory))
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
if args[:2] in (["account", "launch-profile"], ["account", "resume-profile"]) and len(args) == 4:
    # This branch came from the profile-enrolment fixture while the account
    # switch branch added the ordered call log below. A merged fixture must do
    # both: returning here before logging made the real launch-profile step
    # disappear from five ordering tests even though production ran it.
    log=os.environ.get("STUB_ACCOUNT_LOG")
    if log:
        with open(log, "a") as handle:
            handle.write(json.dumps(args)+"\\n")
    if args[1] == "launch-profile" and os.environ.get("STUB_ACCOUNT_LAUNCH_FAILS") == "1":
        sys.stderr.write("account "+args[3]+" is not selectable: blocked\\n")
        raise SystemExit(1)
    # Enrollment is stubbed by naming a directory. With none named the
    # account is simply not enrolled, which is what the real core reports.
    profile=os.environ.get("STUB_ACCOUNT_PROFILE")
    if not profile:
        sys.stderr.write("no such enrolled account\\n")
        raise SystemExit(1)
    marker=os.environ.get("STUB_ACCOUNT_LAUNCH_MARKER")
    if args[1] == "launch-profile" and marker:
        pathlib.Path(marker).write_text(json.dumps({"provider":args[2],"alias":args[3]}))
    print(json.dumps({"provider": args[2], "alias": args[3], "profile_dir": profile}))
    raise SystemExit(0)
if args[:2] == ["account", "bind"]:
    print(json.dumps({"bound": True}))
    raise SystemExit(0)
if args[:2] == ["platform", "terminate-exact-process"] and not os.environ.get(
    "SESSION_KIT_PROC_ROOT"
):
    if os.environ.get("STUB_EXACT_ALREADY_GONE") == "1":
        raise SystemExit(1)
    log=os.environ.get("SESSION_KIT_TEST_EXACT_SIGNAL_LOG")
    if log:
        with open(log,"a",encoding="utf-8") as handle:
            handle.write(f"{args[2]}\\t{args[3]}\\t15\\n")
    drop_name=os.environ.get("FAKE_DROP_EXACT_SESSION")
    inventory_path=os.environ.get("FAKE_DROP_INVENTORY_ROW")
    if drop_name and inventory_path:
        inventory_file=pathlib.Path(inventory_path)
        inventory=json.loads(inventory_file.read_text())
        inventory["sessions"]=[
            row for row in inventory.get("sessions",[])
            if row.get("shpool_id_raw") != drop_name and row.get("shpool_id") != drop_name
        ]
        inventory_file.write_text(json.dumps(inventory))
    print("terminated")
    raise SystemExit(0)
if args[:2] == ["platform", "exact-shell-gone"] and not os.environ.get(
    "SESSION_KIT_PROC_ROOT"
):
    print("gone")
    raise SystemExit(0)
if args[:2] == ["platform", "shpool-holder-generation"] and not os.environ.get(
    "SESSION_KIT_PROC_ROOT"
):
    inventory=json.loads(pathlib.Path(os.environ["STUB_INVENTORY"]).read_text())
    daemon=inventory["daemon_generation"]
    print(f"{daemon['pid']}\t{daemon['process_start_ticks']}")
    raise SystemExit(0)
if args[:2] in (
    ["platform", "shpool-holder-generation"],
    ["platform", "exact-shell-gone"],
    ["platform", "terminate-exact-process"],
):
    os.execvp("python3", ["python3", os.environ["STUB_REAL_CORE"], *args])
if args[:2] == ["platform", "process-table"] and len(args) == 2:
    table=os.environ.get("STUB_PROCESS_TABLE")
    if not table:
        raise SystemExit(2)
    print(table)
    raise SystemExit(0)
if args[:2] == ["worktree", "lookup"]:
    # Answered by the real core against the fixture's own registry: a restore
    # that has to find the repository a released copy came from is exactly the
    # path a stub would hide.
    os.execv(sys.executable, [sys.executable, os.environ["STUB_REAL_CORE"], *args])
if args[:1] in (["model-availability"], ["model-served"]):
    # The one gate that decides whether a launch may happen at all. The stub
    # hands it to the real core rather than answering for it: a fixture that
    # says yes to every model would test nothing.
    os.execv(sys.executable, [sys.executable, os.environ["STUB_REAL_CORE"], *args])
if args[:1] == ["worktree"]:
    # Delegated work gets its own copy of the code, so the create path asks
    # for one. The stub answers with a directory under the fixture's state so
    # the launch is exercised end to end, and logs every call it is given.
    log=os.environ.get("STUB_WORKTREE_LOG")
    if log:
        with open(log,"a",encoding="utf-8") as handle:
            handle.write(json.dumps(args)+"\\n")
    if len(args) > 1 and args[1] == "copy-check":
        # Three answers, and the launcher must tell them apart. Exit 0 with a
        # reason: do not copy this one. Exit 1: copying it is fine, the
        # ordinary answer. Anything else: the check itself broke, which is not
        # a pass -- STUB_COPY_CHECK_BROKEN makes it break.
        if os.environ.get("STUB_COPY_CHECK_BROKEN"):
            print("Traceback (most recent call last): simulated", file=sys.stderr)
            raise SystemExit(70)
        shared=os.environ.get("STUB_SHARED_REPO","")
        if shared and shared in args:
            print(f"{shared} carries a .session-kit-shared marker")
            raise SystemExit(0)
        raise SystemExit(1)
    if len(args) > 1 and args[1] == "materialize":
        options=dict(zip(args[2::2], args[3::2]))
        branch=options.get("--branch","")
        root=pathlib.Path(os.environ["SESSION_KIT_STATE_DIR"])/"worktrees"/"trees"
        path=root/branch.replace("/","-")
        path.mkdir(parents=True, exist_ok=True)
        print(json.dumps({
            "path":str(path),
            "branch":branch,
            "repo":options.get("--repo",""),
            "auto":"--auto" in args,
            "created":True,
        },sort_keys=True))
        raise SystemExit(0)
    print("{}")
    raise SystemExit(0)
if args[:2] == ["platform", "codex-refresh-target"] and len(args) == 6:
    print("\t".join((
        os.environ.get("STUB_REFRESH_PID", args[4]),
        os.environ.get("STUB_REFRESH_START", args[5]),
    )))
    raise SystemExit(0)
if args[:2] == ["platform", "process-is"] and len(args) == 5:
    if os.environ.get("STUB_PROCESS_IS_TRUE") == "1":
        raise SystemExit(0)
    pid=int(args[2]); generation=int(args[3]); executable=args[4]
    try:
        stat_text=pathlib.Path(f"/proc/{pid}/stat").read_text()
        current=int(stat_text.rsplit(")",1)[1].split()[19])
        argv=pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\\0")
        actual=pathlib.Path(argv[0].decode("utf-8","replace")).name
    except (OSError,ValueError,IndexError):
        raise SystemExit(1)
    raise SystemExit(0 if current == generation and actual == executable else 1)
if args[:1] == ["account"] and len(args) >= 2:
    # Every account verb the automatic handoff calls, answered from fixtures
    # and logged in call order. No real profile, feed or login is touched.
    log=os.environ.get("STUB_ACCOUNT_LOG")
    if log:
        with open(log, "a") as handle:
            handle.write(json.dumps(args)+"\\n")
    verb=args[1]
    if verb == "auto-plan":
        print(os.environ.get("STUB_ACCOUNT_PLAN") or json.dumps({"action":"hold","reason":"no fixture plan","source_alias":"","target_alias":""}))
        raise SystemExit(0)
    if verb == "auto-begin":
        if os.environ.get("STUB_ACCOUNT_BEGIN_FAILS") == "1":
            sys.stderr.write("this conversation has already had its 1 automatic move\\n")
            raise SystemExit(1)
        print(os.environ.get("STUB_ACCOUNT_HOP_TOKEN", "b"*32))
        raise SystemExit(0)
    if verb in ("auto-commit","auto-release"):
        raise SystemExit(0)
    if verb == "auto-target-ok":
        if os.environ.get("STUB_ACCOUNT_TARGET_GONE") == "1":
            print(json.dumps({"alias":args[3],"eligible":False,"reason":"it was switched off"}))
            raise SystemExit(1)
        print(json.dumps({"alias":args[3],"eligible":True,"reason":""}))
        raise SystemExit(0)
    if verb == "switch-prepare":
        print(json.dumps({"txid":os.environ.get("STUB_ACCOUNT_TXID","0"*32)}))
        raise SystemExit(0)
    if verb == "switch-commit" and os.environ.get("STUB_ACCOUNT_COMMIT_FAILS") == "1":
        raise SystemExit(1)
    if verb in ("sync-ui","switch-commit","switch-rollback"):
        print(json.dumps({"ok":True}))
        raise SystemExit(0)
    raise SystemExit(2)
if args[:1] == ["codex-bounce-title"] and len(args) == 2:
    title=os.environ.get("STUB_CODEX_BOUNCE_TITLE", "")
    if title:
        print(title)
        raise SystemExit(0)
    raise SystemExit(1)
if args[:1] == ["claude-bounce-title"] and len(args) == 2:
    if os.environ.get("STUB_CLAUDE_BOUNCE_CLEAR") == "1":
        raise SystemExit(3)
    title=os.environ.get("STUB_CLAUDE_BOUNCE_TITLE", "")
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
if args[:2] == ["color", "conversation-pick"] and len(args) == 4:
    color_log=os.environ.get("STUB_COLOR_LOG")
    if color_log:
        with pathlib.Path(color_log).open("a",encoding="utf-8") as handle:
            handle.write(json.dumps(args)+"\\n")
    print(json.dumps({"schema_version":1,"color":"orange"},sort_keys=True))
    raise SystemExit(0)
if args[:2] in (["color", "set"], ["color", "delete"]):
    action=args[1]
    tail=args[2:]
    if action == "set" and len(tail) == 3:
        provider,uuid,color=tail
    elif action == "delete" and len(tail) == 2:
        provider,uuid=tail
        color=None
    else:
        raise SystemExit(2)
    if provider not in {"claude","codex"}:
        raise SystemExit(2)
    config=pathlib.Path(os.environ["SESSION_KIT_CONFIG"])
    data=json.loads(config.read_text())
    stored=dict(data.get("colors",{}))
    key=f"{provider}:{uuid.lower()}"
    if action == "set":
        stored[key]=color
    else:
        stored.pop(key,None)
    data["colors"]=stored
    fd,tmp=tempfile.mkstemp(prefix=".config.",dir=config.parent)
    with os.fdopen(fd,"w") as out: json.dump(data,out)
    os.replace(tmp,config)
    print(json.dumps({"schema_version":1,"colors":stored},sort_keys=True))
    raise SystemExit(0)
if args[:2] == ["color", "propagate"] and len(args) == 4:
    color_log=os.environ.get("STUB_COLOR_LOG")
    if color_log:
        with pathlib.Path(color_log).open("a",encoding="utf-8") as handle:
            handle.write(json.dumps(args)+"\\n")
    if os.environ.get("STUB_COLOR_PROPAGATE_FAIL") == "1":
        raise SystemExit(1)
    print(json.dumps({"schema_version":1,"provider_color_pushes":[],"provider_color_warnings":[]},sort_keys=True))
    raise SystemExit(0)
if args[:2] == ["alias", "push"] and len(args) == 4:
    name_log=os.environ.get("STUB_NAME_PUSH_LOG")
    if name_log:
        with pathlib.Path(name_log).open("a",encoding="utf-8") as handle:
            handle.write(json.dumps(args)+"\\n")
    if os.environ.get("STUB_ALIAS_PUSH_FAIL") == "1":
        raise SystemExit(1)
    if os.environ.get("STUB_ALIAS_PUSH_PARTIAL") == "1":
        print(json.dumps({"schema_version":1,"title":"fixture name","provider_title_pushes":["codex-session-index"],"provider_title_warnings":["Codex thread row not found; thread title not set"]},sort_keys=True))
        raise SystemExit(3)
    print(json.dumps({"schema_version":1,"title":"fixture name","provider_title_pushes":["claude-nameintent"],"provider_title_warnings":[]},sort_keys=True))
    raise SystemExit(0)
if args and args[0] == "snapshot":
    dynamic_provider=os.environ.get("STUB_DYNAMIC_PROVIDER")
    dynamic_status=os.environ.get("STUB_DYNAMIC_AGENT_STATUS", "working")
    dynamic_count=0
    if dynamic_provider and any(os.environ.get(name) for name in (
        "STUB_DYNAMIC_AGENT_STATUS_AFTER_FIRST",
        "STUB_DYNAMIC_ACCOUNT_ALIAS_AFTER_FIRST",
        "STUB_DYNAMIC_ACCOUNT_MISMATCH_AFTER_FIRST",
    )):
        counter=pathlib.Path(os.environ["STUB_SNAPSHOT_COUNT"])
        try: dynamic_count=int(counter.read_text())
        except (OSError,ValueError): dynamic_count=0
        dynamic_count += 1
        counter.write_text(str(dynamic_count))
        if dynamic_count > 1 and os.environ.get("STUB_DYNAMIC_AGENT_STATUS_AFTER_FIRST"):
            dynamic_status=os.environ["STUB_DYNAMIC_AGENT_STATUS_AFTER_FIRST"]
    if dynamic_provider and os.environ.get("STUB_DYNAMIC_AFTER_SESSIONS"):
        current_state=json.loads(pathlib.Path(os.environ["FAKE_SHPOOL_STATE"]).read_text())
        if len(current_state.get("sessions",[])) <= int(os.environ["STUB_DYNAMIC_AFTER_SESSIONS"]):
            dynamic_provider=None
    if dynamic_provider:
        state=json.loads(pathlib.Path(os.environ["FAKE_SHPOOL_STATE"]).read_text())
        rows=[]
        for index,item in enumerate(state.get("sessions",[]),1):
            name=item["name"]
            skip_name_file=os.environ.get("STUB_DYNAMIC_SKIP_NAME_FILE")
            if (skip_name_file and pathlib.Path(skip_name_file).is_file()
                    and pathlib.Path(skip_name_file).read_text() == name):
                continue
            gone_pid=os.environ.get("FAKE_KILL_PROC_PID")
            exact_log=os.environ.get("SESSION_KIT_TEST_EXACT_SIGNAL_LOG")
            gone_name=os.environ.get("STUB_DYNAMIC_SKIP_NAME_AFTER_SIGNAL")
            proc_root=os.environ.get("SESSION_KIT_PROC_ROOT")
            exact_gone=(
                bool(exact_log and pathlib.Path(exact_log).is_file())
                or bool(gone_pid and proc_root and not (pathlib.Path(proc_root)/gone_pid).exists())
            )
            if ((gone_pid == str(1000 + index) or gone_name == name) and exact_gone):
                continue
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
                "identity":{"uuid":uuid_overrides.get(name,os.environ.get("STUB_DYNAMIC_UUID")),"pid":int(os.environ.get("STUB_DYNAMIC_PROVIDER_PID") or 2000+index),"process_start_ticks":int(os.environ.get("STUB_DYNAMIC_PROVIDER_START") or 20000+index),"confidence":"exact"},
                "title":"dynamic proof",
                "native_title":"dynamic proof",
                "cwd":os.environ.get("STUB_DYNAMIC_CWD","/srv/project"),
                "process_age_seconds":1,
                "agent_status":dynamic_status,
                "model":os.environ.get("STUB_DYNAMIC_MODEL",""),
                # A real snapshot carries the stamp beside what the row IS, and
                # only where a stamp exists. STUB_DYNAMIC_ORIGIN asks for a
                # STAMPED session, so the stub writes both.
                # STUB_DYNAMIC_ORIGIN_INFERRED asks for the other kind: a row
                # that reads as a machine's with no stamp behind it, which is
                # what an unstamped App Server looks like the moment its window
                # is gone.
                "origin":os.environ.get(
                    "STUB_DYNAMIC_ORIGIN",
                    os.environ.get("STUB_DYNAMIC_ORIGIN_INFERRED", "human"),
                ),
                **({"origin_recorded":os.environ["STUB_DYNAMIC_ORIGIN"]}
                   if os.environ.get("STUB_DYNAMIC_ORIGIN") else {}),
                "model_handoff_capable":os.environ.get("STUB_DYNAMIC_MODEL_HANDOFF","1") == "1",
                "needs_you":False,
                "subagents":[],
                # An account switch is not instant: the request file is written,
                # the managed shell consumes it, and only then does the row
                # report the new account. AFTER_REQUEST stands in for that shell
                # so the wait-for-the-new-alias step can actually be exercised.
                "account_alias":(
                    os.environ["STUB_DYNAMIC_ACCOUNT_ALIAS_AFTER_REQUEST"]
                    if os.environ.get("STUB_DYNAMIC_ACCOUNT_ALIAS_AFTER_REQUEST")
                    and (pathlib.Path(os.environ.get("SESSION_KIT_STATE_DIR","/nonexistent"))/"account-switch-requests"/name).exists()
                    else os.environ["STUB_DYNAMIC_ACCOUNT_ALIAS_AFTER_FIRST"]
                    if dynamic_count > 1 and os.environ.get("STUB_DYNAMIC_ACCOUNT_ALIAS_AFTER_FIRST")
                    else os.environ.get("STUB_DYNAMIC_ACCOUNT_ALIAS","")
                ),
                "account_binding_mismatch":(
                    dynamic_count > 1
                    and os.environ.get("STUB_DYNAMIC_ACCOUNT_MISMATCH_AFTER_FIRST") == "1"
                ),
                "account_switch_capable":os.environ.get("STUB_DYNAMIC_ACCOUNT_CAPABLE","0") == "1",
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
elif args[:1] == ["validate-worker-model"] and len(args) == 3:
    import re as _re
    provider, model = args[1], args[2]
    if _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model):
        print(model)
    else:
        raise SystemExit(1)
elif args[:2] == ["closed-sessions", "record"]:
    with open(os.environ["STUB_CLOSED_LOG"], "a") as handle:
        handle.write(json.dumps(args)+"\\n")
    print(json.dumps({"recorded": True}))
elif args[:2] == ["closed-sessions", "forget"]:
    with open(os.environ["STUB_CLOSED_LOG"], "a") as handle:
        handle.write(json.dumps(args)+"\\n")
    print(json.dumps({"forgotten": 1}))
elif args[:2] == ["recovery-pending", "list"] or args[:1] == ["recovery-selectors"]:
    # Delegated to the real core on purpose. This list is the one projection
    # both recovery surfaces read, and what a screen printed beside each row is
    # how a restore knows the word it was handed still means that row, so a
    # stub reimplementation here would test the stub. The fixture already owns
    # every store either of them reads.
    import subprocess
    # sys.executable is empty under this stub's shebang, so name the
    # interpreter rather than borrowing one that is not there.
    real=os.environ["STUB_REAL_INVENTORY_CORE"]
    raise SystemExit(subprocess.run(["python3", real, *args]).returncode)
elif args[:2] == ["closed-sessions", "list"]:
    payload=os.environ.get("STUB_CLOSED_LIST")
    print(payload if payload else json.dumps({"closed": []}))
elif args[:2] == ["closed-sessions", "stream"]:
    payload=os.environ.get("STUB_CLOSED_LIST")
    document=json.loads(payload) if payload else {"closed": []}
    for row in document.get("closed") or []:
        print(json.dumps(row))
elif args[:2] == ["origin", "record"] and len(args) == 4:
    with open(os.environ["STUB_ORIGIN_LOG"], "a") as handle:
        handle.write(json.dumps(args[2:])+"\\n")
    with open(os.environ["STUB_ORIGIN_INSTANCE_LOG"], "a") as handle:
        handle.write(json.dumps({
            "shell_pid":os.environ.get("SESSION_KIT_ORIGIN_SHELL_PID"),
            "shell_start_ticks":os.environ.get("SESSION_KIT_ORIGIN_SHELL_START_TICKS"),
            "started_at_unix_ms":os.environ.get("SESSION_KIT_ORIGIN_STARTED_AT_UNIX_MS"),
        },sort_keys=True)+"\\n")
    print(json.dumps({"recorded": True}))
elif args and args[0] == "close-intent":
    with open(os.environ["STUB_CLOSE_INTENT_LOG"], "a") as handle:
        handle.write(json.dumps(args)+"\\n")
    if os.environ.get("STUB_CLOSE_INTENT_FAIL") == "1":
        print(json.dumps({"recorded": False, "reason": "injected ledger failure"}))
        raise SystemExit(1)
    print(json.dumps({"recorded": True}))
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
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "SESSION_KIT_STATE_DIR": str(self.state),
            "SESSION_KIT_JOURNAL_DIR": str(self.journals),
            "SESSION_KIT_ARCHIVE_DIR": str(self.archives),
            "SESSION_KIT_JOURNAL_RECOVERY_DIR": str(self.recovery),
            "SESSION_KIT_START_DIR": str(self.start),
            "SESSION_KIT_PROJECTS_FILE": str(self.projects),
            "SESSION_KIT_CONFIG": str(self.config),
            "SESSION_KIT_SHPOOL_CMD": str(self.fake_shpool),
            "SESSION_KIT_INVENTORY_CORE": str(self.fake_core),
            "STUB_REAL_INVENTORY_CORE": str(REPO / "lib" / "session_inventory.py"),
            "SESSION_KIT_NONINTERACTIVE": "1",
            "SESSION_KIT_NO_COLOR": "1",
            "SESSION_KIT_RELEASE_ID": self.release_id,
            "SESSION_KIT_BOOT_ID_FILE": str(self.boot_id),
            "FAKE_SHPOOL_STATE": str(self.shpool_state),
            "FAKE_SHPOOL_LOG": str(self.shpool_log),
            "STUB_INVENTORY": str(self.inventory),
            "STUB_SNAPSHOT_COUNT": str(self.snapshot_count),
            "STUB_CLOSE_INTENT_LOG": str(self.close_intent_log),
            "STUB_WORKTREE_LOG": str(self.worktree_log),
            "STUB_REAL_CORE": str(REPO / "lib" / "session_inventory.py"),
            # A delegated session now cuts a worktree without being asked, and
            # this sandbox sits inside a checkout. Pin the root so nothing a
            # test starts can ever materialize a worktree into a real
            # repository's metadata, whichever core answers the call.
            "SESSION_KIT_WORKTREE_ROOT": str(self.base / "worktrees"),
            "STUB_ORIGIN_LOG": str(self.origin_log),
            "STUB_ORIGIN_INSTANCE_LOG": str(self.origin_instance_log),
            "STUB_CLOSED_LOG": str(self.closed_log),
            "STUB_NAME_PUSH_LOG": str(self.name_push_log),
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

    def test_every_selector_verb_refuses_a_wrong_number_the_same_way(self) -> None:
        """One message, one status, whichever verb typed it.

        Nine verbs used to answer "no unique session matches that selection"
        with status 1 while `sp detail` answered a different sentence with
        status 2, and `sp repair` echoed the number back.
        """
        env = self.fixture.env()
        for argv in (
            ["go", "999"],
            ["close", "999"],
            ["takeover", "999"],
            ["history", "999"],
            ["name", "999", "x"],
            ["name", "reset", "999"],
            ["color", "999", "red"],
            ["color", "reset", "999"],
            ["verify-start", "999"],
            ["teardown", "999"],
            ["repair", "999"],
        ):
            with self.subTest(verb=argv[0]):
                refused = run([SP, *argv], env=env, check=False)
                self.assertEqual(2, refused.returncode)
                self.assertEqual(
                    "session-kit: no session matches that selector\n",
                    refused.stderr,
                )
                self.assertNotIn("999", refused.stderr)

    def test_a_wrong_argument_count_says_what_was_wrong_first(self) -> None:
        """`usage:` alone named the shape of the command, never the mistake."""
        env = self.fixture.env()
        refused = run([SP, "close"], env=env, check=False)
        self.assertEqual(2, refused.returncode)
        self.assertTrue(
            refused.stderr.startswith("session-kit: sp close takes one session"),
            refused.stderr,
        )
        self.assertIn("usage: sp <command>", refused.stderr)

    def test_an_unknown_account_command_never_answers_in_pythons_voice(self) -> None:
        """argparse used to answer here: a sixth error prefix, an internal
        file name, and the seventeen machine subcommands `sp help` withholds
        on purpose."""
        env = self.fixture.env()
        for group, machine_verb in (("account", "choices"), ("worktree", "lookup")):
            with self.subTest(group=group):
                refused = run([SP, group, "bogus"], env=env, check=False)
                self.assertEqual(2, refused.returncode)
                self.assertIn(
                    f"session-kit: no {group} command named bogus", refused.stderr
                )
                self.assertNotIn("session_inventory.py", refused.stderr)
                self.assertNotIn("usage:", refused.stderr)
                self.assertNotIn(machine_verb, refused.stderr)

    def test_no_verb_asks_a_question(self) -> None:
        """No `[y/N]`, no typed words, no confirmation step, anywhere."""
        banned = ("[y/N]", "[Y/n]", "Confirm?", "read -r -n 1 -p")
        for name in (
            "bin/sp",
            "bin/session_kit_common",
            "lib/sh/sp_commands.sh",
            "lib/sh/sp_sessions.sh",
            "lib/sh/sp_core.sh",
        ):
            text = (REPO / name).read_text(encoding="utf-8")
            for phrase in banned:
                with self.subTest(file=name, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_shipped_production_tree_has_no_name_addressed_shpool_kill(self) -> None:
        """No automated surface may select a victim by a reusable name."""
        offenders: list[str] = []
        for root in (REPO / "bin", REPO / "lib", REPO / "bashrc"):
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                if "shpool kill" in text or '"$SK_SHPOOL" kill' in text:
                    offenders.append(str(path.relative_to(REPO)))
        self.assertEqual([], offenders)

    def test_stale_name_reallocation_moves_every_staged_launch_record(self) -> None:
        old_id = "stale-create-name"
        staged = {
            "": "start record\n",
            ".account": "account record\n",
            ".launch": "launch record\n",
            ".prompt": "prompt record\n",
        }
        self.fixture.start.mkdir()
        for suffix, content in staged.items():
            (self.fixture.start / f"{old_id}{suffix}").write_text(
                content, encoding="utf-8"
            )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {"name": old_id, "status": "Disconnected"}
                    ]
                }
            ),
            encoding="utf-8",
        )
        reallocated = run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                'source "$1"; sk_reallocate_stale_launch_name "$2"',
                "stale-record-reallocation",
                REPO / "bin" / "session_kit_common",
                old_id,
            ],
            env=self.fixture.env(),
            check=False,
        )
        self.assertEqual(0, reallocated.returncode, reallocated.stderr)
        new_id = reallocated.stdout.strip()
        self.assertNotEqual(old_id, new_id)
        self.assertIn(f"stale shpool entry {old_id}", reallocated.stderr)
        self.assertIn(f"allocated fresh session name {new_id}", reallocated.stderr)
        for suffix, content in staged.items():
            self.assertFalse((self.fixture.start / f"{old_id}{suffix}").exists())
            self.assertEqual(
                content,
                (self.fixture.start / f"{new_id}{suffix}").read_text(
                    encoding="utf-8"
                ),
            )
        state = json.loads(self.fixture.shpool_state.read_text(encoding="utf-8"))
        self.assertEqual([old_id], [row["name"] for row in state["sessions"]])
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_new_allocates_again_when_a_stale_entry_wins_the_attach_race(self) -> None:
        marker = self.fixture.base / "new-name-collision"
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "STUB_DYNAMIC_PROVIDER": "shell",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "FAKE_CREATE_COLLISION_ONCE": str(marker),
            }
        )
        started = run([SP, "new", "shell", "fixture"], env=env, check=False)
        self.assertEqual(0, started.returncode, started.stderr)
        collided = marker.read_text(encoding="utf-8")
        created = started.stdout.strip().splitlines()[-1]
        self.assertNotEqual(collided, created)
        self.assertIn(f"stale shpool entry {collided}", started.stderr)
        self.assertIn(f"allocated fresh session name {created}", started.stderr)
        self.assertEqual(
            [f"collision {collided}", f"attach {created}"],
            self.fixture.shpool_log.read_text(encoding="utf-8").splitlines(),
        )
        state = json.loads(self.fixture.shpool_state.read_text(encoding="utf-8"))
        self.assertEqual(
            {collided, created}, {row["name"] for row in state["sessions"]}
        )

    def test_restore_allocates_again_when_a_stale_entry_wins_the_attach_race(self) -> None:
        marker = self.fixture.base / "restore-name-collision"
        exact_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_UUID": exact_uuid,
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "FAKE_CREATE_COLLISION_ONCE": str(marker),
            }
        )
        restored = run(
            [SP, "restore-exact", "claude", exact_uuid, self.fixture.project],
            env=env,
            check=False,
        )
        self.assertEqual(0, restored.returncode, restored.stderr)
        collided = marker.read_text(encoding="utf-8")
        created = restored.stdout.strip().splitlines()[-1]
        self.assertNotEqual(collided, created)
        self.assertIn(f"stale shpool entry {collided}", restored.stderr)
        self.assertIn(f"allocated fresh session name {created}", restored.stderr)
        self.assertEqual(
            [f"collision {collided}", f"attach {created}"],
            self.fixture.shpool_log.read_text(encoding="utf-8").splitlines(),
        )
        state = json.loads(self.fixture.shpool_state.read_text(encoding="utf-8"))
        self.assertEqual(
            {collided, created}, {row["name"] for row in state["sessions"]}
        )

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

    def test_ignored_directory_is_never_a_launch_target(self) -> None:
        self.fixture.projects.write_text(
            f"fixture\tignore\t{self.fixture.project}\n", encoding="utf-8"
        )
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
        self.assertIn("unknown or invalid project", refused.stderr)
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
        self.assertIn(
            "could not confirm that Claude started", failed.stderr
        )
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
        self.assertIn("cleared the retained launch record for", verified.stdout)
        self.assertNotIn(shpool_id, verified.stdout)
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
        self.assertIn(
            "Claude is not running in that session yet", refused.stderr
        )
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
        self.assertIn("that conversation is already open", refused.stderr)
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
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_PROVIDER_PRESENCE_OVERRIDE": "present",
            }
        )
        started = run([SP, "new", "codex"], env=env, cwd=self.fixture.project)
        shpool_id = started.stdout.strip().splitlines()[-1]
        self.assertRegex(shpool_id, r"^s[0-9]{8}-[0-9]{6}-[0-9]+$")
        # A proven launch clears both records.
        self.assertFalse((self.fixture.start / shpool_id).exists())
        self.assertFalse((self.fixture.start / f"{shpool_id}.expected").exists())

    def test_new_claude_fallback_reserves_exact_color_before_propagation(self) -> None:
        exact_uuid = "00000000-0000-4000-8000-000000000019"
        color_log = self.fixture.base / "color.log"
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_NO_PREBAKE": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "2",
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_UUID": exact_uuid,
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_COLOR_LOG": str(color_log),
            }
        )

        started = run([SP, "new", "claude"], env=env, cwd=self.fixture.project)

        self.assertRegex(
            started.stdout.strip().splitlines()[-1],
            r"^s[0-9]{8}-[0-9]{6}-[0-9]+$",
        )
        self.assertEqual(
            [
                ["color", "conversation-pick", "claude", exact_uuid],
                ["color", "propagate", "claude", exact_uuid],
            ],
            [json.loads(line) for line in color_log.read_text().splitlines()],
        )

    def test_a_deployed_title_template_reaches_the_codex_command_line(self) -> None:
        """The whole shell wiring, not just the parser inside it.

        The fallback case is covered by the fork test above; this proves a
        template the installer deployed actually decides what a launched Codex
        session puts on the tab.
        """
        (self.fixture.home / ".no_shpool_journal").write_text("", encoding="utf-8")
        source_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        deployed = self.fixture.home / ".codex" / "session-kit"
        deployed.mkdir(parents=True)
        (deployed / "terminal-title.toml").write_text(
            '[tui]\nterminal_title = ["thread", "project"]\n', encoding="utf-8"
        )
        if self.fixture.start.exists():
            shutil.rmtree(self.fixture.start)
        self.fixture.start.mkdir()
        record = self.fixture.start / "fork-session"
        record.write_text(
            f"codex\t{self.fixture.project}\t{source_uuid}\tfork\n",
            encoding="utf-8",
        )
        fake_bin = self.fixture.base / "codex-template-bin"
        fake_bin.mkdir()
        provider_log = self.fixture.base / "codex-template.log"
        write_executable(
            fake_bin / "codex",
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
                "codex-template-test",
                BASHRC,
                self.fixture.project,
                self.fixture.start,
                "codex",
                source_uuid,
            ],
            env=env,
        )
        self.assertEqual(0, launched.returncode, launched.stderr)
        self.assertIn(
            '-c tui.terminal_title=["thread", "project"]', provider_log.read_text()
        )

    def test_tab_title_kill_switch_is_case_insensitive_on_codex_launch(self) -> None:
        """The launch path must agree with the writer and doctor about `off`."""
        (self.fixture.home / ".no_shpool_journal").write_text("", encoding="utf-8")
        source_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        fake_bin = self.fixture.base / "codex-title-off-bin"
        fake_bin.mkdir()
        provider_log = self.fixture.base / "codex-title-off.log"
        write_executable(
            fake_bin / "codex",
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "$PROVIDER_LAUNCH_LOG"\n',
        )
        for value in ("off", "OFF", "Off", " off "):
            with self.subTest(value=value):
                if self.fixture.start.exists():
                    shutil.rmtree(self.fixture.start)
                self.fixture.start.mkdir()
                (self.fixture.start / "fork-session").write_text(
                    f"codex\t{self.fixture.project}\t{source_uuid}\tfork\n",
                    encoding="utf-8",
                )
                env = self.fixture.env()
                env.update(
                    {
                        "SHPOOL_SESSION_NAME": "fork-session",
                        "SHPOOL_JOURNAL": "",
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "PROVIDER_LAUNCH_LOG": str(provider_log),
                        "SESSION_KIT_TAB_TITLE": value,
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
                        "codex-title-off-test",
                        BASHRC,
                        self.fixture.project,
                        self.fixture.start,
                        "codex",
                        source_uuid,
                    ],
                    env=env,
                )
                self.assertEqual(0, launched.returncode, launched.stderr)
                launch_line = provider_log.read_text(encoding="utf-8")
                self.assertNotIn("tui.terminal_title=", launch_line)
                self.assertIn(f"--no-alt-screen fork {source_uuid}", launch_line)

    def test_a_broken_title_template_falls_back_instead_of_breaking_the_launch(
        self,
    ) -> None:
        """`["thread]` is safe-looking text and unterminated TOML."""
        (self.fixture.home / ".no_shpool_journal").write_text("", encoding="utf-8")
        source_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        deployed = self.fixture.home / ".codex" / "session-kit"
        deployed.mkdir(parents=True)
        (deployed / "terminal-title.toml").write_text(
            '[tui]\nterminal_title = ["thread]\n', encoding="utf-8"
        )
        if self.fixture.start.exists():
            shutil.rmtree(self.fixture.start)
        self.fixture.start.mkdir()
        record = self.fixture.start / "fork-session"
        record.write_text(
            f"codex\t{self.fixture.project}\t{source_uuid}\tfork\n",
            encoding="utf-8",
        )
        fake_bin = self.fixture.base / "codex-broken-bin"
        fake_bin.mkdir()
        provider_log = self.fixture.base / "codex-broken.log"
        write_executable(
            fake_bin / "codex",
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
                "codex-broken-test",
                BASHRC,
                self.fixture.project,
                self.fixture.start,
                "codex",
                source_uuid,
            ],
            env=env,
        )
        self.assertEqual(0, launched.returncode, launched.stderr)
        launch_line = provider_log.read_text()
        self.assertIn('-c tui.terminal_title=["activity", "thread"]', launch_line)
        self.assertNotIn('["thread]', launch_line)

    def test_new_session_is_refused_when_the_provider_never_started(self) -> None:
        """Relaxing the identity check must not accept an empty shell."""
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "2",
                "STUB_DYNAMIC_PROVIDER": "unknown",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_PROVIDER_PRESENCE_OVERRIDE": "absent",
            }
        )
        refused = run(
            [SP, "new", "codex"], env=env, check=False, cwd=self.fixture.project
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn(
            "could not confirm that Codex started", refused.stderr
        )

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
                "SESSION_KIT_TESTING": "1",
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
        self.assertIn(
            "could not confirm that Codex reopened the conversation",
            refused.stderr,
        )

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
                **self._bound_kill_fixture(row),
            }
        )
        repaired = run([SP, "repair", "wedged"], env=env)
        log = self.fixture.shpool_log.read_text()
        # The exact old shell is ended before the replacement is launched.
        # Its stale manager entry is finalized before replacement allocation.
        self.assertNotIn("kill wedged\n", log)
        self.assertTrue(log.startswith("attach-exit wedged\nattach "))
        new_id = repaired.stdout.strip().splitlines()[-1]
        self.assertRegex(new_id, r"^s[0-9]{8}-[0-9]{6}-[0-9]+$")
        self.assertNotEqual("wedged", new_id)
        stamps = [
            json.loads(line)
            for line in self.fixture.origin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual("human", stamps[-1][1])

    def test_repair_preserves_a_machine_sessions_origin(self) -> None:
        exact_uuid = "00000000-0000-4000-8000-000000000001"
        source = session_row("worker")
        source["origin"] = "machine"
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(source)), encoding="utf-8"
        )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "worker",
                            "status": "Disconnected",
                            "started_at_unix_ms": source["started_at_unix_ms"],
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
                "STUB_DYNAMIC_ORIGIN": "machine",
                **self._bound_kill_fixture(source),
            }
        )
        run([SP, "repair", "worker"], env=env)
        stamps = [
            json.loads(line)
            for line in self.fixture.origin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual("machine", stamps[-1][1])

    def test_repair_refuses_foreign_socket_holder_before_any_kill(self) -> None:
        row = session_row("wedged")
        row["cwd"] = str(self.fixture.project)
        row["recovery"]["cwd"] = str(self.fixture.project)
        row["shpool_shell"] = {"pid": 100, "process_start_ticks": 10_000}
        row["identity"]["pid"] = 200
        row["identity"]["process_start_ticks"] = 20_000
        guard = inventory_document(row)
        guard["daemon_generation"] = {"pid": 10, "process_start_ticks": 1_000}
        self.fixture.inventory.write_text(json.dumps(guard), encoding="utf-8")
        self.fixture.shpool_state.write_text(
            json.dumps({"sessions": [{
                "name": "wedged",
                "status": "Disconnected",
                "started_at_unix_ms": row["started_at_unix_ms"],
            }]}),
            encoding="utf-8",
        )
        proc_root = self.fixture.base / "repair-flip-proc"
        runtime = self.fixture.base / "repair-flip-runtime"
        (proc_root / "net").mkdir(parents=True)
        (runtime / "shpool").mkdir(parents=True)
        self._write_proc(proc_root, 10, 1, "shpool", b"/operator/shpool\0daemon\0")
        self._write_proc(
            proc_root, 100, 10, "bash", b"bash\0", b"SHPOOL_SESSION_NAME=wedged\0"
        )
        self._write_proc(proc_root, 200, 100, "codex", b"codex\0")
        self._write_proc(proc_root, 20, 1, "shpool", b"/foreign/shpool\0daemon\0")
        self._write_proc(
            proc_root, 300, 20, "bash", b"bash\0", b"SHPOOL_SESSION_NAME=wedged\0"
        )
        socket_path = runtime / "shpool" / "shpool.socket"
        (proc_root / "net" / "unix").write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n"
            "0000000000000000: 00000002 00000000 00010000 0001 01 "
            f"11065 {socket_path}\n",
            encoding="utf-8",
        )
        for pid in (10, 20):
            (proc_root / str(pid) / "fd").mkdir()
        (proc_root / "10/fd/3").symlink_to("/dev/null")
        (proc_root / "20/fd/3").symlink_to("socket:[11065]")
        kill_log = self.fixture.base / "repair-flip-kill.log"
        client = self.fixture.base / "repair-flip-shpool"
        write_executable(
            client,
            """#!/usr/bin/env bash
case ${1:-} in
  list) cat "$FAKE_SHPOOL_STATE" ;;
  kill) printf '%s\n' "${2:-}" >> "$REPAIR_FLIP_KILL_LOG" ;;
  daemon) exit 70 ;;
  *) exit 2 ;;
esac
""",
        )
        env = self.fixture.env()
        env.update({
            "XDG_RUNTIME_DIR": str(runtime),
            "SESSION_KIT_TESTING": "1",
            "SESSION_KIT_TEST_PLATFORM": "Linux",
            "SESSION_KIT_PROC_ROOT": str(proc_root),
            "SESSION_KIT_SHPOOL_CMD": str(client),
            "REPAIR_FLIP_KILL_LOG": str(kill_log),
        })
        result = run([SP, "repair", "wedged"], env=env, check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("socket holder changed before repair close", result.stderr)
        self.assertFalse(kill_log.exists())
        self.assertTrue((proc_root / "100/stat").is_file())
        self.assertTrue((proc_root / "200/stat").is_file())
        self.assertEqual(
            ["wedged"],
            [item["name"] for item in json.loads(self.fixture.shpool_state.read_text())["sessions"]],
        )
    def test_repair_never_turns_a_reading_of_the_moment_into_a_stamp(self) -> None:
        """A repair carries the stamp, and an unstamped session has none.

        Repair closes a session and opens it again, declaring an origin as it
        goes -- and a declaration is written down as a real stamp that no
        later refresh can overturn. If it declared what the row currently
        READS as, an unstamped session whose window is missing would come back
        stamped machine, permanently. That is not a rare pairing: repair is
        the path taken when a window could not be opened, which is the state
        most likely to read "no window" in the first place.
        """
        exact_uuid = "00000000-0000-4000-8000-000000000001"
        source = session_row("worker")
        # No stamp anywhere: what the row reads as is all there is.
        source["origin"] = "machine"
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(source)), encoding="utf-8"
        )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "worker",
                            "status": "Disconnected",
                            "started_at_unix_ms": source["started_at_unix_ms"],
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
                # What an unstamped App Server row looks like at the instant
                # its window is gone: machine by inference, no stamp anywhere.
                "STUB_DYNAMIC_ORIGIN_INFERRED": "machine",
            }
        )
        run([SP, "repair", "worker"], env=env)
        stamps = [
            json.loads(line)
            for line in self.fixture.origin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual("human", stamps[-1][1])

    def test_repair_from_inside_a_session_still_leaves_an_unproven_row_visible(
        self,
    ) -> None:
        """The same repair, run where every agent runs it.

        Not carrying the row's reading is only half the fix: with nothing
        declared and nothing recorded, the restore underneath used to read the
        CALLER instead, and inside a managed session the caller reads machine.
        So the guess came back through the other door and was written down as
        a permanent stamp -- and the operator was told `Moving …` with no
        warning. A repair reopens a conversation that already had an owner; if
        nobody knows who, it is theirs.
        """
        exact_uuid = "00000000-0000-4000-8000-000000000001"
        source = session_row("worker")
        source["origin"] = "machine"
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(source)), encoding="utf-8"
        )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "worker",
                            "status": "Disconnected",
                            "started_at_unix_ms": source["started_at_unix_ms"],
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
                "STUB_DYNAMIC_ORIGIN_INFERRED": "machine",
                "SHPOOL_SESSION_NAME": "s20200102-050607-2000000",
            }
        )
        run([SP, "repair", "worker"], env=env)
        stamps = [
            json.loads(line)
            for line in self.fixture.origin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual("human", stamps[-1][1])

    def test_repair_of_an_ordinary_unstamped_session_stays_the_persons(self) -> None:
        """The plain case, and the one on their box: no stamp, no verdict.

        Not an App Server, nothing inferred, nothing recorded -- an ordinary
        session that has simply never been stamped, which today is every
        session running on their machine. Repairing one from inside a session
        must leave it exactly where it was: in their list. This is the shape
        that regressed, so it is asserted on its own rather than as a variant
        of the inferred-machine case.
        """
        exact_uuid = "00000000-0000-4000-8000-000000000001"
        source = session_row("worker")
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(source)), encoding="utf-8"
        )
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "worker",
                            "status": "Disconnected",
                            "started_at_unix_ms": source["started_at_unix_ms"],
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
                "SHPOOL_SESSION_NAME": "s20200102-050607-2000000",
            }
        )
        run([SP, "repair", "worker"], env=env)
        stamps = [
            json.loads(line)
            for line in self.fixture.origin_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual("human", stamps[-1][1])

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
            # hold the session at "pending" forever.
            # ...and with the kit-owned tab title, which is how the kit owns
            # the tab name on the Codex side (K3) without editing the
            # person's own ~/.codex/config.toml.
            "codex": (
                "-c check_for_update_on_startup=false "
                '-c tui.terminal_title=["activity", "thread"] '
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
        # The reason a maintainer needs lives in the quarantine file name
        # checked below; the person gets one fact and one way forward.
        self.assertIn(
            "did not confirm the new session", failed_attach.stderr
        )
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
        self.assertIn(
            "could not confirm it", failed_generation.stderr
        )
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
        self.assertIn("could not record how it started", failed.stderr)
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
        self.assertIn("requires a conversation UUID", invalid.stderr)

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
        self.assertIn("Closed Codex fixture", closed.stdout)
        self.assertNotIn("target", closed.stdout.splitlines()[-1])
        self.assertEqual("attach-exit target\n", self.fixture.shpool_log.read_text())
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
        self.assertIn("Closed Codex fixture", closed.stdout)
        self.assertNotIn("target", closed.stdout.splitlines()[-1])
        self.assertEqual("attach-exit target\n", self.fixture.shpool_log.read_text())

    def test_close_records_that_the_conversation_was_ended_on_purpose(self) -> None:
        """`sp close` is a person's verb exactly as the picker's `k` is.

        Without a tombstone the crash queue cannot tell the two apart, and a
        conversation somebody deliberately ended comes back as unclaimed
        recovery work the next time the snapshot notices its session gone.
        """
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
        self.assertIn("Closed Codex fixture", closed.stdout)
        recorded = [
            json.loads(line)
            for line in self.fixture.close_intent_log.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertEqual(
            [["close-intent", "record", "codex", target["identity"]["uuid"]]],
            recorded,
        )

    def test_failed_close_ledger_message_does_not_promise_a_recovery_offer(
        self,
    ) -> None:
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
        env.update(
            {
                "SESSION_KIT_CONFIRM_ID": "target",
                "STUB_CLOSE_INTENT_FAIL": "1",
            }
        )

        closed = run([SP, "close", "target"], env=env)

        self.assertIn("Closed Codex fixture", closed.stdout)
        self.assertIn("the provider conversation remains on disk", closed.stderr)
        self.assertIn(
            "repair the Closed sessions ledger before relying on `sp recover`",
            closed.stderr,
        )
        self.assertNotIn("still offered by `sp recover`", closed.stderr)
        picker_source = (REPO / "lib/sh/sp_picker.sh").read_text(encoding="utf-8")
        self.assertIn("the provider conversation remains on disk", picker_source)
        self.assertNotIn("still offered by `sp recover`", picker_source)

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
        guard_row = session_row("old", started=old_ms - 10_000, provider="shell")
        guard_row["shpool_status"] = "Disconnected"
        guard_row["shpool_shell"] = {
            "pid": 100,
            "process_start_ticks": 10_000,
        }
        guard = inventory_document(guard_row)
        guard["daemon_generation"]["process_start_ticks"] = 1_000
        self.fixture.inventory.write_text(json.dumps(guard), encoding="utf-8")
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_TESTING": "1",
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

    def test_prune_refuses_foreign_namesake_proof_and_operator_idle_control_closes(self) -> None:
        proc_root = self.fixture.base / "bound-reaper-proc"
        proc_root.mkdir()
        self._write_proc(proc_root, 10, 1, "shpool", b"/foreign/shpool\0daemon\0")
        self._write_proc(
            proc_root,
            100,
            10,
            "bash",
            b"bash\0",
            b"SHPOOL_SESSION_NAME=main\0",
        )
        self._write_proc(proc_root, 20, 1, "shpool", b"/operator/shpool\0daemon\0")
        self._write_proc(
            proc_root,
            300,
            20,
            "bash",
            b"bash\0",
            b"SHPOOL_SESSION_NAME=main\0",
        )
        self._write_proc(proc_root, 400, 300, "codex", b"codex\0")
        old_ms = int((time.time() - 9 * 86400) * 1000)
        started = old_ms - 1_000
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "main",
                            "status": "Disconnected",
                            "started_at_unix_ms": started,
                            "last_disconnected_at_unix_ms": old_ms,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        operator = session_row("main", started=started, provider="codex")
        operator["shpool_status"] = "Disconnected"
        operator["shpool_shell"] = {
            "pid": 300,
            "process_start_ticks": 30_000,
        }
        document = inventory_document(operator)
        document["daemon_generation"] = {
            "pid": 20,
            "process_start_ticks": 2_000,
        }
        self.fixture.inventory.write_text(json.dumps(document), encoding="utf-8")
        foreign = session_row("main", started=started, provider="shell")
        foreign["shpool_status"] = "Disconnected"
        foreign["shpool_shell"] = {
            "pid": 100,
            "process_start_ticks": 10_000,
        }
        foreign_document = inventory_document(foreign)
        foreign_document["daemon_generation"] = {
            "pid": 10,
            "process_start_ticks": 1_000,
        }
        foreign_guard = self.fixture.base / "foreign-guard.json"
        operator_guard = self.fixture.base / "operator-guard.json"
        foreign_guard.write_text(json.dumps(foreign_document), encoding="utf-8")
        operator_guard.write_text(json.dumps(document), encoding="utf-8")
        switching_core = self.fixture.base / "switching-inventory"
        write_executable(
            switching_core,
            """#!/usr/bin/env python3
import os,pathlib,sys
args=sys.argv[1:]
if args and args[0] == "snapshot":
    try: parent=pathlib.Path(f"/proc/{os.getppid()}/cmdline").read_bytes()
    except OSError: raise SystemExit(2)
    key="FOREIGN_GUARD" if b"shpool_reaper" in parent else "OPERATOR_GUARD"
    print(pathlib.Path(os.environ[key]).read_text(),end="")
    raise SystemExit(0)
os.execv(os.environ["ORIGINAL_INVENTORY_CORE"],[os.environ["ORIGINAL_INVENTORY_CORE"],*args])
""",
        )
        path_bin = self.fixture.base / "bound-reaper-bin"
        path_bin.mkdir()
        (path_bin / "shpool").symlink_to(self.fixture.fake_shpool)
        env = self.fixture.env()
        env.update(
            {
                "HOME": str(self.fixture.home),
                "XDG_STATE_HOME": str(self.fixture.base / "xdg-state"),
                "XDG_DATA_HOME": str(self.fixture.base / "xdg-data"),
                "XDG_CONFIG_HOME": str(self.fixture.base / "xdg-config"),
                "XDG_CACHE_HOME": str(self.fixture.base / "xdg-cache"),
                "XDG_RUNTIME_DIR": str(self.fixture.base / "xdg-runtime"),
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_TEST_PLATFORM": "Linux",
                "SESSION_KIT_PROC_ROOT": str(proc_root),
                "SESSION_KIT_DAEMON_PID": "10",
                "SESSION_KIT_REAPER_SENTINEL": str(self.fixture.base / "enabled"),
                "SESSION_KIT_CONFIRM_ID": "main",
                "SESSION_KIT_INVENTORY_CORE": str(switching_core),
                "FOREIGN_GUARD": str(foreign_guard),
                "OPERATOR_GUARD": str(operator_guard),
                "ORIGINAL_INVENTORY_CORE": str(self.fixture.fake_core),
                "PATH": f"{path_bin}:/usr/bin:/bin",
            }
        )

        seed = run([REAPER], env=env)
        self.assertIn("candidates=0 actions=0", seed.stdout)
        refused = run([SP, "prune"], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("changed or is no longer empty", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())
        self.assertEqual(1, len(json.loads(self.fixture.shpool_state.read_text())["sessions"]))

        # Control: the same operator-bound shell is now genuinely empty. Two
        # observations must still nominate it and the actual prune must close.
        (proc_root / "400/comm").unlink()
        (proc_root / "400/cmdline").unlink()
        (proc_root / "400/environ").unlink()
        (proc_root / "400/stat").unlink()
        (proc_root / "400").rmdir()
        env["SESSION_KIT_DAEMON_PID"] = "20"
        env["SESSION_KIT_INVENTORY_CORE"] = str(self.fixture.fake_core)
        env["FAKE_KILL_PROC_PID"] = "300"
        exact_signal_log = self.fixture.base / "exact-signal.log"
        env["SESSION_KIT_TEST_EXACT_SIGNAL"] = "remove"
        env["SESSION_KIT_TEST_EXACT_SIGNAL_LOG"] = str(exact_signal_log)
        env["FAKE_REQUIRE_EXACT_SIGNAL"] = str(exact_signal_log)
        first = run([REAPER], env=env)
        second = run([REAPER], env=env)
        self.assertIn("candidates=0 actions=0", first.stdout)
        self.assertIn("candidates=1 actions=0", second.stdout)
        closed = run([SP, "prune"], env=env)
        self.assertIn("Closed", closed.stdout)
        self.assertEqual(
            [],
            [
                row["name"]
                for row in json.loads(self.fixture.shpool_state.read_text())["sessions"]
            ],
        )
        self.assertEqual("300\t30000\t15\n", exact_signal_log.read_text())
        self.assertEqual("attach-exit main\n", self.fixture.shpool_log.read_text())

    def test_prune_refuses_a_socket_holder_flip_during_final_raw_list(self) -> None:
        """A namesake answer cannot be combined with the older daemon tree."""
        proc_root = self.fixture.base / "socket-flip-proc"
        runtime = self.fixture.base / "socket-flip-runtime"
        (proc_root / "net").mkdir(parents=True)
        (runtime / "shpool").mkdir(parents=True)
        self._write_proc(proc_root, 10, 1, "shpool", b"/operator/shpool\0daemon\0")
        self._write_proc(
            proc_root, 100, 10, "bash", b"bash\0", b"SHPOOL_SESSION_NAME=main\0"
        )
        self._write_proc(proc_root, 20, 1, "shpool", b"/foreign/shpool\0daemon\0")
        self._write_proc(
            proc_root, 300, 20, "bash", b"bash\0", b"SHPOOL_SESSION_NAME=main\0"
        )
        socket_path = runtime / "shpool" / "shpool.socket"
        (proc_root / "net" / "unix").write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n"
            "0000000000000000: 00000002 00000000 00010000 0001 01 "
            f"11065 {socket_path}\n",
            encoding="utf-8",
        )
        for pid in (10, 20):
            (proc_root / str(pid) / "fd").mkdir()
        (proc_root / "10/fd/3").symlink_to("socket:[11065]")
        (proc_root / "20/fd/3").symlink_to("/dev/null")

        old_ms = int((time.time() - 9 * 86400) * 1000)
        started = old_ms - 1_000
        self.fixture.shpool_state.write_text(
            json.dumps(
                {"sessions": [{
                    "name": "main",
                    "status": "Disconnected",
                    "started_at_unix_ms": started,
                    "last_disconnected_at_unix_ms": old_ms,
                }]}
            ),
            encoding="utf-8",
        )
        row = session_row("main", started=started, provider="shell")
        row["shpool_status"] = "Disconnected"
        row["shpool_shell"] = {"pid": 100, "process_start_ticks": 10_000}
        guard = inventory_document(row)
        guard["daemon_generation"] = {"pid": 10, "process_start_ticks": 1_000}
        self.fixture.inventory.write_text(json.dumps(guard), encoding="utf-8")
        env = self.fixture.env()
        env.update({
            "HOME": str(self.fixture.home),
            "XDG_RUNTIME_DIR": str(runtime),
            "SESSION_KIT_TESTING": "1",
            "SESSION_KIT_TEST_PLATFORM": "Linux",
            "SESSION_KIT_PROC_ROOT": str(proc_root),
            "SESSION_KIT_REAPER_SENTINEL": str(self.fixture.base / "enabled"),
            "SESSION_KIT_CONFIRM_ID": "main",
        })
        self.assertIn("candidates=0", run([REAPER], env=env).stdout)
        self.assertIn("candidates=1", run([REAPER], env=env).stdout)

        kill_log = self.fixture.base / "socket-flip-kill.log"
        switching = self.fixture.base / "socket-flip-shpool"
        write_executable(
            switching,
            """#!/usr/bin/env python3
import os,pathlib,sys
root=pathlib.Path(os.environ["SESSION_KIT_PROC_ROOT"])
args=sys.argv[1:]
if args == ["list","--json"]:
    (root/"10/fd/3").unlink()
    (root/"10/fd/3").symlink_to("/dev/null")
    (root/"20/fd/3").unlink()
    (root/"20/fd/3").symlink_to("socket:[11065]")
    print(pathlib.Path(os.environ["FAKE_SHPOOL_STATE"]).read_text(),end="")
    raise SystemExit(0)
if len(args) == 2 and args[0] == "kill":
    pathlib.Path(os.environ["FLIP_KILL_LOG"]).write_text(args[1]+"\\n")
    raise SystemExit(0)
if args and args[0] == "daemon": raise SystemExit(70)
raise SystemExit(2)
""",
        )
        env.update({
            "SESSION_KIT_SHPOOL_CMD": str(switching),
            "FLIP_KILL_LOG": str(kill_log),
        })
        result = run([SP, "prune"], env=env, check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("socket-holder change", result.stderr)
        self.assertNotIn("Closed", result.stdout)
        self.assertFalse(kill_log.exists())
        self.assertEqual(
            ["main"],
            [item["name"] for item in json.loads(self.fixture.shpool_state.read_text())["sessions"]],
        )

    def test_prune_does_not_report_close_while_exact_shell_survives(self) -> None:
        proc_root = self.fixture.base / "surviving-shell-proc"
        proc_root.mkdir()
        self._write_proc(proc_root, 10, 1, "shpool", b"shpool\0daemon\0")
        self._write_proc(
            proc_root, 100, 10, "bash", b"bash\0", b"SHPOOL_SESSION_NAME=main\0"
        )
        old_ms = int((time.time() - 9 * 86400) * 1000)
        started = old_ms - 1_000
        self.fixture.shpool_state.write_text(
            json.dumps({"sessions": [{
                "name": "main",
                "status": "Disconnected",
                "started_at_unix_ms": started,
                "last_disconnected_at_unix_ms": old_ms,
            }]}),
            encoding="utf-8",
        )
        row = session_row("main", started=started, provider="shell")
        row["shpool_status"] = "Disconnected"
        row["shpool_shell"] = {"pid": 100, "process_start_ticks": 10_000}
        guard = inventory_document(row)
        guard["daemon_generation"] = {"pid": 10, "process_start_ticks": 1_000}
        self.fixture.inventory.write_text(json.dumps(guard), encoding="utf-8")
        env = self.fixture.env()
        env.update({
            "SESSION_KIT_TESTING": "1",
            "SESSION_KIT_TEST_PLATFORM": "Linux",
            "SESSION_KIT_PROC_ROOT": str(proc_root),
            "SESSION_KIT_DAEMON_PID": "10",
            "SESSION_KIT_REAPER_SENTINEL": str(self.fixture.base / "enabled"),
            "SESSION_KIT_CONFIRM_ID": "main",
        })
        self.assertIn("candidates=0", run([REAPER], env=env).stdout)
        self.assertIn("candidates=1", run([REAPER], env=env).stdout)
        result = run([SP, "prune"], env=env, check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("verified shell PID 100 start 10000 survives", result.stderr)
        self.assertNotIn("Closed", result.stdout)
        self.assertTrue((proc_root / "100/stat").is_file())
        self.assertEqual(
            ["main"],
            [
                row["name"]
                for row in json.loads(self.fixture.shpool_state.read_text())["sessions"]
            ],
        )
        self.assertFalse(self.fixture.closed_log.exists())

    def test_prune_skips_name_cleanup_when_holder_changes_after_exact_signal(
        self,
    ) -> None:
        proc_root = self.fixture.base / "post-signal-holder-proc"
        runtime = self.fixture.base / "post-signal-holder-runtime"
        (proc_root / "net").mkdir(parents=True)
        (runtime / "shpool").mkdir(parents=True)
        self._write_proc(
            proc_root, 10, 1, "shpool", b"shpool\0daemon\0", start_ticks=1_000
        )
        self._write_proc(
            proc_root,
            100,
            10,
            "bash",
            b"bash\0",
            b"SHPOOL_SESSION_NAME=main\0",
            start_ticks=10_000,
        )
        self._write_proc(
            proc_root, 20, 1, "shpool", b"shpool\0daemon\0", start_ticks=2_000
        )
        self._write_proc(
            proc_root,
            300,
            20,
            "bash",
            b"bash\0",
            b"SHPOOL_SESSION_NAME=main\0",
            start_ticks=30_000,
        )
        self._write_proc(
            proc_root, 400, 300, "codex", b"codex\0", start_ticks=40_000
        )
        socket_path = runtime / "shpool" / "shpool.socket"
        (proc_root / "net" / "unix").write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n"
            "0000000000000000: 00000002 00000000 00010000 0001 01 "
            f"11065 {socket_path}\n",
            encoding="utf-8",
        )
        for pid in (10, 20):
            (proc_root / str(pid) / "fd").mkdir()
        (proc_root / "10/fd/3").symlink_to("socket:[11065]")
        (proc_root / "20/fd/3").symlink_to("/dev/null")

        old_ms = int((time.time() - 9 * 86400) * 1000)
        started = old_ms - 1_000
        self.fixture.shpool_state.write_text(
            json.dumps({"sessions": [{
                "name": "main",
                "status": "Disconnected",
                "started_at_unix_ms": started,
                "last_disconnected_at_unix_ms": old_ms,
            }]}),
            encoding="utf-8",
        )
        row = session_row("main", started=started, provider="shell")
        row["shpool_status"] = "Disconnected"
        row["shpool_shell"] = {"pid": 100, "process_start_ticks": 10_000}
        guard = inventory_document(row)
        guard["daemon_generation"] = {"pid": 10, "process_start_ticks": 1_000}
        self.fixture.inventory.write_text(json.dumps(guard), encoding="utf-8")

        switching_core = self.fixture.base / "post-signal-switching-core"
        write_executable(
            switching_core,
            """#!/usr/bin/env python3
import os,pathlib,subprocess,sys
args=sys.argv[1:]
real=os.environ["ORIGINAL_INVENTORY_CORE"]
if args[:2] == ["platform","terminate-exact-process"]:
    completed=subprocess.run([real,*args],text=True,capture_output=True)
    sys.stdout.write(completed.stdout); sys.stderr.write(completed.stderr)
    if completed.returncode == 0:
        root=pathlib.Path(os.environ["SESSION_KIT_PROC_ROOT"])
        (root/"10/fd/3").unlink(); (root/"10/fd/3").symlink_to("/dev/null")
        (root/"20/fd/3").unlink(); (root/"20/fd/3").symlink_to("socket:[11065]")
    raise SystemExit(completed.returncode)
if args[:1] == ["platform"]:
    os.execv(real,[real,*args])
stub=os.environ["STUB_INVENTORY_CORE"]
os.execv(stub,[stub,*args])
""",
        )
        signal_log = self.fixture.base / "post-signal-exact.log"
        env = self.fixture.env()
        env.update(
            {
                "HOME": str(self.fixture.home),
                "XDG_RUNTIME_DIR": str(runtime),
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_TEST_PLATFORM": "Linux",
                "SESSION_KIT_PROC_ROOT": str(proc_root),
                "SESSION_KIT_REAPER_SENTINEL": str(self.fixture.base / "enabled"),
                "SESSION_KIT_CONFIRM_ID": "main",
                "SESSION_KIT_TEST_EXACT_SIGNAL": "remove",
                "SESSION_KIT_TEST_EXACT_SIGNAL_LOG": str(signal_log),
                "SESSION_KIT_INVENTORY_CORE": str(switching_core),
                "ORIGINAL_INVENTORY_CORE": str(REPO / "lib" / "session_inventory.py"),
                "STUB_INVENTORY_CORE": str(self.fixture.fake_core),
            }
        )
        self.assertIn("candidates=0", run([REAPER], env=env).stdout)
        self.assertIn("candidates=1", run([REAPER], env=env).stdout)
        closed = run([SP, "prune"], env=env, check=False)
        self.assertNotEqual(0, closed.returncode)
        self.assertNotIn("Closed", closed.stdout)
        self.assertIn("manager entry main", closed.stderr)
        self.assertEqual("100\t10000\t15\n", signal_log.read_text())
        self.assertTrue((proc_root / "300/stat").is_file())
        self.assertTrue((proc_root / "400/stat").is_file())
        self.assertFalse(self.fixture.shpool_log.exists())
        self.assertEqual(
            ["main"],
            [row["name"] for row in json.loads(self.fixture.shpool_state.read_text())["sessions"]],
        )

    def test_reaper_report_refuses_a_shell_outside_the_bound_guard_row(self) -> None:
        proc_root = self.fixture.base / "unbound-report-proc"
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
        started = old_ms - 1_000
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": "main",
                            "status": "Disconnected",
                            "started_at_unix_ms": started,
                            "last_disconnected_at_unix_ms": old_ms,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        row = session_row("main", started=started, provider="shell")
        row["shpool_status"] = "Disconnected"
        row["shpool_shell"] = {
            "pid": 101,
            "process_start_ticks": 10_100,
        }
        guard = inventory_document(row)
        guard["daemon_generation"]["process_start_ticks"] = 1_000
        self.fixture.inventory.write_text(json.dumps(guard), encoding="utf-8")
        path_bin = self.fixture.base / "unbound-report-bin"
        path_bin.mkdir()
        (path_bin / "shpool").symlink_to(self.fixture.fake_shpool)
        env = self.fixture.env()
        env.update(
            {
                "HOME": str(self.fixture.home),
                "XDG_STATE_HOME": str(self.fixture.base / "report-xdg-state"),
                "XDG_DATA_HOME": str(self.fixture.base / "report-xdg-data"),
                "XDG_CONFIG_HOME": str(self.fixture.base / "report-xdg-config"),
                "XDG_CACHE_HOME": str(self.fixture.base / "report-xdg-cache"),
                "XDG_RUNTIME_DIR": str(self.fixture.base / "report-xdg-runtime"),
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_TEST_PLATFORM": "Linux",
                "SESSION_KIT_PROC_ROOT": str(proc_root),
                "SESSION_KIT_DAEMON_PID": "10",
                "SESSION_KIT_REAPER_SENTINEL": str(self.fixture.base / "enabled"),
                "PATH": f"{path_bin}:/usr/bin:/bin",
            }
        )

        refused = run([REAPER], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("invalid inventory or process map", refused.stderr)
        self.assertFalse((self.fixture.state / "prune-candidates.json").exists())
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_the_prune_heading_states_the_window_it_actually_used(self) -> None:
        """`sp prune` inherits the caller's environment, headings and all."""
        session = "s20260811-120000-6"
        proc_root = self.fixture.base / "window-proc"
        proc_root.mkdir()
        self._write_proc(proc_root, 10, 1, "shpool", b"shpool\0daemon\0")
        self._write_proc(
            proc_root,
            100,
            10,
            "bash",
            b"bash\0",
            f"SHPOOL_SESSION_NAME={session}\0".encode(),
        )
        recent_ms = int((time.time() - 2 * 86400) * 1000)
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": session,
                            "status": "Disconnected",
                            "started_at_unix_ms": recent_ms - 10_000,
                            "last_disconnected_at_unix_ms": recent_ms,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        guard_row = session_row(
            session,
            started=recent_ms - 10_000,
            provider="shell",
        )
        guard_row["shpool_status"] = "Disconnected"
        guard_row["shpool_shell"] = {
            "pid": 100,
            "process_start_ticks": 10_000,
        }
        guard = inventory_document(guard_row)
        guard["daemon_generation"]["process_start_ticks"] = 1_000
        self.fixture.inventory.write_text(json.dumps(guard), encoding="utf-8")
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_PROC_ROOT": str(proc_root),
                "SESSION_KIT_DAEMON_PID": "10",
                "SESSION_KIT_REAPER_SENTINEL": str(self.fixture.base / "not-disabled"),
                "SESSION_KIT_PRUNE_DAYS": "1",
                "SESSION_KIT_CONFIRM_ID": session,
            }
        )
        run([REAPER], env=env)
        run([REAPER], env=env)
        document = json.loads(
            (self.fixture.state / "prune-candidates.json").read_text()
        )
        self.assertEqual(1, document["max_age_days"])
        self.assertEqual([session], [x["shpool_id"] for x in document["candidates"]])
        listed = run([SP, "prune"], env=env, check=False)
        combined = listed.stdout + listed.stderr
        self.assertIn("Idle and empty for 1 day", combined)
        self.assertNotIn("seven-day", combined)

    def test_kept_terminal_is_never_nominated_and_never_pruned(self) -> None:
        session = "s20260811-120000-4"
        proc_root = self.fixture.base / "keep-proc"
        proc_root.mkdir()
        self._write_proc(proc_root, 10, 1, "shpool", b"shpool\0daemon\0")
        self._write_proc(
            proc_root,
            100,
            10,
            "bash",
            b"bash\0",
            f"SHPOOL_SESSION_NAME={session}\0".encode(),
        )
        old_ms = int((time.time() - 9 * 86400) * 1000)
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": session,
                            "status": "Disconnected",
                            "started_at_unix_ms": old_ms - 10_000,
                            "last_disconnected_at_unix_ms": old_ms,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        guard_row = session_row(
            session,
            started=old_ms - 10_000,
            provider="shell",
        )
        guard_row["shpool_status"] = "Disconnected"
        guard_row["shpool_shell"] = {
            "pid": 100,
            "process_start_ticks": 10_000,
        }
        guard = inventory_document(guard_row)
        guard["daemon_generation"]["process_start_ticks"] = 1_000
        self.fixture.inventory.write_text(json.dumps(guard), encoding="utf-8")
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_PROC_ROOT": str(proc_root),
                "SESSION_KIT_DAEMON_PID": "10",
                "SESSION_KIT_REAPER_SENTINEL": str(self.fixture.base / "not-disabled"),
            }
        )
        run([REAPER], env=env)
        nominated = run([REAPER], env=env)
        self.assertIn("candidates=1 actions=0 kept=0", nominated.stdout)

        lifecycle.record_provider_exit(
            self.fixture.state,
            session_id=session,
            boot_id="00000000-0000-4000-8000-00000000feed",
            shell_pid=100,
            shell_start_ticks=10_000,
            provider="claude",
            exit_code=0,
            input_tracking=True,
        )
        lifecycle.update_state(
            self.fixture.state,
            session_id=session,
            boot_id="00000000-0000-4000-8000-00000000feed",
            shell_pid=100,
            shell_start_ticks=10_000,
            event="keep",
            keep=True,
        )

        kept = run([REAPER], env=env)
        self.assertIn("candidates=0 actions=0 kept=1", kept.stdout)
        candidates = json.loads(
            (self.fixture.state / "prune-candidates.json").read_text()
        )
        self.assertEqual([], candidates["candidates"])

        candidate = self.fixture.base / "kept-candidate.json"
        candidate.write_text(
            json.dumps(
                {
                    "shpool_id": session,
                    "started_at_unix_ms": old_ms - 10_000,
                    "last_disconnected_at_unix_ms": old_ms,
                    "shell_pid": 100,
                    "shell_start_ticks": 10_000,
                }
            ),
            encoding="utf-8",
        )
        candidate.chmod(0o600)
        refused = run(
            [REAPER, "--verify-candidate", candidate], env=env, check=False
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("marked keep", refused.stderr)
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_shell_used_after_provider_exit_is_never_nominated(self) -> None:
        session = "s20260811-120000-5"
        proc_root = self.fixture.base / "used-proc"
        proc_root.mkdir()
        self._write_proc(proc_root, 10, 1, "shpool", b"shpool\0daemon\0")
        self._write_proc(
            proc_root,
            100,
            10,
            "bash",
            b"bash\0",
            f"SHPOOL_SESSION_NAME={session}\0".encode(),
        )
        old_ms = int((time.time() - 9 * 86400) * 1000)
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": session,
                            "status": "Disconnected",
                            "started_at_unix_ms": old_ms - 10_000,
                            "last_disconnected_at_unix_ms": old_ms,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        guard_row = session_row(
            session,
            started=old_ms - 10_000,
            provider="shell",
        )
        guard_row["shpool_status"] = "Disconnected"
        guard_row["shpool_shell"] = {
            "pid": 100,
            "process_start_ticks": 10_000,
        }
        guard = inventory_document(guard_row)
        guard["daemon_generation"]["process_start_ticks"] = 1_000
        self.fixture.inventory.write_text(json.dumps(guard), encoding="utf-8")
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_PROC_ROOT": str(proc_root),
                "SESSION_KIT_DAEMON_PID": "10",
                "SESSION_KIT_REAPER_SENTINEL": str(self.fixture.base / "not-disabled"),
            }
        )
        run([REAPER], env=env)
        lifecycle.record_provider_exit(
            self.fixture.state,
            session_id=session,
            boot_id="00000000-0000-4000-8000-00000000feed",
            shell_pid=100,
            shell_start_ticks=10_000,
            provider="codex",
            exit_code=0,
            input_tracking=True,
        )
        lifecycle.update_state(
            self.fixture.state,
            session_id=session,
            boot_id="00000000-0000-4000-8000-00000000feed",
            shell_pid=100,
            shell_start_ticks=10_000,
            event="user-input",
        )
        used = run([REAPER], env=env)
        self.assertIn("candidates=0 actions=0 kept=1", used.stdout)

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
        guard_row = session_row("main", started=started, provider="shell")
        guard_row["shpool_status"] = "Disconnected"
        guard_row["shpool_shell"] = {
            "pid": 100,
            "process_start_ticks": 10_000,
        }
        guard = inventory_document(guard_row)
        guard["daemon_generation"]["process_start_ticks"] = 1_000
        self.fixture.inventory.write_text(json.dumps(guard), encoding="utf-8")
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_TESTING": "1",
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
        self.assertIn(
            "session-kit: this session is idle, empty, and safe to close.",
            verified.stdout,
        )

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
        guard = json.loads(self.fixture.inventory.read_text())
        guard["daemon_generation"]["process_start_ticks"] = 1_000
        guard["sessions"][0]["shpool_shell"] = {
            "pid": 100,
            "process_start_ticks": 10_000,
        }
        self.fixture.inventory.write_text(json.dumps(guard), encoding="utf-8")
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_TESTING": "1",
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
        start_ticks: int | None = None,
    ) -> None:
        directory = root / str(pid)
        directory.mkdir()
        fields = [
            "S",
            str(ppid),
            *(["0"] * 17),
            str(pid * 100 if start_ticks is None else start_ticks),
        ]
        (directory / "stat").write_text(
            f"{pid} ({comm}) {' '.join(fields)}\n", encoding="utf-8"
        )
        (directory / "comm").write_text(comm + "\n", encoding="utf-8")
        (directory / "cmdline").write_bytes(cmdline)
        (directory / "environ").write_bytes(environ)

    def _bound_kill_fixture(self, row: dict) -> dict[str, str]:
        """Synthetic holder/shell generations for a successful raw kill."""
        proc_root = self.fixture.base / f"kill-proc-{row['shpool_id_raw']}"
        proc_root.mkdir()
        self._write_proc(
            proc_root, 10, 1, "shpool", b"shpool\0daemon\0", start_ticks=100
        )
        shell = row["shpool_shell"]
        self._write_proc(
            proc_root,
            shell["pid"],
            10,
            "bash",
            b"bash\0",
            f"SHPOOL_SESSION_NAME={row['shpool_id_raw']}\0".encode(),
            start_ticks=shell["process_start_ticks"],
        )
        return {
            "SESSION_KIT_TESTING": "1",
            "SESSION_KIT_TEST_PLATFORM": "Linux",
            "SESSION_KIT_PROC_ROOT": str(proc_root),
            "SESSION_KIT_DAEMON_PID": "10",
            "FAKE_KILL_PROC_PID": str(shell["pid"]),
            "SESSION_KIT_TEST_EXACT_SIGNAL": "remove",
            "SESSION_KIT_TEST_EXACT_SIGNAL_LOG": str(
                self.fixture.base / f"exact-signal-{row['shpool_id_raw']}.log"
            ),
            "STUB_DYNAMIC_SKIP_NAME_AFTER_SIGNAL": row["shpool_id_raw"],
        }

    def test_exact_process_term_then_kill_stays_on_one_generation(self) -> None:
        proc_root = self.fixture.base / "exact-process-proc"
        proc_root.mkdir()
        self._write_proc(
            proc_root, 700, 1, "bash", b"bash\0", start_ticks=70_000
        )
        signal_log = self.fixture.base / "exact-process-signals.log"
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_TEST_PLATFORM": "Linux",
                "SESSION_KIT_PROC_ROOT": str(proc_root),
                "SESSION_KIT_TEST_EXACT_SIGNAL": "survive",
                "SESSION_KIT_TEST_EXACT_SIGNAL_LOG": str(signal_log),
            }
        )
        result = run(
            [
                REPO / "lib" / "session_inventory.py",
                "platform",
                "terminate-exact-process",
                "700",
                "70000",
            ],
            env=env,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("survives TERM and KILL", result.stderr)
        self.assertEqual(
            ["700\t70000\t15", "700\t70000\t9"],
            signal_log.read_text().splitlines(),
        )

    def test_exact_process_reuse_refuses_before_any_signal(self) -> None:
        proc_root = self.fixture.base / "recycled-process-proc"
        proc_root.mkdir()
        self._write_proc(
            proc_root, 700, 1, "bash", b"bash\0", start_ticks=99_999
        )
        signal_log = self.fixture.base / "recycled-process-signals.log"
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_TEST_PLATFORM": "Linux",
                "SESSION_KIT_PROC_ROOT": str(proc_root),
                "SESSION_KIT_TEST_EXACT_SIGNAL": "remove",
                "SESSION_KIT_TEST_EXACT_SIGNAL_LOG": str(signal_log),
            }
        )
        result = run(
            [
                REPO / "lib" / "session_inventory.py",
                "platform",
                "terminate-exact-process",
                "700",
                "70000",
            ],
            env=env,
        )
        self.assertEqual("already-gone\n", result.stdout)
        self.assertFalse(signal_log.exists())

    def test_exact_process_treats_same_generation_zombie_as_gone(self) -> None:
        """A parent that delays wait(2) must not turn a real close into refusal."""
        parent = subprocess.Popen(
            [
                sys.executable,
                "-c",
                """
import os, pathlib, sys, time
child = os.fork()
if child == 0:
    time.sleep(300)
    os._exit(0)
stat = pathlib.Path(f\"/proc/{child}/stat\").read_text()
start = int(stat.rsplit(\")\", 1)[1].split()[19])
print(child, start, flush=True)
sys.stdin.read(1)
os.waitpid(child, 0)
""",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert parent.stdout is not None
            pid, start = parent.stdout.readline().split()
            result = run(
                [
                    REPO / "lib" / "session_inventory.py",
                    "platform",
                    "terminate-exact-process",
                    pid,
                    start,
                ],
                env=self.fixture.env(),
                check=False,
            )
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            self.assertEqual("Z", state)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("terminated\n", result.stdout)
        finally:
            if parent.stdin is not None:
                parent.stdin.write("x")
                parent.stdin.flush()
            parent.wait(timeout=10)
            for stream in (parent.stdin, parent.stdout, parent.stderr):
                if stream is not None:
                    stream.close()


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

    def test_picker_model_change_records_requested_and_returned_events(self) -> None:
        row, process = self._live_idle_codex("Disconnected")
        row["model"] = "gpt-old"
        project = self.fixture.base / "project"
        project.mkdir(exist_ok=True)
        row["cwd"] = str(project)
        row["recovery"]["cwd"] = str(project)
        proof = self._prime(row)
        process.terminate()
        process.wait(timeout=2)
        try:
            env = self.fixture.env()
            env["STUB_PROCESS_IS_TRUE"] = "1"
            env["FAKE_DROP_INVENTORY_ROW"] = str(self.fixture.inventory)
            env["FAKE_DROP_EXACT_SESSION"] = "main2"
            env["STUB_PROCESS_TABLE"] = json.dumps(
                {
                    "processes": [
                        {
                            "pid": row["shpool_shell"]["pid"],
                            "ppid": 1,
                            "start_ticks": row["shpool_shell"]["process_start_ticks"],
                            "cmdline": ["bash"],
                        },
                        {
                            "pid": row["identity"]["pid"],
                            "ppid": row["shpool_shell"]["pid"],
                            "start_ticks": row["identity"]["process_start_ticks"],
                            "cmdline": ["codex", "--model", "gpt-live"],
                        },
                    ]
                }
            )
            completed = run(
                [SP, "picker-change-model", proof, "gpt-old"],
                env=env,
                check=False,
            )
            # Last-reply evidence cannot decline an explicit correction: an
            # in-session model change may have happened since that reply.
            self.assertEqual(1, completed.returncode)
            self.assertIn("Moving", completed.stdout)
            self.assertNotIn("Nothing changed", completed.stdout)
            self.assertIn("could not confirm", completed.stderr)
            self.assertTrue(
                self.fixture.shpool_log.read_text(encoding="utf-8").startswith(
                    "attach-exit main2\nattach "
                )
            )
            records = [
                json.loads(line)
                for line in (self.fixture.state / "action-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            model_events = [
                (item["action"], item["outcome"])
                for item in records
                if item.get("action") == "model_change"
            ]
            self.assertEqual(
                [("model_change", "requested"), ("model_change", "failed")],
                model_events,
            )
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)

    def test_account_switch_refuses_working_subagents_and_kill_switch(self) -> None:
        cases = (
            ("working", [], False, "working"),
            ("idle", [{"status": "working"}], False, "working or its state is unproven"),
            ("idle", [], True, "disabled"),
        )
        for status, subagents, disabled, message in cases:
            with self.subTest(status=status, subagents=bool(subagents), disabled=disabled):
                row = session_row("main2", status="Attached")
                row["agent_status"] = status
                row["subagents"] = subagents
                row["active_subagent_count"] = len(subagents)
                proof = self._prime(row)
                sentinel = self.fixture.state / "account-switching-off"
                if disabled:
                    sentinel.touch(mode=0o600)
                else:
                    sentinel.unlink(missing_ok=True)
                refused = run(
                    [SP, "picker-account-switch", proof, "work"],
                    env=self.fixture.env(),
                    check=False,
                )
                self.assertNotEqual(0, refused.returncode)
                self.assertIn(message, refused.stderr)
                self.assertFalse((self.fixture.state / "account-switches").exists())

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
        # The confirmation names the session, never its ID: the exact ID
        # still travels as the argument in SESSION_KIT_CONFIRM_ID.
        self.assertIn("Codex fixture", moved.stdout)
        self.assertNotIn("main2", moved.stdout)
        # And it names nothing else: a bracketed confirm code beside an
        # action nothing asks about reads as a step still owed.
        self.assertNotIn("[confirm", moved.stdout)
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
            self.assertEqual(
                row["identity"]["uuid"],
                bounce.read_text(encoding="utf-8").splitlines()[0],
            )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)

    def test_explicit_pending_title_refresh_restarts_awaiting_reply_codex(self) -> None:
        # "needs your reply" is a stable between-turn wait state: research
        # sessions spend their whole life in it, so an idle-only rule left
        # their bars unnamed forever. The bounce must treat it as TERM-safe.
        row, process = self._live_idle_codex("Attached")
        row["agent_status"] = "needs your reply"
        proof = self._prime(row)
        marker = self._mark_title_pending()
        env = self.fixture.env()
        env["STUB_CODEX_BOUNCE_TITLE"] = "Release Notes"
        try:
            refreshed = run([SP, "picker-title-refresh", proof], env=env)
            process.wait(timeout=2)
            self.assertEqual(0, refreshed.returncode)
            self.assertFalse(marker.exists())
            bounce = self.fixture.state / "provider-bounce" / "main2"
            self.assertEqual(
                row["identity"]["uuid"],
                bounce.read_text(encoding="utf-8").splitlines()[0],
            )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)

    def _live_named_provider(
        self, executable: str, status: str
    ) -> tuple[dict, subprocess.Popen]:
        binary = self.fixture.base / executable
        binary.symlink_to("/bin/sleep")
        process = subprocess.Popen([binary, "60"])
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
        live = session_row("main2", provider=executable, status=status)
        live["identity"]["pid"] = process.pid
        live["identity"]["process_start_ticks"] = start
        live["agent_status"] = "idle"
        live["recent_output_age_seconds"] = 300
        return live, process

    def test_explicit_pending_title_refresh_restarts_named_claude(self) -> None:
        # The Claude window renames only at SessionStart or the next prompt;
        # a session named after boot with no prompt since can only take its
        # name through a restart-as-resume.
        row, process = self._live_named_provider("claude", "Attached")
        proof = self._prime(row)
        marker = self._mark_title_pending()
        env = self.fixture.env()
        env["STUB_CLAUDE_BOUNCE_TITLE"] = "Headliner Question"
        try:
            refreshed = run([SP, "picker-title-refresh", proof], env=env)
            process.wait(timeout=2)
            self.assertEqual(0, refreshed.returncode)
            self.assertIn("Claude provider", refreshed.stdout)
            self.assertFalse(marker.exists())
            bounce = self.fixture.state / "provider-bounce" / "main2"
            self.assertEqual(
                row["identity"]["uuid"],
                bounce.read_text(encoding="utf-8").splitlines()[0],
            )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)

    def test_claude_bounce_clear_verdict_drops_marker_without_restart(self) -> None:
        # Exit 3 from claude-bounce-title = a prompt followed the intent, so
        # the live window already renamed; the marker is dropped and the
        # provider is never touched.
        row, process = self._live_named_provider("claude", "Attached")
        proof = self._prime(row)
        marker = self._mark_title_pending()
        env = self.fixture.env()
        env["STUB_CLAUDE_BOUNCE_CLEAR"] = "1"
        try:
            refused = run(
                [SP, "picker-title-refresh", proof], env=env, check=False
            )
            self.assertEqual(74, refused.returncode)
            self.assertIsNone(process.poll())
            self.assertFalse(marker.exists())
            self.assertFalse(
                (self.fixture.state / "provider-bounce" / "main2").exists()
            )
        finally:
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
        self.assertIn("Closed Unknown exact shell", closed.stdout)
        self.assertEqual(
            "attach-exit main9\n", self.fixture.shpool_log.read_text()
        )

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

    def test_picker_close_holds_lock_through_exact_shell_termination(self) -> None:
        proof = self._prime(session_row("main2", status="Attached"))
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_CONFIRM_ID": "main2",
                "FAKE_EXPECT_CREATE_LOCK": "locked",
            }
        )
        closed = run([SP, "picker-close", proof], env=env)
        self.assertIn("Closed Codex fixture", closed.stdout)
        self.assertEqual(
            "attach-exit main2\n", self.fixture.shpool_log.read_text()
        )

    def test_picker_close_finishes_manager_entry_when_shell_died_first(self) -> None:
        row = session_row("main2", status="Attached")
        proof = self._prime(row)
        dead = dict(row)
        dead.update(
            provider="unknown",
            display_provider="unknown",
            shpool_status="Disconnected",
            availability="ready",
            shpool_shell=None,
            terminal_number=None,
            mutation_allowed=False,
            mutation_rejection_reason="missing-shell-generation",
            identity={
                "uuid": None,
                "pid": None,
                "process_start_ticks": None,
                "provenance": "none",
                "confidence": "unknown",
            },
        )
        dead_inventory = self.fixture.base / "dead-before-close.json"
        dead_inventory.write_text(
            json.dumps(inventory_document(dead)), encoding="utf-8"
        )
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_CONFIRM_ID": "main2",
                "STUB_SECOND_INVENTORY": str(dead_inventory),
                "STUB_EXACT_ALREADY_GONE": "1",
            }
        )

        closed = run([SP, "picker-close", proof], env=env)

        self.assertEqual(0, closed.returncode, closed.stderr)
        self.assertIn("Closed Codex fixture", closed.stdout)
        self.assertEqual([], json.loads(self.fixture.shpool_state.read_text())["sessions"])
        self.assertEqual("attach-exit main2\n", self.fixture.shpool_log.read_text())

    def test_picker_close_names_manager_entry_when_finalization_fails(self) -> None:
        proof = self._prime(session_row("main2", status="Disconnected"))
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_CONFIRM_ID": "main2",
                "FAKE_ATTACH_FAIL": "1",
            }
        )

        failed = run([SP, "picker-close", proof], env=env, check=False)

        self.assertNotEqual(0, failed.returncode)
        self.assertNotIn("Closed Codex fixture", failed.stdout)
        self.assertIn("manager entry main2 remains", failed.stderr)

    def test_picker_close_without_exact_confirmation_never_kills(self) -> None:
        proof = self._prime(session_row("main2", status="Attached"))
        refused = run(
            [SP, "picker-close", proof],
            env=self.fixture.env(),
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("Nothing changed.", refused.stdout)
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

    def test_close_waits_for_creation_lock_before_final_proof_and_signal(self) -> None:
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
        self.assertEqual(
            "attach-exit main2\n", self.fixture.shpool_log.read_text()
        )

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
        self.assertIn("Named the exact codex session", named.stdout)
        # A provider title can carry control bytes; none may reach the screen.
        self.assertNotIn("\x1b", named.stdout)
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
        self.assertIn("Reset the local name for the exact codex session", reset.stdout)
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
        named = run([SP, "name", "main2", "Config audit"], env=env)
        self.assertIn("Named the Claude session", named.stdout)
        stored = json.loads(self.fixture.config.read_text())
        self.assertEqual(
            "Config audit", stored["aliases"][f"claude:{row['identity']['uuid']}"]
        )

        reset = run([SP, "name", "reset", "main2"], env=env)
        self.assertIn("Reset the local name for the Claude session", reset.stdout)
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
        self.assertIn("Named the Claude session", named.stdout)
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
        self.assertIn("Named the Claude session", named.stdout)
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
        self.assertIn("no session matches that selector", refused.stderr)
        self.assertEqual(2, refused.returncode)

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
        # A person is told what was forked, never which conversations were
        # involved; the exact proof lives in the 0600 launch record.
        self.assertIn("into a separate", forked.stdout)
        self.assertNotIn(fork_uuid, forked.stdout)
        self.assertNotIn(source_uuid, forked.stdout)
        state = json.loads(self.fixture.shpool_state.read_text())
        self.assertEqual(2, len(state["sessions"]))
        self.assertTrue(any(item["name"] == "main2" for item in state["sessions"]))
        generated = [item["name"] for item in state["sessions"] if item["name"] != "main2"]
        self.assertEqual(1, len(generated))
        self.assertRegex(generated[0], r"^s[0-9]{8}-[0-9]{6}-[0-9]+")
        self.assertEqual(f"attach {generated[0]}\n", self.fixture.shpool_log.read_text())
        self.assertFalse(any(self.fixture.start.glob(f"{generated[0]}*")))

    def fork_stamps(self) -> list[list[str]]:
        """Origins recorded by a fork. A path that stamps nothing reads []."""
        if not self.fixture.origin_log.exists():
            return []
        return [
            json.loads(line)[1:]
            for line in self.fixture.origin_log.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def test_picker_fork_allocates_again_after_a_stale_name_collision(self) -> None:
        source_uuid = "00000000-0000-4000-8000-000000000001"
        fork_uuid = "00000000-0000-4000-8000-000000000002"
        row = session_row("main2", provider="claude", uuid=source_uuid)
        row["cwd"] = str(self.fixture.project)
        proof = self._prime(row)
        marker = self.fixture.base / "fork-name-collision"
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_AFTER_SESSIONS": "2",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_DYNAMIC_UUID": fork_uuid,
                "STUB_DYNAMIC_UUID_OVERRIDES": json.dumps({"main2": source_uuid}),
                "FAKE_CREATE_COLLISION_ONCE": str(marker),
                "STUB_DYNAMIC_SKIP_NAME_FILE": str(marker),
            }
        )
        forked = run([SP, "picker-fork", proof], env=env, check=False)
        self.assertEqual(0, forked.returncode, forked.stderr)
        collided = marker.read_text(encoding="utf-8")
        self.assertIn(f"stale shpool entry {collided}", forked.stderr)
        log = self.fixture.shpool_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(f"collision {collided}", log[0])
        self.assertTrue(log[1].startswith("attach "))
        self.assertNotEqual(f"attach {collided}", log[1])
        created = log[1].removeprefix("attach ")
        state = json.loads(self.fixture.shpool_state.read_text(encoding="utf-8"))
        self.assertEqual(
            {"main2", collided, created},
            {row["name"] for row in state["sessions"]},
        )

    def test_picker_fork_says_who_opened_it_instead_of_staying_silent(self) -> None:
        """Fork is the third door that creates a session.

        It wrote a launch record and attached without ever stamping an origin,
        so a fork sat in the person's list because nothing said otherwise --
        including one asked for from inside a session, which is an agent's.
        """
        source_uuid = "00000000-0000-4000-8000-000000000001"
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
                "STUB_DYNAMIC_UUID": "00000000-0000-4000-8000-000000000002",
                "STUB_DYNAMIC_UUID_OVERRIDES": json.dumps({"main2": source_uuid}),
                "SHPOOL_SESSION_NAME": "s20200102-050607-2000000",
            }
        )

        run([SP, "picker-fork", proof], env=env)

        self.assertEqual([["machine"]], self.fork_stamps())

    def test_picker_fork_from_the_picker_is_still_the_persons(self) -> None:
        """The same door, opened the way the operator opens it."""
        source_uuid = "00000000-0000-4000-8000-000000000001"
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
                "STUB_DYNAMIC_UUID": "00000000-0000-4000-8000-000000000002",
                "STUB_DYNAMIC_UUID_OVERRIDES": json.dumps({"main2": source_uuid}),
            }
        )

        run([SP, "picker-fork", proof], env=env)

        self.assertEqual([["human"]], self.fork_stamps())

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
        # A person is told what was forked, never which conversations were
        # involved; the exact proof lives in the 0600 launch record.
        self.assertIn("into a separate", forked.stdout)
        self.assertNotIn(fork_uuid, forked.stdout)
        self.assertNotIn(source_uuid, forked.stdout)
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
        self.assertIn("distinct exact claude conversation was not proven", refused.stderr)
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
        self.assertIn("distinct exact codex conversation was not proven", refused.stderr)
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
            # A child that cannot exec must never return: it is a forked copy
            # of the test runner, and returning resumes the suite from here.
            try:
                os.execvpe(str(SP), [str(SP), "picker-open", str(proof)], env)
            finally:
                os._exit(127)
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
            + "Sample: café, 東京, ☃\n".encode("utf-8")
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


class HistoryRecallScreenTests(unittest.TestCase):
    """The changed recall screens, driven through a real terminal."""

    def setUp(self) -> None:
        self.fixture = CommandFixture()
        self.fixture.journals.mkdir()
        self.fixture.archives.mkdir()
        self.fixture.recovery.mkdir()
        self.data = self.fixture.base / "data"
        self.data.mkdir()

    def tearDown(self) -> None:
        self.fixture.close()

    def env(self) -> dict[str, str]:
        return {
            **self.fixture.env(),
            "SESSION_KIT_TESTING": "1",
            "SESSION_KIT_DATA_DIR": str(self.data),
            "SESSION_KIT_HISTORY_SEARCH_TOOL": str(
                REPO / "lib/sessionkit_inventory/history_search.py"
            ),
            "SESSION_KIT_HISTORY_INVENTORY": str(self.fixture.inventory),
            "SESSION_KIT_TRANSCRIPT_TEXT_TOOL": str(
                REPO / "lib/sessionkit_inventory/transcript_text.py"
            ),
        }

    def _pty(self, *argv: str, env: dict[str, str] | None = None) -> str:
        import pty
        import select

        child_env = os.environ.copy()
        child_env.update(env or self.env())
        pid, descriptor = pty.fork()
        if pid == 0:
            try:
                os.execvpe(str(SP), [str(SP), *argv], child_env)
            finally:
                os._exit(127)
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
                break
        else:
            os.kill(pid, 9)
            self.fail(f"PTY command did not finish: {argv!r}")
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(descriptor)
        return output.decode("utf-8", "replace").replace("\r\n", "\n")

    def test_find_screen_uses_names_counts_and_the_exact_empty_state(self) -> None:
        row = session_row("main2")
        row["title"] = "Orphaned Record"
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(row)), encoding="utf-8"
        )
        history = self.fixture.journals / "main2.rendered.txt"
        history.write_text("needle once\nneedle twice\n", encoding="utf-8")
        shown = self._pty("find", "needle")
        self.assertIn("Matches: 2", shown)
        self.assertIn("Orphaned Record · 2 matches", shown)
        self.assertNotIn(str(history), shown)
        self.assertNotIn("main2", shown)

        empty = run([SP, "find", "absent"], env=self.env())
        self.assertEqual("Matches: none.\n", empty.stdout)
        self.assertEqual("", empty.stderr)

    def test_find_searches_a_dash_leading_term_without_argparse_output(self) -> None:
        history = self.fixture.journals / "main2.rendered.txt"
        history.write_text("alpha --force beta\n", encoding="utf-8")

        shown = run([SP, "find", "--force"], env=self.env(), check=False)

        self.assertEqual(0, shown.returncode)
        self.assertIn("Matches: 1\n", shown.stdout)
        self.assertIn("alpha --force beta", shown.stdout)
        self.assertEqual("", shown.stderr)

    def test_raw_fallback_keeps_stdout_exact_and_notices_on_stderr(self) -> None:
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(session_row("history"))), encoding="utf-8"
        )
        (self.fixture.journals / "history.raw").write_text(
            "remembered output\n", encoding="utf-8"
        )
        shown = self._pty("history", "history")
        self.assertIn("This session has no clean recording", shown)
        self.assertIn("remembered output", shown)
        self.assertNotIn("session-kit:", shown)

        piped = run([SP, "history", "history"], env=self.env())
        self.assertEqual("remembered output\n", piped.stdout)
        self.assertEqual(
            "This session has no clean recording; showing the raw capture.\n",
            piped.stderr,
        )

    def test_transcript_fallback_is_readable_on_a_real_terminal(self) -> None:
        uuid = "00000000-0000-4000-8000-000000000001"
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(session_row("history", uuid=uuid))),
            encoding="utf-8",
        )
        rollout = self.fixture.home / ".codex/sessions/2026/08/13"
        rollout.mkdir(parents=True)
        (rollout / f"rollout-2026-08-13T00-00-00-{uuid}.jsonl").write_text(
            json.dumps({
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "Recall this request"}
                ]},
            }) + "\n",
            encoding="utf-8",
        )
        shown = self._pty("history", "history")
        self.assertIn("This session was never recorded", shown)
        self.assertIn("Recall this request", shown)
        self.assertNotIn("session-kit:", shown)

    def test_stale_sidecar_notice_needs_no_phantom_marker(self) -> None:
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(session_row("history"))), encoding="utf-8"
        )
        journal = self.fixture.journals / "history"
        journal.mkdir()
        capture = journal / "segment-000001.raw"
        capture.write_text("unsafe raw capture\n", encoding="utf-8")
        sidecar = journal / "rendered.txt"
        sidecar.write_text("readable history\n", encoding="utf-8")
        os.utime(sidecar, (1, 1))
        os.utime(capture, (4, 4))
        renderer = self.fixture.base / "successful-renderer"
        write_executable(renderer, "#!/usr/bin/env python3\nraise SystemExit(0)\n")
        env = {**self.env(), "SESSION_KIT_JOURNAL_RENDER_TOOL": str(renderer)}
        shown = self._pty("history", "history", env=env)
        self.assertIn("History rendering needs attention", shown)
        self.assertIn("readable history", shown)
        self.assertNotIn("unsafe raw capture", shown)
        self.assertNotIn("session-kit:", shown)

        redirected = run([SP, "history", "history"], env=env)
        self.assertEqual("readable history\n", redirected.stdout)
        self.assertEqual(
            "History rendering needs attention; showing the last readable version.\n",
            redirected.stderr,
        )

    def test_stale_sidecar_notice_follows_pager_exit(self) -> None:
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(session_row("history"))), encoding="utf-8"
        )
        journal = self.fixture.journals / "history"
        journal.mkdir()
        capture = journal / "segment-000001.raw"
        capture.write_text("raw capture\n", encoding="utf-8")
        sidecar = journal / "rendered.txt"
        sidecar.write_text("readable history\n", encoding="utf-8")
        os.utime(sidecar, (1, 1))
        os.utime(capture, (4, 4))
        renderer = self.fixture.base / "successful-renderer"
        write_executable(renderer, "#!/usr/bin/env python3\nraise SystemExit(0)\n")
        fake_bin = self.fixture.base / "pager-bin"
        fake_bin.mkdir()
        write_executable(
            fake_bin / "less",
            "#!/usr/bin/env bash\ncat\nprintf 'PAGER EXIT\\n'\n",
        )
        env = {
            **self.env(),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SESSION_KIT_NONINTERACTIVE": "0",
            "SESSION_KIT_JOURNAL_RENDER_TOOL": str(renderer),
        }
        shown = self._pty("history", "history", env=env)
        self.assertIn("readable history", shown)
        self.assertLess(
            shown.index("History rendering needs attention"),
            shown.index("readable history"),
        )
        self.assertLess(
            shown.index("PAGER EXIT"),
            shown.rindex("History rendering needs attention"),
        )
        self.assertEqual(2, shown.count("History rendering needs attention"))

    def test_capture_append_inside_grace_period_does_not_false_alarm(self) -> None:
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(session_row("history"))), encoding="utf-8"
        )
        journal = self.fixture.journals / "history"
        journal.mkdir()
        capture = journal / "segment-000001.raw"
        capture.write_text("new live capture bytes\n", encoding="utf-8")
        sidecar = journal / "rendered.txt"
        sidecar.write_text("freshly rendered history\n", encoding="utf-8")
        os.utime(sidecar, ns=(2_000_000_000, 2_000_000_000))
        os.utime(capture, ns=(2_000_600_000, 2_000_600_000))
        renderer = self.fixture.base / "successful-renderer-with-live-race"
        write_executable(renderer, "#!/usr/bin/env python3\nraise SystemExit(0)\n")
        env = {**self.env(), "SESSION_KIT_JOURNAL_RENDER_TOOL": str(renderer)}

        redirected = run([SP, "history", "history"], env=env)

        self.assertEqual("freshly rendered history\n", redirected.stdout)
        self.assertEqual("", redirected.stderr)

    def test_fresh_sidecar_does_not_require_gnu_date(self) -> None:
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(session_row("history"))), encoding="utf-8"
        )
        journal = self.fixture.journals / "history"
        journal.mkdir()
        capture = journal / "segment-000001.raw"
        capture.write_text("captured history\n", encoding="utf-8")
        sidecar = journal / "rendered.txt"
        sidecar.write_text("portable rendered history\n", encoding="utf-8")
        os.utime(capture, ns=(1_000_000_000, 1_000_000_000))
        os.utime(sidecar, ns=(2_000_000_000, 2_000_000_000))
        renderer = self.fixture.base / "successful-portable-renderer"
        write_executable(renderer, "#!/usr/bin/env python3\nraise SystemExit(0)\n")
        fake_bin = self.fixture.base / "bsd-date-bin"
        fake_bin.mkdir()
        write_executable(
            fake_bin / "date",
            "#!/usr/bin/env bash\n"
            "[[ ${1:-} != -r ]] || exit 1\n"
            "exec /usr/bin/date \"$@\"\n",
        )
        env = {
            **self.env(),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SESSION_KIT_JOURNAL_RENDER_TOOL": str(renderer),
        }

        redirected = run([SP, "history", "history"], env=env)

        self.assertEqual("portable rendered history\n", redirected.stdout)
        self.assertEqual("", redirected.stderr)

    def test_failed_flush_notices_even_when_sidecar_is_not_older(self) -> None:
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(session_row("history"))), encoding="utf-8"
        )
        journal = self.fixture.journals / "history"
        journal.mkdir()
        capture = journal / "segment-000001.raw"
        capture.write_text("raw capture\n", encoding="utf-8")
        sidecar = journal / "rendered.txt"
        sidecar.write_text("last readable history\n", encoding="utf-8")
        os.utime(capture, (1, 1))
        os.utime(sidecar, (2, 2))
        renderer = self.fixture.base / "failed-renderer-current-sidecar"
        write_executable(renderer, "#!/usr/bin/env python3\nraise SystemExit(1)\n")
        env = {**self.env(), "SESSION_KIT_JOURNAL_RENDER_TOOL": str(renderer)}

        redirected = run([SP, "history", "history"], env=env)

        self.assertEqual("last readable history\n", redirected.stdout)
        self.assertEqual(
            "History rendering needs attention; showing the last readable version.\n",
            redirected.stderr,
        )


class LockWaitTests(unittest.TestCase):
    """A wedged holder must not stop the machine.

    `flock -x` with no bound meant one command stuck inside the daemon held
    every attach, create and close behind it for as long as it stayed stuck.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".lockwait-", dir=REPO)
        self.base = Path(self.temp.name)
        self.lock = self.base / "create.lock"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _acquire(self, seconds: str) -> subprocess.CompletedProcess:
        command = (
            f"source {COMMON}; exec 9>{self.lock}; sk_lock_acquire 9 {self.lock}"
        )
        return subprocess.run(
            ["bash", "-c", command],
            env={
                **os.environ,
                "HOME": str(self.base),
                "SESSION_KIT_STATE_DIR": str(self.base / "state"),
                "SESSION_KIT_LOCK_WAIT_SECONDS": seconds,
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def test_a_held_lock_refuses_within_the_bound_and_says_so(self) -> None:
        with self.lock.open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            refused = self._acquire("1")
            elapsed = time.monotonic() - started
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("create.lock", refused.stderr)
        self.assertIn("nothing changed", refused.stderr)
        self.assertLess(elapsed, 20)

    def test_a_free_lock_is_still_taken(self) -> None:
        self.assertEqual(0, self._acquire("5").returncode)


class ConfirmExactNeverAsksTests(unittest.TestCase):
    """Nothing is asked, anywhere (operator decision, permanent).

    A person at a terminal is told what is about to happen and it happens.
    Automation, which has nobody to tell, still has to name the exact session
    it is acting on.
    """

    def _drive(self, typed: bytes) -> str:
        import pty
        import select
        import time

        script = (
            "source bin/session_kit_common\n"
            'sk_confirm_exact "Closing" "id1" "Title" "codex" "19" && echo CONFIRMED\n'
            "IFS= read -r next\n"
            'echo "NEXT=[$next]"\n'
        )
        pid, descriptor = pty.fork()
        if pid == 0:
            # A child that cannot exec must never return: it is a forked copy
            # of the test runner, and returning resumes the suite from here.
            try:
                os.chdir(REPO)
                os.execvp("bash", ["bash", "-c", script])
            finally:
                os._exit(127)
        os.write(descriptor, typed)
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
        return output.decode("utf-8", "replace")

    def test_a_person_is_told_what_happens_and_is_never_asked(self) -> None:
        """No question, no bracketed letters, and nothing typed is consumed.

        The action names the session and proceeds; the next read still sees
        the person's own input, because nothing was eaten by a prompt.
        """
        text = self._drive(b"PROBE\n")
        self.assertNotIn("Confirm", text)
        self.assertNotIn("[y/N]", text)
        self.assertIn("Closing session 19 (Title).", text)
        self.assertIn("CONFIRMED", text)
        self.assertIn("NEXT=[PROBE]", text)

    def test_a_pipe_is_not_a_person(self) -> None:
        """Without a terminal the scripted contract applies: only the exact
        SESSION_KIT_CONFIRM_ID confirms; piped keystrokes never do."""
        script = (
            "source bin/session_kit_common\n"
            'sk_confirm_exact "Closing" "id1" "Title" "codex" && echo CONFIRMED\n'
            'SESSION_KIT_CONFIRM_ID=id1 '
            'sk_confirm_exact "Closing" "id1" "Title" "codex" && echo SCRIPTED\n'
        )
        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=REPO,
            input="y\ny\n",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertNotIn("CONFIRMED", completed.stdout)
        self.assertIn("SCRIPTED", completed.stdout)

    def test_the_wrong_scripted_id_says_nothing_and_acts_on_nothing(self) -> None:
        script = (
            "source bin/session_kit_common\n"
            "SESSION_KIT_CONFIRM_ID=other "
            'sk_confirm_exact "Closing" "id1" "Title" "codex" "19" || echo REFUSED\n'
        )
        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=REPO,
            text=True,
            # The scripted contract IS the no-terminal one: sk_confirm_exact
            # consults SESSION_KIT_CONFIRM_ID only when `-t 0` is false. An
            # inherited stdin made that condition the launcher's to decide --
            # through a pipe the child had no terminal and refused, on a
            # terminal it took the person branch and announced the close --
            # so pin the absence the assertion is about.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertIn("REFUSED", completed.stdout)
        self.assertNotIn("Closing", completed.stdout)


if __name__ == "__main__":
    unittest.main()
