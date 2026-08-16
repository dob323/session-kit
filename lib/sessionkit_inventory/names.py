"""Name intent and title propagation policy.

What a session should be called, and what happens when telling a provider fails:
the canonical alias and automatic-title document, the audit and prune passes,
title and colour propagation with their retry and reconcile bookkeeping, and the
Claude native hydration pass.

Provider-native writing lives in ``names_push``. The two halves need each other
in both directions, so neither imports the other — the facade injects across the
seam, which keeps the package graph acyclic.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat as statmod
import sys
from typing import Any, Callable, Mapping, Sequence

from .common import (
    PROVIDERS,
    CollectionError,
    _utc_now,
    _valid_aliases,
    _valid_automatic_title_failures,
    _valid_automatic_titles,
    _valid_name_ownership,
    _valid_pushed_titles,
    automatic_naming_enabled,
    clean_text,
    normalize_automatic_title,
    valid_uuid,
)
from .providers_codex import _codex_state_databases
from .state_io import StateLock, _read_bounded_owner_file, _state_paths
from .transcripts import claude_roots


def _alias_document_from_bytes(
    payload: bytes,
    *,
    label: str,
    schema_version: int,
) -> dict[str, Any]:
    try:
        raw = json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectionError(f"{label} is invalid JSON") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version", schema_version) != schema_version
        or ("aliases" in raw and not isinstance(raw["aliases"], Mapping))
        or (
            "automatic_titles" in raw
            and not isinstance(raw["automatic_titles"], Mapping)
        )
        or (
            "automatic_title_failures" in raw
            and not isinstance(raw["automatic_title_failures"], Mapping)
        )
        or ("name_ownership" in raw and not isinstance(raw["name_ownership"], Mapping))
        or ("pushed_titles" in raw and not isinstance(raw["pushed_titles"], Mapping))
    ):
        raise CollectionError(f"{label} has an invalid schema")
    aliases = raw.get("aliases", {})
    normalized = _valid_aliases(aliases)
    if len(normalized) != len(aliases):
        raise CollectionError(f"{label} contains an invalid alias")
    raw["schema_version"] = schema_version
    raw["aliases"] = dict(sorted(normalized.items()))
    if "automatic_titles" in raw:
        titles = _valid_automatic_titles(raw["automatic_titles"])
        if len(titles) != len(raw["automatic_titles"]):
            raise CollectionError(f"{label} contains an invalid automatic title")
        raw["automatic_titles"] = dict(sorted(titles.items()))
    if "automatic_title_failures" in raw:
        failures = _valid_automatic_title_failures(raw["automatic_title_failures"])
        if len(failures) != len(raw["automatic_title_failures"]):
            raise CollectionError(
                f"{label} contains an invalid automatic title failure"
            )
        raw["automatic_title_failures"] = dict(sorted(failures.items()))
    if "name_ownership" in raw:
        owners = _valid_name_ownership(raw["name_ownership"])
        if len(owners) != len(raw["name_ownership"]):
            raise CollectionError(f"{label} contains an invalid name ownership record")
        raw["name_ownership"] = dict(sorted(owners.items()))
    if "pushed_titles" in raw:
        pushed = _valid_pushed_titles(raw["pushed_titles"])
        if len(pushed) != len(raw["pushed_titles"]):
            raise CollectionError(f"{label} contains an invalid pushed title")
        raw["pushed_titles"] = dict(sorted(pushed.items()))
    return raw


def last_kit_title(document: Mapping[str, Any], key: str) -> str:
    """The last title the kit itself put into the provider's own store.

    The recorded push is the exact answer. Documents written before that
    record existed fall back to the retained automatic title, which is the
    value the kit would have pushed — that fallback is what lets a session
    renamed natively before this release still be recognised.
    """
    pushed = _valid_pushed_titles(document.get("pushed_titles")).get(key)
    if pushed:
        return pushed
    return _valid_automatic_titles(document.get("automatic_titles")).get(key, "")


def native_rename(document: Mapping[str, Any], key: str, native_title: str) -> str:
    """The natively typed name for this thread, or "" if the kit wrote it.

    A native title equal to what the kit last pushed is the kit's own echo. A
    native title that differs is a person at a /rename prompt, and that is the
    most recent statement of intent anyone has made about this name.
    """
    title = clean_text(native_title, 120)
    kit_title = last_kit_title(document, key)
    # No kit value means the kit never named this thread, so a native title is
    # not a correction of anything the kit did — it is just the name the
    # thread has always had, and the ordinary precedence rules cover it.
    if not kit_title or not title:
        return ""
    # Byte-exact, end to end. A title is an opaque string: changing only its
    # capitals is still a person deciding what their work is called, and the
    # kit is in no position to rule that it did not count.
    if title == kit_title:
        return ""
    return title


def _name_owner(document: Mapping[str, Any], key: str) -> str:
    """Say who owns one session's name: human, automatic, or nobody ("").

    Documents written before the ownership record exists still carry the
    evidence: an alias is a human rename unless the automatic tier holds the
    identical text, which only a self-name ever writes.
    """
    recorded = _valid_name_ownership(document.get("name_ownership")).get(key)
    if recorded:
        return recorded["owner"]
    aliases = _valid_aliases(document.get("aliases"))
    if key not in aliases:
        return ""
    titles = _valid_automatic_titles(document.get("automatic_titles"))
    return "automatic" if titles.get(key) == aliases[key] else "human"


def _record_name_ownership(document: dict[str, Any], key: str, owner: str) -> None:
    """Stamp who owns a name, without ever re-stamping a human override.

    A human rename takes ownership from an automatic pass; nothing takes it
    back. An automatic claim is one-shot: the first pass to name a thread
    keeps the record, so a later pass finds the thread already owned.
    """
    owners = _valid_name_ownership(document.get("name_ownership"))
    existing = owners.get(key)
    if existing and (owner == "automatic" or existing["owner"] == "human"):
        return
    owners[key] = {"owner": owner, "at": _utc_now()}
    document["name_ownership"] = dict(sorted(owners.items()))


def _private_alias_document(
    path: Path,
    *,
    allow_missing: bool,
    max_private_json_bytes: int,
    schema_version: int,
    alias_document_from_bytes: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    payload = _read_bounded_owner_file(
        path,
        label="canonical alias config",
        max_bytes=max_private_json_bytes,
        exact_mode=0o600,
        allow_missing=allow_missing,
    )
    if payload is None:
        return {"schema_version": schema_version, "aliases": {}}
    return alias_document_from_bytes(payload, label="canonical alias config")


def _private_alias_parent(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.parent.lstat()
    if (
        not statmod.S_ISDIR(metadata.st_mode)
        or statmod.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or statmod.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise CollectionError(
            f"alias config directory must be mode-0700 current-owner: {path.parent}"
        )


def canonical_aliases(
    config: Mapping[str, Any],
    *,
    config_path: Callable[[], Path],
    private_alias_document: Callable[..., dict[str, Any]],
) -> dict[str, str]:
    return dict(private_alias_document(config_path(), allow_missing=True)["aliases"])


def mutate_canonical_alias(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    title: str | None,
    *,
    atomic_write_json: Callable[[Path, Any], None],
    config_path: Callable[[], Path],
    private_alias_document: Callable[..., dict[str, Any]],
    private_alias_parent: Callable[[Path], None],
) -> dict[str, str]:
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise CollectionError("alias requires provider claude|codex and an exact UUID")
    path = config_path()
    paths = _state_paths(config)
    # config.lock is the alias-write lock used by pinned schema-v1 releases.
    # Keep it outermost so an old picker and this core cannot lose each
    # other's config update during a mixed-release rollback window.
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            private_alias_parent(path)
            document = private_alias_document(path, allow_missing=True)
            aliases = dict(document["aliases"])
            key = f"{provider}:{exact_uuid}"
            if title is None:
                aliases.pop(key, None)
            else:
                clean_title = clean_text(title, 100)
                if not clean_title:
                    raise CollectionError("alias title must contain visible text")
                aliases[key] = clean_title
                # This is the human rename surface — `sp name` and the picker
                # reach the alias tier and nothing else does. Marking it here
                # makes the override durable: `sp name reset` drops the alias
                # but keeps the marker, so no automatic pass can resurrect a
                # name over a session a person already named.
                _record_name_ownership(document, key, "human")
            document["aliases"] = dict(sorted(aliases.items()))
            atomic_write_json(path, document)
            return dict(document["aliases"])


def canonical_automatic_titles(
    config: Mapping[str, Any],
    *,
    config_path: Callable[[], Path],
    private_alias_document: Callable[..., dict[str, Any]],
) -> dict[str, str]:
    document = private_alias_document(config_path(), allow_missing=True)
    return _valid_automatic_titles(document.get("automatic_titles"))


def canonical_name_ownership(
    config: Mapping[str, Any],
    *,
    config_path: Callable[[], Path],
    private_alias_document: Callable[..., dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Read every recorded name owner, including inferred legacy renames."""
    document = private_alias_document(config_path(), allow_missing=True)
    owners = _valid_name_ownership(document.get("name_ownership"))
    for key in _valid_aliases(document.get("aliases")):
        if key not in owners:
            owners[key] = {"owner": _name_owner(document, key), "at": ""}
    return dict(sorted(owners.items()))


