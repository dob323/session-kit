"""Collection and private-state orchestration: one exact refresh, or a fallback.

This module owns the order of a refresh rather than any of its steps. It takes a
live collection, and if that collection is complete it publishes the inventory,
the terminal-number registry and the recovery manifest under one lock, in the
order those files have to be written for a crash between any two of them to
leave usable state behind. If the collection is incomplete it serves the last
known good cache instead, marked stale and carrying the reason.

Nothing here reads a process table, a rollout, or a provider database. Every
step arrives as a call-time argument, the collector, the boot identity, the
state reader and publisher, the terminal helpers, the recovery transaction, the
lifecycle passes, so this file can be read as the sequence itself, and so a
facade patch on any single step still reaches the refresh under test.

The two modes are not symmetrical and the asymmetry is the point. ``write_state``
takes the lock, re-reads the cache inside it, and allocates terminal numbers.
``--no-write`` does none of that: it must not create a state directory or a lock
file merely to inspect existing state, because deployment validation runs it
against installations it is not allowed to modify.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from .common import CollectionError, _utc_now, clean_text


class ProviderUntitledQuarantine(Protocol):
    def __call__(
        self,
        config: Mapping[str, Any],
        inventory: Mapping[str, Any],
        *,
        retire_generations: Iterable[tuple[str, str]] = (),
    ) -> list[dict[str, str]]: ...


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


def _accepts_keyword(callback: Callable[..., Any], name: str) -> bool:
    """Whether an injected callback accepts one post-v1 optional keyword."""
    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == name
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        )
        for parameter in parameters
    )


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
    quarantine_orphaned_provider_untitled_markers: ProviderUntitledQuarantine,
    update_recovery_state: Callable[[Mapping[str, Path], Mapping[str, Any]], None],
    enqueue_lost_conversations: Callable[..., list[str]],
    apply_session_origins: Callable[..., None],
    capture_bounce_receipts: Callable[[Path], frozenset[str]],
    capture_bounce_cleanup_generations: Callable[[Path], frozenset[tuple[str, str]]],
    capture_lifecycle_generations: Callable[[Path], frozenset[tuple[str, str]]],
    capture_origin_generations: Callable[[Path], frozenset[tuple[str, str]]],
    capture_provider_untitled_generations: Callable[[Path], frozenset[tuple[str, str]]],
    capture_session_color_generations: Callable[[Path], frozenset[tuple[str, str]]],
    prune_origins: Callable[..., int],
    publish_session_colors: Callable[..., int],
    cold_inventory: Callable[[str], dict[str, Any]],
    allocate_collection_start: Callable[[Mapping[str, Path]], tuple[int, str | None]]
    | None = None,
    preflight_collection_documents: Callable[
        [Mapping[str, Path], tuple[str, ...], int | None], tuple[list[str], list[str]]
    ]
    | None = None,
    write_collection_json: Callable[..., None] | None = None,
    prove_pending_losses: Callable[[Any, tuple[str, ...]], None] | None = None,
    # Optional: a caller assembled before the model cache existed still works,
    # and simply does not reap it.
    prune_model_cache: Callable[..., Any] | None = None,
    apply_session_idle_states: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    # Keep the callback-injection seam used by tests and facades, while older
    # callers that predate publication ordering get the production behavior
    # instead of failing on newly required keyword arguments.
    if (
        allocate_collection_start is None
        or preflight_collection_documents is None
        or write_collection_json is None
    ):
        from . import state_io

        allocate_collection_start = (
            allocate_collection_start or state_io.allocate_collection_start
        )
        preflight_collection_documents = (
            preflight_collection_documents or state_io.preflight_collection_documents
        )
        write_collection_json = write_collection_json or state_io.write_collection_json
    if prove_pending_losses is None:
        from .recovery import prove_pending_losses as prove_pending_document

        def prove_pending_losses(value: Any, expected: tuple[str, ...]) -> None:
            prove_pending_document(
                value,
                expected,
                schema_version=schema_version,
            )

    settings = config or load_config()
    paths = state_paths(settings)
    collection_start: int | None = None
    collection_diagnostic: str | None = None
    publish_state = write_state

    # The inventory cache is a comparison input and display fallback; nothing
    # overwrites recovery evidence based on it, so a corrupt cache reads as
    # absent here and the refresh proceeds to the allocation refusal instead
    # of crashing. Every other state read stays strict: an unreadable pending
    # queue must raise before anything could replace its bytes.
    def _cached_or_none() -> Any:
        try:
            return read_state_json(paths["inventory"])
        except (OSError, ValueError):
            return None

    if write_state:
        with state_lock(paths["root"], paths["lock"]):
            cached = _cached_or_none()
            try:
                collection_start, collection_diagnostic = allocate_collection_start(
                    paths
                )
            except CollectionError as exc:
                publish_state = False
                collection_diagnostic = str(exc)
    else:
        # --no-write is also useful in deployment validation.  Do not create a
        # state directory or lock file merely to inspect existing state.
        cached = _cached_or_none()
    # A receipt name is a bounce generation. Capture the generations before
    # process collection so a later positive window sighting can settle only
    # receipts that are not newer than that sighting. Read-only snapshots
    # never settle state and need no generation capture.
    bounce_receipts = (
        capture_bounce_receipts(paths["root"]) if publish_state else frozenset()
    )
    bounce_cleanup_generations = (
        capture_bounce_cleanup_generations(paths["root"])
        if publish_state
        else frozenset()
    )
    lifecycle_generations = (
        capture_lifecycle_generations(paths["root"]) if publish_state else frozenset()
    )
    origin_generations = (
        capture_origin_generations(paths["root"]) if publish_state else frozenset()
    )
    provider_untitled_generations = (
        capture_provider_untitled_generations(paths["root"])
        if publish_state
        else frozenset()
    )
    session_color_generations = (
        capture_session_color_generations(paths["root"])
        if publish_state
        else frozenset()
    )
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
        if collection_start is not None:
            result["collection_start"] = {"sequence": collection_start}
        try:
            boot_id = boot_id_factory()
            if not boot_id:
                raise CollectionError("current boot identity is unavailable")
            if publish_state:
                with state_lock(paths["root"], paths["lock"]):
                    locked_cached = read_state_json(paths["inventory"])
                    document_keys = (
                        "inventory",
                        "terminal_numbers",
                        "terminal_numbers_retired",
                        "terminal_numbers_epoch",
                        "manifest",
                        "pending",
                    )
                    refused, diagnostics = preflight_collection_documents(
                        paths, document_keys, collection_start
                    )
                    if refused:
                        message = (
                            f"refused stale collection {collection_start}; newer "
                            f"published state stands: {', '.join(sorted(refused))}"
                        )
                        retained = (
                            dict(locked_cached)
                            if isinstance(locked_cached, Mapping)
                            else dict(result)
                        )
                        retained_warnings = list(retained.get("warnings", ()))
                        retained_warnings.append(message)
                        retained["warnings"] = retained_warnings
                        return retained
                    if collection_diagnostic:
                        diagnostics.insert(0, collection_diagnostic)
                    if diagnostics:
                        warnings = list(result.get("warnings", ()))
                        warnings.extend(diagnostics)
                        result["warnings"] = warnings
                    if apply_session_idle_states is not None:
                        # Transcript movement is differential evidence. Compare
                        # against the exact inventory still protected by this
                        # publication lock, not the optimistic pre-collection
                        # read which another refresh may have superseded.
                        apply_session_idle_states(
                            result,
                            (
                                locked_cached
                                if isinstance(locked_cached, Mapping)
                                else None
                            ),
                            state_dir=paths["root"],
                        )
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
                    # Who asked for each session, before anything decides what
                    # to show. An unstamped session belongs to the person.
                    apply_session_origins(
                        result,
                        state_dir=paths["root"],
                        settle_bounce_receipts=bounce_receipts,
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
                    write_collection_json(
                        paths,
                        paths["terminal_numbers"],
                        updated_registry,
                        collection_start=collection_start,
                    )
                    write_collection_json(
                        paths,
                        paths["terminal_numbers_retired"],
                        terminal_retirement_payload(retired_numbers, boot_id),
                        collection_start=collection_start,
                    )
                    write_collection_json(
                        paths,
                        paths["terminal_numbers_epoch"],
                        {
                            "schema_version": schema_version,
                            "boot_id": boot_id,
                        },
                        collection_start=collection_start,
                    )
                    quarantined_markers = quarantine_orphaned_provider_untitled_markers(
                        settings,
                        result,
                        retire_generations=provider_untitled_generations,
                    )
                    if quarantined_markers:
                        result["provider_untitled_quarantine"] = quarantined_markers
                    # A session that went away with its provider still running
                    # is the ordinary way a conversation is lost, and until now
                    # only a whole-generation restart could queue one. Publish
                    # that evidence before inventory or the recovery manifest
                    # discard the last row which can rediscover the transition.
                    # If the pending write fails, the old authorities remain
                    # intact and the next refresh retries the same loss. Every
                    # write in this transaction carries the same allocated
                    # collection sequence, so ordering the pending write first
                    # changes nothing the sequence records prove.
                    enqueue_keywords: dict[str, Any] = {"boot_id": boot_id}
                    if _accepts_keyword(enqueue_lost_conversations, "collection_start"):
                        enqueue_keywords["collection_start"] = collection_start
                    queued_losses = enqueue_lost_conversations(
                        paths,
                        result,
                        (locked_cached if isinstance(locked_cached, Mapping) else None),
                        **enqueue_keywords,
                    )
                    if queued_losses:
                        try:
                            pending_at_advance = read_state_json(paths["pending"])
                        except (OSError, ValueError) as exc:
                            expected = ", ".join(sorted(queued_losses))
                            raise CollectionError(
                                "pending recovery evidence changed before inventory/"
                                f"manifest advance; expected {expected}; found unreadable "
                                f"{paths['pending'].name}: {exc}"
                            ) from exc
                        prove_pending_losses(pending_at_advance, tuple(queued_losses))
                    write_collection_json(
                        paths,
                        paths["inventory"],
                        result,
                        collection_start=collection_start,
                    )
                    recovery_keywords = (
                        {"collection_start": collection_start}
                        if _accepts_keyword(update_recovery_state, "collection_start")
                        else {}
                    )
                    update_recovery_state(paths, result, **recovery_keywords)
                    # One colour per session, published where the session's
                    # own prompt can read it without asking anything.
                    publish_session_colors(
                        result,
                        state_dir=paths["root"],
                        retire_generations=session_color_generations,
                    )
                    prune_origins(
                        paths["root"],
                        [
                            item["shpool_id_raw"]
                            for item in result.get("sessions", ())
                            if isinstance(item, Mapping)
                            and isinstance(item.get("shpool_id_raw"), str)
                        ],
                        retire_generations=origin_generations,
                        retire_bounce_generations=bounce_cleanup_generations,
                    )
                    prune_inactive_state(
                        paths["root"],
                        [
                            item["shpool_id_raw"]
                            for item in result.get("sessions", ())
                            if isinstance(item, Mapping)
                            and isinstance(item.get("shpool_id_raw"), str)
                        ],
                        retire_generations=lifecycle_generations,
                    )
                    # The model cache is state like any other and gets the same
                    # reaper. It writes one small record per conversation the
                    # kit has read a model for, and without this it grows for
                    # the life of the install.
                    if prune_model_cache is not None:
                        prune_model_cache(
                            paths["root"],
                            [
                                (item.get("identity") or {}).get("uuid")
                                for item in result.get("sessions", ())
                                if isinstance(item, Mapping)
                                and isinstance(item.get("identity"), Mapping)
                            ],
                        )
            else:
                if apply_session_idle_states is not None:
                    apply_session_idle_states(
                        result,
                        (cached if isinstance(cached, Mapping) else None),
                        state_dir=paths["root"],
                    )
                if collection_diagnostic:
                    warnings = list(result.get("warnings", ()))
                    warnings.append(collection_diagnostic)
                    result["warnings"] = warnings
                apply_provider_exit_states(
                    result,
                    cached if isinstance(cached, Mapping) else None,
                    state_dir=paths["root"],
                    boot_id=boot_id,
                )
                # Reading only. This branch promised not to create a state
                # directory merely to inspect state, so it certainly may not
                # delete state -- and it holds no lock while every `sp` guard
                # snapshot comes through here. Rows are still classified; the
                # settled bounce markers are left to a collection that is
                # allowed to write, and until then the row stays visible.
                apply_session_origins(
                    result, state_dir=paths["root"], clear_settled_bounces=False
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
