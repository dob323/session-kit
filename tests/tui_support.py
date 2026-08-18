"""Fixtures for the picker's tests.

Rows here are the shape `shpool_status --json` writes, so a test that passes
against these is a test against the document the picker actually reads.
"""

from __future__ import annotations

from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "lib") not in sys.path:
    sys.path.insert(0, str(REPO / "lib"))

from sessionkit_tui.model import parse_inventory  # noqa: E402
from sessionkit_tui.state import Picker  # noqa: E402


class Clock:
    """A clock a test can move. Nothing in the core reads a real one."""

    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, milliseconds: int) -> None:
        self.now += milliseconds


def row(
    title: str,
    *,
    number: int,
    provider: str = "claude",
    availability: str = "ready",
    needs_you: bool = False,
    origin: str | None = None,
    model: str | None = None,
    account_alias: str | None = None,
    quiet_seconds: int | None = None,
    subagents: int = 0,
    age_seconds: int = 300,
    # A provider turn that has finished. This is a WAITING session: `idle`
    # maps to `needs you`, so a fixture row that is meant to want nothing
    # from a person has to say `agent_status="working"` for itself.
    agent_status: str = "idle",
    display_color: str | None = None,
) -> dict:
    suffix = sum(map(ord, title)) + number
    uuid = f"00000000-0000-4000-8000-{suffix:012d}" if provider != "shell" else None
    record = {
        "row": number,
        "terminal_number": number,
        "shpool_id": f"sk-{suffix}",
        "shpool_id_raw": f"sk-{suffix}",
        "display_shpool_id": f"sk-{suffix}",
        "mutation_allowed": True,
        "mutation_rejection_reason": None,
        "shpool_shell": {"pid": 1000 + suffix, "process_start_ticks": 10_000 + suffix},
        "started_at_unix_ms": 1_700_000_000_000 + suffix,
        "shpool_status": "Disconnected" if availability == "ready" else "Attached",
        "availability": availability,
        "provider": provider,
        "display_provider": provider,
        "identity": {
            "uuid": uuid,
            "pid": 2000 + suffix,
            "process_start_ticks": 20_000 + suffix,
            "provenance": "fixture",
            "confidence": "exact",
        },
        "title": title,
        "display_title": title,
        "native_title": title,
        "cwd": "/srv/project",
        "process_age_seconds": age_seconds,
        "recent_output_age_seconds": quiet_seconds,
        "recent_output_at_unix_ms": None,
        "agent_status": agent_status,
        "needs_you": needs_you,
        "subagents": [{"status": "working"} for _ in range(subagents)],
        "active_subagent_count": subagents,
        "recovery": {"available": bool(uuid), "provider": provider, "uuid": uuid},
        "diagnostics": [],
    }
    if origin is not None:
        record["origin"] = origin
    if model is not None:
        record["model"] = model
    if account_alias is not None:
        record["account_alias"] = account_alias
    if display_color is not None:
        record["display_color"] = display_color
    return record


def document(*rows: dict, stale: bool = False) -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-08-12T00:00:00Z",
        "source": "cache" if stale else "live",
        "stale": stale,
        "warnings": [],
        "daemon_generation": {"boot_id": "fixture", "pid": 77, "process_start_ticks": 770},
        "sessions": list(rows),
        "outside_agents": [],
    }


def inventory(*rows: dict, stale: bool = False):
    return parse_inventory(document(*rows, stale=stale))


def picker(*rows: dict, clock: Clock | None = None, **keywords) -> Picker:
    clock = clock or Clock()
    return Picker(inventory=inventory(*rows), clock=clock, stall=2700, **keywords)


def write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def panel_to(screen, label: str):
    """Move the panel highlight onto a named row, or fail loudly."""

    for index, item in enumerate(screen.panel.items):
        if item.label == label:
            screen.move(index - screen.panel.index)
            return screen.panel.current
    raise AssertionError(f"no panel row named {label}")


def row_to(screen, kind: str, limit: int = 60):
    """Move the list highlight onto the first row of a kind."""

    for _ in range(limit):
        current = screen.current_row()
        if current is not None and current.kind == kind:
            return current
        before = screen.cursor_index()
        screen.move(1)
        if screen.cursor_index() == before:
            break
    raise AssertionError(f"no row of kind {kind}")
