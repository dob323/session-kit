"""Message identity and the frozen operator envelope.

Nothing here touches the filesystem or a provider; the ledger and both
delivery paths import these pure helpers so one envelope definition reaches
Claude and Codex byte for byte.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Callable

PROVIDERS = ("claude", "codex")
MSG_ID_RE = re.compile(r"\A[0-9a-f]{8}\Z")
UUID_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
THREAD_KEY_RE = re.compile(
    r"\A(claude|codex):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
# An idempotency key names a PURPOSE — "the standing brief for this
# supervisor" — not one attempt at it. It is also a filename in the ledger, so
# it starts with an alphanumeric and carries no separator: `.`, `..`, and any
# path of its own are unrepresentable rather than rejected later.
IDEMPOTENCY_KEY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
IDEMPOTENCY_KEY_RULE = (
    "an idempotency key must be 1-128 characters of A-Z a-z 0-9 . _ : - "
    "and start with a letter or digit"
)

ENVELOPE_SEPARATOR = "---"
# An FYI drops the reply instructions, so it has to say so outright. Silence
# reads as "answer me" to an agent that has just been woken.
FYI_LINE = "FYI only — no reply needed."
MAX_OPERATOR_TEXT = 8000
MAX_REPLY_TEXT = 4000
# One message, three truths about where it got to. `delivered-*` is proven by
# the target's own transcript; `landed-unconfirmed` is a message the sender
# handed to the target and whose arrival is simply not observable yet — a
# newborn or busy Claude session takes a queued message at its next turn
# boundary, which can be minutes away; everything else never reached it.
#
# The distinction is the whole point: a landed message is NOT a retryable
# failure. Treating one as failed is how a caller ends up delivering the same
# message five times and then declaring it undelivered.
DELIVERED_WOKE = "delivered-woke"
DELIVERED_MIDTURN = "delivered-midturn"
LANDED_UNCONFIRMED = "landed-unconfirmed"
REPLIED = "replied"
PRE_DISPATCH = "pre-dispatch"
IN_FLIGHT = "in-flight"
LANDED_UNCONFIRMED_DETAIL = (
    "the sender delivered it to this target; the target's own transcript has "
    "not shown it inside the check window"
)
TRANSCRIPT_RECEIPT_DETAIL = "message id found in the target's own transcript"
# Eight hex characters over a per-call random draw: short enough to type into
# `sp msg reply`, wide enough that a 500-record store never collides in
# practice. Uniqueness is still proven by the store, never assumed.
MSG_ID_ATTEMPTS = 64


class MessageError(RuntimeError):
    """A messaging operation refused to proceed."""


def valid_uuid(value: object) -> str:
    """Return the exact lowercase UUID, or "" when the value is not one."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    return candidate if UUID_RE.match(candidate) else ""


def valid_msg_id(value: object) -> str:
    """Return the exact 8-hex message id, or "" when the value is not one."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    return candidate if MSG_ID_RE.match(candidate) else ""


def valid_idempotency_key(value: object) -> str:
    """Return the exact idempotency key, or "" when the value is not one."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate if IDEMPOTENCY_KEY_RE.match(candidate) else ""


def landed(status: object) -> bool:
    """True when the message reached the target, whether or not that is proven.

    Delivery, an answer, and an unconfirmed landing are all arrivals. Only the
    statuses outside this set may ever be sent again.
    """
    if not isinstance(status, str):
        return False
    return status.startswith("delivered") or status in {LANDED_UNCONFIRMED, REPLIED}


def thread_key(provider: object, uuid: object) -> str:
    """Compose the `<provider>:<uuid>` key, refusing anything unexact.

    The key is also a filename in the ledger, so a value that is not one of
    the two providers plus one exact UUID never reaches the filesystem.
    """
    exact = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact:
        raise MessageError("a thread key needs provider claude|codex and an exact UUID")
    return f"{provider}:{exact}"


