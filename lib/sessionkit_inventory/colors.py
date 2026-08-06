"""Session colour: the palettes, their pure derivations, and colour state.

The palettes live here together with everything that validates or rotates
against them, so widening one is an edit in this module plus the truecolor table
in ``render``. Provider colour pushes stay with the naming domain: they share
the propagation and retry machinery with title pushes rather than the palette.

There are two palettes because the providers are not equally free. Claude Code's
``/color`` accepts exactly eight names and rejects everything else, so the Claude
palette is a hard external constraint rather than a preference. Codex resolves a
colour from an ``sk-*.tmTheme`` file this kit ships and applies no allow-list at
all, so its palette is whatever we build themes for. Giving Codex six names that
Claude cannot use means a Claude window and a Codex window can never land on the
same colour, and each still shows its own native tint.

Every function that consumes a palette takes it as an argument, and the facade
decides which one applies from the provider. That keeps the two constants the
single place the in-force palettes are declared, and it means a caller that
forgets the provider gets an empty palette rather than the wrong one.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from .common import PROVIDERS, CollectionError, valid_uuid


# Claude Code's /color accepts these eight names and nothing else. Twenty-two
# names were probed against Claude Code 2.1.223 with known-good and known-bad
# controls: every other name is rejected, and gray/grey resolve to `default`,
# which is no colour at all. This tuple is therefore a measurement, not a taste
# decision — do not add to it without re-probing the provider.
CLAUDE_SESSION_COLORS = (
    "red",
    "blue",
    "green",
    "yellow",
    "purple",
    "orange",
    "pink",
    "cyan",
)

# Codex reads a theme file from disk and applies no allow-list, so these six are
# ours to choose. They are deliberately disjoint from the Claude palette: no
# name here is one Claude Code would accept, which is what makes a cross-provider
# collision impossible rather than merely unlikely. Each has a matching
# config/codex-themes/sk-<name>.tmTheme; adding a name without its theme file
# leaves a Codex window untinted and trips the doctor's theme check.
CODEX_SESSION_COLORS = (
    "lime",
    "magenta",
    "silver",
    "sand",
    "sky",
    "sea",
)

# Every colour the kit knows. Use this only where the provider genuinely is not
# known — CLI argument choices, the shared reservation ledger, stored-document
# scans. Anywhere the provider IS known, use palette_for_provider: validating a
# Claude override against this union would accept `lime`, which Claude Code
# rejects at the moment it matters and leaves the window with no colour.
SESSION_COLORS = CLAUDE_SESSION_COLORS + CODEX_SESSION_COLORS


def palette_for_provider(
    provider: str,
    *,
    claude_palette: Sequence[str],
    codex_palette: Sequence[str],
) -> tuple[str, ...]:
    """The in-force palette for one provider, or empty for anything else.

    Empty rather than a default is deliberate. A caller that reaches here with
    ``shell`` or an unknown provider has no colour to assign, and returning a
    populated palette would let it assign one that the provider cannot show.
    """
    if provider == "claude":
        return tuple(claude_palette)
    if provider == "codex":
        return tuple(codex_palette)
    return ()


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


def first_free_color(
    preferred: str,
    occupied_colors: Iterable[str] = (),
    *,
    palette: Sequence[str],
) -> str:
    """Keep ``preferred``; if it is taken, the next free colour in palette order.

    When every colour in the palette is occupied this returns ``preferred``
    unchanged and allows the repeat. That is the only honest answer — there is
    no free colour to give — and it keeps the result a deterministic function of
    the identity rather than of arrival order, so a session that has to share
    shares with the same partner every time.

    This is the one place the rule lives. It used to exist twice, once in the
    launch picker and once inlined in the conversation reservation, which is how
    the two drifted: only one of them treated a palette-foreign colour as
    unoccupied.
    """
    if preferred not in palette:
        return preferred
    occupied = {color for color in occupied_colors if color in palette}
    start = palette.index(preferred)
    for offset in range(len(palette)):
        candidate = palette[(start + offset) % len(palette)]
        if candidate not in occupied:
            return candidate
    return preferred


def launch_color_for(
    shpool_id: str,
    occupied_colors: Iterable[str] = (),
    *,
    palette: Sequence[str],
) -> str:
    digest = hashlib.sha256(f"launch:{shpool_id}".encode("utf-8")).digest()
    return first_free_color(
        palette[digest[0] % len(palette)],
        occupied_colors,
        palette=palette,
    )


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
    free_color: Callable[..., str],
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
            occupied.update(
                item["color"]
                for reservation_key, item in reservations.items()
                if reservation_key != f"conversation:{key}"
            )
            preferred = color_for_session(provider, exact_uuid)
            assert preferred is not None
            color = free_color(preferred, occupied, palette=palette)
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


def reconcile_conversation_colors(
    config: Mapping[str, Any],
    sessions: Sequence[Mapping[str, Any]],
    *,
    config_path: Callable[[], Path],
    state_paths: Callable[[Mapping[str, Any]], Mapping[str, Path]],
    state_lock: Callable[..., Any],
    private_alias_parent: Callable[[Path], None],
    private_alias_document: Callable[..., dict[str, Any]],
    valid_colors: Callable[[Any], dict[str, str]],
    atomic_write_json: Callable[[Path, Any], None],
    color_for_session: Callable[..., str | None],
    free_color: Callable[..., str],
    palette_for: Callable[[str], Sequence[str]],
) -> dict[str, Any]:
    """Separate the live sessions that already share a colour, in one pass.

    Sessions that were already running when a palette changed would otherwise
    keep their collision until each one happened to relaunch. This walks the
    live rows instead and settles all of them at once.

    Running it twice must not shuffle anything a second time, and it cannot,
    by construction rather than by a guard. Rows are settled in a fixed
    ``(provider, uuid)`` order rather than in snapshot order, and each row
    prefers the colour it currently shows. The first pass therefore hands every
    row exactly the colour it will prefer on the second pass, every preference
    is free at the moment it is asked for, and nothing moves. That holds at the
    exhaustion boundary too: rows that had to repeat a colour prefer the
    repeated one next time, find the palette full again, and keep it.

    An override is written only where the palette order moved a row off its own
    identity hash. A row sitting on its hash colour needs no record, so the
    stored set stays a list of deviations rather than a copy of the inventory.
    Nothing is ever deleted here except entries that name a colour outside the
    palette now in force, which are already ignored on read.
    """
    ordered: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in sessions:
        if not isinstance(item, Mapping):
            continue
        provider = str(item.get("provider") or "")
        if provider not in PROVIDERS or not palette_for(provider):
            continue
        identity = item.get("identity")
        raw = identity.get("uuid") if isinstance(identity, Mapping) else None
        exact = valid_uuid(str(raw or ""))
        if not exact or (provider, exact) in seen:
            continue
        seen.add((provider, exact))
        ordered.append((provider, exact))
    ordered.sort()

    path = config_path()
    paths = state_paths(config)
    with state_lock(paths["root"], paths["config_lock"]):
        with state_lock(paths["root"], paths["lock"]):
            private_alias_parent(path)
            document = private_alias_document(path, allow_missing=True)
            raw_colors = document.get("colors")
            before = dict(raw_colors) if isinstance(raw_colors, Mapping) else {}
            colors = valid_colors(raw_colors)
            dropped = sorted(
                key for key in before if key not in colors or before[key] != colors[key]
            )
            taken: dict[str, set[str]] = {}
            moved: dict[str, str] = {}
            for provider, exact in ordered:
                key = f"{provider}:{exact}"
                preferred = color_for_session(provider, exact, colors)
                hashed = color_for_session(provider, exact, {})
                if preferred is None or hashed is None:
                    continue
                held = taken.setdefault(provider, set())
                color = free_color(preferred, held, palette=palette_for(provider))
                held.add(color)
                if color == hashed and key not in colors:
                    continue
                if colors.get(key) != color:
                    colors[key] = color
                    moved[key] = color
            if before != colors:
                if colors:
                    document["colors"] = dict(sorted(colors.items()))
                else:
                    document.pop("colors", None)
                atomic_write_json(path, document)
            return {
                "moved": dict(sorted(moved.items())),
                "dropped": dropped,
                "colors": dict(sorted(colors.items())),
            }


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
