#!/usr/bin/env python3
"""Exact, read-only shpool/Claude/Codex session inventory.

The collector takes one shpool JSON snapshot and one Claude agents JSON
snapshot, then joins provider identities to a bounded native process table.
Linux Codex identities come from an open root rollout owned by the native
Codex CLI or App Server process; Darwin uses that exact process's
``CODEX_THREAD_ID``. It
never selects a conversation by cwd or recency.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil  # noqa: F401  # re-exported facade symbol
import stat as statmod
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

_SESSION_KIT_LIB_DIR = os.fspath(Path(__file__).resolve().parent)
if _SESSION_KIT_LIB_DIR not in sys.path:
    sys.path.insert(0, _SESSION_KIT_LIB_DIR)

from sessionkit_messages import command as _messages  # noqa: E402
from sessionkit_messages.envelope import MessageError  # noqa: E402
from sessionkit_events import queue as _events  # noqa: E402
from sessionkit_inventory import collector as _collector  # noqa: E402
from sessionkit_inventory import accounts as _accounts  # noqa: E402
from sessionkit_inventory import colors as _colors  # noqa: E402
from sessionkit_inventory import common as _common  # noqa: E402
from sessionkit_inventory import lifecycle as _lifecycle  # noqa: E402
from sessionkit_inventory import model as _model  # noqa: E402
from sessionkit_inventory import migration as _migration  # noqa: E402
from sessionkit_inventory import names as _names  # noqa: E402
from sessionkit_inventory import names_push as _names_push  # noqa: E402
from sessionkit_inventory import processes as _processes  # noqa: E402
from sessionkit_inventory import recovery as _recovery  # noqa: E402
from sessionkit_inventory import render as _render  # noqa: E402
from sessionkit_inventory import providers as _providers  # noqa: E402
from sessionkit_inventory import providers_claude as _providers_claude  # noqa: E402
from sessionkit_inventory import providers_codex as _providers_codex  # noqa: E402
from sessionkit_inventory import state_io as _state_io  # noqa: E402
from sessionkit_inventory import self_name as _self_name  # noqa: E402
from sessionkit_inventory import snapshot as _snapshot  # noqa: E402
from sessionkit_inventory import terminal as _terminal  # noqa: E402
from sessionkit_inventory import validation as _validation  # noqa: E402
from sessionkit_inventory.collector import (  # noqa: E402, F401
    AVAILABILITY_ORDER,
    PROVIDER_ORDER,
)
from sessionkit_inventory.terminal import (  # noqa: E402, F401
    TERMINAL_NUMBER_QUARANTINE_SECONDS,
)
from sessionkit_inventory.colors import (  # noqa: E402, F401
    CLAUDE_SESSION_COLORS,
    CODEX_SESSION_COLORS,
    COLOR_RESERVATION_MAX_AGE_SECONDS,
    LAUNCH_COLOR_MAX_AGE_SECONDS,
    SESSION_COLORS,
    first_free_color,
)
from sessionkit_inventory.self_name import (  # noqa: E402, F401
    MAX_AUTO_TITLE_TRANSCRIPT_BYTES,
    RESUME_CONTINUATION_TEXT,
    _TITLE_TRAILING_STOPWORDS,
)
from sessionkit_inventory.names_push import (  # noqa: E402, F401
    MAX_CLAUDE_SESSION_RECORDS,
    MAX_CODEX_LIVE_RENAME_FRAME,
    MAX_CODEX_LIVE_RENAME_SOCKETS,
)
from sessionkit_inventory.render import (  # noqa: E402, F401
    DEFAULT_STALL_SECONDS,
    _color_enabled,
    _display_title,
    _display_width,
    _format_age,
    lookup,
    stall_threshold_seconds,
)
from sessionkit_inventory.validation import (  # noqa: E402, F401
    _missing_shell_generation_is_quarantinable,
    _missing_shell_generation_is_quarantined,
)
from sessionkit_inventory.recovery import (  # noqa: E402, F401
    _generation_key,
    _pending_conflict_fields,
    _pending_evidence,
    _pending_preferred_entry,
    _remove_pending_entry,
)
from sessionkit_inventory.model import (  # noqa: E402, F401
    _agent_identity,
    _base_agent,
    _empty_recovery,
    _shell_title,
    recovery_spec,
)
from sessionkit_inventory.providers import _arg_value, _parse_shpool_payload  # noqa: E402, F401
from sessionkit_inventory.providers_claude import (  # noqa: E402, F401
    MAX_CLAUDE_TITLE_SCAN_BYTES,
    _is_native_claude,
)
from sessionkit_inventory.providers_codex import (  # noqa: E402, F401
    ROLLOUT_LIFECYCLE_SEARCH_BYTES,
    ROLLOUT_TAIL_BYTES,
    _codex_state_databases,
    _codex_turn_state,
    _is_native_codex,
)
from sessionkit_inventory.common import (  # noqa: E402, F401
    GENERATED_OPERATIONAL_ID_RE,
    LEGACY_OPERATIONAL_ID_RE,
    MAX_OPERATIONAL_ID_BYTES,
    PROVIDERS,
    UUID_RE,
    CollectionError,
    _load_json_file,
    _positive_float,
    _positive_int,
    _utc_now,
    _valid_aliases,
    _valid_automatic_title_failures,
    _valid_automatic_titles,
    automatic_naming_enabled,
    clean_text,
    default_runner,
    display_shpool_id,
    natural_name_key,
    normalize_automatic_title,
    shpool_id_mutation_policy,
    valid_uuid,
)
from sessionkit_inventory.state_io import (  # noqa: E402, F401
    StateLock,
    _atomic_json_bytes,
    _launch_fields,
    _read_bounded_owner_file,
    _read_private_launch_file,
    _state_paths,
    _terminal_generation_key,
    atomic_write_json,
)
from sessionkit_inventory.processes import (  # noqa: E402, F401
    DARWIN_PLATFORM,
    _children_index,
    _DarwinBsdInfo,
    _DarwinTimeval,
    _darwin_bsd_info,
    _darwin_generation,
    _darwin_procargs2,
    _decode_c_string,
    _is_shpool_daemon,
    _parse_darwin_procargs2,
    _proc_environ,
    _proc_stat,
    _process_age,
    _process_ancestor_chain,
    descendants,
)


SCHEMA_VERSION = 1
MAX_PRIVATE_JSON_BYTES = 1024 * 1024
MAX_CODEX_SESSION_INDEX_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_PROC_NODES = 16384
ABSENT_ALIAS_CONFIG_BACKUP = b"session-kit-alias-config-absent-v1\n"
Runner = Callable[[Sequence[str], float], str]

def _runtime_platform() -> str:
    """Return the real platform, with a test-only override for Linux fixtures."""
    return _processes._runtime_platform(
        environ=os.environ,
        platform_name=sys.platform,
    )


def _require_supported_platform() -> str:
    return _processes._require_supported_platform(
        runtime_platform=_runtime_platform,
    )


def _home() -> Path:
    return _common._home(environ=os.environ, home_factory=Path.home)


def _xdg_path(env_name: str, fallback: Path) -> Path:
    return _common._xdg_path(env_name, fallback, environ=os.environ)


def config_path() -> Path:
    return _common.config_path(
        environ=os.environ,
        home=_home,
        xdg_path=_xdg_path,
    )


def default_state_dir() -> Path:
    return _common.default_state_dir(
        environ=os.environ,
        home=_home,
        xdg_path=_xdg_path,
    )


def _scoped_config_path(environ: Mapping[str, str] | None) -> Path | None:
    """Resolve the naming document inside the caller's own environment.

    An explicit environment IS the caller's sandbox — the automatic namers
    run inside one, and resolving the real home from in there would read (and
    write) name ownership outside the sandbox that asked for the work.
    """
    if environ is None:
        return config_path()
    env = dict(environ)
    if not (
        env.get("SESSION_KIT_CONFIG") or env.get("XDG_CONFIG_HOME") or env.get("HOME")
    ):
        return None
    return _common.config_path(
        environ=env,
        home=lambda: _common._home(environ=env, home_factory=Path.home),
        xdg_path=lambda name, fallback: _common._xdg_path(
            name, fallback, environ=env
        ),
    )


def _scoped_state_config(environ: Mapping[str, str] | None) -> dict[str, Any] | None:
    """The state root the caller's own environment locks its naming writes in."""
    if environ is None:
        return load_config()
    env = dict(environ)
    if not (
        env.get("SESSION_KIT_STATE_DIR")
        or env.get("XDG_STATE_HOME")
        or env.get("HOME")
    ):
        return None
    return {
        "schema_version": SCHEMA_VERSION,
        "state_dir": _common.default_state_dir(
            environ=env,
            home=lambda: _common._home(environ=env, home_factory=Path.home),
            xdg_path=lambda name, fallback: _common._xdg_path(
                name, fallback, environ=env
            ),
        ),
    }


def record_pushed_title(
    provider: str, uuid: str, title: str, *, environ: Mapping[str, str] | None = None
) -> None:
    """Remember what the kit just wrote into a provider's own store.

    Fail-open: naming is advisory, and a thread with no record simply falls
    back to its retained automatic title as the last kit-authored value.
    """
    path = _scoped_config_path(environ)
    config = _scoped_state_config(environ)
    if path is None or config is None:
        return
    try:
        _names.record_pushed_title(
            config,
            provider,
            uuid,
            title,
            atomic_write_json=atomic_write_json,
            config_path=lambda: path,
            private_alias_document=_private_alias_document,
            private_alias_parent=_private_alias_parent,
        )
    except (CollectionError, OSError):
        return