def mutate_canonical_automatic_title(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    title: str | None,
    *,
    overwrite: bool = False,
    revalidate: Callable[[], None] | None = None,
    schema_version: int,
    atomic_write_json: Callable[[Path, Any], None],
    config_path: Callable[[], Path],
    private_alias_document: Callable[..., dict[str, Any]],
    private_alias_parent: Callable[[Path], None],
) -> dict[str, Any]:
    """Atomically set/reset one automatic title without crossing alias provenance."""
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise CollectionError(
            "automatic title requires provider claude|codex and an exact UUID"
        )
    if title is not None and not automatic_naming_enabled():
        raise CollectionError("automatic naming is disabled")
    clean_title = normalize_automatic_title(title) if title is not None else None
    path = config_path()
    paths = _state_paths(config)
    key = f"{provider}:{exact_uuid}"
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            if revalidate is not None:
                revalidate()
            private_alias_parent(path)
            document = private_alias_document(path, allow_missing=True)
            aliases = dict(document["aliases"])
            if clean_title is not None and key in aliases:
                raise CollectionError("explicit local alias already owns this title")
            if clean_title is not None and _name_owner(document, key) == "human":
                raise CollectionError("a human name owns this session")
            titles = _valid_automatic_titles(document.get("automatic_titles"))
            failures = _valid_automatic_title_failures(
                document.get("automatic_title_failures")
            )
            existing = titles.get(key)
            if clean_title is None:
                titles.pop(key, None)
            elif existing and existing != clean_title and not overwrite:
                raise CollectionError("automatic title is already set")
            else:
                titles[key] = clean_title
            failures.pop(key, None)
            if clean_title is not None:
                _record_name_ownership(document, key, "automatic")
            document["automatic_titles"] = dict(sorted(titles.items()))
            if failures:
                document["automatic_title_failures"] = dict(sorted(failures.items()))
            else:
                document.pop("automatic_title_failures", None)
            atomic_write_json(path, document)
            verified = private_alias_document(path, allow_missing=False)
            verified_titles = _valid_automatic_titles(verified.get("automatic_titles"))
            if verified_titles.get(key) != clean_title:
                if not (clean_title is None and key not in verified_titles):
                    raise CollectionError("automatic title atomic verification failed")
            if clean_title is not None and key in verified["aliases"]:
                raise CollectionError(
                    "explicit alias appeared during automatic title write"
                )
            return {
                "schema_version": schema_version,
                "provider": provider,
                "uuid": exact_uuid,
                "title": clean_title,
                "automatic_titles": verified_titles,
            }


