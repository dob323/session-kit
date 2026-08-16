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
            "started_at_unix_ms": item.get("started_at_unix_ms"),
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
            "started_at_unix_ms": item.get("started_at_unix_ms"),
            "cwd": recovery.get("cwd"),
            "argv": recovery.get("argv", []),
            "command": recovery.get("command"),
        }
    return {
        "schema_version": schema_version,
        "generated_at": inventory.get("generated_at"),
        "collection_start": inventory.get("collection_start"),
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


def _pending_conversation_keys(value: Any, *, schema_version: int) -> set[str]:
    found: set[str] = set()
    if not _valid_recovery_state(value, schema_version=schema_version):
        return found
    generations: list[Mapping[str, Any]] = [value]
    generations.extend(
        item
        for item in value.get("queued_generations", ())
        if isinstance(item, Mapping)
    )
    for generation in generations:
        if _generation_key(
            {
                "boot_id": generation.get("source_boot_id"),
                "daemon_generation": generation.get("source_daemon_generation"),
            }
        ) is None:
            continue
        for group in ("sessions", "outside_agents"):
            entries = generation.get(group, {})
            if not isinstance(entries, Mapping):
                continue
            for entry in entries.values():
                if not isinstance(entry, Mapping):
                    continue
                provider = entry.get("provider")
                uuid = valid_uuid(entry.get("uuid"))
                if provider in PROVIDERS and uuid:
                    found.add(f"{provider}:{uuid}")
    return found


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
    collection_start: int | None = None,
    write_collection_json: Callable[..., None] | None = None,
) -> None:
    """Preserve exact pre-generation recovery data until explicitly resolved.

    ``recovery-manifest.json`` is the latest nonempty generation.  When a new
    boot or daemon generation appears, the prior manifest is copied to
    ``recovery-pending.json`` before the current manifest can advance.  Empty
    and partial post-reboot inventories therefore cannot erase the queue.
    """
    detected = recovery_manifest(inventory)
    publish = (
        (lambda path, payload: write_collection_json(
            paths, path, payload, collection_start=collection_start
        ))
        if write_collection_json is not None
        else atomic_write_json
    )
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
            publish(paths["pending"], new_pending)
            expected = _pending_conversation_keys(
                new_pending, schema_version=schema_version
            )
            try:
                published_pending = read_state_json(paths["pending"])
            except (OSError, ValueError) as exc:
                expected_text = ", ".join(sorted(expected))
                raise CollectionError(
                    "pending recovery evidence changed before inventory/manifest "
                    f"advance; expected {expected_text}; found unreadable "
                    f"{paths['pending'].name}: {exc}"
                ) from exc
            prove_pending_losses(
                published_pending,
                tuple(expected),
                schema_version=schema_version,
            )

    # Never replace the latest nonempty exact manifest with an empty snapshot.
    if detected["sessions"] or detected["outside_agents"]:
        publish(paths["manifest"], detected)


