"""Attention-queue projection and Codex event synthesis."""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from .store import (
    EventStore,
    _atomic_write,
    _read_bounded,
    event_record,
    thread_key,
    valid_thread_key,
)

# `lib/session_inventory.py` imports this module as `_events` and reaches
# `_events.mark_seen` for `sp msg queue --mark-seen`; the redundant alias marks
# the re-export as deliberate so it is not mistaken for an unused import.
from .store import mark_seen as mark_seen


STALE_AFTER_MS = 20 * 60 * 1000
LIVE_INVENTORY_MAX_AGE_MS = 120 * 1000
MAX_SYNTH_STATE_BYTES = 4 * 1024 * 1024
BUCKET_ORDER = {
    "needs_you": 0,
    "finished_unseen": 1,
    "working": 2,
    "idle": 3,
}


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(
        " " if ord(character) < 32 else character for character in value
    )
    return " ".join(text.split())[:limit]


def _row_key(row: Mapping[str, Any]) -> str:
    identity = row.get("identity")
    if not isinstance(identity, Mapping):
        return ""
    try:
        return thread_key(row.get("provider"), identity.get("uuid"))
    except ValueError:
        return ""


def _status_kind(row: Mapping[str, Any]) -> str:
    if row.get("needs_you") is True:
        return "needs"
    status = _clean_text(row.get("agent_status"), 60).casefold()
    if status == "needs your reply":
        return "needs"
    if status == "idle":
        return "idle"
    # Unknown provider status values are deliberately active, never idle.
    return "working"


def _fingerprint(row: Mapping[str, Any]) -> str:
    identity = row.get("identity")
    uuid = identity.get("uuid") if isinstance(identity, Mapping) else None
    fields = {
        "agent_status": row.get("agent_status"),
        "availability": row.get("availability"),
        "provider": row.get("provider"),
        "recent_output_at_unix_ms": row.get("recent_output_at_unix_ms"),
        "terminal_number": row.get("terminal_number"),
        "title": row.get("title"),
        "uuid": uuid,
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)


def _initial_change_ms(row: Mapping[str, Any], now_ms: int) -> int:
    evidence = [
        value
        for value in (
            _integer(row.get("started_at_unix_ms")),
            _integer(row.get("recent_output_at_unix_ms")),
        )
        if value is not None and value <= now_ms
    ]
    return max(evidence, default=now_ms)