def record_pushed_title(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    title: str,
    *,
    atomic_write_json: Callable[[Path, Any], None],
    config_path: Callable[[], Path],
    private_alias_document: Callable[..., dict[str, Any]],
    private_alias_parent: Callable[[Path], None],
) -> None:
    """Remember exactly what the kit last wrote into a provider's own store."""
    exact_uuid = valid_uuid(uuid)
    clean = clean_text(title, 120)
    if provider not in PROVIDERS or not exact_uuid or not clean:
        return
    path = config_path()
    paths = _state_paths(config)
    key = f"{provider}:{exact_uuid}"
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            private_alias_parent(path)
            document = private_alias_document(path, allow_missing=True)
            pushed = _valid_pushed_titles(document.get("pushed_titles"))
            if pushed.get(key) == clean:
                return
            pushed[key] = clean
            document["pushed_titles"] = dict(sorted(pushed.items()))
            atomic_write_json(path, document)


def adopt_native_rename(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    native_title: str,
    *,
    atomic_write_json: Callable[[Path, Any], None],
    config_path: Callable[[], Path],
    private_alias_document: Callable[..., dict[str, Any]],
    private_alias_parent: Callable[[Path], None],
) -> str:
    """Promote a provider-native rename to the permanent, human-owned name.

    Renaming a room with /rename is a person naming their work, and it is the
    newest thing they have said about it. So it wins outright: it becomes the
    alias, it takes human ownership, and the automatic title it replaced is
    dropped rather than left behind to be restored by a later pass.

    This is also the migration. A session renamed natively before any of this
    existed carries the same evidence — a native title that is not the
    retained automatic one — so the first reconciliation after the release
    settles it without anyone re-typing anything.

    Returns the adopted title, or "" when there was no native rename to adopt.
    """
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        return ""
    path = config_path()
    paths = _state_paths(config)
    key = f"{provider}:{exact_uuid}"
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            private_alias_parent(path)
            document = private_alias_document(path, allow_missing=True)
            adopted = native_rename(document, key, native_title)
            if not adopted:
                return ""
            aliases = dict(document["aliases"])
            if (
                aliases.get(key) == adopted
                and _name_owner(document, key) == "human"
                and _valid_pushed_titles(document.get("pushed_titles")).get(key)
                == adopted
            ):
                return ""
            aliases[key] = adopted
            document["aliases"] = dict(sorted(aliases.items()))
            _record_name_ownership(document, key, "human")
            titles = _valid_automatic_titles(document.get("automatic_titles"))
            titles.pop(key, None)
            document["automatic_titles"] = dict(sorted(titles.items()))
            failures = _valid_automatic_title_failures(
                document.get("automatic_title_failures")
            )
            failures.pop(key, None)
            if failures:
                document["automatic_title_failures"] = dict(sorted(failures.items()))
            else:
                document.pop("automatic_title_failures", None)
            # The adopted name is now what both stores hold, so the next pass
            # sees agreement rather than detecting the same rename forever.
            pushed = _valid_pushed_titles(document.get("pushed_titles"))
            pushed[key] = adopted
            document["pushed_titles"] = dict(sorted(pushed.items()))
            atomic_write_json(path, document)
            return adopted


