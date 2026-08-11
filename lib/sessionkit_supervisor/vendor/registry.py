"""Fresh-inventory worker registry adapted from Maniple.

Source: github.com/Martian-Engineering/maniple
Commit: 0987ccf59552989600f6134e6602abe72a3214d0
License: MIT, per the source project's pyproject.toml.

Maniple's terminal-backed mutable registry is deliberately replaced by a
read-only registry seeded from one Session Kit inventory snapshot. Session IDs
are exact ``thread_key`` values, so there is no parallel identity namespace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Callable, Mapping


PROVIDERS = ("claude", "codex")
IDLE_STATUSES = ("idle", "reply optional")


def _text(value: object, limit: int = 4096) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _uuid(row: Mapping[str, Any]) -> str:
    identity = row.get("identity")
    if not isinstance(identity, Mapping):
        return ""
    value = _text(identity.get("uuid"), 36).lower()
    parts = value.split("-")
    if len(parts) != 5 or [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        return ""
    try:
        int("".join(parts), 16)
    except ValueError:
        return ""
    return value


@dataclass
class Worker:
    """One exact managed Session Kit provider thread."""

    session_id: str
    provider: str
    uuid: str
    name: str
    project_path: str
    status: str
    terminal_number: int | None
    shpool_id: str
    availability: str
    created_at_unix_ms: int | None
    recent_output_age_seconds: int | None
    source_row: dict[str, Any] = field(repr=False)
    clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)
    coordinator_badge: str | None = None

    @classmethod
    def from_row(
        cls,
        row: Mapping[str, Any],
        *,
        clock: Callable[[], float] = time.time,
    ) -> "Worker | None":
        provider = _text(row.get("provider"), 20).casefold()
        uuid = _uuid(row)
        if provider not in PROVIDERS or not uuid:
            return None
        number = row.get("terminal_number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            number = None
        created = row.get("started_at_unix_ms")
        if isinstance(created, bool) or not isinstance(created, int):
            created = None
        recent = row.get("recent_output_age_seconds")
        if isinstance(recent, bool) or not isinstance(recent, int) or recent < 0:
            recent = None
        title = _text(row.get("title"), 200)
        shpool_id = _text(
            row.get("shpool_id_raw") or row.get("shpool_id"), 128
        )
        return cls(
            session_id=f"{provider}:{uuid}",
            provider=provider,
            uuid=uuid,
            name=title or shpool_id or uuid[:8],
            project_path=_text(row.get("cwd"), 4096),
            status=_text(row.get("agent_status"), 100) or "unknown",
            terminal_number=number,
            shpool_id=shpool_id,
            availability=_text(row.get("availability"), 40) or "unknown",
            created_at_unix_ms=created,
            recent_output_age_seconds=recent,
            source_row=dict(row),
            clock=clock,
        )

    @property
    def created_at(self) -> datetime:
        if self.created_at_unix_ms is None:
            return datetime.fromtimestamp(0, tz=timezone.utc)
        return datetime.fromtimestamp(self.created_at_unix_ms / 1000, tz=timezone.utc)

    @property
    def last_activity(self) -> datetime:
        if self.recent_output_age_seconds is None:
            return self.created_at
        return datetime.fromtimestamp(
            max(0.0, self.clock() - self.recent_output_age_seconds), tz=timezone.utc
        )

    def is_closed(self) -> bool:
        """Identify retained closed rows without hiding their native status."""
        return self.status.casefold() == "provider exited"

    def is_idle(self) -> bool:
        """Keep the native status visible; only known parked states are idle."""
        return self.status.casefold() in IDLE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "thread_key": self.session_id,
            "provider": self.provider,
            "agent_type": self.provider,
            "uuid": self.uuid,
            "name": self.name,
            "title": self.name,
            "project_path": self.project_path,
            "cwd": self.project_path,
            "status": self.status,
            "agent_status": self.status,
            "is_idle": self.is_idle(),
            "terminal_number": self.terminal_number,
            "shpool_id": self.shpool_id,
            "availability": self.availability,
            "created_at": self.created_at.isoformat(),
            "recent_output_age_seconds": self.recent_output_age_seconds,
            "coordinator_badge": self.coordinator_badge,
            "source": "session_kit_inventory",
        }


class SessionRegistry:
    """Resolve exact threads and unambiguous human-facing selectors."""

    def __init__(self, workers: list[Worker] | None = None) -> None:
        self._workers = list(workers or [])

    @classmethod
    def from_inventory(
        cls,
        inventory: Mapping[str, Any],
        *,
        clock: Callable[[], float] = time.time,
    ) -> "SessionRegistry":
        rows = inventory.get("sessions")
        workers: list[Worker] = []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, Mapping) and (worker := Worker.from_row(row, clock=clock)):
                    workers.append(worker)
        workers.sort(
            key=lambda worker: (
                worker.created_at_unix_ms or 0,
                worker.session_id,
            )
        )
        return cls(workers)

    def list_all(self) -> list[Worker]:
        return list(self._workers)

    def count(self) -> int:
        return len(self._workers)

    def get(self, session_id: str) -> Worker | None:
        return next(
            (worker for worker in self._workers if worker.session_id == session_id),
            None,
        )

    def resolve(self, selector: str) -> Worker | None:
        candidate = selector.strip()
        exact = self.get(candidate)
        if exact is not None:
            return exact
        matches = [
            worker
            for worker in self._workers
            if candidate
            and candidate
            in {
                worker.name,
                worker.shpool_id,
                str(worker.terminal_number or ""),
                worker.uuid,
            }
        ]
        return matches[0] if len(matches) == 1 else None

    def list_by_status(self, status: str) -> list[Worker]:
        wanted = status.casefold()
        return [worker for worker in self._workers if worker.status.casefold() == wanted]

    def apply_annotations(self, annotations: Mapping[str, str]) -> None:
        for worker in self._workers:
            badge = annotations.get(worker.session_id)
            if isinstance(badge, str) and badge:
                worker.coordinator_badge = badge