def valid_thread_key(value: object) -> str:
    """Return the exact thread key, or "" when the value is not one."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    return candidate if THREAD_KEY_RE.match(candidate) else ""


def split_thread_key(value: object) -> tuple[str, str]:
    """Return (provider, uuid) for an exact thread key."""
    key = valid_thread_key(value)
    if not key:
        raise MessageError("thread key must be <claude|codex>:<uuid>")
    provider, uuid = key.split(":", 1)
    return provider, uuid


def new_msg_id(
    created_unix_ms: int,
    *,
    taken: Callable[[str], bool] | None = None,
    entropy: Callable[[int], bytes] = os.urandom,
) -> str:
    """Mint an unused 8-hex message id from the clock plus fresh entropy.

    The timestamp only spreads ids across a run; uniqueness comes from
    ``entropy`` and is then proven against the store through ``taken``, so a
    coarse or repeated clock reading can never hand out a live id twice.
    """
    for _ in range(MSG_ID_ATTEMPTS):
        digest = hashlib.blake2b(
            str(int(created_unix_ms)).encode("ascii") + entropy(16), digest_size=8
        ).hexdigest()[:8]
        if taken is None or not taken(digest):
            return digest
    raise MessageError("cannot mint an unused message id")


def clean_operator_text(value: object, *, limit: int = MAX_OPERATOR_TEXT) -> str:
    """Bound an operator string and strip control characters except newline."""
    if not isinstance(value, str):
        raise MessageError("message text must be a string")
    text = "".join(
        character
        for character in value.replace("\r\n", "\n").replace("\r", "\n")
        if character == "\n" or character >= " " or character == "\t"
    ).strip()
    if not text:
        raise MessageError("message text is empty")
    return text[:limit]


def compose_envelope(msg_id: str, operator_text: str, *, fyi: bool = False) -> str:
    """Return the frozen envelope one target receives verbatim.

    ``fyi`` replaces the reply-instruction paragraph with a single line saying
    no reply is wanted: telling the reader how to reply would be an
    instruction to ignore, but saying nothing at all reads as "answer me".
    """
    exact = valid_msg_id(msg_id)
    if not exact:
        raise MessageError("an envelope needs an exact 8-hex message id")
    header = f"[session-kit operator message {exact}]"
    body = clean_operator_text(operator_text)
    if fyi:
        return f"{header}\n{FYI_LINE}\n{ENVELOPE_SEPARATOR}\n{body}"
    return (
        f"{header}\n"
        "From: the operator. When you have your answer, reply with EXACTLY "
        "this command:\n"
        f'  sp msg reply {exact} "your one-line answer"\n'
        "Do not reply via SendMessage — the sending process is ephemeral and "
        "replies to it\n"
        "misdeliver. Then continue your prior work.\n"
        f"{ENVELOPE_SEPARATOR}\n"
        f"{body}"
    )


def preview(text: object, limit: int = 60) -> str:
    """One-line, bounded preview of an operator message for list views."""
    if not isinstance(text, str):
        return ""
    flattened = " ".join(text.split())
    return flattened[:limit]


def target_row(
    *,
    key: str,
    provider: str,
    shpool_id: str | None,
    uuid: str,
    terminal_number: int | None,
    title: str,
    agent_status: str,
) -> dict[str, object]:
    """The exact resolve-shape target row, with no delivery fields.

    ``agent_status`` is the state the session was in when it was resolved, so
    a confirmation table can show who is idle and who is mid-task before the
    operator commits, and a receipt still says what was interrupted afterwards.
    """
    return {
        "thread_key": key,
        "provider": provider,
        "shpool_id": shpool_id,
        "uuid": uuid,
        "terminal_number": terminal_number,
        "title": title,
        "agent_status": agent_status,
    }


def dispatch_row(row: dict[str, object], updated_unix_ms: int) -> dict[str, object]:
    """Extend a resolve row with the undelivered delivery fields.

    ``pre-dispatch`` is the only state, besides an explicit ``unreachable``,
    that a keyed repeat may reserve for another attempt. The reservation is
    changed to ``in-flight`` under the same ledger lock before provider code
    runs, so a concurrent repeat can never dispatch the same target too.
    """
    record = dict(row)
    record.update(
        {
            "method": None,
            "status": PRE_DISPATCH,
            "detail": "delivery not attempted",
            "updated_unix_ms": int(updated_unix_ms),
        }
    )
    return record


def set_status(
    row: dict[str, object],
    *,
    status: str,
    detail: str,
    method: str | None = None,
    updated_unix_ms: int,
) -> dict[str, object]:
    """Record one delivery outcome on a target row, in place."""
    row["status"] = status
    row["detail"] = detail
    if method is not None:
        row["method"] = method
    row["updated_unix_ms"] = int(updated_unix_ms)
    return row
