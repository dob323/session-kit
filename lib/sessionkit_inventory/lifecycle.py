"""Privacy-minimal provider-exit state for managed terminal lifecycles."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any, Mapping

from .common import CollectionError, PROVIDERS, valid_uuid
from .model import recovery_spec
from .state_io import (
    atomic_write_private_json,
    create_private_json,
    read_private_json,
)


LIFECYCLE_SCHEMA_VERSION = 3
MAX_LIFECYCLE_BYTES = 16 * 1024
_SESSION_ID_RE = re.compile(
    r"(?:main(?:[1-9][0-9]*)?|"
    r"s[0-9]{8}-[0-9]{6}-[1-9][0-9]*(?:-[1-9][0-9]*)?)"
)
_BOOT_ID_RE = re.compile(r"[0-9a-fA-F-]{8,128}")


def _checked_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise CollectionError("lifecycle state requires a managed shpool session ID")
    return session_id


def _secret_path(state_dir: Path) -> Path:
    return state_dir / "lifecycle" / "key.json"


def _lifecycle_secret(state_dir: Path, *, create: bool) -> bytes | None:
    path = _secret_path(state_dir)
    value = read_private_json(
        path,
        max_bytes=4096,
        allow_missing=True,
    )
    if value is None and create:
        create_private_json(
            path,
            {
                "schema_version": 1,
                "secret": secrets.token_hex(32),
            },
        )
        value = read_private_json(path, max_bytes=4096)
    if value is None:
        return None
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema_version", "secret"}
        or value.get("schema_version") != 1
        or not isinstance(value.get("secret"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["secret"])
    ):
        raise CollectionError("lifecycle key has an invalid schema")
    return bytes.fromhex(value["secret"])


def session_key(state_dir: Path, session_id: str, *, create: bool) -> str | None:
    """Return a keyed local identifier that does not expose predictable IDs."""
    checked = _checked_session_id(session_id)
    secret = _lifecycle_secret(state_dir, create=create)
    if secret is None:
        return None
    return hmac.new(secret, checked.encode("utf-8"), hashlib.sha256).hexdigest()


def lifecycle_path(
    state_dir: Path,
    session_id: str,
    *,
    create_key: bool = False,
) -> Path | None:
    key = session_key(state_dir, session_id, create=create_key)
    return None if key is None else state_dir / "lifecycle" / f"{key}.json"


def _last_exact_path(
    state_dir: Path,
    session_id: str,
    *,
    create_key: bool = False,
) -> Path | None:
    key = session_key(state_dir, session_id, create=create_key)
    return None if key is None else state_dir / "lifecycle" / f"{key}.exact.json"


def _positive_number(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CollectionError(f"lifecycle {label} is invalid")
    return value


def _validated_state(value: Any, expected_key: str | None = None) -> dict[str, Any]:
    if (
        isinstance(value, Mapping)
        and value.get("schema_version") == 2
        and "conversation_uuid" not in value
    ):
        value = {**value, "schema_version": 3, "conversation_uuid": None}
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "session_key",
        "boot_id",
        "shell_pid",
        "shell_start_ticks",
        "provider",
        "conversation_uuid",
        "provider_exited_at_monotonic_ns",
        "exit_code",
        "input_tracking",
        "user_input_after_exit",
        "keep",
    }:
        raise CollectionError("lifecycle state has an invalid schema")
    key = value.get("session_key")
    boot_id = value.get("boot_id")
    provider = value.get("provider")
    conversation_uuid = value.get("conversation_uuid")
    exit_code = value.get("exit_code")
    if (
        value.get("schema_version") != LIFECYCLE_SCHEMA_VERSION
        or not isinstance(key, str)
        or not re.fullmatch(r"[0-9a-f]{64}", key)
        or (expected_key is not None and key != expected_key)
        or not isinstance(boot_id, str)
        or not _BOOT_ID_RE.fullmatch(boot_id)
        or provider not in PROVIDERS
        or (
            conversation_uuid is not None
            and valid_uuid(conversation_uuid) != conversation_uuid
        )
        or isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or exit_code < 0
        or exit_code > 255
        or not isinstance(value.get("input_tracking"), bool)
        or not isinstance(value.get("user_input_after_exit"), bool)
        or not isinstance(value.get("keep"), bool)
    ):
        raise CollectionError("lifecycle state has invalid fields")
    for name in (
        "shell_pid",
        "shell_start_ticks",
        "provider_exited_at_monotonic_ns",
    ):
        _positive_number(value.get(name), name)
    return dict(value)


def load_state(state_dir: Path, session_id: str) -> dict[str, Any] | None:
    key = session_key(state_dir, session_id, create=False)
    if key is None:
        return None
    path = lifecycle_path(state_dir, session_id)
    assert path is not None
    value = read_private_json(
        path,
        max_bytes=MAX_LIFECYCLE_BYTES,
        allow_missing=True,
    )
    return None if value is None else _validated_state(value, key)


def record_provider_exit(
    state_dir: Path,
    *,
    session_id: str,
    boot_id: str,
    shell_pid: int,
    shell_start_ticks: int,
    provider: str,
    conversation_uuid: str | None = None,
    exit_code: int,
    input_tracking: bool,
    now_monotonic_ns: int | None = None,
) -> dict[str, Any]:
    """Record one exact provider exit while preserving prior safety blockers."""
    if (
        conversation_uuid is not None
        and valid_uuid(conversation_uuid) != conversation_uuid
    ):
        raise CollectionError("provider-exit conversation UUID is invalid")
    key = session_key(state_dir, session_id, create=True)
    assert key is not None
    previous = load_state(state_dir, session_id)
    same_generation = bool(
        previous
        and previous["boot_id"] == boot_id
        and previous["shell_pid"] == shell_pid
        and previous["shell_start_ticks"] == shell_start_ticks
    )
    if same_generation and previous is not None:
        prior_conversation = previous.get("conversation_uuid")
        if previous.get("provider") != provider:
            raise CollectionError(
                "provider-exit provider changed within one shell generation"
            )
        if (
            prior_conversation is not None
            and conversation_uuid is not None
            and prior_conversation != conversation_uuid
        ):
            raise CollectionError(
                "provider-exit conversation changed within one shell generation"
            )
        if conversation_uuid is None:
            conversation_uuid = prior_conversation
    value = {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "session_key": key,
        "boot_id": boot_id,
        "shell_pid": shell_pid,
        "shell_start_ticks": shell_start_ticks,
        "provider": provider,
        "conversation_uuid": valid_uuid(conversation_uuid) or None,
        "provider_exited_at_monotonic_ns": (
            time.monotonic_ns() if now_monotonic_ns is None else now_monotonic_ns
        ),
        "exit_code": exit_code,
        "input_tracking": input_tracking,
        "user_input_after_exit": bool(
            previous["user_input_after_exit"]
            if same_generation and previous is not None
            else False
        ),
        "keep": bool(
            previous["keep"] if same_generation and previous is not None else False
        ),
    }
    checked = _validated_state(value, key)
    path = lifecycle_path(state_dir, session_id)
    assert path is not None
    atomic_write_private_json(path, checked)
    return checked


def update_state(
    state_dir: Path,
    *,
    session_id: str,
    boot_id: str,
    shell_pid: int,
    shell_start_ticks: int,
    event: str,
    keep: bool | None = None,
) -> dict[str, Any]:
    """Record permanent shell use or an explicit keep choice."""
    current = load_state(state_dir, session_id)
    if current is None:
        raise CollectionError("provider-exit lifecycle state is unavailable")
    if (
        current["boot_id"] != boot_id
        or current["shell_pid"] != shell_pid
        or current["shell_start_ticks"] != shell_start_ticks
    ):
        raise CollectionError("provider-exit lifecycle generation changed")
    if event == "user-input":
        current["user_input_after_exit"] = True
    elif event == "keep" and isinstance(keep, bool):
        current["keep"] = keep
    else:
        raise CollectionError("unsupported lifecycle update")
    key = session_key(state_dir, session_id, create=False)
    assert key is not None
    checked = _validated_state(current, key)
    path = lifecycle_path(state_dir, session_id)
    assert path is not None
    atomic_write_private_json(path, checked)
    return checked


def _same_shell_generation(
    current: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> bool:
    current_shell = current.get("shpool_shell")
    previous_shell = previous.get("shpool_shell")
    return (
        current.get("shpool_id_raw") == previous.get("shpool_id_raw")
        and current.get("started_at_unix_ms") == previous.get("started_at_unix_ms")
        and isinstance(current_shell, Mapping)
        and isinstance(previous_shell, Mapping)
        and current_shell.get("pid") == previous_shell.get("pid")
        and current_shell.get("process_start_ticks")
        == previous_shell.get("process_start_ticks")
    )


def _historical_provider(
    prior: Mapping[str, Any],
    provider: str,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    prior_provider = prior.get("provider")
    prior_exited_provider = prior.get("exited_provider")
    prior_identity = prior.get("identity")
    prior_exited_identity = prior.get("exited_identity")
    prior_recovery = prior.get("recovery")
    identity_hint: dict[str, str] | None = None
    if (
        prior_provider == provider
        and isinstance(prior_identity, Mapping)
        and prior_identity.get("confidence") == "exact"
    ):
        uuid = valid_uuid(prior_identity.get("uuid"))
        if uuid:
            identity_hint = {"provider": provider, "uuid": uuid}
    elif prior_exited_provider == provider and isinstance(
        prior_exited_identity, Mapping
    ):
        uuid = valid_uuid(prior_exited_identity.get("uuid"))
        if uuid:
            identity_hint = {"provider": provider, "uuid": uuid}
    recovery = (
        dict(prior_recovery)
        if isinstance(prior_recovery, Mapping)
        and prior_recovery.get("available") is True
        and prior_recovery.get("provider") == provider
        and valid_uuid(prior_recovery.get("uuid"))
        else None
    )
    return identity_hint, recovery


def _last_exact_document(
    item: Mapping[str, Any],
    *,
    state_dir: Path,
    boot_id: str,
) -> dict[str, Any] | None:
    session_id = item.get("shpool_id_raw")
    shell = item.get("shpool_shell")
    provider = item.get("provider")
    identity = item.get("identity")
    recovery = item.get("recovery")
    if (
        not isinstance(session_id, str)
        or not isinstance(shell, Mapping)
        or provider not in PROVIDERS
        or not isinstance(identity, Mapping)
        or identity.get("confidence") != "exact"
        or not isinstance(recovery, Mapping)
        or recovery.get("available") is not True
    ):
        return None
    uuid = valid_uuid(identity.get("uuid"))
    if not uuid or recovery.get("provider") != provider:
        return None
    session_hash = session_key(state_dir, session_id, create=True)
    assert session_hash is not None
    return {
        "schema_version": 1,
        "session_key": session_hash,
        "boot_id": boot_id,
        "shell_pid": shell.get("pid"),
        "shell_start_ticks": shell.get("process_start_ticks"),
        "provider": provider,
        "uuid": uuid,
        "title": item.get("title"),
        "display_title": item.get("display_title"),
        "title_source": item.get("title_source"),
        "recovery": dict(recovery),
    }


def _validated_last_exact(
    value: Any,
    *,
    expected_key: str,
    boot_id: str,
    shell_pid: Any,
    shell_start_ticks: Any,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "session_key",
        "boot_id",
        "shell_pid",
        "shell_start_ticks",
        "provider",
        "uuid",
        "title",
        "display_title",
        "title_source",
        "recovery",
    }:
        return None
    provider = value.get("provider")
    uuid = valid_uuid(value.get("uuid"))
    recovery = value.get("recovery")
    if (
        value.get("schema_version") != 1
        or value.get("session_key") != expected_key
        or value.get("boot_id") != boot_id
        or value.get("shell_pid") != shell_pid
        or value.get("shell_start_ticks") != shell_start_ticks
        or provider not in PROVIDERS
        or not uuid
        or not isinstance(recovery, Mapping)
        or recovery.get("available") is not True
        or recovery.get("provider") != provider
        or valid_uuid(recovery.get("uuid")) != uuid
    ):
        return None
    return dict(value)


def persist_last_exact(
    inventory: Mapping[str, Any],
    previous_inventory: Mapping[str, Any] | None,
    *,
    state_dir: Path,
    boot_id: str,
) -> None:
    """Preserve exact recovery across the provider-exit/cache handoff race."""
    prior_by_id = {
        item.get("shpool_id_raw"): item
        for item in (
            previous_inventory.get("sessions", ())
            if isinstance(previous_inventory, Mapping)
            else ()
        )
        if isinstance(item, Mapping) and isinstance(item.get("shpool_id_raw"), str)
    }
    for item in inventory.get("sessions", ()):
        if not isinstance(item, Mapping):
            continue
        # The canonical inventory already retains exact identity while the
        # provider is live. Write a separate handoff record only at the moment
        # a same-generation live row would otherwise be replaced by an idle
        # shell row.
        if item.get("provider") in PROVIDERS:
            continue
        document = None
        prior = prior_by_id.get(item.get("shpool_id_raw"))
        if isinstance(prior, Mapping) and _same_shell_generation(item, prior):
            document = _last_exact_document(
                prior,
                state_dir=state_dir,
                boot_id=boot_id,
            )
        if document is None:
            continue
        path = _last_exact_path(
            state_dir,
            str(item["shpool_id_raw"]),
            create_key=True,
        )
        assert path is not None
        atomic_write_private_json(path, document)


def load_last_exact(
    state_dir: Path,
    session_id: str,
    *,
    boot_id: str,
    shell_pid: Any,
    shell_start_ticks: Any,
) -> dict[str, Any] | None:
    key = session_key(state_dir, session_id, create=False)
    path = _last_exact_path(state_dir, session_id)
    if key is None or path is None:
        return None
    value = read_private_json(
        path,
        max_bytes=MAX_LIFECYCLE_BYTES,
        allow_missing=True,
    )
    if value is None:
        return None
    return _validated_last_exact(
        value,
        expected_key=key,
        boot_id=boot_id,
        shell_pid=shell_pid,
        shell_start_ticks=shell_start_ticks,
    )


def prune_inactive_state(
    state_dir: Path,
    active_session_ids: list[str],
) -> int:
    """Remove generation state only after its managed session disappears."""
    secret = _lifecycle_secret(state_dir, create=False)
    lifecycle_dir = state_dir / "lifecycle"
    if secret is None or not lifecycle_dir.exists():
        return 0
    active_keys = {
        hmac.new(
            secret,
            _checked_session_id(session_id).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        for session_id in active_session_ids
    }
    removed = 0
    for path in lifecycle_dir.iterdir():
        match = re.fullmatch(
            r"([0-9a-f]{64})(?:\.exact)?\.json",
            path.name,
        )
        if not match or match.group(1) in active_keys:
            continue
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise CollectionError(
                "inactive lifecycle state is not a private regular file"
            )
        path.unlink()
        removed += 1
    if removed:
        directory = os.open(lifecycle_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return removed


def apply_provider_exit_states(
    inventory: dict[str, Any],
    previous_inventory: Mapping[str, Any] | None,
    *,
    state_dir: Path,
    boot_id: str,
) -> None:
    """Overlay exact provider-exit facts onto otherwise idle shell rows."""
    prior_by_id = {
        item.get("shpool_id_raw"): item
        for item in (
            previous_inventory.get("sessions", ())
            if isinstance(previous_inventory, Mapping)
            else ()
        )
        if isinstance(item, Mapping) and isinstance(item.get("shpool_id_raw"), str)
    }
    for item in inventory.get("sessions", ()):
        if (
            not isinstance(item, dict)
            or item.get("provider") != "shell"
            or item.get("native_title") != "Idle shell"
        ):
            continue
        session_id = item.get("shpool_id_raw")
        shell = item.get("shpool_shell")
        if not isinstance(session_id, str) or not isinstance(shell, Mapping):
            continue
        try:
            lifecycle = load_state(state_dir, session_id)
        except CollectionError as exc:
            diagnostics = item.setdefault("diagnostics", [])
            if isinstance(diagnostics, list):
                diagnostics.append(f"provider-exit state unavailable: {exc}")
            continue
        if (
            lifecycle is None
            or lifecycle["boot_id"] != boot_id
            or lifecycle["shell_pid"] != shell.get("pid")
            or lifecycle["shell_start_ticks"] != shell.get("process_start_ticks")
        ):
            continue
        provider = lifecycle["provider"]
        prior = prior_by_id.get(session_id)
        recovery: dict[str, Any] | None = None
        identity_hint: dict[str, str] | None = None
        if isinstance(prior, Mapping) and _same_shell_generation(item, prior):
            identity_hint, recovery = _historical_provider(prior, provider)
            for key in ("title", "display_title", "title_source"):
                if isinstance(prior.get(key), str) and prior[key]:
                    item[key] = prior[key]
        if identity_hint is None or recovery is None:
            retained = load_last_exact(
                state_dir,
                session_id,
                boot_id=boot_id,
                shell_pid=shell.get("pid"),
                shell_start_ticks=shell.get("process_start_ticks"),
            )
            if retained is not None and retained["provider"] == provider:
                retained_hint = {
                    "provider": provider,
                    "uuid": retained["uuid"],
                }
                identity_hint = identity_hint or retained_hint
                recovery = recovery or dict(retained["recovery"])
                for key in ("title", "display_title", "title_source"):
                    if isinstance(retained.get(key), str) and retained[key]:
                        item[key] = retained[key]
        committed_uuid = valid_uuid(lifecycle.get("conversation_uuid"))
        if committed_uuid:
            identity_hint = identity_hint or {
                "provider": provider,
                "uuid": committed_uuid,
            }
            recovery = recovery or recovery_spec(
                provider,
                committed_uuid,
                item.get("cwd") if isinstance(item.get("cwd"), str) else None,
            )
        item["display_provider"] = provider
        item["agent_status"] = "provider exited"
        item["needs_you"] = False
        item["exited_provider"] = provider
        item["provider_exited_at_monotonic_ns"] = lifecycle[
            "provider_exited_at_monotonic_ns"
        ]
        item["provider_exit_code"] = lifecycle["exit_code"]
        item["provider_exit_input_tracking"] = lifecycle["input_tracking"]
        item["user_input_after_provider_exit"] = lifecycle["user_input_after_exit"]
        item["provider_exit_keep"] = lifecycle["keep"]
        if identity_hint is not None:
            item["_terminal_identity_hint"] = identity_hint
            item["exited_identity"] = {
                "uuid": identity_hint["uuid"],
                "provenance": (
                    "committed provider-exit conversation for this shell generation"
                    if identity_hint["uuid"] == committed_uuid
                    else "last exact live inventory for this shell generation"
                ),
                "confidence": "historical-exact",
            }
        if recovery is not None:
            item["recovery"] = recovery
