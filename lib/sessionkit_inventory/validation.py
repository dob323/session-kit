"""Strict validation of snapshots and operator-supplied inventory input."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .common import (
    PROVIDERS,
    CollectionError,
    clean_text,
    shpool_id_mutation_policy,
    valid_uuid,
)


def _missing_shell_generation_is_quarantinable(
    item: Mapping[str, Any],
) -> bool:
    """Recognize only an inert disconnected shpool row with no live shell."""
    identity = item.get("identity")
    recovery = item.get("recovery")
    raw_id = item.get("shpool_id_raw")
    started = item.get("started_at_unix_ms")
    diagnostics = item.get("diagnostics")
    expected_diagnostic = (
        f"expected one daemon child for {raw_id!r}, found 0"
        if isinstance(raw_id, str)
        else ""
    )
    return (
        item.get("provider") == "unknown"
        and item.get("display_provider") == "unknown"
        and item.get("availability") == "ready"
        and clean_text(item.get("shpool_status"), 32).casefold() == "disconnected"
        and isinstance(raw_id, str)
        and bool(raw_id)
        and raw_id == item.get("shpool_id")
        and shpool_id_mutation_policy(raw_id) == (True, None)
        and isinstance(started, int)
        and not isinstance(started, bool)
        and started > 0
        and item.get("shpool_shell") is None
        and isinstance(identity, Mapping)
        and identity.get("confidence") == "unknown"
        and identity.get("uuid") is None
        and identity.get("pid") is None
        and identity.get("process_start_ticks") is None
        and identity.get("provenance") == "none"
        and isinstance(recovery, Mapping)
        and recovery.get("available") is False
        and recovery.get("provider") is None
        and recovery.get("uuid") is None
        and not isinstance(item.get("_terminal_identity_hint"), Mapping)
        and isinstance(diagnostics, list)
        and expected_diagnostic in diagnostics
    )


def _missing_shell_generation_is_quarantined(
    item: Mapping[str, Any],
) -> bool:
    return (
        _missing_shell_generation_is_quarantinable(item)
        and item.get("terminal_number") is None
        and item.get("mutation_allowed") is False
        and item.get("mutation_rejection_reason") == "missing-shell-generation"
    )


def guard_live_inventory(
    inventory: Mapping[str, Any],
    *,
    schema_version: int,
) -> bool:
    """Validate a live mutation snapshot without rejecting unrelated unknown rows.

    Unlike ``strict_live_inventory``, this predicate permits an unresolved
    provider session.  It still requires an exact daemon generation, an exact
    shell generation for every managed row, and exact provider identity for
    every row whose provider is known.
    """

    def positive_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    if not isinstance(inventory, Mapping):
        return False
    generation = inventory.get("daemon_generation")
    warnings = inventory.get("warnings")
    sessions = inventory.get("sessions")
    outside_agents = inventory.get("outside_agents")
    if (
        inventory.get("schema_version") != schema_version
        or inventory.get("source") != "live"
        or inventory.get("stale") is not False
        or not isinstance(warnings, list)
        or warnings
        or not isinstance(generation, Mapping)
        or not positive_int(generation.get("pid"))
        or not positive_int(generation.get("process_start_ticks"))
        or not isinstance(sessions, list)
        or not isinstance(outside_agents, list)
    ):
        return False

    rows: set[int] = set()
    terminal_numbers: set[int] = set()
    raw_ids: set[str] = set()
    exact_provider_uuids: set[tuple[str, str]] = set()
    for item in sessions:
        if not isinstance(item, Mapping):
            return False
        row = item.get("row")
        raw_id = item.get("shpool_id_raw")
        identity = item.get("identity")
        shell = item.get("shpool_shell")
        provider = item.get("provider")
        terminal_number = item.get("terminal_number")
        if _missing_shell_generation_is_quarantined(item):
            if (
                isinstance(row, bool)
                or not isinstance(row, int)
                or row <= 0
                or row in rows
                or not isinstance(raw_id, str)
                or raw_id in raw_ids
            ):
                return False
            rows.add(row)
            raw_ids.add(raw_id)
            continue
        mutation_allowed, mutation_reason = shpool_id_mutation_policy(raw_id)
        if (
            isinstance(row, bool)
            or not isinstance(row, int)
            or row <= 0
            or row in rows
            or not isinstance(raw_id, str)
            or not raw_id
            or raw_id in raw_ids
            or raw_id != item.get("shpool_id")
            or item.get("mutation_allowed") is not mutation_allowed
            or item.get("mutation_rejection_reason") != mutation_reason
            or not isinstance(identity, Mapping)
            or not isinstance(shell, Mapping)
            or not positive_int(shell.get("pid"))
            or not positive_int(shell.get("process_start_ticks"))
        ):
            return False
        if terminal_number is not None and (
            isinstance(terminal_number, bool)
            or not isinstance(terminal_number, int)
            or terminal_number <= 0
            or terminal_number in terminal_numbers
        ):
            return False
        if isinstance(terminal_number, int):
            terminal_numbers.add(terminal_number)
        rows.add(row)
        raw_ids.add(raw_id)

        if provider == "unknown":
            if (
                identity.get("confidence") != "unknown"
                or identity.get("uuid") is not None
                or identity.get("pid") != shell.get("pid")
                or identity.get("process_start_ticks")
                != shell.get("process_start_ticks")
            ):
                return False
            continue
        if provider not in {"claude", "codex", "shell"}:
            return False
        if (
            identity.get("confidence") != "exact"
            or not positive_int(identity.get("pid"))
            or not positive_int(identity.get("process_start_ticks"))
        ):
            return False
        if provider in {"claude", "codex"}:
            uuid = valid_uuid(identity.get("uuid"))
            if not uuid:
                return False
            key = (provider, uuid)
            if key in exact_provider_uuids:
                return False
            exact_provider_uuids.add(key)
        elif identity.get("uuid") is not None:
            return False

    for item in outside_agents:
        if not isinstance(item, Mapping):
            return False
        provider = item.get("provider")
        identity = item.get("identity")
        if (
            provider not in PROVIDERS
            or not isinstance(identity, Mapping)
            or identity.get("confidence") != "exact"
            or not positive_int(identity.get("pid"))
            or not positive_int(identity.get("process_start_ticks"))
        ):
            return False
        if not isinstance(provider, str):
            return False
        uuid = valid_uuid(identity.get("uuid"))
        if not uuid:
            return False
        key = (provider, uuid)
        if key in exact_provider_uuids:
            return False
        exact_provider_uuids.add(key)

    return rows == set(range(1, len(sessions) + 1))


def strict_live_inventory(
    inventory: Mapping[str, Any],
    *,
    guard_inventory: Callable[[Mapping[str, Any]], bool],
) -> bool:
    """True only for a complete, unambiguous live mutation guard snapshot."""
    if not guard_inventory(inventory):
        return False
    generation = inventory.get("daemon_generation")
    outside_agents = inventory.get("outside_agents")
    if (
        inventory.get("source") != "live"
        or inventory.get("stale") is not False
        or inventory.get("warnings")
        or not isinstance(generation, Mapping)
        or not isinstance(generation.get("pid"), int)
        or generation.get("pid", 0) <= 0
        or not isinstance(generation.get("process_start_ticks"), int)
        or generation.get("process_start_ticks", 0) <= 0
        or not isinstance(outside_agents, list)
    ):
        return False
    exact_provider_uuids: set[tuple[str, str]] = set()
    for item in inventory.get("sessions", ()):
        identity = item.get("identity")
        shell = item.get("shpool_shell")
        raw_id = item.get("shpool_id_raw")
        provider = item.get("provider")
        mutation_allowed, mutation_reason = shpool_id_mutation_policy(raw_id)
        if (
            not isinstance(raw_id, str)
            or raw_id != item.get("shpool_id")
            or item.get("mutation_allowed") is not mutation_allowed
            or item.get("mutation_rejection_reason") != mutation_reason
            or provider not in {"claude", "codex", "shell"}
            or not isinstance(identity, Mapping)
            or identity.get("confidence") != "exact"
            or not isinstance(identity.get("pid"), int)
            or not isinstance(identity.get("process_start_ticks"), int)
            or not isinstance(shell, Mapping)
            or not isinstance(shell.get("pid"), int)
            or not isinstance(shell.get("process_start_ticks"), int)
        ):
            return False
        if provider in PROVIDERS:
            if not isinstance(provider, str):
                return False
            uuid = valid_uuid(identity.get("uuid"))
            if not uuid:
                return False
            key = (provider, uuid)
            if key in exact_provider_uuids:
                return False
            exact_provider_uuids.add(key)
        elif identity.get("uuid") is not None:
            return False
    for item in outside_agents:
        if not isinstance(item, Mapping):
            return False
        provider = item.get("provider")
        identity = item.get("identity")
        if (
            provider not in PROVIDERS
            or not isinstance(identity, Mapping)
            or identity.get("confidence") != "exact"
            or isinstance(identity.get("pid"), bool)
            or not isinstance(identity.get("pid"), int)
            or identity.get("pid", 0) <= 0
            or isinstance(identity.get("process_start_ticks"), bool)
            or not isinstance(identity.get("process_start_ticks"), int)
            or identity.get("process_start_ticks", 0) <= 0
        ):
            return False
        if not isinstance(provider, str):
            return False
        uuid = valid_uuid(identity.get("uuid"))
        if not uuid:
            return False
        key = (provider, uuid)
        if key in exact_provider_uuids:
            return False
        exact_provider_uuids.add(key)
    return True


def load_inventory_input(
    path: str | Path,
    *,
    schema_version: int,
    load_json_file: Callable[[Path], Any],
) -> dict[str, Any]:
    """Load a previously frozen v1 snapshot for TOCTOU-safe render/lookup."""
    source = Path(path).expanduser()
    try:
        value = load_json_file(source)
    except (OSError, ValueError) as exc:
        raise CollectionError(f"cannot read inventory input {source}: {exc}") from exc
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != schema_version
        or not isinstance(value.get("sessions"), list)
        or not isinstance(value.get("outside_agents"), list)
    ):
        raise CollectionError(f"inventory input {source} is not a valid v1 snapshot")
    names: set[str] = set()
    rows: set[int] = set()
    terminal_numbers: set[int] = set()
    has_terminal_numbers = any(
        isinstance(item, Mapping)
        and isinstance(item.get("terminal_number"), int)
        and not isinstance(item.get("terminal_number"), bool)
        and item.get("terminal_number", 0) > 0
        for item in value["sessions"]
    )
    for item in value["sessions"]:
        if not isinstance(item, Mapping):
            raise CollectionError(f"inventory input {source} has a non-object session")
        name = item.get("shpool_id")
        raw_name = item.get("shpool_id_raw", name)
        row = item.get("row")
        terminal_number = item.get("terminal_number")
        if (
            not isinstance(name, str)
            or not name
            or raw_name != name
            or not isinstance(row, int)
            or row <= 0
            or name in names
            or row in rows
            or (
                has_terminal_numbers
                and _missing_shell_generation_is_quarantinable(item)
                and not _missing_shell_generation_is_quarantined(item)
            )
            or (
                has_terminal_numbers
                and terminal_number is None
                and not _missing_shell_generation_is_quarantined(item)
            )
            or (
                has_terminal_numbers
                and terminal_number is not None
                and (
                    isinstance(terminal_number, bool)
                    or not isinstance(terminal_number, int)
                    or terminal_number <= 0
                    or terminal_number in terminal_numbers
                )
            )
        ):
            raise CollectionError(
                f"inventory input {source} has invalid or duplicate selectors"
            )
        names.add(name)
        rows.add(row)
        if has_terminal_numbers and terminal_number is not None:
            terminal_numbers.add(terminal_number)
    return dict(value)
