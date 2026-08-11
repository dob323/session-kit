"""Poll worker changes tool adapted from Maniple.

Source: github.com/Martian-Engineering/maniple
Commit: 0987ccf59552989600f6134e6602abe72a3214d0
License: MIT, per the source project's pyproject.toml.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .worker_events import parse_since

if TYPE_CHECKING:
    from ...adapter import KitAdapter


def invoke(adapter: "KitAdapter", arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        since = parse_since(arguments.get("since"))
        threshold = int(arguments.get("stale_threshold_minutes", 20))
    except (TypeError, ValueError):
        return {"error": "Invalid since or stale_threshold_minutes"}
    if threshold <= 0:
        return {"error": "stale_threshold_minutes must be greater than 0"}
    event_result = adapter.events(since_unix_ms=since, limit=2000)
    events = event_result["events"]
    workers = adapter.registry().list_all()
    idle = [worker for worker in workers if worker.is_idle()]
    active = [worker for worker in workers if not worker.is_idle()]
    stuck = [
        {
            "name": worker.name,
            "session_id": worker.session_id,
            "inactive_minutes": worker.recent_output_age_seconds // 60,
        }
        for worker in active
        if worker.recent_output_age_seconds is not None
        and worker.recent_output_age_seconds >= threshold * 60
    ]
    return {
        "events": events,
        "summary": {
            "completed": [row for row in events if row.get("event") == "turn_done"],
            "started": [row for row in events if row.get("event") == "session_start"],
            "stuck": stuck,
        },
        "active_count": len(active),
        "idle_count": len(idle),
        "poll_ts_unix_ms": int(adapter.clock() * 1000),
        "truncated": event_result["truncated"],
    }