def claim_automatic_name(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    *,
    atomic_write_json: Callable[[Path, Any], None],
    config_path: Callable[[], Path],
    private_alias_document: Callable[..., dict[str, Any]],
    private_alias_parent: Callable[[Path], None],
) -> str:
    """Take the one-shot automatic claim on one session's name.

    Returns "" when the claim landed and the reason it did not otherwise. The
    claim is what makes first-prompt ownership hold across restarts: the
    record outlives the process that wrote it, so the pass that runs after a
    reboot finds the thread already named instead of naming it again.
    """
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise CollectionError(
            "automatic name claim requires provider claude|codex and an exact UUID"
        )
    path = config_path()
    paths = _state_paths(config)
    key = f"{provider}:{exact_uuid}"
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            private_alias_parent(path)
            document = private_alias_document(path, allow_missing=True)
            owner = _name_owner(document, key)
            if owner:
                return f"a {owner} name already owns this session"
            _record_name_ownership(document, key, "automatic")
            atomic_write_json(path, document)
            verified = private_alias_document(path, allow_missing=False)
            if _name_owner(verified, key) != "automatic":
                raise CollectionError("automatic name claim verification failed")
    return ""


def release_automatic_name_claim(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    *,
    atomic_write_json: Callable[[Path, Any], None],
    config_path: Callable[[], Path],
    private_alias_document: Callable[..., dict[str, Any]],
    private_alias_parent: Callable[[Path], None],
) -> bool:
    """Release an automatic claim that never produced a durable name."""
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        return False
    path = config_path()
    paths = _state_paths(config)
    key = f"{provider}:{exact_uuid}"
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            private_alias_parent(path)
            document = private_alias_document(path, allow_missing=True)
            owners = _valid_name_ownership(document.get("name_ownership"))
            if (
                owners.get(key, {}).get("owner") != "automatic"
                or key in _valid_aliases(document.get("aliases"))
                or key in _valid_automatic_titles(document.get("automatic_titles"))
            ):
                return False
            owners.pop(key, None)
            if owners:
                document["name_ownership"] = dict(sorted(owners.items()))
            else:
                document.pop("name_ownership", None)
            atomic_write_json(path, document)
            verified = private_alias_document(path, allow_missing=False)
            return not _name_owner(verified, key)


def mutate_canonical_self_name(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    title: str,
    *,
    revalidate: Callable[[], None] | None = None,
    schema_version: int,
    atomic_write_json: Callable[[Path, Any], None],
    config_path: Callable[[], Path],
    private_alias_document: Callable[..., dict[str, Any]],
    private_alias_parent: Callable[[Path], None],
) -> dict[str, Any]:
    """Atomically set the paired alias and automatic title for a proved caller."""
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise CollectionError(
            "self-name requires provider claude|codex and an exact UUID"
        )
    if not automatic_naming_enabled():
        raise CollectionError("automatic naming is disabled")
    clean_title = normalize_automatic_title(title)
    path = config_path()
    paths = _state_paths(config)
    key = f"{provider}:{exact_uuid}"
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            if revalidate is not None:
                revalidate()
            private_alias_parent(path)
            document = private_alias_document(path, allow_missing=True)
            if _name_owner(document, key) == "human":
                raise CollectionError("a human name owns this session")
            aliases = dict(document["aliases"])
            titles = _valid_automatic_titles(document.get("automatic_titles"))
            failures = _valid_automatic_title_failures(
                document.get("automatic_title_failures")
            )
            aliases[key] = clean_title
            titles[key] = clean_title
            failures.pop(key, None)
            _record_name_ownership(document, key, "automatic")
            document["aliases"] = dict(sorted(aliases.items()))
            document["automatic_titles"] = dict(sorted(titles.items()))
            if failures:
                document["automatic_title_failures"] = dict(sorted(failures.items()))
            else:
                document.pop("automatic_title_failures", None)
            atomic_write_json(path, document)
            verified = private_alias_document(path, allow_missing=False)
            verified_titles = _valid_automatic_titles(verified.get("automatic_titles"))
            if verified["aliases"].get(key) != clean_title:
                raise CollectionError("self-name alias atomic verification failed")
            if verified_titles.get(key) != clean_title:
                raise CollectionError(
                    "self-name automatic title atomic verification failed"
                )
            return {
                "schema_version": schema_version,
                "provider": provider,
                "uuid": exact_uuid,
                "title": clean_title,
                "aliases": dict(verified["aliases"]),
                "automatic_titles": verified_titles,
            }


