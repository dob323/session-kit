"""Read and validate a project's committable ``session-kit.toml`` manifest.

A manifest is repository content: it travels with a clone, it is written by
whoever can push to that repository, and it names things that decide what
Session Kit launches. It is therefore parsed as untrusted input, and the
fields that change a launch are applied only for a project the host has
deliberately added (see :mod:`lib.sessionkit_projects.identity`).

The file is parsed by one hand-written reader for a strict TOML subset on
every supported Python, rather than by ``tomllib`` where it exists and a
fallback where it does not. Two parsers mean a manifest that means one thing
on Python 3.11 and another on 3.10; one parser cannot. The subset is the
documented contract, and ``tests/test_projects_manifest.py`` asserts that
``tomllib`` reads the same fixtures to the same values wherever it is
available.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable, Mapping

MANIFEST_NAME = "session-kit.toml"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_MANIFEST_LINES = 2000
MAX_TEAM_ROLES = 32
MAX_TEXT = 2000

PROVIDERS = ("claude", "codex", "shell")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
ACCOUNT_RE = re.compile(r"[a-z][a-z0-9_-]{0,11}")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
ROLE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

# The keys a manifest may set. An unknown key is refused by name rather than
# ignored: a typo in `provider` that silently launched the default would be
# indistinguishable from the manifest working.
TOP_LEVEL_KEYS = (
    "name",
    "root",
    "description",
    "provider",
    "account",
    "model",
    "startup",
)
TEAM_KEYS = (
    "role",
    "provider",
    "account",
    "model",
    "branch",
    "expertise",
    "scope",
    "workstream",
    "rationale",
)

_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


class ManifestError(ValueError):
    """A manifest is unreadable, malformed, or outside the supported subset."""


def _fail(line_number: int, problem: str) -> "ManifestError":
    return ManifestError(f"{MANIFEST_NAME} line {line_number}: {problem}")


# ---- the strict TOML subset --------------------------------------------


def _basic_string(raw: str, line_number: int) -> tuple[str, str]:
    index = 1
    out: list[str] = []
    while index < len(raw):
        char = raw[index]
        if char == '"':
            return "".join(out), raw[index + 1 :]
        if char == "\\":
            index += 1
            if index >= len(raw):
                break
            escape = raw[index]
            if escape in _ESCAPES:
                out.append(_ESCAPES[escape])
                index += 1
                continue
            if escape == "u":
                digits = raw[index + 1 : index + 5]
                if len(digits) != 4 or not re.fullmatch(r"[0-9A-Fa-f]{4}", digits):
                    raise _fail(line_number, "a \\u escape needs four hex digits")
                code = int(digits, 16)
                if 0xD800 <= code <= 0xDFFF:
                    raise _fail(line_number, "a \\u escape may not name a surrogate")
                out.append(chr(code))
                index += 5
                continue
            raise _fail(line_number, f"unsupported string escape \\{escape}")
        if char in "\n\r":
            break
        out.append(char)
        index += 1
    raise _fail(line_number, "a string is missing its closing quote")


def _literal_string(raw: str, line_number: int) -> tuple[str, str]:
    end = raw.find("'", 1)
    if end == -1:
        raise _fail(line_number, "a string is missing its closing quote")
    return raw[1:end], raw[end + 1 :]


def _scalar(raw: str, line_number: int) -> tuple[Any, str]:
    raw = raw.lstrip()
    if not raw:
        raise _fail(line_number, "a key needs a value")
    if raw.startswith('"""') or raw.startswith("'''"):
        raise _fail(line_number, "multi-line strings are not supported")
    if raw.startswith('"'):
        return _basic_string(raw, line_number)
    if raw.startswith("'"):
        return _literal_string(raw, line_number)
    match = re.match(r"(true|false)(?![A-Za-z0-9_-])", raw)
    if match:
        return match.group(1) == "true", raw[match.end() :]
    match = re.match(r"[+-]?[0-9](?:_?[0-9])*(?![A-Za-z0-9._+-])", raw)
    if match:
        return int(match.group(0).replace("_", "")), raw[match.end() :]
    raise _fail(
        line_number,
        "only strings, integers, booleans, and arrays of those are supported",
    )


