"""What a Claude session needs, taken from the vendor's own push signal.

Until now "needs your reply" was decided by one poll: `claude agents --json`,
run for every snapshot, read for a `status` string and a `waitingFor` field.
Two things are wrong with that as the PRIMARY signal.

The first is timing. The poll only knows what was true when it ran, and it is
the most expensive thing a snapshot does -- it is the reason the picker cannot
simply ask more often. A session that puts a question on screen the instant
after a collection is invisible until the next one.

The second is vocabulary, and it is the one that has already bitten. The
documented status for a working session is "working". The value a live 2.1.229
actually reports is "busy" (probed live, see GOLDEN_POLL_ROW). Code written
against the documentation matches nothing, forever, silently. Every string this
module compares against is therefore pinned by a golden test, and the tests
carry the date and version they were taken on.

So the primary signal becomes Claude Code's own Notification hook, which fires
at the moment attention is wanted, and the poll is demoted to reconciliation --
still collected, still able to correct a hook record, never the only thing that
can notice a waiting session. The hook writes one small record per session; the
merge below decides which of the two is newer and therefore right.

Kill switch: SESSION_KIT_ATTENTION_SOURCE=poll restores exactly the old
behaviour (the hook is written but never read). `hook` is the drill setting for
proving the hook path alone. Default `auto` merges, and an estate with no hook
installed simply has no records to merge, which is the old behaviour again.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any, Iterable, Mapping

from .common import clean_text, valid_uuid


SCHEMA_VERSION = 1
ATTENTION_DIRECTORY = "attention/claude"
# Hook records and their readers run on this machine. Five seconds covers a
# small scheduling/NTP correction without accepting a wall clock that has
# stepped far enough backwards to turn old input into evidence of a person now.
RECORD_FUTURE_TOLERANCE_MS = 5_000

# ---------------------------------------------------------------------------
# Pinned vendor vocabulary. Every string below was read off a live install, not
# a document; tests/test_attention_truth.py fails if any of them is changed
# without new live evidence.
# ---------------------------------------------------------------------------

# `claude agents --json`, Claude Code 2.1.229, probed 2026-08-13: an interactive
# session that is working reports "busy". The docs say "working". They are
# wrong, and a `== "working"` comparison silently never fires.
#
# The row is the live SHAPE with every identifying field replaced -- pid,
# conversation id, name and working directory are all invented here, because
# this file ships in the public export and the machine it was read on is
# nobody's business. The pinned facts are the KEYS the command returns and the
# value of `status`, and neither depends on whose machine it was read on. Note what is
# NOT in the row: no `waitingFor`, no `aiTitle`, no `agentName`, no colour and
# no messaging socket. Everything the kit shows beyond these seven fields is
# read from somewhere else.
GOLDEN_POLL_ROW = {
    "pid": 424242,
    "cwd": "/home/operator/project",
    "kind": "interactive",
    "startedAt": 1786600761803,
    "sessionId": "00000000-0000-4000-8000-000000000001",
    "name": "An Operator Session",
    "status": "busy",
}
POLL_BUSY_STATUS = "busy"
# Statuses seen on live rows so far: "busy" on a session mid-turn and "idle" on
# one waiting at its prompt (drill session, same day, same version). Neither is
# the documented "working"; both are read here rather than assumed.
GOLDEN_POLL_STATUSES = ("busy", "idle")
# The box updated itself to 2.1.231 during the same session; the command was
# re-run there and returned the identical seven fields and the same "busy".
# Two versions, one vocabulary -- which is worth knowing when the next one
# changes it.
POLL_NEEDS_YOU_STATUSES = frozenset({"waiting", "needs_input", "needs your reply"})

# The Notification hook's own matcher enum, read out of the 2.1.229 binary on
# 2026-08-13. Unknown values are recorded and treated as "no opinion" rather
# than dropped, so a new vendor type cannot make the kit lie.
NOTIFICATION_TYPES = (
    "permission_prompt",
    "idle_prompt",
    "auth_success",
    "elicitation_dialog",
    "elicitation_complete",
    "elicitation_response",
    "agent_needs_input",
    "agent_completed",
)
# The types that mean a person is being waited on.
NEEDS_YOU_NOTIFICATIONS = frozenset(
    {"permission_prompt", "idle_prompt", "elicitation_dialog", "agent_needs_input"}
)
# The types that answer one of those, and the hook events that do the same by
# happening at all: a prompt submitted is a human who has replied.
CLEARING_NOTIFICATIONS = frozenset(
    {"elicitation_complete", "elicitation_response", "auth_success", "agent_completed"}
)
# The hook events that answer a raised hand by happening at all. SessionEnd
# removes the record rather than writing one (the session is gone, so is
# anything it needed), which is why only UserPromptSubmit ever appears in a
# record on disk -- both are named here because this frozenset is the contract
# config/claude/attention_hook.sh implements, and tests check the two against
# each other.
CLEARING_EVENTS = frozenset({"UserPromptSubmit", "SessionEnd"})


def attention_root(state_dir: Path) -> Path:
    return Path(state_dir) / ATTENTION_DIRECTORY


def record_path(state_dir: Path, uuid: str) -> Path | None:
    exact = valid_uuid(uuid)
    if not exact:
        return None
    return attention_root(state_dir) / f"{exact}.json"


def attention_source(environ: Mapping[str, str]) -> str:
    """auto (merge) · poll (pre-hook behaviour) · hook (drills only)."""
    value = (environ.get("SESSION_KIT_ATTENTION_SOURCE") or "auto").strip().lower()
    return value if value in {"auto", "poll", "hook"} else "auto"


def read_record(state_dir: Path, uuid: str) -> dict[str, Any] | None:
    """One session's last hook record, or None when there is no usable one.

    Unreadable is not "nothing needs you": the caller is told there is no
    evidence here, and the poll -- which is still collected -- decides.
    """
    path = record_path(state_dir, uuid)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    if record.get("schema_version") != SCHEMA_VERSION:
        return None
    if valid_uuid(record.get("session_id")) != valid_uuid(uuid):
        return None
    # Typed strictly, because the two natural coercions both lie. `bool("false")`
    # is True, so a record whose needs_you arrived as the STRING "false" would
    # raise a hand nobody raised; and `isinstance(True, int)` is True in Python,
    # so a recorded_at_ms of `true` would pass for the timestamp 1 -- which
    # loses every merge comparison and silently disables the record. A record
    # that is not exactly what the hook writes is not evidence: the poll, which
    # is always collected, answers for that session instead.
    recorded_at_ms = record.get("recorded_at_ms")
    if not isinstance(recorded_at_ms, int) or isinstance(recorded_at_ms, bool):
        return None
    if recorded_at_ms <= 0:
        return None
    if recorded_at_ms > int(time.time() * 1000) + RECORD_FUTURE_TOLERANCE_MS:
        # A future record cannot answer what is true now. In particular, after
        # a backwards clock step an old UserPromptSubmit must not look freshly
        # typed until the wall clock catches up.
        return None
    needs_you = record.get("needs_you")
    if not isinstance(needs_you, bool):
        return None
    return {
        "session_id": valid_uuid(record.get("session_id")),
        "hook_event": clean_text(record.get("hook_event"), 40),
        "notification_type": clean_text(record.get("notification_type"), 40),
        "needs_you": needs_you,
        "message": clean_text(record.get("message"), 200),
        "recorded_at_ms": recorded_at_ms,
    }


def read_all(
    state_dir: Path, uuids: Iterable[object] | None
) -> dict[str, dict[str, Any]]:
    """Hook records for exactly the sessions asked about."""
    records: dict[str, dict[str, Any]] = {}
    for uuid in uuids or ():
        exact = valid_uuid(uuid)
        if not exact:
            continue
        record = read_record(state_dir, exact)
        if record is not None:
            records[exact] = record
    return records


def needs_you_from_notification(notification_type: str) -> bool | None:
    """True, False, or None for a type this kit has no opinion about."""
    if notification_type in NEEDS_YOU_NOTIFICATIONS:
        return True
    if notification_type in CLEARING_NOTIFICATIONS:
        return False
    return None


def poll_attention(raw_status: str, waiting_for: Any) -> tuple[str, bool]:
    """The pre-hook reading of one poll row, unchanged and still the fallback."""
    status = (raw_status or "").casefold()
    needs_you = bool(waiting_for) or status in POLL_NEEDS_YOU_STATUSES
    display = (
        "needs your reply"
        if needs_you
        else ("working" if status == POLL_BUSY_STATUS else status)
    )
    return display or "unknown", needs_you


def merge(
    *,
    poll_status: str,
    poll_needs_you: bool,
    poll_stamp_ms: int | None,
    record: Mapping[str, Any] | None,
    source: str = "auto",
) -> dict[str, Any]:
    """Decide what one session needs, and say which evidence decided it.

    The rule is "the newer evidence wins", and the poll's age is the vendor's
    own ``statusUpdatedAt`` on the session record -- not the time we ran the
    poll, which would make the poll permanently the newest thing in the room.
    Without a stamp there is nothing to compare against, so the rule changes
    shape: only a hook record that RAISES attention outranks the poll, and it
    keeps outranking it until the hook itself takes the hand down (a prompt
    submitted, a clearing notification, the session ending). Be clear about the
    cost of that, because it is not one refresh: a session whose operator
    approved a permission prompt with a keypress sends no clearing signal and
    stays "needs your reply" until its next turn boundary. That is the
    direction to be wrong in -- a needs-you nobody notices costs a person their
    evening, a stale one costs a glance -- but it is stickier than a poll, and
    only on records with no stamp beside them.
    """
    result = {
        "agent_status": poll_status,
        "needs_you": poll_needs_you,
        "attention_source": "poll",
    }
    if source == "poll" or record is None:
        return result
    hook_needs_you = bool(record.get("needs_you"))
    if source == "hook":
        decided_by_hook = True
    elif poll_stamp_ms is None:
        # No stamp to compare against: only a hook record that RAISES attention
        # outranks the poll.
        decided_by_hook = hook_needs_you
    else:
        recorded_at_ms = record.get("recorded_at_ms")
        decided_by_hook = (
            isinstance(recorded_at_ms, int) and recorded_at_ms >= poll_stamp_ms
        )
    if not decided_by_hook:
        return result
    result["needs_you"] = hook_needs_you
    result["attention_source"] = "hook"
    result["notification_type"] = clean_text(
        record.get("notification_type"), 40
    )
    if hook_needs_you:
        result["agent_status"] = "needs your reply"
    elif poll_needs_you:
        # The hook says the question was answered and the poll has not caught
        # up. The status the poll printed was "needs your reply"; the honest
        # replacement is what the session is doing now, which the poll's own
        # raw status already carries.
        result["agent_status"] = "working" if poll_status == "needs your reply" else poll_status
    return result


def default_state_dir(environ: Mapping[str, str], home: Path) -> Path:
    configured = environ.get("SESSION_KIT_STATE_DIR")
    if configured:
        return Path(configured)
    state_home = environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else home / ".local" / "state"
    return base / "session-kit"


def write_record(
    state_dir: Path,
    *,
    session_id: str,
    hook_event: str,
    notification_type: str = "",
    message: str = "",
    needs_you: bool,
    recorded_at_ms: int,
) -> Path | None:
    """Replace one session's record atomically, owner-only. Used by the hook."""
    path = record_path(state_dir, session_id)
    if path is None:
        return None
    payload = {
        "schema_version": SCHEMA_VERSION,
        "session_id": valid_uuid(session_id),
        "hook_event": clean_text(hook_event, 40),
        "notification_type": clean_text(notification_type, 40),
        "message": clean_text(message, 200),
        "needs_you": bool(needs_you),
        "recorded_at_ms": int(recorded_at_ms),
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    os.replace(temporary, path)
    return path
