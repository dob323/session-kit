"""Worker events tool adapted from Maniple.

Source: github.com/Martian-Engineering/maniple
Commit: 0987ccf59552989600f6134e6602abe72a3214d0
License: MIT, per the source project's pyproject.toml.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ...adapter import KitAdapter


def parse_since(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("invalid since timestamp")
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        raise ValueError("invalid since timestamp")
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def invoke(adapter: "KitAdapter", arguments: dict[str, Any]) -> dict[str, Any]:
    selector = arguments.get("session_id")
    worker = None
    if selector is not None:
        if not isinstance(selector, str):
            return {"error": "session_id must be a string"}
        worker = adapter.registry().resolve(selector)
        if worker is None:
            return {"error": f"Session not found: {selector}"}
    try:
        since = parse_since(arguments.get("since"))
        limit = int(arguments.get("limit", 1000))
    except (TypeError, ValueError):
        return {"error": "Invalid since or limit"}
    event_result = adapter.events(worker=worker, since_unix_ms=since, limit=limit)
    events = event_result["events"]
    response: dict[str, Any] = {
        "events": events,
        "count": len(events),
        "truncated": event_result["truncated"],
    }
    if arguments.get("include_summary"):
        response["summary"] = {
            "needs_input": [
                row["thread_key"]
                for row in events
                if row.get("event") in {"needs_input", "permission_prompt"}
            ],
            "turn_done": [
                row["thread_key"]
                for row in events
                if row.get("event") == "turn_done"
            ],
            "last_event_ts_unix_ms": events[-1]["ts_unix_ms"] if events else None,
        }
    return response
