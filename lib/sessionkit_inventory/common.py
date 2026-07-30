"""Pure shared validation and identity helpers for Session Kit inventory."""

from __future__ import annotations

import os
import re
from typing import Any, Mapping
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