def record_automatic_title_failure(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    *,
    revalidate: Callable[[], None] | None = None,
    atomic_write_json: Callable[[Path, Any], None],
    config_path: Callable[[], Path],
    private_alias_document: Callable[..., dict[str, Any]],
    private_alias_parent: Callable[[Path], None],
) -> int:
    """Record at most two proved root-turn naming failures."""
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise CollectionError("automatic title failure requires an exact identity")
    path = config_path()
    paths = _state_paths(config)
    key = f"{provider}:{exact_uuid}"
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            if revalidate is not None:
                revalidate()
            private_alias_parent(path)
            document = private_alias_document(path, allow_missing=True)
            if key in document["aliases"]:
                return 0
            titles = _valid_automatic_titles(document.get("automatic_titles"))
            if key in titles:
                return 0
            failures = _valid_automatic_title_failures(
                document.get("automatic_title_failures")
            )
            failures[key] = min(2, failures.get(key, 0) + 1)
            document["automatic_title_failures"] = dict(sorted(failures.items()))
            atomic_write_json(path, document)
            verified = private_alias_document(path, allow_missing=False)
            attempts = _valid_automatic_title_failures(
                verified.get("automatic_title_failures")
            ).get(key)
            if attempts != failures[key]:
                raise CollectionError(
                    "automatic title failure atomic verification failed"
                )
            return attempts


