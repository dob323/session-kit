"""Owner-only provider account profiles and exact-thread switch transactions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping
import uuid as uuidlib

from .common import CollectionError, clean_text, valid_uuid
from .state_io import StateLock, ensure_private_directory, read_private_json


ACCOUNT_SCHEMA_VERSION = 1
PROFILE_ALIAS_RE = re.compile(r"[a-z][a-z0-9_-]{0,11}")
PROFILE_KEY_RE = re.compile(r"(claude|codex):([a-z][a-z0-9_-]{0,11})")
DEFAULT_ADVICE_MAX_AGE_SECONDS = 600
MAX_REGISTRY_BYTES = 1024 * 1024
# Claude's own `.claude.json` is not a kit state file and does not obey a kit
# size budget: the provider keeps one entry per project directory ever opened,
# so it grows with the operator's history and never shrinks. Reading it under
# the 1 MiB registry cap meant that crossing that line would silently stop the
# first-run marker from being copied -- and the symptom of that is exactly the
# bug this marker exists to fix, coming back years later with no explanation
# (lane B F8). Measured 2026-08-15: the default profile is 90,786 bytes and the
# two enrolled profiles 98,594 and 48,814, so this is headroom rather than a
# fix for today. The cap still exists, because an unbounded read of a file this
# code did not write is not acceptable either -- it is just set where a real
# provider file will not reach it, and passing it is now reported, never
# silent.
MAX_CLAUDE_STATE_BYTES = 64 * 1024 * 1024
MAX_TRANSACTION_BYTES = 256 * 1024
MAX_FEED_CONFIG_BYTES = 16 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
PROVIDERS = frozenset({"claude", "codex"})
# One rulebook, every profile. Each provider reads its own instruction file, and
# each enrolled profile keeps a private copy of it, so an instruction added in
# one place used to reach only that one place. The canonical file is rendered
# into every rulebook between these markers; anything outside them is that
# provider's or that profile's own text and is preserved untouched.
RULES_BEGIN = "<!-- BEGIN UNIVERSAL RULES"
RULES_END = "<!-- END UNIVERSAL RULES -->"
RULES_BLOCK_RE = re.compile(
    re.escape(RULES_BEGIN) + r".*?" + re.escape(RULES_END), re.DOTALL
)
RULES_FILENAME = {"claude": "CLAUDE.md", "codex": "AGENTS.md"}
MAX_RULES_BYTES = 512 * 1024


def _now_ms() -> int:
    return int(time.time() * 1000)


def _profile_key(provider: str, alias: str) -> str:
    if provider not in PROVIDERS or not PROFILE_ALIAS_RE.fullmatch(alias):
        raise CollectionError("account provider or alias is invalid")
    return f"{provider}:{alias}"


def registry_path(config: Mapping[str, Any]) -> Path:
    override = os.environ.get("SESSION_KIT_ACCOUNT_REGISTRY")
    if override:
        path = Path(override)
    else:
        path = Path(str(config["state_dir"])) / "accounts.json"
    if not path.is_absolute():
        raise CollectionError("account registry path must be absolute")
    return path


def account_root(config: Mapping[str, Any]) -> Path:
    override = os.environ.get("SESSION_KIT_ACCOUNT_ROOT")
    if override:
        root = Path(override)
    else:
        data_home = Path(
            os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        )
        root = data_home / "session-kit" / "accounts"
    if not root.is_absolute():
        raise CollectionError("account profile root must be absolute")
    return root


def kill_switch_path(config: Mapping[str, Any]) -> Path:
    return Path(str(config["state_dir"])) / "account-switching-off"


def feed_config_path(config: Mapping[str, Any]) -> Path:
    return Path(str(config["state_dir"])) / "account-feeds.json"


def configure_feeds(
    config: Mapping[str, Any], roster_path: str, advice_path: str
) -> dict[str, Any]:
    value = {
        "schema_version": ACCOUNT_SCHEMA_VERSION,
        "roster_path": roster_path,
        "advice_path": advice_path,
    }
    for key in ("roster_path", "advice_path"):
        path = Path(str(value[key]))
        if not path.is_absolute() or "\n" in str(path) or "\r" in str(path):
            raise CollectionError("account feed path must be absolute")
    _atomic_private_json(feed_config_path(config), value)
    return value


def _feed_paths(config: Mapping[str, Any]) -> tuple[Path, Path]:
    raw = read_private_json(
        feed_config_path(config),
        max_bytes=MAX_FEED_CONFIG_BYTES,
        allow_missing=True,
    )
    if raw is None:
        root = Path(str(config["state_dir"]))
        return root / "account-roster.json", root / "account-advice.json"
    if (
        not isinstance(raw, Mapping)
        or raw.get("schema_version") != ACCOUNT_SCHEMA_VERSION
    ):
        raise CollectionError("account feed configuration is invalid")
    paths = tuple(
        Path(str(raw.get(key) or "")) for key in ("roster_path", "advice_path")
    )
    if any(not path.is_absolute() for path in paths):
        raise CollectionError("account feed configuration is invalid")
    return paths[0], paths[1]


def _empty_registry() -> dict[str, Any]:
    return {
        "schema_version": ACCOUNT_SCHEMA_VERSION,
        "generation": 0,
        "profiles": {},
        "bindings": {},
    }


def _safe_profile_path(
    provider: str,
    alias: str,
    raw: Any,
    *,
    config: Mapping[str, Any],
    legacy: bool,
) -> Path:
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise CollectionError("account profile path is invalid")
    path = Path(raw)
    if legacy:
        expected = Path.home() / (".claude" if provider == "claude" else ".codex")
        if path != expected:
            raise CollectionError("legacy account profile is not the provider default")
    else:
        expected = account_root(config) / provider / alias
        if path != expected:
            raise CollectionError("account profile escaped its owner-only root")
    return path


def _validate_registry(raw: Any, config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CollectionError("account registry is not an object")
    if raw.get("schema_version") != ACCOUNT_SCHEMA_VERSION:
        raise CollectionError("account registry schema is unsupported")
    generation = raw.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
    ):
        raise CollectionError("account registry generation is invalid")
    profiles_raw = raw.get("profiles")
    bindings_raw = raw.get("bindings")
    if not isinstance(profiles_raw, Mapping) or not isinstance(bindings_raw, Mapping):
        raise CollectionError("account registry collections are invalid")
    profiles: dict[str, dict[str, Any]] = {}
    for key, value in profiles_raw.items():
        match = PROFILE_KEY_RE.fullmatch(str(key))
        if not match or not isinstance(value, Mapping):
            raise CollectionError("account registry profile is invalid")
        provider, alias = match.groups()
        if value.get("provider") != provider or value.get("alias") != alias:
            raise CollectionError("account profile identity does not match its key")
        email = clean_text(value.get("email"), 254).casefold()
        if "@" not in email or any(character.isspace() for character in email):
            raise CollectionError("account profile email is invalid")
        legacy = value.get("legacy") is True
        path = _safe_profile_path(
            provider, alias, value.get("profile_dir"), config=config, legacy=legacy
        )
        verified_at = value.get("verified_at_unix_ms")
        if (
            not isinstance(verified_at, int)
            or isinstance(verified_at, bool)
            or verified_at <= 0
        ):
            raise CollectionError("account profile verification time is invalid")
        profiles[str(key)] = {
            "provider": provider,
            "alias": alias,
            "email": email,
            "profile_dir": os.fspath(path),
            "legacy": legacy,
            "plan": clean_text(value.get("plan"), 80),
            "verified_at_unix_ms": verified_at,
            "enabled": value.get("enabled") is not False,
        }
    bindings: dict[str, dict[str, Any]] = {}
    for thread_key, value in bindings_raw.items():
        if not isinstance(thread_key, str) or not isinstance(value, Mapping):
            raise CollectionError("account binding is invalid")
        parts = thread_key.split(":", 1)
        exact = valid_uuid(parts[1]) if len(parts) == 2 else None
        profile_key = value.get("profile")
        if (
            parts[0] not in PROVIDERS
            or not exact
            or not isinstance(profile_key, str)
            or profile_key not in profiles
            or not profile_key.startswith(parts[0] + ":")
        ):
            raise CollectionError("account binding identity is invalid")
        bound_at = value.get("bound_at_unix_ms")
        if not isinstance(bound_at, int) or isinstance(bound_at, bool) or bound_at <= 0:
            raise CollectionError("account binding time is invalid")
        bindings[f"{parts[0]}:{exact}"] = {
            "profile": profile_key,
            "bound_at_unix_ms": bound_at,
            "source": clean_text(value.get("source"), 40) or "verified",
        }
    return {
        "schema_version": ACCOUNT_SCHEMA_VERSION,
        "generation": generation,
        "profiles": profiles,
        "bindings": bindings,
    }


def load_registry(
    config: Mapping[str, Any], *, allow_missing: bool = True
) -> dict[str, Any]:
    path = registry_path(config)
    raw = read_private_json(
        path,
        max_bytes=MAX_REGISTRY_BYTES,
        allow_missing=allow_missing,
    )
    if raw is None:
        return _empty_registry()
    return _validate_registry(raw, config)


def _registry_lock(config: Mapping[str, Any]) -> StateLock:
    root = Path(str(config["state_dir"]))
    return StateLock(root, root / "accounts.lock")


class _FileChangedUnderUs(Exception):
    """Someone else wrote the file between our read and our replace."""


def _file_identity(path: Path) -> tuple[int, int, int, int] | None:
    """(dev, inode, mtime_ns, size), or None when the file is not there.

    Enough to detect either shape of foreign write: an in-place write moves
    mtime_ns and usually size, and a write-to-temp-then-rename moves the inode.
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


