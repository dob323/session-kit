"""Exact recovery transactions: the manifest, the pending queue, and the ack.

Recovery state is the only private state that has to outlive the event it
describes. ``recovery-manifest.json`` holds the latest exact generation;
``recovery-pending.json`` holds the generations that vanished before anyone
claimed them. Nothing here decides when to run — the snapshot does that — and
nothing here reads a clock, a process table, or a configuration file.

Every collaborator arrives as a call-time argument instead of an import. That
is deliberate rather than uniform style: recovery rewrites the state a person
depends on immediately after losing a session, so a facade patch aimed at the
state reader, the publisher, the live collector, or the strict live guard has to
reach the transaction it was aimed at. A patch that silently applies against
nothing is worse in this module than anywhere else in the tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .common import (
    PROVIDERS,
    CollectionError,
    _utc_now,
    clean_text,
    display_shpool_id,
    natural_name_key,
    valid_uuid,
)


def recovery_manifest(
    inventory: Mapping[str, Any],
    *,
    schema_version: int,
    boot_id: Callable[[], str | None],
) -> dict[str, Any]:
    sessions: dict[str, dict[str, Any]] = {}
    for item in inventory.get("sessions", ()):
        recovery = item.get("recovery", {})
        shpool_id = item.get("shpool_id_raw") or item.get("shpool_id")
        if not shpool_id or not recovery.get("available"):
            continue
        sessions[shpool_id] = {
            "scope": "shpool",
            "provider": recovery["provider"],
            "uuid": recovery["uuid"],
            "title": item.get("title", ""),
            "cwd": recovery.get("cwd"),
            "argv": recovery.get("argv", []),
            "command": recovery.get("command"),
        }
    outside_agents: dict[str, dict[str, Any]] = {}
    for item in inventory.get("outside_agents", ()):
        recovery = item.get("recovery", {})
        provider = recovery.get("provider")
        uuid = valid_uuid(recovery.get("uuid"))
        if not recovery.get("available") or provider not in PROVIDERS or not uuid:
            continue
        key = f"outside:{provider}:{uuid}"
        outside_agents[key] = {
            "scope": "outside",
            "provider": provider,
            "uuid": uuid,
            "title": item.get("title", ""),
            "cwd": recovery.get("cwd"),
            "argv": recovery.get("argv", []),
            "command": recovery.get("command"),
        }
    return {
        "schema_version": schema_version,
        "generated_at": inventory.get("generated_at"),
        "boot_id": boot_id(),
        "daemon_generation": inventory.get("daemon_generation"),
        "sessions": sessions,
        "outside_agents": outside_agents,
    }


def _generation_key(value: Mapping[str, Any]) -> tuple[str, int, int] | None:
    generation = value.get("daemon_generation")
    if not isinstance(generation, Mapping):
        return None
    boot_id = value.get("boot_id")
    pid = generation.get("pid")
    start_ticks = generation.get("process_start_ticks")
    if (
        not isinstance(boot_id, str)
        or not boot_id
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(start_ticks, int)
        or start_ticks <= 0
    ):
        return None
    return boot_id, pid, start_ticks


def _valid_recovery_state(value: Any, *, schema_version: int) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("schema_version") == schema_version
        and isinstance(value.get("sessions"), Mapping)
        and (
            "outside_agents" not in value
            or isinstance(value.get("outside_agents"), Mapping)
        )
    )


def _has_recovery_entries(
    value: Any,
    *,
    valid_recovery_state: Callable[[Any], bool],
) -> bool:
    return bool(
        valid_recovery_state(value)
        and (value.get("sessions") or value.get("outside_agents"))
    )


def update_recovery_state(
    paths: Mapping[str, Path],
    inventory: Mapping[str, Any],
    *,
    schema_version: int,
    recovery_manifest: Callable[[Mapping[str, Any]], dict[str, Any]],
    read_state_json: Callable[[Path], Any],
    has_recovery_entries: Callable[[Any], bool],
    generation_key: Callable[[Mapping[str, Any]], tuple[str, int, int] | None],
    atomic_write_json: Callable[[Path, Any], None],
) -> None:
    """Preserve exact pre-generation recovery data until explicitly resolved.

    ``recovery-manifest.json`` is the latest nonempty generation.  When a new
    boot or daemon generation appears, the prior manifest is copied to
    ``recovery-pending.json`` before the current manifest can advance.  Empty
    and partial post-reboot inventories therefore cannot erase the queue.
    """
    detected = recovery_manifest(inventory)
    existing = read_state_json(paths["manifest"])
    pending = read_state_json(paths["pending"])
    existing_valid = has_recovery_entries(existing)
    pending_valid = has_recovery_entries(pending)
    detected_key = generation_key(detected)
    existing_key = generation_key(existing) if existing_valid else None

    # An unavailable/ambiguous daemon identity is not evidence of a restart.
    # Do not advance or queue anything until both sides have exact generation
    # keys; a later complete refresh can make the transition safely.
    if detected_key is None or (existing_valid and existing_key is None):
        return

    if existing_valid and existing_key == detected_key:
        existing_semantic = dict(existing)
        detected_semantic = dict(detected)
        existing_semantic.pop("generated_at", None)
        detected_semantic.pop("generated_at", None)
        if existing_semantic == detected_semantic:
            # Timestamp-only refreshes must not invalidate an exact migration
            # postimage or create needless state churn.
            return

    if existing_valid and existing_key != detected_key:
        assert existing_key is not None
        boot_changed = existing_key[0] != detected_key[0]
        new_pending = {
            "schema_version": schema_version,
            "generated_at": inventory.get("generated_at"),
            "source_boot_id": existing.get("boot_id"),
            "source_daemon_generation": existing.get("daemon_generation"),
            "detected_boot_id": detected.get("boot_id"),
            "detected_daemon_generation": detected.get("daemon_generation"),
            "sessions": dict(existing["sessions"]),
            # Outside-provider roots survive a daemon-only restart; they are
            # recovery work only when the host boot itself changed.
            "outside_agents": (
                dict(existing.get("outside_agents", {})) if boot_changed else {}
            ),
        }
        if pending_valid:
            pending_source = (
                pending.get("source_boot_id"),
                json.dumps(pending.get("source_daemon_generation"), sort_keys=True),
            )
            new_source = (
                new_pending.get("source_boot_id"),
                json.dumps(new_pending.get("source_daemon_generation"), sort_keys=True),
            )
            if pending_source == new_source:
                # Never shrink an unresolved queue during a partial restore.
                new_pending["sessions"] = {
                    **new_pending["sessions"],
                    **dict(pending["sessions"]),
                }
                new_pending["outside_agents"] = {
                    **new_pending.get("outside_agents", {}),
                    **dict(pending.get("outside_agents", {})),
                }
            else:
                # A second generation arrived before the first was reviewed.
                # Keep the oldest queue primary and retain the later one
                # explicitly instead of silently overwriting either.
                new_pending = dict(pending)
                queued = list(new_pending.get("queued_generations", ()))
                candidate = {
                    "source_boot_id": existing.get("boot_id"),
                    "source_daemon_generation": existing.get("daemon_generation"),
                    "sessions": dict(existing["sessions"]),
                    "outside_agents": (
                        dict(existing.get("outside_agents", {})) if boot_changed else {}
                    ),
                }
                candidate_key = (
                    candidate["source_boot_id"],
                    json.dumps(candidate["source_daemon_generation"], sort_keys=True),
                )
                existing_keys = {
                    (
                        item.get("source_boot_id"),
                        json.dumps(
                            item.get("source_daemon_generation"), sort_keys=True
                        ),
                    )
                    for item in queued
                    if isinstance(item, Mapping)
                }
                if candidate_key not in existing_keys:
                    queued.append(candidate)
                new_pending["queued_generations"] = queued
                new_pending["detected_boot_id"] = detected.get("boot_id")
                new_pending["detected_daemon_generation"] = detected.get(
                    "daemon_generation"
                )
        if has_recovery_entries(new_pending):
            atomic_write_json(paths["pending"], new_pending)

    # Never replace the latest nonempty exact manifest with an empty snapshot.
    if detected["sessions"] or detected["outside_agents"]:
        atomic_write_json(paths["manifest"], detected)


def source_generation_key(
    boot_id: Any,
    generation: Any,
    *,
    generation_key: Callable[[Mapping[str, Any]], tuple[str, int, int] | None],
) -> str | None:
    if not isinstance(generation, Mapping):
        return None
    candidate = {
        "boot_id": boot_id,
        "daemon_generation": generation,
    }
    exact = generation_key(candidate)
    if exact is None:
        return None
    return f"{exact[0]}:{exact[1]}:{exact[2]}"


def _pending_evidence(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_generation_key": entry.get("source_generation_key"),
        "queue": entry.get("queue"),
        "queue_index": entry.get("queue_index"),
        "scope": entry.get("scope"),
        "old_shpool_id": entry.get("old_shpool_id"),
    }


def _pending_conflict_fields(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    fields = ("scope", "cwd", "argv", "command")
    return [
        field
        for field in fields
        if len({json.dumps(item.get(field), sort_keys=True) for item in entries}) > 1
    ]


def _pending_preferred_entry(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    primary = [item for item in entries if item.get("queue") == "primary"]
    if primary:
        return primary[0]

    def queue_index(item: dict[str, Any]) -> int:
        value = item.get("queue_index")
        return value if isinstance(value, int) else -1

    return max(entries, key=queue_index)


def flatten_pending(
    value: Any,
    *,
    schema_version: int,
    valid_recovery_state: Callable[[Any], bool],
    source_generation_key: Callable[[Any, Any], str | None],
    pending_preferred_entry: Callable[[Sequence[dict[str, Any]]], dict[str, Any]],
    pending_conflict_fields: Callable[[Sequence[Mapping[str, Any]]], list[str]],
    pending_evidence: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Return one safe recovery candidate per exact provider conversation."""
    entries: list[dict[str, Any]] = []
    if not valid_recovery_state(value):
        return {
            "schema_version": schema_version,
            "generated_at": None,
            "entries": [],
        }
    generations: list[
        tuple[
            str,
            int | None,
            Any,
            Any,
            Mapping[str, Any],
            Mapping[str, Any],
        ]
    ] = [
        (
            "primary",
            None,
            value.get("source_boot_id"),
            value.get("source_daemon_generation"),
            value.get("sessions", {}),
            value.get("outside_agents", {}),
        )
    ]
    for index, queued in enumerate(value.get("queued_generations", ())):
        if not isinstance(queued, Mapping) or not isinstance(
            queued.get("sessions"), Mapping
        ):
            continue
        generations.append(
            (
                "queued",
                index,
                queued.get("source_boot_id"),
                queued.get("source_daemon_generation"),
                queued["sessions"],
                queued.get("outside_agents", {})
                if isinstance(queued.get("outside_agents", {}), Mapping)
                else {},
            )
        )
    for (
        queue,
        queue_index,
        boot_id,
        generation,
        sessions,
        outside_agents,
    ) in generations:
        key = source_generation_key(boot_id, generation)
        scoped_entries = (
            ("shpool", sessions),
            ("outside", outside_agents),
        )
        for scope, stored_entries in scoped_entries:
            for old_shpool_id, raw_session in stored_entries.items():
                if not isinstance(old_shpool_id, str) or not isinstance(
                    raw_session, Mapping
                ):
                    continue
                uuid = valid_uuid(raw_session.get("uuid"))
                provider = raw_session.get("provider")
                entries.append(
                    {
                        "source_generation_key": key,
                        "source_boot_id": boot_id,
                        "source_daemon_generation": generation,
                        "queue": queue,
                        "queue_index": queue_index,
                        "scope": scope,
                        "old_shpool_id": old_shpool_id,
                        "display_old_shpool_id": display_shpool_id(old_shpool_id),
                        "provider": provider if provider in PROVIDERS else None,
                        "uuid": uuid,
                        "title": clean_text(raw_session.get("title"), 120),
                        "cwd": clean_text(raw_session.get("cwd"), 4096) or None,
                        "argv": list(raw_session.get("argv", ()))
                        if isinstance(raw_session.get("argv"), list)
                        else [],
                        "command": clean_text(raw_session.get("command"), 8192) or None,
                        "actionable": bool(key and uuid and provider in PROVIDERS),
                    }
                )
    entries.sort(
        key=lambda item: (
            item["source_generation_key"] or "",
            natural_name_key(item["old_shpool_id"]),
            item["uuid"] or "",
        )
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    ungrouped: list[dict[str, Any]] = []
    for entry in entries:
        provider = entry.get("provider")
        uuid = entry.get("uuid")
        if provider in PROVIDERS and isinstance(uuid, str):
            grouped.setdefault((provider, uuid), []).append(entry)
        else:
            ungrouped.append(entry)
    deduplicated: list[dict[str, Any]] = []
    for duplicates in grouped.values():
        preferred = dict(pending_preferred_entry(duplicates))
        conflicts = pending_conflict_fields(duplicates)
        preferred["duplicate_count"] = len(duplicates) - 1
        preferred["evidence"] = [pending_evidence(item) for item in duplicates]
        preferred["conflict_fields"] = conflicts
        preferred["actionable"] = bool(preferred["actionable"] and not conflicts)
        deduplicated.append(preferred)
    for entry in ungrouped:
        retained = dict(entry)
        retained.update(
            duplicate_count=0,
            evidence=[pending_evidence(entry)],
            conflict_fields=[],
        )
        deduplicated.append(retained)
    deduplicated.sort(
        key=lambda item: (
            item["source_generation_key"] or "",
            natural_name_key(item["old_shpool_id"]),
            item["uuid"] or "",
        )
    )
    return {
        "schema_version": schema_version,
        "generated_at": value.get("generated_at"),
        "detected_boot_id": value.get("detected_boot_id"),
        "detected_daemon_generation": value.get("detected_daemon_generation"),
        "entries": deduplicated,
    }


def list_pending(
    config: Mapping[str, Any],
    *,
    state_paths: Callable[[Mapping[str, Any]], dict[str, Path]],
    state_lock: Callable[..., Any],
    read_state_json: Callable[[Path], Any],
    flatten_pending: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    paths = state_paths(config)
    with state_lock(paths["root"], paths["lock"]):
        return flatten_pending(read_state_json(paths["pending"]))


def _remove_pending_entry(
    pending: dict[str, Any],
    *,
    queue: str,
    queue_index: int | None,
    scope: str,
    old_shpool_id: str,
) -> None:
    container = "outside_agents" if scope == "outside" else "sessions"
    if queue == "primary":
        sessions = pending.get(container)
        if isinstance(sessions, dict):
            sessions.pop(old_shpool_id, None)
        return
    queued = pending.get("queued_generations")
    if not isinstance(queued, list) or not isinstance(queue_index, int):
        raise CollectionError("pending queue changed during acknowledgment")
    if queue_index < 0 or queue_index >= len(queued):
        raise CollectionError("pending generation changed during acknowledgment")
    generation = queued[queue_index]
    if not isinstance(generation, dict) or not isinstance(
        generation.get(container), dict
    ):
        raise CollectionError("pending generation is malformed")
    generation[container].pop(old_shpool_id, None)
    if not generation.get("sessions") and not generation.get("outside_agents"):
        queued.pop(queue_index)


def acknowledge_pending(
    config: Mapping[str, Any],
    generation_key: str,
    old_shpool_id: str,
    uuid: str,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    schema_version: int,
    state_paths: Callable[[Mapping[str, Any]], dict[str, Path]],
    state_lock: Callable[..., Any],
    read_state_json: Callable[[Path], Any],
    valid_recovery_state: Callable[[Any], bool],
    flatten_pending: Callable[[Any], dict[str, Any]],
    collect_live: Callable[[Mapping[str, Any]], dict[str, Any]],
    strict_live_inventory: Callable[[Mapping[str, Any]], bool],
    remove_pending_entry: Callable[..., None],
    atomic_write_json: Callable[[Path, Any], None],
) -> dict[str, Any]:
    """Compare-and-ack one restored entry under the inventory state lock."""
    exact_uuid = valid_uuid(uuid)
    if not generation_key or not old_shpool_id or not exact_uuid:
        raise CollectionError(
            "pending ack requires generation key, old shpool ID, and exact UUID"
        )
    paths = state_paths(config)
    with state_lock(paths["root"], paths["lock"]):
        pending_raw = read_state_json(paths["pending"])
        if not valid_recovery_state(pending_raw):
            raise CollectionError("no valid pending recovery queue exists")
        pending = json.loads(json.dumps(pending_raw))
        flattened = flatten_pending(pending)
        matches = [
            item
            for item in flattened["entries"]
            if item["source_generation_key"] == generation_key
            and item["old_shpool_id"] == old_shpool_id
            and item["uuid"] == exact_uuid
            and item["actionable"]
        ]
        if len(matches) != 1:
            raise CollectionError(
                "pending entry no longer matches generation, shpool ID, and UUID"
            )
        target = matches[0]

        settings = dict(config)
        live = collector(settings) if collector is not None else collect_live(settings)
        complete = bool(live.pop("_complete", True))
        if not complete or not strict_live_inventory(live):
            raise CollectionError(
                "strict live inventory unavailable; pending entry was not acknowledged"
            )
        active_matches = [
            item
            for item in [*live.get("sessions", ()), *live.get("outside_agents", ())]
            if item.get("provider") == target["provider"]
            and item.get("identity", {}).get("uuid") == exact_uuid
            and item.get("identity", {}).get("confidence") == "exact"
        ]
        if len(active_matches) != 1:
            raise CollectionError(
                "exact pending provider UUID is not uniquely active; pending entry was not acknowledged"
            )
        evidence = target.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise CollectionError("pending recovery evidence is incomplete")
        ordered_evidence = sorted(
            evidence,
            key=lambda item: (
                item.get("queue") == "primary",
                -(
                    item.get("queue_index")
                    if isinstance(item.get("queue_index"), int)
                    else -1
                ),
            ),
        )
        for source in ordered_evidence:
            if not isinstance(source, Mapping):
                raise CollectionError("pending recovery evidence is malformed")
            remove_pending_entry(
                pending,
                queue=str(source.get("queue") or ""),
                queue_index=source.get("queue_index"),
                scope=str(source.get("scope") or ""),
                old_shpool_id=str(source.get("old_shpool_id") or ""),
            )
        pending["updated_at"] = _utc_now()
        atomic_write_json(paths["pending"], pending)
        return {
            "schema_version": schema_version,
            "acknowledged": target,
            "remaining": flatten_pending(pending),
        }
