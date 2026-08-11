"""Read worker logs tool adapted from Maniple.

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
    if not isinstance(selector, str) or not selector:
        return {"error": "session_id is required"}
    worker = adapter.registry().resolve(selector)
    if worker is None:
        return {"error": f"Session not found: {selector}"}
    try:
        pages = int(arguments.get("pages", 1))
        offset = int(arguments.get("offset", 0))
        return dict(adapter.read_logs(worker, pages=pages, offset=offset))
    except (TypeError, ValueError, RuntimeError) as exc:
        return {"error": str(exc), "session_id": worker.session_id}
