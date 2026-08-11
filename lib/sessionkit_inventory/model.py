"""Session record shaping: identity, recovery, titles, and the base row.

Pure composition only. Nothing here reads the process table, the filesystem,
or provider state; callers pass in whatever evidence they already collected.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import shlex
from typing import Any, Callable, Mapping, Sequence

from .common import (
    PROVIDERS,
    automatic_naming_enabled,
    clean_text,
    display_shpool_id,
    shpool_id_mutation_policy,
    valid_uuid,
)


def recovery_spec(provider: str, uuid: str, cwd: str | None = None) -> dict[str, Any]:
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise ValueError("recovery requires provider claude|codex and an exact UUID")
    argv = (
        ["claude", "--resume", exact_uuid]
        if provider == "claude"
        else ["codex", "--no-alt-screen", "resume", exact_uuid]
    )
    clean_cwd = clean_text(cwd, 4096) if cwd else None
    command = shlex.join(argv)
    if clean_cwd:
        command = f"cd -- {shlex.quote(clean_cwd)} && {command}"
    return {
        "available": True,
        "provider": provider,
        "uuid": exact_uuid,
        "cwd": clean_cwd,
        "argv": argv,
        "command": command,
    }


def _empty_recovery() -> dict[str, Any]:
    return {
        "available": False,
        "provider": None,
        "uuid": None,
        "cwd": None,
        "argv": [],
        "command": None,
    }


def _agent_identity(
    *,
    provider: str,
    uuid: str | None,
    pid: int | None,
    process_table: Mapping[int, Mapping[str, Any]],
    provenance: str,
    confidence: str,
) -> dict[str, Any]:
    process = process_table.get(pid or -1, {})
    return {
        "uuid": uuid,
        "pid": pid,
        "process_start_ticks": process.get("start_ticks"),
        "provenance": provenance,
        "confidence": confidence,
    }


def _context_title(
    provider: str,
    cwd: str,
    started_at_unix_ms: int | None,
    *,
    home: Callable[[], Path],
) -> str:
    """Return a deterministic project hint without exposing a full path."""
    if (
        isinstance(started_at_unix_ms, bool)
        or not isinstance(started_at_unix_ms, int)
        or started_at_unix_ms <= 0
    ):
        return ""
    try:
        local_start = dt.datetime.fromtimestamp(
            started_at_unix_ms / 1000, tz=dt.timezone.utc
        ).astimezone()
    except (OverflowError, OSError, ValueError):
        return ""
    timestamp = local_start.strftime("%b %d %H:%M")
    clean_cwd = clean_text(cwd, 4096)
    if not clean_cwd or not clean_cwd.startswith("/"):
        return f"{provider.title()} started {timestamp}"
    parts = [
        clean_text(part, 40)
        for part in Path(clean_cwd).parts
        if part not in {"", os.sep}
    ]
    generic = {
        "app",
        "apps",
        "code",
        "current",
        "home",
        "repo",
        "repos",
        "src",
        "srv",
        "v2",
        "var",
        "workspace",
        "workspaces",
    }
    home_name = clean_text(home().name, 40).casefold()
    for part in reversed(parts):
        folded = part.casefold()
        if (
            part
            and not part.startswith(".")
            and folded not in generic
            and folded != home_name
        ):
            return f"{provider.title()} in {part} at {timestamp}"
    return f"{provider.title()} started {timestamp}"


def _provider_title_info(
    provider: str,
    uuid: str | None,
    native_title: str,
    aliases: Mapping[str, str],
    cwd: str = "",
    started_at_unix_ms: int | None = None,
    automatic_titles: Mapping[str, str] | None = None,
    *,
    provider_title_is_explicit: bool = True,
    context_title: Callable[[str, str, int | None], str],
    pushed_titles: Mapping[str, str] | None = None,
    name_ownership: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[str, str]:
    key = f"{provider}:{uuid}" if uuid else ""
    if uuid:
        alias = aliases.get(key)
        # A provider-native rename outranks the alias tier. The alias layer
        # holds kit-authored names as well as typed ones, so an automatic
        # title that was also written as an alias would otherwise mask the
        # name a person just typed into /rename — which is the newest thing
        # anyone has said about what this room is called. Reconciliation then
        # promotes it into the alias for good; this keeps the row honest in
        # the meantime.
        last_kit_title = (pushed_titles or {}).get(key) or (automatic_titles or {}).get(
            key, ""
        )
        # Divergence is the timestamp. `last_kit_title` is the last value the
        # kit itself wrote into the provider's store — including the one
        # `sp name` pushed — so a native title that differs from it can only
        # have been typed after that push. Newer human act, newer name; an
        # earlier `sp name` keeps its ownership but not its stale text.
        if (
            last_kit_title
            and native_title
            and provider_title_is_explicit
            and native_title != last_kit_title
        ):
            return native_title, "native"
        if alias:
            return alias, "alias"
    if native_title and provider_title_is_explicit:
        return native_title, "native"
    if uuid and automatic_naming_enabled():
        automatic = (automatic_titles or {}).get(key)
        if automatic:
            return automatic, "automatic"
    context = context_title(provider, cwd, started_at_unix_ms)
    if context:
        return context, "context"
    # Nothing named this session and its context yielded nothing either. The
    # provider alone is a poor label but an honest one; a UUID prefix here
    # used to put a raw identifier on every human surface at once.
    return provider.title(), "provider"


def _provider_title(
    provider: str,
    uuid: str | None,
    native_title: str,
    aliases: Mapping[str, str],
    cwd: str = "",
    started_at_unix_ms: int | None = None,
    automatic_titles: Mapping[str, str] | None = None,
    *,
    provider_title_is_explicit: bool = True,
    provider_title_info: Callable[..., tuple[str, str]],
    pushed_titles: Mapping[str, str] | None = None,
    name_ownership: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    return provider_title_info(
        provider,
        uuid,
        native_title,
        aliases,
        cwd,
        started_at_unix_ms,
        automatic_titles,
        provider_title_is_explicit=provider_title_is_explicit,
        pushed_titles=pushed_titles,
        name_ownership=name_ownership,
    )[0]


def _shell_title(
    tree: Sequence[int], root_pid: int, process_table: Mapping[int, Mapping[str, Any]]
) -> tuple[str, str, int | None]:
    for pid in tree:
        if pid == root_pid:
            continue
        process = process_table.get(pid, {})
        comm = clean_text(process.get("comm"), 80)
        if comm and comm not in {"bash", "zsh", "fish", "script"}:
            return comm, clean_text(process.get("cwd"), 4096), pid
    root = process_table.get(root_pid, {})
    return "Idle shell", clean_text(root.get("cwd"), 4096), root_pid


def _base_agent(
    *,
    row: int | None,
    shpool_id: str | None,
    shpool_status: str | None,
    availability: str | None,
    provider: str,
    identity: dict[str, Any],
    title: str,
    title_source: str,
    native_title: str,
    cwd: str,
    started_at_unix_ms: int | None,
    process_age_seconds: int | None,
    agent_status: str,
    needs_you: bool,
    subagents: list[dict[str, Any]],
    recovery: dict[str, Any],
    diagnostics: list[str],
    shpool_shell: dict[str, Any] | None,
) -> dict[str, Any]:
    mutation_allowed, mutation_reason = (
        shpool_id_mutation_policy(shpool_id)
        if shpool_id is not None
        else (False, "outside-shpool")
    )
    return {
        "row": row,
        "terminal_number": None,
        "shpool_id": shpool_id,
        "shpool_id_raw": shpool_id,
        "display_shpool_id": (
            display_shpool_id(shpool_id) if shpool_id is not None else None
        ),
        "mutation_allowed": mutation_allowed,
        "mutation_rejection_reason": mutation_reason,
        "shpool_status": shpool_status,
        "availability": availability,
        "provider": provider,
        "display_provider": provider,
        "setup_incomplete": False,
        "identity": identity,
        "title": clean_text(title, 120),
        "title_source": clean_text(title_source, 20),
        "display_title": clean_text(title, 120),
        "native_title": clean_text(native_title, 120),
        "cwd": clean_text(cwd, 4096),
        "started_at_unix_ms": started_at_unix_ms,
        "process_age_seconds": process_age_seconds,
        "agent_status": clean_text(agent_status, 60) or "unknown",
        "needs_you": bool(needs_you),
        "subagents": subagents,
        "active_subagent_count": sum(
            1
            for child in subagents
            if str(child.get("status") or "").casefold()
            not in {
                "idle",
                "complete",
                "completed",
                "closed",
                "stopped",
                "failed",
                "cancelled",
                "canceled",
            }
        ),
        "recovery": recovery,
        "diagnostics": diagnostics,
        "shpool_shell": shpool_shell,
    }