_NO_GUARD: Any = object()


def _atomic_private_json(path: Path, value: Any, *, expect: Any = _NO_GUARD) -> None:
    """Durably replace one owner-only JSON file.

    ``expect`` is a compare-and-swap guard: pass the file identity observed
    BEFORE the content was read, and the replace is abandoned -- raising
    ``_FileChangedUnderUs`` -- if the file has moved since. The default
    sentinel means no guard, which is what every caller that owns its file
    outright wants. Only the Claude profile, which the provider also writes,
    passes one.
    """
    ensure_private_directory(path.parent)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # As late as it can possibly be: the whole point is to shrink the gap
        # between "the file is still the one I read" and the replace itself to
        # two adjacent syscalls.
        if expect is not _NO_GUARD and _file_identity(path) != expect:
            raise _FileChangedUnderUs(str(path))
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def write_registry(
    config: Mapping[str, Any], registry: Mapping[str, Any]
) -> dict[str, Any]:
    checked = _validate_registry(registry, config)
    _atomic_private_json(registry_path(config), checked)
    return checked


def _default_profile_dir(provider: str) -> Path:
    return Path.home() / (".claude" if provider == "claude" else ".codex")


def profile(config: Mapping[str, Any], provider: str, alias: str) -> dict[str, Any]:
    registry = load_registry(config)
    value = registry["profiles"].get(_profile_key(provider, alias))
    if not isinstance(value, Mapping) or value.get("enabled") is not True:
        raise CollectionError("account profile is unavailable")
    return dict(value)


def list_profiles(
    config: Mapping[str, Any], provider: str | None = None
) -> list[dict[str, Any]]:
    if provider is not None and provider not in PROVIDERS:
        raise CollectionError("account provider is invalid")
    registry = load_registry(config)
    result = [
        dict(value)
        for value in registry["profiles"].values()
        if provider is None or value["provider"] == provider
    ]
    return sorted(result, key=lambda item: (item["provider"], item["alias"]))


def binding_for(
    config: Mapping[str, Any], provider: str, uuid: str
) -> dict[str, Any] | None:
    exact = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact:
        return None
    registry = load_registry(config)
    binding = registry["bindings"].get(f"{provider}:{exact}")
    if not binding:
        return None
    found = dict(registry["profiles"][binding["profile"]])
    found["binding_source"] = binding["source"]
    found["bound_at_unix_ms"] = binding["bound_at_unix_ms"]
    return found


def source_profile_for_thread(
    config: Mapping[str, Any], provider: str, uuid: str
) -> dict[str, Any]:
    """Return the exact bound source, or uniquely prove a legacy first switch."""
    exact = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact:
        raise CollectionError("account source needs an exact provider UUID")
    current = binding_for(config, provider, exact)
    if current is not None:
        return current
    matches: list[dict[str, Any]] = []
    for item in list_profiles(config, provider):
        try:
            _artifact_paths(provider, Path(item["profile_dir"]), exact)
        except CollectionError:
            continue
        matches.append(item)
    if len(matches) != 1:
        raise CollectionError("legacy account source is missing or ambiguous")
    bind(config, provider, exact, matches[0]["alias"], source="legacy-first-switch")
    found = binding_for(config, provider, exact)
    if found is None:
        raise CollectionError("legacy account source binding could not be recorded")
    return found


def bind(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    alias: str,
    *,
    source: str,
) -> dict[str, Any]:
    exact = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact:
        raise CollectionError("account binding needs an exact provider UUID")
    with _registry_lock(config):
        registry = load_registry(config)
        key = _profile_key(provider, alias)
        if key not in registry["profiles"] or not registry["profiles"][key]["enabled"]:
            raise CollectionError("account binding target is unavailable")
        registry["bindings"][f"{provider}:{exact}"] = {
            "profile": key,
            "bound_at_unix_ms": _now_ms(),
            "source": clean_text(source, 40) or "verified",
        }
        registry["generation"] += 1
        return write_registry(config, registry)