def _exact_active_title_keys(inventory: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for item in [*inventory.get("sessions", ()), *inventory.get("outside_agents", ())]:
        if not isinstance(item, Mapping):
            continue
        provider = item.get("provider")
        identity = item.get("identity")
        uuid = identity.get("uuid") if isinstance(identity, Mapping) else None
        if (
            provider in PROVIDERS
            and isinstance(identity, Mapping)
            and identity.get("confidence") == "exact"
            and valid_uuid(uuid)
        ):
            keys.add(f"{provider}:{valid_uuid(uuid)}")
    return keys


def _automatic_title_prune_plan(
    titles: Mapping[str, str],
    active_keys: set[str],
    *,
    schema_version: int,
) -> tuple[list[str], str]:
    orphans = sorted(set(titles) - active_keys)
    evidence = json.dumps(
        {
            "schema_version": schema_version,
            "automatic_titles": dict(sorted(titles.items())),
            "active_keys": sorted(active_keys),
            "orphans": orphans,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return orphans, hashlib.sha256(evidence).hexdigest()


def audit_automatic_titles(
    config: Mapping[str, Any],
    inventory: Mapping[str, Any],
    *,
    schema_version: int,
    automatic_title_prune_plan: Callable[..., Any],
    exact_active_title_keys: Callable[[Mapping[str, Any]], set[str]],
    canonical_automatic_titles_reader: Callable[..., dict[str, str]],
) -> dict[str, Any]:
    titles = canonical_automatic_titles_reader(config)
    orphans, token = automatic_title_prune_plan(
        titles, exact_active_title_keys(inventory)
    )
    return {
        "schema_version": schema_version,
        "automatic_title_count": len(titles),
        "orphan_count": len(orphans),
        "orphans": orphans,
        "prune_token": token,
        "dry_run": True,
    }


def prune_automatic_titles(
    config: Mapping[str, Any],
    inventory: Mapping[str, Any],
    prune_token: str,
    *,
    schema_version: int,
    atomic_write_json: Callable[[Path, Any], None],
    config_path: Callable[[], Path],
    automatic_title_prune_plan: Callable[..., Any],
    exact_active_title_keys: Callable[[Mapping[str, Any]], set[str]],
    private_alias_document: Callable[..., dict[str, Any]],
    private_alias_parent: Callable[[Path], None],
    revalidate_inventory: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply only the exact orphan set previously exposed by a dry-run token."""
    path = config_path()
    paths = _state_paths(config)
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            if revalidate_inventory is not None:
                inventory = revalidate_inventory()
            private_alias_parent(path)
            document = private_alias_document(path, allow_missing=True)
            titles = _valid_automatic_titles(document.get("automatic_titles"))
            active_keys = exact_active_title_keys(inventory)
            orphans, expected = automatic_title_prune_plan(titles, active_keys)
            if (
                not re.fullmatch(r"[0-9a-f]{64}", prune_token)
                or prune_token != expected
            ):
                raise CollectionError(
                    "automatic title prune token is stale or does not match dry run"
                )
            for key in orphans:
                titles.pop(key, None)
            document["automatic_titles"] = dict(sorted(titles.items()))
            failures = _valid_automatic_title_failures(
                document.get("automatic_title_failures")
            )
            owners = _valid_name_ownership(document.get("name_ownership"))
            for key in orphans:
                failures.pop(key, None)
                # A spent automatic claim goes with the title it claimed. A
                # human override never does: it has to outlive the pruning of
                # a conversation that is merely not running right now.
                if owners.get(key, {}).get("owner") == "automatic":
                    owners.pop(key, None)
            if owners:
                document["name_ownership"] = dict(sorted(owners.items()))
            else:
                document.pop("name_ownership", None)
            if failures:
                document["automatic_title_failures"] = dict(sorted(failures.items()))
            else:
                document.pop("automatic_title_failures", None)
            atomic_write_json(path, document)
            return {
                "schema_version": schema_version,
                "pruned": orphans,
                "automatic_title_count": len(titles),
            }


def propagate_provider_color(
    provider: str,
    uuid: str,
    color: str,
    *,
    environ: Mapping[str, str] | None = None,
    palette: Sequence[str],
    push_claude_color: Callable[..., tuple[list[str], list[str]]],
) -> dict[str, Any]:
    """One-shot push of a session color into provider-native storage.

    Codex has no native color surface; its rows carry the kit-side color only.
    """
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid or color not in palette:
        return {
            "provider_color_pushes": [],
            "provider_color_warnings": ["invalid provider color push request"],
        }
    if provider != "claude":
        return {"provider_color_pushes": [], "provider_color_warnings": []}
    env = environ if environ is not None else os.environ
    # An explicit environment IS the caller's sandbox; falling back to the
    # real home from inside one would leak pushes into live provider stores.
    home_raw = env.get("HOME") or (os.fspath(Path.home()) if environ is None else "")
    if not home_raw:
        return {
            "provider_color_pushes": [],
            "provider_color_warnings": [
                "caller environment has no HOME; provider color not pushed"
            ],
        }
    home = Path(home_raw)
    pushed, warnings = push_claude_color(home, exact_uuid, color)
    for warning in warnings:
        print(f"session inventory: {warning}", file=sys.stderr)
    return {
        "provider_color_pushes": pushed,
        "provider_color_warnings": warnings,
    }


def propagate_provider_title(
    provider: str,
    uuid: str,
    title: str,
    *,
    environ: Mapping[str, str] | None = None,
    codex_home: Callable[..., Path],
    push_claude_title: Callable[..., tuple[list[str], list[str]]],
    push_codex_title: Callable[..., tuple[list[str], list[str]]],
    session_kit_state_dir: Callable[[Mapping[str, str], Path], Path],
    record_pushed: Callable[..., None],
) -> dict[str, Any]:
    """One-shot push of an assigned name into the provider's own surfaces.

    Assignment semantics are last-writer-wins: the push happens once at
    assignment time and later provider-side renames stand until the next
    assignment. Every failure is fail-open — naming never breaks because a
    provider surface is unavailable — but each skipped surface is reported.
    """
    exact_uuid = valid_uuid(uuid)
    clean_title = clean_text(title, 100)
    if (
        provider not in PROVIDERS
        or not exact_uuid
        or not clean_title
        # A placeholder identity must never be written into provider stores.
        or set(exact_uuid.replace("-", "")) <= {"0"}
    ):
        return {
            "provider_title_pushes": [],
            "provider_title_warnings": ["invalid provider title push request"],
        }
    env = environ if environ is not None else os.environ
    # An explicit environment IS the caller's sandbox; falling back to the
    # real home from inside one would leak pushes into live provider stores.
    home_raw = env.get("HOME") or (os.fspath(Path.home()) if environ is None else "")
    if not home_raw:
        return {
            "provider_title_pushes": [],
            "provider_title_warnings": [
                "caller environment has no HOME; provider title not pushed"
            ],
        }
    home = Path(home_raw)
    if provider == "claude":
        pushed, warnings = push_claude_title(home, exact_uuid, clean_title)
    else:
        live_state_dir: Path | None = session_kit_state_dir(env, home)
        if env.get("SESSION_KIT_CODEX_LIVE_RENAME") == "0":
            live_state_dir = None
        pushed, warnings = push_codex_title(
            codex_home(env, default_home=home),
            exact_uuid,
            clean_title,
            kit_state_dir=live_state_dir,
        )
    for warning in warnings:
        print(f"session inventory: {warning}", file=sys.stderr)
    if pushed and not warnings:
        # Only a push that landed becomes the kit's last-pushed value. A push
        # that failed leaves the old title in the provider store, and calling
        # that the kit's own value would read the kit's own stale name back as
        # somebody's rename.
        record_pushed(provider, exact_uuid, clean_title, environ=environ)
    return {
        "provider_title_pushes": pushed,
        "provider_title_warnings": warnings,
    }


def _record_provider_title_retry(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    title: str,
    *,
    schema_version: int,
    read_state_json: Callable[[Path], Any],
    atomic_write_json: Callable[[Path, Any], None],
) -> None:
    paths = _state_paths(config)
    key = f"{provider}:{uuid}"
    with StateLock(paths["root"], paths["lock"]):
        raw = read_state_json(paths["provider_title_retries"])
        entries = dict(raw.get("entries", {})) if isinstance(raw, Mapping) else {}
        entries[key] = {
            "provider": provider,
            "uuid": uuid,
            "title": title,
            "attempts": 0,
            "updated_at": _utc_now(),
        }
        atomic_write_json(
            paths["provider_title_retries"],
            {"schema_version": schema_version, "entries": entries},
        )


def _provider_title_retry_disposition(
    provider: str,
    uuid: str,
    title: str,
    environ: Mapping[str, str],
    *,
    codex_home: Callable[..., Path],
    codex_title_echoes_prompt: Callable[[str, str], bool],
    home_resolver: Callable[[], Path],
    transcript_signals: Callable[..., dict[str, str]],
    read_session_index: Callable[..., dict[str, str]],
) -> str:
    """Return retry, satisfied, superseded, or defer from native evidence."""
    home_raw = environ.get("HOME")
    if provider == "claude":
        if not home_raw:
            return "defer"
        home = Path(home_raw)
        # Every profile, not just the default root. An account session keeps
        # its transcript under that account's profile, so this glob found
        # nothing and answered "defer" on every pass: a title push that
        # failed once was never retried for an account session, and its entry
        # never left the retry ledger.
        transcripts: list[Path] = []
        for root in claude_roots({**environ, "HOME": home_raw}):
            try:
                transcripts.extend(
                    path
                    for path in (root / "projects").glob(f"*/{uuid}.jsonl")
                    if path.is_file() and not path.is_symlink()
                )
            except OSError:
                continue
        if not transcripts:
            return "defer"
        native = transcript_signals(uuid, home)["agent_name"]
        if native == title:
            return "satisfied"
        if native:
            return "superseded"
        return "retry"

    codex_root = codex_home(
        environ, default_home=Path(home_raw) if home_raw else home_resolver()
    )
    try:
        indexed = read_session_index(codex_root / "session_index.jsonl")
    except CollectionError:
        return "defer"
    index_title = indexed.get(uuid, "")
    if index_title and index_title != title:
        return "superseded"
    databases = _codex_state_databases(codex_root)
    if not databases:
        return "defer"
    try:
        connection = sqlite3.connect(f"file:{databases[-1]}?mode=ro", uri=True)
        try:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(threads)")
            }
            if "title" not in columns:
                return "defer"
            first_column = (
                "first_user_message" if "first_user_message" in columns else "NULL"
            )
            row = connection.execute(
                f"SELECT title, {first_column} FROM threads WHERE id = ?", (uuid,)
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return "defer"
    if row is None:
        return "defer"
    native_title = clean_text(row[0], 120)
    first_message = clean_text(row[1], 8192)
    if native_title == title and index_title == title:
        return "satisfied"
    if (
        native_title
        and native_title != title
        and not codex_title_echoes_prompt(native_title, first_message)
    ):
        return "superseded"
    return "retry"


def _reconcile_pending_provider_titles(
    config: Mapping[str, Any],
    provider: str,
    environ: Mapping[str, str],
    limit: int = 4,
    *,
    schema_version: int,
    read_state_json: Callable[[Path], Any],
    atomic_write_json: Callable[[Path, Any], None],
    retry_disposition: Callable[..., Any],
    propagate_title: Callable[..., dict[str, Any]],
    human_named: Callable[[Mapping[str, str]], frozenset[str]],
) -> list[dict[str, Any]]:
    """Retry a bounded number of exact failed self-name provider pushes."""
    paths = _state_paths(config)
    with StateLock(paths["root"], paths["lock"]):
        raw = read_state_json(paths["provider_title_retries"])
    entries = dict(raw.get("entries", {})) if isinstance(raw, Mapping) else {}
    # A queued retry outlives the process that queued it. If a person renamed
    # the session in the meantime, replaying the old automatic title would
    # resurrect it over their name — after a restart, without them asking.
    owned = human_named(environ)
    candidates = [
        (key, item)
        for key, item in sorted(entries.items())
        if isinstance(item, Mapping)
        and item.get("provider") == provider
        and valid_uuid(item.get("uuid"))
        and clean_text(item.get("title"), 100)
        and isinstance(item.get("attempts"), int)
        and item.get("attempts", 0) < 3
        and f"{provider}:{valid_uuid(item.get('uuid'))}" not in owned
    ][:limit]
    results: list[dict[str, Any]] = []
    for key, item in candidates:
        disposition = retry_disposition(
            provider, str(item["uuid"]), str(item["title"]), environ
        )
        if disposition in {"satisfied", "superseded"}:
            with StateLock(paths["root"], paths["lock"]):
                current = read_state_json(paths["provider_title_retries"])
                current_entries = (
                    dict(current.get("entries", {}))
                    if isinstance(current, Mapping)
                    else {}
                )
                stored = current_entries.get(key)
                if isinstance(stored, Mapping) and stored.get("title") == item.get(
                    "title"
                ):
                    current_entries.pop(key, None)
                    atomic_write_json(
                        paths["provider_title_retries"],
                        {"schema_version": schema_version, "entries": current_entries},
                    )
            results.append(
                {
                    "uuid": str(item["uuid"]),
                    "title": str(item["title"]),
                    "status": disposition,
                }
            )
            continue
        if disposition == "defer":
            results.append(
                {
                    "uuid": str(item["uuid"]),
                    "title": str(item["title"]),
                    "status": "deferred",
                }
            )
            continue
        result = propagate_title(
            provider,
            str(item["uuid"]),
            str(item["title"]),
            environ=environ,
        )
        with StateLock(paths["root"], paths["lock"]):
            current = read_state_json(paths["provider_title_retries"])
            current_entries = (
                dict(current.get("entries", {})) if isinstance(current, Mapping) else {}
            )
            stored = current_entries.get(key)
            if not isinstance(stored, Mapping) or stored.get("title") != item.get(
                "title"
            ):
                continue
            if result.get("provider_title_warnings"):
                updated = dict(stored)
                updated["attempts"] = min(3, int(stored.get("attempts", 0)) + 1)
                updated["updated_at"] = _utc_now()
                current_entries[key] = updated
            else:
                current_entries.pop(key, None)
            atomic_write_json(
                paths["provider_title_retries"],
                {"schema_version": schema_version, "entries": current_entries},
            )
        results.append(
            {
                "uuid": str(item["uuid"]),
                "title": str(item["title"]),
                **result,
            }
        )
    return results


def claude_pending_native_hydrations(
    config: dict[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    canonical_colors: Callable[[Mapping[str, Any]], dict[str, str]],
    guard_inventory: Callable[[Mapping[str, Any]], bool],
    load_config: Callable[[], dict[str, Any]],
    transcript_signals: Callable[..., dict[str, str]],
    session_color: Callable[..., str | None],
    snapshot_inventory: Callable[..., dict[str, Any]],
    propagate_color: Callable[..., dict[str, Any]],
    propagate_title: Callable[..., dict[str, Any]],
    reconcile_pending_titles: Callable[..., Any],
    human_named: Callable[[Mapping[str, str]], frozenset[str]],
    adopt_native: Callable[..., str],
) -> list[dict[str, Any]]:
    """Fill absent visible records for exact managed Claude sessions.

    Claude's ai-title record names the conversation in inventory, but the
    prompt bar and future resumes use agent-name. A missing native color has
    the same next-start behavior. This bounded reconciliation never replaces
    a provider-side rename or color.
    """
    env = environ if environ is not None else os.environ
    naming_enabled = automatic_naming_enabled(env)
    settings = config or load_config()
    hydrated: list[dict[str, Any]] = reconcile_pending_titles(settings, "claude", env)
    try:
        live = snapshot_inventory(write_state=False, config=settings)
    except CollectionError:
        return hydrated
    if not guard_inventory(live):
        return hydrated
    home_raw = env.get("HOME") or (os.fspath(Path.home()) if environ is None else "")
    if not home_raw:
        return hydrated
    home = Path(home_raw)
    colors = canonical_colors(settings)
    owned = human_named(env)
    for item in live.get("sessions", ()):
        if not isinstance(item, Mapping) or item.get("provider") != "claude":
            continue
        uuid = valid_uuid(str((item.get("identity") or {}).get("uuid") or ""))
        if not uuid:
            continue
        pushes: list[str] = []
        warnings: list[str] = []
        # One session's file cannot cost every other session its colour and
        # its name. This pass walks every live Claude session and its output
        # is discarded by both callers (`bin/shpool_status`, `sp_core.sh`),
        # so anything raised here used to end the pass in silence: from the
        # first damaged transcript onward nobody got named or coloured and
        # nothing anywhere said why. Each session is now its own blast
        # radius, and the reason is carried out in the result.
        try:
            signals = transcript_signals(uuid, home)
            # A /rename lands in agent-name, the same record the kit pushes
            # to. Whatever is there now that the kit did not put there is a
            # person's name for this conversation, and it wins for good.
            adopted = adopt_native("claude", uuid, signals["agent_name"], environ=env)
            if adopted:
                hydrated.append({"uuid": uuid, "adopted_native_rename": adopted})
                continue
            title = signals["ai_title"]
            # A session someone renamed keeps that name on its prompt bar. The
            # provider's own ai-title is the automatic tier and never outranks
            # it.
            if (
                naming_enabled
                and f"claude:{uuid}" not in owned
                and title
                and not signals["agent_name"]
                and item.get("native_title") == title
                and item.get("provider_title_state") == "pending"
            ):
                result = propagate_title("claude", uuid, title, environ=env)
                pushes.extend(result["provider_title_pushes"])
                warnings.extend(result["provider_title_warnings"])
            color = session_color("claude", uuid, colors)
            if color and not signals["agent_color"]:
                result = propagate_color("claude", uuid, color, environ=env)
                pushes.extend(result["provider_color_pushes"])
                warnings.extend(result["provider_color_warnings"])
        except Exception as exc:  # noqa: BLE001 - one session, not the pass
            warnings.append(f"{type(exc).__name__}: {exc}")
        if pushes or warnings:
            hydrated.append({"uuid": uuid, "pushes": pushes, "warnings": warnings})
    return hydrated
