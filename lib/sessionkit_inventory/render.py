"""Terminal width, semantic colour, and the dashboard, detail, and lookup views."""

from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from typing import Any, Callable, Mapping

from .common import _positive_int, clean_text


DEFAULT_STALL_SECONDS = 2700


def _format_age(seconds: int | None) -> str:
    if seconds is None:
        return ""
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _short_path(path: str, *, home_factory: Callable[[], Any]) -> str:
    home = str(home_factory())
    return (
        f"~{path[len(home) :]}"
        if path == home or path.startswith(home + os.sep)
        else path
    )


def _display_width(value: str) -> int:
    """Conservative terminal-cell width without adding a runtime dependency."""
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in value
    )


def _display_title(value: Any, limit: int = 48) -> str:
    """Control-safe, terminal-cell-aware truncation for display only."""
    text = clean_text(value, 10000)
    if _display_width(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    kept: list[str] = []
    used = 0
    for character in text:
        cells = _display_width(character)
        if used + cells > limit - 1:
            break
        kept.append(character)
        used += cells
    return f"{''.join(kept)}…"


def _color_enabled() -> bool:
    return (
        sys.stdout.isatty()
        and not os.environ.get("NO_COLOR")
        and not os.environ.get("SESSION_KIT_NO_COLOR")
    )


def stall_threshold_seconds() -> int:
    """Silence after which a session stops being described as running.

    Deliberately far above any normal pause. Codex sessions report "running"
    for their whole life, working or waiting, so a short threshold would label
    every parked session and the warning would stop meaning anything. Override
    with SESSION_KIT_STALL_SECONDS.
    """
    return _positive_int(
        os.environ.get("SESSION_KIT_STALL_SECONDS"),
        DEFAULT_STALL_SECONDS,
        60,
        86400,
    )


def render_inventory(
    inventory: Mapping[str, Any],
    rows_only: bool = False,
    *,
    color_enabled: Callable[[], bool],
) -> str:
    """Render action-first/provider groups with sequential visible row numbers."""
    color = color_enabled()
    bold = "\033[1m" if color else ""
    dim = "\033[2m" if color else ""
    cyan = "\033[36m" if color else ""
    green = "\033[32m" if color else ""
    yellow = "\033[33m" if color else ""
    reset = "\033[0m" if color else ""
    # Truecolor values equal to what Codex RENDERS for each sk-<color> theme
    # accent (measured from captured frames — Codex contrast-adjusts the raw
    # theme hex), so a session's name is pixel-identical here and in its own
    # Codex status bar. Remeasure if the theme anchors ever change.
    session_palette = (
        {
            "red": "\033[38;2;237;93;93m",
            "blue": "\033[38;2;97;166;240m",
            "green": "\033[38;2;63;221;115m",
            "yellow": "\033[38;2;249;215;108m",
            "purple": "\033[38;2;173;115;239m",
            "orange": "\033[38;2;242;144;81m",
            "pink": "\033[38;2;240;113;177m",
            "cyan": "\033[38;2;64;216;209m",
        }
        if color
        else {}
    )

    def tint(item: Mapping[str, Any], text: str) -> str:
        code = session_palette.get(item.get("display_color") or "")
        return f"{code}{text}{reset}" if code else text

    terminal_columns = shutil.get_terminal_size(fallback=(101, 24)).columns
    columns = _positive_int(
        os.environ.get("COLUMNS"),
        max(2, min(240, terminal_columns)),
        2,
        240,
    )
    width = max(1, columns - 1)
    lines: list[str] = []
    if inventory.get("stale"):
        lines.append(
            f"{yellow}  Warning: showing {inventory.get('source')} inventory.{reset}"
        )
    sessions = list(inventory.get("sessions", ()))
    has_terminal_numbers = any(
        isinstance(item, Mapping)
        and isinstance(item.get("terminal_number"), int)
        and not isinstance(item.get("terminal_number"), bool)
        and item.get("terminal_number", 0) > 0
        for item in sessions
    )
    available_count = sum(1 for item in sessions if item.get("availability") == "ready")
    attached_count = sum(
        1 for item in sessions if item.get("availability") == "attached"
    )
    session_word = "session" if len(sessions) == 1 else "sessions"
    lines.append(
        f"  {len(sessions)} {session_word}: {available_count} available, "
        f"{attached_count} open in another window"
    )
    if sessions:
        lines.append("")
    for availability in ("ready", "attached"):
        selected = [
            item for item in sessions if item.get("availability") == availability
        ]
        if not selected:
            continue
        heading = (
            "Available to open"
            if availability == "ready"
            else "Already open in another window"
        )
        lines.append(f"  {bold}{heading}{reset}")
        for provider in ("claude", "codex", "shell", "unknown"):
            # Group by the display provider so this view agrees with the
            # chooser: a started-but-unused Codex session belongs under Codex,
            # not under Unknown, even though its identity is not yet resolvable.
            group = [
                item
                for item in selected
                if (item.get("display_provider") or item.get("provider")) == provider
            ]
            if not group:
                continue
            lines.append(f"    {cyan}{provider.title()}{reset}")
            for item in group:
                process_age = _format_age(item.get("process_age_seconds"))
                recent_output_age = _format_age(item.get("recent_output_age_seconds"))
                status = clean_text(item.get("agent_status") or "unknown", 64)
                recent_seconds = item.get("recent_output_age_seconds")
                # "running" is not evidence for Codex, which reports it whether
                # it is working or waiting. State the silence instead.
                if (
                    status.casefold() == "running"
                    and isinstance(recent_seconds, int)
                    and not isinstance(recent_seconds, bool)
                    and recent_seconds >= stall_threshold_seconds()
                    and recent_output_age
                ):
                    status = f"quiet {recent_output_age}"
                status_parts = (
                    ["needs your reply"] if item.get("needs_you") else [status]
                )
                if recent_output_age:
                    status_parts.append(f"recent output {recent_output_age} ago")
                if process_age:
                    status_parts.append(f"process age {process_age}")
                agent_count = int(
                    item.get("active_subagent_count", len(item.get("subagents", ())))
                )
                if agent_count:
                    status_parts.append(
                        f"{agent_count} subagent{'s' if agent_count != 1 else ''}"
                    )
                selector = item.get("terminal_number")
                if (
                    isinstance(selector, bool)
                    or not isinstance(selector, int)
                    or selector <= 0
                ):
                    selector = "-" if has_terminal_numbers else item.get("row")
                prefix = f"      {selector:>2}  "
                title_room = max(1, width - _display_width(prefix))
                title = _display_title(item.get("title"), title_room)
                lines.append(f"      {green}{selector:>2}{reset}  {tint(item, title)}")
                detail_prefix = "          "
                detail_room = max(1, width - _display_width(detail_prefix))
                details = _display_title(" | ".join(status_parts), detail_room)
                detail_color = (
                    yellow
                    if item.get("needs_you") or status.casefold() == "provider exited"
                    else dim
                )
                lines.append(f"{detail_prefix}{detail_color}{details}{reset}")
    outside = list(inventory.get("outside_agents", ()))
    if outside:
        lines.append(f"  {bold}Outside shpool{reset}")
        for item in outside:
            uuid = item.get("identity", {}).get("uuid") or ""
            agent_count = int(
                item.get("active_subagent_count", len(item.get("subagents", ())))
            )
            prefix = f"      -  [{item.get('provider')}] "
            title = _display_title(
                item.get("title"), max(1, width - _display_width(prefix))
            )
            lines.append(f"{prefix}{tint(item, title)}")
            detail_parts = [clean_text(item.get("agent_status") or "unknown", 64)]
            if uuid:
                detail_parts.append(uuid[:8])
            if agent_count:
                detail_parts.append(
                    f"{agent_count} subagent{'s' if agent_count != 1 else ''}"
                )
            detail_prefix = "          "
            details = _display_title(
                " | ".join(detail_parts),
                max(1, width - _display_width(detail_prefix)),
            )
            lines.append(f"{detail_prefix}{dim}{details}{reset}")
    if not sessions and not outside:
        lines.append("  No sessions found.")
    if not rows_only:
        lines.extend(
            [
                "",
                f"  {dim}sp go <n>: open | sp new: start{reset}",
                f'  {dim}sp close <n>: close | sp find "text": search history{reset}',
            ]
        )
    return "\n".join(lines)


def lookup(inventory: Mapping[str, Any], selector: str) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    if selector.isdigit():
        number = int(selector)
        sessions = list(inventory.get("sessions", ()))
        if any(
            isinstance(item.get("terminal_number"), int)
            and not isinstance(item.get("terminal_number"), bool)
            and item.get("terminal_number", 0) > 0
            for item in sessions
            if isinstance(item, Mapping)
        ):
            matches = [
                item for item in sessions if item.get("terminal_number") == number
            ]
        else:
            matches = [item for item in sessions if item.get("row") == number]
    else:
        matches = [
            item
            for item in inventory.get("sessions", ())
            if (item.get("shpool_id_raw") or item.get("shpool_id")) == selector
        ]
    return matches[0] if len(matches) == 1 else None