def _lost_entry(
    item: Mapping[str, Any],
    *,
    now_unix_ms: int,
) -> tuple[str, str, dict[str, Any]] | None:
    """One queue entry for a conversation whose session went away.

    Two shapes qualify, and both mean "nobody said to end this":

    * a row still carrying a LIVE provider with exact identity -- a window
      killed mid-conversation, a daemon that took its session down, a machine
      that went away;
    * an idle shell whose provider had already exited, still carrying the
      exact conversation it ran. That is what a crash left behind, and when
      the reaper's proven-safe auto-close eventually takes the terminal, the
      conversation inside it is lost rather than finished. An automatic reap
      is not intent (operator ruling, 2026-08-11): only a person's explicit
      verb is, and a person's verb leaves a tombstone.

    The net invariant: a conversation leaves the visible world either because
    somebody said so, or by entering this queue. Never silently.
    """
    session_id = item.get("shpool_id_raw") or item.get("shpool_id")
    provider = item.get("provider")
    identity = item.get("identity")
    recovery = item.get("recovery")
    if not isinstance(session_id, str) or not isinstance(recovery, Mapping):
        return None
    if provider in PROVIDERS:
        exact = (
            isinstance(identity, Mapping)
            and identity.get("confidence") == "exact"
        )
    else:
        # The idle shell an exited provider leaves behind. Its exact identity
        # is the one the overlay put back on the row, from the committed
        # conversation or the retained exact record -- never a guess.
        provider = item.get("exited_provider")
        exited_identity = item.get("exited_identity")
        exact = (
            provider in PROVIDERS
            and isinstance(exited_identity, Mapping)
            and exited_identity.get("confidence") == "historical-exact"
        )
        identity = exited_identity
    if (
        not exact
        or provider not in PROVIDERS
        or recovery.get("available") is not True
        or recovery.get("provider") != provider
    ):
        return None
    uuid = valid_uuid(recovery.get("uuid"))
    if not uuid or (
        isinstance(identity, Mapping)
        and valid_uuid(identity.get("uuid"))
        and valid_uuid(identity.get("uuid")) != uuid
    ):
        return None
    number = item.get("terminal_number")
    return (
        session_id,
        uuid,
        {
            "provider": provider,
            "uuid": uuid,
            "title": item.get("title", ""),
            "started_at_unix_ms": item.get("started_at_unix_ms"),
            "cwd": recovery.get("cwd"),
            "argv": list(recovery.get("argv", ())),
            "command": recovery.get("command"),
            "account_alias": item.get("account_alias"),
            "terminal_number": (
                number
                if isinstance(number, int) and not isinstance(number, bool)
                else None
            ),
            "crashed_at_unix_ms": now_unix_ms,
        },
    )


def _live_conversations(inventory: Mapping[str, Any]) -> set[str]:
    live: set[str] = set()
    for group in ("sessions", "outside_agents"):
        for item in inventory.get(group, ()):
            if not isinstance(item, Mapping):
                continue
            for source in (item.get("identity"), item.get("recovery")):
                if isinstance(source, Mapping):
                    uuid = valid_uuid(source.get("uuid"))
                    provider = source.get("provider") or item.get("provider")
                    if uuid and provider in PROVIDERS:
                        live.add(f"{provider}:{uuid}")
    return live


