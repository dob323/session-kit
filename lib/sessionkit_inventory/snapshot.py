"""Collection and private-state orchestration: one exact refresh, or a fallback.

This module owns the order of a refresh rather than any of its steps. It takes a
live collection, and if that collection is complete it publishes the inventory,
the terminal-number registry and the recovery manifest under one lock, in the
order those files have to be written for a crash between any two of them to
leave usable state behind. If the collection is incomplete it serves the last
known good cache instead, marked stale and carrying the reason.

Nothing here reads a process table, a rollout, or a provider database. Every
step arrives as a call-time argument — the collector, the boot identity, the
state reader and publisher, the terminal helpers, the recovery transaction, the
lifecycle passes — so this file can be read as the sequence itself, and so a
facade patch on any single step still reaches the refresh under test.

The two modes are not symmetrical and the asymmetry is the point. ``write_state``
takes the lock, re-reads the cache inside it, and allocates terminal numbers.
``--no-write`` does none of that: it must not create a state directory or a lock
file merely to inspect existing state, because deployment validation runs it
against installations it is not allowed to modify.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .common import CollectionError, _utc_now, clean_text


def _cold_inventory(error: str, *, schema_version: int) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "generated_at": _utc_now(),
        "source": "cold",
        "stale": True,
        "warnings": [clean_text(error, 400)],
        "daemon_generation": None,
        "sessions": [],
        "outside_agents": [],
    }


def snapshot(
    *,
    write_state: bool = True,
    config: dict[str, Any] | None = None,
    schema_version: int,
    load_config: Callable[[], dict[str, Any]],
    state_paths: Callable[[Mapping[str, Any]], dict[str, Path]],
    state_lock: Callable[..., Any],
    read_state_json: Callable[[Path], Any],
    collect_live: Callable[[Mapping[str, Any]], dict[str, Any]],
    boot_id_factory: Callable[[], str | None],
    persist_last_exact: Callable[..., Any],
    apply_provider_exit_states: Callable[..., Any],
    prune_inactive_state: Callable[..., Any],
    read_terminal_registry: Callable[..., dict[str, Any]],
    read_terminal_retirements: Callable[[Path, str], dict[int, float]],
    apply_terminal_numbers: Callable[..., dict[str, Any]],
    terminal_retirement_payload: Callable[[Mapping[int, float], str], dict[str, Any]],
    atomic_write_json: Callable[[Path, Any], None],
    quarantine_orphaned_provider_untitled_markers: Callable[
        [Mapping[str, Any], Mapping[str, Any]], list[dict[str, Any]]
    ],
    update_recovery_state: Callable[[Mapping[str, Path], Mapping[str, Any]], None],
    cold_inventory: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    settings = config or load_config()
    paths = state_paths(settings)
    if write_state:
        with state_lock(paths["root"], paths["lock"]):
            cached = read_state_json(paths["inventory"])
    else:
        # --no-write is also useful in deployment validation.  Do not create a
        # state directory or lock file merely to inspect existing state.
        cached = read_state_json(paths["inventory"])
    try:
        live = collect_live(settings)
    except CollectionError as exc:
        live = None
        failure = str(exc)
    else:
        assert live is not None
        failure = "; ".join(live.get("warnings", ()))
    complete = bool(live and live.pop("_complete", True))
    if complete and live is not None:
        result: dict[str, Any] = live
        try:
            boot_id = boot_id_factory()
            if not boot_id:
                raise CollectionError("current boot identity is unavailable")
            if write_state:
                with state_lock(paths["root"], paths["lock"]):
                    locked_cached = read_state_json(paths["inventory"])
                    persist_last_exact(
                        result,
                        (locked_cached if isinstance(locked_cached, Mapping) else None),
                        state_dir=paths["root"],
                        boot_id=boot_id,
                    )
                    apply_provider_exit_states(
                        result,
                        (locked_cached if isinstance(locked_cached, Mapping) else None),
                        state_dir=paths["root"],
                        boot_id=boot_id,
                    )
                    registry = read_terminal_registry(
                        paths["terminal_numbers"],
                        boot_id,
                        paths["terminal_numbers_epoch"],
                    )
                    retired_numbers = read_terminal_retirements(
                        paths["terminal_numbers_retired"], boot_id
                    )
                    updated_registry = apply_terminal_numbers(
                        result,
                        registry,
                        boot_id=boot_id,
                        allocate=True,
                        retired=retired_numbers,
                    )
                    atomic_write_json(paths["terminal_numbers"], updated_registry)
                    atomic_write_json(
                        paths["terminal_numbers_retired"],
                        terminal_retirement_payload(retired_numbers, boot_id),
                    )
                    atomic_write_json(
                        paths["terminal_numbers_epoch"],
                        {
                            "schema_version": schema_version,
                            "boot_id": boot_id,
                        },
                    )
                    quarantined_markers = quarantine_orphaned_provider_untitled_markers(
                        settings, result
                    )
                    if quarantined_markers:
                        result["provider_untitled_quarantine"] = quarantined_markers
                    atomic_write_json(paths["inventory"], result)
                    update_recovery_state(paths, result)
                    prune_inactive_state(
                        paths["root"],
                        [
                            item["shpool_id_raw"]
                            for item in result.get("sessions", ())
                            if isinstance(item, Mapping)
                            and isinstance(item.get("shpool_id_raw"), str)
                        ],
                    )
            else:
                apply_provider_exit_states(
                    result,
                    cached if isinstance(cached, Mapping) else None,
                    state_dir=paths["root"],
                    boot_id=boot_id,
                )
                registry = read_terminal_registry(
                    paths["terminal_numbers"],
                    boot_id,
                    paths["terminal_numbers_epoch"],
                )
                apply_terminal_numbers(
                    result, registry, boot_id=boot_id, allocate=False
                )
        except CollectionError as exc:
            failure = str(exc)
            warnings = list(result.get("warnings", ()))
            warnings.append(f"terminal numbering unavailable: {failure}")
            result["warnings"] = warnings
            complete = False
        if complete:
            return result
    if isinstance(cached, Mapping) and cached.get("schema_version") == schema_version:
        result = dict(cached)
        result["source"] = "cache"
        result["stale"] = True
        warnings = list(result.get("warnings", ()))
        warnings.append(f"live refresh failed; showing last-known-good: {failure}")
        result["warnings"] = warnings
        return result
    if live is not None:
        live["source"] = "cold"
        live["stale"] = True
        return live
    return cold_inventory(
        f"inventory unavailable and no last-known-good cache exists: {failure}"
    )
