#!/usr/bin/env bash
# User-level hook (Notification + UserPromptSubmit + SessionEnd): record the
# moment a Claude session starts or stops needing a person, so the session kit
# learns it from Claude Code itself instead of catching it on the next poll.
#
# One small file per session under
#   ${SESSION_KIT_STATE_DIR:-~/.local/state/session-kit}/attention/claude/<uuid>.json
# and nothing else: no network, no locks, no growth. The picker reads it beside
# `claude agents --json`, which stays as the reconciliation source.
#
# Which notification types mean what is decided here in one place and pinned by
# tests/test_attention_truth.py against the 2.1.229 matcher enum. A type this
# hook does not recognise is left alone deliberately: the poll still sees the
# session, so an unknown vendor event costs a refresh of latency, never a
# wrong answer.
#
# FAIL OPEN: any problem exits 0 with no output. A hook that fails must never
# be able to hold up the session it is watching.

# The payload never becomes an argument. A notification's text is whatever a
# session was asking about, and /proc/<pid>/cmdline is readable by every user
# on the box; /proc/<pid>/environ is readable only by its owner. So the payload
# is read from stdin and handed on in the environment.
set -uo pipefail
INPUT=$(cat 2>/dev/null) || exit 0
[ -n "$INPUT" ] || exit 0

SESSION_KIT_HOOK_PAYLOAD=$INPUT python3 - <<'PY' 2>/dev/null || exit 0
import json
import os
import re
import time
from pathlib import Path

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NEEDS_YOU = {
    "permission_prompt",
    "idle_prompt",
    "elicitation_dialog",
    "agent_needs_input",
}
CLEARING = {
    "elicitation_complete",
    "elicitation_response",
    "auth_success",
    "agent_completed",
}


def clean(value, limit):
    text = "".join(
        character
        for character in str(value or "")
        if character.isprintable() or character == " "
    )
    return text.strip()[:limit]


try:
    payload = json.loads(os.environ.get("SESSION_KIT_HOOK_PAYLOAD") or "")
except ValueError:
    raise SystemExit(0)
if not isinstance(payload, dict):
    raise SystemExit(0)

session_id = str(payload.get("session_id") or "").strip().lower()
if not UUID.match(session_id):
    raise SystemExit(0)
event = clean(payload.get("hook_event_name"), 40)

needs_you = None
notification_type = ""
if event == "Notification":
    notification_type = clean(payload.get("notification_type"), 40)
    if notification_type in NEEDS_YOU:
        needs_you = True
    elif notification_type in CLEARING:
        needs_you = False
elif event == "UserPromptSubmit":
    # The person answered. Whatever the session was waiting for, it is not
    # waiting for that any more.
    needs_you = False
elif event == "SessionEnd":
    needs_you = None
else:
    raise SystemExit(0)

configured = os.environ.get("SESSION_KIT_STATE_DIR")
if configured:
    state_dir = Path(configured)
else:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    state_dir = base / "session-kit"
path = state_dir / "attention" / "claude" / f"{session_id}.json"

if event == "SessionEnd":
    # The session is gone; so is anything it needed.
    try:
        path.unlink()
    except OSError:
        pass
    raise SystemExit(0)
if needs_you is None:
    # A notification type this kit has no opinion about. Leave the record and
    # let the poll speak for this session.
    raise SystemExit(0)

record = {
    "schema_version": 1,
    "session_id": session_id,
    "hook_event": event,
    "notification_type": notification_type,
    "message": clean(payload.get("message"), 200),
    "needs_you": bool(needs_you),
    "recorded_at_ms": time.time_ns() // 1_000_000,
}
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
descriptor = os.open(
    temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
)
with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    json.dump(record, handle, sort_keys=True)
    handle.write("\n")
os.replace(temporary, path)
PY
exit 0
