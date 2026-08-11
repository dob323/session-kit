"""The `msg` verbs: resolve, send, report, reply, list, unread-count.

Every verb takes its evidence as an argument — the inventory, the environment,
the state directory, the ledger — so the facade owns process concerns and this
module stays testable without a provider, a socket, or a real home.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import claude_send, codex_send
from .envelope import (
    DELIVERED_MIDTURN,
    DELIVERED_WOKE,
    IDEMPOTENCY_KEY_RULE,
    IN_FLIGHT,
    LANDED_UNCONFIRMED,
    LANDED_UNCONFIRMED_DETAIL,
    MAX_REPLY_TEXT,
    MessageError,
    PRE_DISPATCH,
    TRANSCRIPT_RECEIPT_DETAIL,
    compose_envelope,
    dispatch_row,
    new_msg_id,
    preview,
    set_status,
    target_row,
    thread_key,
    valid_idempotency_key,
    valid_msg_id,
    valid_thread_key,
    valid_uuid,
)
from .ledger import Ledger, THREAD_TAIL_LINES, now_unix_ms

MESSAGING_OFF = "SESSION_KIT_MSG"
CODEX_OFF = "SESSION_KIT_MSG_CODEX"
CLAUDE_OFF = "SESSION_KIT_MSG_CLAUDE"
DELIVERABLE_PROVIDERS = ("claude", "codex")
# "Truly unoccupied" is wider than the word idle: a Codex thread that has
# finished and left an optional follow-up open is nobody's work in progress.
# A session already waiting on the operator is a different case — it is
# occupied by a question of its own, so `idle` leaves it alone and any send
# that does reach it says so on the receipt (maintainer decision, 2026-08-07).
IDLE_STATUSES = ("idle", "reply optional")
# What a numeric selection can look like before the grammar reads it.
SELECTION_RE = re.compile(r"[0-9][0-9,\- ]*")
DIGITS_RE = re.compile(r"[0-9]+")
WAITING_ON_OPERATOR_STATUS = "needs your reply"
WAITING_ON_OPERATOR_NOTE = "this session is waiting on the operator's reply"


def _note_waiting(detail: str, waiting: bool) -> str:
    return f"{detail} ({WAITING_ON_OPERATOR_NOTE})" if waiting else detail


def _waiting_on_operator(row: Mapping[str, Any]) -> bool:
    return row.get("agent_status") == WAITING_ON_OPERATOR_STATUS


def _working(row: Mapping[str, Any]) -> bool:
    """True only for a session with a turn actually in progress.

    A session waiting on the operator is parked, not working: a message wakes
    it, so its receipt reads delivered-woke rather than mid-turn.
    """
    return (
        row.get("agent_status") not in IDLE_STATUSES and not _waiting_on_operator(row)
    )


# ---- target selection --------------------------------------------------


def _row_uuid(row: Mapping[str, Any]) -> str:
    identity = row.get("identity")
    if not isinstance(identity, Mapping):
        return ""
    return valid_uuid(identity.get("uuid"))


def _row_key(row: Mapping[str, Any]) -> str:
    provider = row.get("provider")
    uuid = _row_uuid(row)
    if provider not in DELIVERABLE_PROVIDERS or not uuid:
        return ""
    return f"{provider}:{uuid}"


def _row_shpool_id(row: Mapping[str, Any]) -> str | None:
    value = row.get("shpool_id_raw") or row.get("shpool_id")
    return value if isinstance(value, str) and value else None


def _row_terminal_number(row: Mapping[str, Any]) -> int | None:
    value = row.get("terminal_number")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _row_title(row: Mapping[str, Any]) -> str:
    for field in ("display_title", "title", "native_title"):
        value = row.get(field)
        if isinstance(value, str) and value:
            return value
    return str(row.get("provider") or "session")


def _skip_reason(row: Mapping[str, Any]) -> str:
    provider = row.get("provider")
    if provider not in DELIVERABLE_PROVIDERS:
        return f"provider {provider or 'unknown'} has no message path"
    return "session has no exact conversation UUID yet"


def _skipped_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "shpool_id": _row_shpool_id(row),
        "terminal_number": _row_terminal_number(row),
        "provider": row.get("provider"),
        "title": _row_title(row),
        "reason": _skip_reason(row),
    }


def _matches_selector(
    row: Mapping[str, Any], selector: str, *, numbered: bool
) -> bool:
    if selector.startswith("key:"):
        return _row_key(row) == valid_thread_key(selector[len("key:") :])
    # ASCII digits only. `str.isdigit()` also accepts superscripts, which
    # raise inside int(), and other scripts' digits, which would quietly
    # address a session the operator never named.
    if DIGITS_RE.fullmatch(selector):
        number = int(selector)
        if numbered:
            return _row_terminal_number(row) == number
        return row.get("row") == number
    return (row.get("shpool_id_raw") or row.get("shpool_id")) == selector


def parse_keys_selector(selector: str) -> list[str]:
    """The exact thread keys named by ``keys:<k1>,<k2>,…``, in the given order.

    Every key is validated before anything looks at a session: a follow-up
    addressed to a list must fail on a malformed key rather than quietly
    address a shorter list than the operator wrote.
    """
    raw = [part.strip() for part in selector[len("keys:") :].split(",")]
    named = [part for part in raw if part]
    if not named:
        raise MessageError("keys: needs at least one thread key")
    keys: list[str] = []
    for candidate in named:
        exact = valid_thread_key(candidate)
        if not exact:
            raise MessageError(
                f"{candidate!r} is not a thread key (<claude|codex>:<uuid>)"
            )
        if exact not in keys:
            keys.append(exact)
    return keys


def _rows_for_numbers(
    sessions: Sequence[Mapping[str, Any]], selector: str
) -> list[Mapping[str, Any]] | None:
    """The rows a "1,3-5" selection names, or None when this is not one.

    The grammar is the kit's one selection grammar, so the numbers a person
    types at `sp msg` mean what the same numbers mean in the picker. A single
    number stays on the old exact-selector path, where an unknown number is
    an error rather than an empty list.
    """
    if not SELECTION_RE.fullmatch(selector) or DIGITS_RE.fullmatch(selector):
        return None
    # The grammar lives with the inventory, which owns terminal numbers. The
    # import is local so this package keeps working on its own in every other
    # verb, and a selection simply is not offered if it is not importable.
    from sessionkit_inventory.common import (
        CollectionError,
        parse_number_selection,
    )

    numbered: dict[int, Mapping[str, Any]] = {}
    for row in sessions:
        number = _row_terminal_number(row)
        if number is not None:
            numbered[number] = row
    if not numbered:
        return None
    try:
        chosen = parse_number_selection(selector, max(numbered))
    except CollectionError as reason:
        raise MessageError(str(reason)) from reason
    absent = [number for number in chosen if number not in numbered]
    if absent:
        listed = ", ".join(str(number) for number in absent)
        raise MessageError(f"no live session is numbered {listed}")
    rows = [numbered[number] for number in chosen if number in numbered]
    if not rows:
        raise MessageError(f"no live session matches {selector!r}")
    return rows


def _departed_row(key: str) -> dict[str, Any]:
    """A named key with no live session behind it any more."""
    provider = key.split(":", 1)[0]
    return {
        "shpool_id": None,
        "terminal_number": None,
        "provider": provider,
        "title": key,
        "reason": "session is no longer live",
    }


def _rows_for_keys(
    sessions: Sequence[Mapping[str, Any]], keys: Sequence[str]
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    """Resolve named keys in the order given, reporting the ones that are gone.

    A key that no longer has a live session is skipped rather than fatal: one
    exited session must not cancel a follow-up to everyone else. Nothing
    matching at all is still an error.
    """
    by_key: dict[str, list[Mapping[str, Any]]] = {}
    for row in sessions:
        row_key = _row_key(row)
        if row_key:
            by_key.setdefault(row_key, []).append(row)
    chosen: list[Mapping[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for key in keys:
        matches = by_key.get(key, [])
        if len(matches) > 1:
            raise MessageError(f"{key!r} matches more than one live session")
        if not matches:
            missing.append(_departed_row(key))
            continue
        chosen.append(matches[0])
    if not chosen:
        raise MessageError("none of the named sessions are live any more")
    return chosen, missing


def select_rows(
    inventory: Mapping[str, Any], selector: str
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    """Return the deliverable session rows for a selector, plus what was skipped."""
    if not isinstance(selector, str) or not selector.strip():
        raise MessageError("a target is required: all, idle, a number, or a session id")
    selector = selector.strip()
    # `a` is what the picker takes for "all", so `sp msg a` means it too, and
    # a person who shouts ALL means the same thing as one who does not.
    if selector.casefold() in {"a", "all", "idle"}:
        selector = "all" if selector.casefold() in {"a", "all"} else "idle"
    sessions = [
        row for row in inventory.get("sessions", ()) if isinstance(row, Mapping)
    ]
    departed: list[dict[str, Any]] = []
    if selector in {"all", "idle"}:
        chosen = sessions
    elif selector.startswith("keys:"):
        chosen, departed = _rows_for_keys(sessions, parse_keys_selector(selector))
    elif (selected := _rows_for_numbers(sessions, selector)) is not None:
        chosen = selected
    else:
        numbered = any(_row_terminal_number(row) for row in sessions)
        chosen = [
            row for row in sessions if _matches_selector(row, selector, numbered=numbered)
        ]
        if not chosen:
            raise MessageError(f"no live session matches {selector!r}")
        if len(chosen) > 1:
            raise MessageError(f"{selector!r} matches more than one live session")
    targets: list[Mapping[str, Any]] = []
    skipped: list[dict[str, Any]] = list(departed)
    for row in chosen:
        if selector == "idle" and row.get("agent_status") not in IDLE_STATUSES:
            continue
        if not _row_key(row):
            skipped.append(_skipped_row(row))
            continue
        targets.append(row)
    return targets, skipped


def _target_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One session as a target row, carrying its state at resolve time."""
    return target_row(
        key=_row_key(row),
        provider=str(row.get("provider")),
        shpool_id=_row_shpool_id(row),
        uuid=_row_uuid(row),
        terminal_number=_row_terminal_number(row),
        title=_row_title(row),
        agent_status=str(row.get("agent_status") or "unknown"),
    )


