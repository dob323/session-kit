"""List workers tool adapted from Maniple.

Source: github.com/Martian-Engineering/maniple
Commit: 0987ccf59552989600f6134e6602abe72a3214d0
License: MIT, per the source project's pyproject.toml.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ...adapter import KitAdapter


def invoke(adapter: "KitAdapter", arguments: dict[str, Any]) -> dict[str, Any]:
    registry = adapter.registry()
    status = arguments.get("status_filter")
    workers = (
        registry.list_by_status(status)
        if isinstance(status, str) and status.strip()
        else registry.list_all()
    )
    if arguments.get("include_closed") is not True:
        workers = [worker for worker in workers if not worker.is_closed()]
    project = arguments.get("project_filter")
    if isinstance(project, str) and (project := project.strip()):
        workers = [
            worker
            for worker in workers
            if project == worker.project_path
            or project == worker.project_path.rstrip("/").rsplit("/", 1)[-1]
            or project in worker.project_path
        ]
    return {"workers": [worker.to_dict() for worker in workers], "count": len(workers)}
