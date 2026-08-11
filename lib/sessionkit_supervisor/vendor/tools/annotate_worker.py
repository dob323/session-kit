"""Annotate worker tool adapted from Maniple.

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
    badge = arguments.get("badge")
    if not isinstance(selector, str) or not isinstance(badge, str):
        return {"error": "session_id and badge are required"}
    worker = adapter.registry().resolve(selector)
    if worker is None:
        return {"error": f"Session not found: {selector}"}
    try:
        adapter.annotate(worker, badge)
    except RuntimeError as exc:
        return {"error": str(exc)}
    return {
        "success": True,
        "session_id": worker.session_id,
        "badge": " ".join(badge.split())[:500],
        "message": "Badge saved",
    }