def adopt_native_rename(
    provider: str,
    uuid: str,
    native_title: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Take a provider-native rename as the permanent human-owned name."""
    path = _scoped_config_path(environ)
    config = _scoped_state_config(environ)
    if path is None or config is None:
        return ""
    try:
        return _names.adopt_native_rename(
            config,
            provider,
            uuid,
            native_title,
            atomic_write_json=atomic_write_json,
            config_path=lambda: path,
            private_alias_document=_private_alias_document,
            private_alias_parent=_private_alias_parent,
        )
    except (CollectionError, OSError):
        return ""


def name_owner(
    provider: str, uuid: str, *, environ: Mapping[str, str] | None = None
) -> str:
    """Say who owns a session's name: human, automatic, or nobody ("").

    Fails open: a document that cannot be read owns nothing, because a
    naming pass that cannot prove a name is taken is exactly the pass that
    should keep looking rather than refuse to work at all.
    """
    exact = _common.valid_uuid(uuid)
    if provider not in PROVIDERS or not exact:
        return ""
    path = _scoped_config_path(environ)
    if path is None:
        return ""
    try:
        document = _private_alias_document(path, allow_missing=True)
    except (CollectionError, OSError):
        return ""
    return _names._name_owner(document, f"{provider}:{exact}")


def human_named_keys(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """Every session a person renamed, as provider:uuid keys."""
    path = _scoped_config_path(environ)
    if path is None:
        return frozenset()
    try:
        document = _private_alias_document(path, allow_missing=True)
    except (CollectionError, OSError):
        return frozenset()
    keys = {
        key
        for key, record in _common._valid_name_ownership(
            document.get("name_ownership")
        ).items()
        if record["owner"] == "human"
    }
    # Documents written before the ownership record still hold the evidence.
    keys.update(
        key
        for key in _names._valid_aliases(document.get("aliases"))
        if _names._name_owner(document, key) == "human"
    )
    return frozenset(keys)


def claim_automatic_name(
    provider: str, uuid: str, *, environ: Mapping[str, str] | None = None
) -> str:
    """Take the one-shot automatic claim on a session's name.

    Returns "" when the claim landed, or the reason it did not. Naming is
    advisory work that must never break a session, so every failure is
    reported rather than raised.
    """
    exact = _common.valid_uuid(uuid)
    if provider not in PROVIDERS or not exact:
        return "invalid automatic name claim request"
    path = _scoped_config_path(environ)
    config = _scoped_state_config(environ)
    if path is None or config is None:
        return "caller environment has no home; automatic name not claimed"
    try:
        return _names.claim_automatic_name(
            config,
            provider,
            exact,
            atomic_write_json=atomic_write_json,
            config_path=lambda: path,
            private_alias_document=_private_alias_document,
            private_alias_parent=_private_alias_parent,
        )
    except (CollectionError, OSError) as exc:
        return f"automatic name not claimed: {exc}"


def default_journal_dir() -> Path:
    return _common.default_journal_dir(
        environ=os.environ,
        home=_home,
        xdg_path=_xdg_path,
    )


def default_journal_recovery_dir() -> Path:
    return _common.default_journal_recovery_dir(
        environ=os.environ,
        home=_home,
        xdg_path=_xdg_path,
    )


def default_start_dir() -> Path:
    return _common.default_start_dir(
        environ=os.environ,
        home=_home,
        xdg_path=_xdg_path,
    )


def load_config() -> dict[str, Any]:
    """Load and validate configuration, with safe defaults."""
    config = _common.load_config(
        config_path=config_path,
        load_json_file=_load_json_file,
        default_state_dir=default_state_dir,
        positive_float=_positive_float,
        positive_int=_positive_int,
        valid_aliases=_valid_aliases,
        valid_automatic_titles=_valid_automatic_titles,
        valid_automatic_title_failures=_valid_automatic_title_failures,
        schema_version=SCHEMA_VERSION,
        default_max_proc_nodes=DEFAULT_MAX_PROC_NODES,
    )
    # Session colors ride the same document; the pinned kernel contract
    # predates them, so they are validated here at the facade layer.
    path = config_path()
    raw: Any = {}
    if path.is_file():
        try:
            raw = _load_json_file(path)
        except (OSError, ValueError):
            raw = {}
    config["colors"] = _valid_colors(
        raw.get("colors") if isinstance(raw, Mapping) else None
    )
    # So does the record of what the kit last pushed into each provider's own
    # store — the collector needs it to tell a native /rename from its own echo.
    config["pushed_titles"] = _common._valid_pushed_titles(
        raw.get("pushed_titles") if isinstance(raw, Mapping) else None
    )
    config["name_ownership"] = _common._valid_name_ownership(
        raw.get("name_ownership") if isinstance(raw, Mapping) else None
    )
    return config


def _command_from_env(env_name: str, default: str) -> list[str]:
    return _common._command_from_env(env_name, default, environ=os.environ)


def _command_json(
    *,
    fixture_env: str,
    command_env: str,
    default_command: Sequence[str],
    runner: Runner,
    timeout: float,
) -> Any:
    return _common._command_json(
        fixture_env=fixture_env,
        command_env=command_env,
        default_command=default_command,
        runner=runner,
        timeout=timeout,
        environ=os.environ,
        load_json_file=_load_json_file,
        command_from_env=_command_from_env,
    )


def _parse_claude_payload(payload: Any) -> list[dict[str, Any]]:
    return _providers_claude._parse_claude_payload(payload, palette=CLAUDE_SESSION_COLORS)


def read_claude_transcript_signals(
    uuid: str, home: Path | None = None
) -> dict[str, str]:
    """Return Claude's persisted per-conversation title and color evidence."""
    return _providers_claude.read_claude_transcript_signals(
        uuid,
        home,
        environ=os.environ,
        home_factory=Path.home,
        palette=CLAUDE_SESSION_COLORS,
    )


def read_claude_ai_title(uuid: str, home: Path | None = None) -> str:
    """Compatibility wrapper: the auto-title half of the transcript signals."""
    return _providers_claude.read_claude_ai_title(
        uuid,
        home,
        transcript_signals=read_claude_transcript_signals,
    )


def _enrich_claude_payload(payload: Any) -> Any:
    """Attach per-session nameSource and ai-title evidence for the collector."""
    return _providers_claude._enrich_claude_payload(
        payload,
        environ=os.environ,
        home_factory=Path.home,
        palette=CLAUDE_SESSION_COLORS,
        transcript_signals=read_claude_transcript_signals,
    )


def scan_process_table(proc_root: Path, max_nodes: int) -> dict[int, dict[str, Any]]:
    """Read one bounded process-table view from proc_root."""
    def inventory_environ(path: Path) -> dict[str, str]:
        values = _proc_environ(path)
        return {
            key: values[key]
            for key in (
                "SHPOOL_SESSION_NAME",
                "SESSION_KIT_REQUESTED_MODEL",
                "SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY",
                "CLAUDE_CONFIG_DIR",
                "CODEX_HOME",
                "SESSION_KIT_ACCOUNT_ALIAS",
                "SESSION_KIT_ACCOUNT_CAPABLE",
            )
            if key in values
        }

    table = _processes.scan_process_table(
        proc_root,
        max_nodes,
        proc_stat=_proc_stat,
        proc_environ=inventory_environ,
    )
    for pid, row in table.items():
        try:
            values = inventory_environ(proc_root / str(pid) / "environ")
        except OSError:
            continue
        row["requested_model"] = values.get("SESSION_KIT_REQUESTED_MODEL", "")
        row["launch_idempotency_key"] = values.get(
            "SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY", ""
        )
    return table


def scan_darwin_process_table(
    max_nodes: int,
    *,
    pids: Sequence[int] | None = None,
    bsd_reader: Callable[[int], _DarwinBsdInfo] | None = None,
    args_reader: Callable[[int], bytes] | None = None,
) -> dict[int, dict[str, Any]]:
    """Read one bounded Darwin process view using libproc and KERN_PROCARGS2."""
    return _processes.scan_darwin_process_table(
        max_nodes,
        pids=pids,
        bsd_reader=bsd_reader,
        args_reader=args_reader,
        darwin_libraries=_darwin_libraries,
        darwin_bsd_info=_darwin_bsd_info,
        darwin_procargs2=_darwin_procargs2,
        darwin_generation=_darwin_generation,
        parse_darwin_procargs2=_parse_darwin_procargs2,
        decode_c_string=_decode_c_string,
    )


def _darwin_libraries() -> tuple[Any, Any]:
    return _processes._darwin_libraries(runtime_platform=_runtime_platform)


def platform_process_table(
    proc_root: Path, max_nodes: int
) -> dict[int, dict[str, Any]]:
    return _processes.platform_process_table(
        proc_root,
        max_nodes,
        require_supported_platform=_require_supported_platform,
        darwin_process_table=scan_darwin_process_table,
        linux_process_table=scan_process_table,
    )


def shpool_roots(
    session_names: Iterable[str], process_table: Mapping[int, Mapping[str, Any]]
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Map a shpool name only through a unique daemon direct child."""
    return _processes.shpool_roots(
        session_names,
        process_table,
        is_shpool_daemon=_is_shpool_daemon,
    )


def daemon_generation(
    process_table: Mapping[int, Mapping[str, Any]]
) -> dict[str, int] | None:
    return _processes.daemon_generation(
        process_table,
        is_shpool_daemon=_is_shpool_daemon,
    )


def claude_subagents(
    root_uuid: str, process_table: Mapping[int, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return _providers_claude.claude_subagents(
        root_uuid,
        process_table,
        arg_value=_arg_value,
    )


def _is_codex_app_server(process: Mapping[str, Any]) -> bool:
    """Recognize the native Codex process that owns remote TUI threads."""
    return _providers_codex._is_codex_app_server(
        process,
        is_native_codex=_is_native_codex,
    )


def codex_refresh_target(
    process_table: Mapping[int, Mapping[str, Any]],
    shell_pid: int,
    shell_generation: int,
    provider_pid: int,
    provider_generation: int,
) -> tuple[int, int]:
    """Prove the exact Codex process a title refresh may restart."""
    return _providers_codex.codex_refresh_target(
        process_table,
        shell_pid,
        shell_generation,
        provider_pid,
        provider_generation,
        is_native_codex=_is_native_codex,
        is_codex_app_server=_is_codex_app_server,
        children_index=_children_index,
        descendants=descendants,
        arg_value=_arg_value,
        max_proc_nodes=DEFAULT_MAX_PROC_NODES,
    )


def _native_claude_uuid(process: Mapping[str, Any]) -> str | None:
    return _providers_claude._native_claude_uuid(
        process,
        is_native_claude=_is_native_claude,
        arg_value=_arg_value,
    )


def _rollout_turn_state(descriptor: int) -> str:
    """Compatibility facade for the provider-specific structured parser."""
    return _providers_codex.rollout_turn_state(descriptor)


def _rollout_meta_fd(descriptor: int) -> dict[str, Any] | None:
    """Parse bounded rollout metadata from an already-open descriptor."""
    return _providers_codex._rollout_meta_fd(
        descriptor,
        rollout_turn_state=_rollout_turn_state,
    )


def _expected_proc_identity(
    proc_root: Path, pid: int, expected_process: Mapping[str, Any]
) -> tuple[int, int, int] | None:
    return _processes._expected_proc_identity(
        proc_root,
        pid,
        expected_process,
        proc_stat=_proc_stat,
    )


def codex_open_rollouts(
    pid: int,
    proc_root: Path,
    codex_home: Path,
    expected_process: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Read metadata through stable, native Codex-owned proc descriptors."""
    return _providers_codex.codex_open_rollouts(
        pid,
        proc_root,
        codex_home,
        expected_process,
        expected_proc_identity=_expected_proc_identity,
        rollout_meta_fd=_rollout_meta_fd,
    )


def codex_rollout_by_uuid(
    codex_home: Path,
    uuid: str,
    *,
    max_files: int = 10_000,
) -> list[dict[str, Any]]:
    """Read one exact Darwin Codex rollout selected only by its process UUID."""
    return _providers_codex.codex_rollout_by_uuid(
        codex_home,
        uuid,
        max_files=max_files,
        rollout_meta_fd=_rollout_meta_fd,
    )


def codex_rollout_state_by_path(codex_home: Path, rollout_path: object) -> str:
    """Read one child thread's state from an owner-controlled Codex rollout."""
    return _providers_codex.codex_rollout_state_by_path(
        codex_home,
        rollout_path,
        rollout_turn_state=_rollout_turn_state,
    )


def index_codex_processes(
    process_table: Mapping[int, Mapping[str, Any]], proc_root: Path, codex_home: Path
) -> dict[int, list[dict[str, Any]]]:
    return _providers_codex.index_codex_processes(
        process_table,
        proc_root,
        codex_home,
        runtime_platform=_runtime_platform,
        darwin_platform=DARWIN_PLATFORM,
        is_native_codex=_is_native_codex,
        rollout_by_uuid=codex_rollout_by_uuid,
        open_rollouts=codex_open_rollouts,
    )


def _root_codex_uuid(
    process: Mapping[str, Any], metadata: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Return exact root threads owned by one native Codex process."""
    return _providers_codex._root_codex_uuid(
        process,
        metadata,
        is_codex_app_server=_is_codex_app_server,
    )


def _codex_home(
    environ: Mapping[str, str] | None = None, *, default_home: Path | None = None
) -> Path:
    """Resolve one Codex home consistently for reads and writes."""
    return _providers_codex._codex_home(
        environ,
        default_home=default_home,
        process_environ=os.environ,
        home=_home,
    )


def _codex_paths() -> tuple[Path, Path]:
    return _providers_codex._codex_paths(
        environ=os.environ,
        codex_home=_codex_home,
        codex_state_databases=_codex_state_databases,
    )


def read_codex_session_index(
    path: Path, warning_sink: list[str] | None = None
) -> dict[str, str]:
    """Read exact UUID-bound Codex names from the append-only local index."""
    return _providers_codex.read_codex_session_index(
        path,
        warning_sink,
        read_bounded_owner_file=_read_bounded_owner_file,
        max_bytes=MAX_CODEX_SESSION_INDEX_BYTES,
    )


def read_codex_db(
    db_path: Path,
    session_index_names: Mapping[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Read titles and spawn edges from Codex state without creating journal files."""
    return _providers_codex.read_codex_db(
        db_path,
        session_index_names,
        session_index_reader=read_codex_session_index,
        rollout_state_by_path=codex_rollout_state_by_path,
    )


def _provider_title(
    provider: str,
    uuid: str | None,
    native_title: str,
    aliases: Mapping[str, str],
    cwd: str = "",
    started_at_unix_ms: int | None = None,
    automatic_titles: Mapping[str, str] | None = None,
    *,
    provider_title_is_explicit: bool = True,
    pushed_titles: Mapping[str, str] | None = None,
    name_ownership: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    return _model._provider_title(
        provider,
        uuid,
        native_title,
        aliases,
        cwd,
        started_at_unix_ms,
        automatic_titles,
        provider_title_is_explicit=provider_title_is_explicit,
        provider_title_info=_provider_title_info,
        pushed_titles=pushed_titles,
        name_ownership=name_ownership,
    )


def _provider_title_info(
    provider: str,
    uuid: str | None,
    native_title: str,
    aliases: Mapping[str, str],
    cwd: str = "",
    started_at_unix_ms: int | None = None,
    automatic_titles: Mapping[str, str] | None = None,
    *,
    provider_title_is_explicit: bool = True,
    pushed_titles: Mapping[str, str] | None = None,
    name_ownership: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[str, str]:
    return _model._provider_title_info(
        provider,
        uuid,
        native_title,
        aliases,
        cwd,
        started_at_unix_ms,
        automatic_titles,
        provider_title_is_explicit=provider_title_is_explicit,
        context_title=_context_title,
        pushed_titles=pushed_titles,
        name_ownership=name_ownership,
    )


def _context_title(
    provider: str, cwd: str, started_at_unix_ms: int | None
) -> str:
    """Return a deterministic project hint without exposing a full path."""
    return _model._context_title(
        provider,
        cwd,
        started_at_unix_ms,
        home=_home,
    )


def _regular_file_mtime_ms(path: Path) -> int | None:
    """Return an exact regular file's mtime without following a symlink."""
    return _collector.regular_file_mtime_ms(path)


def recent_output_times(
    shpool_ids: Iterable[str],
    *,
    journal_dir: Path | None = None,
    recovery_dir: Path | None = None,
) -> dict[str, int]:
    """Map operational shpool IDs to their exact active journal mtime."""
    return _collector.recent_output_times(
        shpool_ids,
        journal_dir=journal_dir,
        recovery_dir=recovery_dir,
        journal_dir_factory=default_journal_dir,
        journal_recovery_dir_factory=default_journal_recovery_dir,
        regular_file_mtime_ms=_regular_file_mtime_ms,
    )


def apply_retained_setup_attributions(
    inventory: dict[str, Any],
    *,
    start_dir: Path | None = None,
    boot_id: str | None = None,
) -> dict[str, Any]:
    """Add display-only provider hints from exact retained startup proofs."""
    return _collector.apply_retained_setup_attributions(
        inventory,
        start_dir=start_dir,
        boot_id=boot_id,
        start_dir_factory=default_start_dir,
        boot_id_reader=_boot_id,
        launch_fields=_launch_fields,
        read_private_launch_file=_read_private_launch_file,
    )


def build_inventory(
    shpool_payload: Any,
    claude_payload: Any,
    process_table: Mapping[int, Mapping[str, Any]],
    codex_index: Mapping[int, Sequence[Mapping[str, Any]]],
    codex_db_rows: tuple[
        Mapping[str, Mapping[str, Any]], Mapping[str, Sequence[dict[str, Any]]]
    ],
    config: Mapping[str, Any],
    now: float | None = None,
    recent_output_by_shpool_id: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Pure inventory composition for fixture tests and the live collector."""
    inventory = _collector.build_inventory(
        shpool_payload,
        claude_payload,
        process_table,
        codex_index,
        codex_db_rows,
        config,
        now,
        recent_output_by_shpool_id,
        descendants=descendants,
        shpool_roots=shpool_roots,
        daemon_generation=daemon_generation,
        parse_claude_payload=_parse_claude_payload,
        native_claude_uuid=_native_claude_uuid,
        claude_subagents=claude_subagents,
        root_codex_uuid=_root_codex_uuid,
        provider_title_info=_provider_title_info,
        session_color=session_color,
        valid_colors=_valid_colors,
        adopt_launch_colors=_adopt_launch_colors,
        codex_title_echoes_prompt=_codex_title_echoes_prompt,
        schema_version=SCHEMA_VERSION,
        claude_palette=CLAUDE_SESSION_COLORS,
        default_max_proc_nodes=DEFAULT_MAX_PROC_NODES,
    )
    from sessionkit_messages.envelope import valid_idempotency_key

    children = _children_index(process_table)
    for item in inventory.get("sessions", ()):
        if not isinstance(item, dict):
            continue
        shell = item.get("shpool_shell")
        shell_pid = shell.get("pid") if isinstance(shell, Mapping) else None
        candidate_pids = (
            descendants(
                shell_pid,
                children,
                max_nodes=DEFAULT_MAX_PROC_NODES,
                max_depth=8,
            )
            if isinstance(shell_pid, int)
            else ()
        )
        evidence: set[tuple[str, str]] = set()
        for candidate_pid in candidate_pids:
            candidate = process_table.get(candidate_pid, {})
            model = candidate.get("requested_model")
            launch_key = valid_idempotency_key(
                candidate.get("launch_idempotency_key")
            )
            argv = candidate.get("cmdline")
            command_model = (
                _arg_value(argv, "--model") if isinstance(argv, list) else ""
            )
            command_names = {
                Path(str(value)).name
                for value in (argv[:2] if isinstance(argv, list) else ())
            }
            if (
                item.get("provider") in command_names
                and isinstance(model, str)
                and model
                and command_model == model
                and launch_key
            ):
                evidence.add((model, launch_key))
        if len(evidence) == 1:
            model, launch_key = next(iter(evidence))
            item["actual_model"] = model
            item["launch_idempotency_key"] = launch_key
    return inventory


def apply_provider_title_states(
    inventory: dict[str, Any], config: Mapping[str, Any]
) -> None:
    """Expose a safe, display-only state for Codex bars awaiting a restart."""
    state_dir = config.get("state_dir")
    if not state_dir:
        return
    marker_root = Path(str(state_dir)) / "provider-untitled"
    for item in inventory.get("sessions", []):
        if not isinstance(item, dict) or item.get("provider") != "codex":
            continue
        shpool_id = clean_text(item.get("shpool_id_raw"), 128)
        if not shpool_id or "/" in shpool_id or shpool_id.startswith("."):
            continue
        marker = marker_root / shpool_id
        try:
            marker_stat = marker.lstat()
        except OSError:
            item["provider_title_state"] = "ready"
            continue
        pending = statmod.S_ISREG(marker_stat.st_mode) and not marker.is_symlink()
        busy_or_attached = (
            str(item.get("availability") or "").casefold() == "attached"
            or str(item.get("agent_status") or "").casefold()
            in {"running", "working", "needs your reply", "reply optional"}
        )
        if pending and busy_or_attached:
            item["provider_title_state"] = "deferred"
        elif pending:
            item["provider_title_state"] = "pending"
        else:
            item["provider_title_state"] = "ready"


def _quarantine_orphaned_provider_untitled_markers(
    config: Mapping[str, Any], inventory: Mapping[str, Any], *, now: float | None = None
) -> list[dict[str, str]]:
    """Quarantine old markers only after fresh inventory proves absence."""
    if inventory.get("source") != "live" or inventory.get("stale") is not False:
        return []
    active_ids = {
        str(item.get("shpool_id_raw") or "")
        for item in inventory.get("sessions", ())
        if isinstance(item, Mapping)
    }
    paths = _state_paths(config)
    marker_root = paths["root"] / "provider-untitled"
    try:
        root_metadata = marker_root.lstat()
        if (
            not statmod.S_ISDIR(root_metadata.st_mode)
            or marker_root.is_symlink()
            or root_metadata.st_uid != os.geteuid()
            or statmod.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            return []
        markers = sorted(marker_root.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    current = time.time() if now is None else now
    moved: list[dict[str, str]] = []
    for marker in markers:
        try:
            metadata = marker.lstat()
            if (
                marker.name in active_ids
                or current - metadata.st_mtime <= 7 * 86400
                or not statmod.S_ISREG(metadata.st_mode)
                or marker.is_symlink()
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
            ):
                continue
            quarantine = paths["provider_untitled_quarantine"]
            quarantine.mkdir(mode=0o700, parents=True, exist_ok=True)
            qstat = quarantine.lstat()
            if (
                not statmod.S_ISDIR(qstat.st_mode)
                or statmod.S_IMODE(qstat.st_mode) != 0o700
                or qstat.st_uid != os.geteuid()
            ):
                continue
            fingerprint = hashlib.sha256(
                f"{marker.name}:{metadata.st_ino}:{metadata.st_mtime_ns}".encode()
            ).hexdigest()[:12]
            destination = quarantine / f"{int(current)}-{fingerprint}-{marker.name}"
            if destination.exists() or destination.is_symlink():
                continue
            os.replace(marker, destination)
            moved.append(
                {
                    "marker": marker.name,
                    "quarantine": os.fspath(destination),
                    "reason": "fresh inventory proved session absent after 7 days",
                }
            )
        except OSError:
            continue
    return moved


def _shpool_executable() -> str:
    """Resolve shpool for contexts (systemd user services) whose PATH omits it."""
    return _providers._shpool_executable(home_factory=Path.home)


def collect_live(
    config: Mapping[str, Any],
    *,
    runner: Runner | None = None,
    proc_root: Path | None = None,
) -> dict[str, Any]:
    """Collect a live inventory using one call per external list command."""
    inventory = _collector.collect_live(
        config,
        runner=runner,
        proc_root=proc_root,
        environ=os.environ,
        default_runner=default_runner,
        command_json=_command_json,
        shpool_executable=_shpool_executable,
        platform_process_table=platform_process_table,
        default_max_proc_nodes=DEFAULT_MAX_PROC_NODES,
        enrich_claude_payload=_enrich_claude_payload,
        parse_claude_payload=_parse_claude_payload,
        codex_paths=_codex_paths,
        index_codex_processes=index_codex_processes,
        read_codex_session_index=read_codex_session_index,
        read_codex_db=read_codex_db,
        recent_output_times=recent_output_times,
        build_inventory=build_inventory,
        apply_provider_title_states=apply_provider_title_states,
        apply_retained_setup_attributions=apply_retained_setup_attributions,
    )
    return _apply_account_bindings(inventory, config)


def _read_state_json(path: Path) -> Any:
    return _state_io._read_state_json(path, load_json_file=_load_json_file)


def _alias_document_from_bytes(payload: bytes, *, label: str) -> dict[str, Any]:
    return _names._alias_document_from_bytes(
        payload,
        label=label,
        schema_version=SCHEMA_VERSION,
    )


def _private_alias_document(path: Path, *, allow_missing: bool) -> dict[str, Any]:
    return _names._private_alias_document(
        path,
        allow_missing=allow_missing,
        max_private_json_bytes=MAX_PRIVATE_JSON_BYTES,
        schema_version=SCHEMA_VERSION,
        alias_document_from_bytes=_alias_document_from_bytes,
    )


def _private_alias_parent(path: Path) -> None:
    return _names._private_alias_parent(
        path,
    )


def canonical_aliases(config: Mapping[str, Any]) -> dict[str, str]:
    return _names.canonical_aliases(
        config,
        config_path=config_path,
        private_alias_document=_private_alias_document,
    )


def mutate_canonical_alias(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    title: str | None,
) -> dict[str, str]:
    return _names.mutate_canonical_alias(
        config,
        provider,
        uuid,
        title,
        atomic_write_json=atomic_write_json,
        config_path=config_path,
        private_alias_document=_private_alias_document,
        private_alias_parent=_private_alias_parent,
    )


def canonical_automatic_titles(config: Mapping[str, Any]) -> dict[str, str]:
    return _names.canonical_automatic_titles(
        config,
        config_path=config_path,
        private_alias_document=_private_alias_document,
    )


def canonical_name_ownership(
    config: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    """Read who owns every named session, human renames included."""
    return _names.canonical_name_ownership(
        config,
        config_path=config_path,
        private_alias_document=_private_alias_document,
    )


def mutate_canonical_automatic_title(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    title: str | None,
    *,
    overwrite: bool = False,
    revalidate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Atomically set/reset one automatic title without crossing alias provenance."""
    return _names.mutate_canonical_automatic_title(
        config,
        provider,
        uuid,
        title,
        overwrite=overwrite,
        revalidate=revalidate,
        schema_version=SCHEMA_VERSION,
        atomic_write_json=atomic_write_json,
        config_path=config_path,
        private_alias_document=_private_alias_document,
        private_alias_parent=_private_alias_parent,
    )


def mutate_canonical_self_name(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    title: str,
    *,
    revalidate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Atomically set the paired alias and automatic title for a proved caller."""
    return _names.mutate_canonical_self_name(
        config,
        provider,
        uuid,
        title,
        revalidate=revalidate,
        schema_version=SCHEMA_VERSION,
        atomic_write_json=atomic_write_json,
        config_path=config_path,
        private_alias_document=_private_alias_document,
        private_alias_parent=_private_alias_parent,
    )


def record_automatic_title_failure(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    *,
    revalidate: Callable[[], None] | None = None,
) -> int:
    """Record at most two proved root-turn naming failures."""
    return _names.record_automatic_title_failure(
        config,
        provider,
        uuid,
        revalidate=revalidate,
        atomic_write_json=atomic_write_json,
        config_path=config_path,
        private_alias_document=_private_alias_document,
        private_alias_parent=_private_alias_parent,
    )


def _exact_active_title_keys(inventory: Mapping[str, Any]) -> set[str]:
    return _names._exact_active_title_keys(
        inventory,
    )


def _automatic_title_prune_plan(
    titles: Mapping[str, str], active_keys: set[str]
) -> tuple[list[str], str]:
    return _names._automatic_title_prune_plan(
        titles,
        active_keys,
        schema_version=SCHEMA_VERSION,
    )


def audit_automatic_titles(
    config: Mapping[str, Any], inventory: Mapping[str, Any]
) -> dict[str, Any]:
    return _names.audit_automatic_titles(
        config,
        inventory,
        schema_version=SCHEMA_VERSION,
        automatic_title_prune_plan=_automatic_title_prune_plan,
        exact_active_title_keys=_exact_active_title_keys,
        canonical_automatic_titles_reader=canonical_automatic_titles,
    )


def prune_automatic_titles(
    config: Mapping[str, Any],
    inventory: Mapping[str, Any],
    prune_token: str,
) -> dict[str, Any]:
    """Apply only the exact orphan set previously exposed by a dry-run token."""
    return _names.prune_automatic_titles(
        config,
        inventory,
        prune_token,
        schema_version=SCHEMA_VERSION,
        atomic_write_json=atomic_write_json,
        config_path=config_path,
        automatic_title_prune_plan=_automatic_title_prune_plan,
        exact_active_title_keys=_exact_active_title_keys,
        private_alias_document=_private_alias_document,
        private_alias_parent=_private_alias_parent,
    )


def propagate_provider_color(
    provider: str,
    uuid: str,
    color: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One-shot push of a session color into provider-native storage."""
    return _names.propagate_provider_color(
        provider,
        uuid,
        color,
        environ=environ,
        palette=palette_for_provider(provider),
        push_claude_color=_push_claude_color,
    )


def propagate_provider_title(
    provider: str,
    uuid: str,
    title: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One-shot push of an assigned name into the provider's own surfaces."""
    return _names.propagate_provider_title(
        provider,
        uuid,
        title,
        environ=environ,
        codex_home=_codex_home,
        push_claude_title=_push_claude_title,
        push_codex_title=_push_codex_title,
        session_kit_state_dir=_session_kit_state_dir,
        record_pushed=record_pushed_title,
    )


def _record_provider_title_retry(
    config: Mapping[str, Any], provider: str, uuid: str, title: str
) -> None:
    return _names._record_provider_title_retry(
        config,
        provider,
        uuid,
        title,
        schema_version=SCHEMA_VERSION,
        read_state_json=_read_state_json,
        atomic_write_json=atomic_write_json,
    )


def _provider_title_retry_disposition(
    provider: str, uuid: str, title: str, environ: Mapping[str, str]
) -> str:
    """Return retry, satisfied, superseded, or defer from native evidence."""
    return _names._provider_title_retry_disposition(
        provider,
        uuid,
        title,
        environ,
        codex_home=_codex_home,
        codex_title_echoes_prompt=_codex_title_echoes_prompt,
        home_resolver=_home,
        transcript_signals=read_claude_transcript_signals,
        read_session_index=read_codex_session_index,
    )


def _reconcile_pending_provider_titles(
    config: Mapping[str, Any], provider: str, environ: Mapping[str, str], limit: int = 4
) -> list[dict[str, Any]]:
    """Retry a bounded number of exact failed self-name provider pushes."""
    return _names._reconcile_pending_provider_titles(
        config,
        provider,
        environ,
        limit,
        schema_version=SCHEMA_VERSION,
        read_state_json=_read_state_json,
        atomic_write_json=atomic_write_json,
        retry_disposition=_provider_title_retry_disposition,
        propagate_title=propagate_provider_title,
        human_named=human_named_keys,
    )


def claude_pending_native_hydrations(
    config: dict[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Fill absent visible records for exact managed Claude sessions."""
    return _names.claude_pending_native_hydrations(
        config,
        environ,
        canonical_colors=canonical_colors,
        guard_inventory=guard_live_inventory,
        load_config=load_config,
        transcript_signals=read_claude_transcript_signals,
        session_color=session_color,
        snapshot_inventory=snapshot,
        propagate_color=propagate_provider_color,
        propagate_title=propagate_provider_title,
        reconcile_pending_titles=_reconcile_pending_provider_titles,
        human_named=human_named_keys,
        adopt_native=adopt_native_rename,
    )


def _push_claude_title(home: Path, uuid: str, title: str) -> tuple[list[str], list[str]]:
    return _names_push._push_claude_title(
        home,
        uuid,
        title,
        atomic_write_json=atomic_write_json,
        max_session_records=MAX_CLAUDE_SESSION_RECORDS,
    )


def _codex_title_echoes_prompt(title: str, first_message: str) -> bool:
    """True when a stored title is just the first prompt (or a prefix cut)."""
    return _names_push._codex_title_echoes_prompt(
        title,
        first_message,
    )


def codex_bounce_prepare(
    uuid: str, codex_root: Path | None = None, now: float | None = None
) -> str:
    """Resolve the real name a bounced Codex process should boot under."""
    return _names_push.codex_bounce_prepare(
        uuid,
        codex_root,
        now,
        codex_home=_codex_home,
        max_session_index_bytes=MAX_CODEX_SESSION_INDEX_BYTES,
    )


def claude_bounce_prepare(
    uuid: str, home: Path | None = None, now: float | None = None
) -> tuple[str, bool]:
    """Decide whether a Claude window can only be named by a restart."""
    return _names_push.claude_bounce_prepare(
        uuid,
        home,
        now,
    )


def codex_pending_auto_titles(
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Kit-side auto-titler for Codex threads nobody has named."""
    return _names_push.codex_pending_auto_titles(
        environ,
        codex_paths=_codex_paths,
        reconcile_pending_titles=_reconcile_pending_provider_titles,
        derive_title=derive_prompt_title,
        load_config=load_config,
        max_session_index_bytes=MAX_CODEX_SESSION_INDEX_BYTES,
        push_live_rename=_push_codex_live_rename,
        name_owner=name_owner,
        claim_name=claim_automatic_name,
        adopt_native=adopt_native_rename,
    )


def _append_codex_index_entry(index: Path, uuid: str, title: str) -> None:
    return _names_push._append_codex_index_entry(
        index,
        uuid,
        title,
    )


def _push_codex_thread_title(
    codex_root: Path, uuid: str, title: str
) -> tuple[list[str], list[str]]:
    """Set threads.title in Codex's own state database."""
    return _names_push._push_codex_thread_title(
        codex_root,
        uuid,
        title,
    )


def _session_kit_state_dir(env: Mapping[str, str], home: Path) -> Path:
    """The session-kit state directory under the caller's exact sandbox."""
    return _names_push._session_kit_state_dir(
        env,
        home,
    )


def _ws_send_frame(connection: Any, payload: bytes, opcode: int = 1) -> None:
    return _names_push._ws_send_frame(
        connection,
        payload,
        opcode,
    )


def _ws_recv_frame(connection: Any) -> tuple[int, bytes]:
    return _names_push._ws_recv_frame(
        connection,
        max_frame=MAX_CODEX_LIVE_RENAME_FRAME,
    )


def _ws_request(
    connection: Any, request_id: int, method: str, params: dict[str, Any]
) -> None:
    return _names_push._ws_request(
        connection,
        request_id,
        method,
        params,
        max_frame=MAX_CODEX_LIVE_RENAME_FRAME,
    )


def _push_codex_live_rename(
    kit_state_dir: Path, uuid: str, title: str
) -> tuple[list[str], list[str]]:
    """Repaint live app-server-backed Codex windows with the new name."""
    return _names_push._push_codex_live_rename(
        kit_state_dir,
        uuid,
        title,
        max_sockets=MAX_CODEX_LIVE_RENAME_SOCKETS,
        max_frame=MAX_CODEX_LIVE_RENAME_FRAME,
    )


def _push_codex_title(
    codex_root: Path,
    uuid: str,
    title: str,
    kit_state_dir: Path | None = None,
) -> tuple[list[str], list[str]]:
    return _names_push._push_codex_title(
        codex_root,
        uuid,
        title,
        kit_state_dir,
        max_session_index_bytes=MAX_CODEX_SESSION_INDEX_BYTES,
        push_live_rename=_push_codex_live_rename,
    )


def _push_claude_color(
    home: Path, uuid: str, color: str
) -> tuple[list[str], list[str]]:
    """Append the exact agent-color record /color itself writes."""
    return _names_push._push_claude_color(
        home,
        uuid,
        color,
    )


def _valid_colors(raw: Any) -> dict[str, str]:
    return _common._valid_colors(raw, palette_for=palette_for_provider)


def canonical_colors(config: Mapping[str, Any]) -> dict[str, str]:
    return _colors.canonical_colors(
        config,
        config_path=config_path,
        private_alias_document=_private_alias_document,
        valid_colors=_valid_colors,
    )


def palette_for_provider(provider: str) -> tuple[str, ...]:
    """The colours one provider can actually display, empty for anything else."""
    return _colors.palette_for_provider(
        provider,
        claude_palette=CLAUDE_SESSION_COLORS,
        codex_palette=CODEX_SESSION_COLORS,
    )


def session_color(
    provider: str,
    uuid: str | None,
    overrides: Mapping[str, str] | None = None,
) -> str | None:
    """Stable per-conversation color: explicit override, else identity hash."""
    return _colors.session_color(
        provider,
        uuid,
        overrides,
        palette=palette_for_provider(provider),
    )


def launch_color_for(shpool_id: str, occupied_colors: Iterable[str] = ()) -> str:
    return _colors.launch_color_for(
        shpool_id,
        occupied_colors,
        palette=CODEX_SESSION_COLORS,
    )


def _launch_color_dir(config: Mapping[str, Any]) -> Path | None:
    return _colors._launch_color_dir(config)


def _active_color_reservations(path: Path, now: float) -> dict[str, dict[str, Any]]:
    return _colors._active_color_reservations(
        path,
        now,
        read_state_json=_read_state_json,
        palette=SESSION_COLORS,
        reservation_max_age=COLOR_RESERVATION_MAX_AGE_SECONDS,
    )


def record_launch_color(
    config: Mapping[str, Any], shpool_id: str, occupied_colors: Iterable[str] = ()
) -> str | None:
    """Pick and persist a launch color for a session with no conversation ID."""
    return _colors.record_launch_color(
        config,
        shpool_id,
        occupied_colors,
        launch_color_dir=_launch_color_dir,
        launch_color=launch_color_for,
        active_color_reservations=_active_color_reservations,
        state_paths=_state_paths,
        state_lock=StateLock,
        atomic_write_json=atomic_write_json,
        schema_version=SCHEMA_VERSION,
    )


def _reserve_conversation_color(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    occupied_colors: Iterable[str],
) -> str:
    return _colors._reserve_conversation_color(
        config,
        provider,
        uuid,
        occupied_colors,
        state_paths=_state_paths,
        config_path=config_path,
        state_lock=StateLock,
        active_color_reservations=_active_color_reservations,
        color_for_session=session_color,
        free_color=first_free_color,
        private_alias_parent=_private_alias_parent,
        private_alias_document=_private_alias_document,
        valid_colors=_valid_colors,
        atomic_write_json=atomic_write_json,
        schema_version=SCHEMA_VERSION,
        palette=palette_for_provider(provider),
    )


def reconcile_session_colors(
    config: Mapping[str, Any],
    sessions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Settle every live same-provider colour collision in one idempotent pass."""
    return _colors.reconcile_conversation_colors(
        config,
        sessions,
        config_path=config_path,
        state_paths=_state_paths,
        state_lock=StateLock,
        private_alias_parent=_private_alias_parent,
        private_alias_document=_private_alias_document,
        valid_colors=_valid_colors,
        atomic_write_json=atomic_write_json,
        color_for_session=session_color,
        free_color=first_free_color,
        palette_for=palette_for_provider,
    )


def _release_conversation_color(
    config: Mapping[str, Any], provider: str, uuid: str, color: str
) -> bool:
    """Roll back one failed pre-bake only when reservation and override match."""
    return _colors._release_conversation_color(
        config,
        provider,
        uuid,
        color,
        state_paths=_state_paths,
        config_path=config_path,
        state_lock=StateLock,
        active_color_reservations=_active_color_reservations,
        private_alias_document=_private_alias_document,
        valid_colors=_valid_colors,
        atomic_write_json=atomic_write_json,
        schema_version=SCHEMA_VERSION,
        palette=palette_for_provider(provider),
    )


def _adopt_launch_colors(
    config: Mapping[str, Any],
    sessions: Sequence[Mapping[str, Any]],
    overrides: Mapping[str, str],
) -> dict[str, str]:
    """Turn launch-color markers into explicit overrides once IDs are known."""
    return _colors._adopt_launch_colors(
        config,
        sessions,
        overrides,
        launch_color_dir=_launch_color_dir,
        mutate_color=mutate_canonical_color,
        palette=CODEX_SESSION_COLORS,
        launch_color_max_age=LAUNCH_COLOR_MAX_AGE_SECONDS,
    )


def mutate_canonical_color(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    color: str | None,
) -> dict[str, str]:
    return _colors.mutate_canonical_color(
        config,
        provider,
        uuid,
        color,
        config_path=config_path,
        state_paths=_state_paths,
        state_lock=StateLock,
        private_alias_parent=_private_alias_parent,
        private_alias_document=_private_alias_document,
        valid_colors=_valid_colors,
        atomic_write_json=atomic_write_json,
        palette=palette_for_provider(provider),
    )


def derive_prompt_title(prompt: Any) -> str | None:
    """Derive a short display title from a first user prompt."""
    return _self_name.derive_prompt_title(
        prompt,
        stopwords=_TITLE_TRAILING_STOPWORDS,
    )


def _first_text_block(content: Any) -> str:
    return _self_name._first_text_block(
        content,
    )


def auto_title_from_hook(
    raw: Any, *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Title a pre-baked Claude conversation from its first real prompt."""
    return _self_name.auto_title_from_hook(
        raw,
        environ=environ,
        derive_title=derive_prompt_title,
        propagate_title=propagate_provider_title,
        max_transcript_bytes=MAX_AUTO_TITLE_TRANSCRIPT_BYTES,
        resume_continuation_text=RESUME_CONTINUATION_TEXT,
        name_owner=name_owner,
        claim_name=claim_automatic_name,
    )


def prove_self_name_caller(
    inventory: Mapping[str, Any],
    process_table: Mapping[int, Mapping[str, Any]],
    environ: Mapping[str, str],
    current_pid: int,
) -> dict[str, Any]:
    """Prove one exact managed root conversation and its current shell generation."""
    return _self_name.prove_self_name_caller(
        inventory,
        process_table,
        environ,
        current_pid,
    )


def self_name_automatic_title(
    config: Mapping[str, Any],
    title: str,
    *,
    inventory: Mapping[str, Any] | None = None,
    process_table: Mapping[int, Mapping[str, Any]] | None = None,
    environ: Mapping[str, str] | None = None,
    current_pid: int | None = None,
) -> dict[str, Any]:
    """Provider-neutral exact self-name entry point used by root agents."""
    return _self_name.self_name_automatic_title(
        config,
        title,
        inventory=inventory,
        process_table=process_table,
        environ=environ,
        current_pid=current_pid,
        default_max_proc_nodes=DEFAULT_MAX_PROC_NODES,
        record_retry=_record_provider_title_retry,
        canonical_colors=canonical_colors,
        mutate_self_name=mutate_canonical_self_name,
        normalize_title=normalize_automatic_title,
        process_table_reader=platform_process_table,
        propagate_color=propagate_provider_color,
        propagate_title=propagate_provider_title,
        prove_caller=prove_self_name_caller,
        record_title_failure=record_automatic_title_failure,
        session_color=session_color,
        snapshot_inventory=snapshot,
    )


def _strict_legacy_aliases(path: Path) -> tuple[bytes, dict[str, str]] | None:
    return _migration._strict_legacy_aliases(
        path,
        max_private_json_bytes=MAX_PRIVATE_JSON_BYTES,
        schema_version=SCHEMA_VERSION,
    )


def _create_private_backup(path: Path, payload: bytes) -> None:
    return _migration._create_private_backup(
        path,
        payload,
        max_private_json_bytes=MAX_PRIVATE_JSON_BYTES,
    )


def _alias_migration_images(
    backup_bytes: bytes, runtime_aliases: Mapping[str, str]
) -> tuple[bytes | None, dict[str, Any], bytes]:
    return _migration._alias_migration_images(
        backup_bytes,
        runtime_aliases,
        absent_alias_backup=ABSENT_ALIAS_CONFIG_BACKUP,
        schema_version=SCHEMA_VERSION,
        alias_document_from_bytes=_alias_document_from_bytes,
    )


def migrate_runtime_aliases(config: Mapping[str, Any]) -> dict[str, Any]:
    """Explicitly preserve the old effective runtime-wins values in config."""
    return _migration.migrate_runtime_aliases(
        config,
        absent_alias_backup=ABSENT_ALIAS_CONFIG_BACKUP,
        max_private_json_bytes=MAX_PRIVATE_JSON_BYTES,
        schema_version=SCHEMA_VERSION,
        private_alias_parent=_private_alias_parent,
        atomic_write_json=atomic_write_json,
        canonical_aliases=canonical_aliases,
        config_path=config_path,
        alias_migration_images_fn=_alias_migration_images,
        create_private_backup_fn=_create_private_backup,
        strict_legacy_aliases_fn=_strict_legacy_aliases,
    )


def _release_sha(value: Any) -> str:
    return _migration._release_sha(
        value,
        release_root=Path(__file__).resolve().parent.parent,
    )


def _daemon_start_epoch(
    proc_root: Path, generation: Mapping[str, Any]
) -> float:
    return _migration._daemon_start_epoch(
        proc_root,
        generation,
    )


def _legacy_identities(value: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    return _migration._legacy_identities(
        value,
    )


def _evidence_identities(value: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    return _migration._evidence_identities(
        value,
    )


def _current_identities(
    inventory: Mapping[str, Any],
) -> tuple[dict[str, tuple[str, str]], set[tuple[str, str]]]:
    return _migration._current_identities(
        inventory,
    )


def _migration_context(
    config: Mapping[str, Any],
    *,
    legacy_bytes: bytes,
    evidence_path: Path,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    proc_root: Path | None = None,
) -> dict[str, Any]:
    return _migration._migration_context(
        config,
        legacy_bytes=legacy_bytes,
        evidence_path=evidence_path,
        collector=collector,
        proc_root=proc_root,
        generation_key=_generation_key,
        has_recovery_entries=_has_recovery_entries,
        parse_private_json=_parse_private_json,
        parse_utc_timestamp=_parse_utc_timestamp,
        sha256=_sha256,
        collect_live=collect_live,
        recovery_manifest=recovery_manifest,
        strict_live_inventory=strict_live_inventory,
    )


def _plan_token(plan: Mapping[str, Any]) -> str:
    return _migration._plan_token(
        plan,
        json_bytes=_json_bytes,
        sha256=_sha256,
    )


def plan_legacy_recovery_manifest(
    config: Mapping[str, Any],
    continuity_evidence: str | Path,
    release_sha: str,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    proc_root: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Create a read-only, content-addressed migration plan."""
    return _migration.plan_legacy_recovery_manifest(
        config,
        continuity_evidence,
        release_sha,
        collector=collector,
        proc_root=proc_root,
        now=now,
        schema_version=SCHEMA_VERSION,
        json_bytes=_json_bytes,
        read_private_regular_bytes=_read_private_regular_bytes,
        sha256=_sha256,
        migration_context_fn=_migration_context,
        plan_token_fn=_plan_token,
        verify_release_sha=_release_sha,
    )


def publish_legacy_migration_plan(
    config: Mapping[str, Any], output: str | Path, plan: Mapping[str, Any]
) -> dict[str, Any]:
    """Durably create a reviewed plan as a private, non-overwriting state file."""
    return _migration.publish_legacy_migration_plan(
        config,
        output,
        plan,
        schema_version=SCHEMA_VERSION,
        json_bytes=_json_bytes,
        read_private_regular_bytes=_read_private_regular_bytes,
        write_manifest_backup=_write_legacy_manifest_backup,
    )


def _validate_migration_plan(
    plan: Mapping[str, Any], paths: Mapping[str, Path], release_sha: str
) -> None:
    return _migration._validate_migration_plan(
        plan,
        paths,
        release_sha,
        schema_version=SCHEMA_VERSION,
        generation_key=_generation_key,
        json_bytes=_json_bytes,
        sha256=_sha256,
        plan_token_fn=_plan_token,
        verify_release_sha=_release_sha,
    )


def _receipt_path(paths: Mapping[str, Path]) -> Path:
    return _migration._receipt_path(
        paths,
    )


def _migration_receipt(
    plan: Mapping[str, Any], phase: str, *, now: float | None = None
) -> dict[str, Any]:
    return _migration._migration_receipt(
        plan,
        phase,
        now=now,
        schema_version=SCHEMA_VERSION,
    )


def _read_matching_receipt(
    path: Path, plan: Mapping[str, Any], *, required: bool
) -> Mapping[str, Any] | None:
    return _migration._read_matching_receipt(
        path,
        plan,
        required=required,
        schema_version=SCHEMA_VERSION,
        parse_private_json=_parse_private_json,
        parse_utc_timestamp=_parse_utc_timestamp,
    )


def _revalidate_plan_context(
    config: Mapping[str, Any],
    plan: Mapping[str, Any],
    legacy_bytes: bytes,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None,
    proc_root: Path | None,
) -> None:
    return _migration._revalidate_plan_context(
        config,
        plan,
        legacy_bytes,
        collector=collector,
        proc_root=proc_root,
        json_bytes=_json_bytes,
        sha256=_sha256,
        migration_context_fn=_migration_context,
    )


def apply_legacy_recovery_manifest(
    config: Mapping[str, Any],
    plan_path: str | Path,
    release_sha: str,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    proc_root: Path | None = None,
) -> dict[str, Any]:
    """Apply or safely resume one reviewed legacy migration plan."""
    return _migration.apply_legacy_recovery_manifest(
        config,
        plan_path,
        release_sha,
        collector=collector,
        proc_root=proc_root,
        schema_version=SCHEMA_VERSION,
        parse_private_json=_parse_private_json,
        read_private_regular_bytes=_read_private_regular_bytes,
        sha256=_sha256,
        write_manifest_backup=_write_legacy_manifest_backup,
        atomic_write_json=atomic_write_json,
        migration_receipt_fn=_migration_receipt,
        read_matching_receipt_fn=_read_matching_receipt,
        revalidate_plan_context_fn=_revalidate_plan_context,
        validate_migration_plan_fn=_validate_migration_plan,
    )


def rollback_legacy_recovery_manifest(
    config: Mapping[str, Any],
    plan_path: str | Path,
    release_sha: str,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    proc_root: Path | None = None,
) -> dict[str, Any]:
    """Restore exact legacy bytes while every plan and generation guard holds."""
    return _migration.rollback_legacy_recovery_manifest(
        config,
        plan_path,
        release_sha,
        collector=collector,
        proc_root=proc_root,
        schema_version=SCHEMA_VERSION,
        atomic_write_private_bytes=_atomic_write_private_bytes,
        parse_private_json=_parse_private_json,
        read_private_regular_bytes=_read_private_regular_bytes,
        sha256=_sha256,
        atomic_write_json=atomic_write_json,
        migration_receipt_fn=_migration_receipt,
        read_matching_receipt_fn=_read_matching_receipt,
        revalidate_plan_context_fn=_revalidate_plan_context,
        validate_migration_plan_fn=_validate_migration_plan,
    )


def _boot_id() -> str | None:
    return _processes._boot_id(
        environ=os.environ,
        runtime_platform=_runtime_platform,
        darwin_libraries=_darwin_libraries,
    )


def _empty_terminal_registry(boot_id: str) -> dict[str, Any]:
    return _state_io._empty_terminal_registry(
        boot_id,
        schema_version=SCHEMA_VERSION,
    )


def _validate_terminal_registry(raw: Any, boot_id: str) -> dict[str, Any]:
    return _state_io._validate_terminal_registry(
        raw,
        boot_id,
        schema_version=SCHEMA_VERSION,
        empty_registry=_empty_terminal_registry,
    )


def _read_terminal_registry(
    path: Path, boot_id: str, epoch_path: Path | None = None
) -> dict[str, Any]:
    return _terminal._with_supervisor_identities(
        _state_io._read_terminal_registry(
            path,
            boot_id,
            epoch_path,
            schema_version=SCHEMA_VERSION,
            max_bytes=MAX_PRIVATE_JSON_BYTES,
            read_bounded_owner_file=_read_bounded_owner_file,
            empty_registry=_empty_terminal_registry,
            validate_registry=_validate_terminal_registry,
        ),
        path.parent / "supervisor",
    )


def _terminal_ai_key(item: Mapping[str, Any]) -> str | None:
    return _terminal._terminal_ai_key(item)


def _read_terminal_retirements(path: Path, boot_id: str) -> dict[int, float]:
    """Retirement ledger: number -> unix time its last session disappeared."""
    return _terminal._read_terminal_retirements(
        path,
        boot_id,
        max_bytes=MAX_PRIVATE_JSON_BYTES,
    )


def _terminal_retirement_payload(
    retired: Mapping[int, float], boot_id: str
) -> dict[str, Any]:
    return _terminal._terminal_retirement_payload(
        retired,
        boot_id,
        schema_version=SCHEMA_VERSION,
    )


def apply_terminal_numbers(
    inventory: dict[str, Any],
    registry: dict[str, Any],
    *,
    boot_id: str,
    allocate: bool,
    retired: dict[int, float] | None = None,
    current_time: float | None = None,
    supervisor_key: str | None = None,
    previous_supervisor_key: str | None = None,
) -> dict[str, Any]:
    """Apply boot-stable selectors without changing contiguous internal rows."""
    return _terminal.apply_terminal_numbers(
        inventory,
        registry,
        boot_id=boot_id,
        allocate=allocate,
        retired=retired,
        current_time=current_time,
        supervisor_key=(
            supervisor_key
            if supervisor_key is not None
            else getattr(registry, "supervisor_key", None)
        ),
        previous_supervisor_key=(
            previous_supervisor_key
            if previous_supervisor_key is not None
            else getattr(registry, "previous_supervisor_key", None)
        ),
        validate_registry=_validate_terminal_registry,
        schema_version=SCHEMA_VERSION,
        quarantine_seconds=TERMINAL_NUMBER_QUARANTINE_SECONDS,
    )


def recovery_manifest(inventory: Mapping[str, Any]) -> dict[str, Any]:
    return _recovery.recovery_manifest(
        inventory,
        schema_version=SCHEMA_VERSION,
        boot_id=_boot_id,
    )


def _valid_recovery_state(value: Any) -> bool:
    return _recovery._valid_recovery_state(
        value,
        schema_version=SCHEMA_VERSION,
    )


def _has_recovery_entries(value: Any) -> bool:
    return _recovery._has_recovery_entries(
        value,
        valid_recovery_state=_valid_recovery_state,
    )


def _read_private_regular_bytes(path: Path) -> bytes:
    """Read one owner-only state artifact without following a symlink."""
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CollectionError(f"cannot open recovery state {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not statmod.S_ISREG(metadata.st_mode)
            or statmod.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            raise CollectionError(
                f"recovery state must be a mode-0600 current-owner regular file: {path}"
            )
        blocks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                return b"".join(blocks)
            blocks.append(block)
    except OSError as exc:
        raise CollectionError(f"cannot read recovery state {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _write_legacy_manifest_backup(path: Path, content: bytes) -> None:
    """Atomically publish one durable, non-overwriting private backup."""
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while preserving legacy recovery manifest")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise CollectionError(
                f"legacy recovery backup already exists: {path}"
            ) from exc
        os.unlink(temporary)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_write_private_bytes(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"short write while publishing {path}")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parse_private_json(path: Path, label: str) -> tuple[bytes, Mapping[str, Any]]:
    content = _read_private_regular_bytes(path)
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectionError(f"{label} is invalid JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise CollectionError(f"{label} must be a JSON object: {path}")
    return content, value


def _parse_utc_timestamp(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise CollectionError(f"{label} must be a UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectionError(f"{label} must be a valid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise CollectionError(f"{label} must include an explicit UTC offset")
    return parsed.astimezone(dt.timezone.utc)


def update_recovery_state(paths: Mapping[str, Path], inventory: Mapping[str, Any]) -> None:
    """Preserve exact pre-generation recovery data until explicitly resolved.

    ``recovery-manifest.json`` is the latest nonempty generation.  When a new
    boot or daemon generation appears, the prior manifest is copied to
    ``recovery-pending.json`` before the current manifest can advance.  Empty
    and partial post-reboot inventories therefore cannot erase the queue.
    """
    return _recovery.update_recovery_state(
        paths,
        inventory,
        schema_version=SCHEMA_VERSION,
        recovery_manifest=recovery_manifest,
        read_state_json=_read_state_json,
        has_recovery_entries=_has_recovery_entries,
        generation_key=_generation_key,
        atomic_write_json=atomic_write_json,
    )


def source_generation_key(boot_id: Any, generation: Any) -> str | None:
    return _recovery.source_generation_key(
        boot_id,
        generation,
        generation_key=_generation_key,
    )


def flatten_pending(value: Any) -> dict[str, Any]:
    """Return one safe recovery candidate per exact provider conversation."""
    return _recovery.flatten_pending(
        value,
        schema_version=SCHEMA_VERSION,
        valid_recovery_state=_valid_recovery_state,
        source_generation_key=source_generation_key,
        pending_preferred_entry=_pending_preferred_entry,
        pending_conflict_fields=_pending_conflict_fields,
        pending_evidence=_pending_evidence,
    )


def list_pending(config: Mapping[str, Any]) -> dict[str, Any]:
    return _recovery.list_pending(
        config,
        state_paths=_state_paths,
        state_lock=StateLock,
        read_state_json=_read_state_json,
        flatten_pending=flatten_pending,
    )


def acknowledge_pending(
    config: Mapping[str, Any],
    generation_key: str,
    old_shpool_id: str,
    uuid: str,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare-and-ack one restored entry under the inventory state lock."""
    return _recovery.acknowledge_pending(
        config,
        generation_key,
        old_shpool_id,
        uuid,
        collector=collector,
        schema_version=SCHEMA_VERSION,
        state_paths=_state_paths,
        state_lock=StateLock,
        read_state_json=_read_state_json,
        valid_recovery_state=_valid_recovery_state,
        flatten_pending=flatten_pending,
        collect_live=collect_live,
        strict_live_inventory=strict_live_inventory,
        remove_pending_entry=_remove_pending_entry,
        atomic_write_json=atomic_write_json,
    )


def _cold_inventory(error: str) -> dict[str, Any]:
    return _snapshot._cold_inventory(
        error,
        schema_version=SCHEMA_VERSION,
    )


def _apply_account_bindings(
    inventory: dict[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    """Project verified profile bindings onto exact rows without guessing."""
    if "state_dir" not in config:
        return inventory
    try:
        registry = _accounts.load_registry(config)
    except (CollectionError, OSError, ValueError):
        registry = _accounts._empty_registry()
    profiles = registry.get("profiles", {})
    bindings = registry.get("bindings", {})
    for item in inventory.get("sessions", ()):
        if not isinstance(item, dict):
            continue
        provider = item.get("provider")
        identity = item.get("identity")
        exact = (
            valid_uuid(identity.get("uuid"))
            if provider in PROVIDERS and isinstance(identity, Mapping)
            else None
        )
        live_alias = clean_text(item.get("account_alias"), 12)
        if live_alias and f"{provider}:{live_alias}" not in profiles:
            item.pop("account_alias", None)
            live_alias = ""
        live_profile = profiles.get(f"{provider}:{live_alias}") if live_alias else None
        if live_alias and not isinstance(live_profile, Mapping):
            item["account_binding_mismatch"] = True
            item.pop("account_alias", None)
            live_alias = ""
        if isinstance(live_profile, Mapping):
            item["account_email"] = clean_text(live_profile.get("email"), 254)
            item["account_plan"] = clean_text(live_profile.get("plan"), 80)
        binding = bindings.get(f"{provider}:{exact}") if exact else None
        if live_alias and isinstance(binding, Mapping):
            bound_profile = profiles.get(binding.get("profile"))
            if isinstance(bound_profile, Mapping):
                bound_alias = clean_text(bound_profile.get("alias"), 12)
                if bound_alias != live_alias:
                    item["account_binding_mismatch"] = True
    return inventory


def snapshot(*, write_state: bool = True, config: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = config if config is not None else load_config()
    inventory = _snapshot.snapshot(
        write_state=write_state,
        config=settings,
        schema_version=SCHEMA_VERSION,
        load_config=load_config,
        state_paths=_state_paths,
        state_lock=StateLock,
        read_state_json=_read_state_json,
        collect_live=collect_live,
        boot_id_factory=_boot_id,
        persist_last_exact=_lifecycle.persist_last_exact,
        apply_provider_exit_states=_lifecycle.apply_provider_exit_states,
        prune_inactive_state=_lifecycle.prune_inactive_state,
        read_terminal_registry=_read_terminal_registry,
        read_terminal_retirements=_read_terminal_retirements,
        apply_terminal_numbers=apply_terminal_numbers,
        terminal_retirement_payload=_terminal_retirement_payload,
        atomic_write_json=atomic_write_json,
        quarantine_orphaned_provider_untitled_markers=(
            _quarantine_orphaned_provider_untitled_markers
        ),
        update_recovery_state=update_recovery_state,
        cold_inventory=_cold_inventory,
    )
    return inventory




def _short_path(path: str) -> str:
    return _render._short_path(path, home_factory=_home)












def render_inventory(inventory: Mapping[str, Any], rows_only: bool = False) -> str:
    return _render.render_inventory(
        inventory,
        rows_only,
        color_enabled=_color_enabled,
    )


def render_detail(
    inventory: Mapping[str, Any],
    selector: str,
    *,
    state_dir: Path | str | None = None,
) -> str:
    """One session in full, for a person to read."""
    activity: Mapping[str, Any] | None = None
    now_ms = int(time.time() * 1000)
    row = lookup(inventory, selector)
    if row is not None and state_dir is not None:
        try:
            queue = _events.build_attention_queue(
                inventory,
                Path(state_dir),
                now_ms=now_ms,
                mutate=False,
            )
            identity = row.get("identity")
            uuid = identity.get("uuid") if isinstance(identity, Mapping) else None
            key = f"{row.get('provider')}:{uuid}"
            activity = next(
                (
                    item
                    for item in queue.get("items", [])
                    if isinstance(item, Mapping) and item.get("thread_key") == key
                ),
                None,
            )
            projected = queue.get("as_of_unix_ms")
            if isinstance(projected, int) and not isinstance(projected, bool):
                now_ms = projected
        except (OSError, ValueError):
            activity = None
    return _render.render_detail(
        inventory,
        selector,
        home_factory=_home,
        activity=activity,
        now_ms=now_ms,
    )




def strict_live_inventory(inventory: Mapping[str, Any]) -> bool:
    return _validation.strict_live_inventory(
        inventory,
        guard_inventory=guard_live_inventory,
    )


def guard_live_inventory(inventory: Mapping[str, Any]) -> bool:
    return _validation.guard_live_inventory(
        inventory,
        schema_version=SCHEMA_VERSION,
    )


def load_inventory_input(path: str | Path) -> dict[str, Any]:
    return _validation.load_inventory_input(
        path,
        schema_version=SCHEMA_VERSION,
        load_json_file=_load_json_file,
    )


def _json_print(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _platform_command(args: argparse.Namespace) -> int:
    if args.platform_action == "app-server-dir":
        print(_private_app_server_dir(Path(args.state_root), args.session_id))
        return 0
    platform = _require_supported_platform()
    if args.platform_action == "timeout":
        if not 0 < args.seconds <= 3600:
            raise CollectionError("timeout seconds are outside the safe range")
        command = list(args.argv)
        if command and command[0] == "--":
            command.pop(0)
        if not command:
            raise CollectionError("timeout command is empty")
        child = subprocess.Popen(command, start_new_session=True)
        try:
            returncode = child.wait(timeout=args.seconds)
            return returncode if returncode >= 0 else 128 - returncode
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, 15)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, 9)
                child.wait()
            return 124
    if args.platform_action == "boot-id":
        value = _boot_id()
        if not value:
            raise CollectionError("current boot identity is unavailable")
        print(value)
        return 0
    if args.platform_action == "process-table":
        table = platform_process_table(
            _common.proc_root(),
            DEFAULT_MAX_PROC_NODES,
        )
        _json_print({"processes": [table[pid] for pid in sorted(table)]})
        return 0
    root = _common.proc_root()
    if platform == DARWIN_PLATFORM and args.platform_action == "process-info":
        table = scan_darwin_process_table(1, pids=[args.pid])
    else:
        table = platform_process_table(root, DEFAULT_MAX_PROC_NODES)
    if args.platform_action == "codex-refresh-target":
        pid, generation = codex_refresh_target(
            table,
            args.shell_pid,
            args.shell_generation,
            args.provider_pid,
            args.provider_generation,
        )
        print(f"{pid}\t{generation}")
        return 0
    process = table.get(args.pid)
    if process is None:
        raise CollectionError("process generation is unavailable")
    if args.platform_action == "process-info":
        print(f"{process.get('ppid', 0)}\t{process.get('start_ticks', 0)}")
        return 0
    if args.platform_action == "process-generation-is":
        if process.get("start_ticks") != args.generation:
            raise CollectionError("process generation changed")
        return 0
    if args.platform_action == "process-is":
        argv = process.get("cmdline") or []
        executable = Path(str(argv[0])).name if argv else str(process.get("comm", ""))
        if process.get("start_ticks") != args.generation or executable != args.executable:
            raise CollectionError("process executable or generation changed")
        return 0
    if process.get("start_ticks") != args.generation:
        raise CollectionError("process generation changed")
    pattern = re.compile(rf"(^|/){re.escape(args.provider)}$")
    children = _children_index(table)
    for pid in descendants(
        args.pid, children, max_nodes=DEFAULT_MAX_PROC_NODES, max_depth=8
    ):
        if pid == args.pid:
            continue
        candidate = table.get(pid, {})
        argv = candidate.get("cmdline") or []
        executable = argv[0] if argv else candidate.get("comm", "")
        if pattern.search(str(executable)):
            return 0
    raise CollectionError(f"{args.provider} descendant is unavailable")


def _alias_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    if args.alias_action == "list":
        _json_print(
            {
                "schema_version": SCHEMA_VERSION,
                "aliases": canonical_aliases(config),
            }
        )
        return 0
    if args.alias_action == "migrate-runtime":
        _json_print(migrate_runtime_aliases(config))
        return 0
    title = str(args.title) if args.alias_action == "set" else None
    aliases = mutate_canonical_alias(config, args.provider, args.uuid, title)
    payload: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "aliases": aliases}
    if args.alias_action == "set":
        assert title is not None
        # Explicit assignment pushes once into the provider's own surfaces;
        # deletion only clears the local alias and leaves provider titles alone.
        payload.update(propagate_provider_title(args.provider, args.uuid, title))
        effective_color = session_color(
            args.provider, args.uuid, canonical_colors(config)
        )
        if effective_color:
            payload.update(
                propagate_provider_color(args.provider, args.uuid, effective_color)
            )
    _json_print(payload)
    return 0


def _private_app_server_dir(state_root: Path, session_id: str) -> Path:
    """Create an App Server directory through an owner-private real-dir chain."""
    if (
        not session_id
        or "/" in session_id
        or session_id.startswith(".")
        or len(session_id) > 128
    ):
        raise CollectionError("unsafe App Server session ID")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(state_root, flags)
    except OSError as exc:
        raise CollectionError("App Server state root is unavailable") from exc
    current = state_root
    try:
        root_metadata = os.fstat(descriptor)
        if (
            not statmod.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.geteuid()
            or statmod.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise CollectionError("App Server state root must be owner mode-0700")
        for component in ("session-kit", "app-server", session_id):
            parent = descriptor
            try:
                os.mkdir(component, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            try:
                descriptor = os.open(component, flags, dir_fd=parent)
            except OSError as exc:
                raise CollectionError("unsafe App Server directory chain") from exc
            finally:
                os.close(parent)
            metadata = os.fstat(descriptor)
            if (
                not statmod.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or statmod.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise CollectionError("App Server directory chain must be owner mode-0700")
            current /= component
        return current
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _color_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    def occupied_live_colors() -> set[str] | None:
        try:
            live = snapshot(write_state=False, config=config)
        except CollectionError:
            return None
        if live.get("source") != "live" or live.get("stale") is not False:
            return None
        return {
            str(item.get("display_color"))
            for item in [*live.get("sessions", ()), *live.get("outside_agents", ())]
            if isinstance(item, Mapping) and item.get("display_color") in SESSION_COLORS
        }

    if args.color_action == "reconcile":
        live = snapshot(write_state=False, config=config)
        if live.get("source") != "live" or live.get("stale") is not False:
            print(
                "session inventory: refusing to reconcile from a stale inventory",
                file=sys.stderr,
            )
            return 1
        result = reconcile_session_colors(
            config,
            [*live.get("sessions", ()), *live.get("outside_agents", ())],
        )
        # Push each moved color the way `set` does, so a window that is already
        # open shows the new one at its next start or resume instead of waiting
        # for something else to touch it.
        pushes: list[str] = []
        warnings: list[str] = []
        for key, color in result["moved"].items():
            provider, _, uuid = key.partition(":")
            pushed = propagate_provider_color(provider, uuid, color)
            pushes.extend(pushed["provider_color_pushes"])
            warnings.extend(pushed["provider_color_warnings"])
        _json_print(
            {
                "schema_version": SCHEMA_VERSION,
                **result,
                "provider_color_pushes": pushes,
                "provider_color_warnings": warnings,
            }
        )
        return 0
    if args.color_action == "conversation-release":
        released = _release_conversation_color(
            config, args.provider, args.uuid, args.color
        )
        _json_print({"schema_version": SCHEMA_VERSION, "released": released})
        return 0 if released else 1
    if args.color_action == "launch-pick":
        occupied = occupied_live_colors()
        if occupied is None:
            return 1
        color = record_launch_color(config, args.shpool_id, occupied)
        if not color:
            return 1
        print(color)
        return 0
    if args.color_action == "conversation-pick":
        occupied = occupied_live_colors()
        if occupied is None:
            return 1
        color = _reserve_conversation_color(
            config, args.provider, args.uuid, occupied
        )
        _json_print({"schema_version": SCHEMA_VERSION, "color": color})
        return 0
    if args.color_action == "propagate":
        effective = session_color(
            args.provider, args.uuid, canonical_colors(config)
        )
        if not effective:
            return 1
        _json_print(
            {
                "schema_version": SCHEMA_VERSION,
                "color": effective,
                **propagate_provider_color(args.provider, args.uuid, effective),
            }
        )
        return 0
    if args.color_action == "effective":
        effective = session_color(
            args.provider, args.uuid, canonical_colors(config)
        )
        if not effective:
            return 1
        print(effective)
        return 0
    if args.color_action == "list":
        _json_print(
            {
                "schema_version": SCHEMA_VERSION,
                "colors": canonical_colors(config),
            }
        )
        return 0
    color = args.color if args.color_action == "set" else None
    colors = mutate_canonical_color(config, args.provider, args.uuid, color)
    payload: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "colors": colors}
    effective_color = session_color(args.provider, args.uuid, colors)
    if effective_color:
        payload.update(
            propagate_provider_color(args.provider, args.uuid, effective_color)
        )
    _json_print(payload)
    return 0


def _automatic_title_command(
    args: argparse.Namespace, config: dict[str, Any]
) -> int:
    if args.automatic_title_action == "list":
        document = _private_alias_document(config_path(), allow_missing=True)
        _json_print(
            {
                "schema_version": SCHEMA_VERSION,
                "automatic_titles": _valid_automatic_titles(
                    document.get("automatic_titles")
                ),
                "automatic_title_failures": _valid_automatic_title_failures(
                    document.get("automatic_title_failures")
                ),
                "name_ownership": canonical_name_ownership(config),
            }
        )
        return 0
    if args.automatic_title_action == "self-name":
        _json_print(self_name_automatic_title(config, args.title))
        return 0
    if args.automatic_title_action == "from-hook":
        _json_print(auto_title_from_hook(sys.stdin.read()))
        return 0
    if args.automatic_title_action == "codex-pending":
        _json_print({"titled": codex_pending_auto_titles()})
        return 0
    if args.automatic_title_action == "claude-pending":
        _json_print({"hydrated": claude_pending_native_hydrations(config)})
        return 0
    if args.automatic_title_action == "reset":
        _json_print(
            mutate_canonical_automatic_title(
                config, args.provider, args.uuid, None
            )
        )
        return 0
    live = snapshot(write_state=False, config=config)
    if args.automatic_title_action == "audit":
        _json_print(audit_automatic_titles(config, live))
        return 0
    if live.get("source") != "live" or live.get("stale") is not False:
        raise CollectionError(
            "automatic title prune requires a fresh live inventory"
        )
    _json_print(prune_automatic_titles(config, live, args.prune_token))
    return 0


def _account_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Run one owner-only account profile or switch-transaction verb."""
    action = args.account_action
    if action == "list":
        _json_print(
            {
                "schema_version": _accounts.ACCOUNT_SCHEMA_VERSION,
                "profiles": _accounts.list_profiles(config, args.provider),
            }
        )
    elif action == "choices":
        _json_print(_accounts.account_choices(config, args.provider))
    elif action == "configure-feeds":
        _json_print(
            _accounts.configure_feeds(config, args.roster_path, args.advice_path)
        )
    elif action == "adopt-default":
        _json_print(
            _accounts.adopt_default(
                config, args.provider, args.alias, args.email
            )
        )
    elif action == "enroll":
        _json_print(
            _accounts.enroll(config, args.provider, args.alias, args.email)
        )
    elif action == "verify":
        _json_print(_accounts.verify_profile(config, args.provider, args.alias))
    elif action == "binding":
        value = _accounts.binding_for(config, args.provider, args.uuid)
        if value is None:
            return 1
        _json_print(value)
    elif action == "source":
        _json_print(
            _accounts.source_profile_for_thread(config, args.provider, args.uuid)
        )
    elif action == "bind":
        _json_print(
            _accounts.bind(
                config,
                args.provider,
                args.uuid,
                args.alias,
                source=args.source,
            )
        )
    elif action == "launch-profile":
        _json_print(_accounts.launch_profile(config, args.provider, args.alias))
    elif action == "resume-profile":
        _json_print(_accounts.resume_profile(config, args.provider, args.alias))
    elif action == "sync-ui":
        item = _accounts.resume_profile(config, args.provider, args.alias)
        profile_dir = item["profile_dir"]
        if args.provider == "claude":
            os.environ["CLAUDE_CONFIG_DIR"] = profile_dir
        else:
            os.environ["CODEX_HOME"] = profile_dir
            os.environ["SESSION_KIT_CODEX_HOME"] = profile_dir
        key = f"{args.provider}:{valid_uuid(args.uuid) or ''}"
        payload: dict[str, Any] = {"provider": args.provider, "uuid": args.uuid}
        title = canonical_aliases(config).get(key)
        if title:
            payload.update(propagate_provider_title(args.provider, args.uuid, title))
        color = canonical_colors(config).get(key)
        if color:
            payload.update(propagate_provider_color(args.provider, args.uuid, color))
        _json_print(payload)
    elif action == "switch-prepare":
        _json_print(
            _accounts.prepare_switch(
                config,
                args.provider,
                args.uuid,
                args.source_alias,
                args.target_alias,
                args.cwd,
                args.shpool_id,
            )
        )
    elif action == "switch-apply":
        _json_print(_accounts.apply_switch(config, args.txid))
    elif action == "switch-commit":
        _json_print(_accounts.commit_switch(config, args.txid))
    elif action == "switch-rollback":
        _json_print(_accounts.rollback_switch(config, args.txid))
    elif action == "switch-status":
        _json_print(_accounts.transaction(config, args.txid))
    else:  # pragma: no cover - argparse owns this boundary
        raise CollectionError("unknown account action")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--no-write", action="store_true")
    snapshot_parser.add_argument("--strict-live", action="store_true")
    snapshot_parser.add_argument("--guard-live", action="store_true")
    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("--rows", action="store_true")
    render_parser.add_argument("--input")
    subparsers.add_parser("waiting-count")
    lookup_parser = subparsers.add_parser("lookup")
    lookup_parser.add_argument("selector")
    lookup_parser.add_argument("--input")
    detail_parser = subparsers.add_parser("detail")
    detail_parser.add_argument("selector")
    detail_parser.add_argument("--input")
    recovery_parser = subparsers.add_parser("recovery-command")
    recovery_parser.add_argument("provider", choices=PROVIDERS)
    recovery_parser.add_argument("uuid")
    recovery_parser.add_argument("--cwd")
    worker_model_parser = subparsers.add_parser("validate-worker-model")
    worker_model_parser.add_argument("provider", choices=PROVIDERS)
    worker_model_parser.add_argument("model")
    bounce_parser = subparsers.add_parser("codex-bounce-title")
    bounce_parser.add_argument("uuid")
    claude_bounce_parser = subparsers.add_parser("claude-bounce-title")
    claude_bounce_parser.add_argument("uuid")
    platform_parser = subparsers.add_parser("platform")
    platform_subparsers = platform_parser.add_subparsers(
        dest="platform_action", required=True
    )
    platform_subparsers.add_parser("boot-id")
    platform_subparsers.add_parser("process-table")
    platform_app_dir = platform_subparsers.add_parser("app-server-dir")
    platform_app_dir.add_argument("state_root")
    platform_app_dir.add_argument("session_id")
    platform_timeout = platform_subparsers.add_parser("timeout")
    platform_timeout.add_argument("seconds", type=float)
    platform_timeout.add_argument("argv", nargs=argparse.REMAINDER)
    platform_process = platform_subparsers.add_parser("process-info")
    platform_process.add_argument("pid", type=int)
    platform_generation = platform_subparsers.add_parser("process-generation-is")
    platform_generation.add_argument("pid", type=int)
    platform_generation.add_argument("generation", type=int)
    platform_is = platform_subparsers.add_parser("process-is")
    platform_is.add_argument("pid", type=int)
    platform_is.add_argument("generation", type=int)
    platform_is.add_argument("executable")
    platform_refresh = platform_subparsers.add_parser("codex-refresh-target")
    platform_refresh.add_argument("shell_pid", type=int)
    platform_refresh.add_argument("shell_generation", type=int)
    platform_refresh.add_argument("provider_pid", type=int)
    platform_refresh.add_argument("provider_generation", type=int)
    platform_provider = platform_subparsers.add_parser("provider-present")
    platform_provider.add_argument("pid", type=int)
    platform_provider.add_argument("generation", type=int)
    platform_provider.add_argument("provider", choices=PROVIDERS)
    alias_parser = subparsers.add_parser("alias")
    alias_subparsers = alias_parser.add_subparsers(dest="alias_action", required=True)
    alias_subparsers.add_parser("list")
    alias_subparsers.add_parser("migrate-runtime")
    alias_set = alias_subparsers.add_parser("set")
    alias_set.add_argument("provider", choices=PROVIDERS)
    alias_set.add_argument("uuid")
    alias_set.add_argument("title")
    alias_delete = alias_subparsers.add_parser("delete")
    alias_delete.add_argument("provider", choices=PROVIDERS)
    alias_delete.add_argument("uuid")
    color_parser = subparsers.add_parser("color")
    color_subparsers = color_parser.add_subparsers(
        dest="color_action", required=True
    )
    color_subparsers.add_parser("list")
    color_subparsers.add_parser("reconcile")
    color_set = color_subparsers.add_parser("set")
    color_set.add_argument("provider", choices=PROVIDERS)
    color_set.add_argument("uuid")
    color_set.add_argument("color", choices=SESSION_COLORS)
    color_delete = color_subparsers.add_parser("delete")
    color_delete.add_argument("provider", choices=PROVIDERS)
    color_delete.add_argument("uuid")
    color_effective = color_subparsers.add_parser("effective")
    color_effective.add_argument("provider", choices=PROVIDERS)
    color_effective.add_argument("uuid")
    color_propagate = color_subparsers.add_parser("propagate")
    color_propagate.add_argument("provider", choices=PROVIDERS)
    color_propagate.add_argument("uuid")
    color_launch = color_subparsers.add_parser("launch-pick")
    color_launch.add_argument("shpool_id")
    color_conversation = color_subparsers.add_parser("conversation-pick")
    color_conversation.add_argument("provider", choices=PROVIDERS)
    color_conversation.add_argument("uuid")
    color_release = color_subparsers.add_parser("conversation-release")
    color_release.add_argument("provider", choices=PROVIDERS)
    color_release.add_argument("uuid")
    color_release.add_argument("color", choices=SESSION_COLORS)
    automatic_parser = subparsers.add_parser("automatic-title")
    automatic_subparsers = automatic_parser.add_subparsers(
        dest="automatic_title_action", required=True
    )
    automatic_subparsers.add_parser("list")
    automatic_subparsers.add_parser("from-hook")
    automatic_subparsers.add_parser("codex-pending")
    automatic_subparsers.add_parser("claude-pending")
    automatic_self = automatic_subparsers.add_parser("self-name")
    automatic_self.add_argument("title")
    automatic_reset = automatic_subparsers.add_parser("reset")
    automatic_reset.add_argument("provider", choices=PROVIDERS)
    automatic_reset.add_argument("uuid")
    automatic_subparsers.add_parser("audit")
    automatic_prune = automatic_subparsers.add_parser("prune")
    automatic_prune.add_argument("--apply", dest="prune_token", required=True)
    account_parser = subparsers.add_parser("account")
    account_subparsers = account_parser.add_subparsers(
        dest="account_action", required=True
    )
    account_list = account_subparsers.add_parser("list")
    account_list.add_argument("provider", nargs="?", choices=sorted(_accounts.PROVIDERS))
    account_choices = account_subparsers.add_parser("choices")
    account_choices.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_feeds = account_subparsers.add_parser("configure-feeds")
    account_feeds.add_argument("roster_path")
    account_feeds.add_argument("advice_path")
    for account_action in ("adopt-default", "enroll"):
        account_mutation = account_subparsers.add_parser(account_action)
        account_mutation.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
        account_mutation.add_argument("alias")
        account_mutation.add_argument("email")
    account_verify = account_subparsers.add_parser("verify")
    account_verify.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_verify.add_argument("alias")
    account_binding = account_subparsers.add_parser("binding")
    account_binding.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_binding.add_argument("uuid")
    account_source = account_subparsers.add_parser("source")
    account_source.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_source.add_argument("uuid")
    account_bind = account_subparsers.add_parser("bind")
    account_bind.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_bind.add_argument("uuid")
    account_bind.add_argument("alias")
    account_bind.add_argument("--source", default="verified")
    for account_action in ("launch-profile", "resume-profile"):
        account_launch = account_subparsers.add_parser(account_action)
        account_launch.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
        account_launch.add_argument("alias")
    account_sync = account_subparsers.add_parser("sync-ui")
    account_sync.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_sync.add_argument("uuid")
    account_sync.add_argument("alias")
    account_prepare = account_subparsers.add_parser("switch-prepare")
    account_prepare.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_prepare.add_argument("uuid")
    account_prepare.add_argument("source_alias")
    account_prepare.add_argument("target_alias")
    account_prepare.add_argument("cwd")
    account_prepare.add_argument("shpool_id")
    for account_action in (
        "switch-apply",
        "switch-commit",
        "switch-rollback",
        "switch-status",
    ):
        account_transaction = account_subparsers.add_parser(account_action)
        account_transaction.add_argument("txid")
    pending_parser = subparsers.add_parser("recovery-pending")
    pending_subparsers = pending_parser.add_subparsers(
        dest="pending_action", required=True
    )
    pending_subparsers.add_parser("list")
    pending_ack = pending_subparsers.add_parser("ack")
    pending_ack.add_argument("source_generation_key")
    pending_ack.add_argument("old_shpool_id")
    pending_ack.add_argument("uuid")
    manifest_parser = subparsers.add_parser("recovery-manifest")
    manifest_subparsers = manifest_parser.add_subparsers(
        dest="manifest_action", required=True
    )
    manifest_plan = manifest_subparsers.add_parser("plan-legacy")
    manifest_plan.add_argument("--continuity-evidence", required=True)
    manifest_plan.add_argument("--release-sha", required=True)
    manifest_plan.add_argument("--output", required=True)
    manifest_apply = manifest_subparsers.add_parser("apply-legacy")
    manifest_apply.add_argument("--plan", required=True)
    manifest_apply.add_argument("--release-sha", required=True)
    manifest_rollback = manifest_subparsers.add_parser("rollback-legacy")
    manifest_rollback.add_argument("--plan", required=True)
    manifest_rollback.add_argument("--release-sha", required=True)
    lifecycle_parser = subparsers.add_parser("lifecycle")
    lifecycle_subparsers = lifecycle_parser.add_subparsers(
        dest="lifecycle_action", required=True
    )
    lifecycle_subparsers.add_parser("provider-exited")
    lifecycle_subparsers.add_parser("user-input")
    lifecycle_subparsers.add_parser("reopen")
    lifecycle_keep = lifecycle_subparsers.add_parser("keep")
    lifecycle_keep.add_argument("choice", choices=("on", "off"))
    message_parser = subparsers.add_parser("msg")
    message_subparsers = message_parser.add_subparsers(
        dest="msg_action", required=True
    )
    message_resolve = message_subparsers.add_parser("resolve")
    message_resolve.add_argument("--target", required=True)
    message_send = message_subparsers.add_parser("send")
    message_send.add_argument("--target", required=True)
    message_send.add_argument("--text", required=True)
    message_send.add_argument("--fyi", action="store_true")
    # One repeatable purpose, one message. A caller that cannot prove delivery
    # repeats the send under the same key rather than minting a second copy.
    message_send.add_argument("--key", dest="idempotency_key")
    message_report = message_subparsers.add_parser("report")
    message_report.add_argument("--id", dest="msg_id")
    message_report.add_argument("--key", dest="idempotency_key")
    message_mark_read = message_subparsers.add_parser("mark-read")
    message_mark_read.add_argument("--thread", required=True)
    message_reply = message_subparsers.add_parser("reply")
    message_reply.add_argument("msg_id")
    message_reply.add_argument("--text", required=True)
    message_subparsers.add_parser("list")
    message_subparsers.add_parser("unread-count")
    message_queue = message_subparsers.add_parser("queue")
    message_queue.add_argument("--mark-seen", dest="queue_mark_seen")
    # The intake spool: the durable side of a project that arrived as a
    # message. Every verb is machine JSON — the entries carry thread keys and
    # message ids, which belong in a record and never in an operator's view.
    message_intake = message_subparsers.add_parser("intake")
    intake_subparsers = message_intake.add_subparsers(
        dest="intake_action", required=True
    )
    intake_record = intake_subparsers.add_parser("record")
    intake_record.add_argument("--msg-id", dest="msg_id", required=True)
    intake_record.add_argument("--source", required=True)
    intake_record.add_argument("--key", dest="intake_key")
    intake_record.add_argument("--summary")
    intake_record.add_argument("--terminal", type=int)
    intake_record.add_argument("--title")
    intake_record.add_argument("--cwd")
    # The automatic producer's entry point: one provider hook payload on
    # stdin, at most one intake out. The hooks call the library directly; this
    # is the same door for anything that cannot.
    intake_subparsers.add_parser("from-hook")
    # Detached provider hooks use this exact real facade verb to wake an
    # already-running supervisor after the durable intake write.
    intake_subparsers.add_parser("flush")
    intake_subparsers.add_parser("dismiss-machine")
    intake_ack = intake_subparsers.add_parser("ack")
    intake_ack.add_argument("--msg-id", dest="msg_id", required=True)
    intake_ack.add_argument("--text", required=True)
    intake_preflight = intake_subparsers.add_parser("preflight")
    intake_preflight.add_argument("--msg-id", dest="msg_id", required=True)
    intake_preflight.add_argument("--source-event-id", dest="source_event_id")
    intake_preflight.add_argument("--analysis", required=True)
    intake_preflight.add_argument("--scope", required=True)
    intake_preflight.add_argument("--required-expertise", dest="required_expertise", required=True)
    intake_preflight.add_argument("--worker-plan-json", dest="worker_plan_json", required=True)
    intake_preflight.add_argument("--risks", required=True)
    intake_preflight.add_argument("--tests", required=True)
    intake_preflight.add_argument("--manual-policy-exception", dest="manual_policy_exception")
    intake_delegate = intake_subparsers.add_parser("delegate")
    intake_delegate.add_argument("--msg-id", dest="msg_id", required=True)
    intake_delegate.add_argument(
        "--branch", dest="branches", action="append", required=True
    )
    for intake_relay in ("progress", "complete"):
        intake_note = intake_subparsers.add_parser(intake_relay)
        intake_note.add_argument("--msg-id", dest="msg_id", required=True)
        intake_note.add_argument("--text", required=True)
    intake_subparsers.add_parser("open")
    return parser


def _messages_run(action: str, config: dict[str, Any], **fields: Any) -> tuple[int, Any]:
    """One messaging verb, with the process evidence only the facade owns."""
    return _messages.run(
        action,
        config=config,
        environ=os.environ,
        home=_home(),
        snapshot_inventory=snapshot,
        process_table_reader=platform_process_table,
        prove_caller=prove_self_name_caller,
        max_proc_nodes=int(config.get("max_proc_nodes", DEFAULT_MAX_PROC_NODES)),
        **fields,
    )


def _launch_intake_worker(
    assignment: Mapping[str, Any],
    *,
    cwd: Path,
    environ: Mapping[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Mapping[str, Any]:
    """Run the installed exact-model worker launcher; its stdout proves nothing."""
    from sessionkit_messages.envelope import valid_idempotency_key
    from sessionkit_supervisor.intake import validate_requested_model

    provider = str(assignment.get("provider") or "")
    model = validate_requested_model(provider, assignment.get("requested_model"))
    launch_key = valid_idempotency_key(assignment.get("idempotency_key"))
    if not launch_key:
        raise CollectionError("worker launch has no exact idempotency key")
    if not cwd.is_absolute() or not cwd.is_dir():
        raise CollectionError("worker launch directory is unavailable")
    sp_raw = environ.get("SESSION_KIT_SP_CMD") or os.fspath(
        Path(__file__).resolve().parents[1] / "bin" / "sp"
    )
    sp_path = Path(sp_raw)
    try:
        sp_info = sp_path.lstat()
    except OSError as exc:
        raise CollectionError("installed Session Kit launcher is unavailable") from exc
    if (
        not sp_path.is_absolute()
        or statmod.S_ISLNK(sp_info.st_mode)
        or not statmod.S_ISREG(sp_info.st_mode)
        or not os.access(sp_path, os.X_OK)
    ):
        raise CollectionError("installed Session Kit launcher is unavailable")
    launch_env = dict(environ)
    launch_env["SESSION_KIT_BACKGROUND"] = "1"
    prompt_path: Path | None = None
    argv = [
        os.fspath(sp_path), "new", provider,
        "--model", model, "--launch-key", launch_key,
    ]
    if provider == "codex":
        # A new Codex process has no conversation identity until its first
        # submitted prompt creates a rollout.  Reconciliation deliberately
        # refuses an identity-free process, so bootstrap the managed worker
        # with a private no-work prompt and let the supervisor send the actual
        # scoped assignment only after exact model/identity proof succeeds.
        descriptor, raw_prompt_path = tempfile.mkstemp(
            prefix="session-kit-worker-", suffix=".prompt"
        )
        prompt_path = Path(raw_prompt_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(
                    "Session Kit initialized this managed worker. Do not inspect, "
                    "change, or execute anything yet. Wait for the Fleet Supervisor "
                    "to send the scoped assignment.\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            with contextlib.suppress(OSError):
                prompt_path.unlink()
            raise
        argv.extend(("--prompt-file", os.fspath(prompt_path)))
    try:
        completed = runner(
            argv,
            cwd=os.fspath(cwd),
            env=launch_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
    finally:
        if prompt_path is not None:
            with contextlib.suppress(OSError):
                prompt_path.unlink()
    if completed.returncode != 0:
        detail = clean_text(completed.stderr or completed.stdout, 300)
        raise CollectionError(
            f"installed worker launcher exited {completed.returncode}: {detail}"
        )
    return {"dispatched": True, "launch_idempotency_key": launch_key}


def _reconcile_intake_worker(
    assignment: Mapping[str, Any],
    inventory: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Independently bind one dispatch to one exact fresh inventory row."""
    from sessionkit_messages.envelope import valid_idempotency_key
    from sessionkit_supervisor.intake import validate_requested_model

    if inventory.get("source") != "live" or inventory.get("stale") is not False:
        raise CollectionError("worker reconciliation requires fresh live inventory")
    launch_key = valid_idempotency_key(assignment.get("idempotency_key"))
    provider = str(assignment.get("provider") or "")
    model = validate_requested_model(provider, assignment.get("requested_model"))
    matches = [
        row for row in inventory.get("sessions", ())
        if isinstance(row, Mapping)
        and row.get("launch_idempotency_key") == launch_key
    ]
    if not launch_key or len(matches) != 1:
        raise CollectionError("inventory has no unique exact worker launch key")
    row = matches[0]
    identity = row.get("identity")
    uuid = valid_uuid(identity.get("uuid")) if isinstance(identity, Mapping) else ""
    if (
        row.get("provider") != provider
        or row.get("actual_model") != model
        or not isinstance(identity, Mapping)
        or identity.get("confidence") != "exact"
        or not uuid
        or row.get("setup_incomplete") is True
    ):
        raise CollectionError("inventory worker provider, model, or identity is unproven")
    worker_title = ""
    if config is not None:
        tokens = [
            token
            for token in re.split(
                r"[^A-Za-z0-9]+", str(assignment.get("branch") or "")
            )
            if token
        ][:5]
        if len(tokens) == 1:
            tokens.append("Worker")
        if tokens:
            worker_title = " ".join(
                token.upper() if any(character.isdigit() for character in token)
                else token.capitalize()
                for token in tokens
            )
            try:
                mutate_canonical_self_name(config, provider, uuid, worker_title)
                propagate_provider_title(provider, uuid, worker_title)
            except CollectionError as exc:
                if "a human name owns this session" in str(exc):
                    worker_title = ""
                else:
                    raise
    return {
        "inventory_verified": True,
        "provider": provider,
        "actual_model": model,
        "worker_identity": f"{provider}:{uuid}",
        "worker_title": worker_title,
        "launch_idempotency_key": launch_key,
    }


def _intake_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Run one intake-spool verb and print its JSON result.

    Both relay paths are the messaging core's own verbs: a note to the source
    thread is a `send` under the note's idempotency key, an acknowledgement is
    a `reply` to the intake itself. The spool records what they did and never
    reaches a session on its own.
    """
    # The supervisor package pulls its MCP surface in at import time; one
    # intake verb must not put that cost on every snapshot.
    from sessionkit_supervisor import intake as _intake

    def deliver(*, thread_key: str, text: str, key: str) -> Mapping[str, Any]:
        try:
            _code, payload = _messages_run(
                "send",
                config,
                target=f"key:{thread_key}",
                text=text,
                fyi=True,
                idempotency_key=key,
            )
        except MessageError as exc:
            # A send the core refused — an exited source session, a kill
            # switch — is a note still owed, recorded with the reason. Losing
            # it to an exception would leave the spool claiming nothing was
            # ever written.
            return {
                "msg_id": None,
                "targets": [
                    {
                        "thread_key": thread_key,
                        "status": "unreachable",
                        "detail": str(exc),
                    }
                ],
            }
        return payload if isinstance(payload, Mapping) else {}

    def reply(*, msg_id: str, text: str) -> Mapping[str, Any]:
        _code, payload = _messages_run("reply", config, msg_id=msg_id, text=text)
        return payload if isinstance(payload, Mapping) else {}

    hook_payload = None
    if args.intake_action == "from-hook":
        hook_payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(hook_payload, Mapping):
            raise MessageError("a hook payload must be a JSON object")
    worker_plan = ()
    if args.intake_action == "preflight":
        worker_plan = json.loads(args.worker_plan_json)
        if not isinstance(worker_plan, list):
            raise MessageError("worker plan JSON must be an array")
    launcher: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    reconciler: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    if args.intake_action == "delegate":
        spool = _intake.Spool(Path(config["state_dir"]))
        # Two things the delegate verb itself already knows, and this lookup
        # has to agree with or the launcher is handed nowhere to run. The
        # entry stores the project's directory as `source_cwd` — a stored
        # entry is validated into that fixed key set, so no on-disk entry has
        # ever carried a plain `cwd`. And `delegate` names its intake through
        # `resolve`, so any id the project was delivered under has to reach
        # the same entry here.
        primary = spool.resolve(getattr(args, "msg_id", ""))
        entry = spool.read_entry(primary) if primary else None
        cwd_raw = entry.get("source_cwd") if isinstance(entry, Mapping) else None
        launch_cwd = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw else Path("")

        def launch(assignment: Mapping[str, Any]) -> Mapping[str, Any]:
            return _launch_intake_worker(
                assignment, cwd=launch_cwd, environ=os.environ
            )

        def reconcile(assignment: Mapping[str, Any]) -> Mapping[str, Any]:
            return _reconcile_intake_worker(
                assignment, snapshot(config=config), config=config
            )

        launcher = launch
        reconciler = reconcile
    else:
        spool = _intake.Spool(Path(config["state_dir"]))
    code, payload = _intake.run(
        args.intake_action,
        spool=spool,
        deliver=deliver,
        reply=reply,
        msg_id=getattr(args, "msg_id", None),
        source=getattr(args, "source", None),
        intake_key=getattr(args, "intake_key", None),
        summary=getattr(args, "summary", None),
        terminal=getattr(args, "terminal", None),
        title=getattr(args, "title", None),
        text=getattr(args, "text", None),
        cwd=getattr(args, "cwd", None),
        branches=getattr(args, "branches", None) or (),
        worker_plan=worker_plan,
        source_event_id=getattr(args, "source_event_id", None),
        analysis=getattr(args, "analysis", None),
        scope=getattr(args, "scope", None),
        required_expertise=getattr(args, "required_expertise", None),
        risks=getattr(args, "risks", None),
        tests=getattr(args, "tests", None),
        manual_policy_exception=getattr(args, "manual_policy_exception", None),
        launcher=launcher,
        reconciler=reconciler,
        payload=hook_payload,
        state_dir=Path(config["state_dir"]),
        environ=os.environ,
    )
    _json_print(payload)
    return code


def _msg_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Run one mass-messaging verb and print its JSON result."""
    if args.msg_action == "queue":
        if args.queue_mark_seen:
            timestamp = _events.mark_seen(
                Path(config["state_dir"]), args.queue_mark_seen
            )
            _json_print(
                {
                    "thread_key": _events.valid_thread_key(args.queue_mark_seen),
                    "seen_unix_ms": timestamp,
                }
            )
            return 0
        queue_input = os.environ.get("SESSION_KIT_INPUT_SNAPSHOT")
        queue_inventory = (
            load_inventory_input(queue_input)
            if queue_input
            else snapshot(config=config)
        )
        _json_print(
            _events.build_attention_queue(
                queue_inventory,
                Path(config["state_dir"]),
            )
        )
        return 0
    if args.msg_action == "intake":
        return _intake_command(args, config)
    code, payload = _messages_run(
        args.msg_action,
        config,
        target=getattr(args, "target", None),
        text=getattr(args, "text", None),
        fyi=getattr(args, "fyi", False),
        msg_id=getattr(args, "msg_id", None),
        thread=getattr(args, "thread", None),
        idempotency_key=getattr(args, "idempotency_key", None),
    )
    _json_print(payload)
    return code


def _lifecycle_environment() -> tuple[Path, str, str, int, int]:
    state_dir = Path(load_config()["state_dir"])
    session_id = os.environ.get("SESSION_KIT_LIFECYCLE_SESSION_ID", "")
    boot_id = os.environ.get("SESSION_KIT_LIFECYCLE_BOOT_ID", "")
    try:
        shell_pid = int(os.environ.get("SESSION_KIT_LIFECYCLE_SHELL_PID", ""))
        shell_start = int(
            os.environ.get("SESSION_KIT_LIFECYCLE_SHELL_START_TICKS", "")
        )
    except ValueError as exc:
        raise CollectionError("lifecycle shell generation is invalid") from exc
    return state_dir, session_id, boot_id, shell_pid, shell_start


def _prove_lifecycle_caller(
    session_id: str,
    shell_pid: int,
    shell_start: int,
) -> None:
    platform = _require_supported_platform()
    proc_root = _common.proc_root()
    process_table = (
        scan_darwin_process_table(DEFAULT_MAX_PROC_NODES)
        if platform == DARWIN_PLATFORM
        else scan_process_table(proc_root, DEFAULT_MAX_PROC_NODES)
    )
    chain = _process_ancestor_chain(process_table, os.getpid())
    shell = process_table.get(shell_pid)
    if (
        shell_pid not in chain
        or not isinstance(shell, Mapping)
        or shell.get("start_ticks") != shell_start
        or shell.get("session_name") != session_id
    ):
        raise CollectionError(
            "lifecycle caller is outside the exact managed shell generation"
        )


def _lifecycle_committed_conversation(
    *,
    provider: str,
    boot_id: str,
    shell_pid: int,
    shell_start: int,
) -> str | None:
    conversation = os.environ.get("SESSION_KIT_LIFECYCLE_CONVERSATION_UUID", "")
    marker_raw = os.environ.get("SESSION_KIT_LIFECYCLE_INTAKE_COMMIT", "")
    generation = os.environ.get("SESSION_KIT_MANAGED_GENERATION", "")
    if not conversation and not marker_raw and not generation:
        return None
    exact = valid_uuid(conversation)
    # Resumes and ordinary provider starts bind their exact launch-record UUID
    # without a first-prompt handoff. A configured handoff path/generation is
    # stricter: accepted-without-intake must still fail closed.
    if exact and not marker_raw and not generation:
        return exact
    if not exact or not marker_raw or not generation:
        raise CollectionError("lifecycle intake commit evidence is incomplete")
    parts = generation.split(":")
    if (
        len(parts) != 4
        or parts[0] != boot_id
        or parts[1] != str(shell_pid)
        or parts[2] != str(shell_start)
        or not parts[3].isdigit()
        or int(parts[3]) <= 0
    ):
        raise CollectionError("lifecycle intake commit generation does not match the shell")
    marker = Path(marker_raw)
    if not marker.is_absolute() or not marker.name.endswith(".intake_committed"):
        raise CollectionError("lifecycle intake commit path is invalid")
    try:
        parent_info = marker.parent.lstat()
        info = marker.lstat()
        value = _state_io.read_private_json(marker, max_bytes=8192)
    except (OSError, ValueError) as exc:
        raise CollectionError("lifecycle intake commit marker is unavailable") from exc
    if (
        statmod.S_ISLNK(parent_info.st_mode)
        or not statmod.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or statmod.S_IMODE(parent_info.st_mode) != 0o700
        or statmod.S_ISLNK(info.st_mode)
        or not statmod.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or statmod.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 8192
        or not isinstance(value, Mapping)
    ):
        raise CollectionError("lifecycle intake commit marker does not match the provider generation")
    # Read the three numeric fields once. Repeating `value.get(...)` inside the
    # chain re-reads the mapping after its own type test, which is both slower
    # and unprovable.
    marker_bytes = value.get("bytes")
    revision = value.get("requirements_revision")
    committed = value.get("committed_unix_ms")
    if (
        value.get("schema_version") != 2
        or value.get("status") != "intake_committed"
        or value.get("provider") != provider
        or valid_uuid(value.get("session_id")) != exact
        or value.get("managed_generation") != generation
        or not isinstance(value.get("submission_key"), str)
        or not value.get("submission_key")
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("prompt_sha256") or ""))
        or not isinstance(marker_bytes, int)
        or marker_bytes <= 0
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("source_event_id") or ""))
        or not re.fullmatch(r"[0-9a-f]{8}", str(value.get("intake_msg_id") or ""))
        or not isinstance(revision, int)
        or revision < 0
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("requirements_digest") or ""))
        or not isinstance(committed, int)
        or committed <= 0
    ):
        raise CollectionError("lifecycle intake commit marker does not match the provider generation")
    return exact


def _lifecycle_command(args: argparse.Namespace) -> int:
    state_dir, session_id, boot_id, shell_pid, shell_start = (
        _lifecycle_environment()
    )
    _prove_lifecycle_caller(session_id, shell_pid, shell_start)
    if args.lifecycle_action == "provider-exited":
        provider = os.environ.get("SESSION_KIT_LIFECYCLE_PROVIDER", "")
        try:
            exit_code = int(
                os.environ.get("SESSION_KIT_LIFECYCLE_EXIT_CODE", "")
            )
        except ValueError as exc:
            raise CollectionError("lifecycle provider exit code is invalid") from exc
        value = _lifecycle.record_provider_exit(
            state_dir,
            session_id=session_id,
            boot_id=boot_id,
            shell_pid=shell_pid,
            shell_start_ticks=shell_start,
            provider=provider,
            conversation_uuid=_lifecycle_committed_conversation(
                provider=provider,
                boot_id=boot_id,
                shell_pid=shell_pid,
                shell_start=shell_start,
            ),
            exit_code=exit_code,
            input_tracking=True,
        )
    elif args.lifecycle_action == "user-input":
        value = _lifecycle.update_state(
            state_dir,
            session_id=session_id,
            boot_id=boot_id,
            shell_pid=shell_pid,
            shell_start_ticks=shell_start,
            event="user-input",
        )
    elif args.lifecycle_action == "reopen":
        state = _lifecycle.load_state(state_dir, session_id)
        if state is None:
            raise CollectionError("provider-exit lifecycle state is unavailable")
        settings = load_config()
        live = snapshot(write_state=True, config=settings)
        item = lookup(live, session_id)
        if (
            item is None
            or not guard_live_inventory(live)
            or item.get("provider") != "shell"
            or item.get("exited_provider") != state["provider"]
            or item.get("shpool_shell", {}).get("pid") != shell_pid
            or item.get("shpool_shell", {}).get("process_start_ticks")
            != shell_start
        ):
            raise CollectionError(
                "exact provider-exit recovery is unavailable; nothing reopened"
            )
        recovery = item.get("recovery")
        if not isinstance(recovery, Mapping):
            raise CollectionError(
                "exact provider recovery is unavailable; nothing reopened"
            )
        provider = state["provider"]
        uuid = valid_uuid(recovery.get("uuid"))
        if recovery.get("provider") != provider or not uuid:
            raise CollectionError(
                "exact provider recovery is unavailable; nothing reopened"
            )
        expected = recovery_spec(provider, uuid, recovery.get("cwd"))
        if recovery.get("argv") != expected["argv"]:
            raise CollectionError(
                "provider recovery command changed; nothing reopened"
            )
        argv = list(expected["argv"])
        if provider == "codex":
            argv[1:1] = ["-c", "check_for_update_on_startup=false"]
            # The session's theme rides on the launch command everywhere
            # else; a reopen without it came back in the stock theme and
            # lost the window's color identity.
            theme_color = session_color(
                "codex", uuid, canonical_colors(load_config())
            )
            if theme_color:
                argv[1:1] = ["-c", f'tui.theme="sk-{theme_color}"']
        elif provider == "claude":
            signals = read_claude_transcript_signals(uuid)
            provider_name = signals["agent_name"] or signals["ai_title"]
            if provider_name:
                argv[1:1] = ["--name", provider_name]
        cwd = expected.get("cwd")
        if cwd is not None and (not os.path.isabs(cwd) or not os.path.isdir(cwd)):
            raise CollectionError(
                "provider recovery directory is unavailable; nothing reopened"
            )
        completed = subprocess.run(argv, cwd=cwd, check=False)
        value = _lifecycle.record_provider_exit(
            state_dir,
            session_id=session_id,
            boot_id=boot_id,
            shell_pid=shell_pid,
            shell_start_ticks=shell_start,
            provider=provider,
            exit_code=completed.returncode % 256,
            input_tracking=True,
        )
        return 0
    else:
        value = _lifecycle.update_state(
            state_dir,
            session_id=session_id,
            boot_id=boot_id,
            shell_pid=shell_pid,
            shell_start_ticks=shell_start,
            event="keep",
            keep=args.choice == "on",
        )
    _json_print(value)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-worker-model":
            from sessionkit_supervisor.intake import validate_requested_model

            print(validate_requested_model(args.provider, args.model))
            return 0
        if args.command == "platform":
            return _platform_command(args)
        if args.command == "lifecycle":
            return _lifecycle_command(args)
        config = load_config()
        if args.command == "recovery-command":
            _json_print(recovery_spec(args.provider, args.uuid, args.cwd))
            return 0
        if args.command == "codex-bounce-title":
            bounce_title = codex_bounce_prepare(args.uuid)
            if not bounce_title:
                return 1
            print(bounce_title)
            return 0
        if args.command == "claude-bounce-title":
            bounce_title, clear_marker = claude_bounce_prepare(args.uuid)
            if bounce_title:
                print(bounce_title)
                return 0
            return 3 if clear_marker else 1
        if args.command == "alias":
            return _alias_command(args, config)
        if args.command == "color":
            return _color_command(args, config)
        if args.command == "automatic-title":
            return _automatic_title_command(args, config)
        if args.command == "account":
            return _account_command(args, config)
        if args.command == "msg":
            return _msg_command(args, config)
        if args.command == "recovery-pending":
            if args.pending_action == "list":
                _json_print(list_pending(config))
                return 0
            _json_print(
                acknowledge_pending(
                    config,
                    args.source_generation_key,
                    args.old_shpool_id,
                    args.uuid,
                )
            )
            return 0
        if args.command == "recovery-manifest":
            if args.manifest_action == "plan-legacy":
                plan = plan_legacy_recovery_manifest(
                    config,
                    args.continuity_evidence,
                    args.release_sha,
                )
                _json_print(
                    publish_legacy_migration_plan(config, args.output, plan)
                )
            elif args.manifest_action == "apply-legacy":
                _json_print(
                    apply_legacy_recovery_manifest(
                        config, args.plan, args.release_sha
                    )
                )
            else:
                _json_print(
                    rollback_legacy_recovery_manifest(
                        config, args.plan, args.release_sha
                    )
                )
            return 0
        input_path = getattr(args, "input", None) or os.environ.get(
            "SESSION_KIT_INPUT_SNAPSHOT"
        )
        if input_path and args.command in {"render", "lookup", "detail"}:
            inventory = load_inventory_input(input_path)
        else:
            inventory = snapshot(
                write_state=not (
                    args.command == "snapshot"
                    and (args.no_write or args.guard_live)
                ),
                config=config,
            )
        if args.command == "snapshot":
            if args.strict_live and not strict_live_inventory(inventory):
                print(
                    "session inventory: strict live snapshot unavailable; refusing stale, partial, or ambiguous data",
                    file=sys.stderr,
                )
                return 3
            if args.guard_live and not guard_live_inventory(inventory):
                print(
                    "session inventory: guard live snapshot unavailable; refusing stale, partial, or malformed data",
                    file=sys.stderr,
                )
                return 3
            _json_print(inventory)
        elif args.command == "render":
            print(render_inventory(inventory, rows_only=args.rows))
        elif args.command == "waiting-count":
            print(sum(1 for item in inventory["sessions"] if item.get("needs_you")))
        elif args.command == "lookup":
            item = lookup(inventory, args.selector)
            if item is None:
                print(f"no unique shpool session matches {args.selector!r}", file=sys.stderr)
                return 2
            _json_print(item)
        elif args.command == "detail":
            # `lookup` is the machine mode and carries every identifier.
            # This is what a person is shown instead.
            if lookup(inventory, args.selector) is None:
                print(
                    "no single session matches that selector", file=sys.stderr
                )
                return 2
            print(
                render_detail(
                    inventory,
                    args.selector,
                    state_dir=Path(config["state_dir"]),
                ),
                end="",
            )
        return 0
    except (CollectionError, MessageError, OSError, ValueError) as exc:
        print(f"session inventory: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
