"""Wait idle workers tool adapted from Maniple.

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
    if (
        not isinstance(selectors, list)
        or not selectors
        or not all(isinstance(value, str) for value in selectors)
    ):
        return {"error": "session_ids is required and must contain strings"}
    mode = arguments.get("mode") or "all"
    if mode not in {"all", "any"}:
        return {"error": f"Invalid mode: {mode}. Must be 'all' or 'any'"}
    try:
        timeout = float(arguments.get("timeout", 600.0))
        interval = float(arguments.get("poll_interval", 2.0))
    except (TypeError, ValueError):
        return {"error": "timeout and poll_interval must be numbers"}
    interval = max(2.0, interval)
    if not 0 <= timeout <= 600 or interval > 60:
        return {"error": "timeout must be 0..600 and poll_interval at most 60 seconds"}
    started = adapter.clock()
    idle_ids: list[str] = []
    statuses: dict[str, str] = {}
    missing: list[str] = []
    blocked_on_human = False
    while True:
        registry = adapter.registry()
        resolved = [(selector, registry.resolve(selector)) for selector in selectors]
        missing = [selector for selector, worker in resolved if worker is None]
        if missing:
            return {"error": f"Sessions not found: {', '.join(missing)}"}
        idle_ids = [selector for selector, worker in resolved if worker and worker.is_idle()]
        statuses = {selector: worker.status for selector, worker in resolved if worker}
        reached = len(idle_ids) == len(selectors) if mode == "all" else bool(idle_ids)
        non_idle = [worker for _, worker in resolved if worker and not worker.is_idle()]
        blocked_on_human = bool(non_idle) and all(
            worker.status.casefold() == "needs your reply" for worker in non_idle
        )
        elapsed = max(0.0, adapter.clock() - started)
        if reached or blocked_on_human or elapsed >= timeout:
            break
        adapter.sleeper(min(interval, timeout - elapsed))
    waiting = [selector for selector in selectors if selector not in idle_ids]
    return {
        "session_ids": selectors,
        "idle_session_ids": idle_ids,
        "statuses": statuses,
        "all_idle": len(idle_ids) == len(selectors),
        "waiting_on": waiting,
        "mode": mode,
        "waited_seconds": max(0.0, adapter.clock() - started),
        "timed_out": bool(waiting) and max(0.0, adapter.clock() - started) >= timeout,
        "blocked_on_human": blocked_on_human,
    }
