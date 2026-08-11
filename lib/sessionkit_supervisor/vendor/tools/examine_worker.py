"""Examine worker tool adapted from Maniple.

Source: github.com/Martian-Engineering/maniple
Commit: 0987ccf59552989600f6134e6602abe72a3214d0
License: MIT, per the source project's pyproject.toml.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ...adapter import KitAdapter


def invoke(adapter: "KitAdapter", arguments: dict[str, Any]) -> dict[str, Any]:
    selector = arguments.get("session_id")
    if not isinstance(selector, str) or not selector.strip():
        return {"error": "session_id is required"}
    worker = adapter.registry().resolve(selector)
    if worker is None:
        return {"error": f"Session not found: {selector}"}
    result = worker.to_dict()
    try:
        logs = adapter.read_logs(worker, pages=1, offset=0)
        records = logs["records"]
        result["conversation_stats"] = {
            "bounded_record_count": len(records),
            "last_record": records[-1] if records else None,
        }
    except RuntimeError as exc:
        result["conversation_stats"] = {"unavailable": str(exc)}
    return result
