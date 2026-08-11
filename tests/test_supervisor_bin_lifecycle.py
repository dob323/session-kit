from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import textwrap
import time
import unittest

from tests.support import REPO, run


SUPERVISOR = REPO / "bin" / "supervisor"


SP_STUB = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(os.environ["STUB_ROOT"])
state_path = root / "sessions.json"
log_path = root / "sp-log.jsonl"
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "args": args,
        "background": os.environ.get("SESSION_KIT_BACKGROUND"),
        "confirm_id": os.environ.get("SESSION_KIT_CONFIRM_ID"),
        "msg_key": os.environ.get("SESSION_KIT_MSG_KEY"),
        "cwd": os.getcwd(),
    }) + "\n")


def uuid_for(shpool_id):
    for row in state["sessions"]:
        if row.get("shpool_id_raw") == shpool_id:
            return row["identity"]["uuid"]
    return None


def record_receipt(shpool_id):
    """What the real `sp msg` leaves behind: a send record and its key claim."""
    key = os.environ.get("SESSION_KIT_MSG_KEY")
    uuid = uuid_for(shpool_id)
    if not key or not uuid or os.environ.get("STUB_RECEIPT", "1") != "1":
        return None
    messages = Path(os.environ["SESSION_KIT_STATE_DIR"]) / "messages"
    for name in ("sends", "keys"):
        (messages / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    claim = messages / "keys" / key
    msg_id = claim.read_text().strip() if claim.exists() else "aaaa0001"
    record = {
        "msg_id": msg_id,
        "created_unix_ms": int(time.time() * 1000),
        "operator_text": "standing brief",
        "fyi": True,
        "idempotency_key": key,
        "targets": [
            {
                "thread_key": "claude:" + uuid,
                "provider": "claude",
                "shpool_id": shpool_id,
                "uuid": uuid,
                "terminal_number": 1,
                "title": "Fleet Supervisor",
                "method": "headless-send",
                "status": os.environ.get("STUB_MSG_STATUS", "delivered-woke"),
                "detail": "stub receipt",
                "updated_unix_ms": int(time.time() * 1000),
            }
        ],
    }
    (messages / "sends" / (msg_id + ".json")).write_text(json.dumps(record))
    claim.write_text(msg_id + "\n")
    return msg_id


def record_handoff_reply(shpool_id, msg_id):
    if not msg_id or os.environ.get("STUB_HANDOFF_REPLY", "1") != "1":
        return
    uuid = uuid_for(shpool_id)
    if not uuid:
        return
    messages = Path(os.environ["SESSION_KIT_STATE_DIR"]) / "messages"
    threads = messages / "threads"
    threads.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = os.environ.get(
        "STUB_HANDOFF_TEXT",
        "SESSION_KIT_HANDOFF_V1 "
        + json.dumps(
            {
                "active_decisions": ["Keep terminal 1 reserved"],
                "unresolved_needs_you": [],
                "budget_state": "Within budget",
                "unbriefed_lane_2": [],
                "evidence_paths": ["proof/example"],
            },
            separators=(",", ":"),
        ),
    )
    line = {
        "ts_unix_ms": int(time.time() * 1000),
        "dir": "in",
        "msg_id": msg_id,
        "text": payload,
        "via": "reply",
    }
    path = threads / ("claude:" + uuid + ".jsonl")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


TURN_WRITER = """
import json, sys, time
time.sleep(float(sys.argv[2]))
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "ts_unix_ms": int(time.time() * 1000),
        "event": "turn_done",
        "question": None,
        "source": "hook",
    }) + "\\n")
"""


def record_turn(shpool_id):
    """An unrelated turn boundary, which is never proof of the brief.

    This models another message or a person typing after the send receipt.
    """
    uuid = uuid_for(shpool_id)
    if not uuid or os.environ.get("STUB_UNRELATED_TURN", "0") != "1":
        return
    events = Path(os.environ["SESSION_KIT_STATE_DIR"]) / "events"
    events.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = events / ("claude:" + uuid + ".jsonl")
    delay = os.environ.get("STUB_REGISTER_DELAY", "0")
    # Detached from this process's pipes: the caller reads sp's output with a
    # command substitution, which would otherwise wait for the child too.
    subprocess.Popen(
        [sys.executable, "-c", TURN_WRITER, str(path), delay],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if float(delay) <= 0:
        # A prompt turn must be on disk before this send returns, or the first
        # registration check races the writer.
        for _ in range(200):
            if path.exists():
                return
            time.sleep(0.01)


TRANSCRIPT_WRITER = """
import json, pathlib, sys, time
time.sleep(float(sys.argv[2]))
path = pathlib.Path(sys.argv[1])
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "sessionId": sys.argv[3],
        "type": "user",
        "message": {"content": "[session-kit operator message " + sys.argv[4] + "]"},
    }) + "\\n")
"""


def record_transcript(shpool_id, msg_id):
    """The exact message marker in the exact supervisor transcript."""
    uuid = uuid_for(shpool_id)
    if (
        not uuid
        or not msg_id
        or os.environ.get("STUB_REGISTER", "1") != "1"
    ):
        return
    home = Path(os.environ["HOME"])
    cwd = os.getcwd()
    project = "-" + cwd.strip("/").replace("/", "-")
    path = home / ".claude" / "projects" / project / (uuid + ".jsonl")
    delay = os.environ.get("STUB_REGISTER_DELAY", "0")
    subprocess.Popen(
        [sys.executable, "-c", TRANSCRIPT_WRITER, str(path), delay, uuid, msg_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if float(delay) <= 0:
        for _ in range(200):
            if path.exists():
                return
            time.sleep(0.01)
if args == ["new", "claude"]:
    number = int((root / "next").read_text())
    (root / "next").write_text(str(number + 1))
    shpool_id = f"supervisor-{number}"
    uuid = f"00000000-0000-4000-8000-{number:012d}"
    state["sessions"].append({
        "shpool_id_raw": shpool_id,
        "provider": "claude",
        "identity": {"uuid": uuid, "confidence": "exact"},
    })
    state_path.write_text(json.dumps(state))
    print(f"Starting claude session {shpool_id} in {os.getcwd()}")
    print(shpool_id)
elif len(args) >= 3 and args[0] == "msg":
    msg_id = record_receipt(args[1])
    if "SESSION_KIT_HANDOFF_V1" in args[2]:
        record_handoff_reply(args[1], msg_id)
    else:
        record_turn(args[1])
        record_transcript(args[1], msg_id)
    print("Sent aaaa0001 to 1 target(s)  ·  delivered 1  ·  not delivered 0")
elif len(args) == 2 and args[0] == "close":
    if (
        os.environ.get("SESSION_KIT_NONINTERACTIVE") == "1"
        and os.environ.get("SESSION_KIT_CONFIRM_ID") != args[1]
    ):
        raise SystemExit(3)
    state["sessions"] = [row for row in state["sessions"] if row["shpool_id_raw"] != args[1]]
    state_path.write_text(json.dumps(state))
    print("closed")
elif len(args) == 2 and args[0] == "go":
    print(f"attached {args[1]}")
elif len(args) == 3 and args[0] == "name":
    print(f"named {args[1]} {args[2]}")
else:
    raise SystemExit(2)
'''


STATUS_STUB = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

if sys.argv[1:] not in (["--guard-json"], ["--json"]):
    raise SystemExit(2)
if os.environ.get("STUB_STATUS_FAIL") == "all":
    raise SystemExit(1)
if sys.argv[1:] == ["--guard-json"] and os.environ.get("STUB_GUARD_EMPTY") == "1":
    print(json.dumps({"sessions": []}))
    raise SystemExit(0)
print((Path(os.environ["STUB_ROOT"]) / "sessions.json").read_text())
'''


class SupervisorBinLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="supervisor-bin-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.cwd = self.root / "home"
        self.cwd.mkdir()
        self.fake_home = self.root / "fake-home"
        self.fake_home.mkdir()
        (self.root / "sessions.json").write_text('{"sessions": []}\n')
        (self.root / "next").write_text("1")
        self.sp = self.root / "sp"
        self.status = self.root / "shpool_status"
        self.sp.write_text(textwrap.dedent(SP_STUB), encoding="utf-8")
        self.status.write_text(textwrap.dedent(STATUS_STUB), encoding="utf-8")
        self.sp.chmod(0o755)
        self.status.chmod(0o755)
        self.env = {
            "SESSION_KIT_SP": str(self.sp),
            "SESSION_KIT_SHPOOL_STATUS": str(self.status),
            "SESSION_KIT_STATE_DIR": str(self.state),
            "SESSION_KIT_SUPERVISOR_CWD": str(self.cwd),
            "SESSION_KIT_SUPERVISOR_HANDOFF_WAIT_SECONDS": "0.4",
            "SESSION_KIT_SUPERVISOR_BRIEF_WAIT_SECONDS": "0.4",
            "SESSION_KIT_SUPERVISOR_BRIEF_ATTEMPTS": "3",
            "HOME": str(self.fake_home),
            "STUB_ROOT": str(self.root),
            "STUB_HANDOFF_REPLY": "1",
            "STUB_REGISTER": "1",
        }

    def invoke(self, verb: str, *, check: bool = True, **env: str):
        merged = dict(self.env)
        merged.update(env)
        return run([SUPERVISOR, verb], env=merged, check=check)

    def log(self) -> list[dict]:
        path = self.root / "sp-log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_ensure_creates_once_marks_identity_and_injects_brief(self) -> None:
        created = self.invoke("ensure")
        self.assertEqual("Fleet Supervisor is ready.", created.stdout.strip())
        self.assertNotIn("supervisor-1", created.stdout + created.stderr)
        marker = self.state / "supervisor" / "identity"
        self.assertEqual(
            "claude:00000000-0000-4000-8000-000000000001\n",
            marker.read_text(),
        )
        self.assertEqual(0o600, stat.S_IMODE(marker.stat().st_mode))
        mcp = json.loads((self.state / "supervisor" / "mcp.json").read_text())
        self.assertEqual(
            ["-m", "sessionkit_supervisor"],
            mcp["mcpServers"]["session-kit-supervisor"]["args"],
        )
        entries = self.log()
        self.assertEqual(["new", "claude"], entries[0]["args"])
        self.assertEqual("1", entries[0]["background"])
        self.assertEqual(str(self.cwd), entries[0]["cwd"])
        self.assertEqual(
            ["name", "supervisor-1", "Fleet Supervisor"], entries[1]["args"]
        )
        self.assertEqual("msg", entries[2]["args"][0])
        self.assertIn("Fresh-read law", entries[2]["args"][2])
        self.assertIn('sp self-name "Fleet Supervisor"', entries[2]["args"][2])
        self.assertNotIn("$SK_STATE_DIR", entries[2]["args"][2])
        self.assertNotIn("$INVENTORY_CORE", entries[2]["args"][2])
        self.assertIn(str(self.state.resolve()), entries[2]["args"][2])
        self.assertIn(str((REPO / "lib" / "session_inventory.py").resolve()), entries[2]["args"][2])
        self.assertNotIn("\\n#", entries[2]["args"][2])

        brief_on_disk = (REPO / "config" / "supervisor" / "BRIEF.md").read_text()
        self.assertNotRegex(brief_on_disk, r"/home/[^/\s]+")
        self.assertIn("UTC calendar day", brief_on_disk)

        existing = self.invoke("ensure")
        self.assertEqual("Fleet Supervisor is ready.", existing.stdout.strip())
        self.assertEqual(1, sum(row["args"][:1] == ["new"] for row in self.log()))

    def write_turn_done(self, number: int = 1) -> None:
        """A turn the session finished between two `ensure` runs."""
        uuid = f"00000000-0000-4000-8000-{number:012d}"
        events = self.state / "events"
        events.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts_unix_ms": int(time.time() * 1000),
                "event": "turn_done",
                "question": None,
                "source": "hook",
            }
        )
        with (events / f"claude:{uuid}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def write_brief_transcript(self, number: int = 1, msg_id: str = "aaaa0001") -> None:
        uuid = f"00000000-0000-4000-8000-{number:012d}"
        project = "-" + str(self.cwd).strip("/").replace("/", "-")
        path = self.fake_home / ".claude" / "projects" / project / f"{uuid}.jsonl"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "sessionId": uuid,
                    "type": "user",
                    "message": {
                        "content": f"[session-kit operator message {msg_id}]"
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def briefs(self) -> list[dict]:
        return [
            entry
            for entry in self.log()
            if entry["args"][:1] == ["msg"] and "standing brief" in entry["args"][2]
        ]

    def test_a_registered_supervisor_is_briefed_exactly_once(self) -> None:
        """The proof is the exact message in the exact target transcript."""
        created = self.invoke("ensure")
        self.assertEqual("Fleet Supervisor is ready.", created.stdout.strip())
        sent = self.briefs()
        self.assertEqual(1, len(sent))
        self.assertEqual("supervisor-brief:supervisor-1", sent[0]["msg_key"])

    def test_guard_ambiguity_falls_back_without_creating_a_second_resident(self) -> None:
        self.invoke("ensure")
        repeated = self.invoke("ensure", STUB_GUARD_EMPTY="1")
        self.assertEqual("Fleet Supervisor is ready.", repeated.stdout.strip())
        self.assertEqual(1, sum(row["args"][:1] == ["new"] for row in self.log()))

    def test_inventory_uncertainty_never_creates_a_replacement(self) -> None:
        self.invoke("ensure")
        failed = self.invoke("ensure", check=False, STUB_STATUS_FAIL="all")
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("no replacement was created", failed.stderr)
        self.assertEqual(1, sum(row["args"][:1] == ["new"] for row in self.log()))

    def test_an_unproven_start_is_never_a_silent_success_next_time(self) -> None:
        """The identity marker is published before the brief exists, so a live
        session alone must never answer `ensure` with success."""
        first = self.invoke(
            "ensure", check=False, STUB_REGISTER="0", STUB_MSG_STATUS="unreachable"
        )
        self.assertEqual(1, first.returncode)
        self.assertEqual(3, len(self.briefs()))
        self.assertTrue((self.state / "supervisor" / "identity").exists())
        self.assertFalse((self.state / "supervisor" / "brief-proof").exists())

        second = self.invoke(
            "ensure", check=False, STUB_REGISTER="0", STUB_MSG_STATUS="unreachable"
        )
        self.assertNotEqual(0, second.returncode)
        self.assertEqual("", second.stdout.strip())
        # It resumed against the same session rather than creating another.
        self.assertEqual(6, len(self.briefs()))
        self.assertEqual(1, sum(row["args"][:1] == ["new"] for row in self.log()))

    def test_a_landed_unproven_start_resumes_polling_without_sending(self) -> None:
        first = self.invoke(
            "ensure",
            check=False,
            STUB_REGISTER="0",
            STUB_MSG_STATUS="landed-unconfirmed",
        )
        self.assertEqual(3, first.returncode)
        self.assertEqual(1, len(self.briefs()))

        second = self.invoke(
            "ensure",
            check=False,
            STUB_REGISTER="0",
            STUB_MSG_STATUS="landed-unconfirmed",
        )
        self.assertEqual(3, second.returncode)
        self.assertEqual("", second.stdout.strip())
        # Proof polling resumed; nothing was delivered a second time.
        self.assertEqual(1, len(self.briefs()))
        self.assertIn("sent 0 time(s)", second.stderr)

    def test_a_resumed_ensure_succeeds_on_the_exact_marker_without_resending(self) -> None:
        """Re-entry finishes the start it inherited, against the same session."""
        first = self.invoke(
            "ensure",
            check=False,
            STUB_REGISTER="0",
            STUB_MSG_STATUS="landed-unconfirmed",
        )
        self.assertEqual(3, first.returncode)
        self.write_brief_transcript()
        second = self.invoke("ensure", STUB_REGISTER="0")
        self.assertEqual("Fleet Supervisor is ready.", second.stdout.strip())
        self.assertEqual(1, len(self.briefs()))
        proof = self.state / "supervisor" / "brief-proof"
        self.assertEqual(
            "claude:00000000-0000-4000-8000-000000000001 aaaa0001\n",
            proof.read_text(),
        )
        self.assertEqual(0o600, stat.S_IMODE(proof.stat().st_mode))
        # Proven now, so a third run is the cheap fast path.
        third = self.invoke("ensure")
        self.assertEqual("Fleet Supervisor is ready.", third.stdout.strip())
        self.assertEqual(1, len(self.briefs()))

    def test_an_unreachable_brief_and_later_unrelated_turn_are_not_proof(self) -> None:
        """A turn boundary is caused by any input — another message, a person
        typing. Only a brief that landed makes the next turn mean anything."""
        failed = self.invoke(
            "ensure",
            check=False,
            STUB_REGISTER="0",
            STUB_UNRELATED_TURN="1",
            STUB_MSG_STATUS="unreachable",
        )
        self.assertEqual(1, failed.returncode)
        self.assertIn("NEVER landed", failed.stderr)
        self.assertFalse((self.state / "supervisor" / "brief-proof").exists())
        # The turns are on disk; they simply are not evidence about the brief.
        uuid = "00000000-0000-4000-8000-000000000001"
        events = (self.state / "events" / f"claude:{uuid}.jsonl").read_text()
        self.assertIn("turn_done", events)

    def test_a_landed_brief_and_later_unrelated_turn_are_not_proof(self) -> None:
        first = self.invoke(
            "ensure",
            check=False,
            STUB_REGISTER="0",
            STUB_MSG_STATUS="landed-unconfirmed",
        )
        self.assertEqual(3, first.returncode)
        self.assertEqual(1, len(self.briefs()))
        self.write_turn_done()

        second = self.invoke(
            "ensure",
            check=False,
            STUB_REGISTER="0",
            STUB_MSG_STATUS="landed-unconfirmed",
        )
        self.assertEqual(3, second.returncode)
        self.assertEqual(1, len(self.briefs()))
        self.assertFalse((self.state / "supervisor" / "brief-proof").exists())

    def test_a_landed_brief_keeps_waiting_through_the_whole_budget(self) -> None:
        """A newborn reaches its first turn boundary long after the send
        returns, so every remaining attempt is spent waiting, not sending."""
        created = self.invoke(
            "ensure",
            STUB_MSG_STATUS="landed-unconfirmed",
            # Later than one window, well inside three of them.
            STUB_REGISTER_DELAY="0.9",
        )
        self.assertEqual("Fleet Supervisor is ready.", created.stdout.strip())
        self.assertEqual(1, len(self.briefs()))

    def test_a_landed_brief_is_never_sent_a_second_time(self) -> None:
        """A message in the target's queue is delivery, not a retryable miss."""
        failed = self.invoke(
            "ensure",
            check=False,
            STUB_REGISTER="0",
            STUB_MSG_STATUS="landed-unconfirmed",
        )
        self.assertEqual(3, failed.returncode)
        self.assertIn("LANDED", failed.stderr)
        self.assertIn("was NOT re-sent", failed.stderr)
        self.assertNotIn("NEVER landed", failed.stderr)
        self.assertEqual(1, len(self.briefs()))
        # Exit 3 only after the whole patience budget, never after one window.
        self.assertIn("3 registration check(s)", failed.stderr)
        self.assertIn("sent 1 time(s)", failed.stderr)

    def test_a_brief_that_never_lands_is_retried_then_reported_as_never_landed(
        self,
    ) -> None:
        failed = self.invoke(
            "ensure", check=False, STUB_REGISTER="0", STUB_MSG_STATUS="unreachable"
        )
        self.assertEqual(1, failed.returncode)
        self.assertIn("NEVER landed", failed.stderr)
        self.assertIn("3 attempt(s)", failed.stderr)
        self.assertEqual(3, len(self.briefs()))
        # Every attempt is the same purpose, so the ledger keeps one message.
        keys = {entry["msg_key"] for entry in self.briefs()}
        self.assertEqual({"supervisor-brief:supervisor-1"}, keys)

    def test_no_recorded_receipt_is_never_reported_as_a_proven_miss(self) -> None:
        """Nothing stored is not evidence that nothing landed."""
        failed = self.invoke(
            "ensure", check=False, STUB_REGISTER="0", STUB_RECEIPT="0"
        )
        self.assertEqual(1, failed.returncode)
        self.assertIn("unknown delivery outcome", failed.stderr)
        self.assertIn("was not re-sent", failed.stderr)
        self.assertNotIn("NEVER landed", failed.stderr)
        self.assertEqual(1, len(self.briefs()))

    def test_a_registered_supervisor_stops_the_retry_loop_early(self) -> None:
        """Registration ends the loop; only an unlanded brief earns a repeat."""
        marker = self.state / "supervisor" / "identity"
        failed = self.invoke(
            "ensure", check=False, STUB_REGISTER="0", STUB_MSG_STATUS="unreachable"
        )
        self.assertEqual(1, failed.returncode)
        self.assertEqual(3, len(self.briefs()))
        self.assertTrue(marker.exists())

        # Same stub, now finishing a turn: one attempt is enough.
        second = self.invoke("refresh", SESSION_KIT_NONINTERACTIVE="1")
        self.assertEqual("Fleet Supervisor refreshed.", second.stdout.strip())
        self.assertEqual(
            1,
            len(
                [
                    entry
                    for entry in self.briefs()
                    if entry["args"][1] == "supervisor-2"
                ]
            ),
        )

    def test_open_ensures_then_attaches_exact_session(self) -> None:
        opened = self.invoke("open")
        self.assertIn("attached supervisor-1", opened.stdout)
        self.assertEqual(["go", "supervisor-1"], self.log()[-1]["args"])

    def test_refresh_waits_for_handoff_then_replaces_identity(self) -> None:
        self.invoke("ensure")
        refreshed = self.invoke("refresh", SESSION_KIT_NONINTERACTIVE="1")
        self.assertEqual("Fleet Supervisor refreshed.", refreshed.stdout.strip())
        marker = self.state / "supervisor" / "identity"
        self.assertEqual(
            "claude:00000000-0000-4000-8000-000000000002\n",
            marker.read_text(),
        )
        self.assertFalse((self.state / "supervisor" / "previous-identity").exists())
        self.assertIn(
            "Keep terminal 1 reserved",
            (self.state / "supervisor" / "handoff.md").read_text(),
        )
        calls = [entry["args"] for entry in self.log()]
        close_at = calls.index(["close", "supervisor-1"])
        self.assertEqual("supervisor-1", self.log()[close_at]["confirm_id"])
        new_at = calls.index(["new", "claude"], close_at)
        self.assertLess(close_at, new_at)
        self.assertIn("Replacement handoff", calls[-1][2])
        self.assertIn("Fleet Supervisor handoff", calls[-1][2])
        handoff_call = next(
            entry
            for entry in self.log()
            if "SESSION_KIT_HANDOFF_V1" in " ".join(entry["args"])
        )
        self.assertEqual(
            "supervisor-handoff:supervisor-1", handoff_call["msg_key"]
        )

    def test_refresh_timeout_keeps_the_live_supervisor(self) -> None:
        self.invoke("ensure")
        failed = self.invoke("refresh", check=False, STUB_HANDOFF_REPLY="0")
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("live supervisor was kept", failed.stderr)
        calls = [entry["args"] for entry in self.log()]
        self.assertNotIn(["close", "supervisor-1"], calls)
        state = json.loads((self.root / "sessions.json").read_text())
        self.assertEqual(["supervisor-1"], [row["shpool_id_raw"] for row in state["sessions"]])

    def test_refresh_rejects_malformed_handoff_and_keeps_live_supervisor(self) -> None:
        self.invoke("ensure")
        failed = self.invoke(
            "refresh",
            check=False,
            STUB_HANDOFF_TEXT="not a structured handoff",
        )
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("live supervisor was kept", failed.stderr)
        calls = [entry["args"] for entry in self.log()]
        self.assertNotIn(["close", "supervisor-1"], calls)

    def test_codex_marker_is_never_used_by_refresh_or_close(self) -> None:
        supervisor_dir = self.state / "supervisor"
        supervisor_dir.mkdir(parents=True)
        marker = supervisor_dir / "identity"
        marker.write_text("codex:00000000-0000-4000-8000-000000000099\n")
        marker.chmod(0o600)
        (self.root / "sessions.json").write_text(json.dumps({"sessions": [{
            "shpool_id_raw": "codex-resident",
            "provider": "codex",
            "identity": {"uuid": "00000000-0000-4000-8000-000000000099", "confidence": "exact"},
        }]}))
        failed = self.invoke(
            "refresh", check=False, SESSION_KIT_NONINTERACTIVE="1"
        )
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("private exact Claude identity", failed.stderr)
        self.assertFalse((self.root / "sp-log.jsonl").exists())

    def test_world_readable_marker_is_never_used_by_refresh_or_close(self) -> None:
        supervisor_dir = self.state / "supervisor"
        supervisor_dir.mkdir(parents=True)
        marker = supervisor_dir / "identity"
        marker.write_text("claude:00000000-0000-4000-8000-000000000099\n")
        marker.chmod(0o644)
        (self.root / "sessions.json").write_text(json.dumps({"sessions": [{
            "shpool_id_raw": "insecure-resident",
            "provider": "claude",
            "identity": {"uuid": "00000000-0000-4000-8000-000000000099", "confidence": "exact"},
        }]}))
        failed = self.invoke(
            "refresh", check=False, SESSION_KIT_NONINTERACTIVE="1"
        )
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("private exact Claude identity", failed.stderr)
        self.assertFalse((self.root / "sp-log.jsonl").exists())

    def test_invalid_verb_is_usage_error_without_state_change(self) -> None:
        failed = self.invoke("bad", check=False)
        self.assertEqual(2, failed.returncode)
        self.assertIn("usage: supervisor", failed.stderr)
        self.assertFalse((self.root / "sp-log.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