def _provider_environment(provider: str, profile_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    if provider == "claude":
        environment["CLAUDE_CONFIG_DIR"] = os.fspath(profile_dir)
        environment.pop("ANTHROPIC_API_KEY", None)
        environment.pop("ANTHROPIC_AUTH_TOKEN", None)
        environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    elif provider == "codex":
        environment["CODEX_HOME"] = os.fspath(profile_dir)
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("CODEX_ACCESS_TOKEN", None)
    else:
        raise CollectionError("account provider is invalid")
    return environment


def _run_codex_account_read(profile_dir: Path) -> dict[str, Any]:
    requests = (
        json.dumps(
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "session-kit",
                        "title": "Session Kit",
                        "version": "1",
                    },
                    "capabilities": {},
                },
            },
            separators=(",", ":"),
        )
        + "\n"
        + json.dumps({"method": "initialized", "params": {}}, separators=(",", ":"))
        + "\n"
        + json.dumps(
            {"method": "account/read", "id": 1, "params": {"refreshToken": False}},
            separators=(",", ":"),
        )
        + "\n"
    )
    # Codex 0.145+ exits the app-server on stdin EOF before answering, so the
    # probe must keep stdin open until the account/read reply has arrived.
    try:
        process = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=_provider_environment("codex", profile_dir),
        )
    except OSError as exc:
        raise CollectionError("Codex account identity probe did not complete") from exc
    reply: Mapping[str, Any] | None = None
    timed_out = False
    stdin = process.stdin
    stdout = process.stdout
    assert stdin is not None and stdout is not None
    try:
        try:
            stdin.write(requests)
            stdin.flush()
        except OSError as exc:
            raise CollectionError(
                "Codex account identity probe did not complete"
            ) from exc
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([stdout], [], [], remaining)
            if not ready:
                timed_out = True
                break
            line = stdout.readline()
            if not line:
                break
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, Mapping) and value.get("id") == 1:
                reply = value
                break
    finally:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=5)
    if timed_out and reply is None:
        raise CollectionError("Codex account identity probe did not complete")
    if isinstance(reply, Mapping):
        result = reply.get("result")
        account = result.get("account") if isinstance(result, Mapping) else None
        if isinstance(account, Mapping) and account.get("type") == "chatgpt":
            email = clean_text(account.get("email"), 254).casefold()
            if "@" in email:
                return {
                    "provider": "codex",
                    "email": email,
                    "plan": clean_text(account.get("planType"), 80),
                }
    raise CollectionError("Codex did not report a signed-in ChatGPT account")


