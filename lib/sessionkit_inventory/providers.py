"""Shared provider helpers, the shpool snapshot reader, and the live join.

Claude Code and Codex readers live in ``providers_claude`` and
``providers_codex``; this module holds what both sides share, the shpool
session list, and the one bounded pass that joins every provider snapshot.
"""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any, Callable, Mapping, Sequence

from .common import CollectionError, clean_text


def _arg_value(argv: Sequence[str], option: str) -> str:
    try:
        index = argv.index(option)
    except ValueError:
        return ""
    return argv[index + 1] if index + 1 < len(argv) else ""


def _shpool_executable(*, home_factory: Callable[[], Path]) -> str:
    """Resolve shpool for contexts (systemd user services) whose PATH omits it."""
    found = shutil.which("shpool")
    if found:
        return found
    fallback = home_factory() / ".cargo" / "bin" / "shpool"
    return str(fallback) if fallback.is_file() else "shpool"


def _parse_shpool_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("sessions"), list
    ):
        raise CollectionError("shpool list --json returned an invalid object")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload["sessions"]:
        if not isinstance(item, Mapping):
            raise CollectionError("shpool list --json contained a non-object session")
        name = item.get("name")
        status_raw = clean_text(item.get("status"), 32).casefold()
        if (
            not isinstance(name, str)
            or not name
            or name in seen
            or status_raw not in {"attached", "disconnected"}
        ):
            raise CollectionError(
                "shpool list --json contained an invalid or duplicate session"
            )
        started = item.get("started_at_unix_ms")
        if not isinstance(started, int) or started < 0:
            raise CollectionError(
                f"shpool session {name!r} has invalid started_at_unix_ms"
            )
        seen.add(name)
        result.append(
            {
                "name": name,
                "status": "Attached" if status_raw == "attached" else "Disconnected",
                "started_at_unix_ms": started,
            }
        )
    return result
