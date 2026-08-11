"""Durable record of one project intake, from arrival to the report home.

Layout under ``$SK_STATE_DIR/supervisor/intake`` (mode 0700, files 0600):

    entries/<msg_id>.json   one intake: its lifecycle, its workers, its notes
    keys/<intake_key>       the message id one repeatable intake already has
    aliases/<msg_id>        a redelivered intake's own id, pointing at the first
    intake.lock             flock held across every mutation

An intake arrives as an operator message and outlives the resident that read
it. A supervisor that dies or is refreshed mid-project must lose its
conversation and nothing else: the entry says what was promised, who is
carrying it, and what the source has already been told.

Two arrivals of one intake share one entry. That is the whole double-delegation
guard — a sender that repeats itself, or a replacement resident that reads the
same message again, must find the project already running rather than start it
twice. Every mutation goes through the verbs here: a resident that hand-writes
this directory is forging the one record its replacement will trust.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from typing import (
    Any,
    Callable,
    Iterator,
    Literal,
    Mapping,
    NamedTuple,
    Sequence,
    overload,
)

from sessionkit_messages.envelope import (
    MAX_OPERATOR_TEXT,
    landed,
    new_msg_id,
    valid_idempotency_key,
    valid_msg_id,
    valid_thread_key,
    valid_uuid,
)

# The lifecycle, in the only order it may travel. An intake is reported only
# once the source has actually been told, so a report that never landed leaves
# the project open for whoever reads the spool next.
STATES = ("received", "acknowledged", "delegated", "reported")
_RANK = {name: index for index, name in enumerate(STATES)}

ACKNOWLEDGEMENT = "acknowledgement"
PROGRESS = "progress"
COMPLETION = "completion"
KINDS = (ACKNOWLEDGEMENT, PROGRESS, COMPLETION)

# How the intake reached the spool. Both paths write the same entry: one was
# addressed to the supervisor as a message, the other was taken from a root
# session's own first prompt without anybody addressing anything.
MESSAGE_ORIGIN = "message"
AUTO_ORIGIN = "auto"
ORIGINS = (MESSAGE_ORIGIN, AUTO_ORIGIN)
# The transition a relayed note earns, and only once it has landed. Progress
# is news, not a step: it moves nothing.
_RELAY_STATE = {ACKNOWLEDGEMENT: "acknowledged", COMPLETION: "reported"}

ACTIONS = (
    "record",
    "from-hook",
    "flush",
    "dismiss-machine",
    "ack",
    "preflight",
    "delegate",
    "progress",
    "complete",
    "open",
    "pending",
    "report",
    "retry",
    "reset",
    "cancel",
    "duties",
)

# What one worker's launch has proved so far. `verified` is an exact provider
# identity running the exact model; `commissioned` adds the one thing that
# makes it a worker rather than an idle session — its duty, delivered.
LAUNCH_STATES = (
    "not_started",
    "dispatching",
    "provider_reconciled",
    "verified",
    "commissioned",
)
# What became of the duty itself. The worker reports completed or failed; only
# the supervisor abandons one, and only through `cancel`.
DUTY_STATES = ("assigned", "completed", "failed", "abandoned")
WORKER_REPORT_STATES = ("completed", "failed")
DUTY_DELIVERY_STATES = ("undelivered", "delivered", "failed")

# One entry holds every worker's duty text and every reviewed revision of the
# plan, so the ceiling is generous; it exists to bound a runaway file, not to
# ration the record.
MAX_ENTRY_BYTES = 1024 * 1024
MAX_RECEIPT_BYTES = 16 * 1024
MAX_POINTER_BYTES = 64
MAX_SUMMARY = 500
MAX_TITLE = 200
MAX_CWD = 4096
MAX_NOTE_TEXT = 2000
MAX_NOTES = 100
MAX_AMENDMENTS = 200
MAX_WORKERS = 32
MAX_TASK_TEXT = 6000
MAX_ACCEPTANCE = 2000
MAX_DELIVERABLE = 1000
MAX_DUTY_SUMMARY = 1000
MAX_DUTY_REPORTS = 20
MAX_PREFLIGHTS = 50
MAX_ALIASES = 16
MAX_OPEN = 50
MAX_ENTRIES_SCANNED = 500
MAX_ENTRIES_KEPT = 200
RETENTION_DAYS = 90
RETENTION_MS = RETENTION_DAYS * 24 * 60 * 60 * 1000

# A worker branch is recorded, never executed, but it is also the string a
# replacement resident reads back to find the work, so it stays a plain ref.
BRANCH_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
SOURCE_EVENT_RE = re.compile(r"\A[0-9a-f]{64}\Z")
EXPERTISE_TAGS = (
    "security",
    "implementation",
    "testing",
    "operations",
    "research",
    "documentation",
)
MODEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# One root thread, one OPEN automatic intake: the claim is the thread, and it
# moves to a fresh intake once the one it named has been reported.
AUTO_KEY_PREFIX = "auto-intake"
AMEND_KEY_PREFIX = "intake-amend"
ARRIVAL_KEY_PREFIX = "intake-arrival"
# What the producer did with one prompt. Everything but `duplicate` and
# `refused` changed the record.
CREATED = "created"
REOPENED = "reopened"
AMENDED = "amended"
DUPLICATE = "duplicate"
REFUSED = "refused"
CHANGED = (CREATED, REOPENED, AMENDED)
# What a prompt must be before the machine calls it a project.
SUBSTANTIVE_CHARS = 8
SUBSTANTIVE_WORDS = 2
RESUME_BOILERPLATE = "Continue from where you left off."
DELIVERY_RUNNER_PREFIX = "You are a Session Kit delivery runner."
CROSS_SESSION_MESSAGE_PREFIX = "<cross-session-message "
AUTOMATION_WAKE_PREFIX = "RUNTIME FOR THIS WAKE (ground truth, read off the machine):"
# A prompt built only from these is an answer to something already under way,
# never the statement of a project: "yes please", "go ahead", "ok thanks".
AGREEMENT_WORDS = frozenset(
    {
        "ahead", "continue", "cool", "do", "fine", "go", "good", "great", "it",
        "k", "n", "no", "nope", "now", "ok", "okay", "please", "proceed",
        "right", "sure", "thank", "thanks", "y", "yeah", "yep", "yes", "you",
    }
)
# Payload fields that say this turn belongs to a subagent or a sidechain.
# Root threads only: a Task tool's helper is not a project of its own.
SIDECHAIN_MARKERS = (
    "parent_session_id",
    "parentSessionId",
    "parent_tool_use_id",
    "parentToolUseId",
    "parent_thread_id",
    "is_sidechain",
    "isSidechain",
    "subagent_id",
    "subagent_type",
)
# How long one asked-for `supervisor ensure` covers every other root that
# starts behind it. `ensure` is idempotent and one supervisor serves the fleet.
ENSURE_COOLDOWN_MS = 60_000
# A commissioned worker that has said nothing for this long is not "probably
# fine": it is the exact shape of a worker that finished and never reported.
# The duties view flags it; nothing is decided for you.
SILENT_DUTY_MS = 30 * 60 * 1000
DUTY_KEY_PREFIX = "intake-duty"
# The commissioned message names the worker's own thread key, which a plan row
# cannot know yet. The plan-time length check reserves room for it so a duty
# that fits as a plan cannot stop fitting the moment it has a worker.
DUTY_IDENTITY_RESERVE = 160

# What a relay that did not land waits before a sweep tries it again. The
# first attempt is immediate; each failure moves the notice one rung down and
# the last rung repeats forever. A supervisor that is mid-turn, between
# sessions, or whose harness queue is full is a condition that passes, so a
# failed relay is owed, not refused — it just stops costing a send per wake.
RELAY_BACKOFF_MS = (60_000, 5 * 60_000, 15 * 60_000, 60 * 60_000, 6 * 60 * 60_000)
# How many undelivered relays one pending report names before it stops
# listing them. The count is always exact; the list is for a person to read.
MAX_PENDING_LISTED = 50


class IntakeError(ValueError):
    """An intake record or a requested transition is invalid."""


def now_unix_ms(clock: Callable[[], float] = time.time) -> int:
    return int(clock() * 1000)


# ---- private state on disk ---------------------------------------------


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
    ):
        raise IntakeError(
            f"intake state must be a mode-0700 current-owner directory: {path}"
        )


def _check_private_file(path: Path, info: os.stat_result) -> None:
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_uid != os.geteuid()
    ):
        raise IntakeError(f"intake state must be an owner-private file: {path}")


def _atomic_private_write(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _read_private_bytes(path: Path, limit: int) -> bytes | None:
    """One owner-private file's bytes, or None when it does not exist.

    A file that exists but cannot be trusted — a symlink, another owner's, a
    group-readable one, an oversized one — is an error, never an absence. A
    spool read that answers "no such intake" for a damaged entry is exactly how
    one project gets delegated twice.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    _check_private_file(path, info)
    if info.st_size > limit:
        raise IntakeError(f"intake file exceeds {limit} bytes: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise IntakeError(f"cannot read intake file: {path}") from exc


# ---- validation ---------------------------------------------------------


def _text(value: object, *, limit: int, label: str, required: bool = True) -> str:
    """One bounded line: control characters stripped, whitespace flattened."""
    if value is None:
        if required:
            raise IntakeError(f"{label} is required")
        return ""
    if not isinstance(value, str):
        raise IntakeError(f"{label} must be a string")
    flattened = " ".join(
        "".join(character for character in value if character >= " ").split()
    )
    if required and not flattened:
        raise IntakeError(f"{label} is required")
    return flattened[:limit]


def _stamp(value: object, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise IntakeError(f"{label} must be a positive Unix-millisecond integer")
    return value


def _attempts(value: object) -> int:
    """How many times one relay has actually been tried.

    Read leniently: an entry written before relays counted their attempts is
    not a corrupt entry, it is an entry that has tried an unknown number of
    times and may be tried now.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def next_attempt_unix_ms(attempts: int, now: int) -> int:
    """When a relay that has failed ``attempts`` times may be tried again."""
    rung = min(max(attempts, 1), len(RELAY_BACKOFF_MS)) - 1
    return now + RELAY_BACKOFF_MS[rung]


def relay_due(item: Mapping[str, Any], now: int) -> bool:
    """Whether a sweep may try this undelivered relay now.

    An item with no schedule has never failed, so it is due: a brand-new
    amendment is delivered on the spot exactly as before.
    """
    scheduled = item.get("next_attempt_unix_ms")
    if isinstance(scheduled, bool) or not isinstance(scheduled, int):
        return True
    return scheduled <= now


def _terminal_number(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise IntakeError("a source terminal number must be a positive integer")
    return value


@overload
def _source_event_id(value: object, *, required: Literal[True]) -> str: ...


@overload
def _source_event_id(value: object, *, required: bool = ...) -> str | None: ...


def _source_event_id(value: object, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not SOURCE_EVENT_RE.fullmatch(value):
        raise IntakeError("a hook-origin record needs an exact source_event_id")
    return value


def _valid_branch(value: object) -> str:
    if not isinstance(value, str) or not BRANCH_RE.match(value.strip()):
        raise IntakeError(
            "a worker branch must be 1-128 characters of A-Z a-z 0-9 . _ / - "
            "and start with a letter or digit"
        )
    return value.strip()


def _model(provider: str, value: object) -> str:
    model = _text(value, limit=128, label="requested model")
    if not MODEL_RE.fullmatch(model):
        raise IntakeError("requested model is not a supported identifier")
    if provider == "claude" and not model.startswith("claude-"):
        raise IntakeError("a Claude worker needs a Claude model identifier")
    if provider == "codex" and not model.startswith(("gpt-", "o3", "o4", "codex-")):
        raise IntakeError("a Codex worker needs a Codex model identifier")
    return model


def validate_requested_model(provider: object, value: object) -> str:
    """Single provider-specific model gate shared by preflight and `sp new`."""
    if provider not in ("claude", "codex"):
        raise IntakeError("worker provider must be claude or codex")
    return _model(str(provider), value)


def _expertise(value: object) -> str:
    if value not in EXPERTISE_TAGS:
        raise IntakeError(
            "worker expertise must be one of: " + ", ".join(EXPERTISE_TAGS)
        )
    return str(value)


def _block(value: object, *, limit: int, label: str, required: bool = True) -> str:
    """Bounded prose that keeps its line breaks.

    A duty is written for a worker to read, so paragraphs and checklists have
    to survive the trip. Only the characters a terminal or a transcript would
    misread are dropped; everything a human wrote stays where they put it.
    """
    if value is None:
        if required:
            raise IntakeError(f"{label} is required")
        return ""
    if not isinstance(value, str):
        raise IntakeError(f"{label} must be a string")
    kept = "".join(
        character
        for character in value.replace("\r\n", "\n").replace("\r", "\n")
        if character >= " " or character == "\n" or character == "\t"
    )
    lines = [line.rstrip() for line in kept.split("\n")]
    body = "\n".join(lines).strip("\n")
    if required and not body.strip():
        raise IntakeError(f"{label} is required")
    return body[:limit]


def _duty(value: Mapping[str, Any], *, required: bool) -> dict[str, str]:
    """The three things a worker needs before it can start: what, done, hand back.

    `required` is the difference between reading an entry written before duties
    existed and recording a new plan. Old records stay readable; nothing new is
    planned without saying what the worker is actually for.
    """
    return {
        "task_text": _block(
            value.get("task_text"),
            limit=MAX_TASK_TEXT,
            label="worker task text",
            required=required,
        ),
        "acceptance_criteria": _block(
            value.get("acceptance_criteria"),
            limit=MAX_ACCEPTANCE,
            label="worker acceptance criteria",
            required=required,
        ),
        "deliverable": _block(
            value.get("deliverable"),
            limit=MAX_DELIVERABLE,
            label="worker deliverable",
            required=required,
        ),
    }


def _validated_duty_report(value: object, index: int) -> dict[str, Any]:
    """One entry in a worker's ordered duty history."""
    if not isinstance(value, Mapping):
        raise IntakeError("a duty report must be an object")
    sequence = value.get("seq")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != index:
        raise IntakeError("duty reports must be numbered in the order they arrived")
    state = value.get("state")
    if state not in DUTY_STATES:
        raise IntakeError(f"unknown duty report state: {state!r}")
    reporter = value.get("reporter_identity")
    if reporter is not None and reporter != "" and not valid_thread_key(reporter):
        raise IntakeError("a duty report reporter is not an exact thread key")
    attempt = value.get("duty_attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise IntakeError("a duty report attempt must be a positive integer")
    return {
        "seq": sequence,
        "state": state,
        "summary": _block(
            value.get("summary"), limit=MAX_DUTY_SUMMARY, label="duty report summary"
        ),
        "reporter_identity": str(reporter or ""),
        "duty_attempt": attempt,
        "receipt": _text(
            value.get("receipt"), limit=200, label="duty receipt", required=False
        ),
        "recorded_unix_ms": _stamp(value.get("recorded_unix_ms"), "recorded_unix_ms"),
    }


def _validated_duty_delivery(value: object) -> dict[str, Any] | None:
    """What became of the one message that carried the duty to the worker."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise IntakeError("a duty delivery record must be an object")
    state = value.get("state")
    if state not in DUTY_DELIVERY_STATES:
        raise IntakeError(f"unknown duty delivery state: {state!r}")
    relay_key = valid_idempotency_key(value.get("relay_key"))
    if not relay_key:
        raise IntakeError("a duty delivery needs its exact relay key")
    relay_msg_id = value.get("relay_msg_id")
    if relay_msg_id is not None and not valid_msg_id(relay_msg_id):
        raise IntakeError("a duty delivery relay message id is not exact")
    return {
        "state": state,
        "relay_key": relay_key,
        "relay_msg_id": relay_msg_id,
        "relay_status": _text(
            value.get("relay_status"),
            limit=200,
            label="duty delivery status",
            required=False,
        ),
        "relay_detail": _text(
            value.get("relay_detail"),
            limit=300,
            label="duty delivery detail",
            required=False,
        ),
        "delivered_unix_ms": _stamp(
            value.get("delivered_unix_ms"), "delivered_unix_ms"
        ),
    }


def branch_token(branch: object) -> str:
    """A branch name as an idempotency-key fragment.

    Branch names carry slashes; idempotency keys do not. The digest keeps one
    branch's duty key stable across restarts without inventing a second name
    for the branch itself.
    """
    return hashlib.sha256(str(branch).encode("utf-8")).hexdigest()[:16]


def duty_key(msg_id: str, branch: str, attempt: int) -> str:
    """The key the duty for one attempt at one branch is always sent under."""
    return f"{DUTY_KEY_PREFIX}:{msg_id}:{branch_token(branch)}:{int(attempt)}"


def _validated_receipt(value: object) -> dict[str, Any]:
    """One worker report as it is written to, and read back from, disk."""
    if not isinstance(value, Mapping):
        raise IntakeError("a worker receipt must be an object")
    msg_id = valid_msg_id(value.get("msg_id"))
    if not msg_id:
        raise IntakeError("a worker receipt needs its intake's exact message id")
    sequence = value.get("seq")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise IntakeError("a worker receipt needs a positive sequence number")
    state = value.get("state")
    if state not in DUTY_STATES:
        raise IntakeError(f"unknown worker receipt state: {state!r}")
    attempt = value.get("duty_attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise IntakeError("a worker receipt attempt must be a positive integer")
    reporter = value.get("reporter_identity")
    if reporter is not None and reporter != "" and not valid_thread_key(reporter):
        raise IntakeError("a worker receipt reporter is not an exact thread key")
    # The launch this duty was sent to. A retry mints a new key, so this is
    # what lines one report up with the run that produced it — without the
    # reader having to open the intake entry to translate an attempt number.
    launch_key = value.get("launch_key")
    if launch_key is not None and launch_key != "" and not valid_idempotency_key(
        launch_key
    ):
        raise IntakeError("a worker receipt launch key is not an idempotency key")
    return {
        "msg_id": msg_id,
        "launch_key": str(launch_key or ""),
        "branch": _valid_branch(value.get("branch")),
        "seq": sequence,
        "state": state,
        "summary": _block(
            value.get("summary"), limit=MAX_DUTY_SUMMARY, label="worker receipt summary"
        ),
        "reporter_identity": str(reporter or ""),
        "duty_attempt": attempt,
        "requirements_digest": _text(
            value.get("requirements_digest"),
            limit=64,
            label="worker receipt requirements digest",
            required=False,
        ),
        "recorded_unix_ms": _stamp(value.get("recorded_unix_ms"), "recorded_unix_ms"),
    }


def _validated_note(value: object, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise IntakeError("an intake note must be an object")
    kind = value.get("kind")
    if kind not in KINDS:
        raise IntakeError(f"unknown intake note kind: {kind!r}")
    via = value.get("via")
    if via not in {"reply", "send"}:
        raise IntakeError(f"unknown intake note channel: {via!r}")
    sequence = value.get("seq")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != index:
        raise IntakeError("intake notes must be numbered in the order they were made")
    relay_key = value.get("relay_key")
    if relay_key is not None and not valid_idempotency_key(relay_key):
        raise IntakeError("an intake note's relay key is not an idempotency key")
    relay_msg_id = value.get("relay_msg_id")
    if relay_msg_id is not None and not valid_msg_id(relay_msg_id):
        raise IntakeError("an intake note's relay message id is not exact")
    if not isinstance(value.get("landed"), bool):
        raise IntakeError("an intake note must say whether it landed")
    revision = value.get("requirements_revision")
    return {
        "seq": sequence,
        "kind": kind,
        "via": via,
        "text": _text(value.get("text"), limit=MAX_NOTE_TEXT, label="note text"),
        "recorded_unix_ms": _stamp(value.get("recorded_unix_ms"), "recorded_unix_ms"),
        "relayed_unix_ms": _stamp(value.get("relayed_unix_ms"), "relayed_unix_ms"),
        "relay_key": relay_key,
        "relay_msg_id": relay_msg_id,
        "relay_status": _text(
            value.get("relay_status"), limit=200, label="relay status", required=False
        ),
        "relay_detail": _text(
            value.get("relay_detail"), limit=300, label="relay detail", required=False
        ),
        "landed": bool(value["landed"]),
        "source_event_id": _source_event_id(value.get("source_event_id")),
        "requirements_digest": _text(
            value.get("requirements_digest") or ("0" * 64),
            limit=64,
            label="note requirements digest",
        ),
        "requirements_revision": (
            revision
            if isinstance(revision, int)
            and not isinstance(revision, bool)
            and revision >= 0
            else 0
        ),
        "stale_generation": bool(value.get("stale_generation", False)),
    }


def prompt_digest(text: object) -> str:
    """The stable SHA-256 digest used with a provider turn id for dedup."""
    body = text if isinstance(text, str) else ""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _validated_amendment(value: object, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise IntakeError("an intake amendment must be an object")
    sequence = value.get("seq")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != index:
        raise IntakeError("amendments must be numbered in the order they arrived")
    relay_key = value.get("relay_key")
    if not valid_idempotency_key(relay_key):
        raise IntakeError("an amendment's relay key is not an idempotency key")
    relay_msg_id = value.get("relay_msg_id")
    if relay_msg_id is not None and not valid_msg_id(relay_msg_id):
        raise IntakeError("an amendment's relay message id is not exact")
    if not isinstance(value.get("delivered"), bool):
        raise IntakeError("an amendment must say whether it was delivered")
    digest = _text(
        value.get("digest"), limit=64, label="prompt digest", required=False
    )
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise IntakeError("an amendment needs a lowercase SHA-256 prompt digest")
    return {
        "seq": sequence,
        "turn_id": _text(
            value.get("turn_id"), limit=200, label="turn id", required=False
        ),
        "digest": digest,
        "text": _text(value.get("text"), limit=MAX_SUMMARY, label="amendment text"),
        "recorded_unix_ms": _stamp(value.get("recorded_unix_ms"), "recorded_unix_ms"),
        "delivered_unix_ms": _stamp(value.get("delivered_unix_ms"), "delivered_unix_ms"),
        "relay_key": relay_key,
        "relay_msg_id": relay_msg_id,
        "relay_status": _text(
            value.get("relay_status"), limit=200, label="relay status", required=False
        ),
        "delivered": bool(value["delivered"]),
        "source_event_id": _source_event_id(
            value.get("source_event_id"), required=True
        ),
        "delivery_attempts": _attempts(value.get("delivery_attempts")),
        "last_attempt_unix_ms": _stamp(
            value.get("last_attempt_unix_ms"), "last_attempt_unix_ms"
        ),
        "next_attempt_unix_ms": _stamp(
            value.get("next_attempt_unix_ms"), "next_attempt_unix_ms"
        ),
    }


def _validated_arrival_notice(
    value: object, *, origin: str, msg_id: str
) -> dict[str, Any] | None:
    """The one machine notice that wakes a resident for a new auto intake.

    Message-origin intakes already arrived as messages. Automatic intakes did
    not. Older entries predate this contract and remain readable without
    generating an install-time wake storm; every new entry writes an explicit
    pending record. The messaging ledger's stable key and this landed flag
    together make retries safe.
    """
    if origin == MESSAGE_ORIGIN:
        if value is not None:
            raise IntakeError("a messaged intake cannot carry an arrival notice")
        return None
    if value is None:
        value = {
            "relay_key": f"{ARRIVAL_KEY_PREFIX}:{msg_id}",
            "relay_msg_id": None,
            "relay_status": "legacy entry without an arrival-wake contract",
            "delivered": True,
            "delivered_unix_ms": None,
        }
    if not isinstance(value, Mapping):
        raise IntakeError("an automatic intake arrival notice must be an object")
    expected_key = f"{ARRIVAL_KEY_PREFIX}:{msg_id}"
    if value.get("relay_key") != expected_key:
        raise IntakeError("an automatic intake arrival notice has the wrong key")
    relay_msg_id = value.get("relay_msg_id")
    if relay_msg_id is not None and not valid_msg_id(relay_msg_id):
        raise IntakeError("an arrival notice relay message id is not exact")
    if not isinstance(value.get("delivered"), bool):
        raise IntakeError("an arrival notice must say whether it was delivered")
    return {
        "relay_key": expected_key,
        "relay_msg_id": relay_msg_id,
        "relay_status": _text(
            value.get("relay_status"),
            limit=200,
            label="arrival notice status",
            required=False,
        ),
        "delivered": bool(value["delivered"]),
        "delivered_unix_ms": _stamp(
            value.get("delivered_unix_ms"), "delivered_unix_ms"
        ),
        "delivery_attempts": _attempts(value.get("delivery_attempts")),
        "last_attempt_unix_ms": _stamp(
            value.get("last_attempt_unix_ms"), "last_attempt_unix_ms"
        ),
        "next_attempt_unix_ms": _stamp(
            value.get("next_attempt_unix_ms"), "next_attempt_unix_ms"
        ),
    }


def _validated_worker(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise IntakeError("a recorded worker must be an object")
    result: dict[str, Any] = {
        "branch": _valid_branch(value.get("branch")),
        "recorded_unix_ms": _stamp(value.get("recorded_unix_ms"), "recorded_unix_ms"),
    }
    revision = value.get("preflight_revision")
    if revision is not None:
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
            raise IntakeError("a worker preflight revision must be positive")
        provider = value.get("provider")
        if provider not in ("claude", "codex"):
            raise IntakeError("a planned worker provider must be claude or codex")
        result.update(
            provider=provider,
            idempotency_key=valid_idempotency_key(value.get("idempotency_key")),
            workstream=_text(value.get("workstream"), limit=500, label="workstream"),
            scope=_text(value.get("scope"), limit=2000, label="worker scope"),
            requested_model=_model(provider, value.get("requested_model")),
            verified_actual_model=_text(
                value.get("verified_actual_model"),
                limit=200,
                label="verified actual model",
                required=False,
            ),
            expertise=_expertise(value.get("expertise")),
            rationale=_text(value.get("rationale"), limit=1000, label="worker rationale"),
            worker_identity=_text(
                value.get("worker_identity"), limit=300, label="worker identity", required=False
            ),
            launch_state=value.get("launch_state"),
            dispatch_unix_ms=_stamp(value.get("dispatch_unix_ms"), "dispatch_unix_ms"),
            reconciled_unix_ms=_stamp(
                value.get("reconciled_unix_ms"), "reconciled_unix_ms"
            ),
            launched_unix_ms=_stamp(value.get("launched_unix_ms"), "launched_unix_ms"),
            verified_unix_ms=_stamp(value.get("verified_unix_ms"), "verified_unix_ms"),
            preflight_revision=revision,
            intake_generation=value.get("intake_generation"),
            requirements_digest=_text(
                value.get("requirements_digest"),
                limit=64,
                label="worker requirements digest",
            ),
            authority_verifications=value.get("authority_verifications"),
            duty_state=value.get("duty_state") or "assigned",
            duty_attempt=value.get("duty_attempt", 1),
            duty_delivery=_validated_duty_delivery(value.get("duty_delivery")),
            duty_reports=[
                _validated_duty_report(row, index)
                for index, row in enumerate(value.get("duty_reports") or (), start=1)
            ],
            **_duty(value, required=False),
        )
        if not result["idempotency_key"]:
            raise IntakeError("a worker needs an exact idempotency key")
        if result["launch_state"] not in LAUNCH_STATES:
            raise IntakeError("a worker launch state is invalid")
        if result["duty_state"] not in DUTY_STATES:
            raise IntakeError("a worker duty state is invalid")
        if (
            not isinstance(result["duty_attempt"], int)
            or isinstance(result["duty_attempt"], bool)
            or result["duty_attempt"] < 1
        ):
            raise IntakeError("a worker duty attempt must be a positive integer")
        if len(result["duty_reports"]) > MAX_DUTY_REPORTS:
            raise IntakeError("a worker already records its duty report limit")
        if result["launch_state"] == "commissioned" and (
            not result["task_text"]
            or (result["duty_delivery"] or {}).get("state") != "delivered"
        ):
            raise IntakeError(
                "a commissioned worker needs its duty text and a landed delivery"
            )
        if result["launch_state"] in (
            "provider_reconciled", "verified", "commissioned"
        ) and (
            not result["worker_identity"]
            or not result["verified_actual_model"]
            or result["reconciled_unix_ms"] is None
        ):
            raise IntakeError("a reconciled worker needs exact identity and actual model")
        if result["launch_state"] != "not_started" and result["dispatch_unix_ms"] is None:
            raise IntakeError("a dispatched worker needs its dispatch timestamp")
        if result["launch_state"] == "verified" and result["verified_unix_ms"] is None:
            raise IntakeError("a verified worker needs its verification timestamp")
        worker_digest = result["requirements_digest"]
        if not isinstance(worker_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", worker_digest
        ):
            raise IntakeError("a worker needs its exact requirements digest")
        receipts = result["authority_verifications"]
        if not isinstance(receipts, list):
            raise IntakeError("a worker needs its authority verification receipts")
        for receipt in receipts:
            if (
                not isinstance(receipt, Mapping)
                or not SOURCE_EVENT_RE.fullmatch(str(receipt.get("event_id") or ""))
                or receipt.get("basis") not in ("transcript", "hook-ledger")
            ):
                raise IntakeError("a worker authority verification receipt is malformed")
        if (
            not isinstance(result["intake_generation"], int)
            or isinstance(result["intake_generation"], bool)
            or result["intake_generation"] < 0
        ):
            raise IntakeError("a worker intake generation must be non-negative")
    return result


def _validated_worker_plan(value: object, *, require_duty: bool = False) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise IntakeError("a preflight worker plan row must be an object")
    provider = value.get("provider")
    if provider not in ("claude", "codex"):
        raise IntakeError("a planned worker provider must be claude or codex")
    key = valid_idempotency_key(value.get("idempotency_key"))
    if not key:
        raise IntakeError("a planned worker needs an idempotency key")
    return {
        "branch": _valid_branch(value.get("branch")),
        "idempotency_key": key,
        "workstream": _text(value.get("workstream"), limit=500, label="workstream"),
        "scope": _text(value.get("scope"), limit=2000, label="worker scope"),
        "provider": provider,
        "requested_model": _model(provider, value.get("requested_model")),
        "expertise": _expertise(value.get("expertise")),
        "rationale": _text(value.get("rationale"), limit=1000, label="worker rationale"),
        **_duty(value, required=require_duty),
    }


def _required_expertise_tags(value: object, *, required: bool) -> list[str]:
    """The expertise this project needs, named from the fixed tag set.

    This is the plan's stated need, and the only composition rule left: a plan
    is whatever shape covers it. One worker for a one-skill project is a
    complete plan; four workers that between them miss a declared need is not.
    """
    if value is None:
        if required:
            raise IntakeError("a preflight needs the expertise this project requires")
        return []
    if not isinstance(value, (list, tuple)):
        raise IntakeError("required expertise tags must be an array")
    tags = [_expertise(item) for item in value]
    if len(set(tags)) != len(tags):
        raise IntakeError("required expertise tags cannot repeat")
    if required and not tags:
        raise IntakeError("a preflight needs the expertise this project requires")
    return tags


def _uncovered_expertise(
    plan: Sequence[Mapping[str, Any]], tags: Sequence[str]
) -> list[str]:
    """Declared needs no planned worker brings, in the order they were declared."""
    covered = {str(row.get("expertise")) for row in plan}
    return [tag for tag in tags if tag not in covered]


def _validated_preflight(value: object, revision: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise IntakeError("an intake preflight must be an object")
    if value.get("revision") != revision:
        raise IntakeError("preflights must be revisioned in append order")
    generation = value.get("intake_generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise IntakeError("a preflight intake generation must be non-negative")
    if value.get("requirements_revision") != generation:
        raise IntakeError("preflight requirements revision does not match its generation")
    source_event = _source_event_id(value.get("source_event_id"))
    requirements_digest = _text(
        value.get("requirements_digest"), limit=64, label="requirements digest"
    )
    if not re.fullmatch(r"[0-9a-f]{64}", requirements_digest):
        raise IntakeError("a preflight needs an exact requirements digest")
    supervisor = valid_thread_key(value.get("supervisor_thread_key"))
    if not supervisor:
        raise IntakeError("a preflight needs the exact supervisor identity")
    raw_plan = value.get("worker_plan")
    if not isinstance(raw_plan, list) or not raw_plan or len(raw_plan) > MAX_WORKERS:
        raise IntakeError("a preflight needs 1-32 planned workers")
    plan = [_validated_worker_plan(row) for row in raw_plan]
    if len({row["branch"] for row in plan}) != len(plan):
        raise IntakeError("a preflight worker plan cannot repeat a branch")
    if len({row["idempotency_key"] for row in plan}) != len(plan):
        raise IntakeError("a preflight worker plan cannot repeat an idempotency key")
    basis = value.get("verification_basis")
    if source_event is not None and basis not in ("transcript", "hook-ledger"):
        raise IntakeError("a source preflight needs a verified source-event basis")
    if source_event is None and basis != "manual-intake":
        raise IntakeError("a manual intake preflight needs manual-intake basis")
    exception = _text(
        value.get("manual_policy_exception"),
        limit=1000,
        label="manual policy exception",
        required=False,
    )
    # An entry written before needs were declared carried the old fixed-shape
    # rule instead, and passed it. It stays readable; nothing new is recorded
    # without saying what the project needs.
    required_tags = _required_expertise_tags(
        value.get("required_expertise_tags"), required=False
    )
    missing = _uncovered_expertise(plan, required_tags)
    if missing:
        raise IntakeError(
            "the worker plan does not cover the required expertise: "
            + ", ".join(missing)
        )
    return {
        "revision": revision,
        "intake_generation": generation,
        "source_event_id": source_event,
        "requirements_revision": generation,
        "requirements_digest": requirements_digest,
        "supervisor_thread_key": supervisor,
        "analysis": _text(value.get("analysis"), limit=4000, label="project analysis"),
        "scope": _text(value.get("scope"), limit=4000, label="project scope"),
        "required_expertise": _text(
            value.get("required_expertise"), limit=2000, label="required expertise"
        ),
        "required_expertise_tags": required_tags,
        "worker_plan": plan,
        "risks": _text(value.get("risks"), limit=4000, label="project risks"),
        "tests": _text(value.get("tests"), limit=4000, label="required tests"),
        "verification_basis": basis,
        "manual_policy_exception": exception,
        "recorded_unix_ms": _stamp(
            value.get("recorded_unix_ms"), "recorded_unix_ms"
        ),
    }


def validated_entry(value: object) -> dict[str, Any]:
    """The exact stored shape of one intake, or a refusal.

    Nothing is written or handed back without passing here, so a hand-edited
    entry fails loudly at the next verb instead of quietly changing what the
    machine believes about a live project.
    """
    if not isinstance(value, Mapping):
        raise IntakeError("an intake entry must be an object")
    msg_id = valid_msg_id(value.get("msg_id"))
    if not msg_id:
        raise IntakeError("an intake entry needs an exact 8-hex message id")
    source = valid_thread_key(value.get("source_thread_key"))
    if not source:
        raise IntakeError("an intake entry needs a source thread key")
    state = value.get("state")
    if state not in STATES:
        raise IntakeError(f"unknown intake state: {state!r}")
    # An entry written before there was a second path is a message intake:
    # that was the only way one could arrive.
    origin = value.get("origin") or MESSAGE_ORIGIN
    if origin not in ORIGINS:
        raise IntakeError(f"unknown intake origin: {origin!r}")
    source_event = _source_event_id(
        value.get("source_event_id"), required=origin == AUTO_ORIGIN
    )
    source_digest = _text(
        value.get("source_digest"),
        limit=64,
        label="source prompt digest",
        required=origin == AUTO_ORIGIN,
    )
    if source_digest and not re.fullmatch(r"[0-9a-f]{64}", source_digest):
        raise IntakeError("an intake needs a lowercase SHA-256 source prompt digest")
    intake_key = value.get("intake_key")
    if intake_key is not None and not valid_idempotency_key(intake_key):
        raise IntakeError("an intake key is not an idempotency key")
    raw_aliases = value.get("also_delivered_as")
    if not isinstance(raw_aliases, Sequence) or isinstance(raw_aliases, (str, bytes)):
        raise IntakeError("also_delivered_as must be a list of message ids")
    aliases: list[str] = []
    for candidate in raw_aliases[:MAX_ALIASES]:
        exact = valid_msg_id(candidate)
        if not exact:
            raise IntakeError("also_delivered_as must be a list of message ids")
        if exact not in aliases and exact != msg_id:
            aliases.append(exact)
    raw_workers = value.get("workers")
    raw_notes = value.get("notes")
    raw_amendments = value.get("amendments") or []
    raw_preflights = value.get("preflights") or []
    if (
        not isinstance(raw_workers, list)
        or not isinstance(raw_notes, list)
        or not isinstance(raw_amendments, list)
        or not isinstance(raw_preflights, list)
    ):
        raise IntakeError("an intake entry needs its worker, note, and amendment lists")
    if (
        len(raw_workers) > MAX_WORKERS
        or len(raw_notes) > MAX_NOTES
        or len(raw_amendments) > MAX_AMENDMENTS
        or len(raw_preflights) > MAX_PREFLIGHTS
    ):
        raise IntakeError("an intake entry exceeds one of its list bounds")
    follows = value.get("follows")
    if follows is not None and not valid_msg_id(follows):
        raise IntakeError("an intake can only follow an exact message id")
    received = _stamp(value.get("received_unix_ms"), "received_unix_ms")
    if received is None:
        raise IntakeError("an intake entry needs the time it arrived")
    return {
        "msg_id": msg_id,
        "origin": origin,
        "follows": valid_msg_id(follows) or None,
        "intake_key": intake_key,
        "also_delivered_as": aliases,
        "source_thread_key": source,
        "source_turn_id": _text(
            value.get("source_turn_id"), limit=200, label="turn id", required=False
        ),
        "source_digest": source_digest,
        "source_event_id": source_event,
        "source_terminal": _terminal_number(value.get("source_terminal")),
        "source_title": _text(
            value.get("source_title"), limit=MAX_TITLE, label="source title", required=False
        ),
        "source_cwd": _text(
            value.get("source_cwd"), limit=MAX_CWD, label="source cwd", required=False
        ),
        "summary": _text(
            value.get("summary"), limit=MAX_SUMMARY, label="summary", required=False
        ),
        "state": state,
        "received_unix_ms": received,
        "updated_unix_ms": _stamp(value.get("updated_unix_ms"), "updated_unix_ms")
        or received,
        "acknowledged_unix_ms": _stamp(
            value.get("acknowledged_unix_ms"), "acknowledged_unix_ms"
        ),
        "delegated_unix_ms": _stamp(value.get("delegated_unix_ms"), "delegated_unix_ms"),
        "reported_unix_ms": _stamp(value.get("reported_unix_ms"), "reported_unix_ms"),
        "arrival_notice": _validated_arrival_notice(
            value.get("arrival_notice"), origin=origin, msg_id=msg_id
        ),
        "workers": [_validated_worker(worker) for worker in raw_workers],
        "notes": [
            _validated_note(note, index) for index, note in enumerate(raw_notes, 1)
        ],
        "amendments": [
            _validated_amendment(item, index)
            for index, item in enumerate(raw_amendments, 1)
        ],
        "preflights": [
            _validated_preflight(item, index)
            for index, item in enumerate(raw_preflights, 1)
        ],
    }


# ---- the store ----------------------------------------------------------


class Spool:
    """Every read and write of the supervisor's intake spool."""

    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)
        self.root = self.state_dir / "supervisor" / "intake"
        self.entries = self.root / "entries"
        self.keys = self.root / "keys"
        self.aliases = self.root / "aliases"
        self.receipts = self.root / "receipts"
        self.lock_path = self.root / "intake.lock"

    # ---- structure -----------------------------------------------------

    def ensure(self) -> None:
        _private_directory(self.root.parent)
        _private_directory(self.root)
        for directory in (self.entries, self.keys, self.aliases, self.receipts):
            _private_directory(directory)

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the spool's exclusive lock for one mutation.

        A relay can take minutes while the headless sender runs, so the lock
        covers reserving a note and settling it — never the delivery between
        them, which would park every other verb behind one send.
        """
        self.ensure()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    # ---- entries -------------------------------------------------------

    def entry_path(self, msg_id: object) -> Path:
        exact = valid_msg_id(msg_id)
        if not exact:
            raise IntakeError("an intake is named by an exact 8-hex message id")
        return self.entries / f"{exact}.json"

    def read_entry(self, msg_id: object) -> dict[str, Any] | None:
        raw = _read_private_bytes(self.entry_path(msg_id), MAX_ENTRY_BYTES)
        if raw is None:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise IntakeError(f"intake entry is not valid JSON: {msg_id}") from exc
        return validated_entry(value)

    def write_entry(self, record: Mapping[str, Any]) -> dict[str, Any]:
        checked = validated_entry(record)
        self.ensure()
        payload = (json.dumps(checked, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(payload) > MAX_ENTRY_BYTES:
            raise IntakeError("intake entry exceeds the spool's size limit")
        _atomic_private_write(self.entry_path(checked["msg_id"]), payload)
        return checked

    def update_entry(
        self, msg_id: str, mutate: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        """Read-modify-write one entry under the spool lock."""
        with self.locked():
            record = self.read_entry(msg_id)
            if record is None:
                raise IntakeError(f"no intake {msg_id}")
            return self.write_entry(mutate(record))

    def entry_ids(self) -> list[str]:
        try:
            names = sorted(
                entry.name
                for entry in os.scandir(self.entries)
                if entry.is_file(follow_symlinks=False) and entry.name.endswith(".json")
            )
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise IntakeError("cannot list the intake spool") from exc
        found = [
            msg_id
            for name in names[:MAX_ENTRIES_SCANNED]
            if (msg_id := valid_msg_id(name[: -len(".json")]))
        ]
        return found

    # ---- pointers ------------------------------------------------------

    def key_path(self, key: object) -> Path:
        exact = valid_idempotency_key(key)
        if not exact:
            raise IntakeError("an intake key must be an idempotency key")
        return self.keys / exact

    def alias_path(self, msg_id: object) -> Path:
        exact = valid_msg_id(msg_id)
        if not exact:
            raise IntakeError("an intake alias is named by an exact message id")
        return self.aliases / exact

    def _read_pointer(self, path: Path) -> str:
        raw = _read_private_bytes(path, MAX_POINTER_BYTES)
        if raw is None:
            return ""
        return valid_msg_id(raw.decode("ascii", "ignore").strip())

    def _write_pointer(self, path: Path, msg_id: str) -> None:
        exact = valid_msg_id(msg_id)
        if not exact:
            raise IntakeError("an intake pointer needs an exact message id")
        self.ensure()
        _atomic_private_write(path, (exact + "\n").encode("ascii"))

    def msg_id_for_key(self, key: object) -> str:
        """The intake this repeatable purpose already has, or "".

        A pointer whose entry is gone is not a pointer: the entry IS the
        intake, and resuming an id nothing reads back would leave a repeat with
        no lifecycle to join.
        """
        pointer = self._read_pointer(self.key_path(key))
        return pointer if pointer and self.read_entry(pointer) else ""

    def claim_key(self, key: object, msg_id: str) -> None:
        self._write_pointer(self.key_path(key), msg_id)

    def msg_id_for_alias(self, msg_id: object) -> str:
        pointer = self._read_pointer(self.alias_path(msg_id))
        return pointer if pointer and self.read_entry(pointer) else ""

    def claim_alias(self, msg_id: object, primary: str) -> None:
        self._write_pointer(self.alias_path(msg_id), primary)

    def resolve(self, msg_id: object) -> str:
        """The entry id behind any id this intake was ever delivered under."""
        exact = valid_msg_id(msg_id)
        if not exact:
            raise IntakeError("an intake is named by an exact 8-hex message id")
        if self.read_entry(exact) is not None:
            return exact
        return self.msg_id_for_alias(exact)

    # ---- worker receipts -------------------------------------------------

    def receipt_dir(self, msg_id: object) -> Path:
        exact = valid_msg_id(msg_id)
        if not exact:
            raise IntakeError("a receipt belongs to an exact 8-hex message id")
        return self.receipts / exact

    def receipt_path(self, msg_id: object, branch: str, sequence: int) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", _valid_branch(branch))[:64]
        return self.receipt_dir(msg_id) / f"{safe}.{branch_token(branch)}.{sequence}.json"

    def write_receipt(self, receipt: Mapping[str, Any]) -> Path:
        """Put one worker's report on disk before anything else can fail.

        The entry is the supervisor's record; this is the worker's. They are
        written in that order on purpose: a receipt with no matching report in
        the entry is a visible fault, and a report the worker never made is
        impossible.
        """
        checked = _validated_receipt(receipt)
        self.ensure()
        _private_directory(self.receipt_dir(checked["msg_id"]))
        path = self.receipt_path(
            checked["msg_id"], checked["branch"], checked["seq"]
        )
        payload = (json.dumps(checked, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_private_write(path, payload)
        return path

    def read_receipts(self, msg_id: object) -> list[dict[str, Any]]:
        """Every report one intake's workers wrote down, oldest first."""
        directory = self.receipt_dir(msg_id)
        try:
            names = sorted(
                item.name
                for item in os.scandir(directory)
                if item.is_file(follow_symlinks=False) and item.name.endswith(".json")
            )
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise IntakeError("cannot list this intake's worker receipts") from exc
        rows: list[dict[str, Any]] = []
        for name in names[:MAX_ENTRIES_SCANNED]:
            raw = _read_private_bytes(directory / name, MAX_RECEIPT_BYTES)
            if raw is None:
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise IntakeError(f"worker receipt is not valid JSON: {name}") from exc
            checked = _validated_receipt(value)
            # The file name is how a report in the entry points back here, so
            # the reader keeps it alongside the record it names.
            checked["file"] = name
            rows.append(checked)
        rows.sort(key=lambda row: (row["recorded_unix_ms"] or 0, row["branch"], row["seq"]))
        return rows

    # ---- views and retention -------------------------------------------

    def open_entries(self, limit: int = MAX_OPEN) -> list[dict[str, Any]]:
        """Every unfinished intake, the one waiting longest first."""
        rows = [
            entry
            for msg_id in self.entry_ids()
            if (entry := self.read_entry(msg_id)) is not None
            and entry["state"] != "reported"
        ]
        rows.sort(key=lambda entry: (entry["received_unix_ms"], entry["msg_id"]))
        return rows[: max(0, limit)]

    def prune(self, now_ms: int) -> dict[str, list[str]]:
        """Drop reported intakes past retention, and pointers to nothing.

        The caller already holds the lock. An unfinished intake is never
        pruned, however old: age is not a report to its source.
        """
        dropped: list[str] = []
        reported: list[tuple[int, str]] = []
        for msg_id in self.entry_ids():
            entry = self.read_entry(msg_id)
            if entry is None or entry["state"] != "reported":
                continue
            reported.append((int(entry["updated_unix_ms"]), msg_id))
        reported.sort(reverse=True)
        for index, (updated, msg_id) in enumerate(reported):
            if index < MAX_ENTRIES_KEPT and now_ms - updated <= RETENTION_MS:
                continue
            with contextlib.suppress(OSError):
                self.entry_path(msg_id).unlink()
                dropped.append(msg_id)
            directory = self.receipt_dir(msg_id)
            with contextlib.suppress(OSError):
                for item in os.scandir(directory):
                    if item.is_file(follow_symlinks=False):
                        with contextlib.suppress(OSError):
                            Path(item.path).unlink()
                directory.rmdir()
        return {"entries": dropped, "pointers": self._prune_pointers()}

    def _prune_pointers(self) -> list[str]:
        dropped: list[str] = []
        for directory, resolver in (
            (self.keys, self.msg_id_for_key),
            (self.aliases, self.msg_id_for_alias),
        ):
            try:
                names = [
                    entry.name
                    for entry in os.scandir(directory)
                    if entry.is_file(follow_symlinks=False)
                ]
            except OSError:
                continue
            for name in sorted(names):
                try:
                    if resolver(name):
                        continue
                except IntakeError:
                    # A pointer that cannot be read is not a pointer that may
                    # be dropped: uncertainty is not permission to lose state.
                    continue
                with contextlib.suppress(OSError):
                    (directory / name).unlink()
                    dropped.append(name)
        return dropped


# ---- lifecycle ----------------------------------------------------------


def _advance(entry: dict[str, Any], state: str, now: int) -> dict[str, Any]:
    """Move one intake forward, never back, stamping the transition once."""
    if _RANK[state] > _RANK[str(entry["state"])]:
        entry["state"] = state
    stamp = f"{state}_unix_ms"
    if entry.get(stamp) is None:
        entry[stamp] = now
    entry["updated_unix_ms"] = now
    return entry


def _require(spool: Spool, msg_id: object) -> str:
    primary = spool.resolve(msg_id)
    if not primary:
        raise IntakeError(
            f"no intake was recorded for message {valid_msg_id(msg_id) or msg_id}"
        )
    return primary


def _existing_note(
    entry: Mapping[str, Any], kind: str, text: str, generation_digest: str
) -> dict[str, Any] | None:
    """The note this one already is.

    Same words, same kind, same intake is the same note — the rule the message
    ledger applies to a repeated send. Different words are something new to
    say and get their own place in the record.
    """
    for note in entry.get("notes", ()):
        if (
            note.get("kind") == kind
            and note.get("text") == text
            and note.get("requirements_digest") == generation_digest
        ):
            return dict(note)
    return None


def relay_text(
    kind: str, msg_id: str, text: str, source_event_id: str | None = None
) -> str:
    """What the source thread actually receives.

    The envelope around it is the operator's, so the note names its author:
    the supervisor speaks for itself and never as the operator.
    """
    lead = "completion report" if kind == COMPLETION else "progress note"
    authority = source_event_id or "none (manual intake)"
    return (
        f"Fleet Supervisor {lead} on intake {msg_id} "
        f"(source_event_id {authority}): {text}"
    )


class Delivery(NamedTuple):
    """One relay's fate, as the send receipt reported it."""

    status: str
    detail: str
    landed: bool
    msg_id: str | None


def _delivery_outcome(payload: Mapping[str, Any] | None, thread_key: str) -> Delivery:
    """Read one relay's fate from the receipt the messaging core wrote.

    The source thread's own row decides. A receipt that never mentions it is
    not a delivery, whatever else the send did.
    """
    rows = payload.get("targets") if isinstance(payload, Mapping) else None
    row = None
    if isinstance(rows, list):
        row = next(
            (
                candidate
                for candidate in rows
                if isinstance(candidate, Mapping)
                and candidate.get("thread_key") == thread_key
            ),
            None,
        )
    if row is None:
        return Delivery("unknown", "the receipt has no row for the source thread", False, None)
    status = str(row.get("status") or "unknown")
    detail = _text(row.get("detail"), limit=300, label="relay detail", required=False)
    relay_msg_id = valid_msg_id(payload.get("msg_id")) if payload else ""
    return Delivery(status, detail, landed(status), relay_msg_id or None)


# ---- verbs --------------------------------------------------------------


def record(
    spool: Spool,
    *,
    msg_id: str,
    source: str,
    intake_key: str | None = None,
    summary: str | None = None,
    terminal: int | None = None,
    title: str | None = None,
    cwd: str | None = None,
    origin: str = MESSAGE_ORIGIN,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Write the arrival of one intake, or recognise one already recorded.

    Recognition runs before creation on two keys: the message id this delivery
    carries, and the purpose key the sender repeats under. Either one matching
    means the project is already in hand, and the caller is told so rather than
    handed a second entry to delegate from.
    """
    now = now_unix_ms(clock)
    exact = valid_msg_id(msg_id)
    if not exact:
        raise IntakeError("an intake is named by an exact 8-hex message id")
    thread = valid_thread_key(source)
    if not thread:
        raise IntakeError("an intake needs its source thread key (<claude|codex>:<uuid>)")
    key = ""
    if intake_key:
        key = valid_idempotency_key(intake_key)
        if not key:
            raise IntakeError("an intake key must be an idempotency key")
    with spool.locked():
        primary = spool.resolve(exact)
        if primary:
            return _duplicate(spool.read_entry(primary), primary)
        claimed = spool.msg_id_for_key(key) if key else ""
        if claimed:
            # The same project, sent again under a new message id. The second
            # id becomes an alias so a reply to either one reaches this entry.
            stored = spool.read_entry(claimed)
            if stored is None:
                raise IntakeError(f"intake {claimed} is claimed but its entry is gone")
            stored["also_delivered_as"] = (stored["also_delivered_as"] + [exact])[
                :MAX_ALIASES
            ]
            stored["updated_unix_ms"] = now
            entry = spool.write_entry(stored)
            spool.claim_alias(exact, claimed)
            return _duplicate(entry, claimed)
        entry = spool.write_entry(
            {
                "msg_id": exact,
                "origin": origin,
                "follows": None,
                "intake_key": key or None,
                "also_delivered_as": [],
                "source_thread_key": thread,
                "source_turn_id": "",
                "source_digest": "",
                "source_event_id": None,
                "source_terminal": _terminal_number(terminal),
                "source_title": _text(
                    title, limit=MAX_TITLE, label="source title", required=False
                ),
                "source_cwd": _text(
                    cwd, limit=MAX_CWD, label="source cwd", required=False
                ),
                "summary": _text(
                    summary, limit=MAX_SUMMARY, label="summary", required=False
                ),
                "state": "received",
                "received_unix_ms": now,
                "updated_unix_ms": now,
                "acknowledged_unix_ms": None,
                "delegated_unix_ms": None,
                "reported_unix_ms": None,
                "arrival_notice": None,
                "workers": [],
                "notes": [],
                "amendments": [],
                "preflights": [],
            }
        )
        if key:
            spool.claim_key(key, exact)
        spool.prune(now)
        return {
            "recorded": True,
            "duplicate": False,
            "duplicate_of": None,
            "entry": entry,
        }


def _duplicate(entry: dict[str, Any] | None, primary: str) -> dict[str, Any]:
    if entry is None:
        raise IntakeError(f"intake {primary} is claimed but its entry is unreadable")
    return {
        "recorded": False,
        "duplicate": True,
        "duplicate_of": primary,
        "entry": entry,
    }


def acknowledge(
    spool: Spool,
    *,
    msg_id: str,
    text: str,
    reply: Callable[..., Mapping[str, Any]],
    deliver: Callable[..., Mapping[str, Any]],
    clock: Callable[[], float] = time.time,
) -> tuple[int, dict[str, Any]]:
    """Answer the intake wherever it came from, and record that it was answered.

    A message intake is answered on its own thread, by replying to the message
    that carried it. An intake the machine took from a root session's first
    prompt was never sent by anyone, so there is nothing to reply to: its
    acknowledgement goes to the source thread the way a progress note does.
    Either way the delivery is the messaging core's, and this verb owns only
    the record.

    A second acknowledgement would answer one question twice, so the recorded
    one is returned instead.
    """
    now = now_unix_ms(clock)
    named = valid_msg_id(msg_id)
    if not named:
        raise IntakeError("an intake is named by an exact 8-hex message id")
    primary = _require(spool, named)
    body = _text(text, limit=MAX_NOTE_TEXT, label="acknowledgement text")
    entry = spool.read_entry(primary)
    assert entry is not None
    existing = _existing_note(entry, ACKNOWLEDGEMENT, body, requirements_digest(entry))
    if entry["acknowledged_unix_ms"] is not None or existing is not None:
        return 0, {
            "acknowledged": False,
            "duplicate": True,
            "note": existing,
            "entry": entry,
        }
    if entry["origin"] == AUTO_ORIGIN:
        code, relayed = relay(
            spool,
            msg_id=named,
            text=body,
            kind=ACKNOWLEDGEMENT,
            deliver=deliver,
            clock=clock,
        )
        return code, {
            "acknowledged": bool(relayed["relayed"]),
            "duplicate": bool(relayed["duplicate"]),
            "note": relayed["note"],
            "entry": relayed["entry"],
        }
    outcome = reply(msg_id=named, text=body)
    if not outcome.get("ok"):
        # The reply was kept as unsolicited text, not delivered as an answer.
        # Leaving the intake unacknowledged is the truthful record: whoever
        # reads the spool next still owes this source an answer.
        return 1, {
            "acknowledged": False,
            "duplicate": False,
            "reason": _text(
                outcome.get("reason"), limit=200, label="reason", required=False
            )
            or "the reply was not recorded against that message",
            "entry": entry,
        }

    source_event_id = entry.get("source_event_id")

    def acknowledged(record: dict[str, Any]) -> dict[str, Any]:
        record["notes"] = record["notes"] + [
            {
                "seq": len(record["notes"]) + 1,
                "kind": ACKNOWLEDGEMENT,
                "via": "reply",
                "text": body,
                "recorded_unix_ms": now,
                "relayed_unix_ms": now,
                "relay_key": None,
                "relay_msg_id": named,
                "relay_status": "replied",
                "relay_detail": "recorded against the intake's own message",
                "landed": True,
                "source_event_id": source_event_id,
                "requirements_digest": requirements_digest(record),
                "requirements_revision": len(record["amendments"]),
                "stale_generation": False,
            }
        ]
        return _advance(record, "acknowledged", now)

    entry = spool.update_entry(primary, acknowledged)
    return 0, {
        "acknowledged": True,
        "duplicate": False,
        "note": entry["notes"][-1],
        "entry": entry,
    }


def _current_source_event(entry: Mapping[str, Any]) -> str | None:
    if entry.get("amendments"):
        return str(entry["amendments"][-1]["source_event_id"])
    value = entry.get("source_event_id")
    return str(value) if value else None


def requirements_digest(entry: Mapping[str, Any]) -> str:
    """Digest the initial authority event and every ordered amendment event."""
    events = [str(entry.get("source_event_id") or "manual-intake")]
    events.extend(str(row["source_event_id"]) for row in entry.get("amendments", ()))
    return hashlib.sha256("\0".join(events).encode("ascii")).hexdigest()


def preflight(
    spool: Spool,
    *,
    msg_id: str,
    source_event_id: str | None,
    analysis: str,
    scope: str,
    required_expertise: str,
    required_expertise_tags: Sequence[str] = (),
    worker_plan: Sequence[Mapping[str, Any]],
    risks: str,
    tests: str,
    manual_policy_exception: str = "",
    state_dir: Path | str,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Record the supervisor's reviewed plan for the current intake generation.

    The plan's shape is the plan's business: any composition that covers the
    declared required expertise is a complete plan. What is not optional is
    the duty — every planned worker says what it is for, what finishing means,
    and what it hands back, because that text is what the worker is sent.
    """
    from .source_authority import verify_source_event

    primary = _require(spool, msg_id)
    entry = spool.read_entry(primary)
    assert entry is not None
    if entry["state"] == "reported":
        raise IntakeError("a reported intake cannot receive a new preflight")
    generation = len(entry["amendments"])
    revision_digest = requirements_digest(entry)
    current_event = _current_source_event(entry)
    requested_event = _source_event_id(source_event_id)
    if requested_event != current_event:
        raise IntakeError("preflight source_event_id is not the current intake generation")
    supervisor = supervisor_identity(state_dir)
    if not supervisor:
        raise IntakeError("preflight needs the live exact supervisor identity")
    if current_event:
        verification = verify_source_event(state_dir, current_event, clock=clock)
        if not verification.get("verified"):
            raise IntakeError(
                "preflight source event did not verify: "
                + str(verification.get("reason") or "unknown failure")
            )
        basis = str(verification["basis"])
    else:
        basis = "manual-intake"
    checked_plan = [
        _validated_worker_plan(row, require_duty=True) for row in worker_plan
    ]
    if not checked_plan:
        raise IntakeError("preflight needs at least one planned worker")
    if len(checked_plan) > MAX_WORKERS:
        raise IntakeError(f"a preflight plans at most {MAX_WORKERS} workers")
    if len({row["branch"] for row in checked_plan}) != len(checked_plan):
        raise IntakeError("a preflight worker plan cannot repeat a branch")
    if len({row["idempotency_key"] for row in checked_plan}) != len(checked_plan):
        raise IntakeError("a preflight worker plan cannot repeat an idempotency key")
    for row in checked_plan:
        # The messaging core truncates an over-long operator message instead of
        # refusing it, so a duty that cannot travel whole is refused here,
        # while it is still a plan and not a worker waiting on half a brief.
        if len(duty_text(primary, row)) + DUTY_IDENTITY_RESERVE > MAX_OPERATOR_TEXT:
            raise IntakeError(
                f"worker {row['branch']} duty is longer than one message can carry"
            )
    tags = _required_expertise_tags(list(required_expertise_tags), required=True)
    missing = _uncovered_expertise(checked_plan, tags)
    if missing:
        raise IntakeError(
            "the worker plan does not cover the required expertise: "
            + ", ".join(missing)
        )
    now = now_unix_ms(clock)

    def append(record: dict[str, Any]) -> dict[str, Any]:
        if len(record["amendments"]) != generation:
            raise IntakeError("the intake changed while its preflight was reviewed")
        if _current_source_event(record) != current_event:
            raise IntakeError("the source event changed while its preflight was reviewed")
        if requirements_digest(record) != revision_digest:
            raise IntakeError("the ordered requirements changed during preflight")
        if len(record["preflights"]) >= MAX_PREFLIGHTS:
            raise IntakeError(f"intake {primary} already records {MAX_PREFLIGHTS} preflights")
        revision = len(record["preflights"]) + 1
        record["preflights"].append(
            {
                "revision": revision,
                "intake_generation": generation,
                "requirements_revision": generation,
                "requirements_digest": revision_digest,
                "source_event_id": current_event,
                "supervisor_thread_key": supervisor,
                "analysis": analysis,
                "scope": scope,
                "required_expertise": required_expertise,
                "required_expertise_tags": tags,
                "worker_plan": checked_plan,
                "risks": risks,
                "tests": tests,
                "verification_basis": basis,
                "manual_policy_exception": manual_policy_exception,
                "recorded_unix_ms": now,
            }
        )
        record["updated_unix_ms"] = now
        return record

    updated = spool.update_entry(primary, append)
    return {"recorded": True, "preflight": updated["preflights"][-1], "entry": updated}


def duty_text(msg_id: str, worker: Mapping[str, Any]) -> str:
    """The message a worker actually receives when it is commissioned.

    It carries the work, what finishing means, what to hand back, and the one
    command that closes the loop — a worker that never learns how to report is
    the finished-but-silent worker this whole channel exists to prevent.
    """
    branch = str(worker["branch"])
    # Naming its own key in the command is how the worker's report ends up
    # attributed to it: nothing here proves who ran the verb, but a report that
    # says which session made it is the difference between a record and a rumour.
    identity = valid_thread_key(worker.get("worker_identity"))
    reporter = f" --reporter {identity}" if identity else ""
    lines = [
        f"Fleet Supervisor duty for intake {msg_id}, branch {branch}.",
        f"Workstream: {worker.get('workstream') or 'unstated'}",
        "",
        "TASK",
        str(worker.get("task_text") or ""),
        "",
        "ACCEPTANCE CRITERIA",
        str(worker.get("acceptance_criteria") or ""),
        "",
        "DELIVERABLE",
        str(worker.get("deliverable") or ""),
        "",
        "REPORT WHEN DONE (or when you cannot finish):",
        f"  sp msg intake report --msg-id {msg_id} --branch {branch} "
        f"--state completed|failed --summary '<what happened>'{reporter}",
        "A duty with no report reads as an abandoned worker, whatever you did.",
    ]
    return "\n".join(lines)


def duty_owed(worker: Mapping[str, Any]) -> bool:
    """True when this worker has a duty its session has not been sent."""
    if not worker.get("task_text"):
        return False
    delivery = worker.get("duty_delivery") or {}
    return delivery.get("state") != "delivered"


def delegate(
    spool: Spool,
    *,
    msg_id: str,
    branches: Sequence[str],
    workers: Sequence[Mapping[str, Any]] = (),
    launcher: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    reconciler: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    deliver: Callable[..., Mapping[str, Any]] | None = None,
    state_dir: Path | str | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Reverify, reserve, dispatch, reconcile, verify, and commission workers.

    A verified worker is a proven session, not yet a worker: until its duty
    has actually landed in it, it is an idle process holding a branch name.
    Commissioning is that last step, and it is recorded like every other
    delivery here — reserved on disk under the key the send will use, settled
    with what the receipt said. A duty that did not land leaves the worker
    verified and named in `undelivered`, never quietly counted as launched.
    """
    from .source_authority import verify_source_event

    primary = _require(spool, msg_id)
    requested: list[dict[str, Any]] = []
    for branch in branches:
        requested.append({"branch": _valid_branch(branch)})
    for worker in workers:
        if not isinstance(worker, Mapping):
            raise IntakeError("a delegated worker request must be an object")
        row: dict[str, Any] = {"branch": _valid_branch(worker.get("branch"))}
        for field in (
            "idempotency_key",
            "provider",
            "requested_model",
            "expertise",
        ):
            if field in worker:
                row[field] = worker[field]
        requested.append(row)
    wanted = list(dict.fromkeys(str(row["branch"]) for row in requested))
    if not wanted:
        raise IntakeError("delegating an intake needs at least one worker branch")

    state_root = Path(state_dir if state_dir is not None else spool.state_dir)
    reviewed = spool.read_entry(primary)
    assert reviewed is not None
    reviewed_digest = requirements_digest(reviewed)
    event_ids = [
        value
        for value in [reviewed.get("source_event_id")]
        + [item.get("source_event_id") for item in reviewed.get("amendments", ())]
        if isinstance(value, str) and value
    ]
    receipts: list[dict[str, Any]] = []
    for event_id in event_ids:
        verification = verify_source_event(state_root, event_id, clock=clock)
        if not verification.get("verified"):
            raise IntakeError(
                "delegate source-event reverification failed: "
                + str(verification.get("reason") or event_id)
            )
        receipts.append(
            {
                "event_id": event_id,
                "basis": verification["basis"],
                "prompt_sha256": verification["prompt_sha256"],
            }
        )
    reserved: list[str] = []
    now = now_unix_ms(clock)
    current_supervisor = supervisor_identity(state_root)
    if not current_supervisor:
        raise IntakeError("delegate needs the live exact supervisor identity")

    def reserve(record: dict[str, Any]) -> dict[str, Any]:
        if record["state"] == "reported":
            raise IntakeError(
                f"intake {primary} is already reported; new work is a new intake"
            )
        known_rows = {worker["branch"]: worker for worker in record["workers"]}
        known = set(known_rows)
        new_branches = [branch for branch in wanted if branch not in known]
        generation = len(record["amendments"])
        current_event = _current_source_event(record)
        if requirements_digest(record) != reviewed_digest:
            raise IntakeError("requirements changed after delegate reverification")
        matching = [
            row for row in record["preflights"]
            if row["intake_generation"] == generation
            and row["source_event_id"] == current_event
            and row["requirements_digest"] == requirements_digest(record)
            and row["supervisor_thread_key"] == current_supervisor
        ]
        stale_not_started = [
            branch for branch in wanted
            if branch in known_rows
            and known_rows[branch].get("launch_state") == "not_started"
            and known_rows[branch].get("requirements_digest") != reviewed_digest
        ]
        if (new_branches or stale_not_started) and not matching:
            raise IntakeError(
                "delegate requires this supervisor's reviewed preflight for the current requirements"
            )
        active_preflight = matching[-1] if matching else None
        plan = {
            row["branch"]: row
            for row in (active_preflight["worker_plan"] if active_preflight else [])
        }
        if (
            record["origin"] == AUTO_ORIGIN
            and new_branches
            and not known
            and set(new_branches) != set(plan)
        ):
            raise IntakeError(
                "automatic intake delegation must launch the complete reviewed plan"
            )
        for request in requested:
            branch = str(request["branch"])
            planned = plan.get(branch)
            if branch in new_branches + stale_not_started and planned is None:
                raise IntakeError(f"worker branch {branch} is outside the reviewed preflight")
            if planned is not None:
                for field in (
                    "idempotency_key",
                    "provider",
                    "requested_model",
                    "expertise",
                ):
                    if field in request and request[field] != planned[field]:
                        raise IntakeError(
                            f"worker {branch} {field} differs from the reviewed preflight"
                        )
        actionable = [
            branch
            for branch in wanted
            if branch not in known_rows
            or known_rows[branch].get("launch_state") != "verified"
        ]
        if actionable and (launcher is None or reconciler is None):
            raise IntakeError(
                "the gated worker launcher and independent inventory reconciler are both required"
            )
        for branch in wanted:
            if branch in stale_not_started:
                assert active_preflight is not None
                planned = plan[branch]
                row = known_rows[branch]
                if planned["idempotency_key"] == row["idempotency_key"]:
                    raise IntakeError(
                        f"worker {branch} rebased reservation needs a new idempotency key"
                    )
                row.update(
                    idempotency_key=planned["idempotency_key"],
                    workstream=planned["workstream"],
                    scope=planned["scope"],
                    provider=planned["provider"],
                    requested_model=planned["requested_model"],
                    verified_actual_model="",
                    expertise=planned["expertise"],
                    rationale=planned["rationale"],
                    task_text=planned["task_text"],
                    acceptance_criteria=planned["acceptance_criteria"],
                    deliverable=planned["deliverable"],
                    duty_state="assigned",
                    duty_attempt=int(row.get("duty_attempt") or 1) + 1,
                    duty_delivery=None,
                    worker_identity="",
                    launch_state="not_started",
                    dispatch_unix_ms=None,
                    reconciled_unix_ms=None,
                    launched_unix_ms=None,
                    verified_unix_ms=None,
                    preflight_revision=active_preflight["revision"],
                    intake_generation=generation,
                    requirements_digest=reviewed_digest,
                    authority_verifications=receipts,
                    recorded_unix_ms=now,
                )
                reserved.append(branch)
                continue
            if branch in known:
                continue
            if len(record["workers"]) >= MAX_WORKERS:
                raise IntakeError(f"intake {primary} already records {MAX_WORKERS} workers")
            assert active_preflight is not None
            planned = plan[branch]
            record["workers"].append(
                {
                    "branch": branch,
                    "idempotency_key": planned["idempotency_key"],
                    "workstream": planned["workstream"],
                    "scope": planned["scope"],
                    "provider": planned["provider"],
                    "requested_model": planned["requested_model"],
                    "verified_actual_model": "",
                    "expertise": planned["expertise"],
                    "rationale": planned["rationale"],
                    "task_text": planned["task_text"],
                    "acceptance_criteria": planned["acceptance_criteria"],
                    "deliverable": planned["deliverable"],
                    "duty_state": "assigned",
                    "duty_attempt": 1,
                    "duty_delivery": None,
                    "duty_reports": [],
                    "worker_identity": "",
                    "launch_state": "not_started",
                    "dispatch_unix_ms": None,
                    "reconciled_unix_ms": None,
                    "launched_unix_ms": None,
                    "verified_unix_ms": None,
                    "preflight_revision": active_preflight["revision"],
                    "intake_generation": generation,
                    "requirements_digest": reviewed_digest,
                    "authority_verifications": receipts,
                    "recorded_unix_ms": now,
                }
            )
            known.add(branch)
            reserved.append(branch)
        record["updated_unix_ms"] = now
        return record

    entry = spool.update_entry(primary, reserve)
    verified: list[str] = []
    for branch in wanted:
        assignment = next(row for row in entry["workers"] if row["branch"] == branch)
        settled = assignment["launch_state"] in ("verified", "commissioned")
        if assignment["launch_state"] == "commissioned" or (
            settled and not duty_owed(assignment)
        ):
            continue
        if assignment["requirements_digest"] != reviewed_digest:
            raise IntakeError(f"worker {branch} reservation belongs to stale requirements")
        if assignment["launch_state"] == "not_started":
            if launcher is None:
                raise IntakeError(
                    f"worker {branch} cannot be dispatched without a launcher"
                )
            dispatched_at = now_unix_ms(clock)

            def dispatch(record: dict[str, Any]) -> dict[str, Any]:
                row = next(item for item in record["workers"] if item["branch"] == branch)
                if row["launch_state"] != "not_started":
                    raise IntakeError(f"worker {branch} is no longer not_started")
                row["launch_state"] = "dispatching"
                row["dispatch_unix_ms"] = dispatched_at
                record["updated_unix_ms"] = dispatched_at
                return record

            entry = spool.update_entry(primary, dispatch)
            assignment = next(row for row in entry["workers"] if row["branch"] == branch)
            try:
                launcher(dict(assignment))
            except Exception as exc:
                raise IntakeError(
                    f"worker {branch} dispatch is uncertain; reconcile before retry"
                ) from exc
        if assignment["launch_state"] in ("dispatching", "not_started"):
            if reconciler is None:
                raise IntakeError(
                    f"worker {branch} cannot be verified without a reconciler"
                )
            try:
                proof = reconciler(dict(assignment))
            except Exception as exc:
                raise IntakeError(f"worker {branch} inventory reconciliation failed") from exc
            if not isinstance(proof, Mapping) or proof.get("inventory_verified") is not True:
                raise IntakeError(
                    f"worker {branch} remains dispatching; inventory has no exact proof"
                )
            provider = proof.get("provider")
            actual_model = proof.get("actual_model")
            worker_identity = valid_thread_key(proof.get("worker_identity"))
            if proof.get("launch_idempotency_key") != assignment["idempotency_key"]:
                raise IntakeError(f"worker {branch} inventory proof has the wrong launch key")
            if provider != assignment["provider"]:
                raise IntakeError(f"worker {branch} inventory provider differs from preflight")
            if actual_model != assignment["requested_model"]:
                raise IntakeError(f"worker {branch} inventory model differs from preflight")
            if not worker_identity or not worker_identity.startswith(f"{provider}:"):
                raise IntakeError(f"worker {branch} inventory has no exact worker identity")
            reconciled_at = now_unix_ms(clock)

            def reconciled(record: dict[str, Any]) -> dict[str, Any]:
                row = next(item for item in record["workers"] if item["branch"] == branch)
                if row["launch_state"] in (
                    "provider_reconciled", "verified", "commissioned"
                ):
                    if (
                        row.get("worker_identity") != worker_identity
                        or row.get("verified_actual_model") != actual_model
                    ):
                        raise IntakeError(f"worker {branch} reconciliation proof conflicts")
                    return record
                if row["launch_state"] != "dispatching":
                    raise IntakeError(f"worker {branch} is not dispatching")
                row.update(
                    launch_state="provider_reconciled",
                    worker_identity=worker_identity,
                    verified_actual_model=actual_model,
                    reconciled_unix_ms=reconciled_at,
                    launched_unix_ms=reconciled_at,
                )
                record["updated_unix_ms"] = reconciled_at
                return record

            entry = spool.update_entry(primary, reconciled)
            assignment = next(row for row in entry["workers"] if row["branch"] == branch)
        verified_at = now_unix_ms(clock)

        def confirm(record: dict[str, Any]) -> dict[str, Any]:
            row = next(item for item in record["workers"] if item["branch"] == branch)
            if row["launch_state"] in ("verified", "commissioned"):
                return record
            if row["launch_state"] != "provider_reconciled":
                raise IntakeError(f"worker {branch} is not provider-reconciled")
            if requirements_digest(record) != row["requirements_digest"]:
                raise IntakeError(f"worker {branch} requirements changed before verification")
            row["launch_state"] = "verified"
            row["verified_unix_ms"] = verified_at
            return _advance(record, "delegated", verified_at)

        entry = spool.update_entry(primary, confirm)
        # A worker that arrived here already verified was launched by an
        # earlier call; re-delivering its duty does not make this the call
        # that launched it.
        if not settled:
            verified.append(branch)

    undelivered: list[str] = []
    for branch in wanted:
        assignment = next(row for row in entry["workers"] if row["branch"] == branch)
        if not duty_owed(assignment):
            continue
        if deliver is None:
            raise IntakeError(
                f"worker {branch} has a duty and no courier to deliver it with"
            )
        entry, landed_duty = _commission(
            spool,
            primary=primary,
            branch=branch,
            deliver=deliver,
            clock=clock,
        )
        if not landed_duty:
            undelivered.append(branch)
    return {
        "delegated": verified,
        "already_recorded": [branch for branch in wanted if branch not in verified],
        "undelivered": undelivered,
        "entry": entry,
    }


def _commission(
    spool: Spool,
    *,
    primary: str,
    branch: str,
    deliver: Callable[..., Mapping[str, Any]],
    clock: Callable[[], float],
) -> tuple[dict[str, Any], bool]:
    """Deliver one verified worker's duty and record what became of it.

    The delivery record is on disk before the send leaves, under the key the
    send will use, so a crash mid-commission leaves a duty that is owed rather
    than one nobody knows about.
    """
    reserved_at = now_unix_ms(clock)

    def reserve_duty(record: dict[str, Any]) -> dict[str, Any]:
        row = next(item for item in record["workers"] if item["branch"] == branch)
        if row["launch_state"] not in ("verified", "commissioned"):
            raise IntakeError(f"worker {branch} is not verified; its duty cannot be sent")
        row["duty_delivery"] = {
            "state": "undelivered",
            "relay_key": duty_key(record["msg_id"], branch, row.get("duty_attempt") or 1),
            "relay_msg_id": None,
            "relay_status": "unsent",
            "relay_detail": "the duty is recorded; its delivery has not returned",
            "delivered_unix_ms": None,
        }
        record["updated_unix_ms"] = reserved_at
        return record

    entry = spool.update_entry(primary, reserve_duty)
    worker = next(row for row in entry["workers"] if row["branch"] == branch)
    target = str(worker["worker_identity"])
    outcome = _delivery_outcome(
        deliver(
            thread_key=target,
            text=duty_text(primary, worker),
            key=str(worker["duty_delivery"]["relay_key"]),
        ),
        target,
    )
    settled = now_unix_ms(clock)

    def settle_duty(record: dict[str, Any]) -> dict[str, Any]:
        row = next(item for item in record["workers"] if item["branch"] == branch)
        delivery = dict(row["duty_delivery"] or {})
        delivery.update(
            state="delivered" if outcome.landed else "failed",
            relay_msg_id=outcome.msg_id,
            relay_status=outcome.status,
            relay_detail=outcome.detail,
            delivered_unix_ms=settled if outcome.landed else None,
        )
        row["duty_delivery"] = delivery
        if outcome.landed:
            row["launch_state"] = "commissioned"
        record["updated_unix_ms"] = settled
        return record

    return spool.update_entry(primary, settle_duty), outcome.landed


def _live_worker(record: Mapping[str, Any], branch: object) -> dict[str, Any]:
    """The worker row a duty verb may act on, or an exact refusal.

    A row recorded before duties existed carries only its branch and the moment
    it was reserved. Writing a duty state onto it would be dropped by the
    validator on the way to disk, so it is refused here instead of quietly
    losing a worker's report.
    """
    exact = _valid_branch(branch)
    for row in record.get("workers", ()):
        if row.get("branch") == exact:
            if "launch_state" not in row:
                raise IntakeError(
                    f"worker {exact} was recorded before duties existed; "
                    "re-delegate it against a current preflight to give it one"
                )
            return row
    raise IntakeError(f"intake {record['msg_id']} has no worker on branch {exact}")


def _worker_row(entry: Mapping[str, Any], branch: object) -> dict[str, Any]:
    return dict(_live_worker(entry, branch))


def _append_duty_report(
    record: dict[str, Any],
    *,
    branch: str,
    state: str,
    summary: str,
    reporter_identity: str,
    receipt: str,
    now: int,
) -> dict[str, Any]:
    row = _live_worker(record, branch)
    reports = row.setdefault("duty_reports", [])
    if len(reports) >= MAX_DUTY_REPORTS:
        raise IntakeError(
            f"worker {branch} already records {MAX_DUTY_REPORTS} duty reports"
        )
    sequence = len(reports) + 1
    reports.append(
        {
            "seq": sequence,
            "state": state,
            "summary": summary,
            "reporter_identity": reporter_identity,
            "duty_attempt": int(row.get("duty_attempt") or 1),
            "receipt": receipt,
            "recorded_unix_ms": now,
        }
    )
    row["duty_state"] = state
    record["updated_unix_ms"] = now
    return record


def report(
    spool: Spool,
    *,
    msg_id: str,
    branch: str,
    state: str,
    summary: str,
    reporter_identity: str = "",
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """A worker says what became of its duty, on disk, in the spool.

    This is the channel that makes a finished-but-silent worker impossible to
    miss: the report is a file the worker wrote, not a message somebody has to
    still be alive to receive. The receipt lands before the entry is touched,
    so a report that exists is never a report the record can lose.
    """
    if state not in WORKER_REPORT_STATES:
        raise IntakeError(
            "a worker reports one of: " + ", ".join(WORKER_REPORT_STATES)
        )
    primary = _require(spool, msg_id)
    exact_branch = _valid_branch(branch)
    body = _block(summary, limit=MAX_DUTY_SUMMARY, label="duty report summary")
    reporter = ""
    if reporter_identity:
        reporter = valid_thread_key(reporter_identity)
        if not reporter:
            raise IntakeError("a duty report reporter is not an exact thread key")
    now = now_unix_ms(clock)
    written: list[str] = []

    def record_report(record: dict[str, Any]) -> dict[str, Any]:
        row = _live_worker(record, exact_branch)
        if row["launch_state"] == "not_started":
            raise IntakeError(
                f"worker {exact_branch} has not been launched; there is no duty to report"
            )
        if row["duty_state"] == "abandoned":
            raise IntakeError(
                f"worker {exact_branch} duty was cancelled; reset it before reporting"
            )
        sequence = len(row.get("duty_reports") or ()) + 1
        path = spool.write_receipt(
            {
                "msg_id": record["msg_id"],
                "branch": exact_branch,
                "seq": sequence,
                "state": state,
                "summary": body,
                "reporter_identity": reporter,
                "duty_attempt": int(row.get("duty_attempt") or 1),
                "launch_key": row.get("idempotency_key") or "",
                "requirements_digest": row.get("requirements_digest") or "",
                "recorded_unix_ms": now,
            }
        )
        written.append(path.name)
        return _append_duty_report(
            record,
            branch=exact_branch,
            state=state,
            summary=body,
            reporter_identity=reporter,
            receipt=path.name,
            now=now,
        )

    entry = spool.update_entry(primary, record_report)
    return {
        "reported": True,
        "msg_id": primary,
        "branch": exact_branch,
        "state": state,
        "receipt": written[-1] if written else "",
        "worker": _worker_row(entry, exact_branch),
        "entry": entry,
    }


def retry(
    spool: Spool,
    *,
    msg_id: str,
    branch: str,
    idempotency_key: str,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Send a failed duty back to the start line for one more launch.

    A retry is a new launch, so it takes a new launch key: reusing the old one
    would let the reconciler bind this attempt to the dead attempt's inventory
    row. The reviewed preflight still decides what the work is; `delegate`
    runs the attempt.
    """
    primary = _require(spool, msg_id)
    exact_branch = _valid_branch(branch)
    fresh_key = valid_idempotency_key(idempotency_key)
    if not fresh_key:
        raise IntakeError("a duty retry needs an exact new idempotency key")
    now = now_unix_ms(clock)

    def rewind(record: dict[str, Any]) -> dict[str, Any]:
        row = _live_worker(record, exact_branch)
        delivery_failed = (row.get("duty_delivery") or {}).get("state") == "failed"
        if row["duty_state"] != "failed" and not delivery_failed:
            raise IntakeError(
                f"worker {exact_branch} has not failed; reset or cancel its duty instead"
            )
        if fresh_key == row.get("idempotency_key"):
            raise IntakeError(
                f"worker {exact_branch} retry needs a launch key the last attempt did not use"
            )
        row.update(
            idempotency_key=fresh_key,
            verified_actual_model="",
            worker_identity="",
            launch_state="not_started",
            dispatch_unix_ms=None,
            reconciled_unix_ms=None,
            launched_unix_ms=None,
            verified_unix_ms=None,
            duty_state="assigned",
            duty_attempt=int(row.get("duty_attempt") or 1) + 1,
            duty_delivery=None,
        )
        record["updated_unix_ms"] = now
        return _append_duty_report(
            record,
            branch=exact_branch,
            state="assigned",
            summary=f"duty retried as attempt {row['duty_attempt']}",
            reporter_identity="",
            receipt="",
            now=now,
        )

    entry = spool.update_entry(primary, rewind)
    return {
        "retried": True,
        "msg_id": primary,
        "branch": exact_branch,
        "worker": _worker_row(entry, exact_branch),
        "entry": entry,
    }


def reset(
    spool: Spool,
    *,
    msg_id: str,
    branch: str,
    summary: str = "",
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Put a settled duty back to assigned without relaunching the worker.

    The session and its proven identity stay exactly as they are; only what
    the record believes about the duty changes. The history is kept — a reset
    is another line in it, never an erasure.
    """
    primary = _require(spool, msg_id)
    exact_branch = _valid_branch(branch)
    body = _block(
        summary or "duty reset by the supervisor",
        limit=MAX_DUTY_SUMMARY,
        label="duty reset summary",
    )
    now = now_unix_ms(clock)

    def rewind(record: dict[str, Any]) -> dict[str, Any]:
        row = _live_worker(record, exact_branch)
        if row["duty_state"] == "assigned":
            raise IntakeError(
                f"worker {exact_branch} duty is already assigned; there is nothing to reset"
            )
        return _append_duty_report(
            record,
            branch=exact_branch,
            state="assigned",
            summary=body,
            reporter_identity="",
            receipt="",
            now=now,
        )

    entry = spool.update_entry(primary, rewind)
    return {
        "reset": True,
        "msg_id": primary,
        "branch": exact_branch,
        "worker": _worker_row(entry, exact_branch),
        "entry": entry,
    }


def cancel(
    spool: Spool,
    *,
    msg_id: str,
    branch: str,
    summary: str,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Abandon one duty, with the reason recorded against it.

    Only the supervisor abandons a duty, and only here. This closes the record
    for that branch; it does not close the worker's session — a live session
    with an abandoned duty is exactly what the duties view is for.
    """
    primary = _require(spool, msg_id)
    exact_branch = _valid_branch(branch)
    body = _block(summary, limit=MAX_DUTY_SUMMARY, label="duty cancellation reason")
    now = now_unix_ms(clock)

    def abandon(record: dict[str, Any]) -> dict[str, Any]:
        row = _live_worker(record, exact_branch)
        if row["duty_state"] == "abandoned":
            raise IntakeError(f"worker {exact_branch} duty is already abandoned")
        return _append_duty_report(
            record,
            branch=exact_branch,
            state="abandoned",
            summary=body,
            reporter_identity="",
            receipt="",
            now=now,
        )

    entry = spool.update_entry(primary, abandon)
    return {
        "cancelled": True,
        "msg_id": primary,
        "branch": exact_branch,
        "worker": _worker_row(entry, exact_branch),
        "entry": entry,
    }


def _duty_row(
    entry: Mapping[str, Any],
    worker: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]],
    now: int,
) -> dict[str, Any]:
    delivery = worker.get("duty_delivery") or {}
    reports = list(worker.get("duty_reports") or ())
    last = reports[-1] if reports else None
    delivered_at = delivery.get("delivered_unix_ms")
    mine = [row for row in receipts if row["branch"] == worker["branch"]]
    folded = {str(row.get("receipt")) for row in reports if row.get("receipt")}
    unfolded = [
        str(row.get("file") or row["seq"])
        for row in mine
        if str(row.get("file") or "") not in folded
    ]
    silent_for = (
        now - int(delivered_at)
        if delivered_at and worker.get("duty_state") == "assigned"
        else 0
    )
    return {
        "msg_id": entry["msg_id"],
        "branch": worker["branch"],
        "workstream": worker.get("workstream", ""),
        "provider": worker.get("provider", ""),
        "requested_model": worker.get("requested_model", ""),
        "worker_identity": worker.get("worker_identity", ""),
        "launch_state": worker.get("launch_state", ""),
        "duty_state": worker.get("duty_state", "assigned"),
        "duty_attempt": worker.get("duty_attempt", 1),
        "has_duty_text": bool(worker.get("task_text")),
        "duty_delivery_state": delivery.get("state", "none"),
        "duty_delivered_unix_ms": delivered_at,
        "duty_owed": duty_owed(worker),
        "reports": len(reports),
        "last_report": last,
        "receipts_on_disk": len(mine),
        "unfolded_receipts": unfolded,
        "silent_for_ms": silent_for,
        "silent": bool(
            worker.get("launch_state") == "commissioned"
            and worker.get("duty_state") == "assigned"
            and delivered_at
            and silent_for >= SILENT_DUTY_MS
        ),
    }


def duties(
    spool: Spool,
    *,
    msg_id: str | None = None,
    clock: Callable[[], float] = time.time,
    limit: int = MAX_OPEN,
) -> dict[str, Any]:
    """Every commissioned duty and what it has said for itself.

    One intake when named, otherwise every open intake. A worker that was sent
    its duty and has reported nothing since is flagged `silent`; a receipt on
    disk with no matching line in the entry is flagged too. Neither is decided
    for you — they are the two ways a worker goes quiet, made visible.
    """
    now = now_unix_ms(clock)
    if msg_id:
        primary = _require(spool, msg_id)
        entry = spool.read_entry(primary)
        assert entry is not None
        entries = [entry]
    else:
        entries = spool.open_entries(limit)
    rows: list[dict[str, Any]] = []
    for entry in entries:
        receipts = spool.read_receipts(entry["msg_id"])
        for worker in entry.get("workers", ()):
            rows.append(_duty_row(entry, worker, receipts, now))
    return {
        "as_of_unix_ms": now,
        "count": len(rows),
        "silent": [row["branch"] for row in rows if row["silent"]],
        "owed": [row["branch"] for row in rows if row["duty_owed"]],
        "unfolded_receipts": [
            row["branch"] for row in rows if row["unfolded_receipts"]
        ],
        "duties": rows,
    }


def relay(
    spool: Spool,
    *,
    msg_id: str,
    text: str,
    kind: str,
    deliver: Callable[..., Mapping[str, Any]],
    clock: Callable[[], float] = time.time,
) -> tuple[int, dict[str, Any]]:
    """Send one note to the intake's source thread and record what became of it.

    The note is on disk before the send leaves, carrying the idempotency key
    the send will use, so a crash mid-relay leaves a note that is owed rather
    than one nobody knows about — and the retry resumes that key instead of
    delivering the same words twice.
    """
    if kind not in KINDS:
        raise IntakeError(f"{kind!r} is not an intake note kind")
    primary = _require(spool, msg_id)
    body = _text(text, limit=MAX_NOTE_TEXT, label="note text")
    entry = spool.read_entry(primary)
    assert entry is not None
    generation_digest = requirements_digest(entry)
    note = _existing_note(entry, kind, body, generation_digest)
    if note is not None and note["landed"]:
        return 0, {"relayed": False, "duplicate": True, "note": note, "entry": entry}
    if note is None:
        reserved = now_unix_ms(clock)
        entry = spool.update_entry(
            primary,
            lambda record: _reserve(
                record, kind, body, reserved, generation_digest
            ),
        )
        note = entry["notes"][-1]
    outcome = _delivery_outcome(
        deliver(
            thread_key=entry["source_thread_key"],
            text=relay_text(kind, primary, body, note.get("source_event_id")),
            key=str(note["relay_key"]),
        ),
        entry["source_thread_key"],
    )
    settled = now_unix_ms(clock)

    def settle(record: dict[str, Any]) -> dict[str, Any]:
        for row in record["notes"]:
            if row["seq"] != note["seq"]:
                continue
            row["relay_msg_id"] = outcome.msg_id
            row["relay_status"] = outcome.status
            row["relay_detail"] = outcome.detail
            row["landed"] = outcome.landed
            row["relayed_unix_ms"] = settled if outcome.landed else None
        record["updated_unix_ms"] = settled
        # A transition means the source was told, not that the resident meant
        # to tell it. A note that never landed moves nothing.
        current_digest = requirements_digest(record)
        stale = current_digest != note["requirements_digest"]
        for row in record["notes"]:
            if row["seq"] == note["seq"]:
                row["stale_generation"] = stale
        if outcome.landed and not stale and kind in _RELAY_STATE:
            _advance(record, _RELAY_STATE[kind], settled)
        return record

    entry = spool.update_entry(primary, settle)
    return (0 if outcome.landed else 1), {
        "relayed": outcome.landed,
        "duplicate": False,
        "note": entry["notes"][note["seq"] - 1],
        "entry": entry,
    }


def _reserve(
    record: dict[str, Any],
    kind: str,
    text: str,
    now: int,
    generation_digest: str,
) -> dict[str, Any]:
    if len(record["notes"]) >= MAX_NOTES:
        raise IntakeError(f"intake {record['msg_id']} already records {MAX_NOTES} notes")
    sequence = len(record["notes"]) + 1
    latest_event = (
        record["amendments"][-1]["source_event_id"]
        if record.get("amendments")
        else record.get("source_event_id")
    )
    record["notes"].append(
        {
            "seq": sequence,
            "kind": kind,
            "via": "send",
            "text": text,
            "recorded_unix_ms": now,
            "relayed_unix_ms": None,
            "relay_key": f"intake-note:{record['msg_id']}:{sequence}",
            "relay_msg_id": None,
            "relay_status": "unsent",
            "relay_detail": "the note is recorded; its relay has not returned",
            "landed": False,
            "source_event_id": latest_event,
            "requirements_digest": generation_digest,
            "requirements_revision": len(record["amendments"]),
            "stale_generation": False,
        }
    )
    record["updated_unix_ms"] = now
    return record


# ---- the automatic producer ---------------------------------------------


def machine_transport_prompt(value: object) -> bool:
    """Whether a provider prompt is machine transport, never a new project."""
    if not isinstance(value, str):
        return False
    text = value.lstrip()
    if text.startswith(DELIVERY_RUNNER_PREFIX):
        return True
    if text.startswith(AUTOMATION_WAKE_PREFIX):
        return True
    return (
        text.startswith(CROSS_SESSION_MESSAGE_PREFIX)
        and "[session-kit operator message " in text
    )


def substantive_prompt(value: object) -> str:
    """The prompt as a project statement, or "" when it is not one.

    A project arrives as somebody saying what they want done. These are the
    things that reach the same hook and are not that: an empty line, a slash
    command, the kit's own operator envelope (a message the supervisor sent is
    not a project the supervisor was given), the resume boilerplate a
    relaunched session replays, a single word, and a prompt built only out of
    agreement — "yes please" answers a question, it does not set one.

    The bias is deliberate: an intake nobody wanted is noise the supervisor
    can close, while a project nobody recorded is the failure this whole spool
    exists to prevent. Anything not on this list is treated as a project.
    """
    if not isinstance(value, str):
        return ""
    text = _text(value, limit=MAX_SUMMARY, label="prompt", required=False)
    if not text or text.startswith("/"):
        return ""
    if text.startswith("[session-kit operator message"):
        return ""
    if text.startswith(RESUME_BOILERPLATE):
        return ""
    if machine_transport_prompt(text):
        return ""
    words = text.split(" ")
    if len(text) < SUBSTANTIVE_CHARS or len(words) < SUBSTANTIVE_WORDS:
        return ""
    if all(word.strip(",.!?;:").casefold() in AGREEMENT_WORDS for word in words):
        return ""
    return text


def payload_fields(payload: Mapping[str, Any]) -> dict[str, str]:
    """What one hook payload says about a root thread's own first prompt.

    Returns empty when the payload is not that: a subagent or sidechain, a
    turn belonging to some other thread, a provider or identity that is not
    exact. Both providers' adapters normalise into the same keys and this is
    the only place the rules live, so neither can drift into its own idea of
    what a root prompt is.
    """
    if not isinstance(payload, Mapping):
        return {}
    for marker in SIDECHAIN_MARKERS:
        value = payload.get(marker)
        if isinstance(value, str) and value.strip():
            return {}
        if value is True:
            return {}
    provider = payload.get("provider")
    if provider not in ("claude", "codex"):
        return {}
    uuid = ""
    for field in ("session_id", "sessionId", "thread_id", "conversation_id"):
        uuid = valid_uuid(payload.get(field))
        if uuid:
            break
    if not uuid:
        return {}
    prompt = substantive_prompt(payload.get("prompt"))
    if not prompt:
        return {}
    turn = payload.get("turn_id") or payload.get("turnId")
    return {
        "thread_key": f"{provider}:{uuid}",
        "prompt": prompt,
        "turn_id": _text(turn, limit=200, label="turn id", required=False),
        "cwd": _text(payload.get("cwd"), limit=MAX_CWD, label="cwd", required=False),
    }


def supervisor_identity(state_dir: Path | str) -> str:
    """The supervisor's own thread key, or "" when nothing readable says.

    Read tolerantly on purpose. A marker that cannot be read is a reason to
    let one questionable intake through, never a reason to stop recording
    everybody's projects.
    """
    path = Path(state_dir) / "supervisor" / "identity"
    try:
        raw = _read_private_bytes(path, MAX_POINTER_BYTES)
    except IntakeError:
        return ""
    if raw is None:
        return ""
    return valid_thread_key(raw.decode("ascii", "ignore").strip())


def _new_auto_entry(
    spool: Spool,
    *,
    thread: str,
    key: str,
    body: str,
    turn_id: str,
    source_event_id: str,
    source_digest: str,
    cwd: str,
    follows: str | None,
    now: int,
) -> dict[str, Any]:
    """One fresh automatic intake, claimed by its thread. Caller holds the lock."""
    msg_id = new_msg_id(now, taken=lambda value: spool.read_entry(value) is not None)
    entry = spool.write_entry(
        {
            "msg_id": msg_id,
            "origin": AUTO_ORIGIN,
            "follows": follows,
            "intake_key": key,
            "also_delivered_as": [],
            "source_thread_key": thread,
            "source_turn_id": turn_id,
            "source_digest": source_digest,
            "source_event_id": source_event_id,
            "source_terminal": None,
            "source_title": "",
            "source_cwd": cwd,
            "summary": body,
            "state": "received",
            "received_unix_ms": now,
            "updated_unix_ms": now,
            "acknowledged_unix_ms": None,
            "delegated_unix_ms": None,
            "reported_unix_ms": None,
            "arrival_notice": {
                "relay_key": f"{ARRIVAL_KEY_PREFIX}:{msg_id}",
                "relay_msg_id": None,
                "relay_status": "not delivered yet",
                "delivered": False,
                "delivered_unix_ms": None,
                "delivery_attempts": 0,
                "last_attempt_unix_ms": None,
                "next_attempt_unix_ms": None,
            },
            "workers": [],
            "notes": [],
            "amendments": [],
            "preflights": [],
        }
    )
    spool.claim_key(key, msg_id)
    return entry


def _already_stated(entry: Mapping[str, Any], turn_id: str, digest: str) -> bool:
    """True when this thread has already said exactly this, on this turn.

    The pair is the whole dedup rule: a hook that fires twice repeats one
    turn's words, and so does a relaunch replaying them. A provider that gives
    no turn id leaves the digest to carry it alone — repeating a prompt
    verbatim then reads as the same statement, which is the safe way to be
    wrong: the supervisor already has those words.
    """
    if entry.get("source_turn_id", "") == turn_id and entry.get("source_digest") == digest:
        return True
    return any(
        item.get("turn_id", "") == turn_id and item.get("digest") == digest
        for item in entry.get("amendments", ())
    )


def produce(
    spool: Spool,
    *,
    thread_key: str,
    prompt: str,
    turn_id: str = "",
    cwd: str = "",
    supervisor_key: str = "",
    source_event_id: str = "",
    source_digest: str = "",
    amendment_only: bool = False,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Take one root prompt: open a project, extend it, or recognise a repeat.

    A thread states a project once and then keeps adding to it, so one prompt
    has three honest outcomes. The first substantive prompt CREATES the intake.
    Every later one while that intake is still open is an AMENDMENT appended to
    it — the requirement was added to the same project, and it must reach the
    supervisor without a second delegation. A prompt after the project was
    reported REOPENS: the thread is talking again about finished work, which is
    a new project, recorded fresh and linked back to the one it follows.

    The claim `auto-intake:<provider>:<uuid>` always names the thread's one
    OPEN intake, so every path above is decided by a single pointer read.
    """
    from .source_authority import machine_envelope_prompt

    now = now_unix_ms(clock)
    thread = valid_thread_key(thread_key)
    if not thread:
        raise IntakeError("an automatic intake needs an exact source thread key")
    if supervisor_key and thread == supervisor_key:
        # The supervisor's own standing brief is not a project it was handed.
        return {"action": REFUSED, "reason": "supervisor thread", "entry": None}
    if machine_envelope_prompt(prompt):
        # The harness submits its own envelopes down the same channel a person
        # types into. Source authority already refuses them as authority, and
        # the intake ledger is append-only — so letting one become a
        # requirement of an open project would make that project permanently
        # uncertifiable over something nobody asked for. The event is still
        # captured and still auditable; it simply is not a requirement.
        return {
            "action": REFUSED,
            "reason": "a harness envelope is not a project requirement",
            "entry": None,
        }
    body = substantive_prompt(prompt)
    if not body and amendment_only:
        body = _text(prompt, limit=MAX_SUMMARY, label="prompt", required=False)
    if not body or body.startswith(("/", "[session-kit operator message", RESUME_BOILERPLATE)):
        return {"action": REFUSED, "reason": "not a substantive prompt", "entry": None}
    turn = _text(turn_id, limit=200, label="turn id", required=False)
    event_id = _source_event_id(source_event_id, required=True)
    digest = _text(
        source_digest, limit=64, label="source prompt digest", required=True
    )
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise IntakeError("an automatic intake needs an exact source prompt digest")
    key = f"{AUTO_KEY_PREFIX}:{thread}"
    with spool.locked():
        claimed = spool.msg_id_for_key(key)
        if not claimed:
            if amendment_only:
                return {
                    "action": REFUSED,
                    "reason": "not a project-opening prompt",
                    "entry": None,
                }
            entry = _new_auto_entry(
                spool,
                thread=thread,
                key=key,
                body=body,
                turn_id=turn,
                source_event_id=event_id,
                source_digest=digest,
                cwd=cwd,
                follows=None,
                now=now,
            )
            spool.prune(now)
            return {"action": CREATED, "reason": "first prompt", "entry": entry}
        open_entry = spool.read_entry(claimed)
        if open_entry is None:
            raise IntakeError(f"intake {claimed} is claimed but its entry is gone")
        if open_entry["state"] == "reported":
            entry = _new_auto_entry(
                spool,
                thread=thread,
                key=key,
                body=body,
                turn_id=turn,
                source_event_id=event_id,
                source_digest=digest,
                cwd=cwd,
                follows=claimed,
                now=now,
            )
            return {
                "action": REOPENED,
                "reason": "the thread's last project was reported",
                "entry": entry,
                "follows": claimed,
            }
        if _already_stated(open_entry, turn, digest):
            return {
                "action": DUPLICATE,
                "reason": "already recorded",
                "entry": open_entry,
            }
        if len(open_entry["amendments"]) >= MAX_AMENDMENTS:
            raise IntakeError(
                f"intake {claimed} already records {MAX_AMENDMENTS} amendments"
            )
        sequence = len(open_entry["amendments"]) + 1
        open_entry["amendments"].append(
            {
                "seq": sequence,
                "turn_id": turn,
                "digest": digest,
                "source_event_id": event_id,
                "text": body,
                "recorded_unix_ms": now,
                "delivered_unix_ms": None,
                "relay_key": f"{AMEND_KEY_PREFIX}:{claimed}:{sequence}",
                "relay_msg_id": None,
                "relay_status": "not delivered yet",
                "delivered": False,
                "delivery_attempts": 0,
                "last_attempt_unix_ms": None,
                "next_attempt_unix_ms": None,
            }
        )
        open_entry["updated_unix_ms"] = now
        entry = spool.write_entry(open_entry)
    return {
        "action": AMENDED,
        "reason": "added to the open project",
        "entry": entry,
        "amendment": entry["amendments"][-1],
    }


def _spawn_detached(argv: Sequence[str]) -> None:
    """Start a command this process will never wait for, hear, or outlive."""
    import subprocess

    with open(os.devnull, "r+b") as null:
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(argv),
            stdin=null,
            stdout=null,
            stderr=null,
            start_new_session=True,
            close_fds=True,
        )


def supervisor_command(environ: Mapping[str, str]) -> Path:
    override = environ.get("SESSION_KIT_SUPERVISOR_BIN")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "bin" / "supervisor"


def request_supervisor(
    state_dir: Path | str,
    *,
    environ: Mapping[str, str],
    spawn: Callable[[Sequence[str]], None] = _spawn_detached,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Ask for a supervisor without making anybody's session wait for one.

    The human typed a prompt; that prompt is what matters. `ensure` runs
    detached, is never waited on, and its outcome never reaches this caller —
    a missing, parked, or slow supervisor must cost the prompt nothing.

    Two guards keep a fleet-wide start from becoming an ensure storm: one
    non-blocking lock, so simultaneous roots do not even queue behind each
    other, and one durable stamp, so the next minute's roots find the ask
    already made. Skipping is the right answer both times — `ensure` is
    idempotent and the supervisor it creates serves every one of them.
    """
    now = now_unix_ms(clock)
    command = supervisor_command(environ)
    if not os.access(command, os.X_OK):
        return {"requested": False, "reason": "supervisor command is unavailable"}
    root = Path(state_dir) / "supervisor"
    _private_directory(root)
    stamp_path = root / "ensure-requested"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root / "ensure.lock", flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"requested": False, "reason": "another root is already asking"}
        try:
            raw = _read_private_bytes(stamp_path, MAX_POINTER_BYTES)
        except IntakeError:
            raw = None
        try:
            asked = int((raw or b"0").decode("ascii", "ignore").strip() or 0)
        except ValueError:
            asked = 0
        if 0 < asked <= now and now - asked < ENSURE_COOLDOWN_MS:
            return {"requested": False, "reason": "asked within the cooldown"}
        _atomic_private_write(stamp_path, f"{now}\n".encode("ascii"))
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    spawn([os.fspath(command), "ensure"])
    return {"requested": True, "reason": "ensure started detached"}


def amendment_text(msg_id: str, sequence: int, source_event_id: str) -> str:
    """What the supervisor receives when a project grows.

    It names the intake, not the session: the entry has the thread, the
    workers, and everything already reported, and `msg intake open` is one
    read away.
    """
    return (
        f"Source event {source_event_id} amended intake {msg_id} at sequence "
        f"{sequence}. Read the intake, then verify_source_event; this machine "
        "notice carries identifiers only and grants no authority by itself."
    )


def arrival_text(msg_id: str, source_event_id: str) -> str:
    """The identifier-only wake-up for a newly recorded automatic project."""
    return (
        f"Automatic intake {msg_id} is ready from source event {source_event_id}. "
        "Read the complete intake, then verify_source_event and record your own "
        "preflight before any delegation; this machine notice carries identifiers "
        "only and grants no authority by itself."
    )


def inventory_core(environ: Mapping[str, str]) -> Path:
    override = environ.get("SESSION_KIT_INVENTORY_CORE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "session_inventory.py"


def request_delivery(
    environ: Mapping[str, str],
    *,
    spawn: Callable[[Sequence[str]], None] = _spawn_detached,
) -> dict[str, Any]:
    """Start intake-notice delivery behind the prompt, never inside it.

    Delivering to a Claude supervisor runs the headless sender, which takes
    about a minute. That minute belongs to nobody's typing, so it happens in a
    detached process this one neither waits for nor hears from.
    """
    core = inventory_core(environ)
    if not core.is_file():
        return {"requested": False, "reason": "the messaging core is unavailable"}
    # `msg intake flush` is the verb that owns delivery, so the detached run is
    # the same code path a person gets by typing it. `bin/supervisor` has no
    # part in this: it creates supervisors, it does not carry their messages.
    spawn([sys.executable, os.fspath(core), "msg", "intake", "flush"])
    return {"requested": True, "reason": "delivery started detached"}


def undelivered_relays(
    rows: Sequence[Mapping[str, Any]], *, now_ms: int
) -> list[dict[str, Any]]:
    """Every notice these intakes owe the supervisor, oldest first.

    One flat list of two kinds: the arrival notice that wakes a resident for a
    new project, and each amendment that added to one already open. Both are
    machine notices carrying identifiers, and both are lost silently today
    when their single delivery attempt fails.
    """
    pending: list[dict[str, Any]] = []
    for entry in rows:
        notice = entry.get("arrival_notice")
        if (
            entry.get("origin") == AUTO_ORIGIN
            and isinstance(notice, Mapping)
            and not notice.get("delivered")
        ):
            pending.append(
                {
                    "msg_id": str(entry["msg_id"]),
                    "kind": "arrival",
                    "seq": 0,
                    "recorded_unix_ms": int(entry["received_unix_ms"]),
                    "waiting_ms": max(0, now_ms - int(entry["received_unix_ms"])),
                    "relay_status": str(notice.get("relay_status") or ""),
                    "delivery_attempts": _attempts(notice.get("delivery_attempts")),
                    "next_attempt_unix_ms": notice.get("next_attempt_unix_ms"),
                    "due": relay_due(notice, now_ms),
                    "source_thread_key": str(entry.get("source_thread_key") or ""),
                }
            )
        for item in entry.get("amendments", ()):
            if item.get("delivered"):
                continue
            recorded = int(item["recorded_unix_ms"])
            pending.append(
                {
                    "msg_id": str(entry["msg_id"]),
                    "kind": "amendment",
                    "seq": int(item["seq"]),
                    "recorded_unix_ms": recorded,
                    "waiting_ms": max(0, now_ms - recorded),
                    "relay_status": str(item.get("relay_status") or ""),
                    "delivery_attempts": _attempts(item.get("delivery_attempts")),
                    "next_attempt_unix_ms": item.get("next_attempt_unix_ms"),
                    "due": relay_due(item, now_ms),
                    "source_thread_key": str(entry.get("source_thread_key") or ""),
                }
            )
    pending.sort(key=lambda item: (item["recorded_unix_ms"], item["msg_id"], item["seq"]))
    return pending


def pending_relays(
    spool: Spool,
    *,
    clock: Callable[[], float] = time.time,
    limit: int = MAX_OPEN,
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """How much the spool still owes the supervisor, and for how long.

    Nothing surfaced this before, so five real operator messages could sit
    undelivered for days with every screen in the fleet reading normal. The
    count is the point: whoever reads it learns that something is owed without
    having to open the state files to find out.
    """
    now = now_unix_ms(clock)
    items = undelivered_relays(
        rows if rows is not None else spool.open_entries(limit), now_ms=now
    )
    return {
        "as_of_unix_ms": now,
        "count": len(items),
        "due_now": sum(1 for item in items if item["due"]),
        "deferred": sum(1 for item in items if not item["due"]),
        "oldest_waiting_ms": items[0]["waiting_ms"] if items else 0,
        "truncated": len(items) > MAX_PENDING_LISTED,
        "items": items[:MAX_PENDING_LISTED],
    }


def _settle_relay(
    row: dict[str, Any], outcome: Delivery, settled: int
) -> dict[str, Any]:
    """Record one relay attempt's fate on the item that owed it.

    A landed relay is finished. One that did not land keeps its place in the
    queue and earns the next rung of the backoff, so the sweep that follows
    tries it again instead of writing it off.
    """
    row["relay_msg_id"] = outcome.msg_id
    row["relay_status"] = outcome.status
    row["delivered"] = outcome.landed
    row["delivered_unix_ms"] = settled if outcome.landed else None
    attempts = _attempts(row.get("delivery_attempts")) + 1
    row["delivery_attempts"] = attempts
    row["last_attempt_unix_ms"] = settled
    row["next_attempt_unix_ms"] = (
        None if outcome.landed else next_attempt_unix_ms(attempts, settled)
    )
    return row


def flush(
    spool: Spool,
    *,
    deliver: Callable[..., Mapping[str, Any]],
    state_dir: Path | str,
    clock: Callable[[], float] = time.time,
    limit: int = MAX_OPEN,
) -> dict[str, Any]:
    """Deliver every intake notice the supervisor is owed and may be tried now.

    Exactly once is held in two places at different timescales. The stored
    `delivered` flag rules out a second send of an amendment already known to
    have landed, and the relay key rules out a second COPY of one whose first
    send is still in flight — the messaging core resumes that key's message and
    skips a target the ledger already shows as landed. The lock stops two
    deliveries from starting at all, which is cheaper than either.

    A relay that does not land stays owed. This verb is therefore the sweep as
    well as the first attempt: run it again — every supervisor wake does — and
    whatever the fleet still owes is tried again, on a backoff so that a
    supervisor nobody can reach costs one send per rung rather than one per
    wake.
    """
    supervisor = supervisor_identity(state_dir)
    now = now_unix_ms(clock)
    rows = spool.open_entries(limit)
    by_id = {str(entry["msg_id"]): entry for entry in rows}
    owed = undelivered_relays(rows, now_ms=now)
    due = [item for item in owed if item["due"]]
    if not owed:
        return {
            "delivered": 0,
            "attempted": 0,
            "pending": 0,
            "deferred": 0,
            "reason": "nothing is owed",
        }
    if not supervisor:
        return {
            "delivered": 0,
            "attempted": 0,
            "pending": len(owed),
            "deferred": len(owed) - len(due),
            "reason": "no supervisor identity to deliver to yet",
        }
    if not due:
        return {
            "delivered": 0,
            "attempted": 0,
            "pending": len(owed),
            "deferred": len(owed),
            "reason": "every undelivered notice is waiting out its backoff",
        }
    spool.ensure()
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(spool.root / "flush.lock", flags, 0o600)
    delivered = 0
    attempted = 0
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "delivered": 0,
                "attempted": 0,
                "pending": len(owed),
                "deferred": len(owed) - len(due),
                "reason": "another delivery is already running",
            }
        for item in due:
            entry = by_id[item["msg_id"]]
            arrival = item["kind"] == "arrival"
            if arrival:
                notice = entry["arrival_notice"]
                key = str(notice["relay_key"])
                text = arrival_text(
                    str(entry["msg_id"]), str(entry["source_event_id"])
                )
            else:
                amendment = entry["amendments"][item["seq"] - 1]
                key = str(amendment["relay_key"])
                text = amendment_text(
                    str(entry["msg_id"]),
                    int(amendment["seq"]),
                    str(amendment["source_event_id"]),
                )
            outcome = _delivery_outcome(
                deliver(thread_key=supervisor, text=text, key=key),
                supervisor,
            )
            settled = now_unix_ms(clock)

            def settle(
                record: dict[str, Any],
                seq: int = int(item["seq"]),
                is_arrival: bool = arrival,
            ) -> dict[str, Any]:
                if is_arrival:
                    current = record.get("arrival_notice")
                    if current is not None:
                        _settle_relay(current, outcome, settled)
                else:
                    for row in record["amendments"]:
                        if row["seq"] == seq:
                            _settle_relay(row, outcome, settled)
                record["updated_unix_ms"] = settled
                return record

            spool.update_entry(str(entry["msg_id"]), settle)
            attempted += 1
            delivered += 1 if outcome.landed else 0
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    return {
        "delivered": delivered,
        "attempted": attempted,
        "pending": len(owed) - delivered,
        "deferred": len(owed) - len(due),
        "reason": "delivered" if delivered else "nothing landed",
    }


def from_hook(
    payload: Mapping[str, Any],
    *,
    state_dir: Path | str,
    environ: Mapping[str, str],
    spool: Spool | None = None,
    spawn: Callable[[Sequence[str]], None] = _spawn_detached,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """One provider hook payload in, at most one recorded intake out.

    Order is the contract: the intake is durable BEFORE the supervisor is
    asked for, and asked for only when something was actually recorded. A
    supervisor that never starts still leaves the project on disk for the next
    one, which is the whole point of writing it down first.
    """
    from .source_authority import (
        SourceAuthorityError,
        capture_hook_event,
        write_intake_commit_marker,
    )

    # A managed worker's bootstrap is transport for establishing its exact
    # provider conversation identity.  It is not a new project and must not
    # recursively re-enter the supervisor queue.
    if valid_idempotency_key(
        environ.get("SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY")
    ):
        return {
            "action": REFUSED,
            "produced": False,
            "reason": "Session Kit managed worker bootstrap is not a project intake",
        }

    # The headless Claude message adapter is itself a root provider process.
    # Its fixed delivery instruction reaches UserPromptSubmit but is transport,
    # not a project. Refuse it before even creating source-event evidence.
    if machine_transport_prompt(payload.get("prompt")):
        return {
            "action": REFUSED,
            "produced": False,
            "reason": "Session Kit delivery transport is not a project intake",
        }
    try:
        source_event = capture_hook_event(payload, state_dir=state_dir, clock=clock)
    except SourceAuthorityError as exc:
        return {
            "action": REFUSED,
            "produced": False,
            "reason": str(exc),
        }
    fields = payload_fields(payload)
    amendment_only = False
    if not fields:
        raw_prompt = payload.get("prompt")
        if not isinstance(raw_prompt, str):
            return {
                "action": REFUSED,
                "produced": False,
                "reason": "not a root thread's own prompt",
                "source_event_id": source_event["event_id"],
            }
        fields = {
            "thread_key": (
                f"{source_event['provider']}:{source_event['session_id']}"
            ),
            "prompt": raw_prompt,
            "turn_id": str(source_event["turn_id"]),
            "cwd": _text(
                payload.get("cwd"), limit=MAX_CWD, label="cwd", required=False
            ),
        }
        amendment_only = True
    store = spool if spool is not None else Spool(state_dir)
    outcome = produce(
        store,
        thread_key=fields["thread_key"],
        prompt=fields["prompt"],
        turn_id=fields["turn_id"],
        cwd=fields["cwd"],
        supervisor_key=supervisor_identity(state_dir),
        source_event_id=str(source_event["event_id"]),
        source_digest=str(source_event["prompt_sha256"]),
        amendment_only=amendment_only,
        clock=clock,
    )
    action = str(outcome["action"])
    outcome["produced"] = action in (CREATED, REOPENED)
    outcome["source_event_id"] = source_event["event_id"]
    entry = outcome.get("entry")
    if isinstance(entry, Mapping):
        try:
            outcome["intake_commit"] = write_intake_commit_marker(
                source_event,
                intake_msg_id=str(entry["msg_id"]),
                requirements_revision=len(entry["amendments"]),
                requirements_digest=requirements_digest(entry),
                environ=environ,
                clock=clock,
            )
        except SourceAuthorityError as exc:
            outcome["intake_commit"] = {
                "configured": True,
                "intake_committed": False,
                "reason": str(exc),
            }
    if action not in CHANGED:
        # This prompt added nothing, but an earlier one may still be owed to
        # the supervisor: a relay that failed is retried on the next wake, and
        # a prompt IS a wake. The scan is the open entries already on disk, and
        # nothing is sent from here — `flush` decides, behind the prompt.
        if pending_relays(store, clock=clock)["due_now"]:
            outcome["delivery"] = request_delivery(environ, spawn=spawn)
        return outcome
    try:
        asked = request_supervisor(state_dir, environ=environ, spawn=spawn, clock=clock)
    except (IntakeError, OSError) as exc:
        # The project is recorded, which is the part that may not fail. A
        # supervisor that could not even be asked for is reported here, never
        # raised over the top of a record that succeeded.
        asked = {"requested": False, "reason": _text(
            str(exc), limit=200, label="reason", required=False
        )}
    outcome["supervisor"] = asked
    if action in CHANGED:
        # The arrival or amendment is durable; telling the supervisor about it
        # is the slow part and belongs behind the prompt, not inside it.
        outcome["delivery"] = request_delivery(environ, spawn=spawn)
    return outcome


def open_intakes(
    spool: Spool,
    *,
    clock: Callable[[], float] = time.time,
    limit: int = MAX_OPEN,
) -> dict[str, Any]:
    """Every intake still owing somebody something, read live.

    This is the first read of a replacement resident's first turn: whatever it
    does not find here, it does not know it owes.
    """
    rows = spool.open_entries(limit)
    return {
        "as_of_unix_ms": now_unix_ms(clock),
        "count": len(rows),
        "truncated": len(rows) >= limit,
        # What the fleet still owes this reader. A resident that reads its
        # open intakes and sees `undelivered.count` above zero knows some of
        # what it is looking at never reached it as a message.
        "undelivered": pending_relays(spool, clock=clock, limit=limit, rows=rows),
        "open": rows,
    }


def dismiss_machine_intakes(
    spool: Spool, *, clock: Callable[[], float] = time.time
) -> dict[str, Any]:
    """Retire only the kit's exact historical delivery-transport intakes.

    These entries were produced by Session Kit talking to itself. They have no
    human source awaiting a report, so dismissal never sends a message. The
    entry remains in retained history as reported instead of being deleted.
    """
    now = now_unix_ms(clock)
    dismissed = 0
    examined = 0
    with spool.locked():
        for msg_id in spool.entry_ids():
            entry = spool.read_entry(msg_id)
            if entry is None or entry["state"] == "reported":
                continue
            examined += 1
            if entry["origin"] != AUTO_ORIGIN or not machine_transport_prompt(
                entry.get("summary")
            ):
                continue
            entry["state"] = "reported"
            entry["reported_unix_ms"] = now
            entry["updated_unix_ms"] = now
            spool.write_entry(entry)
            dismissed += 1
    return {"dismissed": dismissed, "examined": examined, "messages_sent": 0}


def run(
    action: str,
    *,
    spool: Spool,
    deliver: Callable[..., Mapping[str, Any]],
    reply: Callable[..., Mapping[str, Any]],
    clock: Callable[[], float] = time.time,
    msg_id: str | None = None,
    source: str | None = None,
    intake_key: str | None = None,
    summary: str | None = None,
    terminal: int | None = None,
    title: str | None = None,
    text: str | None = None,
    cwd: str | None = None,
    branches: Sequence[str] = (),
    workers: Sequence[Mapping[str, Any]] = (),
    worker_plan: Sequence[Mapping[str, Any]] = (),
    source_event_id: str | None = None,
    analysis: str | None = None,
    scope: str | None = None,
    required_expertise: str | None = None,
    required_expertise_tags: Sequence[str] = (),
    risks: str | None = None,
    tests: str | None = None,
    manual_policy_exception: str | None = None,
    branch: str | None = None,
    duty_state: str | None = None,
    reporter_identity: str | None = None,
    idempotency_key: str | None = None,
    launcher: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    reconciler: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    payload: Mapping[str, Any] | None = None,
    state_dir: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    spawn: Callable[[Sequence[str]], None] = _spawn_detached,
) -> tuple[int, Any]:
    """Run one intake verb; returns (exit code, JSON-printable payload)."""
    if action not in ACTIONS:
        raise IntakeError(f"unknown intake verb {action!r}")
    if action == "open":
        return 0, open_intakes(spool, clock=clock)
    if action == "pending":
        return 0, pending_relays(spool, clock=clock)
    if action == "dismiss-machine":
        return 0, dismiss_machine_intakes(spool, clock=clock)
    if action == "record":
        return 0, record(
            spool,
            msg_id=msg_id or "",
            source=source or "",
            intake_key=intake_key,
            summary=summary,
            terminal=terminal,
            title=title,
            cwd=cwd,
            clock=clock,
        )
    if action == "flush":
        return 0, flush(
            spool,
            deliver=deliver,
            state_dir=state_dir if state_dir is not None else spool.state_dir,
            clock=clock,
        )
    if action == "from-hook":
        return 0, from_hook(
            payload or {},
            state_dir=state_dir if state_dir is not None else spool.state_dir,
            environ=environ or {},
            spool=spool,
            spawn=spawn,
            clock=clock,
        )
    if action == "duties":
        return 0, duties(spool, msg_id=msg_id, clock=clock)
    if action == "delegate":
        result = delegate(
            spool,
            msg_id=msg_id or "",
            branches=branches,
            workers=workers,
            launcher=launcher,
            reconciler=reconciler,
            deliver=deliver,
            state_dir=state_dir if state_dir is not None else spool.state_dir,
            clock=clock,
        )
        # A worker whose duty never landed is a launched session with nothing
        # to do. The record says so and so does the exit code.
        return (1 if result["undelivered"] else 0), result
    if action == "report":
        return 0, report(
            spool,
            msg_id=msg_id or "",
            branch=branch or "",
            state=duty_state or "",
            summary=summary or "",
            reporter_identity=reporter_identity or "",
            clock=clock,
        )
    if action == "retry":
        return 0, retry(
            spool,
            msg_id=msg_id or "",
            branch=branch or "",
            idempotency_key=idempotency_key or "",
            clock=clock,
        )
    if action == "reset":
        return 0, reset(
            spool,
            msg_id=msg_id or "",
            branch=branch or "",
            summary=summary or "",
            clock=clock,
        )
    if action == "cancel":
        return 0, cancel(
            spool,
            msg_id=msg_id or "",
            branch=branch or "",
            summary=summary or "",
            clock=clock,
        )
    if action == "preflight":
        return 0, preflight(
            spool,
            msg_id=msg_id or "",
            source_event_id=source_event_id,
            analysis=analysis or "",
            scope=scope or "",
            required_expertise=required_expertise or "",
            required_expertise_tags=required_expertise_tags,
            worker_plan=worker_plan,
            risks=risks or "",
            tests=tests or "",
            manual_policy_exception=manual_policy_exception or "",
            state_dir=state_dir if state_dir is not None else spool.state_dir,
            clock=clock,
        )
    if action == "ack":
        return acknowledge(
            spool,
            msg_id=msg_id or "",
            text=text or "",
            reply=reply,
            deliver=deliver,
            clock=clock,
        )
    return relay(
        spool,
        msg_id=msg_id or "",
        text=text or "",
        kind=PROGRESS if action == "progress" else COMPLETION,
        deliver=deliver,
        clock=clock,
    )