def _array(raw: str, line_number: int) -> tuple[list[Any], str]:
    rest = raw[1:]
    items: list[Any] = []
    while True:
        rest = rest.lstrip()
        if not rest:
            raise _fail(line_number, "an array must open and close on one line")
        if rest.startswith("]"):
            return items, rest[1:]
        if rest.startswith("["):
            raise _fail(line_number, "nested arrays are not supported")
        value, rest = _scalar(rest, line_number)
        items.append(value)
        rest = rest.lstrip()
        if rest.startswith(","):
            rest = rest[1:]
            continue
        if rest.startswith("]"):
            return items, rest[1:]
        raise _fail(line_number, "array items are separated by commas")


def _value(raw: str, line_number: int) -> Any:
    raw = raw.lstrip()
    if raw.startswith("["):
        value, rest = _array(raw, line_number)
    elif raw.startswith("{"):
        raise _fail(line_number, "inline tables are not supported")
    else:
        value, rest = _scalar(raw, line_number)
    rest = rest.lstrip()
    if rest and not rest.startswith("#"):
        raise _fail(line_number, "unexpected text after a value")
    return value


def parse_subset(text: str) -> dict[str, Any]:
    """Parse the manifest subset: top-level pairs and ``[[team]]`` tables.

    Anything outside the subset raises with its line number. A manifest is a
    handful of settings; refusing what this does not understand is what keeps
    the meaning of a file identical on every Python the kit supports.
    """
    lines = text.splitlines()
    if len(lines) > MAX_MANIFEST_LINES:
        raise ManifestError(
            f"{MANIFEST_NAME} is longer than {MAX_MANIFEST_LINES} lines"
        )
    top: dict[str, Any] = {}
    team: list[dict[str, Any]] = []
    # As in TOML, a key after a [[team]] header belongs to that team table,
    # not to the project. The documented layout puts project settings first
    # for exactly that reason.
    current: dict[str, Any] = top
    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            if stripped.startswith("[["):
                header = re.fullmatch(
                    r"\[\[\s*([A-Za-z0-9_-]+)\s*\]\](?:\s*#.*)?", stripped
                )
                if not header:
                    raise _fail(line_number, "malformed array-of-tables header")
                if header.group(1) != "team":
                    raise _fail(
                        line_number,
                        f"unknown section [[{header.group(1)}]]; only [[team]] exists",
                    )
                if len(team) >= MAX_TEAM_ROLES:
                    raise ManifestError(
                        f"{MANIFEST_NAME} declares more than {MAX_TEAM_ROLES} team roles"
                    )
                current = {}
                team.append(current)
                continue
            header = re.fullmatch(r"\[\s*([A-Za-z0-9_.-]+)\s*\](?:\s*#.*)?", stripped)
            if not header:
                raise _fail(line_number, "malformed table header")
            raise _fail(
                line_number,
                f"unknown section [{header.group(1)}]; settings are top level "
                "and roles are [[team]]",
            )
        key_part, separator, value_part = line.partition("=")
        if not separator:
            raise _fail(line_number, "expected key = value")
        key = key_part.strip()
        if key.startswith(('"', "'")):
            raise _fail(line_number, "quoted keys are not supported")
        if not _BARE_KEY_RE.fullmatch(key):
            raise _fail(line_number, f"unsupported key {key!r}")
        if key in current:
            raise _fail(line_number, f"{key} is set twice")
        current[key] = _value(value_part, line_number)
    if team:
        top["team"] = team
    return top


# ---- validation ---------------------------------------------------------


def _string(
    values: Mapping[str, Any],
    key: str,
    *,
    pattern: re.Pattern[str] | None = None,
    limit: int = MAX_TEXT,
    label: str,
) -> str | None:
    if key not in values:
        return None
    raw = values[key]
    if not isinstance(raw, str):
        raise ManifestError(f"{label} must be text")
    value = raw.strip()
    if not value:
        return None
    if len(value) > limit:
        raise ManifestError(f"{label} is longer than {limit} characters")
    if any(char < " " or char == "\x7f" for char in value):
        raise ManifestError(f"{label} may not contain control characters")
    if pattern is not None and not pattern.fullmatch(value):
        raise ManifestError(f"{label} is not in the accepted form: {value!r}")
    return value


def _unknown_keys(values: Iterable[str], allowed: Iterable[str], label: str) -> None:
    extra = sorted(set(values) - set(allowed))
    if extra:
        raise ManifestError(f"{label} has unknown key(s): {', '.join(extra)}")


def _path_parts(raw: str) -> list[str]:
    return raw.replace("\\", "/").split("/")


