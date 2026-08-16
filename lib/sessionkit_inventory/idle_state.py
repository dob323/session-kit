"""Evidence-based ageing of sessions already waiting on the operator.

The provider status does not decide idleness.  A conversation becomes aged
only after its own transcript's path, size and nanosecond mtime remain unchanged
for the configured window.  The prior published inventory is the observation
record, so no second scanner or private clock store is needed.
"""

from __future__ import annotations

import math
from pathlib import Path
import stat
import time
from typing import Any, Callable, Mapping

from .common import stall_threshold_seconds, valid_uuid
from . import labels


IDLE_MINUTES_FILE = "session-idle-minutes"
DEFAULT_IDLE_MINUTES = 30.0
_EVIDENCE_FIELD = "_transcript_idle_evidence"
IDLE_FIELD = "transcript_idle"


def idle_minutes(state_dir: Path) -> float | None:
    """Configured minutes, or None when idling is explicitly/refusably off.

    A missing file selects the default.  Once the pathname exists, every
    failure is a refusal: symlink, irregular file, read error, empty content or
    malformed number.  Falling back to thirty after an unreadable attempted
    override could shorten the operator's intended window, so it is forbidden.
    Zero follows the established sweep-file convention and disables idling.
    """
    path = state_dir / IDLE_MINUTES_FILE
    try:
        info = path.lstat()
    except FileNotFoundError:
        return DEFAULT_IDLE_MINUTES
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    if len(payload) > 64:
        return None
    raw = payload.decode("utf-8", "replace").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0 or not math.isfinite(value * 60_000):
        return None
    return value


def _rows_by_conversation(inventory: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    rows: dict[str, Mapping[str, Any]] = {}
    if not isinstance(inventory, Mapping):
        return rows
    sessions = inventory.get("sessions")
    if not isinstance(sessions, list):
        return rows
    for row in sessions:
        if not isinstance(row, Mapping):
            continue
        provider = row.get("provider")
        identity = row.get("identity")
        uuid = identity.get("uuid") if isinstance(identity, Mapping) else None
        exact = valid_uuid(uuid)
        if provider in {"claude", "codex"} and exact:
            rows[f"{provider}:{exact}"] = row
    return rows


def _signature(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    path = value.get("path")
    size = value.get("size")
    mtime_ns = value.get("mtime_ns")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or isinstance(mtime_ns, bool)
        or not isinstance(mtime_ns, int)
        or mtime_ns < 0
    ):
        return None
    return {"path": path, "size": size, "mtime_ns": mtime_ns}


def apply_idle_evidence(
    inventory: dict[str, Any],
    previous: Mapping[str, Any] | None,
    *,
    state_dir: Path,
    transcript_snapshot: Callable[[str, str], Mapping[str, object] | None],
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Attach one conservative transcript-idle verdict to each live row."""
    minutes = idle_minutes(state_dir)
    sessions = inventory.get("sessions")
    if not isinstance(sessions, list):
        return inventory
    moment = (
        now_ms
        if isinstance(now_ms, int) and not isinstance(now_ms, bool)
        else int(time.time() * 1000)
    )
    prior_rows = _rows_by_conversation(previous)
    window_ms = minutes * 60_000 if minutes is not None else None
    for row in sessions:
        if not isinstance(row, dict):
            continue
        row[IDLE_FIELD] = False
        row.pop(_EVIDENCE_FIELD, None)
        if window_ms is None:
            continue
        # Resolve no transcript unless this row can actually age into idle.
        # In particular, Codex path discovery may inspect a large rollout
        # estate; running that for working/question/pending rows would add a
        # scanner-shaped cost while producing evidence the state rule ignores.
        if (
            labels.session_state(row, stall_seconds=stall_threshold_seconds())
            != labels.WAITING_ON_YOU
        ):
            continue
        provider = row.get("provider")
        identity = row.get("identity")
        uuid = identity.get("uuid") if isinstance(identity, Mapping) else None
        exact = valid_uuid(uuid)
        if provider not in {"claude", "codex"} or not exact:
            continue
        current = _signature(transcript_snapshot(str(provider), exact))
        if current is None:
            continue
        key = f"{provider}:{exact}"
        prior = prior_rows.get(key, {})
        prior_evidence = prior.get(_EVIDENCE_FIELD)
        last_moved = moment
        if isinstance(prior_evidence, Mapping):
            prior_signature = _signature(prior_evidence)
            prior_last_moved = prior_evidence.get("last_moved_at_unix_ms")
            prior_observed = prior_evidence.get("observed_at_unix_ms")
            prior_window = prior_evidence.get("window_seconds")
            comparable = (
                prior_signature == current
                and isinstance(prior_last_moved, int)
                and not isinstance(prior_last_moved, bool)
                and isinstance(prior_observed, int)
                and not isinstance(prior_observed, bool)
                and isinstance(prior_window, (int, float))
                and not isinstance(prior_window, bool)
                and prior_window > 0
                and window_ms / 1000 >= float(prior_window)
                and prior_last_moved <= prior_observed <= moment
                and moment - prior_observed <= window_ms
            )
            if comparable:
                last_moved = prior_last_moved
        evidence = {
            **current,
            "last_moved_at_unix_ms": last_moved,
            "observed_at_unix_ms": moment,
            "window_seconds": window_ms / 1000,
        }
        row[_EVIDENCE_FIELD] = evidence
        row[IDLE_FIELD] = moment - last_moved >= window_ms
    return inventory
