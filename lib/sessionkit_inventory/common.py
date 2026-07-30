"""Pure shared configuration, validation, and identity helpers."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping
import unicodedata


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
PROVIDERS = ("claude", "codex")
MAX_OPERATIONAL_ID_BYTES = 128
LEGACY_OPERATIONAL_ID_RE = re.compile(r"^main(?:[1-9][0-9]*)?$")
GENERATED_OPERATIONAL_ID_RE = re.compile(
    r"^s[0-9]{8}-[0-9]{6}-[1-9][0-9]*(?:-[1-9][0-9]*)?$"
)


class CollectionError(RuntimeError):
    """The inventory could not obtain a trustworthy shpool snapshot."""


def _positive_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if low <= parsed <= high else default


def _positive_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if low <= parsed <= high else default


def _home(
    *,
    environ: Mapping[str, str],
    home_factory: Callable[[], Path],
) -> Path:
    """Return the configured home while preserving the facade's eager fallback."""
    return Path(environ.get("HOME", str(home_factory()))).expanduser()


def _xdg_path(
    env_name: str,
    fallback: Path,
    *,
    environ: Mapping[str, str],
) -> Path:
    value = environ.get(env_name)
    return Path(value).expanduser() if value else fallback


def config_path(
    *,
    environ: Mapping[str, str],
    home: Callable[[], Path],
    xdg_path: Callable[[str, Path], Path],
) -> Path:
    explicit = environ.get("SESSION_KIT_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return xdg_path("XDG_CONFIG_HOME", home() / ".config") / "session-kit" / "inventory.json"


def default_state_dir(
    *,
    environ: Mapping[str, str],
    home: Callable[[], Path],
    xdg_path: Callable[[str, Path], Path],
) -> Path:
    explicit = environ.get("SESSION_KIT_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return xdg_path("XDG_STATE_HOME", home() / ".local" / "state") / "session-kit"


def default_journal_dir(
    *,
    environ: Mapping[str, str],
    home: Callable[[], Path],
    xdg_path: Callable[[str, Path], Path],
) -> Path:
    explicit = environ.get("SESSION_KIT_JOURNAL_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return xdg_path("XDG_STATE_HOME", home() / ".local" / "state") / "shpool-journal"


def default_journal_recovery_dir(
    *,
    environ: Mapping[str, str],
    home: Callable[[], Path],
    xdg_path: Callable[[str, Path], Path],
) -> Path:
    explicit = environ.get("SESSION_KIT_JOURNAL_RECOVERY_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return (
        xdg_path("XDG_STATE_HOME", home() / ".local" / "state")
        / "shpool-journal-recovery"
    )


def default_start_dir(
    *,
    environ: Mapping[str, str],
    home: Callable[[], Path],
    xdg_path: Callable[[str, Path], Path],
) -> Path:
    explicit = environ.get("SESSION_KIT_START_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return xdg_path("XDG_STATE_HOME", home() / ".local" / "state") / "shpool-start"


def load_config(
    *,
    config_path: Callable[[], Path],
    load_json_file: Callable[[Path], Any],
    default_state_dir: Callable[[], Path],
    positive_float: Callable[[Any, float, float, float], float],
    positive_int: Callable[[Any, int, int, int], int],
    valid_aliases: Callable[[Any], dict[str, str]],
    valid_automatic_titles: Callable[[Any], dict[str, str]],
    valid_automatic_title_failures: Callable[[Any], dict[str, int]],
    schema_version: int,
    default_max_proc_nodes: int,
) -> dict[str, Any]:
    """Load and validate configuration through facade-owned dependencies."""
    raw: Any = {}
    path = config_path()
    if path.is_file():
        try:
            raw = load_json_file(path)
        except (OSError, ValueError) as exc:
            raise CollectionError(f"invalid config {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise CollectionError(f"invalid config {path}: top level must be an object")
    if raw.get("schema_version", schema_version) != schema_version:
        raise CollectionError(f"unsupported config schema_version in {path}")
    configured_state = raw.get("state_dir")
    state_dir = (
        Path(configured_state).expanduser()
        if isinstance(configured_state, str) and configured_state
        else default_state_dir()
    )
    return {
        "schema_version": schema_version,
        "state_dir": state_dir,
        "command_timeout_seconds": positive_float(
            raw.get("command_timeout_seconds"), 6.0, 0.2, 60.0
        ),
        "max_proc_nodes": positive_int(
            raw.get("max_proc_nodes"), default_max_proc_nodes, 64, 100000
        ),
        "max_proc_depth": positive_int(raw.get("max_proc_depth"), 32, 2, 128),
        "aliases": valid_aliases(raw.get("aliases")),
        "automatic_titles": valid_automatic_titles(raw.get("automatic_titles")),
        "automatic_title_failures": valid_automatic_title_failures(
            raw.get("automatic_title_failures")
        ),
    }


def clean_text(value: Any, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    # Source metadata can contain terminal controls. Replace all Unicode
    # control/format/surrogate/private-use
    # characters, including ESC/CSI/OSC introducers, before whitespace folding.
    safe = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in value
    )
    text = " ".join(safe.split())
    return text[:limit]


def valid_uuid(value: Any) -> str | None:
    if isinstance(value, str) and UUID_RE.fullmatch(value):
        return value.lower()
    return None


def automatic_naming_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return false only for the explicit automatic-name kill switch."""
    values = environ if environ is not None else os.environ
    value = values.get("SESSION_KIT_AUTO_NAME")
    return value is None or value.strip().casefold() not in {"0", "false", "no", "off"}


def normalize_automatic_title(value: Any) -> str:
    """Return a strict, task-focused 2-5 word title without provider prefixes."""
    if not isinstance(value, str):
        raise CollectionError("automatic title must be text")
    safe = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in value
    )
    title = " ".join(safe.split())
    if len(title) > 60:
        raise CollectionError("automatic title must be at most 60 characters")
    words = title.split()
    if not 2 <= len(words) <= 5:
        raise CollectionError("automatic title must contain 2-5 words")
    if words[0].rstrip(":").casefold() in PROVIDERS:
        raise CollectionError("automatic title must not start with a provider name")
    if any(
        not any(character.isalnum() for character in word)
        or any(character in "/\\|[]{}<>" for character in word)
        for word in words
    ):
        raise CollectionError("automatic title contains unsupported punctuation")
    for word in words:
        first = next((character for character in word if character.isalpha()), None)
        if first is not None and not first.isupper():
            raise CollectionError("automatic title must use Title Case")
    return title


def natural_name_key(name: str) -> tuple[Any, ...]:
    """Natural, deterministic ordering for names such as main, main2, main10."""
    parts = re.split(r"(\d+)", name.casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def shpool_id_mutation_policy(raw: Any) -> tuple[bool, str | None]:
    if not isinstance(raw, str) or not raw:
        return False, "invalid"
    if any(unicodedata.category(character).startswith("C") for character in raw):
        return False, "control"
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError:
        return False, "invalid"
    if len(encoded) > MAX_OPERATIONAL_ID_BYTES:
        return False, "oversize"
    lowered = raw.casefold()
    if "template" in lowered or lowered in {"unmanaged", "control"}:
        return False, "template" if "template" in lowered else "unmanaged"
    if LEGACY_OPERATIONAL_ID_RE.fullmatch(raw) or GENERATED_OPERATIONAL_ID_RE.fullmatch(
        raw
    ):
        return True, None
    return False, "unmanaged"
