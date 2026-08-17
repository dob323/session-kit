"""Session record shaping: identity, recovery, titles, and the base row.

Pure composition only. Nothing here reads the process table, the filesystem,
or provider state; callers pass in whatever evidence they already collected.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import re
import shlex
from typing import Any, Callable, Mapping, Sequence

from .common import (
    PROVIDERS,
    automatic_naming_enabled,
    clean_text,
    _pending_native_title_matches,
    display_shpool_id,
    stall_threshold_seconds,
    shpool_id_mutation_policy,
    valid_uuid,
)
from . import labels


_PICKER_PROVIDER_ORDER = {"claude": 0, "codex": 1, "shell": 2, "unknown": 3}
_PICKER_AVAILABILITY_ORDER = {"ready": 0, "attached": 1}
_PICKER_STATE_ORDER = {
    "question": 0,
    "needs you": 1,
    "pending": 2,
    "working": 2,
    "idle": 3,
}

# A shell beneath the managed session shell is work in its own right.  Keep
# this vocabulary shared with ``_shell_title`` so the detail inventory does
# not grow a second, conflicting idea of which process names are shells.
SHELL_PROCESS_NAMES = frozenset({"bash", "zsh", "fish", "script"})


def session_is_unavailable(row: Mapping[str, Any]) -> bool:
    """Whether a managed record is quarantined from every board action.

    Only a PROVEN missing shell quarantines. An unprovable generation is the
    opposite case — the kit cannot tell — and a session it cannot judge must
    stay on the board where a person can: two review lanes demonstrated a
    real, actionable human session disappearing from the shell and curses
    lists when this set also matched ``unprovable-generation``. Uncertainty
    surfaces; it never hides.
    """
    number = row.get("terminal_number")
    return (
        (isinstance(number, bool) or not isinstance(number, int) or number <= 0)
        and row.get("mutation_allowed") is False
        and row.get("mutation_rejection_reason") == "missing-shell-generation"
    )


def canonical_session_order_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """The daily picker order shared by every session-list surface.

    Ready sessions precede sessions open elsewhere. Within each location,
    attention comes first, then provider, newest recorded activity, and the
    stable natural session ID. The final selector/key fallback makes malformed
    or old snapshots deterministic without changing a valid session's place.
    """

    provider = str(row.get("display_provider") or row.get("provider") or "").casefold()
    identifier = row.get("shpool_id_raw") or row.get("shpool_id") or ""
    natural_id = tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"([0-9]+)", str(identifier))
    )
    recent = row.get("recent_output_at_unix_ms")
    if isinstance(recent, bool) or not isinstance(recent, int):
        recent = None
    selector = row.get("terminal_number")
    if isinstance(selector, bool) or not isinstance(selector, int):
        selector = 10**9
    # Derive from collector evidence every time. Cached/published ``state`` is
    # display output and can lag a row mutation in tests or compatibility
    # overlays; it must never outrank the evidence which creates it.
    state = labels.session_state(row, stall_seconds=stall_threshold_seconds())
    return (
        _PICKER_AVAILABILITY_ORDER.get(
            str(row.get("availability") or "").casefold(), 9
        ),
        _PICKER_STATE_ORDER.get(str(state), 9),
        _PICKER_PROVIDER_ORDER.get(provider, 9),
        recent is None,
        -(recent or 0),
        natural_id,
        selector,
        str(identifier),
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


_INACTIVE_SUBAGENT_STATES = frozenset(
    {
        "idle",
        "complete",
        "completed",
        "closed",
        "stopped",
        "failed",
        "cancelled",
        "canceled",
    }
)


def classify_top_level_sessions(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return human roots, expandable machine roots, and hidden child orphans.

    A provider child can own a managed shell of its own.  That shell is still
    part of the parent conversation, not another top-level conversation.  The
    collector records the exact provider/UUID parent edge in ``parent_session``;
    this projection folds such a row into the highest present root and keeps an
    orphan behind the machine count.  Callers receive copies so rendering never
    mutates the collected snapshot.
    """

    copied = [dict(row) for row in rows if isinstance(row, Mapping)]

    def identity_key(row: Mapping[str, Any]) -> tuple[str, str] | None:
        identity = row.get("identity")
        uuid = identity.get("uuid") if isinstance(identity, Mapping) else None
        provider = str(row.get("provider") or "").casefold()
        if provider in {"claude", "codex"} and isinstance(uuid, str) and uuid:
            return provider, uuid.casefold()
        return None

    def parent_key(row: Mapping[str, Any]) -> tuple[str, str] | None:
        parent = row.get("parent_session")
        if not isinstance(parent, Mapping):
            return None
        provider = str(parent.get("provider") or "").casefold()
        uuid = parent.get("uuid")
        if provider in {"claude", "codex"} and isinstance(uuid, str) and uuid:
            return provider, uuid.casefold()
        return None

    by_identity = {key: row for row in copied if (key := identity_key(row)) is not None}

    def root_for(row: Mapping[str, Any]) -> dict[str, Any] | None:
        wanted = parent_key(row)
        seen: set[tuple[str, str]] = set()
        while wanted is not None and wanted not in seen:
            seen.add(wanted)
            candidate = by_identity.get(wanted)
            if candidate is None:
                return None
            next_parent = parent_key(candidate)
            if next_parent is None:
                return candidate
            wanted = next_parent
        return None

    orphans: list[dict[str, Any]] = []
    roots: list[dict[str, Any]] = []
    for row in copied:
        if row.get("is_subagent") is not True and parent_key(row) is None:
            roots.append(row)
            continue
        parent = root_for(row)
        if parent is None:
            orphans.append(row)
            continue
        nested = parent.get("subagents")
        summaries = (
            [dict(item) for item in nested if isinstance(item, Mapping)]
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes))
            else []
        )
        identity = row.get("identity")
        uuid = identity.get("uuid") if isinstance(identity, Mapping) else None
        pid = identity.get("pid") if isinstance(identity, Mapping) else None

        def same_child(item: Mapping[str, Any]) -> bool:
            return bool(
                (uuid and item.get("uuid") == uuid)
                or (isinstance(pid, int) and item.get("pid") == pid)
            )

        if not any(same_child(item) for item in summaries):
            summaries.append(
                {
                    "provider": row.get("provider"),
                    "uuid": uuid,
                    "pid": pid,
                    "title": row.get("display_title") or row.get("title") or "subagent",
                    "status": row.get("agent_status") or "unknown",
                }
            )
        parent["subagents"] = summaries
        parent["_grouped_subagent_count"] = (
            int(parent.get("_grouped_subagent_count") or 0) + 1
        )
        parent["active_subagent_count"] = sum(
            1
            for item in summaries
            if str(item.get("status") or "").casefold() not in _INACTIVE_SUBAGENT_STATES
        )
        if row.get("needs_you") is True:
            parent["needs_you"] = True

    machines = [row for row in roots if row.get("origin") == "machine"]
    people = [row for row in roots if row.get("origin") != "machine"]
    return people, machines, orphans


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
    native_name_source: str = "",
    native_name_since: Any = None,
    context_title: Callable[[str, str, int | None], str],
    pushed_titles: Mapping[str, str] | None = None,
    pending_native_titles: Mapping[str, Mapping[str, Any]] | None = None,
    name_ownership: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[str, str]:
    key = f"{provider}:{uuid}" if uuid else ""
    native_is_explicit = provider_title_is_explicit and not (
        provider == "claude" and native_name_source == "derived"
    )
    if (
        provider == "claude"
        and key
        and native_title
        and _pending_native_title_matches(
            (pending_native_titles or {}).get(key),
            native_title,
            native_name_since,
            native_name_source,
        )
        and ((pushed_titles or {}).get(key) or (automatic_titles or {}).get(key))
    ):
        native_is_explicit = False
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
        # Divergence is the timestamp once provider placeholders and the one
        # recorded pre-push value have been excluded above. `last_kit_title`
        # is the last value the kit wrote into the provider store, including
        # `sp name`; any third explicit value is therefore a newer human act.
        if (
            last_kit_title
            and native_title
            and native_is_explicit
            and native_title != last_kit_title
        ):
            return native_title, "native"
        if alias:
            return alias, "alias"
    if native_title and native_is_explicit:
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
    native_name_source: str = "",
    native_name_since: Any = None,
    provider_title_info: Callable[..., tuple[str, str]],
    pushed_titles: Mapping[str, str] | None = None,
    pending_native_titles: Mapping[str, Mapping[str, Any]] | None = None,
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
        native_name_source=native_name_source,
        native_name_since=native_name_since,
        pushed_titles=pushed_titles,
        pending_native_titles=pending_native_titles,
        name_ownership=name_ownership,
    )[0]


def _shell_title(
    tree: Sequence[int],
    root_pid: int,
    process_table: Mapping[int, Mapping[str, Any]],
    ignored: frozenset[int] = frozenset(),
) -> tuple[str, str, int | None]:
    """Name what the shell is running, never the inventory that is asking.

    ``ignored`` is the census's own process tree: its chain of ancestors and
    everything it spawned. A collection verb run inside a managed shell is a
    descendant of the shell it is titling — the provider-exit menu's reopen is
    exactly that call — so counting it made an idle terminal read as busy,
    the Idle-shell overlay never applied, and provider-exit recovery could
    never pass its gate.
    """
    for pid in tree:
        if pid == root_pid or pid in ignored:
            continue
        process = process_table.get(pid, {})
        comm = clean_text(process.get("comm"), 80)
        if comm and comm not in SHELL_PROCESS_NAMES:
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
    safe_subagents = [dict(child) for child in subagents if isinstance(child, Mapping)]
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
        "subagents": safe_subagents,
        # Detail-only process evidence.  It never contributes to state or the
        # active-subagent count.
        "aged_children": [],
        "active_subagent_count": sum(
            1
            for child in safe_subagents
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
