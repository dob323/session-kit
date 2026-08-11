"""Pure shared configuration, validation, and identity helpers."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence
import unicodedata


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
DEFAULT_PROC_ROOT = Path("/proc")
PROVIDERS = ("claude", "codex")
# Who a session's name belongs to. "human" is permanent; "automatic" is the
# first-prompt claim one automatic pass takes and later passes respect.
NAME_OWNERS = ("human", "automatic")
# The one selection grammar, and the one sentence that describes it.
ASCII_DIGITS_RE = re.compile(r"[0-9]+")
MAX_SELECTION_SPAN = 200
SELECTION_GRAMMAR = "use numbers such as 1,3,5, ranges such as 2-4, or all"
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


def _load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


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
    return (
        xdg_path("XDG_CONFIG_HOME", home() / ".config")
        / "session-kit"
        / "inventory.json"
    )


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


def _valid_aliases(raw: Any) -> dict[str, str]:
    aliases: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return aliases
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        provider, separator, uuid = key.partition(":")
        title = clean_text(value, 100)
        if separator and provider in PROVIDERS and UUID_RE.fullmatch(uuid) and title:
            aliases[f"{provider}:{uuid.lower()}"] = title
    return aliases


def _valid_automatic_titles(raw: Any) -> dict[str, str]:
    """Validate retained, provider/UUID-bound automatic display titles."""
    titles: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return titles
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        provider, separator, uuid = key.partition(":")
        try:
            title = normalize_automatic_title(value)
        except CollectionError:
            continue
        if separator and provider in PROVIDERS and valid_uuid(uuid):
            titles[f"{provider}:{uuid.lower()}"] = title
    return titles


def _valid_automatic_title_failures(raw: Any) -> dict[str, int]:
    failures: dict[str, int] = {}
    if not isinstance(raw, Mapping):
        return failures
    for key, value in raw.items():
        provider, separator, uuid = (
            key.partition(":") if isinstance(key, str) else ("", "", "")
        )
        if (
            separator
            and provider in PROVIDERS
            and valid_uuid(uuid)
            and not isinstance(value, bool)
            and isinstance(value, int)
            and 0 < value <= 2
        ):
            failures[f"{provider}:{uuid.lower()}"] = value
    return failures


def parse_number_selection(answer: Any, count: int) -> list[int]:
    """One grammar for every place a person types session numbers.

    Accepts ``all`` (and its ``a`` shorthand), comma- or space-separated
    numbers, and inclusive ranges written ``2-5``, in any mixture. Returns the
    chosen numbers in ascending order with duplicates collapsed; an empty
    answer chooses nothing. Anything else raises with the one sentence every
    surface says, so a person who learns the grammar in one place has learnt
    it everywhere.

    Only ASCII digits count. Unicode digits pass ``str.isdigit()`` and then
    either mean something surprising or raise inside ``int()``, and a picker
    is no place for a traceback.
    """
    if not isinstance(answer, str):
        raise CollectionError(SELECTION_GRAMMAR)
    text = answer.strip().casefold()
    if not text:
        return []
    if text in {"a", "all"}:
        return list(range(1, count + 1))
    picked: set[int] = set()
    for chunk in text.split(","):
        # An empty entry between commas is a typo, not an empty choice.
        parts = chunk.split()
        if not parts:
            raise CollectionError(SELECTION_GRAMMAR)
        for part in parts:
            first, separator, last = part.partition("-")
            if separator:
                if not (
                    ASCII_DIGITS_RE.fullmatch(first) and ASCII_DIGITS_RE.fullmatch(last)
                ):
                    raise CollectionError(SELECTION_GRAMMAR)
                low, high = int(first), int(last)
                if low > high:
                    raise CollectionError(SELECTION_GRAMMAR)
                if high - low >= MAX_SELECTION_SPAN:
                    # Refuse before building the set: a mistyped 1-99999999
                    # must cost a sentence, not the memory of the machine.
                    raise CollectionError(
                        f"a range covers at most {MAX_SELECTION_SPAN} numbers"
                    )
                picked.update(range(low, high + 1))
            elif ASCII_DIGITS_RE.fullmatch(part):
                picked.add(int(part))
            else:
                raise CollectionError(SELECTION_GRAMMAR)
    if not picked:
        raise CollectionError(SELECTION_GRAMMAR)
    if any(number < 1 or number > count for number in picked):
        raise CollectionError(f"choose between 1 and {count}")
    return sorted(picked)


def _valid_pushed_titles(raw: Any) -> dict[str, str]:
    """The exact title the kit last wrote into each provider's own store.

    This is the discriminator for a provider-native human rename. Session Kit
    pushes its own titles into Claude's transcript records and Codex's thread
    table, so "the native store disagrees with us" means nothing on its own —
    half the time the kit put that value there. A native title that differs
    from the last value the kit itself pushed is somebody typing /rename.
    """
    titles: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return titles
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        provider, separator, uuid = key.partition(":")
        title = clean_text(value, 120)
        if separator and provider in PROVIDERS and valid_uuid(uuid) and title:
            titles[f"{provider}:{uuid.lower()}"] = title
    return titles


def _valid_name_ownership(raw: Any) -> dict[str, dict[str, str]]:
    """Validate the durable record of who named each session.

    ``human`` is an override: a person renamed the session through `sp name`
    or the picker, and no automatic pass may ever rename it again — not after
    a `sp name reset`, not after a restart. ``automatic`` is the one-shot
    claim taken at a thread's first prompt, so no second automatic pass
    re-titles work a first pass already named.
    """
    owners: dict[str, dict[str, str]] = {}
    if not isinstance(raw, Mapping):
        return owners
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            continue
        provider, separator, uuid = key.partition(":")
        owner = value.get("owner")
        if (
            separator
            and provider in PROVIDERS
            and valid_uuid(uuid)
            and owner in NAME_OWNERS
        ):
            owners[f"{provider}:{uuid.lower()}"] = {
                "owner": str(owner),
                "at": clean_text(value.get("at"), 40),
            }
    return owners


def _valid_colors(
    raw: Any,
    *,
    palette_for: Callable[[str], Sequence[str]],
) -> dict[str, str]:
    """Validate provider/UUID-bound colors against each provider's own palette.

    The key carries the provider, so every entry is checked against the palette
    that provider can actually display rather than against the union. An entry
    naming a colour outside its provider's palette is dropped, not corrected:
    that is what silently migrates a stored override left behind by an earlier
    palette, since the caller then falls through to the identity hash and lands
    inside the palette now in force.
    """
    colors: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return colors
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        provider, separator, uuid = key.partition(":")
        if (
            separator
            and provider in PROVIDERS
            and UUID_RE.fullmatch(uuid)
            and value in palette_for(provider)
        ):
            colors[f"{provider}:{uuid.lower()}"] = value
    return colors


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


def proc_root(environ: Mapping[str, str] | None = None) -> Path:
    """Return the process table root, honouring the fixture root only in tests.

    ``/proc`` is where every identity decision in the kit ultimately gets its
    answer: which PIDs exist, when each started, what each is running, who its
    parent is. ``SESSION_KIT_PROC_ROOT`` points that at a directory tree the
    caller controls, which a test needs and production must never accept --
    an attacker who can set one environment variable would otherwise be able
    to hand-author the process table the kit proves session identity against.
    The ``SESSION_KIT_TESTING`` gate is applied here so no reader can forget
    it, and every reader in the tree goes through this function.
    """
    values = environ if environ is not None else os.environ
    override = values.get("SESSION_KIT_PROC_ROOT")
    if override and values.get("SESSION_KIT_TESTING") == "1":
        return Path(override)
    return DEFAULT_PROC_ROOT


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


def display_shpool_id(raw: str, limit: int = 32) -> str:
    display_source = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in raw
    )
    visible = clean_text(display_source, 10000)
    if not visible:
        visible = "(non-printing ID)"
    return visible if len(visible) <= limit else f"{visible[: limit - 1]}…"


def _utc_now(now: float | None = None) -> str:
    instant = dt.datetime.fromtimestamp(
        now if now is not None else time.time(), dt.timezone.utc
    )
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


def _command_from_env(
    env_name: str,
    default: str,
    *,
    environ: Mapping[str, str],
) -> list[str]:
    value = environ.get(env_name)
    if value:
        command = shlex.split(value)
        if command:
            return command
    return [default]


def default_runner(argv: Sequence[str], timeout: float) -> str:
    completed = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = clean_text(completed.stderr, 240) or f"exit {completed.returncode}"
        raise CollectionError(f"{shlex.join(argv)} failed: {detail}")
    return completed.stdout


def _command_json(
    *,
    fixture_env: str,
    command_env: str,
    default_command: Sequence[str],
    runner: Callable[[Sequence[str], float], str],
    timeout: float,
    environ: Mapping[str, str],
    load_json_file: Callable[[Path], Any],
    command_from_env: Callable[[str, str], list[str]],
) -> Any:
    """Read one provider snapshot from a fixture or the configured command.

    The fixture variable replaces the provider's own answer with a file of the
    caller's choosing, and that answer is what the inventory treats as the
    truth about which sessions exist. Outside the isolated test harness it is
    ignored, so no environment variable can substitute a fabricated roster for
    what shpool and the provider CLIs actually report. ``command_env`` stays
    ungated: pointing at a differently installed binary is real configuration.
    """
    fixture = environ.get(fixture_env)
    if fixture and environ.get("SESSION_KIT_TESTING") == "1":
        return load_json_file(Path(fixture).expanduser())
    prefix = command_from_env(command_env, default_command[0])
    return json.loads(runner([*prefix, *default_command[1:]], timeout))
