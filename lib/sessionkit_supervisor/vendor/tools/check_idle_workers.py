"""Check idle workers tool adapted from Maniple.

Source: github.com/Martian-Engineering/maniple
Commit: 0987ccf59552989600f6134e6602abe72a3214d0
License: MIT, per the source project's pyproject.toml.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ...adapter import KitAdapter


def invoke(adapter: "KitAdapter", arguments: dict[str, Any]) -> dict[str, Any]:
    selectors = arguments.get("session_ids")
    if not isinstance(selectors, list) or not selectors:
        return {"error": "No session_ids provided"}
    registry = adapter.registry()
    resolved = [(selector, registry.resolve(selector)) for selector in selectors if isinstance(selector, str)]
    missing = [selector for selector, worker in resolved if worker is None]
    if missing or len(resolved) != len(selectors):
        return {"error": f"Sessions not found: {', '.join(missing) or 'invalid selector'}"}
    idle = {selector: bool(worker and worker.is_idle()) for selector, worker in resolved}
    statuses = {selector: worker.status for selector, worker in resolved if worker}
    idle_count = sum(idle.values())
    return {
        "session_ids": selectors,
        "idle": idle,
        "statuses": statuses,
        "all_idle": idle_count == len(selectors),
        "idle_count": idle_count,
        "busy_count": len(selectors) - idle_count,
    }