def enqueue_lost_conversations(
    inventory: Mapping[str, Any],
    previous_inventory: Mapping[str, Any] | None,
    *,
    paths: Mapping[str, Path],
    schema_version: int,
    boot_id: str,
    read_state_json: Callable[[Path], Any],
    atomic_write_json: Callable[[Path, Any], None],
    valid_recovery_state: Callable[[Any], bool],
    closed_on_purpose: Callable[[Any, Any], bool],
    now_unix_ms: int,
    collection_start: int | None = None,
    write_collection_json: Callable[..., None] | None = None,
) -> list[str]:
    """Queue conversations whose session vanished with the provider still in it.

    The queue could only ever learn about a whole generation at once: when the
    daemon restarted, everything the old manifest held became pending. One
    session dying on its own -- the ordinary way a conversation is lost -- was
    invisible to it until the next daemon restart, and by then the row it came
    from had aged out of every screen.

    Exact loss evidence is always retained. A tombstone may suppress its
    public offer while the matching closed-sessions row remains findable, but
    neither that row nor a newly restored live session is permanent. Keeping
    the raw candidate is what lets the projection offer it again if the record
    that justified suppression later vanishes.

    Returns the conversation keys persisted, for the caller to report.
    """
    if not isinstance(previous_inventory, Mapping):
        return []
    generation = inventory.get("daemon_generation")
    if not isinstance(generation, Mapping) or not boot_id:
        # Without an exact generation the entry could not be acknowledged
        # later, and an entry nobody can resolve is worse than no entry.
        return []
    current_ids = {
        item.get("shpool_id_raw")
        for item in inventory.get("sessions", ())
        if isinstance(item, Mapping)
    }
    live = _live_conversations(inventory)
    found: dict[str, dict[str, Any]] = {}
    queued: list[str] = []
    for item in previous_inventory.get("sessions", ()):
        if not isinstance(item, Mapping):
            continue
        parsed = _lost_entry(item, now_unix_ms=now_unix_ms)
        if parsed is None:
            continue
        session_id, uuid, entry = parsed
        key = f"{entry['provider']}:{uuid}"
        if session_id in current_ids or key in live:
            continue
        # Evaluate the agreement here as well as in the public projection so
        # a missing-row inconsistency is diagnosed at the moment the loss is
        # observed. Never discard the candidate: a later ledger replacement
        # must be able to make the recovery offer heal itself.
        closed_on_purpose(entry["provider"], uuid)
        found[session_id] = entry
        queued.append(key)
    if not found:
        return []
    pending = read_state_json(paths["pending"])
    if valid_recovery_state(pending):
        document = json.loads(json.dumps(pending))
        same_source = (
            document.get("source_boot_id") == boot_id
            and document.get("source_daemon_generation") == generation
        )
        if same_source:
            # Never overwrite an entry already queued for this generation: the
            # first record of a loss is the one that names when it happened.
            document["sessions"] = {**found, **dict(document.get("sessions", {}))}
        else:
            bucket = None
            for candidate in document.get("queued_generations", ()):
                if (
                    isinstance(candidate, Mapping)
                    and candidate.get("source_boot_id") == boot_id
                    and candidate.get("source_daemon_generation") == generation
                    and isinstance(candidate.get("sessions"), Mapping)
                ):
                    bucket = candidate
                    break
            if bucket is None:
                document.setdefault("queued_generations", []).append(
                    {
                        "source_boot_id": boot_id,
                        "source_daemon_generation": generation,
                        "sessions": found,
                    }
                )
            else:
                bucket["sessions"] = {**found, **dict(bucket["sessions"])}
    else:
        document = {
            "schema_version": schema_version,
            "generated_at": inventory.get("generated_at"),
            "source_boot_id": boot_id,
            "source_daemon_generation": generation,
            "detected_boot_id": boot_id,
            "detected_daemon_generation": generation,
            "sessions": found,
        }
    document["updated_at"] = _utc_now()
    if write_collection_json is not None:
        write_collection_json(
            paths,
            paths["pending"],
            document,
            collection_start=collection_start,
        )
    else:
        atomic_write_json(paths["pending"], document)
    return queued


def prove_pending_losses(
    value: Any,
    expected_conversation_keys: Sequence[str],
    *,
    schema_version: int,
) -> None:
    """Prove the pending pathname still contains this snapshot's exact losses."""
    expected = set(expected_conversation_keys)
    found = _pending_conversation_keys(value, schema_version=schema_version)
    missing = expected - found
    if missing:
        expected_text = ", ".join(sorted(expected))
        found_text = ", ".join(sorted(found)) if found else "none"
        raise CollectionError(
            "pending recovery evidence changed before inventory/manifest advance; "
            f"expected {expected_text}; found {found_text}"
        )


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
    closed_on_purpose: Callable[[Any, Any], bool] | None = None,
) -> dict[str, Any]:
    """Return one safe recovery candidate per exact provider conversation.

    A conversation somebody closed on purpose is not recovery work. The
    tombstone is honoured HERE, at the one place every consumer passes
    through -- the picker's offers, the count in its header, and the ack --
    so an entry queued before the close disappears from all three together
    rather than being rewritten out of a file under its own lock.
    """
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
                if (
                    closed_on_purpose is not None
                    and provider in PROVIDERS
                    and uuid
                    and closed_on_purpose(provider, uuid)
                ):
                    continue
                started_at = raw_session.get("started_at_unix_ms")
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
                        "started_at_unix_ms": (
                            started_at
                            if isinstance(started_at, int)
                            and not isinstance(started_at, bool)
                            and started_at > 0
                            else None
                        ),
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
) -> dict[str, Any]:
    """Prove one pending entry is restored without deleting its evidence.

    The live projection suppresses retained evidence while the exact provider
    conversation is active. If that live record disappears after this check,
    the next projection offers the still-persisted candidate again.
    """
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
        if not all(isinstance(source, Mapping) for source in evidence):
            raise CollectionError("pending recovery evidence is malformed")
        return {
            "schema_version": schema_version,
            "acknowledged": target,
            "evidence_retained": True,
            "remaining": flatten_pending(pending),
        }