def resolve(
    inventory: Mapping[str, Any],
    selector: str,
    *,
    state_dir: Path,
    registry: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """The public `msg resolve` payload."""
    rows, skipped = select_rows(inventory, selector)
    targets = [_target_row(row) for row in rows]
    unreachable = sum(
        1 for row in rows if not _looks_reachable(row, state_dir=state_dir, registry=registry)
    )
    return {
        "targets": targets,
        "counts": {
            "claude": sum(1 for row in rows if row.get("provider") == "claude"),
            "codex": sum(1 for row in rows if row.get("provider") == "codex"),
            "unreachable_hint": unreachable,
        },
        "skipped": skipped,
    }


def _looks_reachable(
    row: Mapping[str, Any],
    *,
    state_dir: Path,
    registry: Mapping[str, Mapping[str, str]],
) -> bool:
    """A cheap pre-flight hint, never a delivery decision.

    Codex needs a live app-server socket of its own; Claude needs to be in the
    agent registry. Both are re-checked at send time — this only lets the
    confirmation prompt say how many targets look out of reach.
    """
    if row.get("provider") == "codex":
        return codex_send.live_socket(
            codex_send.socket_path(state_dir, _row_shpool_id(row))
        )
    return _row_uuid(row) in registry


# ---- send --------------------------------------------------------------


def _switch_off(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip() == "0"


def _commit(
    ledger: Ledger,
    msg_id: str,
    key: str,
    *,
    status: str,
    detail: str,
    method: str | None,
    updated_unix_ms: int,
    waiting_on_operator: bool = False,
) -> None:
    """Persist one target's outcome without overwriting an arrived reply.

    Every write for a session that was waiting on the operator carries the
    note, so
    the receipt says so whichever outcome the target ends up with.
    """

    def mutate(record: dict[str, Any]) -> dict[str, Any]:
        for row in record.get("targets", []):
            if not isinstance(row, dict) or row.get("thread_key") != key:
                continue
            if row.get("status") == "replied":
                continue
            set_status(
                row,
                status=status,
                detail=_note_waiting(detail, waiting_on_operator),
                method=method,
                updated_unix_ms=updated_unix_ms,
            )
        return record

    ledger.update_send(msg_id, mutate)


def send(
    *,
    inventory: Mapping[str, Any],
    selector: str,
    text: str,
    fyi: bool,
    state_dir: Path,
    environ: Mapping[str, str],
    home: Path,
    ledger: Ledger,
    clock: Callable[[], float] = time.time,
    registry_entries: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    codex_deliver: Callable[..., Mapping[str, Any]] = codex_send.deliver,
    sender_runner: claude_send.Runner = claude_send.default_runner,
    verify: Callable[..., Mapping[str, bool]] = claude_send.verify_delivery,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Deliver one operator message to every selected session, and record it.

    ``idempotency_key`` names a repeatable purpose. A second send under the
    same key resumes the message id that purpose already has instead of
    minting a second message, and any target the ledger already shows as
    landed is left alone — so a caller that retries because it could not
    prove delivery never delivers twice.
    """
    if _switch_off(environ, MESSAGING_OFF):
        raise MessageError("mass messaging is switched off (SESSION_KIT_MSG=0)")
    read_registry = registry_entries or (
        lambda: claude_send.registry_entries(environ)
    )
    key = ""
    if idempotency_key:
        key = valid_idempotency_key(idempotency_key)
        if not key:
            raise MessageError(IDEMPOTENCY_KEY_RULE)
    rows, skipped = select_rows(inventory, selector)
    if not rows:
        raise MessageError(f"no live Claude or Codex session matches {selector!r}")

    created = now_unix_ms(clock)
    ledger.ensure()
    by_key = {_row_key(row): row for row in rows}
    with ledger.locked():
        record, resumed = _claimed_record(
            ledger, key, text=text, fyi=fyi, created=created, by_key=by_key
        )
        pending, suppressed = _reserve_targets(record, by_key, created)
        ledger.write_send(record)
        if key:
            ledger.claim_key(key, str(record["msg_id"]))
    msg_id = str(record["msg_id"])
    envelope = compose_envelope(msg_id, text, fyi=fyi)
    _reconcile_suppressed_claude(
        [(key_, row) for key_, row in by_key.items() if key_ in suppressed],
        msg_id=msg_id,
        home=home,
        ledger=ledger,
        clock=clock,
        read_registry=read_registry,
        verify=verify,
    )
    _deliver_codex(
        [(key_, row) for key_, row in pending if row.get("provider") == "codex"],
        msg_id=msg_id,
        envelope=envelope,
        display_text=text,
        state_dir=state_dir,
        environ=environ,
        ledger=ledger,
        clock=clock,
        codex_deliver=codex_deliver,
    )
    _deliver_claude(
        [(key_, row) for key_, row in pending if row.get("provider") == "claude"],
        msg_id=msg_id,
        envelope=envelope,
        display_text=text,
        environ=environ,
        home=home,
        ledger=ledger,
        clock=clock,
        read_registry=read_registry,
        sender_runner=sender_runner,
        verify=verify,
        resumed=resumed,
    )

    with ledger.locked():
        ledger.prune(now_unix_ms(clock))
    final = ledger.read_send(msg_id) or record
    return {
        "msg_id": msg_id,
        "fyi": bool(fyi),
        "idempotency_key": key or None,
        "resumed": resumed,
        "suppressed": suppressed,
        "skipped": skipped,
        "targets": final.get("targets", []),
    }


def _claimed_record(
    ledger: Ledger,
    key: str,
    *,
    text: str,
    fyi: bool,
    created: int,
    by_key: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """The send record this call belongs to, and whether it already existed.

    A key resumes its stored message only while the message is still the same
    one: different words, or a different reply expectation, are a different
    thing to say and get their own id rather than arriving under a header the
    recipient has already read.
    """
    claimed_id = ledger.claimed_msg_id_for_key(key) if key else None
    stored_id = ledger.msg_id_for_key(key) if key else None
    if claimed_id and not stored_id:
        raise MessageError(
            "the idempotency key has a durable claim but no stored send; "
            "delivery outcome is unknown and will not be retried"
        )
    stored = ledger.read_send(stored_id) if stored_id else None
    if (
        stored is not None
        and stored.get("operator_text") == text
        and bool(stored.get("fyi")) == bool(fyi)
    ):
        stored["targets"] = _merged_targets(stored, by_key, created)
        return stored, True
    msg_id = new_msg_id(created, taken=ledger.has_send)
    return (
        {
            "msg_id": msg_id,
            "created_unix_ms": created,
            "operator_text": text,
            "fyi": bool(fyi),
            "idempotency_key": key or None,
            "targets": [_initial_target(row, created) for row in by_key.values()],
        },
        False,
    )


def _merged_targets(
    record: Mapping[str, Any],
    by_key: Mapping[str, Mapping[str, Any]],
    created: int,
) -> list[dict[str, Any]]:
    """Stored target rows, plus a fresh row for anyone newly in range."""
    targets = [row for row in record.get("targets", ()) if isinstance(row, dict)]
    known = {row.get("thread_key") for row in targets}
    for key, row in by_key.items():
        if key not in known:
            targets.append(_initial_target(row, created))
    return targets


def _reserve_targets(
    record: Mapping[str, Any],
    by_key: Mapping[str, Mapping[str, Any]],
    updated_unix_ms: int,
) -> tuple[list[tuple[str, Mapping[str, Any]]], list[str]]:
    """Reserve retryable targets while suppressing every uncertain attempt.

    This runs while ``send`` holds the ledger lock. Only a target explicitly
    recorded as not yet dispatched or unreachable may be tried. In-flight,
    unknown, failed/ambiguous, replied, and every landed state are suppressed:
    none proves that another provider call would be safe.
    """
    stored: dict[Any, dict[str, Any]] = {
        row.get("thread_key"): row
        for row in record.get("targets", ())
        if isinstance(row, dict)
    }
    pending: list[tuple[str, Mapping[str, Any]]] = []
    suppressed: list[str] = []
    for key, row in by_key.items():
        previous = stored.get(key)
        if previous is None:
            suppressed.append(key)
            continue
        status = previous.get("status")
        if status not in {PRE_DISPATCH, "unreachable"}:
            suppressed.append(key)
            continue
        set_status(
            previous,
            status=IN_FLIGHT,
            detail=_note_waiting(
                codex_send.UNKNOWN_OUTCOME_DETAIL, _waiting_on_operator(row)
            ),
            method=None,
            updated_unix_ms=updated_unix_ms,
        )
        pending.append((key, row))
    return pending, suppressed


def _initial_target(row: Mapping[str, Any], created: int) -> dict[str, Any]:
    """One undelivered target row, already carrying its waiting note."""
    record = dispatch_row(_target_row(row), created)
    record["detail"] = _note_waiting(str(record["detail"]), _waiting_on_operator(row))
    return record


def _record_thread_line(
    ledger: Ledger,
    key: str,
    *,
    msg_id: str,
    display_text: str,
    via: str,
    clock: Callable[[], float],
) -> None:
    # The thread shows the operator what THEY said. The envelope's reply
    # instructions are transport, delivered to the agent and kept out of the
    # conversation view.
    ledger.append_thread(
        key,
        ts_unix_ms=now_unix_ms(clock),
        direction="out",
        msg_id=msg_id,
        text=display_text,
        via=via,
    )


def _deliver_codex(
    pairs: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    msg_id: str,
    envelope: str,
    display_text: str,
    state_dir: Path,
    environ: Mapping[str, str],
    ledger: Ledger,
    clock: Callable[[], float],
    codex_deliver: Callable[..., Mapping[str, Any]],
) -> None:
    disabled = _switch_off(environ, CODEX_OFF)
    for key, row in pairs:
        waiting = _waiting_on_operator(row)
        if disabled:
            _commit(
                ledger,
                msg_id,
                key,
                status="unreachable",
                detail="Codex delivery is switched off (SESSION_KIT_MSG_CODEX=0)",
                method=None,
                updated_unix_ms=now_unix_ms(clock),
                waiting_on_operator=waiting,
            )
            continue
        path = codex_send.socket_path(state_dir, _row_shpool_id(row))
        if not codex_send.live_socket(path):
            _commit(
                ledger,
                msg_id,
                key,
                status="unreachable",
                detail=codex_send.NO_SOCKET_DETAIL,
                method=None,
                updated_unix_ms=now_unix_ms(clock),
                waiting_on_operator=waiting,
            )
            continue
        # The target was atomically reserved as in-flight before this provider
        # path began. A crash here leaves a suppressible unknown outcome.
        outcome = codex_deliver(uuid=_row_uuid(row), envelope=envelope, path=path)
        method = outcome.get("method")
        _commit(
            ledger,
            msg_id,
            key,
            status=str(outcome.get("status", "failed")),
            detail=str(outcome.get("detail", "")),
            method=str(method) if method else None,
            updated_unix_ms=now_unix_ms(clock),
            waiting_on_operator=waiting,
        )
        _record_thread_line(
            ledger,
            key,
            msg_id=msg_id,
            display_text=display_text,
            via=str(method or "codex-app-server"),
            clock=clock,
        )


def _deliver_claude(
    pairs: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    msg_id: str,
    envelope: str,
    display_text: str,
    environ: Mapping[str, str],
    home: Path,
    ledger: Ledger,
    clock: Callable[[], float],
    read_registry: Callable[[], Sequence[Mapping[str, Any]]],
    sender_runner: claude_send.Runner,
    verify: Callable[..., Mapping[str, bool]],
    resumed: bool = False,
) -> None:
    if not pairs:
        return
    if _switch_off(environ, CLAUDE_OFF):
        for key, row in pairs:
            _commit(
                ledger,
                msg_id,
                key,
                status="unreachable",
                detail="Claude delivery is switched off (SESSION_KIT_MSG_CLAUDE=0)",
                method=None,
                updated_unix_ms=now_unix_ms(clock),
                waiting_on_operator=_waiting_on_operator(row),
            )
        return
    registry = claude_send.registry_index(read_registry())
    deliverable: list[dict[str, Any]] = []
    for key, row in pairs:
        uuid = _row_uuid(row)
        entry = registry.get(uuid)
        if entry is None:
            _commit(
                ledger,
                msg_id,
                key,
                status="unreachable",
                detail=claude_send.NOT_REGISTERED_DETAIL,
                method=None,
                updated_unix_ms=now_unix_ms(clock),
                waiting_on_operator=_waiting_on_operator(row),
            )
            continue
        deliverable.append(
            {
                "thread_key": key,
                "uuid": uuid,
                "cwd": entry.get("cwd", ""),
                "display_name": entry.get("name", ""),
                "busy": _working(row),
                "waiting_on_operator": _waiting_on_operator(row),
            }
        )
    if not deliverable:
        return
    if resumed:
        deliverable = _drop_already_landed(
            deliverable,
            msg_id=msg_id,
            home=home,
            ledger=ledger,
            clock=clock,
            verify=verify,
        )
        if not deliverable:
            return
    instructions = claude_send.compose_instructions(deliverable, envelope, msg_id)
    result = claude_send.run_sender(
        instructions,
        model=claude_send.sender_model(environ),
        cwd=home,
        runner=sender_runner,
    )
    report = claude_send.sender_report(result.get("rows") or [])
    found = verify(deliverable, msg_id=msg_id, home=home)
    for target in deliverable:
        key = str(target["thread_key"])
        status, detail = _claude_outcome(
            target,
            delivered=bool(found.get(key)),
            report=report,
            sender_ok=bool(result.get("ok")),
            sender_detail=str(result.get("detail", "")),
        )
        _commit(
            ledger,
            msg_id,
            key,
            status=status,
            detail=detail,
            method="headless-send",
            updated_unix_ms=now_unix_ms(clock),
            waiting_on_operator=bool(target["waiting_on_operator"]),
        )
        _record_thread_line(
            ledger,
            key,
            msg_id=msg_id,
            display_text=display_text,
            via="headless-send",
            clock=clock,
        )


def _reconcile_suppressed_claude(
    pairs: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    msg_id: str,
    home: Path,
    ledger: Ledger,
    clock: Callable[[], float],
    read_registry: Callable[[], Sequence[Mapping[str, Any]]],
    verify: Callable[..., Mapping[str, bool]],
) -> None:
    """Upgrade a suppressed unknown when the exact transcript now proves it.

    This is observation only. The target remains suppressed regardless of the
    transcript result, so an absent receipt can never turn into another send.
    """
    claude_pairs = [
        (key, row) for key, row in pairs if row.get("provider") == "claude"
    ]
    if not claude_pairs:
        return
    registry = claude_send.registry_index(read_registry())
    candidates: list[dict[str, Any]] = []
    for key, row in claude_pairs:
        entry = registry.get(_row_uuid(row))
        if entry is None:
            continue
        candidates.append(
            {
                "thread_key": key,
                "uuid": _row_uuid(row),
                "cwd": entry.get("cwd", ""),
                "display_name": entry.get("name", ""),
                "busy": _working(row),
                "waiting_on_operator": _waiting_on_operator(row),
            }
        )
    if candidates:
        _drop_already_landed(
            candidates,
            msg_id=msg_id,
            home=home,
            ledger=ledger,
            clock=clock,
            verify=verify,
        )


def _drop_already_landed(
    deliverable: Sequence[Mapping[str, Any]],
    *,
    msg_id: str,
    home: Path,
    ledger: Ledger,
    clock: Callable[[], float],
    verify: Callable[..., Mapping[str, bool]],
) -> list[dict[str, Any]]:
    """Read the transcripts once before a repeat, and send to nobody who has it.

    The ledger suppresses a target it already recorded as landed; this catches
    the other order — a first attempt whose receipt never arrived inside its
    window, and whose message has since appeared in the target's own record.
    """
    found = verify(deliverable, msg_id=msg_id, home=home, deadline_seconds=0.0)
    remaining: list[dict[str, Any]] = []
    for target in deliverable:
        key = str(target["thread_key"])
        if not found.get(key):
            remaining.append(dict(target))
            continue
        _commit(
            ledger,
            msg_id,
            key,
            status=_delivered_status(target),
            detail=TRANSCRIPT_RECEIPT_DETAIL,
            method="headless-send",
            updated_unix_ms=now_unix_ms(clock),
            waiting_on_operator=bool(target.get("waiting_on_operator")),
        )
    return remaining


def _delivered_status(target: Mapping[str, Any]) -> str:
    return DELIVERED_MIDTURN if target.get("busy") else DELIVERED_WOKE


def _claude_outcome(
    target: Mapping[str, Any],
    *,
    delivered: bool,
    report: Mapping[str, Mapping[str, Any]],
    sender_ok: bool,
    sender_detail: str,
) -> tuple[str, str]:
    """The target's transcript decides; the sender's claim only explains a miss.

    A sender that reported this exact target delivered, with no transcript
    receipt yet, is the one case that is neither proof nor failure: the
    message is in the target's queue and a newborn or busy session takes it at
    its next turn boundary. That is ``landed-unconfirmed`` — never retried,
    never counted as delivered.
    """
    if delivered:
        return _delivered_status(target), TRANSCRIPT_RECEIPT_DETAIL
    claim = report.get(str(target.get("display_name", "")))
    if not sender_ok:
        return "failed", sender_detail or "sender did not complete"
    if claim is None:
        return "ambiguous", "sender did not report this target"
    if not claim.get("send_ok"):
        reason = str(claim.get("reason") or "sender skipped this target")
        return "ambiguous", reason[:200]
    return LANDED_UNCONFIRMED, LANDED_UNCONFIRMED_DETAIL


# ---- report, reply, list, unread ---------------------------------------


def report(
    ledger: Ledger, msg_id: str | None = None, key: str | None = None
) -> dict[str, Any]:
    """One send with each target's thread tail. Reading changes nothing.

    Reading used to clear the unread marks, which made the picker cue lie:
    the Phase 2 console polls this every couple of seconds, so a reply was
    marked read before anyone had seen it. Clearing is now its own deliberate
    act — ``mark-read``.

    An empty store is a valid state, not a failure: the console polls before
    the first send exists, so that answers with a null id rather than an error.
    A ``key`` that has never sent anything answers the same way: a caller
    asking what one purpose delivered is polling, not naming a known message.
    """
    if key and not msg_id:
        exact_key = valid_idempotency_key(key)
        if not exact_key:
            raise MessageError(IDEMPOTENCY_KEY_RULE)
        claimed = ledger.msg_id_for_key(exact_key)
        if not claimed:
            return {"msg_id": None, "send": None, "threads": {}}
        msg_id = claimed
    chosen = valid_msg_id(msg_id) if msg_id else ledger.newest_send_id()
    if msg_id and not chosen:
        raise MessageError("message id must be 8 hex characters")
    if not chosen:
        if msg_id:
            raise MessageError(f"no stored send {msg_id}")
        return {"msg_id": None, "send": None, "threads": {}}
    record = ledger.read_send(chosen)
    if record is None:
        raise MessageError(f"no stored send {chosen}")
    unread = set(ledger.unread_keys())
    threads: dict[str, Any] = {}
    for row in record.get("targets", []):
        if not isinstance(row, Mapping):
            continue
        key = valid_thread_key(row.get("thread_key"))
        if not key or key in threads:
            continue
        threads[key] = {
            "unread": key in unread,
            "lines": ledger.read_thread(key, THREAD_TAIL_LINES),
        }
    return {"msg_id": chosen, "send": record, "threads": threads}


def mark_read(ledger: Ledger, thread: str) -> dict[str, Any]:
    """Clear one thread's unread marker; already-clear is a success, not an error."""
    key = valid_thread_key(thread)
    if not key:
        raise MessageError("mark-read needs a thread key (<claude|codex>:<uuid>)")
    was_unread = key in set(ledger.unread_keys())
    ledger.clear_unread(key)
    return {"thread_key": key, "cleared": was_unread}


def reply(
    ledger: Ledger,
    *,
    msg_id: str,
    text: str,
    caller: Mapping[str, Any],
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Record one agent's answer, keeping unsolicited text rather than losing it."""
    key = thread_key(caller.get("provider"), caller.get("uuid"))
    body = str(text).strip()
    if not body:
        raise MessageError("a reply needs text")
    body = body[:MAX_REPLY_TEXT]
    stamp = now_unix_ms(clock)
    exact = valid_msg_id(msg_id)
    record = ledger.read_send(exact) if exact else None
    targeted = bool(
        record
        and any(
            isinstance(row, Mapping) and row.get("thread_key") == key
            for row in record.get("targets", [])
        )
    )
    if not targeted:
        reason = (
            f"no stored send {msg_id}"
            if record is None
            else "this session was not a target of that message"
        )
        ledger.append_thread(
            key,
            ts_unix_ms=stamp,
            direction="in",
            msg_id=msg_id,
            text=body,
            via="unsolicited",
        )
        ledger.mark_unread(key)
        return {
            "ok": False,
            "msg_id": msg_id,
            "thread_key": key,
            "reason": reason,
            "kept": "unsolicited",
        }
    ledger.append_thread(
        key,
        ts_unix_ms=stamp,
        direction="in",
        msg_id=exact,
        text=body,
        via="reply",
    )
    ledger.mark_unread(key)

    def mutate(stored: dict[str, Any]) -> dict[str, Any]:
        for row in stored.get("targets", []):
            if isinstance(row, dict) and row.get("thread_key") == key:
                set_status(
                    row,
                    status="replied",
                    detail=preview(body, 200),
                    updated_unix_ms=stamp,
                )
        return stored

    ledger.update_send(exact, mutate)
    return {"ok": True, "msg_id": exact, "thread_key": key}


def list_sends(ledger: Ledger, limit: int = 50) -> dict[str, Any]:
    return {"sends": ledger.list_sends(limit)}


def unread_count(ledger: Ledger) -> dict[str, int]:
    return {"count": ledger.unread_count()}