def probe_identity(provider: str, profile_dir: Path) -> dict[str, Any]:
    if not profile_dir.is_absolute() or not profile_dir.is_dir():
        raise CollectionError("account profile directory is unavailable")
    if provider == "codex":
        return _run_codex_account_read(profile_dir)
    if provider != "claude":
        raise CollectionError("account provider is invalid")
    try:
        completed = subprocess.run(
            ["claude", "auth", "status", "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
            env=_provider_environment("claude", profile_dir),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectionError("Claude account identity probe did not complete") from exc
    try:
        value = json.loads(completed.stdout)
    except ValueError as exc:
        raise CollectionError("Claude account identity response was invalid") from exc
    if not isinstance(value, Mapping) or value.get("loggedIn") is not True:
        raise CollectionError("Claude did not report a signed-in account")
    email = clean_text(value.get("email"), 254).casefold()
    if "@" not in email:
        raise CollectionError("Claude did not report an account email")
    return {
        "provider": "claude",
        "email": email,
        "plan": clean_text(
            value.get("subscriptionType") or value.get("subscription_type"), 80
        ),
    }


def _copy_regular(source: Path, destination: Path, *, mode: int = 0o600) -> None:
    info = source.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
    ):
        raise CollectionError("profile configuration source is unsafe")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    temporary = destination.parent / f".{destination.name}.{uuidlib.uuid4().hex}.tmp"
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            os.chmod(temporary, mode)
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        return
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.is_symlink():
            continue
        target = destination / entry.name
        if entry.is_dir():
            _copy_tree(entry, target)
        elif entry.is_file():
            _copy_regular(entry, target)


def sync_profile_configuration(provider: str, profile_dir: Path) -> None:
    source = _default_profile_dir(provider)
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(profile_dir, 0o700)
    if provider == "claude":
        candidate = source / "CLAUDE.md"
        if candidate.is_file() and not candidate.is_symlink():
            _copy_regular(candidate, profile_dir / "CLAUDE.md")
        settings = source / "settings.json"
        # A source profile with no settings file still yields one here: the
        # auto-continue default below is a promise enrolment makes about
        # every managed profile, not a transform applied only when there was
        # something to copy (review lane rv-pdn-2, 2026-08-17).
        value: dict[str, Any] = {}
        if settings.is_file() and not settings.is_symlink():
            try:
                parsed = json.loads(settings.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise CollectionError(
                    "Claude settings could not be safely copied"
                ) from exc
            if not isinstance(parsed, dict):
                raise CollectionError("Claude settings could not be safely copied")
            value = parsed
            allowed_environment = {
                "CLAUDE_AFK_TIMEOUT_MS",
                "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
                "CLAUDE_CODE_SUBAGENT_MODEL",
                "CLAUDE_CODE_TMPDIR",
            }
            environment = value.get("env")
            if isinstance(environment, dict):
                value["env"] = {
                    key: item
                    for key, item in environment.items()
                    if key in allowed_environment and isinstance(item, str)
                }
            else:
                value.pop("env", None)
        # Claude Code 2.1.234 resumes a limited session by itself when the
        # usage window resets. In a managed profile the account layer is
        # the only thing that decides when a reset account spends again,
        # so enrolment starts every profile with the switch off; the
        # person can turn it back on per profile in /config afterwards.
        value["autoContinueAtUsageLimit"] = False
        destination = profile_dir / "settings.json"
        temporary = profile_dir / f".settings.{uuidlib.uuid4().hex}.tmp"
        try:
            temporary.write_text(
                json.dumps(value, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
        for name in ("agents", "commands", "output-styles"):
            _copy_tree(source / name, profile_dir / name)
        return
    if provider == "codex":
        for name in ("AGENTS.md", "hooks.json"):
            candidate = source / name
            if candidate.is_file() and not candidate.is_symlink():
                _copy_regular(candidate, profile_dir / name)
        candidate = source / "config.toml"
        if candidate.is_file() and not candidate.is_symlink():
            raw = candidate.read_text(encoding="utf-8", errors="replace")
            if not re.search(
                r"(?im)^\s*[A-Za-z0-9_.-]*(?:token|secret|password|credential|api[_-]?key)[A-Za-z0-9_.-]*\s*=",
                raw,
            ):
                _copy_regular(candidate, profile_dir / "config.toml")
        for name in ("skills", "prompts", "themes"):
            _copy_tree(source / name, profile_dir / name)
        return
    raise CollectionError("account provider is invalid")


def _load_owned_claude_state(path: Path) -> Mapping[str, Any] | None:
    """Claude's own default state, or None with a stated reason.

    Every None here means a first-run offer will NOT be marked seen, and the
    operator meets the question again on their next fresh profile. That used to
    happen in complete silence, so the one symptom they would ever see was the
    bug returning (lane B F8). Now each refusal says which check failed.
    """
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            reason = "it is a symlink"
        elif not stat.S_ISREG(info.st_mode):
            reason = "it is not a regular file"
        elif info.st_uid != os.geteuid():
            reason = "it is owned by another user"
        elif info.st_size > MAX_CLAUDE_STATE_BYTES:
            reason = (
                f"it is {info.st_size} bytes, past the {MAX_CLAUDE_STATE_BYTES} cap"
            )
        else:
            reason = ""
        if reason:
            _unmarked(path, reason)
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # There is no default Claude profile on this machine. Nothing to copy
        # from, and nothing has gone wrong -- printing "could not be read: No
        # such file" would dress an ordinary state up as a fault, on every
        # enrollment, forever. The states that MATTER all still speak.
        return None
    except OSError as error:
        _unmarked(path, f"it could not be read: {error}")
        return None
    except ValueError as error:
        _unmarked(path, f"it is not valid JSON: {error}")
        return None
    if not isinstance(value, Mapping):
        _unmarked(path, "it does not hold a JSON object")
        return None
    return value


def _unmarked(path: Path, reason: str) -> None:
    """Say why a first-run offer is going unanswered. Never silent."""
    print(
        f"session inventory: not marking Claude's first-run offers seen -- "
        f"{path} could not be used because {reason}",
        file=sys.stderr,
    )


# First-run offers this kit may answer on the operator's behalf, by marking
# them SEEN. Two rules decide what may go here, and both must hold:
#   1. marking it seen changes nothing about how the session behaves, and
#   2. "you have already been shown this" is honestly true -- the operator has
#      seen it, on the profile Claude runs by default.
# So a flag is only ever copied FROM the default profile, and only when it is
# already true there. Choosing a permission mode, a model, or an update policy
# is not on this list and must never be: those are answers, not acknowledgments.
#
# hasSeenAutoDefaultNudge -- the "Make auto mode your default permission mode?"
#   offer. Seen means it stops being offered; the permission mode is untouched,
#   which is exactly the decision this must not make. A brand-new session on a
#   kit profile opened onto this question and the operator could not answer it
#   (2026-08-15), because a fresh profile has never been shown it.
CLAUDE_FIRST_RUN_SEEN_FLAGS = ("hasSeenAutoDefaultNudge",)
# Three shots at a file the provider may be writing. A real collision is one
# save, so the second attempt wins; a third means the provider is writing
# continuously and this courtesy write should stand aside.
_CLAUDE_PROFILE_WRITE_ATTEMPTS = 3
# Claude Code guards its own config writes with a proper-lockfile mkdir lock at
# `<config>.lock`, refreshed every 5s and considered stale after 10s (read out
# of the shipped 2.1.233 binary, 2026-08-15).
#
# This code deliberately does NOT take that lock. Claude acquires it with
# `retries: 0`: a Claude process that finds the lock held does not wait, it
# logs "Failed to save config with lock" and writes anyway down an UNLOCKED
# path that skips its own re-read. Taking the lock would therefore not
# serialise anything -- it would convert an ordinary collision into a
# guaranteed unlocked overwrite. Its exit-time plugin-usage flush takes no lock
# at all either.
#
# What the lock is good for is the other direction: its presence is a reliable
# "Claude is writing right now" signal, so this pass stands aside instead of
# starting a compare-and-swap that is certain to lose.
_CLAUDE_LOCK_STALE_SECONDS = 10.0
_CLAUDE_LOCK_WAIT_SECONDS = 0.2


def _provider_is_writing(path: Path) -> bool:
    """True while Claude's own config lock is held and fresh."""
    try:
        info = os.stat(path.with_name(path.name + ".lock"))
    except OSError:
        return False
    return (time.time() - info.st_mtime) < _CLAUDE_LOCK_STALE_SECONDS


def _complete_claude_profile_onboarding(profile_dir: Path) -> None:
    """Keep an authenticated isolated profile out of Claude's first-run offers.

    Compare-and-swap against the profile Claude itself writes, so a value the
    provider set while this ran can never be clobbered: only missing keys are
    filled, and the replace is abandoned rather than forced if the file moved
    under us. See the write at the foot of this function for why refusing is
    the right failure.
    """
    path = profile_dir / ".claude.json"
    value = read_private_json(
        path,
        max_bytes=MAX_CLAUDE_STATE_BYTES,
        allow_missing=False,
    )
    if not isinstance(value, dict):
        raise CollectionError("Claude profile state is invalid")
    changed = value.get("hasCompletedOnboarding") is not True
    value["hasCompletedOnboarding"] = True

    default_state = _load_owned_claude_state(Path.home() / ".claude.json")
    default_projects = (
        default_state.get("projects", {}) if isinstance(default_state, Mapping) else {}
    )
    projects = value.get("projects")
    if projects is not None and not isinstance(projects, dict):
        # Some other shape entirely: the provider owns this key and this
        # function does not understand what it is looking at. Leave it exactly
        # as found and copy no trust into it -- normalising it to {} destroyed
        # a real provider value (Codex finding 7).
        projects = None
    elif projects is None:
        projects = {}
        value["projects"] = projects
    if isinstance(default_projects, Mapping) and projects is not None:
        for project_path, project_state in default_projects.items():
            if (
                not isinstance(project_path, str)
                or not isinstance(project_state, Mapping)
                or project_state.get("hasTrustDialogAccepted") is not True
            ):
                continue
            current = projects.get(project_path)
            if not isinstance(current, dict):
                current = {}
                projects[project_path] = current
            if current.get("hasTrustDialogAccepted") is not True:
                current["hasTrustDialogAccepted"] = True
                changed = True
    # Only what the operator has already been shown, and only where this
    # profile has said nothing itself. An existing value -- true or false --
    # is their answer and is left exactly as it is.
    for flag in CLAUDE_FIRST_RUN_SEEN_FLAGS:
        if flag in value:
            continue
        if isinstance(default_state, Mapping) and default_state.get(flag) is True:
            value[flag] = True
            changed = True
    if not changed:
        return
    # THE WRITE. The provider owns this file too and takes no lock this code
    # could share, so "re-read, then replace" cannot be made safe by reading
    # later -- there is always a gap after the last read, and a provider save
    # landing in it was simply lost (lane B F7, Codex 5).
    #
    # So the write is a compare-and-swap instead of a replace. The file's
    # identity is captured BEFORE the content is read -- that ordering is what
    # makes it sound: if the identity is unchanged at replace time then nothing
    # touched the file across the whole read-decide-write span, so the content
    # being written is current. If it did change, the attempt is thrown away
    # and retried against the new content. And if it keeps changing, the write
    # is ABANDONED, not forced: the cost of not writing is that a first-run
    # offer is shown once, and the cost of forcing is losing whatever the
    # provider just saved. Those are not close.
    #
    # The guard detects a provider save with certainty rather than by luck:
    # Claude writes this file as `.tmp.<pid>.<hex>` beside the target followed
    # by a rename, so every one of its saves changes the inode. (Read out of
    # the shipped binary; the orphaned `.claude.json.tmp.*` files on this box
    # are the same mechanism leaving litter.) And a merged write of ours is not
    # ignored by a running Claude: it polls the file's mtime once a second and
    # re-reads on any advance.
    for _attempt in range(_CLAUDE_PROFILE_WRITE_ATTEMPTS):
        if _provider_is_writing(path):
            # Claude is mid-save. Starting a swap now just burns an attempt.
            time.sleep(_CLAUDE_LOCK_WAIT_SECONDS)
            if _provider_is_writing(path):
                continue
        before = _file_identity(path)
        latest = read_private_json(
            path, max_bytes=MAX_CLAUDE_STATE_BYTES, allow_missing=True
        )
        merged = value
        if isinstance(latest, dict):
            merged = dict(latest)
            merged["hasCompletedOnboarding"] = True
            latest_projects = merged.get("projects")
            if latest_projects is not None and not isinstance(latest_projects, dict):
                latest_projects = None  # not ours to reshape
            elif latest_projects is None:
                latest_projects = {}
                merged["projects"] = latest_projects
            for project_path, project_state in (projects or {}).items():
                if latest_projects is None:
                    break
                current = latest_projects.get(project_path)
                if not isinstance(current, dict):
                    latest_projects[project_path] = dict(project_state)
                elif project_state.get("hasTrustDialogAccepted") is True:
                    current.setdefault("hasTrustDialogAccepted", True)
            for flag in CLAUDE_FIRST_RUN_SEEN_FLAGS:
                if flag not in merged and value.get(flag) is True:
                    merged[flag] = True
        try:
            _atomic_private_json(path, merged, expect=before)
            return
        except _FileChangedUnderUs:
            continue
    print(
        "session inventory: not marking Claude's first-run offers seen -- "
        f"{path} is being written by Claude right now, and overwriting it "
        f"would lose what Claude just saved (gave up after "
        f"{_CLAUDE_PROFILE_WRITE_ATTEMPTS} attempts)",
        file=sys.stderr,
    )


def _register_profile(
    config: Mapping[str, Any],
    provider: str,
    alias: str,
    expected_email: str,
    profile_dir: Path,
    *,
    legacy: bool,
    enable: bool = True,
) -> dict[str, Any]:
    """Record one verified profile.

    ``enable=True`` is enrolment: the operator is adding or adopting an
    account, and enabling it is the point of the call. ``enable=False`` is
    re-verification of an account that is already enrolled, and it may never
    turn a disabled profile back on.

    That distinction is not cosmetic. The identity probe below runs a provider
    binary and can take seconds; the registry is shared, and the operator can
    disable an account inside that window. Writing ``enabled: True``
    unconditionally afterwards silently undid their decision, and any caller
    that then used the profile spent money on an account they had switched off.
    Re-verification therefore re-reads the row under the lock and refuses on
    anything but a still-enabled profile at the same directory.
    """
    if enable:
        before: Mapping[str, Any] | None = None
    else:
        registry_before = load_registry(config)
        found = registry_before["profiles"].get(_profile_key(provider, alias))
        before = dict(found) if isinstance(found, Mapping) else None
        if before is None or before.get("enabled") is not True:
            raise CollectionError("account profile is unavailable")
    identity = probe_identity(provider, profile_dir)
    email = clean_text(expected_email, 254).casefold()
    if identity["email"] != email:
        raise CollectionError(
            "provider-reported account does not match the selected email"
        )
    if provider == "claude" and not legacy:
        _complete_claude_profile_onboarding(profile_dir)
    with _registry_lock(config):
        registry = load_registry(config)
        key = _profile_key(provider, alias)
        current = registry["profiles"].get(key)
        if not enable:
            # Compare-and-set against what was read before the probe. An
            # unrelated write elsewhere in the registry is fine; a change to
            # THIS profile is not, and being switched off is the change that
            # matters most.
            if not isinstance(current, Mapping) or current.get("enabled") is not True:
                raise CollectionError(
                    "account %s was disabled while it was being verified; "
                    "it was left disabled" % alias
                )
            # This is a real compare-and-set on the operator-controlled
            # identity fields, not merely an enabled-bit check wearing that
            # name.  Another verifier may refresh plan/timestamp while the
            # probe runs, but an operator changing the email, directory,
            # legacy status, or enabled state makes this probe stale and it
            # may not overwrite that newer decision.
            guarded_fields = (
                "provider",
                "alias",
                "email",
                "profile_dir",
                "legacy",
                "enabled",
            )
            if before is None or any(
                current.get(field) != before.get(field) for field in guarded_fields
            ):
                raise CollectionError(
                    "account %s changed while it was being verified; its newer "
                    "settings were left unchanged" % alias
                )
        for other_key, other in registry["profiles"].items():
            if (
                other_key != key
                and other["provider"] == provider
                and other["email"] == email
            ):
                raise CollectionError("that provider account is already enrolled")
        _safe_profile_path(
            provider, alias, os.fspath(profile_dir), config=config, legacy=legacy
        )
        registry["profiles"][key] = {
            "provider": provider,
            "alias": alias,
            "email": email,
            "profile_dir": os.fspath(profile_dir),
            "legacy": legacy,
            "plan": identity.get("plan", ""),
            "verified_at_unix_ms": _now_ms(),
            # Never resurrected: re-verification carries the value it just
            # proved was True, and enrolment is the only path that sets it.
            "enabled": True if enable else current.get("enabled") is True,
        }
        registry["generation"] += 1
        write_registry(config, registry)
        return dict(registry["profiles"][key])


def adopt_default(
    config: Mapping[str, Any], provider: str, alias: str, expected_email: str
) -> dict[str, Any]:
    return _register_profile(
        config,
        provider,
        alias,
        expected_email,
        _default_profile_dir(provider),
        legacy=True,
    )


def enroll(
    config: Mapping[str, Any], provider: str, alias: str, expected_email: str
) -> dict[str, Any]:
    if kill_switch_path(config).exists():
        raise CollectionError("account enrollment and switching are disabled")
    target = account_root(config) / provider / alias
    _safe_profile_path(provider, alias, os.fspath(target), config=config, legacy=False)
    sync_profile_configuration(provider, target)
    environment = _provider_environment(provider, target)
    argv = (
        ["claude", "auth", "login", "--claudeai", "--email", expected_email]
        if provider == "claude"
        else ["codex", "login", "--device-auth"]
    )
    completed = subprocess.run(argv, env=environment, check=False)
    if completed.returncode != 0:
        raise CollectionError("provider login did not complete")
    return _register_profile(
        config, provider, alias, expected_email, target, legacy=False
    )


def verify_profile(
    config: Mapping[str, Any], provider: str, alias: str
) -> dict[str, Any]:
    """Re-prove an already-enrolled profile. Never enables anything."""
    current = profile(config, provider, alias)
    return _register_profile(
        config,
        provider,
        alias,
        current["email"],
        Path(current["profile_dir"]),
        legacy=current["legacy"],
        enable=False,
    )


def _load_optional_json(path: Path, max_bytes: int) -> Mapping[str, Any] | None:
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > max_bytes
        ):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _provider_advice(advice: Mapping[str, Any], provider: str) -> Any:
    """Return the rotation advice addressed to one provider.

    Advice started out Claude-only: one top-level ``use_now``, with no way to
    say which provider it meant. The roster beside it has always carried both
    providers, so a Codex account could be healthy, serving, and still never
    recommended, the audit finding this fixes.

    A feed that covers both says so, either as ``use_now_<provider>`` or under
    ``providers.<provider>.use_now``. The bare top-level key keeps its original
    meaning, Claude, because that is what every feed writing it means today;
    reading it for Codex would recommend a Claude account for a Codex session.
    """
    keyed = advice.get(f"use_now_{provider}")
    if isinstance(keyed, Mapping):
        return keyed
    providers = advice.get("providers")
    if isinstance(providers, Mapping):
        section = providers.get(provider)
        if isinstance(section, Mapping) and isinstance(section.get("use_now"), Mapping):
            return section["use_now"]
    if provider == "claude":
        return advice.get("use_now")
    return None


def account_choices(config: Mapping[str, Any], provider: str) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise CollectionError("account provider is invalid")
    now = int(time.time())
    configured_roster, configured_advice = _feed_paths(config)
    roster_path = Path(
        os.environ.get("SESSION_KIT_ACCOUNT_ROSTER") or configured_roster
    )
    advice_path = Path(
        os.environ.get("SESSION_KIT_ROTATION_ADVICE") or configured_advice
    )
    roster = _load_optional_json(roster_path, 2 * 1024 * 1024)
    advice = _load_optional_json(advice_path, 2 * 1024 * 1024)
    try:
        max_age = int(
            os.environ.get(
                "SESSION_KIT_ACCOUNT_ADVICE_MAX_AGE_SECONDS",
                str(DEFAULT_ADVICE_MAX_AGE_SECONDS),
            )
        )
    except ValueError:
        max_age = DEFAULT_ADVICE_MAX_AGE_SECONDS
    roster_ts = roster.get("ts") if roster else None
    roster_fresh = isinstance(roster_ts, int) and 0 <= now - roster_ts <= max_age
    health_by_email: dict[str, Mapping[str, Any]] = {}
    if roster is not None and roster_fresh:
        key = "accounts" if provider == "claude" else "codex_accounts"
        rows = roster.get(key)
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                email = clean_text(row.get("email"), 254).casefold()
                if email:
                    health_by_email[email] = row
    recommended_email = ""
    advice_reason = ""
    advice_ts = advice.get("ts") if advice else None
    advice_fresh = isinstance(advice_ts, int) and 0 <= now - advice_ts <= max_age
    if advice is not None and advice_fresh:
        use_now = _provider_advice(advice, provider)
        if isinstance(use_now, Mapping):
            recommended_email = clean_text(use_now.get("account"), 254).casefold()
            # Feeds have shipped the explanation under both spellings. Taking
            # either beats showing a recommendation with no stated reason.
            advice_reason = clean_text(use_now.get("why") or use_now.get("reason"), 240)
    roster_state = (
        "fresh" if roster_fresh else ("stale" if roster is not None else "absent")
    )
    choices: list[dict[str, Any]] = []
    for item in list_profiles(config, provider):
        health = health_by_email.get(item["email"])
        # `serving` is the CLIProxy cron-lane routing toggle. Interactive
        # sessions authenticate with the profile's own login and never ride
        # that lane, so a resting account with a healthy probe is selectable.
        # `serving` stays in the payload as advisory display.
        #
        # A stale or absent roster proves nothing about any account. Failing
        # closed there turns a dead feed process into a machine where no
        # session can be started at all, so eligibility falls open to the
        # profile registry and the row says its health is unverified.
        if roster_fresh:
            eligible = bool(
                item["enabled"]
                and health
                and str(health.get("health") or "").casefold() == "ok"
            )
        else:
            eligible = bool(item["enabled"])
        health_word = (
            clean_text(health.get("health"), 40)
            if (health and roster_fresh)
            else "unverified"
        )
        if eligible:
            state = "ready" if roster_fresh else "ready (feed %s)" % roster_state
        elif not item["enabled"]:
            state = "blocked: profile disabled"
        elif not health:
            state = "blocked: not in the account roster"
        else:
            state = "blocked: health %s" % (health_word or "unknown")
        choices.append(
            {
                "alias": item["alias"],
                "email": item["email"],
                "plan": item["plan"],
                "eligible": eligible,
                "state": state,
                "health": health_word,
                "serving": health.get("serving") is True if health else False,
                "u5h": health.get("u5h") if (health and roster_fresh) else None,
                "u7d": health.get("u7d") if (health and roster_fresh) else None,
                "recommended": eligible and item["email"] == recommended_email,
            }
        )
    recommendation = next((row for row in choices if row["recommended"]), None)
    # The reason travels only with a live recommendation. An advised account
    # that is present but blocked is reported as exactly that, so a renderer
    # can say "X is advised but not selectable because Y" instead of nothing.
    blocked_advice = None
    if recommendation is None and recommended_email:
        advised = next(
            (row for row in choices if row["email"] == recommended_email), None
        )
        if advised is not None:
            blocked_advice = {"alias": advised["alias"], "state": advised["state"]}
    return {
        "schema_version": ACCOUNT_SCHEMA_VERSION,
        "provider": provider,
        "roster_fresh": roster_fresh,
        "roster_state": roster_state,
        "advice_fresh": advice_fresh,
        "recommendation": recommendation["alias"] if recommendation else None,
        "recommendation_reason": advice_reason if recommendation else "",
        "recommendation_blocked": blocked_advice,
        "choices": choices,
    }


def _require_eligible_profile(
    config: Mapping[str, Any], provider: str, alias: str
) -> None:
    choices = account_choices(config, provider)["choices"]
    selected = next((row for row in choices if row["alias"] == alias), None)
    if not selected:
        raise CollectionError(f"account {alias} is not enrolled for {provider}")
    if selected.get("eligible") is not True:
        # Name the actual predicate that failed; "unavailable" refusals that
        # guess at a login problem send the operator to re-enroll a working
        # account.
        raise CollectionError(
            f"account {alias} is not selectable: {selected.get('state')}"
        )


def resume_profile(
    config: Mapping[str, Any], provider: str, alias: str
) -> dict[str, Any]:
    item = verify_profile(config, provider, alias)
    if provider == "claude":
        # Every profile the kit can launch, not only the ones it creates from
        # here on. A profile enrolled before this shipped has never been told
        # the first-run offers were seen, and it is exactly such a profile the
        # operator's blocked session was running on. This writes only when a
        # marker is missing, so it costs one write per profile, once.
        try:
            _complete_claude_profile_onboarding(Path(str(item["profile_dir"])))
        except (CollectionError, OSError):
            # A launch must never fail because a courtesy write did not land.
            pass
    return {
        "provider": provider,
        "alias": alias,
        "email": item["email"],
        "profile_dir": item["profile_dir"],
        "plan": item["plan"],
    }


def launch_profile(
    config: Mapping[str, Any], provider: str, alias: str
) -> dict[str, Any]:
    if kill_switch_path(config).exists():
        raise CollectionError("account enrollment and switching are disabled")
    item = resume_profile(config, provider, alias)
    _require_eligible_profile(config, provider, alias)
    return item


def _transaction_root(config: Mapping[str, Any]) -> Path:
    return Path(str(config["state_dir"])) / "account-switches"


def _transaction_path(config: Mapping[str, Any], txid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", txid):
        raise CollectionError("account switch transaction id is invalid")
    return _transaction_root(config) / f"{txid}.json"


def _load_transaction(config: Mapping[str, Any], txid: str) -> dict[str, Any]:
    raw = read_private_json(
        _transaction_path(config, txid), max_bytes=MAX_TRANSACTION_BYTES
    )
    if (
        not isinstance(raw, Mapping)
        or raw.get("schema_version") != ACCOUNT_SCHEMA_VERSION
    ):
        raise CollectionError("account switch transaction is invalid")
    if raw.get("txid") != txid or raw.get("provider") not in PROVIDERS:
        raise CollectionError("account switch transaction identity is invalid")
    if not valid_uuid(raw.get("uuid")):
        raise CollectionError("account switch transaction UUID is invalid")
    return dict(raw)


def _save_transaction(config: Mapping[str, Any], value: Mapping[str, Any]) -> None:
    _atomic_private_json(_transaction_path(config, str(value["txid"])), value)


def prepare_switch(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    source_alias: str,
    target_alias: str,
    cwd: str,
    shpool_id: str,
) -> dict[str, Any]:
    if kill_switch_path(config).exists():
        raise CollectionError("account enrollment and switching are disabled")
    exact = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact or not os.path.isabs(cwd):
        raise CollectionError("account switch identity is invalid")
    source = profile(config, provider, source_alias)
    target = profile(config, provider, target_alias)
    _require_eligible_profile(config, provider, target_alias)
    if source_alias == target_alias:
        raise CollectionError("account switch target is already active")
    current = binding_for(config, provider, exact)
    if current is None or current["alias"] != source_alias:
        raise CollectionError("account switch source binding changed")
    txid = uuidlib.uuid4().hex
    value = {
        "schema_version": ACCOUNT_SCHEMA_VERSION,
        "txid": txid,
        "provider": provider,
        "uuid": exact,
        "cwd": cwd,
        "shpool_id": clean_text(shpool_id, 128),
        "source_alias": source_alias,
        "source_profile": source["profile_dir"],
        "target_alias": target_alias,
        "target_profile": target["profile_dir"],
        "status": "prepared",
        "created_at_unix_ms": _now_ms(),
        "updated_at_unix_ms": _now_ms(),
        "artifacts": [],
    }
    _save_transaction(config, value)
    return value


def _artifact_paths(provider: str, profile_dir: Path, uuid: str) -> list[Path]:
    if provider == "claude":
        candidates = list((profile_dir / "projects").glob(f"**/{uuid}.jsonl"))
        if len(candidates) != 1:
            raise CollectionError("Claude exact transcript is missing or ambiguous")
        result = [candidates[0]]
        companion = candidates[0].with_suffix("")
        if companion.is_dir() and not companion.is_symlink():
            result.append(companion)
        return result
    candidates = list((profile_dir / "sessions").glob(f"**/rollout-*{uuid}.jsonl"))
    if len(candidates) != 1:
        raise CollectionError("Codex exact rollout is missing or ambiguous")
    return [candidates[0]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            if size > MAX_ARTIFACT_BYTES:
                raise CollectionError("provider artifact exceeds the switch limit")
            digest.update(chunk)
    return digest.hexdigest()


def _copy_artifact(source: Path, destination: Path) -> list[dict[str, Any]]:
    if source.is_symlink():
        raise CollectionError("provider artifact is a symlink")
    if source.is_file():
        _copy_regular(source, destination)
        return [{"path": os.fspath(destination), "sha256": _sha256(destination)}]
    if not source.is_dir():
        raise CollectionError("provider artifact type is unsupported")
    records: list[dict[str, Any]] = []
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    for entry in sorted(source.rglob("*")):
        if entry.is_symlink():
            raise CollectionError("provider artifact tree contains a symlink")
        relative = entry.relative_to(source)
        target = destination / relative
        if entry.is_dir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif entry.is_file():
            _copy_regular(entry, target)
            records.append({"path": os.fspath(target), "sha256": _sha256(target)})
    return records


def apply_switch(config: Mapping[str, Any], txid: str) -> dict[str, Any]:
    value = _load_transaction(config, txid)
    if value.get("status") != "prepared":
        raise CollectionError("account switch transaction is not prepared")
    provider = str(value["provider"])
    exact = str(value["uuid"])
    source_root = Path(str(value["source_profile"]))
    target_root = Path(str(value["target_profile"]))
    transaction_dir = _transaction_root(config) / txid
    backup_root = transaction_dir / "backup"
    ensure_private_directory(transaction_dir)
    ensure_private_directory(backup_root)
    artifacts: list[dict[str, Any]] = []
    copied_targets: list[Path] = []
    try:
        for source in _artifact_paths(provider, source_root, exact):
            relative = source.relative_to(source_root)
            backup = backup_root / relative
            target = target_root / relative
            if target.exists():
                raise CollectionError(
                    "target profile already contains this exact conversation"
                )
            copied_targets.append(target)
            _copy_artifact(source, backup)
            _copy_artifact(source, target)
            artifacts.append(
                {
                    "relative": os.fspath(relative),
                    "source_sha256": _sha256(source) if source.is_file() else "tree",
                    "kind": "file" if source.is_file() else "directory",
                }
            )
    except BaseException:
        for target in reversed(copied_targets):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.is_file() and not target.is_symlink():
                target.unlink()
        with contextlib.suppress(OSError):
            shutil.rmtree(backup_root)
        raise
    value["artifacts"] = artifacts
    value["status"] = "target_launching"
    value["updated_at_unix_ms"] = _now_ms()
    _save_transaction(config, value)
    return {
        "txid": txid,
        "alias": value["target_alias"],
        "profile_dir": value["target_profile"],
        "uuid": exact,
    }


def commit_switch(config: Mapping[str, Any], txid: str) -> dict[str, Any]:
    value = _load_transaction(config, txid)
    if value.get("status") != "target_launching":
        raise CollectionError("account switch transaction cannot commit")
    provider = str(value["provider"])
    exact = str(value["uuid"])
    target_identity = probe_identity(provider, Path(str(value["target_profile"])))
    target_profile = profile(config, provider, str(value["target_alias"]))
    if target_identity["email"] != target_profile["email"]:
        raise CollectionError("target provider account identity changed")
    source_root = Path(str(value["source_profile"]))
    for artifact in value.get("artifacts", []):
        relative = Path(str(artifact["relative"]))
        source = source_root / relative
        if source.is_dir() and not source.is_symlink():
            shutil.rmtree(source)
        elif source.is_file() and not source.is_symlink():
            source.unlink()
    bind(
        config,
        provider,
        exact,
        str(value["target_alias"]),
        source="account-switch",
    )
    value["status"] = "committed"
    value["updated_at_unix_ms"] = _now_ms()
    _save_transaction(config, value)
    return value


def rollback_switch(config: Mapping[str, Any], txid: str) -> dict[str, Any]:
    value = _load_transaction(config, txid)
    if value.get("status") not in {"target_launching", "prepared"}:
        raise CollectionError("account switch transaction cannot roll back")
    source_root = Path(str(value["source_profile"]))
    target_root = Path(str(value["target_profile"]))
    backup_root = _transaction_root(config) / txid / "backup"
    for artifact in value.get("artifacts", []):
        relative = Path(str(artifact["relative"]))
        source = source_root / relative
        target = target_root / relative
        backup = backup_root / relative
        # The target may contain newer provider output. Preserve that newest
        # valid artifact when returning to the source profile.
        restore_from = target if target.exists() else backup
        if source.exists():
            if source.is_dir() and not source.is_symlink():
                shutil.rmtree(source)
            elif source.is_file() and not source.is_symlink():
                source.unlink()
        _copy_artifact(restore_from, source)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.is_file() and not target.is_symlink():
            target.unlink()
    # A commit can fail after publishing the target binding but before its
    # transaction record reaches `committed`. Recovery must restore both the
    # provider artifact and the exact UUID-to-profile binding.
    bind(
        config,
        str(value["provider"]),
        str(value["uuid"]),
        str(value["source_alias"]),
        source="account-switch-rollback",
    )
    value["status"] = "rolled_back"
    value["updated_at_unix_ms"] = _now_ms()
    _save_transaction(config, value)
    return {
        "txid": txid,
        "alias": value["source_alias"],
        "profile_dir": value["source_profile"],
        "uuid": value["uuid"],
    }


def transaction(config: Mapping[str, Any], txid: str) -> dict[str, Any]:
    return _load_transaction(config, txid)


def canonical_rules_path() -> Path:
    """Where the one rulebook lives.

    Owner-private by design: it holds the operator's own working rules and is
    never part of a repository, so the kit only ever reads it from a
    configurable location outside its own tree.
    """
    override = os.environ.get("SESSION_KIT_RULES_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "agent-rules" / "universal-rules.md"


def rules_targets(config: Mapping[str, Any]) -> list[dict[str, str]]:
    """Every rulebook that should carry the canonical block.

    Both provider default homes are included even when no profile is enrolled:
    an unenrolled machine still has one rulebook per provider, and leaving it
    out would let the default home drift away from the profiles.
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def offer(provider: str, path: Path, label: str) -> None:
        resolved = os.path.realpath(path)
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        found.append({"provider": provider, "label": label, "path": os.fspath(path)})

    for provider in sorted(PROVIDERS):
        offer(
            provider,
            _default_profile_dir(provider) / RULES_FILENAME[provider],
            f"{provider}:default",
        )
    for item in list_profiles(config):
        provider = str(item.get("provider", ""))
        if provider not in RULES_FILENAME:
            continue
        profile_dir = str(item.get("profile_dir", ""))
        if not profile_dir:
            continue
        offer(
            provider,
            Path(profile_dir) / RULES_FILENAME[provider],
            f"{provider}:{item.get('alias', '')}",
        )
    return found


def render_rules_block(body: str) -> str:
    """Wrap the canonical text in a marked, fingerprinted block."""
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    head = (
        f"{RULES_BEGIN} · generated · sha256:{digest} · DO NOT EDIT HERE — edit "
        f"the canonical rules file and run `sp account sync-rules` -->"
    )
    return f"{head}\n\n{body.strip()}\n\n{RULES_END}"


def _write_rulebook(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".sync-tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as writer:
            writer.write(text)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def sync_rules(config: Mapping[str, Any], *, check: bool = False) -> dict[str, Any]:
    """Render the canonical rulebook into every provider and profile copy.

    `check` reports drift and changes nothing, which is what the doctor and any
    pre-push gate should call. A rulebook that has never carried the block keeps
    all of its existing text: the block is added above it rather than replacing
    it, so nothing a provider already relies on is lost by adopting the sync.
    """
    source = canonical_rules_path()
    if not source.is_file():
        return {
            "ok": False,
            "reason": "no canonical rules file",
            "source": os.fspath(source),
            "targets": [],
            "drifted": 0,
        }
    if source.stat().st_size > MAX_RULES_BYTES:
        raise CollectionError("the canonical rules file is implausibly large")
    try:
        body = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise CollectionError(
            f"the canonical rules file is not UTF-8 text: {source}"
        ) from error
    block = render_rules_block(body)
    results = []
    drifted = 0
    for target in rules_targets(config):
        path = Path(target["path"])
        # A symlinked rulebook belongs to whatever manages the link target,
        # dotfiles usually. Rewriting through the link would edit that other
        # system's file, and the backup copy refuses symlinks anyway -- so the
        # skip happens here, by name, instead of five targets in as an abort
        # that leaves the run half applied.
        if path.is_symlink():
            results.append({**target, "state": "skipped", "reason": "symlink"})
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            results.append({**target, "state": "skipped", "reason": "not UTF-8 text"})
            continue
        if RULES_BLOCK_RE.search(original):
            updated = RULES_BLOCK_RE.sub(lambda _: block, original, count=1)
        else:
            updated = f"{block}\n\n{original.lstrip()}"
        if updated == original:
            state = "current"
        elif check:
            state = "drifted"
            drifted += 1
        else:
            backup = path.with_name(path.name + ".bak-sync")
            _copy_regular(path, backup)
            _write_rulebook(path, updated)
            state = "updated"
        results.append({**target, "state": state})
    return {
        "ok": True,
        "source": os.fspath(source),
        "targets": results,
        "drifted": drifted,
    }
