"""Session colour: the palette, its pure derivations, and colour state.

The palette lives here together with everything that validates or rotates
against it, so widening it is one edit in this module plus the truecolor table
in ``render``. Provider colour pushes stay with the naming domain: they share
the propagation and retry machinery with title pushes rather than the palette.

Every function that consumes the palette takes it as an argument. The facade
passes its own ``SESSION_COLORS``, which keeps that constant the single place
the in-force palette is decided.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from .common import PROVIDERS, CollectionError, valid_uuid


SESSION_COLORS = (
    "red",
    "blue",
    "green",
    "yellow",
    "purple",
    "orange",
    "pink",
    "cyan",
)

# A brand-new Codex session has no conversation ID until Codex boots, so its
# launch theme cannot come from the identity hash. The launch color is picked
# deterministically from the shpool session name, recorded as a marker, and
# adopted as that conversation's explicit override the first time the live
# collector sees the session's real ID — from then on the window theme, the
# picker row, and every future resume agree.
LAUNCH_COLOR_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


COLOR_RESERVATION_MAX_AGE_SECONDS = 10 * 60


def session_color(
    provider: str,
    uuid: str | None,
    overrides: Mapping[str, str] | None = None,
    *,
    palette: Sequence[str],
) -> str | None:
    """Stable per-conversation color: explicit override, else identity hash."""
    if provider not in PROVIDERS or not uuid:
        return None
    exact_uuid = valid_uuid(uuid)
    if not exact_uuid:
        return None
    override = (overrides or {}).get(f"{provider}:{exact_uuid}")
    if override in palette:
        return override
    digest = hashlib.sha256(f"{provider}:{exact_uuid}".encode("utf-8")).digest()
    return palette[digest[0] % len(palette)]


def launch_color_for(
    shpool_id: str,
    occupied_colors: Iterable[str] = (),
    *,
    palette: Sequence[str],
) -> str:
    digest = hashlib.sha256(f"launch:{shpool_id}".encode("utf-8")).digest()
    start = digest[0] % len(palette)
    occupied = {color for color in occupied_colors if color in palette}
    for offset in range(len(palette)):
        candidate = palette[(start + offset) % len(palette)]
        if candidate not in occupied:
            return candidate
    return palette[start]


def _launch_color_dir(config: Mapping[str, Any]) -> Path | None:
    state_dir = config.get("state_dir")
    if not state_dir:
        return None
    return Path(state_dir) / "launch-color"


def canonical_colors(
    config: Mapping[str, Any],
    *,
    config_path: Callable[[], Path],
    private_alias_document: Callable[..., dict[str, Any]],
    valid_colors: Callable[[Any], dict[str, str]],
) -> dict[str, str]:
    document = private_alias_document(config_path(), allow_missing=True)
    return valid_colors(document.get("colors"))


def _active_color_reservations(
    path: Path,
    now: float,
    *,
    read_state_json: Callable[[Path], Any],
    palette: Sequence[str],
    reservation_max_age: float,
) -> dict[str, dict[str, Any]]:
    raw = read_state_json(path)
    entries = raw.get("entries", {}) if isinstance(raw, Mapping) else {}
    return {
        key: {"color": item["color"], "created_at": float(item["created_at"])}
        for key, item in entries.items()
        if isinstance(key, str)
        and isinstance(item, Mapping)
        and item.get("color") in palette
        and isinstance(item.get("created_at"), (int, float))
        and not isinstance(item.get("created_at"), bool)
        and 0 <= now - float(item["created_at"]) <= reservation_max_age
    }


def record_launch_color(
    config: Mapping[str, Any],
    shpool_id: str,
    occupied_colors: Iterable[str] = (),
    *,
    launch_color_dir: Callable[[Mapping[str, Any]], Path | None],
    launch_color: Callable[..., str],
    active_color_reservations: Callable[[Path, float], dict[str, dict[str, Any]]],
    state_paths: Callable[[Mapping[str, Any]], Mapping[str, Path]],
    state_lock: Callable[..., Any],
    atomic_write_json: Callable[[Path, Any], None],
    schema_version: int,
) -> str | None:
    """Pick and persist a launch color for a session with no conversation ID."""
    if (
        not shpool_id
        or "/" in shpool_id
        or shpool_id.startswith(".")
        or len(shpool_id) > 128
    ):
        return None
    directory = launch_color_dir(config)
    if directory is None:
        return launch_color(shpool_id, occupied_colors)
    paths = state_paths(config)
    with state_lock(paths["root"], paths["lock"]):
        now = time.time()
        reservations = active_color_reservations(paths["color_reservations"], now)
        reservation_key = f"launch:{shpool_id}"
        existing = reservations.get(reservation_key)
        if existing:
            color = str(existing["color"])
        else:
            occupied = set(occupied_colors)
            occupied.update(item["color"] for item in reservations.values())
            color = launch_color(shpool_id, occupied)
            reservations[reservation_key] = {"color": color, "created_at": now}
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(directory / shpool_id, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(color + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        atomic_write_json(
            paths["color_reservations"],
            {"schema_version": schema_version, "entries": reservations},
        )
        return color


def _reserve_conversation_color(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    occupied_colors: Iterable[str],
    *,
    state_paths: Callable[[Mapping[str, Any]], Mapping[str, Path]],
    config_path: Callable[[], Path],
    state_lock: Callable[..., Any],
    active_color_reservations: Callable[[Path, float], dict[str, dict[str, Any]]],
    color_for_session: Callable[..., str | None],
    private_alias_parent: Callable[[Path], None],
    private_alias_document: Callable[..., dict[str, Any]],
    valid_colors: Callable[[Any], dict[str, str]],
    atomic_write_json: Callable[[Path, Any], None],
    schema_version: int,
    palette: Sequence[str],
) -> str:
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise CollectionError("conversation color requires an exact provider UUID")
    paths = state_paths(config)
    path = config_path()
    key = f"{provider}:{exact_uuid}"
    with state_lock(paths["root"], paths["config_lock"]):
        with state_lock(paths["root"], paths["lock"]):
            now = time.time()
            reservations = active_color_reservations(paths["color_reservations"], now)
            occupied = set(occupied_colors)
            occupied.update(item["color"] for item in reservations.values())
            preferred = color_for_session(provider, exact_uuid)
            assert preferred is not None
            start = list(palette).index(preferred)
            color = next(
                (
                    palette[(start + offset) % len(palette)]
                    for offset in range(len(palette))
                    if palette[(start + offset) % len(palette)] not in occupied
                ),
                preferred,
            )
            private_alias_parent(path)
            document = private_alias_document(path, allow_missing=True)
            colors = valid_colors(document.get("colors"))
            colors[key] = color
            document["colors"] = dict(sorted(colors.items()))
            atomic_write_json(path, document)
            reservations[f"conversation:{key}"] = {"color": color, "created_at": now}
            atomic_write_json(
                paths["color_reservations"],
                {"schema_version": schema_version, "entries": reservations},
            )
            return color


def _release_conversation_color(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    color: str,
    *,
    state_paths: Callable[[Mapping[str, Any]], Mapping[str, Path]],
    config_path: Callable[[], Path],
    state_lock: Callable[..., Any],
    active_color_reservations: Callable[[Path, float], dict[str, dict[str, Any]]],
    private_alias_document: Callable[..., dict[str, Any]],
    valid_colors: Callable[[Any], dict[str, str]],
    atomic_write_json: Callable[[Path, Any], None],
    schema_version: int,
    palette: Sequence[str],
) -> bool:
    """Roll back one failed pre-bake only when reservation and override match."""
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid or color not in palette:
        return False
    paths = state_paths(config)
    path = config_path()
    key = f"{provider}:{exact_uuid}"
    reservation_key = f"conversation:{key}"
    with state_lock(paths["root"], paths["config_lock"]):
        with state_lock(paths["root"], paths["lock"]):
            reservations = active_color_reservations(
                paths["color_reservations"], time.time()
            )
            reserved = reservations.get(reservation_key)
            document = private_alias_document(path, allow_missing=True)
            colors = valid_colors(document.get("colors"))
            if (
                not reserved
                or reserved.get("color") != color
                or colors.get(key) != color
            ):
                return False
            reservations.pop(reservation_key, None)
            colors.pop(key, None)
            if colors:
                document["colors"] = dict(sorted(colors.items()))
            else:
                document.pop("colors", None)
            atomic_write_json(path, document)
            atomic_write_json(
                paths["color_reservations"],
                {"schema_version": schema_version, "entries": reservations},
            )
            return True


def _adopt_launch_colors(
    config: Mapping[str, Any],
    sessions: Sequence[Mapping[str, Any]],
    overrides: Mapping[str, str],
    *,
    launch_color_dir: Callable[[Mapping[str, Any]], Path | None],
    mutate_color: Callable[..., dict[str, str]],
    palette: Sequence[str],
    launch_color_max_age: float,
) -> dict[str, str]:
    """Turn launch-color markers into explicit overrides once IDs are known."""
    current = dict(overrides)
    directory = launch_color_dir(config)
    if directory is None or not directory.is_dir():
        return current
    now = time.time()
    by_shpool_id: dict[str, Mapping[str, Any]] = {
        str(item.get("shpool_id") or ""): item
        for item in sessions
        if item.get("provider") == "codex"
    }
    try:
        markers = list(directory.iterdir())
    except OSError:
        return current
    for marker in markers:
        try:
            if marker.is_symlink() or not marker.is_file():
                continue
            stat_result = marker.stat()
            if now - stat_result.st_mtime > launch_color_max_age:
                marker.unlink(missing_ok=True)
                continue
            item = by_shpool_id.get(marker.name)
            if item is None:
                continue
            uuid = valid_uuid(str((item.get("identity") or {}).get("uuid") or ""))
            if not uuid:
                continue
            color = marker.read_text(encoding="utf-8", errors="strict").strip()
            if color not in palette:
                marker.unlink(missing_ok=True)
                continue
            key = f"codex:{uuid}"
            if key not in current:
                current = mutate_color(config, "codex", uuid, color)
            marker.unlink(missing_ok=True)
        except (OSError, ValueError, CollectionError):
            continue
    return current


def mutate_canonical_color(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    color: str | None,
    *,
    config_path: Callable[[], Path],
    state_paths: Callable[[Mapping[str, Any]], Mapping[str, Path]],
    state_lock: Callable[..., Any],
    private_alias_parent: Callable[[Path], None],
    private_alias_document: Callable[..., dict[str, Any]],
    valid_colors: Callable[[Any], dict[str, str]],
    atomic_write_json: Callable[[Path, Any], None],
    palette: Sequence[str],
) -> dict[str, str]:
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise CollectionError("color requires provider claude|codex and an exact UUID")
    if color is not None and color not in palette:
        raise CollectionError("color must be one of: " + ", ".join(palette))
    path = config_path()
    paths = state_paths(config)
    with state_lock(paths["root"], paths["config_lock"]):
        with state_lock(paths["root"], paths["lock"]):
            private_alias_parent(path)
            document = private_alias_document(path, allow_missing=True)
            colors = valid_colors(document.get("colors"))
            key = f"{provider}:{exact_uuid}"
            if color is None:
                colors.pop(key, None)
            else:
                colors[key] = color
            if colors:
                document["colors"] = dict(sorted(colors.items()))
            else:
                document.pop("colors", None)
            atomic_write_json(path, document)
            return dict(colors)
