"""One-time legacy transitions: runtime aliases and the recovery manifest.

Two migrations live here because they share a shape rather than a subject. Each
runs once per installation, each rewrites private state that a pinned older
release may still read, and each is written to be abandonable — plan, publish,
apply, roll back — rather than applied in place.

Keeping them together also keeps them out of ``recovery``. The recovery module
handles state that changes every time a session appears or disappears; nothing
here runs again once an installation has crossed the version it was written for.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import stat as statmod
from typing import Any, Callable, Mapping

from . import state_io as _state_io
from .common import (
    PROVIDERS,
    CollectionError,
    _utc_now,
    _valid_aliases,
    natural_name_key,
    valid_uuid,
)
from .processes import _proc_stat
from .state_io import (
    StateLock,
    _atomic_json_bytes,
    _read_bounded_owner_file,
    _state_paths,
)


def _strict_legacy_aliases(
    path: Path,
    *,
    max_private_json_bytes: int,
    schema_version: int,
) -> tuple[bytes, dict[str, str]] | None:
    payload = _read_bounded_owner_file(
        path,
        label="legacy runtime alias file",
        max_bytes=max_private_json_bytes,
        exact_mode=0o600,
        allow_missing=True,
    )
    if payload is None:
        return None
    try:
        raw = json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectionError("legacy runtime alias file is invalid JSON") from exc
    aliases = raw.get("aliases") if isinstance(raw, Mapping) else None
    normalized = _valid_aliases(aliases)
    if (
        not isinstance(raw, Mapping)
        or raw.get("schema_version") != schema_version
        or not isinstance(aliases, Mapping)
        or len(normalized) != len(aliases)
    ):
        raise CollectionError("legacy runtime alias file has an invalid schema")
    return payload, normalized


def _create_private_backup(
    path: Path,
    payload: bytes,
    *,
    max_private_json_bytes: int,
) -> None:
    _state_io._create_private_backup(
        path,
        payload,
        read_bounded_owner_file=_read_bounded_owner_file,
        max_bytes=max_private_json_bytes,
    )


def _alias_migration_images(
    backup_bytes: bytes,
    runtime_aliases: Mapping[str, str],
    *,
    absent_alias_backup: bytes,
    schema_version: int,
    alias_document_from_bytes: Callable[..., dict[str, Any]],
) -> tuple[bytes | None, dict[str, Any], bytes]:
    if backup_bytes == absent_alias_backup:
        preimage = {
            "schema_version": schema_version,
            "aliases": {},
        }
        preimage_bytes = None
    else:
        preimage = alias_document_from_bytes(
            backup_bytes, label="alias migration backup"
        )
        preimage_bytes = backup_bytes
    merged = {
        **_valid_aliases(preimage.get("aliases")),
        **dict(runtime_aliases),
    }
    postimage = dict(preimage)
    postimage["aliases"] = dict(sorted(merged.items()))
    return preimage_bytes, postimage, _atomic_json_bytes(postimage)


def migrate_runtime_aliases(
    config: Mapping[str, Any],
    *,
    absent_alias_backup: bytes,
    max_private_json_bytes: int,
    schema_version: int,
    private_alias_parent: Callable[[Path], None],
    atomic_write_json: Callable[[Path, Any], None],
    canonical_aliases: Callable[[Mapping[str, Any]], dict[str, str]],
    config_path: Callable[[], Path],
    alias_migration_images_fn: Callable[..., Any],
    create_private_backup_fn: Callable[..., Any],
    strict_legacy_aliases_fn: Callable[..., Any],
) -> dict[str, Any]:
    """Explicitly preserve the old effective runtime-wins values in config."""
    paths = _state_paths(config)
    config_file = config_path()
    backup = config_file.with_name(f"{config_file.name}.pre-runtime-alias-migration-v1")
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            legacy = strict_legacy_aliases_fn(paths["aliases"])
            archived = strict_legacy_aliases_fn(paths["aliases_archive"])
            if legacy is None:
                if archived is not None:
                    source_backup = _read_bounded_owner_file(
                        paths["aliases_source_backup"],
                        label="legacy runtime alias source backup",
                        max_bytes=max_private_json_bytes,
                        exact_mode=0o600,
                    )
                    if source_backup != archived[0]:
                        raise CollectionError(
                            "archived runtime aliases diverge from migration evidence"
                        )
                    backup_bytes = _read_bounded_owner_file(
                        backup,
                        label="alias migration backup",
                        max_bytes=max_private_json_bytes,
                        exact_mode=0o600,
                    )
                    if backup_bytes is None:
                        raise CollectionError("alias migration backup is unavailable")
                    before = _read_bounded_owner_file(
                        config_file,
                        label="canonical alias config",
                        max_bytes=max_private_json_bytes,
                        exact_mode=0o600,
                        allow_missing=True,
                    )
                    (
                        preimage_bytes,
                        postimage,
                        postimage_bytes,
                    ) = alias_migration_images_fn(backup_bytes, archived[1])
                    repaired = False
                    if before == postimage_bytes:
                        pass
                    elif before == preimage_bytes:
                        private_alias_parent(config_file)
                        atomic_write_json(config_file, postimage)
                        repaired = True
                    else:
                        raise CollectionError(
                            "canonical alias config diverged from the migration postimage"
                        )
                    directory = os.open(paths["root"], os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                return {
                    "schema_version": schema_version,
                    "migrated": bool(archived is not None and repaired),
                    "already_migrated": bool(archived is not None and not repaired),
                    "aliases": canonical_aliases(config),
                }
            rollback_retry = archived is not None
            if archived is not None and archived[0] != legacy[0]:
                raise CollectionError("active and archived runtime aliases differ")
            private_alias_parent(config_file)
            before = _read_bounded_owner_file(
                config_file,
                label="canonical alias config",
                max_bytes=max_private_json_bytes,
                exact_mode=0o600,
                allow_missing=True,
            )
            backup_bytes = _read_bounded_owner_file(
                backup,
                label="alias migration backup",
                max_bytes=max_private_json_bytes,
                exact_mode=0o600,
                allow_missing=True,
            )
            if backup_bytes is None:
                backup_bytes = before if before is not None else absent_alias_backup
                create_private_backup_fn(backup, backup_bytes)
            create_private_backup_fn(paths["aliases_source_backup"], legacy[0])
            (
                preimage_bytes,
                postimage,
                postimage_bytes,
            ) = alias_migration_images_fn(backup_bytes, legacy[1])
            if before == postimage_bytes:
                pass
            elif before == preimage_bytes:
                atomic_write_json(config_file, postimage)
            else:
                raise CollectionError(
                    "canonical alias config diverged during migration"
                )
            if rollback_retry:
                os.unlink(paths["aliases"])
            else:
                os.replace(paths["aliases"], paths["aliases_archive"])
            directory = os.open(paths["root"], os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            durable_archive = strict_legacy_aliases_fn(paths["aliases_archive"])
            source_backup = _read_bounded_owner_file(
                paths["aliases_source_backup"],
                label="legacy runtime alias source backup",
                max_bytes=max_private_json_bytes,
                exact_mode=0o600,
            )
            if durable_archive is None:
                raise CollectionError(
                    "runtime alias archive did not reach its durable postcondition"
                )
            if (
                durable_archive[0] != legacy[0]
                or source_backup != legacy[0]
                or paths["aliases"].exists()
            ):
                raise CollectionError(
                    "runtime alias archive did not reach its durable postcondition"
                )
            return {
                "schema_version": schema_version,
                "migrated": True,
                "already_migrated": False,
                "aliases": dict(postimage["aliases"]),
            }


def _release_sha(value: Any, *, release_root: Path) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise CollectionError(
            "release SHA must be one full lowercase 40-character commit"
        )
    release_metadata = release_root / "RELEASE.json"
    configured_root = os.environ.get("SESSION_KIT_RELEASE_DIR")
    if configured_root and Path(configured_root).resolve() != release_root:
        raise CollectionError(
            "executing release does not match SESSION_KIT_RELEASE_DIR"
        )
    if release_metadata.exists() or configured_root:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(release_metadata, flags)
            metadata = os.fstat(descriptor)
            content = b""
            while True:
                block = os.read(descriptor, 64 * 1024)
                if not block:
                    break
                content += block
        except OSError as exc:
            raise CollectionError("cannot verify executing immutable release") from exc
        finally:
            if "descriptor" in locals():
                os.close(descriptor)
        try:
            document = json.loads(content)
        except (UnicodeDecodeError, ValueError) as exc:
            raise CollectionError("executing RELEASE.json is invalid") from exc
        if (
            not statmod.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or statmod.S_IMODE(metadata.st_mode) & 0o222
            or metadata.st_size > 64 * 1024
            or release_root.name != value
            or not isinstance(document, Mapping)
            or document.get("commit") != value
        ):
            raise CollectionError(
                "release SHA is not bound to the executing immutable release"
            )
    return value


def _daemon_start_epoch(proc_root: Path, generation: Mapping[str, Any]) -> float:
    pid = generation.get("pid")
    expected_ticks = generation.get("process_start_ticks")
    if (
        not isinstance(pid, int)
        or pid <= 0
        or not isinstance(expected_ticks, int)
        or expected_ticks <= 0
    ):
        raise CollectionError("current daemon generation is not exact")
    try:
        before = _proc_stat(proc_root / str(pid) / "stat")
        lines = (
            (proc_root / "stat")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
        boot_values = [
            int(line.split()[1])
            for line in lines
            if len(line.split()) == 2 and line.split()[0] == "btime"
        ]
        ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
        after = _proc_stat(proc_root / str(pid) / "stat")
    except (OSError, ValueError) as exc:
        raise CollectionError("cannot prove current daemon wall-clock start") from exc
    if (
        len(boot_values) != 1
        or boot_values[0] <= 0
        or ticks_per_second <= 0
        or before != after
        or before[0] != pid
        or before[2] != expected_ticks
    ):
        raise CollectionError("current daemon changed while proving continuity")
    return boot_values[0] + (expected_ticks / ticks_per_second)


def _legacy_identities(value: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    sessions = value.get("sessions")
    outside = value.get("outside_agents", {})
    if not isinstance(sessions, Mapping) or not sessions or outside:
        raise CollectionError(
            "legacy migration requires nonempty shpool sessions and no outside roots"
        )
    result: dict[str, tuple[str, str]] = {}
    uuids: set[str] = set()
    for shpool_id, item in sessions.items():
        if (
            not isinstance(shpool_id, str)
            or not shpool_id
            or not isinstance(item, Mapping)
        ):
            raise CollectionError(
                "legacy recovery manifest contains an invalid session"
            )
        provider = item.get("provider")
        uuid = valid_uuid(item.get("uuid"))
        if provider not in PROVIDERS or not uuid or uuid in uuids:
            raise CollectionError("legacy recovery identities must be exact and unique")
        result[shpool_id] = (provider, uuid)
        uuids.add(uuid)
    return result


def _evidence_identities(value: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    rows = value.get("sessions")
    if not isinstance(rows, list) or not rows:
        raise CollectionError(
            "continuity evidence must contain a nonempty session list"
        )
    result: dict[str, tuple[str, str]] = {}
    uuids: set[str] = set()
    for item in rows:
        if not isinstance(item, Mapping):
            raise CollectionError("continuity evidence contains a non-object session")
        shpool_id = item.get("shpool_name")
        provider = item.get("provider")
        uuid = valid_uuid(item.get("provider_uuid"))
        if (
            not isinstance(shpool_id, str)
            or not shpool_id
            or provider not in PROVIDERS
            or not uuid
            or shpool_id in result
            or uuid in uuids
        ):
            raise CollectionError(
                "continuity evidence identities must be exact and unique"
            )
        result[shpool_id] = (provider, uuid)
        uuids.add(uuid)
    return result


def _current_identities(
    inventory: Mapping[str, Any],
) -> tuple[dict[str, tuple[str, str]], set[tuple[str, str]]]:
    sessions: dict[str, tuple[str, str]] = {}
    all_roots: set[tuple[str, str]] = set()
    seen_uuids: set[str] = set()
    for item in inventory.get("sessions", ()):
        identity = item.get("identity")
        recovery = item.get("recovery")
        shpool_id = item.get("shpool_id_raw")
        provider = item.get("provider")
        uuid = (
            valid_uuid(identity.get("uuid")) if isinstance(identity, Mapping) else None
        )
        recovery_uuid = (
            valid_uuid(recovery.get("uuid")) if isinstance(recovery, Mapping) else None
        )
        if (
            not isinstance(shpool_id, str)
            or provider not in PROVIDERS
            or not uuid
            or not isinstance(recovery, Mapping)
            or recovery.get("available") is not True
            or recovery.get("provider") != provider
            or recovery_uuid != uuid
            or shpool_id in sessions
            or uuid in seen_uuids
        ):
            raise CollectionError("current shpool identities are not exact and unique")
        sessions[shpool_id] = (provider, uuid)
        all_roots.add((provider, uuid))
        seen_uuids.add(uuid)
    outside = inventory.get("outside_agents", ())
    if outside:
        # This one-time migration has no trusted historical evidence for
        # outside roots, so including or excluding them would be an assumption.
        raise CollectionError("legacy migration refuses current outside provider roots")
    return sessions, all_roots


def _migration_context(
    config: Mapping[str, Any],
    *,
    legacy_bytes: bytes,
    evidence_path: Path,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    proc_root: Path | None = None,
    generation_key: Callable[[Mapping[str, Any]], Any],
    has_recovery_entries: Callable[[Any], bool],
    parse_private_json: Callable[..., tuple[bytes, Mapping[str, Any]]],
    parse_utc_timestamp: Callable[..., Any],
    sha256: Callable[[bytes], str],
    collect_live: Callable[..., dict[str, Any]],
    recovery_manifest: Callable[[Mapping[str, Any]], dict[str, Any]],
    strict_live_inventory: Callable[[Mapping[str, Any]], bool],
) -> dict[str, Any]:
    collect = collector or collect_live
    try:
        legacy = json.loads(legacy_bytes)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectionError("legacy recovery manifest is invalid JSON") from exc
    if (
        not isinstance(legacy, Mapping)
        or not has_recovery_entries(legacy)
        or legacy.get("daemon_generation") is not None
        or not isinstance(legacy.get("boot_id"), str)
        or not legacy.get("boot_id")
    ):
        raise CollectionError("source is not a null-generation legacy manifest")
    legacy_generated = parse_utc_timestamp(
        legacy.get("generated_at"), "legacy generated_at"
    )
    evidence_bytes, evidence = parse_private_json(evidence_path, "continuity evidence")
    evidence_captured = parse_utc_timestamp(
        evidence.get("captured_at"), "continuity evidence captured_at"
    )
    if evidence_captured > legacy_generated:
        raise CollectionError(
            "continuity evidence was captured after the legacy manifest"
        )
    legacy_ids = _legacy_identities(legacy)
    if _evidence_identities(evidence) != legacy_ids:
        raise CollectionError(
            "continuity evidence does not exactly match every legacy identity"
        )
    settings = dict(config)
    live = collect(settings)
    if not isinstance(live, Mapping):
        raise CollectionError("collector returned a non-object inventory")
    live = dict(live)
    complete = bool(live.pop("_complete", True))
    if not complete or not strict_live_inventory(live):
        raise CollectionError(
            "legacy recovery migration requires a complete strict live inventory"
        )
    detected = recovery_manifest(live)
    detected_key = generation_key(detected)
    if (
        detected_key is None
        or not has_recovery_entries(detected)
        or detected.get("boot_id") != legacy.get("boot_id")
    ):
        raise CollectionError(
            "legacy and current manifests do not have one exact same-boot generation"
        )
    generation = detected["daemon_generation"]
    if evidence.get("daemon_pid") != generation.get("pid"):
        raise CollectionError("current daemon PID does not match continuity evidence")
    root = proc_root or Path(os.environ.get("SESSION_KIT_PROC_ROOT", "/proc"))
    daemon_started = _daemon_start_epoch(root, generation)
    if daemon_started > evidence_captured.timestamp():
        raise CollectionError("current daemon did not predate continuity evidence")
    current_ids, all_current_roots = _current_identities(live)
    if not set(current_ids).issubset(legacy_ids):
        raise CollectionError(
            "current inventory contains a root absent from legacy evidence"
        )
    reconciliation: list[dict[str, Any]] = []
    for shpool_id in sorted(legacy_ids, key=natural_name_key):
        provider, uuid = legacy_ids[shpool_id]
        current = current_ids.get(shpool_id)
        if current is not None:
            if current != (provider, uuid):
                raise CollectionError(
                    f"current identity changed under legacy shpool ID {shpool_id!r}"
                )
            disposition = "carried"
        else:
            if (provider, uuid) in all_current_roots:
                raise CollectionError(
                    f"legacy identity moved from shpool ID {shpool_id!r}"
                )
            disposition = "ended"
        reconciliation.append(
            {
                "shpool_id": shpool_id,
                "provider": provider,
                "uuid": uuid,
                "disposition": disposition,
            }
        )
    return {
        "legacy": dict(legacy),
        "legacy_sha256": sha256(legacy_bytes),
        "evidence_sha256": sha256(evidence_bytes),
        "evidence_captured_at": evidence_captured.isoformat().replace("+00:00", "Z"),
        "daemon_started_at": dt.datetime.fromtimestamp(daemon_started, dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "generation": dict(generation),
        "target_manifest": detected,
        "current_roots": [
            {"shpool_id": key, "provider": value[0], "uuid": value[1]}
            for key, value in sorted(
                current_ids.items(), key=lambda item: natural_name_key(item[0])
            )
        ],
        "reconciliation": reconciliation,
    }


def _plan_token(
    plan: Mapping[str, Any],
    *,
    json_bytes: Callable[[Any], bytes],
    sha256: Callable[[bytes], str],
) -> str:
    body = dict(plan)
    body.pop("plan_token", None)
    return sha256(json_bytes(body))


def plan_legacy_recovery_manifest(
    config: Mapping[str, Any],
    continuity_evidence: str | Path,
    release_sha: str,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    proc_root: Path | None = None,
    now: float | None = None,
    schema_version: int,
    json_bytes: Callable[[Any], bytes],
    read_private_regular_bytes: Callable[[Path], bytes],
    sha256: Callable[[bytes], str],
    migration_context_fn: Callable[..., Any],
    plan_token_fn: Callable[..., Any],
    verify_release_sha: Callable[[Any], str],
) -> dict[str, Any]:
    """Create a read-only, content-addressed migration plan."""
    paths = _state_paths(config)
    if paths["pending"].exists() or paths["pending"].is_symlink():
        raise CollectionError(
            "legacy recovery migration refuses an existing pending queue"
        )
    source_before = read_private_regular_bytes(paths["manifest"])
    evidence_path = Path(continuity_evidence).expanduser()
    if not evidence_path.is_absolute():
        evidence_path = Path.cwd() / evidence_path
    context = migration_context_fn(
        config,
        legacy_bytes=source_before,
        evidence_path=evidence_path,
        collector=collector,
        proc_root=proc_root,
    )
    if read_private_regular_bytes(paths["manifest"]) != source_before:
        raise CollectionError("legacy manifest changed while planning")
    release = verify_release_sha(release_sha)
    source_hash = context["legacy_sha256"]
    archive = paths["root"] / f"recovery-manifest.legacy.{source_hash}.json"
    target = context["target_manifest"]
    plan: dict[str, Any] = {
        "schema_version": schema_version,
        "plan_version": 1,
        "action": "legacy-recovery-manifest-migration",
        "created_at": _utc_now(now),
        "release_sha": release,
        "source_manifest": {
            "path": str(paths["manifest"].resolve()),
            "sha256": source_hash,
            "boot_id": context["legacy"]["boot_id"],
            "generated_at": context["legacy"]["generated_at"],
        },
        "continuity_evidence": {
            "path": str(evidence_path.resolve()),
            "sha256": context["evidence_sha256"],
            "captured_at": context["evidence_captured_at"],
            "daemon_pid": context["generation"]["pid"],
            "daemon_started_at": context["daemon_started_at"],
        },
        "daemon_generation": context["generation"],
        "current_roots": context["current_roots"],
        "reconciliation": context["reconciliation"],
        "target_manifest": target,
        "target_manifest_sha256": sha256(json_bytes(target)),
        "archive_path": str(archive.resolve()),
    }
    plan["plan_token"] = plan_token_fn(plan)
    return plan


def publish_legacy_migration_plan(
    config: Mapping[str, Any],
    output: str | Path,
    plan: Mapping[str, Any],
    *,
    schema_version: int,
    json_bytes: Callable[[Any], bytes],
    read_private_regular_bytes: Callable[[Path], bytes],
    write_manifest_backup: Callable[[Path, bytes], None],
) -> dict[str, Any]:
    """Durably create a reviewed plan as a private, non-overwriting state file."""
    paths = _state_paths(config)
    destination = Path(output).expanduser()
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    with StateLock(paths["root"], paths["lock"]):
        if destination.parent.resolve() != paths["root"].resolve():
            raise CollectionError(
                "migration plan output must be inside the owner-only state directory"
            )
        if destination.exists() or destination.is_symlink():
            raise CollectionError(
                f"migration plan output already exists: {destination}"
            )
        content = json_bytes(plan)
        write_manifest_backup(destination, content)
        if read_private_regular_bytes(destination) != content:
            raise CollectionError("durable migration plan verification failed")
    dispositions = {
        "carried": sum(
            item.get("disposition") == "carried"
            for item in plan.get("reconciliation", ())
            if isinstance(item, Mapping)
        ),
        "ended": sum(
            item.get("disposition") == "ended"
            for item in plan.get("reconciliation", ())
            if isinstance(item, Mapping)
        ),
    }
    return {
        "schema_version": schema_version,
        "result": "planned",
        "plan": str(destination),
        "plan_token": plan.get("plan_token"),
        "release_sha": plan.get("release_sha"),
        "source_manifest_sha256": plan.get("source_manifest", {}).get("sha256"),
        "target_manifest_sha256": plan.get("target_manifest_sha256"),
        "reconciliation": dispositions,
    }


def _validate_migration_plan(
    plan: Mapping[str, Any],
    paths: Mapping[str, Path],
    release_sha: str,
    *,
    schema_version: int,
    generation_key: Callable[[Mapping[str, Any]], Any],
    json_bytes: Callable[[Any], bytes],
    sha256: Callable[[bytes], str],
    plan_token_fn: Callable[..., Any],
    verify_release_sha: Callable[[Any], str],
) -> None:
    expected_top_level = {
        "schema_version",
        "plan_version",
        "action",
        "created_at",
        "release_sha",
        "source_manifest",
        "continuity_evidence",
        "daemon_generation",
        "current_roots",
        "reconciliation",
        "target_manifest",
        "target_manifest_sha256",
        "archive_path",
        "plan_token",
    }
    if (
        set(plan) != expected_top_level
        or plan.get("schema_version") != schema_version
        or plan.get("plan_version") != 1
        or plan.get("action") != "legacy-recovery-manifest-migration"
        or plan.get("release_sha") != verify_release_sha(release_sha)
        or plan.get("plan_token") != plan_token_fn(plan)
        or not isinstance(plan.get("source_manifest"), Mapping)
        or not isinstance(plan.get("continuity_evidence"), Mapping)
        or not isinstance(plan.get("target_manifest"), Mapping)
        or not isinstance(plan.get("reconciliation"), list)
        or not isinstance(plan.get("current_roots"), list)
        or not isinstance(plan.get("daemon_generation"), Mapping)
    ):
        raise CollectionError(
            "migration plan is invalid, altered, or for another release"
        )
    source = plan["source_manifest"]
    evidence = plan["continuity_evidence"]
    target = plan["target_manifest"]
    if (
        set(source) != {"path", "sha256", "boot_id", "generated_at"}
        or set(evidence)
        != {"path", "sha256", "captured_at", "daemon_pid", "daemon_started_at"}
        or source.get("path") != str(paths["manifest"].resolve())
        or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("sha256", "")))
        or plan.get("archive_path")
        != str(
            (
                paths["root"] / f"recovery-manifest.legacy.{source.get('sha256')}.json"
            ).resolve()
        )
        or plan.get("target_manifest_sha256") != sha256(json_bytes(target))
        or generation_key(target)
        != (
            source.get("boot_id"),
            plan.get("daemon_generation", {}).get("pid"),
            plan.get("daemon_generation", {}).get("process_start_ticks"),
        )
        or evidence.get("daemon_pid") != plan.get("daemon_generation", {}).get("pid")
    ):
        raise CollectionError("migration plan paths or hashes are invalid")


def _receipt_path(paths: Mapping[str, Path]) -> Path:
    return paths["root"] / "recovery-manifest-migration-receipt.json"


def _migration_receipt(
    plan: Mapping[str, Any],
    phase: str,
    *,
    now: float | None = None,
    schema_version: int,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "action": "legacy-recovery-manifest-migration",
        "phase": phase,
        "updated_at": _utc_now(now),
        "plan_token": plan["plan_token"],
        "release_sha": plan["release_sha"],
        "source_manifest_sha256": plan["source_manifest"]["sha256"],
        "target_manifest_sha256": plan["target_manifest_sha256"],
        "continuity_evidence_sha256": plan["continuity_evidence"]["sha256"],
        "daemon_generation": plan["daemon_generation"],
        "reconciliation": plan["reconciliation"],
        "archive_path": plan["archive_path"],
    }


def _read_matching_receipt(
    path: Path,
    plan: Mapping[str, Any],
    *,
    required: bool,
    schema_version: int,
    parse_private_json: Callable[..., tuple[bytes, Mapping[str, Any]]],
    parse_utc_timestamp: Callable[..., Any],
) -> Mapping[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        if required:
            raise CollectionError("migration receipt is missing")
        return None
    _, receipt = parse_private_json(path, "migration receipt")
    expected_fields = {
        "schema_version",
        "action",
        "phase",
        "updated_at",
        "plan_token",
        "release_sha",
        "source_manifest_sha256",
        "target_manifest_sha256",
        "continuity_evidence_sha256",
        "daemon_generation",
        "reconciliation",
        "archive_path",
    }
    parse_utc_timestamp(receipt.get("updated_at"), "migration receipt updated_at")
    if (
        set(receipt) != expected_fields
        or receipt.get("schema_version") != schema_version
        or receipt.get("action") != "legacy-recovery-manifest-migration"
        or receipt.get("plan_token") != plan.get("plan_token")
        or receipt.get("release_sha") != plan.get("release_sha")
        or receipt.get("source_manifest_sha256") != plan["source_manifest"]["sha256"]
        or receipt.get("target_manifest_sha256") != plan.get("target_manifest_sha256")
        or receipt.get("continuity_evidence_sha256")
        != plan["continuity_evidence"]["sha256"]
        or receipt.get("daemon_generation") != plan.get("daemon_generation")
        or receipt.get("reconciliation") != plan.get("reconciliation")
        or receipt.get("archive_path") != plan.get("archive_path")
        or receipt.get("phase") not in {"archive-published", "applied", "rolled-back"}
    ):
        raise CollectionError("migration receipt does not match the reviewed plan")
    return receipt


def _revalidate_plan_context(
    config: Mapping[str, Any],
    plan: Mapping[str, Any],
    legacy_bytes: bytes,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None,
    proc_root: Path | None,
    json_bytes: Callable[[Any], bytes],
    sha256: Callable[[bytes], str],
    migration_context_fn: Callable[..., Any],
) -> None:
    evidence_path = Path(str(plan["continuity_evidence"]["path"]))
    context = migration_context_fn(
        config,
        legacy_bytes=legacy_bytes,
        evidence_path=evidence_path,
        collector=collector,
        proc_root=proc_root,
    )
    semantic_target = dict(context["target_manifest"])
    semantic_target["generated_at"] = plan["target_manifest"].get("generated_at")
    if (
        context["legacy_sha256"] != plan["source_manifest"]["sha256"]
        or context["evidence_sha256"] != plan["continuity_evidence"]["sha256"]
        or context["generation"] != plan["daemon_generation"]
        or context["current_roots"] != plan["current_roots"]
        or context["reconciliation"] != plan["reconciliation"]
        or semantic_target != plan["target_manifest"]
        or sha256(json_bytes(plan["target_manifest"])) != plan["target_manifest_sha256"]
    ):
        raise CollectionError(
            "live state no longer matches the reviewed migration plan"
        )


def apply_legacy_recovery_manifest(
    config: Mapping[str, Any],
    plan_path: str | Path,
    release_sha: str,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    proc_root: Path | None = None,
    schema_version: int,
    parse_private_json: Callable[..., tuple[bytes, Mapping[str, Any]]],
    read_private_regular_bytes: Callable[[Path], bytes],
    sha256: Callable[[bytes], str],
    write_manifest_backup: Callable[[Path, bytes], None],
    atomic_write_json: Callable[[Path, Any], None],
    migration_receipt_fn: Callable[..., Any],
    read_matching_receipt_fn: Callable[..., Any],
    revalidate_plan_context_fn: Callable[..., Any],
    validate_migration_plan_fn: Callable[..., Any],
) -> dict[str, Any]:
    """Apply or safely resume one reviewed legacy migration plan."""
    paths = _state_paths(config)
    _, plan = parse_private_json(Path(plan_path).expanduser(), "migration plan")
    validate_migration_plan_fn(plan, paths, release_sha)
    source_hash = plan["source_manifest"]["sha256"]
    target_hash = plan["target_manifest_sha256"]
    archive = Path(str(plan["archive_path"]))
    receipt_path = _receipt_path(paths)
    with StateLock(paths["root"], paths["lock"]):
        if paths["pending"].exists() or paths["pending"].is_symlink():
            raise CollectionError(
                "legacy recovery migration refuses an existing pending queue"
            )
        canonical = read_private_regular_bytes(paths["manifest"])
        canonical_hash = sha256(canonical)
        receipt = read_matching_receipt_fn(
            receipt_path, plan, required=canonical_hash == target_hash
        )
        if receipt and receipt.get("phase") == "rolled-back":
            raise CollectionError("reviewed migration plan was already rolled back")
        if canonical_hash not in {source_hash, target_hash}:
            raise CollectionError(
                "recovery manifest no longer matches plan source or target"
            )
        if archive.exists() or archive.is_symlink():
            legacy_bytes = read_private_regular_bytes(archive)
            if sha256(legacy_bytes) != source_hash:
                raise CollectionError(
                    "legacy archive does not match the reviewed source"
                )
        else:
            if canonical_hash != source_hash:
                raise CollectionError(
                    "legacy archive is missing after target publication"
                )
            legacy_bytes = canonical
        if canonical_hash == target_hash:
            if receipt is None:
                raise CollectionError(
                    "target manifest has no matching migration receipt"
                )
            if receipt.get("phase") != "applied":
                atomic_write_json(receipt_path, migration_receipt_fn(plan, "applied"))
            return {
                "schema_version": schema_version,
                "result": "already-applied",
                "plan_token": plan["plan_token"],
                "receipt": str(receipt_path),
            }
        revalidate_plan_context_fn(
            config,
            plan,
            legacy_bytes,
            collector=collector,
            proc_root=proc_root,
        )
        if not archive.exists():
            write_manifest_backup(archive, canonical)
        if sha256(read_private_regular_bytes(archive)) != source_hash:
            raise CollectionError("durable legacy archive verification failed")
        atomic_write_json(receipt_path, migration_receipt_fn(plan, "archive-published"))
        if sha256(read_private_regular_bytes(paths["manifest"])) != source_hash:
            raise CollectionError(
                "legacy manifest changed before migration publication"
            )
        atomic_write_json(paths["manifest"], plan["target_manifest"])
        if sha256(read_private_regular_bytes(paths["manifest"])) != target_hash:
            raise CollectionError("target recovery manifest verification failed")
        atomic_write_json(receipt_path, migration_receipt_fn(plan, "applied"))
        return {
            "schema_version": schema_version,
            "result": "applied",
            "plan_token": plan["plan_token"],
            "archive": str(archive),
            "receipt": str(receipt_path),
        }


def rollback_legacy_recovery_manifest(
    config: Mapping[str, Any],
    plan_path: str | Path,
    release_sha: str,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    proc_root: Path | None = None,
    schema_version: int,
    atomic_write_private_bytes: Callable[[Path, bytes], None],
    parse_private_json: Callable[..., tuple[bytes, Mapping[str, Any]]],
    read_private_regular_bytes: Callable[[Path], bytes],
    sha256: Callable[[bytes], str],
    atomic_write_json: Callable[[Path, Any], None],
    migration_receipt_fn: Callable[..., Any],
    read_matching_receipt_fn: Callable[..., Any],
    revalidate_plan_context_fn: Callable[..., Any],
    validate_migration_plan_fn: Callable[..., Any],
) -> dict[str, Any]:
    """Restore exact legacy bytes while every plan and generation guard holds."""
    paths = _state_paths(config)
    _, plan = parse_private_json(Path(plan_path).expanduser(), "migration plan")
    validate_migration_plan_fn(plan, paths, release_sha)
    source_hash = plan["source_manifest"]["sha256"]
    target_hash = plan["target_manifest_sha256"]
    archive = Path(str(plan["archive_path"]))
    receipt_path = _receipt_path(paths)
    with StateLock(paths["root"], paths["lock"]):
        if paths["pending"].exists() or paths["pending"].is_symlink():
            raise CollectionError(
                "legacy migration rollback refuses an existing pending queue"
            )
        receipt = read_matching_receipt_fn(receipt_path, plan, required=True)
        assert receipt is not None
        legacy_bytes = read_private_regular_bytes(archive)
        if sha256(legacy_bytes) != source_hash:
            raise CollectionError("legacy archive does not match the reviewed source")
        canonical = read_private_regular_bytes(paths["manifest"])
        canonical_hash = sha256(canonical)
        if canonical_hash not in {source_hash, target_hash}:
            raise CollectionError("rollback refuses a changed recovery manifest")
        revalidate_plan_context_fn(
            config,
            plan,
            legacy_bytes,
            collector=collector,
            proc_root=proc_root,
        )
        if canonical_hash == source_hash:
            if receipt.get("phase") != "rolled-back":
                atomic_write_json(
                    receipt_path, migration_receipt_fn(plan, "rolled-back")
                )
            return {
                "schema_version": schema_version,
                "result": "already-rolled-back",
                "plan_token": plan["plan_token"],
                "receipt": str(receipt_path),
            }
        atomic_write_private_bytes(paths["manifest"], legacy_bytes)
        if sha256(read_private_regular_bytes(paths["manifest"])) != source_hash:
            raise CollectionError("exact legacy rollback verification failed")
        atomic_write_json(receipt_path, migration_receipt_fn(plan, "rolled-back"))
        return {
            "schema_version": schema_version,
            "result": "rolled-back",
            "plan_token": plan["plan_token"],
            "receipt": str(receipt_path),
        }