def _inventory_is_live(inventory: Mapping[str, Any], now_ms: int) -> bool:
    if inventory.get("source") != "live" or inventory.get("stale") is not False:
        return False
    generated_at = inventory.get("generated_at")
    if not isinstance(generated_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    generated_ms = int(parsed.astimezone(timezone.utc).timestamp() * 1000)
    age_ms = now_ms - generated_ms
    return 0 <= age_ms <= LIVE_INVENTORY_MAX_AGE_MS


def _load_synth_state(store: EventStore) -> dict[str, dict[str, Any]]:
    raw = _read_bounded(store.root / "inventory-state.json", MAX_SYNTH_STATE_BYTES)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    sessions = value.get("sessions")
    if not isinstance(sessions, dict):
        return {}
    return {
        exact: dict(item)
        for key, item in sessions.items()
        if isinstance(key, str) and isinstance(item, Mapping)
        if (exact := valid_thread_key(key))
    }


def _state_payload(sessions: Mapping[str, Mapping[str, Any]]) -> bytes:
    return (
        json.dumps(
            {"schema_version": 1, "sessions": sessions},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _event_timestamp(event: Mapping[str, Any]) -> int:
    return _integer(event.get("ts_unix_ms")) or 0


def _latest_event(
    events: Sequence[Mapping[str, Any]], names: set[str] | frozenset[str]
) -> Mapping[str, Any] | None:
    matches = [event for event in events if event.get("event") in names]
    if not matches:
        return None
    return max(enumerate(matches), key=lambda item: (_event_timestamp(item[1]), item[0]))[
        1
    ]


def _unanswered_event(
    events: Sequence[Mapping[str, Any]],
    *,
    row: Mapping[str, Any],
    state: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    alert = _latest_event(events, {"needs_input", "permission_prompt"})
    if alert is None:
        return None
    alert_ms = _event_timestamp(alert)
    later_non_alert = max(
        (
            _event_timestamp(event)
            for event in events
            if event.get("event") not in {"needs_input", "permission_prompt"}
            and _event_timestamp(event) > alert_ms
        ),
        default=0,
    )
    resolved_at = _integer(state.get("attention_resolved_at_unix_ms")) or 0
    if _status_kind(row) != "needs":
        resolved_at = max(
            resolved_at,
            _integer(row.get("recent_output_at_unix_ms")) or 0,
        )
    return None if max(later_non_alert, resolved_at) > alert_ms else alert


def _update_and_synthesize(
    store: EventStore,
    rows_by_key: Mapping[str, Mapping[str, Any]],
    *,
    now_ms: int,
    inventory_is_live: bool,
) -> dict[str, dict[str, Any]]:
    previous = _load_synth_state(store)
    if not inventory_is_live:
        return previous
    updated: dict[str, dict[str, Any]] = {}

    for key, prior in previous.items():
        if key in rows_by_key:
            continue
        ended = dict(prior)
        if prior.get("present") is True and key.startswith("codex:"):
            store._append_locked(
                key,
                event_record("session_end", source="synth", ts_unix_ms=now_ms),
            )
        ended["present"] = False
        ended["changed_at_unix_ms"] = now_ms
        updated[key] = ended

    for key, row in rows_by_key.items():
        prior = previous.get(key, {})
        kind = _status_kind(row)
        fingerprint = _fingerprint(row)
        was_present = prior.get("present") is True
        prior_kind = str(prior.get("status_kind") or "")
        changed_at = _integer(prior.get("changed_at_unix_ms"))
        if not was_present:
            changed_at = _initial_change_ms(row, now_ms)
        elif prior.get("fingerprint") != fingerprint:
            changed_at = now_ms
        if changed_at is None:
            changed_at = now_ms

        resolved_at = _integer(prior.get("attention_resolved_at_unix_ms"))
        if was_present and prior_kind == "needs" and kind != "needs":
            resolved_at = now_ms

        if key.startswith("codex:"):
            if not was_present:
                store._append_locked(
                    key,
                    event_record("session_start", source="synth", ts_unix_ms=now_ms),
                )
            if kind == "needs" and was_present and prior_kind != "needs":
                store._append_locked(
                    key,
                    event_record("needs_input", source="synth", ts_unix_ms=now_ms),
                )
            elif kind == "idle" and was_present and prior_kind != "idle":
                store._append_locked(
                    key,
                    event_record("turn_done", source="synth", ts_unix_ms=now_ms),
                )

        updated[key] = {
            "attention_resolved_at_unix_ms": resolved_at,
            "changed_at_unix_ms": changed_at,
            "fingerprint": fingerprint,
            "present": True,
            "status_kind": kind,
        }

    _atomic_write(store.root / "inventory-state.json", _state_payload(updated))
    return updated


def build_attention_queue(
    inventory: Mapping[str, Any],
    state_dir: Path | str,
    *,
    now_ms: int | None = None,
    mutate: bool = True,
) -> dict[str, Any]:
    """Return the frozen attention-queue JSON shape for one inventory.

    ``mutate=False`` is the dashboard's read-only projection: it reads existing
    hook and seen records but never synthesizes events or creates state paths.
    """
    as_of = int(time.time() * 1000) if now_ms is None else now_ms
    if isinstance(as_of, bool) or not isinstance(as_of, int) or as_of < 0:
        raise ValueError("queue timestamp must be a non-negative integer")
    raw_rows = inventory.get("sessions")
    rows = raw_rows if isinstance(raw_rows, list) else []
    keyed_rows: dict[str, Mapping[str, Any]] = {}
    source_order: dict[str, int] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            continue
        key = _row_key(raw)
        if not key or key in keyed_rows:
            continue
        keyed_rows[key] = raw
        source_order[key] = index

    store = EventStore(state_dir)
    items: list[dict[str, Any]] = []
    lock = store.locked() if mutate else contextlib.nullcontext()
    with lock:
        states = (
            _update_and_synthesize(
                store,
                keyed_rows,
                now_ms=as_of,
                inventory_is_live=_inventory_is_live(inventory, as_of),
            )
            if mutate
            else _load_synth_state(store)
        )
        for key, row in keyed_rows.items():
            state = states.get(key, {})
            events = store.read(key)
            status_kind = _status_kind(row)
            unanswered = _unanswered_event(events, row=row, state=state)
            status_needs = status_kind == "needs"
            done = _latest_event(events, {"turn_done"})
            done_ms = _event_timestamp(done) if done is not None else 0
            seen_ms = store.seen_unix_ms(key) or 0

            question: str | None = None
            waiting_ms: int | None = None
            waiting_since_ms: int | None = None
            if status_needs or unanswered is not None:
                bucket = "needs_you"
                if unanswered is not None:
                    raw_question = unanswered.get("question")
                    question = raw_question if isinstance(raw_question, str) else None
                    waiting_since = _event_timestamp(unanswered)
                else:
                    # When synthesis was skipped (stale snapshot, first run
                    # with no state), fall back to the row's own change age —
                    # never to as_of, which reads as "waiting 0m" for a
                    # session that has been blocked for hours.
                    waiting_since = _integer(
                        state.get("changed_at_unix_ms")
                    ) or _initial_change_ms(row, as_of)
                waiting_since_ms = waiting_since
                waiting_ms = max(0, as_of - waiting_since)
            elif done_ms > seen_ms:
                bucket = "finished_unseen"
            elif status_kind == "idle":
                bucket = "idle"
            else:
                bucket = "working"

            last_event = _latest_event(events, frozenset({
                "needs_input",
                "turn_done",
                "session_start",
                "session_end",
                "permission_prompt",
            }))
            last_event_ms = _event_timestamp(last_event) if last_event else 0
            changed_ms = _integer(state.get("changed_at_unix_ms")) or _initial_change_ms(
                row, as_of
            )
            stale = (
                status_kind == "working"
                and as_of - max(last_event_ms, changed_ms) > STALE_AFTER_MS
            )
            terminal_number = row.get("terminal_number")
            if (
                isinstance(terminal_number, bool)
                or not isinstance(terminal_number, int)
                or terminal_number <= 0
            ):
                terminal_number = None
            items.append(
                {
                    "thread_key": key,
                    "terminal_number": terminal_number,
                    # Never the thread key: this string is printed to a
                    # person, and a key is a UUID with a provider stuck on it.
                    "title": (
                        _clean_text(row.get("title"), 120)
                        or (
                            f"session {terminal_number}"
                            if terminal_number
                            else f"a {key.split(':', 1)[0]} session"
                        )
                    ),
                    "provider": key.split(":", 1)[0],
                    "bucket": bucket,
                    "question": question,
                    "waiting_ms": waiting_ms,
                    "waiting_since_unix_ms": waiting_since_ms,
                    "last_response_at_unix_ms": done_ms or None,
                    "stale": stale,
                }
            )

    items.sort(
        key=lambda item: (
            BUCKET_ORDER[str(item["bucket"])],
            source_order[str(item["thread_key"])],
        )
    )
    return {"as_of_unix_ms": as_of, "items": items}


attention_queue = build_attention_queue