def _relative_root(raw: str | None) -> str:
    """The manifest may point at a subdirectory of itself, never outside it.

    A manifest that could name an absolute path, or climb with ``..``, would
    let a cloned repository redirect a launch into an unrelated directory.
    """
    if raw is None:
        return "."
    if raw.startswith("/") or raw.startswith("~"):
        raise ManifestError("root must be relative to the manifest, not absolute")
    parts = [part for part in _path_parts(raw) if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ManifestError("root may not climb above the manifest with ..")
    return "/".join(parts) or "."


def _team_role(values: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(values, Mapping):
        raise ManifestError(f"team role {index} must be a table")
    _unknown_keys(values.keys(), TEAM_KEYS, f"team role {index}")
    role = _string(
        values, "role", pattern=ROLE_RE, limit=32, label=f"team role {index} role"
    )
    if not role:
        raise ManifestError(f"team role {index} needs a role name")
    provider = _string(
        values, "provider", limit=16, label=f"team role {index} provider"
    )
    if provider is not None and provider not in ("claude", "codex"):
        raise ManifestError(f"team role {index} provider must be claude or codex")
    return {
        "role": role,
        "provider": provider,
        "account": _string(
            values,
            "account",
            pattern=ACCOUNT_RE,
            limit=12,
            label=f"team role {index} account",
        ),
        "model": _string(
            values,
            "model",
            pattern=MODEL_RE,
            limit=128,
            label=f"team role {index} model",
        ),
        "branch": _string(
            values,
            "branch",
            pattern=BRANCH_RE,
            limit=128,
            label=f"team role {index} branch",
        ),
        "expertise": _string(
            values, "expertise", limit=64, label=f"team role {index} expertise"
        ),
        "workstream": _string(
            values, "workstream", limit=500, label=f"team role {index} workstream"
        ),
        "scope": _string(values, "scope", limit=2000, label=f"team role {index} scope"),
        "rationale": _string(
            values, "rationale", limit=1000, label=f"team role {index} rationale"
        ),
    }


def validate(values: Mapping[str, Any]) -> dict[str, Any]:
    """The exact stored shape of a manifest, or a refusal naming the field."""
    if not isinstance(values, Mapping):
        raise ManifestError("a manifest must be a table")
    _unknown_keys(
        (key for key in values if key != "team"), TOP_LEVEL_KEYS, MANIFEST_NAME
    )
    raw_team = values.get("team", [])
    if not isinstance(raw_team, list):
        raise ManifestError("team roles are declared with [[team]] sections")
    provider = _string(values, "provider", limit=16, label="provider")
    if provider is not None and provider not in PROVIDERS:
        raise ManifestError("provider must be claude, codex, or shell")
    roles = [_team_role(role, index) for index, role in enumerate(raw_team, 1)]
    names = [role["role"] for role in roles]
    if len(set(names)) != len(names):
        raise ManifestError("two team roles share one role name")
    return {
        "name": _string(values, "name", pattern=NAME_RE, limit=48, label="name"),
        "root": _relative_root(_string(values, "root", limit=512, label="root")),
        "description": _string(values, "description", limit=200, label="description"),
        "provider": provider,
        "account": _string(
            values, "account", pattern=ACCOUNT_RE, limit=12, label="account"
        ),
        "model": _string(values, "model", pattern=MODEL_RE, limit=128, label="model"),
        "startup": _string(values, "startup", limit=MAX_TEXT, label="startup"),
        "team": roles,
    }


def loads(text: str) -> dict[str, Any]:
    """Parse and validate manifest text."""
    return validate(parse_subset(text))


def read(path: Path) -> dict[str, Any]:
    """Read one manifest file.

    The file is repository content, so its mode and owner are whatever the
    checkout has; what is bounded here is size and shape. A symlink is
    refused because a manifest that points elsewhere is not the committed
    file a reviewer of that repository saw.
    """
    try:
        if path.is_symlink():
            raise ManifestError(f"{path} is a symlink, not a committed manifest")
        info = path.stat()
    except OSError as error:
        raise ManifestError(f"{path} is unreadable: {error.strerror}") from error
    if info.st_size > MAX_MANIFEST_BYTES:
        raise ManifestError(f"{path} is larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ManifestError(f"{path} is unreadable: {error.strerror}") from error
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise ManifestError(f"{path} is not valid UTF-8") from error
    if "\x00" in text:
        raise ManifestError(f"{path} contains a null byte")
    return loads(text)
