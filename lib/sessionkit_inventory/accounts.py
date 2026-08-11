"""Owner-only provider account profiles and exact-thread switch transactions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
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
MAX_TRANSACTION_BYTES = 256 * 1024
MAX_FEED_CONFIG_BYTES = 16 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
PROVIDERS = frozenset({"claude", "codex"})


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
            os.environ.get("XDG_DATA_HOME")
            or (Path.home() / ".local" / "share")
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
    if not isinstance(raw, Mapping) or raw.get("schema_version") != ACCOUNT_SCHEMA_VERSION:
        raise CollectionError("account feed configuration is invalid")
    paths = tuple(Path(str(raw.get(key) or "")) for key in ("roster_path", "advice_path"))
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
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
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


def load_registry(config: Mapping[str, Any], *, allow_missing: bool = True) -> dict[str, Any]:
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


def _atomic_private_json(path: Path, value: Any) -> None:
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


def write_registry(config: Mapping[str, Any], registry: Mapping[str, Any]) -> dict[str, Any]:
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


def list_profiles(config: Mapping[str, Any], provider: str | None = None) -> list[dict[str, Any]]:
    if provider is not None and provider not in PROVIDERS:
        raise CollectionError("account provider is invalid")
    registry = load_registry(config)
    result = [
        dict(value)
        for value in registry["profiles"].values()
        if provider is None or value["provider"] == provider
    ]
    return sorted(result, key=lambda item: (item["provider"], item["alias"]))


def binding_for(config: Mapping[str, Any], provider: str, uuid: str) -> dict[str, Any] | None:
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
                    "clientInfo": {"name": "session-kit", "title": "Session Kit", "version": "1"},
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
    try:
        completed = subprocess.run(
            ["codex", "app-server", "--listen", "stdio://"],
            input=requests,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
            env=_provider_environment("codex", profile_dir),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectionError("Codex account identity probe did not complete") from exc
    for line in completed.stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, Mapping) and value.get("id") == 1:
            result = value.get("result")
            account = result.get("account") if isinstance(result, Mapping) else None
            if isinstance(account, Mapping) and account.get("type") == "chatgpt":
                email = clean_text(account.get("email"), 254).casefold()
                if "@" in email:
                    return {
                        "provider": "codex",
                        "email": email,
                        "plan": clean_text(account.get("planType"), 80),
                    }
            break
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
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
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
        if settings.is_file() and not settings.is_symlink():
            try:
                value = json.loads(settings.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise CollectionError("Claude settings could not be safely copied") from exc
            if not isinstance(value, dict):
                raise CollectionError("Claude settings could not be safely copied")
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
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_size > MAX_REGISTRY_BYTES
        ):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _complete_claude_profile_onboarding(profile_dir: Path) -> None:
    """Keep an authenticated isolated profile out of Claude's first-run login wizard."""
    path = profile_dir / ".claude.json"
    value = read_private_json(
        path,
        max_bytes=MAX_REGISTRY_BYTES,
        allow_missing=False,
    )
    if not isinstance(value, dict):
        raise CollectionError("Claude profile state is invalid")
    changed = value.get("hasCompletedOnboarding") is not True
    value["hasCompletedOnboarding"] = True

    default_state = _load_owned_claude_state(Path.home() / ".claude.json")
    default_projects = (
        default_state.get("projects", {})
        if isinstance(default_state, Mapping)
        else {}
    )
    projects = value.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        value["projects"] = projects
    if isinstance(default_projects, Mapping):
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
    if changed:
        _atomic_private_json(path, value)


def _register_profile(
    config: Mapping[str, Any],
    provider: str,
    alias: str,
    expected_email: str,
    profile_dir: Path,
    *,
    legacy: bool,
) -> dict[str, Any]:
    identity = probe_identity(provider, profile_dir)
    email = clean_text(expected_email, 254).casefold()
    if identity["email"] != email:
        raise CollectionError("provider-reported account does not match the selected email")
    if provider == "claude" and not legacy:
        _complete_claude_profile_onboarding(profile_dir)
    with _registry_lock(config):
        registry = load_registry(config)
        key = _profile_key(provider, alias)
        for other_key, other in registry["profiles"].items():
            if other_key != key and other["provider"] == provider and other["email"] == email:
                raise CollectionError("that provider account is already enrolled")
        _safe_profile_path(provider, alias, os.fspath(profile_dir), config=config, legacy=legacy)
        registry["profiles"][key] = {
            "provider": provider,
            "alias": alias,
            "email": email,
            "profile_dir": os.fspath(profile_dir),
            "legacy": legacy,
            "plan": identity.get("plan", ""),
            "verified_at_unix_ms": _now_ms(),
            "enabled": True,
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


def verify_profile(config: Mapping[str, Any], provider: str, alias: str) -> dict[str, Any]:
    current = profile(config, provider, alias)
    return _register_profile(
        config,
        provider,
        alias,
        current["email"],
        Path(current["profile_dir"]),
        legacy=current["legacy"],
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
    if provider == "claude" and advice is not None and advice_fresh:
        use_now = advice.get("use_now")
        if isinstance(use_now, Mapping):
            recommended_email = clean_text(use_now.get("account"), 254).casefold()
            advice_reason = clean_text(use_now.get("why"), 240)
    choices: list[dict[str, Any]] = []
    for item in list_profiles(config, provider):
        health = health_by_email.get(item["email"])
        eligible = bool(
            item["enabled"]
            and health
            and health.get("serving") is True
            and str(health.get("health") or "").casefold() == "ok"
        )
        choices.append(
            {
                "alias": item["alias"],
                "email": item["email"],
                "plan": item["plan"],
                "eligible": eligible,
                "health": clean_text(health.get("health"), 40) if health else "unverified",
                "serving": health.get("serving") is True if health else False,
                "u5h": health.get("u5h") if health else None,
                "u7d": health.get("u7d") if health else None,
                "recommended": eligible and item["email"] == recommended_email,
            }
        )
    recommendation = next((row for row in choices if row["recommended"]), None)
    return {
        "schema_version": ACCOUNT_SCHEMA_VERSION,
        "provider": provider,
        "roster_fresh": roster_fresh,
        "advice_fresh": advice_fresh,
        "recommendation": recommendation["alias"] if recommendation else None,
        "recommendation_reason": advice_reason,
        "choices": choices,
    }


def _require_eligible_profile(
    config: Mapping[str, Any], provider: str, alias: str
) -> None:
    choices = account_choices(config, provider)["choices"]
    selected = next((row for row in choices if row["alias"] == alias), None)
    if not selected or selected.get("eligible") is not True:
        raise CollectionError("selected account is not healthy and serving")


def resume_profile(config: Mapping[str, Any], provider: str, alias: str) -> dict[str, Any]:
    item = verify_profile(config, provider, alias)
    return {
        "provider": provider,
        "alias": alias,
        "email": item["email"],
        "profile_dir": item["profile_dir"],
        "plan": item["plan"],
    }


def launch_profile(config: Mapping[str, Any], provider: str, alias: str) -> dict[str, Any]:
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
    raw = read_private_json(_transaction_path(config, txid), max_bytes=MAX_TRANSACTION_BYTES)
    if not isinstance(raw, Mapping) or raw.get("schema_version") != ACCOUNT_SCHEMA_VERSION:
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
                raise CollectionError("target profile already contains this exact conversation")
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
