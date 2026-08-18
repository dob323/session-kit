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
import fcntl
import json
import os
from pathlib import Path
import re
import shutil  # noqa: F401  # re-exported facade symbol
import signal
import stat as statmod
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

_SESSION_KIT_LIB_DIR = os.fspath(Path(__file__).resolve().parent)
if _SESSION_KIT_LIB_DIR not in sys.path:
    sys.path.insert(0, _SESSION_KIT_LIB_DIR)

from sessionkit_inventory import collector as _collector  # noqa: E402
from sessionkit_inventory import accounts as _accounts  # noqa: E402
from sessionkit_inventory import attention as _attention  # noqa: E402
from sessionkit_inventory import colors as _colors  # noqa: E402
from sessionkit_inventory import common as _common  # noqa: E402
from sessionkit_inventory import labels as _labels  # noqa: E402
from sessionkit_inventory import idle_state as _idle_state  # noqa: E402
from sessionkit_inventory import lifecycle as _lifecycle  # noqa: E402
from sessionkit_inventory import model as _model  # noqa: E402
from sessionkit_inventory import migration as _migration  # noqa: E402
from sessionkit_inventory import names as _names  # noqa: E402
from sessionkit_inventory import names_push as _names_push  # noqa: E402
from sessionkit_inventory import closed_sessions as _closed_sessions  # noqa: E402
from sessionkit_inventory import origins as _origins  # noqa: E402
from sessionkit_inventory import transcripts as _transcripts  # noqa: E402
from sessionkit_inventory import processes as _processes  # noqa: E402
from sessionkit_inventory import recovery as _recovery  # noqa: E402
from sessionkit_inventory import recovery_list as _recovery_list  # noqa: E402
from sessionkit_inventory import printed_selectors as _printed  # noqa: E402
from sessionkit_inventory import render as _render  # noqa: E402
from sessionkit_inventory import providers as _providers  # noqa: E402
from sessionkit_inventory import providers_claude as _providers_claude  # noqa: E402
from sessionkit_inventory import providers_codex as _providers_codex  # noqa: E402
from sessionkit_inventory import state_io as _state_io  # noqa: E402
from sessionkit_inventory import subagent_sweep as _subagent_sweep  # noqa: E402
from sessionkit_inventory import self_name as _self_name  # noqa: E402
from sessionkit_inventory import session_model as _session_model_reader  # noqa: E402
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
# `lifecycle reopen` answers with the reopened provider's own outcome so the
# managed shell can make the same clean-close / crash-menu decision it makes
# for a first exit. 0 is a clean provider exit and this status is a crashed
# one; every other status is a refusal to reopen anything at all, which is
# why the reopened provider's own code is not passed through directly.
LIFECYCLE_REOPENED_PROVIDER_CRASHED = 76
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
        xdg_path=lambda name, fallback: _common._xdg_path(name, fallback, environ=env),
    )


def _scoped_state_config(environ: Mapping[str, str] | None) -> dict[str, Any] | None:
    """The state root the caller's own environment locks its naming writes in."""
    if environ is None:
        return load_config()
    env = dict(environ)
    if not (
        env.get("SESSION_KIT_STATE_DIR") or env.get("XDG_STATE_HOME") or env.get("HOME")
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
    provider: str,
    uuid: str,
    title: str,
    *,
    previous_native_title: str | None = None,
    previous_native_name_since: Any = None,
    previous_native_name_source: str = "",
    environ: Mapping[str, str] | None = None,
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
            previous_native_title=previous_native_title,
            previous_native_name_since=previous_native_name_since,
            previous_native_name_source=previous_native_name_source,
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
    native_name_source: str = "",
    native_name_since: Any = None,
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
            native_name_source=native_name_source,
            native_name_since=native_name_since,
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


def release_automatic_name_claim(
    provider: str, uuid: str, *, environ: Mapping[str, str] | None = None
) -> bool:
    """Release an untouched claim after its first durable write failed."""
    exact = _common.valid_uuid(uuid)
    if provider not in PROVIDERS or not exact:
        return False
    path = _scoped_config_path(environ)
    config = _scoped_state_config(environ)
    if path is None or config is None:
        return False
    try:
        return _names.release_automatic_name_claim(
            config,
            provider,
            exact,
            atomic_write_json=atomic_write_json,
            config_path=lambda: path,
            private_alias_document=_private_alias_document,
            private_alias_parent=_private_alias_parent,
        )
    except (CollectionError, OSError):
        return False


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
    config["pending_native_titles"] = _common._valid_pending_native_titles(
        raw.get("pending_native_titles") if isinstance(raw, Mapping) else None
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
    """Read the poll rows, corrected by whatever the Notification hook recorded.

    The hook records are read once per snapshot, for exactly the sessions the
    poll returned: a session Claude does not list is not a session the picker
    can act on, so a record without a live row is nobody's evidence.
    """
    source = _attention.attention_source(os.environ)
    records: dict[str, dict[str, Any]] = {}
    if source != "poll" and isinstance(payload, list):
        state_dir = _attention.default_state_dir(os.environ, Path.home())
        records = _attention.read_all(
            state_dir,
            [item.get("sessionId") for item in payload if isinstance(item, Mapping)],
        )
    return _providers_claude._parse_claude_payload(
        payload,
        palette=CLAUDE_SESSION_COLORS,
        attention_records=records,
        attention_source=source,
    )


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
    process_table: Mapping[int, Mapping[str, Any]],
) -> dict[str, int] | None:
    return _processes.daemon_generation(
        process_table,
        is_shpool_daemon=_is_shpool_daemon,
    )


def _payload_daemon_identity(
    process_table: Mapping[int, Mapping[str, Any]],
) -> dict[str, int] | None:
    """Exact holder generation for binding one shpool list payload."""
    # Linux exposes the authoritative listener inode and each candidate's fd
    # tree. Darwin's bounded native scanner has no equivalent socket-owner
    # primitive here, so its safe ordinary case is one definite daemon with
    # the same generation on all three observations; multiple or unreadable-
    # argv candidates still refuse in _kit_daemons.
    require_holder = _require_supported_platform().startswith("linux")
    return _processes.daemon_generation(
        process_table,
        is_shpool_daemon=_is_shpool_daemon,
        require_socket_holder=require_holder,
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
    native_name_source: str = "",
    native_name_since: Any = None,
    pushed_titles: Mapping[str, str] | None = None,
    pending_native_titles: Mapping[str, Mapping[str, Any]] | None = None,
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
        native_name_source=native_name_source,
        native_name_since=native_name_since,
        provider_title_info=_provider_title_info,
        pushed_titles=pushed_titles,
        pending_native_titles=pending_native_titles,
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
    native_name_source: str = "",
    native_name_since: Any = None,
    pushed_titles: Mapping[str, str] | None = None,
    pending_native_titles: Mapping[str, Mapping[str, Any]] | None = None,
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
        native_name_source=native_name_source,
        native_name_since=native_name_since,
        context_title=_context_title,
        pushed_titles=pushed_titles,
        pending_native_titles=pending_native_titles,
        name_ownership=name_ownership,
    )


def _context_title(provider: str, cwd: str, started_at_unix_ms: int | None) -> str:
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


# One repeatable launch, one key. The pattern is the exact shape a launcher
# stamps onto a process, so a stray or truncated value can never bind a
# dispatch to an inventory row.
_LAUNCH_KEY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DAEMON_BINDING_UNSET = object()


def _exact_launch_key(value: object) -> str:
    """Return the exact launch idempotency key, or "" when the value is not one."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate if _LAUNCH_KEY_RE.match(candidate) else ""


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
    *,
    daemon_binding: Mapping[str, Any] | None | object = _DAEMON_BINDING_UNSET,
) -> dict[str, Any]:
    """Pure inventory composition for fixture tests and the live collector."""
    # A live binding is the daemon identity observed immediately before and
    # after the list payload, then revalidated before this composition. Never
    # choose a different daemon after those names have already been supplied.
    # Pure one-daemon fixture callers retain their historical fast path; a
    # multi-daemon fixture without payload provenance is deliberately refused.
    binding_refused = False
    if daemon_binding is _DAEMON_BINDING_UNSET:
        observed = tuple(_processes._kit_daemons(process_table, _is_shpool_daemon))
        definite = tuple(
            pid for pid, process in process_table.items() if _is_shpool_daemon(process)
        )
        unknown = tuple(
            pid
            for pid, process in process_table.items()
            if _processes._is_unknown_shpool_daemon(process)
        )
        daemon_pids = observed if len(definite) == 1 and not unknown else ()
        binding_refused = len(daemon_pids) != 1
    elif isinstance(daemon_binding, Mapping):
        pid = daemon_binding.get("pid")
        start_ticks = daemon_binding.get("process_start_ticks")
        process = process_table.get(pid) if isinstance(pid, int) else None
        if (
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and isinstance(start_ticks, int)
            and not isinstance(start_ticks, bool)
            and isinstance(process, Mapping)
            and process.get("start_ticks") == start_ticks
            and _is_shpool_daemon(process)
        ):
            daemon_pids = (pid,)
        else:
            daemon_pids = ()
            binding_refused = True
    else:
        daemon_pids = ()
        binding_refused = True

    def roots_from_selection(
        session_names: Iterable[str],
        table: Mapping[int, Mapping[str, Any]],
    ) -> tuple[dict[str, int], dict[str, list[str]]]:
        return _processes.shpool_roots(
            session_names,
            table,
            is_shpool_daemon=_is_shpool_daemon,
            daemon_pids=daemon_pids,
        )

    def generation_from_selection(
        table: Mapping[int, Mapping[str, Any]],
    ) -> dict[str, int] | None:
        return _processes.daemon_generation(
            table,
            is_shpool_daemon=_is_shpool_daemon,
            daemon_pids=daemon_pids,
        )

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
        shpool_roots=roots_from_selection,
        daemon_generation=generation_from_selection,
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
    if binding_refused:
        for item in inventory.get("sessions", ()):
            if not isinstance(item, dict):
                continue
            item["mutation_allowed"] = False
            item["mutation_rejection_reason"] = "daemon-identity-unresolved"
    children = _children_index(process_table)
    # Where the per-conversation model cache lives. Without a state directory
    # (fixture composition, `--no-write` inspection) every model read is a
    # bounded scan instead: slower, never wrong, and it writes nothing.
    raw_state_dir = config.get("state_dir")
    model_cache_dir = Path(str(raw_state_dir)) if raw_state_dir else None
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
            launch_key = _exact_launch_key(candidate.get("launch_idempotency_key"))
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
        # What model this session is on RIGHT NOW, for every row rather than
        # only the keyed launches above. The conversation's own record is the
        # source (picker-rows ruling): a launch argument is not what a session
        # runs. Handoff capability still comes from the process evidence.
        item.update(_live_model_fields(item, process_table, model_cache_dir))
        item["model_handoff_capable"] = _model_handoff_capable(item, process_table)
    return inventory


def _live_model_fields(
    item: Mapping[str, Any],
    process_table: Mapping[int, Mapping[str, Any]],
    cache_dir: Path | None,
) -> dict[str, str]:
    """The model a session runs now, its product name, and where that came from.

    The command line is the fallback, never the answer. `--model` records what
    a session was STARTED with; a person who types `/model` inside it changes
    what it actually runs and never touches argv again, so a row built from the
    process table shows the wrong model, confidently, for the rest of that
    session's life. The conversation's own record moves when the person moves
    the model, so that is what is read — and the command line answers only for
    a session whose record this machine cannot reach.
    """
    provider = _common.clean_text(item.get("provider"), 20).casefold()
    if provider not in PROVIDERS:
        return {
            "model": "",
            "display_model": "",
            "model_source": "",
            "model_state": _labels.MODEL_STATE_NOT_APPLICABLE,
        }
    identity = item.get("identity")
    raw_uuid = identity.get("uuid") if isinstance(identity, Mapping) else None
    uuid = _common.valid_uuid(raw_uuid) if isinstance(raw_uuid, str) else ""
    live, found = (
        _session_model_reader.current_model(
            provider,
            key=uuid,
            locate=lambda: _transcripts.locate_transcript(provider, uuid),
            cache_dir=cache_dir,
        )
        if uuid
        else ("", False)
    )
    if live:
        return {
            "model": live,
            "display_model": _session_model_reader.human_model_name(provider, live),
            "model_source": _session_model_reader.SOURCE_TRANSCRIPT,
            "launch_model": "",
            "model_state": "",
        }
    # The launch argument is recorded as evidence and is NEVER shown as the live
    # model. `--model` says what the session was STARTED with; a `/model` typed
    # inside it does not touch argv, and a conversation resumed from another
    # machine has no local record at all — no record is proof of nothing, not
    # proof that the launch value is still current. Showing it would be a
    # confident wrong answer, and the operator asked for this column filled in
    # order to trust it. What the kit cannot prove, it does not say.
    #
    # The row still says WHICH kind of no: a conversation this machine can read
    # that has simply not been answered yet is a different fact from one whose
    # record is gone.
    return {
        "model": "",
        "display_model": "",
        "model_source": "",
        "launch_model": _session_model(item, process_table),
        "model_state": (
            _labels.MODEL_STATE_NO_REPLY_YET
            if found
            else _labels.MODEL_STATE_UNREADABLE
        ),
    }


def _session_model(
    item: Mapping[str, Any], process_table: Mapping[int, Mapping[str, Any]]
) -> str:
    """The model a session's live provider was started with, or ""."""
    identity = item.get("identity")
    pid = identity.get("pid") if isinstance(identity, Mapping) else None
    if not isinstance(pid, int) or isinstance(pid, bool):
        return ""
    process = process_table.get(pid)
    if not isinstance(process, Mapping):
        return ""
    argv = process.get("cmdline")
    model = _arg_value(argv, "--model") if isinstance(argv, list) else ""
    if not model:
        model = process.get("requested_model") or ""
    return _common.clean_text(model, 80)


def _model_handoff_capable(
    item: Mapping[str, Any], process_table: Mapping[int, Mapping[str, Any]]
) -> bool:
    """Whether the managed shell can prove and rebuild this model request."""
    model = _common.clean_text(item.get("model"), 80)
    if not model:
        return True
    identity = item.get("identity")
    pid = identity.get("pid") if isinstance(identity, Mapping) else None
    process = process_table.get(pid) if isinstance(pid, int) else None
    if not isinstance(process, Mapping):
        return False
    argv = process.get("cmdline")
    command_model = _arg_value(argv, "--model") if isinstance(argv, list) else ""
    requested_model = _common.clean_text(process.get("requested_model"), 80)
    return command_model == model and requested_model == model


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
        busy_or_attached = str(
            item.get("availability") or ""
        ).casefold() == "attached" or str(
            item.get("agent_status") or ""
        ).casefold() in {"running", "working", "needs your reply", "reply optional"}
        if pending and busy_or_attached:
            item["provider_title_state"] = "deferred"
        elif pending:
            item["provider_title_state"] = "pending"
        else:
            item["provider_title_state"] = "ready"


_PROVIDER_MARKER_GENERATION = re.compile(r"[A-Za-z0-9:._-]{1,256}")


def _provider_marker_generation(path: Path, name: str) -> tuple[str, str] | None:
    try:
        value = path.read_text(encoding="utf-8", errors="strict").strip()
    except OSError:
        return None
    if not _PROVIDER_MARKER_GENERATION.fullmatch(value):
        return None
    return (name, value)


def capture_provider_untitled_generations(
    state_dir: Path,
) -> frozenset[tuple[str, str]]:
    """Exact untitled-marker generations present before process collection."""
    marker_root = Path(state_dir) / "provider-untitled"
    try:
        markers = list(marker_root.iterdir())
    except OSError:
        return frozenset()
    captured: set[tuple[str, str]] = set()
    for marker in markers:
        try:
            if marker.is_symlink() or not marker.is_file():
                continue
            generation = _provider_marker_generation(marker, marker.name)
            if generation is not None:
                captured.add(generation)
        except OSError:
            continue
    return frozenset(captured)


def _quarantine_orphaned_provider_untitled_markers(
    config: Mapping[str, Any],
    inventory: Mapping[str, Any],
    *,
    now: float | None = None,
    retire_generations: Iterable[tuple[str, str]] = (),
) -> list[dict[str, str]]:
    """Quarantine old markers only after fresh inventory proves absence."""
    if inventory.get("source") != "live" or inventory.get("stale") is not False:
        return []
    active_ids = {
        str(item.get("shpool_id_raw") or "")
        for item in inventory.get("sessions", ())
        if isinstance(item, Mapping)
    }
    allowed = frozenset(retire_generations)
    if not allowed:
        return []
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
            actual = _provider_marker_generation(destination, marker.name)
            if actual not in allowed:
                try:
                    marker.hardlink_to(destination)
                except FileExistsError:
                    # A still-newer marker already owns the live name.
                    pass
                destination.unlink(missing_ok=True)
                continue
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
        observe_claude_name=adopt_native_rename,
        codex_paths=_codex_paths,
        index_codex_processes=index_codex_processes,
        read_codex_session_index=read_codex_session_index,
        read_codex_db=read_codex_db,
        recent_output_times=recent_output_times,
        build_inventory=build_inventory,
        daemon_identity=_payload_daemon_identity,
        apply_provider_title_states=apply_provider_title_states,
        apply_retained_setup_attributions=apply_retained_setup_attributions,
    )
    return _apply_worktree_labels(_apply_account_bindings(inventory, config), config)


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
    *,
    revalidate_inventory: Callable[[], Mapping[str, Any]] | None = None,
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
        revalidate_inventory=revalidate_inventory,
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
        claude_pre_push_title=_claude_pre_push_title,
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


def _claude_pre_push_title(
    home: Path,
    uuid: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> Mapping[str, Any] | None:
    return _names_push._claude_pre_push_title(
        home,
        uuid,
        environ=environ,
        max_session_records=MAX_CLAUDE_SESSION_RECORDS,
    )


def _push_claude_title(
    home: Path,
    uuid: str,
    title: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    return _names_push._push_claude_title(
        home,
        uuid,
        title,
        environ=environ,
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
    uuid: str,
    codex_root: Path | None = None,
    now: float | None = None,
    *,
    mirror_index: bool = True,
) -> str:
    """Resolve the real name a bounced Codex process should boot under."""
    return _names_push.codex_bounce_prepare(
        uuid,
        codex_root,
        now,
        codex_home=_codex_home,
        max_session_index_bytes=MAX_CODEX_SESSION_INDEX_BYTES,
        mirror_index=mirror_index,
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
        human_named=human_named_keys,
        name_owner=name_owner,
        claim_name=claim_automatic_name,
        release_claim=release_automatic_name_claim,
        record_pushed=record_pushed_title,
        adopt_native=adopt_native_rename,
    )


def _append_codex_index_entry(index: Path, uuid: str, title: str) -> None:
    return _names_push._append_codex_index_entry(
        index,
        uuid,
        title,
    )


def _push_codex_thread_title(
    codex_root: Path,
    uuid: str,
    title: str,
    *,
    timeout_seconds: float = 1.0,
    still_automatic: Callable[[], bool] | None = None,
) -> tuple[list[str], list[str]]:
    """Set threads.title in Codex's own state database."""
    return _names_push._push_codex_thread_title(
        codex_root,
        uuid,
        title,
        timeout_seconds=timeout_seconds,
        still_automatic=still_automatic,
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
    kit_state_dir: Path,
    uuid: str,
    title: str,
    *,
    timeout_seconds: float | None = None,
    still_automatic: Callable[[], bool] | None = None,
) -> tuple[list[str], list[str]]:
    """Repaint live app-server-backed Codex windows with the new name."""
    return _names_push._push_codex_live_rename(
        kit_state_dir,
        uuid,
        title,
        max_sockets=MAX_CODEX_LIVE_RENAME_SOCKETS,
        max_frame=MAX_CODEX_LIVE_RENAME_FRAME,
        timeout_seconds=timeout_seconds,
        still_automatic=still_automatic,
    )


DEFAULT_CODEX_TITLE_ITEMS = '["activity", "thread"]'


def _title_template_value(raw: str):
    """One quoted string or one flat array of them; anything else refuses."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        items = []
        for part in inner.split(","):
            part = part.strip()
            if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
                items.append(part[1:-1])
            else:
                raise ValueError("shape")
        return items
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    raise ValueError("shape")


def _parse_title_template(text: str):
    """tomllib when it exists; else a strict reader of the template's own tiny
    dialect (one [table] level, JSON-compatible string and string-array
    values). Python 3.10 ships no tomllib, and the deployed template is
    kit-authored, so the dialect is the contract, not a guess."""
    try:
        import tomllib
    except ImportError:
        tomllib = None  # type: ignore[assignment]
    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except Exception:
            return None
    parsed: dict = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header = re.fullmatch(r"\[([A-Za-z0-9_-]+)\]", line)
        if header:
            current = parsed.setdefault(header.group(1), {})
            if not isinstance(current, dict):
                return None
            continue
        pair = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(.+)", line)
        if not pair:
            return None
        try:
            value = _title_template_value(pair.group(2))
        except ValueError:
            return None
        (current if current is not None else parsed)[pair.group(1)] = value
    return parsed


def _codex_title_items(environ: Mapping[str, str]) -> str:
    """The tab-title items a kit Codex launch passes, or "" when it passes none.

    Same answer as the launcher's, from the same deployed template: the kit
    owns the tab name on both providers (K3), and every path that starts a
    Codex process for a kit session has to agree about what goes on it.
    """
    switch = str(environ.get("SESSION_KIT_TAB_TITLE", "")).strip().casefold()
    if switch == "off":
        return ""
    template = _codex_home(environ) / "session-kit" / "terminal-title.toml"
    try:
        if template.is_symlink():
            raise OSError("refusing a symlinked template")
        parsed = _parse_title_template(template.read_text(encoding="utf-8"))
        value = (parsed or {}).get("tui", {}).get("terminal_title")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_CODEX_TITLE_ITEMS
    if not isinstance(value, list) or not value or len(value) > 12:
        return DEFAULT_CODEX_TITLE_ITEMS
    items = []
    for item in value:
        if not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z-]{0,31}", item):
            return DEFAULT_CODEX_TITLE_ITEMS
        items.append(f'"{item}"')
    return "[" + ", ".join(items) + "]"


def codex_live_capabilities(kit_state_dir: Path) -> dict[str, bool]:
    """What a live Codex app-server in this installation can be asked to do."""
    return _names_push.codex_live_capabilities(kit_state_dir)


def push_codex_live_color(
    kit_state_dir: Path, uuid: str, color: str, send=None
) -> tuple[list[str], list[str]]:
    """The one place the recolour path asks about repainting a live Codex window.

    Live rename already works through the app-server; a live recolour has no
    control in the installed build, so this answers with the real reason and
    never a false success. WS-F flips it by writing the capability file and
    passing its client as ``send``.
    """
    return _names_push.push_codex_live_color(kit_state_dir, uuid, color, send=send)


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
    *,
    revalidate_sessions: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
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
        revalidate_sessions=revalidate_sessions,
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
    *,
    only_if_absent: bool = False,
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
        only_if_absent=only_if_absent,
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


def _daemon_start_epoch(proc_root: Path, generation: Mapping[str, Any]) -> float:
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
        json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
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


def update_recovery_state(
    paths: Mapping[str, Path],
    inventory: Mapping[str, Any],
    *,
    collection_start: int | None = None,
) -> None:
    """Preserve exact pre-generation recovery data until explicitly resolved.

    ``recovery-manifest.json`` is the latest nonempty generation.  When a new
    boot or daemon generation appears, the prior manifest is copied to
    ``recovery-pending.json`` before the current manifest can advance.  Empty
    and partial post-reboot inventories therefore cannot erase the queue.
    """

    def locked_atomic_write(path: Path, payload: Any) -> None:
        if path == paths["pending"]:
            _state_io._require_pending_publication_lock(path)
        atomic_write_json(path, payload)

    def locked_collection_write(
        current_paths: Mapping[str, Path],
        path: Path,
        payload: Any,
        *,
        collection_start: int | None,
    ) -> None:
        if path == paths["pending"]:
            _state_io._require_pending_publication_lock(path)
        _state_io.write_collection_json(
            current_paths,
            path,
            payload,
            collection_start=collection_start,
        )

    with _state_io.publication_lock(paths):
        return _recovery.update_recovery_state(
            paths,
            inventory,
            schema_version=SCHEMA_VERSION,
            recovery_manifest=recovery_manifest,
            read_state_json=_read_state_json,
            has_recovery_entries=_has_recovery_entries,
            generation_key=_generation_key,
            atomic_write_json=locked_atomic_write,
            collection_start=collection_start,
            write_collection_json=(
                locked_collection_write if collection_start is not None else None
            ),
        )


def enqueue_lost_conversations(
    paths: Mapping[str, Path],
    inventory: Mapping[str, Any],
    previous_inventory: Mapping[str, Any] | None,
    *,
    boot_id: str,
    collection_start: int | None = None,
    config: Mapping[str, Any] | None = None,
) -> list[str]:
    """Queue conversations whose session vanished with the provider still in it."""
    diagnostics: list[str] = []

    def locked_atomic_write(path: Path, payload: Any) -> None:
        if path == paths["pending"]:
            _state_io._require_pending_publication_lock(path)
        atomic_write_json(path, payload)

    def locked_collection_write(
        current_paths: Mapping[str, Path],
        path: Path,
        payload: Any,
        *,
        collection_start: int | None,
    ) -> None:
        if path == paths["pending"]:
            _state_io._require_pending_publication_lock(path)
        _state_io.write_collection_json(
            current_paths,
            path,
            payload,
            collection_start=collection_start,
        )

    publication_paths = (
        _state_paths(config)
        if isinstance(config, Mapping) and config.get("state_dir")
        else _state_paths({"state_dir": paths["pending"].parent})
    )
    with _state_io.publication_lock(publication_paths):
        queued = _recovery.enqueue_lost_conversations(
            inventory,
            previous_inventory,
            paths=paths,
            schema_version=SCHEMA_VERSION,
            boot_id=boot_id,
            read_state_json=_read_state_json,
            atomic_write_json=locked_atomic_write,
            valid_recovery_state=_valid_recovery_state,
            closed_on_purpose=_closed_on_purpose_reader(
                config, diagnostic_sink=diagnostics
            ),
            now_unix_ms=int(time.time() * 1000),
            collection_start=collection_start,
            write_collection_json=(
                locked_collection_write if collection_start is not None else None
            ),
        )
    for diagnostic in diagnostics:
        print(f"session inventory: {diagnostic}", file=sys.stderr)
    return queued


def source_generation_key(boot_id: Any, generation: Any) -> str | None:
    return _recovery.source_generation_key(
        boot_id,
        generation,
        generation_key=_generation_key,
    )


_ACTION_ENUM = re.compile(r"[a-z][a-z_]{0,31}")
_ACTION_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


def _append_action_record(
    path: Path, action: str, outcome: str, session: str | None = None
) -> dict[str, Any]:
    """One line on the shared action log, in the shape every reader expects.

    Same file, same lock, and the same four keys the shell logger writes, plus
    the session when the caller can name one. Records that fail the shape are
    dropped by whichever writer next rewrites the file, so the shape is the
    contract rather than a convention.
    """
    if not _ACTION_ENUM.fullmatch(action) or not _ACTION_ENUM.fullmatch(outcome):
        raise CollectionError("action log entries take lower-case words")
    record: dict[str, Any] = {
        "action": action,
        "at_unix_ms": time.time_ns() // 1_000_000,
        "outcome": outcome,
        "schema_version": 1,
    }
    if session is not None:
        if not _ACTION_SESSION.fullmatch(session):
            raise CollectionError("action log session name is invalid")
        record["session"] = session
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(
        lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            )
        os.chmod(path, 0o600)
    finally:
        os.close(descriptor)
    return record


def _closed_on_purpose_reader(
    config: Mapping[str, Any] | None = None,
    *,
    diagnostic_sink: list[str] | None = None,
    closed_ledger_keys: set[str] | None = None,
) -> Callable[[Any, Any], bool]:
    """Honour a tombstone only while its closed-ledger row is findable.

    A close intent is a suppression record: it says crash recovery should not
    offer the conversation.  The closed-sessions row is the record that makes
    that suppression safe.  The two files can be replaced independently, so
    the tombstone alone is never enough evidence.  Validate the complete
    ledger through the same streaming snapshot used by the lists, lazily on
    the first tombstoned candidate, and fail open on every disagreement.
    """
    settings = load_config() if config is None else config
    try:
        state_dir = _state_paths(settings)["root"]
        intents = _lifecycle.load_close_intents(state_dir)
    except (CollectionError, OSError):
        # A tombstone store that cannot be read must not hide crash offers:
        # failing open here shows MORE recovery work, never less.
        return lambda provider, uuid: False

    ledger_keys: set[str] | None = closed_ledger_keys
    ledger_error: str | None = None
    reported: set[str] = set()

    def diagnose(message: str) -> None:
        if diagnostic_sink is not None and message not in reported:
            diagnostic_sink.append(message)
            reported.add(message)

    def load_ledger_keys() -> set[str]:
        nonlocal ledger_keys, ledger_error
        if ledger_keys is not None:
            return ledger_keys
        ledger_keys = set()
        try:
            # ``closed_snapshot`` validates the complete ledger before it
            # yields any rows and keeps row memory bounded for a large file.
            with _closed_sessions.closed_snapshot(limit=None) as rows:
                for row in rows:
                    provider = row.get("provider")
                    uuid = valid_uuid(row.get("uuid"))
                    if provider in PROVIDERS and uuid:
                        ledger_keys.add(f"{provider}:{uuid}")
        except (CollectionError, OSError) as exc:
            ledger_keys.clear()
            ledger_error = str(exc)
        return ledger_keys

    def closed(provider: Any, uuid: Any) -> bool:
        tombstoned = _lifecycle.closed_on_purpose(
            state_dir,
            provider=provider,
            uuid=uuid,
            intents=intents,
        )
        if not tombstoned:
            return False
        exact = valid_uuid(uuid)
        key = f"{provider}:{exact}" if provider in PROVIDERS and exact else ""
        if key and key in load_ledger_keys():
            return True
        if ledger_error:
            diagnose(
                f"close-intent inconsistency for {key or 'an invalid conversation'}: "
                "its tombstone was ignored because the closed-sessions ledger "
                f"could not be completely validated ({ledger_error}); recovery "
                "will keep offering the conversation"
            )
        else:
            diagnose(
                f"close-intent inconsistency for {key or 'an invalid conversation'}: "
                "a tombstone exists but its row is missing from the completely "
                "validated closed-sessions ledger; recovery will keep offering "
                "the conversation"
            )
        return False

    return closed


def flatten_pending(
    value: Any,
    *,
    config: Mapping[str, Any] | None = None,
    closed_ledger_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Return one safe recovery candidate per exact provider conversation."""
    diagnostics: list[str] = []
    flattened = _recovery.flatten_pending(
        value,
        schema_version=SCHEMA_VERSION,
        valid_recovery_state=_valid_recovery_state,
        source_generation_key=source_generation_key,
        pending_preferred_entry=_pending_preferred_entry,
        pending_conflict_fields=_pending_conflict_fields,
        pending_evidence=_pending_evidence,
        closed_on_purpose=_closed_on_purpose_reader(
            config,
            diagnostic_sink=diagnostics,
            closed_ledger_keys=closed_ledger_keys,
        ),
    )
    if diagnostics:
        flattened["diagnostics"] = diagnostics
    return flattened


def list_pending(
    config: Mapping[str, Any], *, closed_ledger_keys: set[str] | None = None
) -> dict[str, Any]:
    return _recovery.list_pending(
        config,
        state_paths=_state_paths,
        state_lock=StateLock,
        read_state_json=_read_state_json,
        # THIS config, not the ambient one: the tombstones that decide what is
        # still recovery work live beside the queue being read.
        flatten_pending=lambda value: flatten_pending(
            value, config=config, closed_ledger_keys=closed_ledger_keys
        ),
    )


def _closed_keys(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    keys: set[str] = set()
    snapshot_keys = getattr(rows, "conversation_keys", None)
    if callable(snapshot_keys):
        for provider, uuid in snapshot_keys():
            exact = valid_uuid(uuid)
            if provider in PROVIDERS and exact:
                keys.add(f"{provider}:{exact}")
        return keys
    for row in rows:
        provider = row.get("provider")
        uuid = valid_uuid(row.get("uuid"))
        if provider in PROVIDERS and uuid:
            keys.add(f"{provider}:{uuid}")
    return keys


def recovery_list_payload(
    config: Mapping[str, Any],
    *,
    include_closed: bool = True,
    include_projection_inputs: bool = False,
    closed_rows: Iterable[dict[str, Any]] | None = None,
    closed_ledger_keys: set[str] | None = None,
) -> dict[str, Any]:
    """The one list of conversations that can be brought back.

    Every recovery surface reads this. It gathers the three stores that know
    about a session that is no longer open, plus the inventory that says which
    conversations are open right now, and hands them to one projection. The
    two surfaces used to read two different stores and agree on almost
    nothing; the number of lists is the fix, not the contents of either.
    """
    if include_closed and (closed_rows is None or closed_ledger_keys is None):
        with _closed_sessions.closed_snapshot(
            limit=None, still_readable=_conversation_is_readable
        ) as snapshot:
            keys = _closed_keys(snapshot)
            return recovery_list_payload(
                config,
                include_closed=include_closed,
                include_projection_inputs=include_projection_inputs,
                closed_rows=snapshot,
                closed_ledger_keys=keys,
            )

    paths = _state_paths(config)
    pending_payload = list_pending(config, closed_ledger_keys=closed_ledger_keys)
    pending = pending_payload.get("entries") or []
    manifest = _read_state_json(paths["manifest"])
    manifest_sessions = (
        list((manifest.get("sessions") or {}).values())
        if isinstance(manifest, Mapping)
        else []
    )
    live = _read_state_json(paths["inventory"])
    # Open is open. A conversation running outside the kit is as live as one
    # inside it, and offering it for restore invites exactly the collision
    # this list exists to prevent -- the picker refused those at the last
    # moment and `sp restore` did not refuse them at all.
    live_sessions = (
        list(live.get("sessions") or []) + list(live.get("outside_agents") or [])
        if isinstance(live, Mapping)
        else []
    )
    closed = list(closed_rows or ()) if include_closed else []
    aliases = canonical_aliases(config)
    automatic_titles = canonical_automatic_titles(config)
    numbers = _terminal_number_bindings(paths["terminal_numbers"])
    rows = _recovery_list.recovery_rows(
        manifest_sessions=manifest_sessions,
        closed_rows=closed,
        pending_entries=pending,
        live_sessions=live_sessions,
        aliases=aliases,
        automatic_titles=automatic_titles,
        numbers=numbers,
        # One readability rule for every store. The ledger asks it above; the
        # crash manifest and the pending queue never asked it at all, so a
        # conversation whose transcript is gone was offered as a full restore
        # by two of the three sources and hidden by the third.
        still_readable=_conversation_is_readable,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "entries": rows,
    }
    if pending_payload.get("diagnostics"):
        payload["diagnostics"] = list(pending_payload["diagnostics"])
    if include_projection_inputs:
        # `sp recover` streams the potentially huge closed ledger separately.
        # These are the bounded inputs needed to give each streamed row the
        # same name, stable number and live-conversation exclusion as the one
        # projection above, without serializing the ledger into one document.
        payload["projection_inputs"] = {
            "aliases": aliases,
            "automatic_titles": automatic_titles,
            "numbers": numbers,
            "live": [
                [row.get("provider"), (row.get("identity") or {}).get("uuid")]
                for row in live_sessions
                if isinstance(row, Mapping) and isinstance(row.get("identity"), Mapping)
            ],
        }
    return payload


def _write_recovery_snapshot(*, config: Mapping[str, Any], allow_large: bool) -> None:
    """Stream projection inputs and Closed rows from one ledger snapshot."""

    with _closed_sessions.closed_snapshot(
        limit=None,
        still_readable=_conversation_is_readable,
        allow_large=allow_large,
    ) as snapshot:
        keys = _closed_keys(snapshot)
        projection = recovery_list_payload(
            config,
            include_closed=False,
            include_projection_inputs=True,
            closed_rows=snapshot,
            closed_ledger_keys=keys,
        )
        json.dump(
            {"recovery_projection": projection},
            sys.stdout,
            ensure_ascii=False,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        for row in snapshot:
            json.dump(row, sys.stdout, ensure_ascii=False, sort_keys=True)
            sys.stdout.write("\n")


def _printed_rows_from(payload: str, *, json_lines: bool = False) -> Iterable[Any]:
    """The rows a surface just printed, read back from what it printed them from.

    A file when the picker passes the payload it drew from, standard input when
    `sp recover` pipes back the same bytes it rendered. Never a fresh
    projection: the whole point is to record the list that was SHOWN, and a
    rebuild here would record a list nobody has seen and call it the screen.
    """
    if json_lines:

        def rows() -> Iterator[Any]:
            handle = (
                Path(payload).open(encoding="utf-8")
                if payload
                else contextlib.nullcontext(sys.stdin)
            )
            with handle as stream:
                for number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError as exc:
                        raise CollectionError(
                            f"recovery screen row {number} is invalid JSON"
                        ) from exc
                    if not isinstance(row, Mapping):
                        raise CollectionError(
                            f"recovery screen row {number} must be an object"
                        )
                    yield row

        return rows()
    text = Path(payload).read_text(encoding="utf-8") if payload else sys.stdin.read()
    if not text.strip():
        return []
    document = json.loads(text)
    if not isinstance(document, Mapping):
        raise CollectionError("a recovery screen payload must be a JSON object")
    entries = document.get("entries")
    return list(entries) if isinstance(entries, list) else []


def _terminal_number_bindings(path: Path) -> dict[str, Any]:
    """The conversation-to-number bindings, read without taking a lock.

    A listing verb never mutates, and the binding it needs outlives the
    session: the registry is keyed by conversation, so a number survives the
    close that ends its terminal. That is the whole reason a closed session
    can still be shown under the number it has everywhere else.
    """
    document = _read_state_json(path)
    if not isinstance(document, Mapping):
        return {}
    bindings = document.get("bindings")
    return dict(bindings) if isinstance(bindings, Mapping) else {}


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
        flatten_pending=lambda value: flatten_pending(value, config=config),
        collect_live=collect_live,
        strict_live_inventory=strict_live_inventory,
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


def _apply_worktree_labels(
    inventory: dict[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    """Name the branch a session is isolated on, from the worktree registry.

    Display only, and matched on the session's own directory: a row says
    "worktree p2/foo" because the kit recorded that directory as that branch's
    worktree, never because a path looked like one.
    """
    if "state_dir" not in config:
        return inventory
    try:
        from sessionkit_inventory import worktrees as _worktrees

        registry = _worktrees.labels(config["state_dir"], os.environ)
    except (CollectionError, OSError, ValueError, ImportError):
        return inventory
    if not registry:
        return inventory
    for group in ("sessions", "outside_agents"):
        for item in inventory.get(group, ()):
            if not isinstance(item, dict):
                continue
            label = registry.get(clean_text(item.get("cwd"), 4096))
            if label:
                item["worktree"] = dict(label)
    return inventory


def _apply_session_idle_states(
    current: dict[str, Any],
    previous: Mapping[str, Any] | None,
    *,
    state_dir: Path,
) -> dict[str, Any]:
    _idle_state.apply_idle_evidence(
        current,
        previous,
        state_dir=state_dir,
        transcript_snapshot=lambda provider, uuid: _transcripts.transcript_snapshot(
            provider, uuid
        ),
    )
    sessions = current.get("sessions")
    if isinstance(sessions, list):
        stall_seconds = _render.stall_threshold_seconds()
        for row in sessions:
            if isinstance(row, dict):
                # ``publish_view_fields`` ran during collection, before the
                # differential transcript overlay. Refresh the published
                # machine field as well as the render-time answer so every
                # consumer receives the new idle verdict.
                row["state"] = _labels.session_state(row, stall_seconds=stall_seconds)
        sessions.sort(key=_model.canonical_session_order_key)
        for index, row in enumerate(sessions, start=1):
            if isinstance(row, dict):
                row["row"] = index
    return current


def snapshot(
    *, write_state: bool = True, config: dict[str, Any] | None = None
) -> dict[str, Any]:
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
        enqueue_lost_conversations=enqueue_lost_conversations,
        apply_session_origins=_origins.apply_session_origins,
        capture_bounce_receipts=_origins.capture_bounce_receipts,
        capture_bounce_cleanup_generations=(
            _origins.capture_bounce_cleanup_generations
        ),
        capture_lifecycle_generations=(_lifecycle.capture_lifecycle_generations),
        capture_origin_generations=_origins.capture_origin_generations,
        capture_provider_untitled_generations=(capture_provider_untitled_generations),
        capture_session_color_generations=capture_session_color_generations,
        prune_origins=_origins.prune_origins,
        publish_session_colors=publish_session_colors,
        apply_provider_exit_states=_lifecycle.apply_provider_exit_states,
        prune_inactive_state=_lifecycle.prune_inactive_state,
        prune_model_cache=_session_model_reader.prune_cache,
        apply_session_idle_states=_apply_session_idle_states,
        read_terminal_registry=_read_terminal_registry,
        read_terminal_retirements=_read_terminal_retirements,
        apply_terminal_numbers=apply_terminal_numbers,
        terminal_retirement_payload=_terminal_retirement_payload,
        atomic_write_json=atomic_write_json,
        allocate_collection_start=_state_io.allocate_collection_start,
        preflight_collection_documents=_state_io.preflight_collection_documents,
        write_collection_json=_state_io.write_collection_json,
        quarantine_orphaned_provider_untitled_markers=(
            _quarantine_orphaned_provider_untitled_markers
        ),
        update_recovery_state=update_recovery_state,
        cold_inventory=_cold_inventory,
    )
    return inventory


def _transcript_reachability(config: Mapping[str, Any]) -> dict[str, Any]:
    """How many recorded conversations this machine can still read.

    Read-only: the last published snapshot is read, never built, so a doctor
    run neither talks to a provider nor writes state. A snapshot that cannot be
    read is a warning about the check, never a verdict about the sessions.
    """
    snapshot_path = _state_paths(config)["inventory"]
    try:
        payload = _state_io.read_private_json(
            snapshot_path, max_bytes=8 * 1024 * 1024, allow_missing=True
        )
    except (CollectionError, OSError):
        payload = None
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("sessions"), list
    ):
        return {
            "status": "warn",
            "checked": 0,
            "unreadable": [],
            "detail": (
                f"no readable session list at {snapshot_path}, so nothing was checked"
            ),
        }
    recorded = str(payload.get("generated_at") or "an unknown time")
    checked = 0
    unreadable: list[str] = []
    for row in payload["sessions"]:
        if not isinstance(row, Mapping) or row.get("provider") not in PROVIDERS:
            continue
        identity = row.get("identity")
        uuid = (
            valid_uuid(identity.get("uuid")) if isinstance(identity, Mapping) else None
        )
        if not uuid:
            continue
        checked += 1
        if _transcripts.locate_transcript(str(row["provider"]), uuid) is None:
            unreadable.append(_common.clean_text(row.get("title"), 60) or "a session")
    if checked == 0:
        detail = f"the list recorded at {recorded} holds no provider session"
        status = "ok"
    elif unreadable:
        detail = (
            f"{len(unreadable)} of {checked} sessions in the list recorded at "
            f"{recorded} have no transcript this machine can read: "
            + "; ".join(unreadable[:5])
            + ("; and more" if len(unreadable) > 5 else "")
        )
        status = "warn"
    else:
        detail = (
            f"all {checked} provider sessions in the list recorded at {recorded}"
            " have a transcript this machine can read"
        )
        status = "ok"
    return {
        "status": status,
        "checked": checked,
        "unreadable": unreadable,
        "detail": detail,
    }


SESSION_COLOR_DIR = "session-color"


def _session_color_generation(row: Mapping[str, Any]) -> str | None:
    shell = row.get("shpool_shell")
    if not isinstance(shell, Mapping):
        return None
    pid = shell.get("pid")
    ticks = shell.get("process_start_ticks")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or isinstance(ticks, bool)
        or not isinstance(ticks, int)
    ):
        return None
    identity = f"{row.get('shpool_id_raw')}:{pid}:{ticks}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _published_color_generation(path: Path) -> tuple[str, str] | None:
    try:
        fields = path.read_text(encoding="utf-8", errors="strict").split()
    except OSError:
        return None
    if len(fields) != 3 or not re.fullmatch(r"[0-9a-f]{64}", fields[2]):
        return None
    return (path.name, fields[2])


def capture_session_color_generations(
    state_dir: Path,
) -> frozenset[tuple[str, str]]:
    directory = Path(state_dir) / SESSION_COLOR_DIR
    try:
        entries = list(directory.iterdir())
    except OSError:
        return frozenset()
    captured: set[tuple[str, str]] = set()
    for path in entries:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            generation = _published_color_generation(path)
            if generation is not None:
                captured.add(generation)
        except OSError:
            continue
    return frozenset(captured)


def publish_session_colors(
    inventory: Mapping[str, Any],
    *,
    state_dir: Path,
    retire_generations: Iterable[tuple[str, str]] = (),
) -> int:
    """Write each live session's colour where its own shell can read it.

    The in-session prompt cannot afford to parse the inventory on every line,
    so it used a colour constant — which is how one session came to be green in
    the picker and a different colour in its own terminal. One tiny file per
    session, rewritten only when the colour actually changes, costs the prompt
    a single read and ends the second owner.
    """
    directory = Path(state_dir) / SESSION_COLOR_DIR
    written = 0
    live: set[str] = set()
    rows = inventory.get("sessions") if isinstance(inventory, Mapping) else None
    if not isinstance(rows, list):
        return 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        shpool_id = row.get("shpool_id_raw")
        color = row.get("display_color")
        if not isinstance(shpool_id, str) or not shpool_id or "/" in shpool_id:
            continue
        code = _colors.SESSION_SGR.get(color if isinstance(color, str) else "")
        if not code:
            continue
        live.add(shpool_id)
        generation = _session_color_generation(row)
        line = f"{color} {code}{f' {generation}' if generation else ''}\n"
        path = directory / shpool_id
        try:
            if path.is_file() and path.read_text(encoding="utf-8") == line:
                continue
            _state_io.ensure_private_directory(directory)
            temporary = directory / f".{shpool_id}.{os.getpid()}"
            temporary.write_text(line, encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            written += 1
        except OSError:
            continue
    # A colour file outliving its session would paint the next session that
    # reuses the name. They go when the session does.
    allowed = frozenset(retire_generations)
    try:
        for path in directory.iterdir():
            if path.name.startswith(".") or path.name in live:
                continue
            if _published_color_generation(path) not in allowed:
                continue
            with contextlib.suppress(OSError):
                path.unlink()
    except OSError:
        pass
    return written


def _conversation_is_readable(provider: str, uuid: str) -> bool:
    """True while this machine can still READ that conversation's record.

    Locating the file is not reading it. A transcript that exists with mode
    000 -- a botched chmod, a restore from an archive that lost its bits, a
    file owned by another account -- was located happily and then reported as
    `restorable: true`, which is a promise the kit cannot keep and which the
    person only discovers when the restore fails. Found in review 2026-08-15,
    together with the close path that trusted the same answer.

    So the file is opened. One byte is enough: an empty transcript is a real
    conversation that has not been written to yet, while EACCES, EISDIR or a
    vanished path are all "this cannot come back".
    """
    found = _transcripts.locate_transcript(provider, uuid)
    if found is None:
        return False
    try:
        with open(found, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _record_closed_session(
    config: Mapping[str, Any],
    *,
    provider: str,
    uuid: str = "",
    shpool_id: str = "",
    title: str = "",
    cwd: str = "",
    origin: str = "",
    account_alias: str = "",
) -> dict[str, Any]:
    """Append one deliberate close, filling in what the caller did not know."""
    title_source = ""
    if not title or not cwd or not origin:
        try:
            cached = _read_state_json(_state_paths(config)["inventory"])
        except (CollectionError, OSError):
            cached = None
        known = _closed_sessions.entry_from_inventory(
            cached if isinstance(cached, Mapping) else None,
            provider=provider,
            uuid=uuid,
            shpool_id=shpool_id,
        )
        # Only a title read from the row carries that row's provenance. One
        # the caller passed in is theirs, and nothing here knows where it
        # came from -- so it is recorded without a source rather than under
        # somebody else's.
        if not title:
            title_source = known["title_source"]
        title = title or known["title"]
        cwd = cwd or known["cwd"]
        origin = origin or known["origin"]
        account_alias = account_alias or known["account_alias"]
    try:
        return _closed_sessions.record_close(
            provider=provider,
            uuid=uuid,
            title=title,
            title_source=title_source,
            cwd=cwd,
            # "unknown", never "human". This row is provenance, it outlives
            # the session, and a session nobody stamped has none to record; a
            # default of "human" turns that absence into a positive claim that
            # every future restore reads back as fact.
            origin=origin or _closed_sessions.UNKNOWN_ORIGIN,
            shpool_id=shpool_id,
            account_alias=account_alias,
        )
    except (CollectionError, OSError) as exc:
        # The close already happened. A ledger that cannot be written costs
        # the person a listing, never the close they asked for.
        return {
            "recorded": False,
            "provider": provider,
            "uuid": uuid,
            "reason": str(exc),
        }


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
    """One session in full, for a person to read.

    `state_dir` is still accepted so every caller keeps working; the activity
    line it used to feed came from the retired attention queue, and a detail
    view now renders from the inventory row alone.
    """
    return _render.render_detail(
        inventory,
        selector,
        home_factory=_home,
        activity=None,
        now_ms=int(time.time() * 1000),
    )


# The fleet flagger writes one file; these are the same bounds the picker reads
# it with. A hostile or corrupt file must never hide the rest of the estate and
# must never stall a login.
FLEET_STALLS_MAX_BYTES = 262_144
FLEET_STALLS_FRESH_SECONDS = 300
FLEET_STALLS_MAX_RECORDS = 200


def _fleet_state_dir() -> Path:
    """Where the fleet keeps its own state. SESSION_KIT_FLEET_DIR overrides."""
    override = os.environ.get("SESSION_KIT_FLEET_DIR")
    if override:
        return Path(override).expanduser()
    return _home() / ".local" / "state" / "fleet"


def read_fleet_stalls(*, now: float | None = None) -> dict[str, list[str]]:
    """Stall flags the fleet raised, as {identifier: reasons}.

    Only the three reasons `docs/voice.md` calls degrees of **needs you** are
    returned. `orphan` is the fourth reason the flagger can write and is
    deliberately dropped: an orphaned session is a dead session, not a person's
    turn, and folding it in would make the count lie. A stale file (the flagger
    stopped running) is no evidence at all and reads as no flags.
    """
    path = _fleet_state_dir() / "stalls.json"
    try:
        if path.stat().st_size > FLEET_STALLS_MAX_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    try:
        generated_at = float(payload.get("generated_at") or 0)
    except (TypeError, ValueError):
        return {}
    moment = time.time() if now is None else now
    if moment - generated_at >= FLEET_STALLS_FRESH_SECONDS:
        return {}
    records = payload.get("stalled")
    records = records if isinstance(records, list) else []
    flags: dict[str, list[str]] = {}
    for record in records[:FLEET_STALLS_MAX_RECORDS]:
        if not isinstance(record, Mapping):
            continue
        key = clean_text(record.get("key"), 200)
        reason = clean_text(record.get("reason"), 32).casefold()
        if not key or reason not in _labels.STALL_REASONS_NEEDS_YOU:
            continue
        reasons = flags.setdefault(key, [])
        if reason not in reasons:
            reasons.append(reason)
    return flags


def _stall_identifiers(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Every identifier the flagger could have keyed this row by.

    It keys by the first of conversation uuid, shpool id, title that it finds,
    against an inventory it read at its own moment. Matching any of them keeps
    a flag attached to its session when a later collection resolves an identity
    the flagger did not have.
    """
    identity = row.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    candidates = (
        identity.get("uuid"),
        row.get("uuid"),
        row.get("shpool_id_raw"),
        row.get("shpool_id"),
        row.get("title"),
    )
    seen: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, str) and candidate and candidate not in seen:
            seen.append(candidate)
    return tuple(seen)


def _view_seconds_since(timestamp_ms: Any, now_ms: int) -> int | None:
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
        return None
    if timestamp_ms <= 0:
        return None
    return max(0, (now_ms - timestamp_ms) // 1000)


def _view_text(value: Any, limit: int = 120) -> str | None:
    text = clean_text(value, limit)
    return text or None


def publish_view_fields(
    inventory: dict[str, Any],
    *,
    stalls: Mapping[str, list[str]] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Add the fields every picker reads, on top of the collectors' evidence.

    Additive by contract: nothing here renames or removes a field, because the
    login picker parses this same document and a rename would blind the screen
    a person is looking at. `lib/sessionkit_inventory/SNAPSHOT.md` is the
    written form of what this publishes.

    One field is not merely added: a session the fleet flagged as unsurfaced,
    unanswered or silent comes back with `needs_you` true and the reasons
    listed. That is the point -- "needs you" means one thing, and it means it at
    the source rather than in each screen's own arithmetic.
    """
    sessions = inventory.get("sessions")
    if not isinstance(sessions, list):
        return inventory
    flags = read_fleet_stalls() if stalls is None else stalls
    as_of = (
        now_ms
        if isinstance(now_ms, int) and not isinstance(now_ms, bool)
        else int(time.time() * 1000)
    )
    stall_seconds = _render.stall_threshold_seconds()
    for row in sessions:
        if not isinstance(row, dict):
            continue
        reasons: list[str] = []
        if flags:
            for identifier in _stall_identifiers(row):
                for reason in flags.get(identifier, ()):
                    if reason not in reasons:
                        reasons.append(reason)
        if reasons:
            row["needs_you"] = True
        row["needs_you_reasons"] = reasons
        number = row.get("terminal_number")
        row["number"] = (
            number
            if isinstance(number, int) and not isinstance(number, bool) and number > 0
            else None
        )
        row["attached"] = row.get("availability") == _labels.AVAILABILITY_ATTACHED
        row["state"] = _labels.session_state(row, stall_seconds=stall_seconds)
        row["age_seconds"] = _view_seconds_since(row.get("started_at_unix_ms"), as_of)
        count = row.get("active_subagent_count")
        if not isinstance(count, int) or isinstance(count, bool):
            subagents = row.get("subagents")
            count = len(subagents) if isinstance(subagents, list) else 0
        row["subagent_count"] = count
        row["account_alias"] = _view_text(row.get("account_alias"), 20)
        # Collected by another pass and simply carried through, so the field is
        # in the contract before the collector exists and no screen has to grow
        # a special case the day it lands.
        row["model"] = _view_text(row.get("model"), 80)
        # The identifier is for a machine; the name is what every screen puts
        # in the column, and the state is why there is no name when there is
        # none. All three ride the same document so no surface has to guess.
        row["display_model"] = _view_text(row.get("display_model"), 80)
        row["model_source"] = _view_text(row.get("model_source"), 40)
        row["model_state"] = _view_text(row.get("model_state"), 40)
        # Evidence, never copy: what the process was started with. No screen
        # reads it, because a session's launch argument is not what it runs.
        row["launch_model"] = _view_text(row.get("launch_model"), 80)
        row["model_handoff_capable"] = row.get("model_handoff_capable") is True
        row["origin"] = _view_text(row.get("origin"), 40)
    return inventory


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


def _terminate_exact_process(pid: int, start_ticks: int) -> str:
    """TERM, then bounded KILL, addressed to one pidfd-pinned generation.

    The TERM must really land before KILL is allowed. Each delivery re-reads
    PID/start ticks after opening the pidfd, so a recycled PID is never a
    target. Synthetic process trees require an explicit test-only delivery
    seam; they must never make a host PID with the same number signalable.
    """
    if pid <= 0 or start_ticks <= 0:
        raise CollectionError("exact process generation is invalid")
    root = _common.proc_root()
    send_signal = None
    if root == Path("/proc") and not _subagent_sweep._HAS_PIDFD:
        raise CollectionError("pidfd-pinned exact signal delivery is unavailable")
    if root != Path("/proc"):
        mode = os.environ.get("SESSION_KIT_TEST_EXACT_SIGNAL", "")
        if os.environ.get("SESSION_KIT_TESTING") != "1" or mode not in {
            "remove",
            "survive",
        }:
            raise CollectionError(
                "exact signal delivery is unavailable for the synthetic process table"
            )

        def synthetic_send(target: int, signum: int) -> None:
            log_path = os.environ.get("SESSION_KIT_TEST_EXACT_SIGNAL_LOG", "")
            if log_path:
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(f"{target}\t{start_ticks}\t{signum}\n")
            if mode == "remove":
                shutil.rmtree(root / str(target))

        send_signal = synthetic_send

    def still_exact() -> bool:
        return _subagent_sweep._still_the_process(root, pid, start_ticks)

    grace_seconds = 0.0 if send_signal is not None else 2.0

    if not still_exact():
        return "already-gone"
    term_delivered = False
    try:
        _subagent_sweep._deliver_exact_process(
            root,
            pid,
            start_ticks,
            signal.SIGTERM,
            send_signal=send_signal,
        )
        term_delivered = True
    except ProcessLookupError:
        return "already-gone"
    except OSError as exc:
        raise CollectionError(f"exact TERM delivery failed: {exc}") from exc

    deadline = time.monotonic() + grace_seconds
    while still_exact() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not still_exact():
        return "terminated"
    if not term_delivered:  # defensive: KILL may only follow a delivered TERM
        raise CollectionError("exact TERM was not delivered; KILL was not attempted")
    try:
        _subagent_sweep._deliver_exact_process(
            root,
            pid,
            start_ticks,
            signal.SIGKILL,
            send_signal=send_signal,
        )
    except ProcessLookupError:
        return "terminated"
    except OSError as exc:
        raise CollectionError(f"exact KILL delivery failed: {exc}") from exc
    deadline = time.monotonic() + grace_seconds
    while still_exact() and time.monotonic() < deadline:
        time.sleep(0.05)
    if still_exact():
        raise CollectionError(
            f"verified process PID {pid} start {start_ticks} survives TERM and KILL"
        )
    return "killed"


def _write_closed_sessions(
    *, limit: int | None, allow_large: bool, json_lines: bool
) -> None:
    """Validate first, then stream one selected row at a time to stdout."""

    with _closed_sessions.closed_snapshot(
        limit=limit,
        still_readable=_conversation_is_readable,
        allow_large=allow_large,
    ) as rows:
        if json_lines:
            for row in rows:
                json.dump(row, sys.stdout, ensure_ascii=False, sort_keys=True)
                sys.stdout.write("\n")
            return
        sys.stdout.write('{"schema_version":' + str(SCHEMA_VERSION) + ',"closed":[')
        separator = ""
        for row in rows:
            sys.stdout.write(separator)
            json.dump(row, sys.stdout, ensure_ascii=False, sort_keys=True)
            separator = ","
        sys.stdout.write("]}\n")


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
    if args.platform_action == "shpool-holder-generation":
        # This is deliberately smaller than a live inventory collection. A
        # caller that already has one raw client payload needs to know who
        # answered that exact read, without issuing another client request.
        # Linux can answer from the listening socket inode and the daemons'
        # fd tables. Darwin has no equivalent observer in this release, so a
        # destructive caller must refuse there instead of treating a unique
        # process name as socket authorship.
        if platform == DARWIN_PLATFORM:
            raise CollectionError("exact shpool socket holder is unavailable on Darwin")
        table = platform_process_table(_common.proc_root(), DEFAULT_MAX_PROC_NODES)
        identity = _payload_daemon_identity(table)
        # Synthetic command fixtures cannot own a real Unix listener. Their
        # existing daemon-PID seam is accepted only behind the testing gate,
        # and only when it names an exact readable daemon generation. Live
        # code never reaches this branch.
        if identity is None and os.environ.get("SESSION_KIT_TESTING") == "1":
            fixture_pid = os.environ.get("SESSION_KIT_DAEMON_PID", "")
            if fixture_pid.isdigit():
                pid = int(fixture_pid)
                process = table.get(pid)
                if isinstance(process, Mapping) and (
                    _is_shpool_daemon(process)
                    or clean_text(process.get("comm"), 128) == "shpool"
                ):
                    start = process.get("start_ticks")
                    if isinstance(start, int) and not isinstance(start, bool):
                        identity = {"pid": pid, "process_start_ticks": start}
        if identity is None:
            raise CollectionError("exact shpool socket holder is unavailable")
        print(f"{identity['pid']}\t{identity['process_start_ticks']}")
        return 0
    if args.platform_action == "terminate-exact-process":
        if platform == DARWIN_PLATFORM:
            raise CollectionError(
                "pidfd-pinned exact termination is unavailable on Darwin"
            )
        print(_terminate_exact_process(args.pid, args.process_generation))
        return 0
    if args.platform_action == "exact-shell-gone":
        # A raw list losing a name proves only that a manager lost a record.
        # A close is complete only when the exact shell generation is gone
        # while the daemon generation on both sides of that observation is
        # unchanged. PID reuse is a successful disappearance of the old
        # generation; unreadable evidence refuses.
        if platform == DARWIN_PLATFORM:
            raise CollectionError(
                "exact post-close shell proof is unavailable on Darwin"
            )
        root = _common.proc_root()

        def exact_stat(pid: int) -> tuple[int, int, int] | None:
            try:
                return _proc_stat(root / str(pid) / "stat")
            except FileNotFoundError:
                return None
            except (OSError, ValueError) as exc:
                raise CollectionError(
                    f"process generation for PID {pid} is unreadable"
                ) from exc

        daemon_before = exact_stat(args.daemon_pid)
        if (
            daemon_before is None
            or daemon_before[0] != args.daemon_pid
            or daemon_before[2] != args.daemon_generation
        ):
            raise CollectionError("bound shpool daemon generation changed")
        shell = exact_stat(args.shell_pid)
        daemon_after = exact_stat(args.daemon_pid)
        if daemon_after != daemon_before:
            raise CollectionError(
                "bound shpool daemon generation changed during close proof"
            )
        if shell is not None and shell[2] == args.shell_generation:
            raise CollectionError(
                f"verified shell PID {args.shell_pid} start {args.shell_generation} survives"
            )
        print("gone")
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
        if (
            process.get("start_ticks") != args.generation
            or executable != args.executable
        ):
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
    if args.alias_action == "push":
        # Re-assert a name the kit already holds, without renaming anything.
        # A restore starts a NEW provider process for an old conversation, and
        # that process reads its name from the provider's own store at start:
        # if the kit's name never made it there (or a later provider write
        # replaced it), the restored window comes back nameless while the
        # picker still shows the name. Restore calls this so the name is in
        # place before the provider reads it.
        key = f"{args.provider}:{str(args.uuid).lower()}"
        push_title = canonical_aliases(config).get(key) or ""
        if not push_title:
            # The alias tier is the name a person gave this session, so it is
            # always safe to re-assert. The automatic tier is not: if somebody
            # renamed the conversation in the window and no inventory build has
            # adopted it yet, pushing the kit's derived title would overwrite
            # the person's name in the provider's own store -- and the evidence
            # adoption needs to recover it goes with it. Ownership decides.
            owner = canonical_name_ownership(config).get(key, {}).get("owner") or ""
            if owner != "human":
                push_title = canonical_automatic_titles(config).get(key) or ""
        push_payload = {"schema_version": SCHEMA_VERSION, "title": push_title}
        if not push_title:
            # Nothing named this conversation, so nothing is missing from the
            # provider store either. Silence here is the truth.
            push_payload.update(
                {"provider_title_pushes": [], "provider_title_warnings": []}
            )
            _json_print(push_payload)
            return 0
        push_payload.update(
            propagate_provider_title(args.provider, args.uuid, push_title)
        )
        _json_print(push_payload)
        # Three outcomes, told apart because the caller says something
        # different about each. Nothing landed: the window comes back nameless.
        # Something landed and something did not -- Codex writes an index entry
        # and a database title, and the status bar reads the database one, so a
        # half-push leaves the name on one surface and not the one people see.
        if not push_payload.get("provider_title_pushes"):
            return 1
        if push_payload.get("provider_title_warnings"):
            return 3
        return 0
    alias_title = str(args.title) if args.alias_action == "set" else None
    aliases = mutate_canonical_alias(config, args.provider, args.uuid, alias_title)
    payload: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "aliases": aliases}
    if args.alias_action == "set":
        assert alias_title is not None
        # Explicit assignment pushes once into the provider's own surfaces;
        # deletion only clears the local alias and leaves provider titles alone.
        payload.update(propagate_provider_title(args.provider, args.uuid, alias_title))
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
                raise CollectionError(
                    "App Server directory chain must be owner mode-0700"
                )
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

        def revalidate_sessions() -> list[Mapping[str, Any]]:
            current = snapshot(write_state=False, config=config)
            if current.get("source") != "live" or current.get("stale") is not False:
                raise CollectionError(
                    "session colors changed while fresh inventory was unavailable"
                )
            return [
                *current.get("sessions", ()),
                *current.get("outside_agents", ()),
            ]

        result = reconcile_session_colors(
            config,
            [*live.get("sessions", ()), *live.get("outside_agents", ())],
            revalidate_sessions=revalidate_sessions,
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
        color = _reserve_conversation_color(config, args.provider, args.uuid, occupied)
        _json_print({"schema_version": SCHEMA_VERSION, "color": color})
        return 0
    if args.color_action == "propagate":
        effective = session_color(args.provider, args.uuid, canonical_colors(config))
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
    if args.color_action == "bounce-ready":
        # Can a restart actually paint this session's colour?
        #
        # `effective` alone answers nothing: it falls back to a hash of the
        # conversation's identity, so it succeeds for any valid uuid. The real
        # question is whether the record the provider reads at start exists --
        # for Claude, an agent-color record in the conversation's transcript;
        # for Codex, the theme file the launcher passes on the command line.
        # Without it the restart costs the person their window and changes
        # nothing.
        effective = session_color(args.provider, args.uuid, canonical_colors(config))
        if not effective:
            return 1
        exact = valid_uuid(args.uuid)
        if not exact:
            return 1
        if args.provider == "claude":
            applied = False
            for transcript in _names_push._claude_transcripts(_home(), exact):
                try:
                    with open(transcript, encoding="utf-8") as handle:
                        for line in handle:
                            try:
                                record = json.loads(line)
                            except ValueError:
                                continue
                            if (
                                isinstance(record, Mapping)
                                and record.get("type") == "agent-color"
                                and record.get("agentColor") == effective
                            ):
                                applied = True
                except OSError:
                    continue
            if not applied:
                print(
                    "session inventory: no color record for this conversation; "
                    "a restart would not change its color",
                    file=sys.stderr,
                )
                return 1
        else:
            theme = _codex_home() / "themes" / f"sk-{effective}.tmTheme"
            try:
                ready = theme.is_file() and not theme.is_symlink()
            except OSError:
                ready = False
            if not ready:
                print(
                    "session inventory: no Codex theme for this color; "
                    "a restart would not change its color",
                    file=sys.stderr,
                )
                return 1
        print(effective)
        return 0
    if args.color_action == "effective":
        effective = session_color(args.provider, args.uuid, canonical_colors(config))
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


def _self_name_command(config: dict[str, Any], title: str) -> int:
    """`sp self-name`, with an exit status that means what it says.

    This used to `return 0` whatever came back. A root agent is told to name
    itself on its first substantive turn and to report `name failed` if that
    does not work, so it needs an answer it can act on; instead a self-name
    that never took looked exactly like one that did. Observed live on
    2026-08-17: the call met the collector jam, was killed while waiting, and
    the session ran for three hours unnamed with nothing to show for it.

    Two things are checked, both cheap and both read-back rather than trust:
    the written document has to actually carry this title for this session,
    and the name has to be showing rather than queued behind a retry. Either
    way the JSON still prints, so a caller that wants the detail keeps it.
    """
    result = self_name_automatic_title(config, title)
    _json_print(result)
    caller = result.get("caller") or {}
    key = f"{caller.get('provider')}:{caller.get('uuid')}"
    stored = (result.get("aliases") or {}).get(key)
    if stored != result.get("title"):
        print(
            f"session-kit: the self-name did not take for {key}; "
            "nothing was written. Retry.",
            file=sys.stderr,
        )
        return 1
    if result.get("automatic_name_state") != "ready":
        print(
            "session-kit: the name is recorded but not showing yet; a retry is "
            "queued. Treat this as not yet named.",
            file=sys.stderr,
        )
        return 1
    return 0


def _automatic_title_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
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
        return _self_name_command(config, args.title)
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
            mutate_canonical_automatic_title(config, args.provider, args.uuid, None)
        )
        return 0
    live = snapshot(write_state=False, config=config)
    if args.automatic_title_action == "audit":
        _json_print(audit_automatic_titles(config, live))
        return 0
    if live.get("source") != "live" or live.get("stale") is not False:
        raise CollectionError("automatic title prune requires a fresh live inventory")

    def revalidate_inventory() -> Mapping[str, Any]:
        current = snapshot(write_state=False, config=config)
        if current.get("source") != "live" or current.get("stale") is not False:
            raise CollectionError(
                "automatic title prune requires a fresh live inventory under lock"
            )
        return current

    _json_print(
        prune_automatic_titles(
            config,
            live,
            args.prune_token,
            revalidate_inventory=revalidate_inventory,
        )
    )
    return 0


def _render_account_list(profiles: list[dict[str, Any]]) -> str:
    """`sp account list` for a person.

    The JSON document this used to print carried profile_dir filesystem paths
    and epoch milliseconds -- machine facts, in a place the help promises
    "enrolled accounts and last verification". The document is still one
    `--json` away.
    """
    if not profiles:
        return f"  {_labels.empty_state('Accounts')}"
    now_ms = int(time.time() * 1000)
    count = len(profiles)
    lines = [f"  {count} {_labels.plural(count, 'account')}", ""]
    for provider in sorted({str(item.get("provider")) for item in profiles}):
        group = [item for item in profiles if item.get("provider") == provider]
        lines.append(f"  {_labels.provider_name(provider)}")
        alias_width = max(len(str(item.get("alias") or "")) for item in group)
        email_width = max(len(str(item.get("email") or "")) for item in group)
        plan_width = max(
            len(str(item.get("plan") or _labels.MISSING)) for item in group
        )
        for item in group:
            verified = _labels.relative_time(
                int(item.get("verified_at_unix_ms") or 0), now_ms
            )
            row = (
                f"    {str(item.get('alias') or ''):<{alias_width}}  "
                f"{str(item.get('email') or ''):<{email_width}}  "
                f"{str(item.get('plan') or _labels.MISSING):<{plan_width}}  "
                f"verified {verified}"
            )
            if item.get("enabled") is False:
                row = f"{row}  (disabled)"
            lines.append(row.rstrip())
    return "\n".join(lines)


def _snapshot_session_row(
    snapshot: str | None, shpool_id: str | None
) -> dict[str, Any] | None:
    """One session row out of a guard snapshot, or None when it is not exactly one.

    The automatic switch decides on the same snapshot the picker's own guard
    writes, and refuses on anything but a unique match: two rows claiming one
    shell is precisely the state in which moving the wrong conversation is
    possible.
    """
    if not snapshot or not shpool_id:
        return None
    try:
        with open(snapshot, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    rows = [
        row
        for row in data.get("sessions", [])
        if isinstance(row, dict) and row.get("shpool_id_raw") == shpool_id
    ]
    return rows[0] if len(rows) == 1 else None


def _account_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Run one owner-only account profile or switch-transaction verb."""
    action = args.account_action
    if action == "list":
        profiles = _accounts.list_profiles(config, args.provider)
        if getattr(args, "account_json", False):
            _json_print(
                {
                    "schema_version": _accounts.ACCOUNT_SCHEMA_VERSION,
                    "profiles": profiles,
                }
            )
        else:
            print(_render_account_list(profiles))
    elif action == "choices":
        _json_print(_accounts.account_choices(config, args.provider))
    elif action == "configure-feeds":
        _json_print(
            _accounts.configure_feeds(config, args.roster_path, args.advice_path)
        )
    elif action == "adopt-default":
        _json_print(
            _accounts.adopt_default(config, args.provider, args.alias, args.email)
        )
    elif action == "enroll":
        _json_print(_accounts.enroll(config, args.provider, args.alias, args.email))
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
    elif action == "auto-plan":
        from sessionkit_inventory import account_guard as _guard

        _json_print(
            _guard.plan(
                config,
                args.provider,
                args.uuid,
                _snapshot_session_row(
                    getattr(args, "snapshot", None), getattr(args, "shpool_id", None)
                ),
            )
        )
    elif action == "auto-spent":
        from sessionkit_inventory import account_guard as _guard

        _json_print(_guard.spent_aliases(config, args.provider))
    elif action == "auto-begin":
        from sessionkit_inventory import account_guard as _guard

        # Reserves the conversation's one move and prints the token that names
        # it. Called before anything irreversible, so a failure afterwards can
        # never leave a moved conversation looking unmoved.
        print(
            _guard.begin_hop(
                config,
                args.provider,
                args.uuid,
                args.source_alias,
                args.target_alias,
                reason=args.reason,
            )
        )
    elif action in ("auto-commit", "auto-release"):
        from sessionkit_inventory import account_guard as _guard

        verb = _guard.commit_hop if action == "auto-commit" else _guard.release_hop
        return 0 if verb(config, args.provider, args.uuid, args.token) else 1
    elif action == "auto-target-ok":
        from sessionkit_inventory import account_guard as _guard

        value = _guard.target_still_eligible(config, args.provider, args.alias)
        _json_print(value)
        return 0 if value["eligible"] else 1
    elif action == "auto-queue-notice":
        from sessionkit_inventory import account_guard as _guard

        # Records the debt only. Delivery claims it separately, so a notifier
        # that is down leaves the operator still owed the sentence.
        return (
            0
            if _guard.queue_notice(
                config, args.provider, args.uuid, args.token, args.sentence
            )
            else 1
        )
    elif action == "auto-notice-delivered":
        from sessionkit_inventory import account_guard as _guard

        return (
            0
            if _guard.notice_delivered(config, args.provider, args.uuid, args.token)
            else 1
        )
    elif action == "auto-pending-notices":
        from sessionkit_inventory import account_guard as _guard

        _json_print({"notices": _guard.pending_notices(config)})
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


def _live_session_names(payload_text: str) -> list[str] | None:
    """Session names from a ``shpool list --json`` payload, or ``None``.

    ``None`` is the answer that matters. "The list could not be read" and
    "there are no sessions" produce the same empty list, and one of those two
    means remove every working copy — so the difference has to survive as far
    as the caller, which turns ``None`` into a refusal rather than a sweep.

    This lives here rather than in the shell that used to do it because the
    shell had no way to tell an enumerator that failed from a machine with
    nothing running: both produced an empty array, and the next line read the
    empty array as "nothing is alive". A single stray row that is not a session
    refuses the whole payload; a list this cannot fully account for is not a
    list of what is alive.
    """
    try:
        payload = json.loads(payload_text)
    except (ValueError, TypeError):
        return None
    rows = payload.get("sessions") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None
    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        name = row.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _worktree_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """One worktree-isolation verb; prints its JSON result."""
    from sessionkit_inventory import worktrees as _worktrees

    state_dir = Path(config["state_dir"])
    if args.worktree_action == "materialize":
        _json_print(
            _worktrees.materialize(
                repo=args.repo,
                branch=args.branch,
                state_dir=state_dir,
                start_ref=args.start_ref,
                environ=os.environ,
                auto=bool(getattr(args, "auto", False)),
                origin=str(getattr(args, "origin", "") or ""),
            )
        )
        return 0
    if args.worktree_action == "copy-check":
        # "Should a delegated session be given a copy of this repository
        # without being asked?" Exit 0 with the reason when the answer is no.
        reason = _worktrees.auto_copy_refusal(args.repo, os.environ)
        if reason:
            print(reason)
            return 0
        return 1
    if args.worktree_action == "release":
        verdict = _worktrees.release(
            state_dir,
            shpool_id=args.shpool_id or None,
            path=args.path or None,
            merged_into_ref=args.merged_into,
            environ=os.environ,
        )
        if args.as_json:
            _json_print(verdict)
        else:
            print(_worktrees.render_verdict(verdict), end="")
        return 0
    if args.worktree_action == "sweep":
        # A sweep with no list of what is alive is not a sweep of nothing —
        # it is a sweep of everything. The list is required, and `--none-alive`
        # is how a caller says an empty list is what it means.
        active = list(args.active or [])
        none_alive = bool(args.none_alive)
        if getattr(args, "active_stdin", False):
            names = _live_session_names(sys.stdin.read())
            if names is None:
                print(
                    "the live session list could not be read, so nothing was "
                    "swept; an unreadable list is never read as 'nothing is "
                    "alive'",
                    file=sys.stderr,
                )
                return 2
            active.extend(names)
            # A payload that parsed cleanly and held no sessions is the one
            # case where an empty list really does mean nothing is alive.
            none_alive = True
        if not active and not none_alive:
            print(
                "sweep needs --active SHPOOL_ID for each session that is alive, "
                "or --none-alive to say there are none",
                file=sys.stderr,
            )
            return 2
        verdicts = _worktrees.release_idle(
            state_dir,
            active,
            merged_into_ref=args.merged_into,
            environ=os.environ,
        )
        if args.as_json:
            _json_print({"schema_version": SCHEMA_VERSION, "released": verdicts})
        else:
            for verdict in verdicts:
                print(_worktrees.render_verdict(verdict), end="")
        return 0
    if args.worktree_action == "bind":
        _json_print(
            _worktrees.bind(
                state_dir=state_dir,
                path=args.path,
                shpool_id=args.shpool_id,
                launch_key=args.launch_key,
                environ=os.environ,
            )
        )
        return 0
    if args.worktree_action == "list":
        found = _worktrees.records(state_dir, os.environ)
        if args.as_json:
            _json_print({"schema_version": SCHEMA_VERSION, "worktrees": found})
        else:
            print(_worktrees.render(found), end="")
        return 0
    if args.worktree_action == "lookup":
        record = _worktrees.lookup(
            state_dir,
            path=args.path,
            repo=args.repo,
            branch=args.branch,
            environ=os.environ,
            include_released=bool(getattr(args, "include_released", False)),
        )
        _json_print(record or {})
        return 0 if record else 2
    _json_print(
        _worktrees.teardown(
            state_dir=state_dir,
            path=args.path,
            repo=args.repo,
            branch=args.branch,
            merged_into_ref=args.merged_into,
            force=args.force,
            environ=os.environ,
        )
    )
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
    model_available_parser = subparsers.add_parser("model-availability")
    model_available_parser.add_argument("provider", choices=PROVIDERS)
    model_available_parser.add_argument("model")
    model_available_parser.add_argument(
        "--flag",
        default="--model-anyway",
        help="the flag a person repeats to ask for the model anyway",
    )
    model_available_parser.add_argument("--json", dest="as_json", action="store_true")
    model_served_parser = subparsers.add_parser("model-served")
    model_served_parser.add_argument("provider", choices=PROVIDERS)
    model_served_parser.add_argument("requested")
    model_served_parser.add_argument("served")
    bounce_parser = subparsers.add_parser("codex-bounce-title")
    bounce_parser.add_argument("uuid")
    bounce_parser.add_argument("--read-only", action="store_true")
    claude_bounce_parser = subparsers.add_parser("claude-bounce-title")
    claude_bounce_parser.add_argument("uuid")
    platform_parser = subparsers.add_parser("platform")
    platform_subparsers = platform_parser.add_subparsers(
        dest="platform_action", required=True
    )
    platform_subparsers.add_parser("boot-id")
    platform_subparsers.add_parser("process-table")
    platform_subparsers.add_parser("shpool-holder-generation")
    platform_terminate = platform_subparsers.add_parser("terminate-exact-process")
    platform_terminate.add_argument("pid", type=int)
    platform_terminate.add_argument("process_generation", type=int)
    platform_shell_gone = platform_subparsers.add_parser("exact-shell-gone")
    platform_shell_gone.add_argument("daemon_pid", type=int)
    platform_shell_gone.add_argument("daemon_generation", type=int)
    platform_shell_gone.add_argument("shell_pid", type=int)
    platform_shell_gone.add_argument("shell_generation", type=int)
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
    alias_push = alias_subparsers.add_parser("push")
    alias_push.add_argument("provider", choices=PROVIDERS)
    alias_push.add_argument("uuid")
    alias_delete = alias_subparsers.add_parser("delete")
    alias_delete.add_argument("provider", choices=PROVIDERS)
    alias_delete.add_argument("uuid")
    color_parser = subparsers.add_parser("color")
    color_subparsers = color_parser.add_subparsers(dest="color_action", required=True)
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
    color_ready = color_subparsers.add_parser("bounce-ready")
    color_ready.add_argument("provider", choices=PROVIDERS)
    color_ready.add_argument("uuid")
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
    account_list.add_argument(
        "provider", nargs="?", choices=sorted(_accounts.PROVIDERS)
    )
    account_list.add_argument("--json", dest="account_json", action="store_true")
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
    account_auto_plan = account_subparsers.add_parser("auto-plan")
    account_auto_plan.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_auto_plan.add_argument("uuid")
    account_auto_plan.add_argument("--snapshot")
    account_auto_plan.add_argument("--shpool-id", dest="shpool_id")
    account_auto_spent = account_subparsers.add_parser("auto-spent")
    account_auto_spent.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_auto_begin = account_subparsers.add_parser("auto-begin")
    account_auto_begin.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_auto_begin.add_argument("uuid")
    account_auto_begin.add_argument("source_alias")
    account_auto_begin.add_argument("target_alias")
    account_auto_begin.add_argument("--reason", default="")
    for account_action in ("auto-commit", "auto-release"):
        account_auto_state = account_subparsers.add_parser(account_action)
        account_auto_state.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
        account_auto_state.add_argument("uuid")
        account_auto_state.add_argument("token")
    account_auto_target = account_subparsers.add_parser("auto-target-ok")
    account_auto_target.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_auto_target.add_argument("alias")
    account_auto_queue = account_subparsers.add_parser("auto-queue-notice")
    account_auto_queue.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_auto_queue.add_argument("uuid")
    account_auto_queue.add_argument("token")
    account_auto_queue.add_argument("sentence")
    account_auto_done = account_subparsers.add_parser("auto-notice-delivered")
    account_auto_done.add_argument("provider", choices=sorted(_accounts.PROVIDERS))
    account_auto_done.add_argument("uuid")
    account_auto_done.add_argument("token")
    account_subparsers.add_parser("auto-pending-notices")
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
    pending_list = pending_subparsers.add_parser("list")
    pending_list.add_argument("--without-closed", action="store_true")
    pending_list.add_argument("--projection-inputs", action="store_true")
    pending_list.add_argument("--stream-closed", action="store_true")
    pending_list.add_argument("--stream-recovery-snapshot", action="store_true")
    pending_list.add_argument("--allow-large-ledger", action="store_true")
    pending_ack = pending_subparsers.add_parser("ack")
    pending_ack.add_argument("source_generation_key")
    pending_ack.add_argument("old_shpool_id")
    pending_ack.add_argument("uuid")
    # What a recovery screen printed beside each row, and the one question an
    # action asks about it: does that word still name the same conversation?
    # A word like "unnamed", or the clock face a shared name falls back to, is
    # a property of the list rather than of the conversation, so a list rebuilt
    # between printing it and acting on it can move it to another row.
    printed_parser = subparsers.add_parser("recovery-selectors")
    printed_subparsers = printed_parser.add_subparsers(
        dest="printed_action", required=True
    )
    printed_remember = printed_subparsers.add_parser("remember")
    printed_remember.add_argument("--payload", default="")
    printed_remember.add_argument("--json-lines", action="store_true")
    printed_check = printed_subparsers.add_parser("check")
    printed_check.add_argument("selector")
    printed_check.add_argument("provider")
    printed_check.add_argument("uuid")
    # Closing on purpose is not a crash, and the queue has to be told so by
    # whoever did it. The in-session verb rides the lifecycle proof; this one
    # exists for `k` on the picker, which closes a session it is not inside.
    # The session shell carries no kit environment, so it cannot source the
    # shell logger. It gets the same log through the one program it always
    # has a path to.
    action_parser = subparsers.add_parser("action-log")
    action_parser.add_argument("action")
    action_parser.add_argument("outcome")
    action_parser.add_argument("--session", default=None)
    # Who asked for a session, stamped by the verb that creates it. The
    # picker's default view is the person's own screen; anything a machine
    # started belongs behind a counted row, and only the creator knows which
    # it is.
    origin_parser = subparsers.add_parser("origin")
    origin_subparsers = origin_parser.add_subparsers(
        dest="origin_action", required=True
    )
    origin_record = origin_subparsers.add_parser("record")
    origin_record.add_argument("shpool_id")
    origin_record.add_argument("origin", choices=sorted(_origins.ORIGINS))
    origin_subparsers.add_parser("list")
    # Closing a session ends the terminal, never the conversation. Every
    # deliberate close lands here so the conversation can be listed and
    # restored afterwards, for as long as its transcript exists.
    closed_parser = subparsers.add_parser("closed-sessions")
    closed_subparsers = closed_parser.add_subparsers(
        dest="closed_action", required=True
    )
    closed_record = closed_subparsers.add_parser("record")
    closed_record.add_argument("provider", choices=sorted(_closed_sessions.PROVIDERS))
    closed_record.add_argument("--uuid", default="")
    closed_record.add_argument("--session", default="")
    closed_record.add_argument("--title", default="")
    closed_record.add_argument("--cwd", default="")
    closed_record.add_argument("--origin", default="")
    closed_record.add_argument("--account", default="")
    closed_list = closed_subparsers.add_parser("list")
    closed_list.add_argument("--limit", type=int, default=_closed_sessions.MAX_ENTRIES)
    closed_list.add_argument("--allow-large-ledger", action="store_true")
    closed_stream = closed_subparsers.add_parser("stream")
    closed_stream.add_argument(
        "--limit", type=int, default=_closed_sessions.MAX_ENTRIES
    )
    closed_stream.add_argument("--allow-large-ledger", action="store_true")
    closed_forget = closed_subparsers.add_parser("forget")
    closed_forget.add_argument("provider", choices=("claude", "codex"))
    closed_forget.add_argument("uuid")
    # Can every conversation this machine recorded still be READ here? A
    # session running under a rotated account profile once had its transcript
    # on this disk while the tool asking for it reported none, because one
    # resolver knew a root the other did not. Nothing warned.
    transcript_parser = subparsers.add_parser("transcript")
    transcript_subparsers = transcript_parser.add_subparsers(
        dest="transcript_action", required=True
    )
    transcript_locate = transcript_subparsers.add_parser("locate")
    transcript_locate.add_argument("provider", choices=("claude", "codex"))
    transcript_locate.add_argument("uuid")
    transcript_subparsers.add_parser("reachability")
    intent_parser = subparsers.add_parser("close-intent")
    intent_subparsers = intent_parser.add_subparsers(
        dest="intent_action", required=True
    )
    intent_record = intent_subparsers.add_parser("record")
    intent_record.add_argument("provider", choices=sorted(PROVIDERS))
    intent_record.add_argument("uuid")
    intent_subparsers.add_parser("list")
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
    lifecycle_closed = lifecycle_subparsers.add_parser("closed")
    # Closing a session that CRASHED is only better than leaving it in the
    # list if the conversation comes back afterwards. With this flag the verb
    # is a single decision — close and keep the conversation, or change
    # nothing at all — so the caller can keep a session it must not lose
    # without the asking itself leaving a record behind.
    lifecycle_closed.add_argument("--only-with-conversation", action="store_true")
    lifecycle_keep = lifecycle_subparsers.add_parser("keep")
    lifecycle_keep.add_argument("choice", choices=("on", "off"))
    worktree_parser = subparsers.add_parser("worktree")
    worktree_subparsers = worktree_parser.add_subparsers(
        dest="worktree_action", required=True
    )
    worktree_materialize = worktree_subparsers.add_parser("materialize")
    worktree_materialize.add_argument("--repo", required=True)
    worktree_materialize.add_argument("--branch", required=True)
    worktree_materialize.add_argument("--start-ref", dest="start_ref", default="HEAD")
    worktree_materialize.add_argument(
        "--auto",
        action="store_true",
        help="the kit chose this branch for delegated work, so the copy is "
        "released again when the session closes",
    )
    worktree_materialize.add_argument("--origin", default="")
    worktree_copy_check = worktree_subparsers.add_parser("copy-check")
    worktree_copy_check.add_argument("--repo", required=True)
    worktree_release = worktree_subparsers.add_parser("release")
    worktree_release.add_argument("--shpool-id", dest="shpool_id", default="")
    worktree_release.add_argument("--path", default="")
    worktree_release.add_argument("--merged-into", dest="merged_into", default="HEAD")
    worktree_release.add_argument("--json", dest="as_json", action="store_true")
    worktree_sweep = worktree_subparsers.add_parser("sweep")
    worktree_sweep.add_argument(
        "--active",
        action="append",
        default=[],
        metavar="SHPOOL_ID",
        help="a session that is still live; repeat for more",
    )
    worktree_sweep.add_argument("--merged-into", dest="merged_into", default="HEAD")
    worktree_sweep.add_argument(
        "--none-alive",
        dest="none_alive",
        action="store_true",
        help="there are no live sessions; without this an empty --active list is refused",
    )
    worktree_sweep.add_argument(
        "--active-stdin",
        dest="active_stdin",
        action="store_true",
        help=(
            "read a shpool list --json payload on stdin and take the live "
            "session names from it; a payload that cannot be read refuses the "
            "sweep instead of being treated as an empty list"
        ),
    )
    worktree_sweep.add_argument("--json", dest="as_json", action="store_true")
    worktree_bind = worktree_subparsers.add_parser("bind")
    worktree_bind.add_argument("--path", required=True)
    worktree_bind.add_argument("--shpool-id", dest="shpool_id", default="")
    worktree_bind.add_argument("--launch-key", dest="launch_key", default="")
    worktree_list = worktree_subparsers.add_parser("list")
    worktree_list.add_argument("--json", dest="as_json", action="store_true")
    worktree_lookup = worktree_subparsers.add_parser("lookup")
    worktree_lookup.add_argument("--path")
    worktree_lookup.add_argument("--repo")
    worktree_lookup.add_argument("--branch")
    worktree_lookup.add_argument(
        "--include-released",
        dest="include_released",
        action="store_true",
        help="also answer for a copy that has been given back, so a caller can "
        "find the repository it came from",
    )
    worktree_teardown = worktree_subparsers.add_parser("teardown")
    worktree_teardown.add_argument("--path")
    worktree_teardown.add_argument("--repo")
    worktree_teardown.add_argument("--branch")
    worktree_teardown.add_argument("--merged-into", dest="merged_into", default="HEAD")
    worktree_teardown.add_argument("--force", action="store_true")
    return parser


def _lifecycle_environment() -> tuple[Path, str, str, int, int]:
    state_dir = Path(load_config()["state_dir"])
    session_id = os.environ.get("SESSION_KIT_LIFECYCLE_SESSION_ID", "")
    boot_id = os.environ.get("SESSION_KIT_LIFECYCLE_BOOT_ID", "")
    try:
        shell_pid = int(os.environ.get("SESSION_KIT_LIFECYCLE_SHELL_PID", ""))
        shell_start = int(os.environ.get("SESSION_KIT_LIFECYCLE_SHELL_START_TICKS", ""))
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


def _prove_unchanged_daemon_generation(live: Mapping[str, Any]) -> None:
    """Re-prove the daemon behind a validated snapshot is still the same one."""
    expected = live.get("daemon_generation")
    platform = _require_supported_platform()
    proc_root = _common.proc_root()
    process_table = (
        scan_darwin_process_table(DEFAULT_MAX_PROC_NODES)
        if platform == DARWIN_PLATFORM
        else scan_process_table(proc_root, DEFAULT_MAX_PROC_NODES)
    )
    current = daemon_generation(process_table)
    if (
        not isinstance(expected, Mapping)
        or current is None
        or current.get("pid") != expected.get("pid")
        or current.get("process_start_ticks") != expected.get("process_start_ticks")
    ):
        raise CollectionError("the shpool daemon generation changed; nothing reopened")


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
        raise CollectionError(
            "lifecycle intake commit generation does not match the shell"
        )
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
        raise CollectionError(
            "lifecycle intake commit marker does not match the provider generation"
        )
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
        or not re.fullmatch(
            r"[0-9a-f]{64}", str(value.get("requirements_digest") or "")
        )
        or not isinstance(committed, int)
        or committed <= 0
    ):
        raise CollectionError(
            "lifecycle intake commit marker does not match the provider generation"
        )
    return exact


MODEL_REFUSED_EXIT = 3


def _model_availability_command(args: argparse.Namespace) -> int:
    """Is the model that was asked for the model that will serve the session?

    Exit 0 when it is, or when nothing on this machine can say. Exit
    ``MODEL_REFUSED_EXIT`` when this host would answer the request with a
    different model, or does not offer it at all — with the reason on standard
    error, because the caller's job is to stop rather than to substitute.
    """
    from sessionkit_inventory import worker_model as _worker_model

    state_dir = Path(load_config()["state_dir"])
    if args.command == "model-served":
        _json_print(
            _worker_model.record_served(
                state_dir, args.provider, args.requested, args.served
            )
        )
        return 0
    verdict = _worker_model.availability(
        args.provider, args.model, state_dir=state_dir, environ=os.environ
    )
    if args.as_json:
        _json_print(verdict)
    if verdict["verdict"] not in _worker_model.REFUSALS:
        if not args.as_json:
            # An answer of "unknown" belongs on stderr with the refusals, not
            # on stdout: the callers capture stderr, and an unknown that lands
            # on stdout is an unknown the person never sees. "This is the model
            # you asked for and nothing here could confirm it" is exactly the
            # sentence R11 exists to make sure gets said.
            stream = (
                sys.stdout
                if verdict["verdict"] != _worker_model.UNKNOWN
                else sys.stderr
            )
            stream.write(f"{verdict['reason']}\n")
        return 0
    sys.stderr.write(_worker_model.render_availability(verdict, flag=args.flag))
    return MODEL_REFUSED_EXIT


def _release_session_worktree(state_dir: Path, session_id: str) -> dict[str, Any]:
    """Give back the working copy a closing session was given, if it had one.

    This runs inside the session that is ending -- `bye`, or a provider that
    exited cleanly -- so it is deliberately quiet and never raises: an exit
    must not fail because of a directory. The core keeps the copy, and says
    why, whenever there is unmerged work or anybody still in it.
    """
    try:
        from sessionkit_inventory import worktrees as _worktrees

        verdict = _worktrees.release(
            state_dir, shpool_id=session_id, environ=os.environ
        )
        # A copy kept because there is work in it is the one thing on this path
        # a person has to be told. The JSON goes to the caller's /dev/null; the
        # sentence goes to standard error, which the close path leaves open for
        # exactly this.
        if verdict.get("action") in (_worktrees.KEPT, _worktrees.MANY):
            sys.stderr.write(_worktrees.render_verdict(verdict))
        return verdict
    except Exception:  # noqa: BLE001 - an exit is never failed by this
        return {"action": "none", "reason": "worktree release was unavailable"}


def _lifecycle_command(args: argparse.Namespace) -> int:
    state_dir, session_id, boot_id, shell_pid, shell_start = _lifecycle_environment()
    _prove_lifecycle_caller(session_id, shell_pid, shell_start)
    if args.lifecycle_action == "provider-exited":
        provider = os.environ.get("SESSION_KIT_LIFECYCLE_PROVIDER", "")
        try:
            exit_code = int(os.environ.get("SESSION_KIT_LIFECYCLE_EXIT_CODE", ""))
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
    elif args.lifecycle_action == "closed":
        # Intent, recorded by the shell that is about to end. A session with
        # no exact conversation (a plain managed shell) has nothing to
        # tombstone and says so instead of inventing a key.
        #
        # THE DOCUMENT MUST BELONG TO THIS SHELL, not merely to this session
        # id. `_prove_lifecycle_caller` checks the caller against /proc; it
        # says nothing about the record, which was then loaded by session id
        # alone. A reused shpool id -- `main2` closes, a new `main2` opens
        # before the collector prunes the old document -- let a fresh shell
        # tombstone the PREVIOUS occupant's conversation, and `update_state`
        # had guarded exactly this since it was written ("provider-exit
        # lifecycle generation changed"). Found in review 2026-08-15, along
        # with the comment further down that claimed this binding already
        # existed; it was true of `load_last_exact` and not of this.
        state = _lifecycle.load_state(state_dir, session_id)
        if state is not None and (
            state.get("boot_id") != boot_id
            or state.get("shell_pid") != shell_pid
            or state.get("shell_start_ticks") != shell_start
        ):
            state = None
        raw_provider = (state or {}).get("provider")
        provider = raw_provider if isinstance(raw_provider, str) else ""
        conversation = valid_uuid((state or {}).get("conversation_uuid"))
        if args.only_with_conversation and (state or {}).get("keep"):
            # `keep_session` says "automatic cleanup is off for this session",
            # and a crash is not the person asking for anything. This flag is
            # only ever passed by an automatic close, so the marker binds here
            # exactly as it binds the reaper (reaper.py `_safe_candidate`
            # requires provider_exit_keep is False). `bye` and a clean provider
            # exit pass no flag and still close: those ARE the person asking.
            _json_print({"recorded": False, "reason": "keep is set"})
            return 0
        if provider in PROVIDERS and not conversation:
            # A provider ran here and the record names no conversation.
            # That is not "there was none": Codex allocates its thread ID
            # inside the TUI, so a session started with `sp new` never hands
            # one back to the shell, and its provider-exit record carries
            # none. Such a session could then never be closed into anything
            # but history — the one shape that loses the conversation — so it
            # sat in the list until the 72-hour reaper.
            #
            # The collector proved an exact conversation while the provider
            # was still live and kept it for exactly this handoff. THIS record
            # validates itself against the boot, the shell PID and the shell
            # start (`_validated_last_exact`), so it can only ever name the
            # conversation this shell generation ran; `record_provider_exit`
            # refuses a conversation that changes inside one generation, so
            # there is only one to name. The same record already decides what
            # the picker offers to recover for these sessions
            # (lifecycle.apply_provider_exit_states).
            retained = _lifecycle.load_last_exact(
                state_dir,
                session_id,
                boot_id=boot_id,
                shell_pid=shell_pid,
                shell_start_ticks=shell_start,
            )
            if retained is not None and retained["provider"] == provider:
                conversation = retained["uuid"]
        if provider not in PROVIDERS or not conversation:
            if args.only_with_conversation:
                # The caller asked to close ONLY if the conversation survives
                # the close. It would not, so nothing is recorded and nothing
                # is closed; the caller keeps the session, which is the only
                # thing left that still knows which conversation this was.
                # Answering used to write the history-only row below, so
                # merely ASKING put a closed row on the list for a session
                # that then stayed open and kept running.
                _json_print({"recorded": False, "reason": "no exact conversation"})
                return 0
            # A plain managed shell has no conversation to tombstone, but the
            # person still closed something on purpose. It goes on the closed
            # list as history: its scrollback is all there ever was.
            row = _record_closed_session(
                load_config(), provider="shell", shpool_id=session_id
            )
            if isinstance(row, Mapping) and row.get("recorded") is False:
                _json_print(row)
                return 1
            released = _release_session_worktree(state_dir, session_id)
            _json_print(
                {
                    "recorded": True,
                    "provider": "shell",
                    "reason": "no exact conversation; shell history was recorded",
                    "worktree": released,
                }
            )
            return 0
        # Asked ONCE, and reported either way. An automatic close refuses on a
        # no; a close the person asked for still happens, but says so, because
        # "closed, and here it is in Closed sessions" is a promise the list
        # will quietly break when it filters the row out as unreadable.
        restorable = _conversation_is_readable(provider, conversation)
        if args.only_with_conversation and not restorable:
            # A syntactically valid UUID is not a conversation you can get
            # back. The transcript is what a restore actually reads, and the
            # closed-sessions list drops any row whose transcript this machine
            # cannot read -- so closing on the UUID alone ended the live
            # session AND produced a row the person would never even see.
            # An automatic close asks the same question the list will ask
            # later, before it does anything irreversible.
            _json_print(
                {"recorded": False, "reason": "the conversation cannot be read back"}
            )
            return 0
        # THE LEDGER ROW FIRST, AND THE TOMBSTONE ONLY IF IT LANDED.
        #
        # These two records do opposite jobs. The ledger row is what makes a
        # closed conversation findable; the tombstone is what stops the crash
        # queue offering it as lost work. Written in the old order -- tombstone
        # first, ledger second, neither checked -- a full disk produced the one
        # state with no way back: no row to restore from, and recovery told not
        # to offer it. The operator was told "the conversation is in Closed
        # sessions" while it was reachable from nowhere at all.
        #
        # So the findable record is written first and its result is read. No
        # row means no tombstone: a conversation the crash queue offers by
        # mistake is a nuisance, and one that no surface will ever mention
        # again is the thing this branch exists to prevent.
        row = _record_closed_session(
            load_config(),
            provider=provider,
            uuid=conversation,
            shpool_id=session_id,
        )
        if isinstance(row, Mapping) and row.get("recorded") is False:
            _json_print(
                {
                    "recorded": False,
                    "reason": row.get("reason")
                    or "the closed-sessions ledger could not be written",
                }
            )
            return 1
        # The row landed, so the conversation is findable and the close is
        # safe to allow. A tombstone that cannot be written after that costs
        # the person one wrong offer from the crash queue -- annoying, and
        # strictly better than refusing a close whose conversation is already
        # safely listed. It is reported rather than hidden.
        tombstoned = True
        try:
            _lifecycle.record_close_intent(
                state_dir,
                provider=provider,
                uuid=conversation,
            )
        except (CollectionError, OSError):
            tombstoned = False
        released = _release_session_worktree(state_dir, session_id)
        _json_print(
            {
                "recorded": True,
                "provider": provider,
                "uuid": conversation,
                "tombstoned": tombstoned,
                "restorable": restorable,
                "worktree": released,
            }
        )
        return 0
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
        # One sentence per condition. The menu that offers this reopen prints
        # what comes back here, and a single "recovery is unavailable" for six
        # different causes told the person nothing about which of them to fix.
        if not guard_live_inventory(live):
            raise CollectionError(
                "the live session list could not be trusted; nothing reopened"
            )
        if item is None:
            raise CollectionError(
                "this terminal is no longer in the live session list; nothing reopened"
            )
        if item.get("provider") != "shell":
            raise CollectionError(
                "this terminal is already running a provider; nothing reopened"
            )
        if item.get("exited_provider") != state["provider"]:
            raise CollectionError(
                "the recorded exit was not the provider this terminal last ran;"
                " nothing reopened"
            )
        shell = item.get("shpool_shell")
        if (
            not isinstance(shell, Mapping)
            or shell.get("pid") != shell_pid
            or shell.get("process_start_ticks") != shell_start
        ):
            raise CollectionError(
                "this shell is not the one that recorded the exit; nothing reopened"
            )
        recovery = item.get("recovery")
        if not isinstance(recovery, Mapping):
            raise CollectionError(
                "no conversation was recorded for this terminal; nothing reopened"
            )
        provider = state["provider"]
        uuid = valid_uuid(recovery.get("uuid"))
        if recovery.get("provider") != provider:
            raise CollectionError(
                f"the recorded conversation is not a {provider} one; nothing reopened"
            )
        if not uuid:
            raise CollectionError(
                "the recorded conversation has no usable id; nothing reopened"
            )
        expected = recovery_spec(provider, uuid, recovery.get("cwd"))
        if recovery.get("argv") != expected["argv"]:
            raise CollectionError("provider recovery command changed; nothing reopened")
        argv = list(expected["argv"])
        if provider == "codex":
            argv[1:1] = ["-c", "check_for_update_on_startup=false"]
            # The session's theme rides on the launch command everywhere
            # else; a reopen without it came back in the stock theme and
            # lost the window's color identity.
            theme_color = session_color("codex", uuid, canonical_colors(load_config()))
            if theme_color:
                argv[1:1] = ["-c", f'tui.theme="sk-{theme_color}"']
            # And the same for the tab name (K3). Without it, a crash-reopen
            # came back writing the personal config's title items over the
            # name the kit had just put on the tab, so that one window
            # disagreed with the picker until it was closed and opened again.
            title_items = _codex_title_items(os.environ)
            if title_items:
                argv[1:1] = ["-c", f"tui.terminal_title={title_items}"]
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
        # The snapshot above is evidence with an age, and everything between
        # it and this line is work. A daemon that restarted in that window
        # would leave this terminal describing a generation that no longer
        # exists, so the generation is proven once more with nothing left to
        # do but launch.
        _prove_unchanged_daemon_generation(live)
        completed = subprocess.run(argv, cwd=cwd, check=False)
        exit_code = completed.returncode % 256
        value = _lifecycle.record_provider_exit(
            state_dir,
            session_id=session_id,
            boot_id=boot_id,
            shell_pid=shell_pid,
            shell_start_ticks=shell_start,
            provider=provider,
            exit_code=exit_code,
            input_tracking=True,
        )
        # The reopened provider's own outcome is the answer, because the
        # caller has to make the same decision it makes for a first exit:
        # a clean exit closes the terminal, a crash stops at the menu.
        # Reporting success either way redrew the menu after a clean /exit.
        return 0 if exit_code == 0 else LIFECYCLE_REOPENED_PROVIDER_CRASHED
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
            from sessionkit_inventory.worker_model import validate_requested_model

            print(validate_requested_model(args.provider, args.model))
            return 0
        if args.command == "platform":
            return _platform_command(args)
        if args.command == "lifecycle":
            return _lifecycle_command(args)
        if args.command in ("model-availability", "model-served"):
            return _model_availability_command(args)
        config = load_config()
        if args.command == "recovery-command":
            _json_print(recovery_spec(args.provider, args.uuid, args.cwd))
            return 0
        if args.command == "codex-bounce-title":
            bounce_title = codex_bounce_prepare(
                args.uuid, mirror_index=not args.read_only
            )
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
        if args.command == "worktree":
            return _worktree_command(args, config)
        if args.command == "recovery-pending":
            if args.pending_action == "list":
                if args.stream_recovery_snapshot:
                    _write_recovery_snapshot(
                        config=config, allow_large=args.allow_large_ledger
                    )
                    return 0
                if args.stream_closed:
                    _write_closed_sessions(
                        limit=_closed_sessions.MAX_ENTRIES,
                        allow_large=args.allow_large_ledger,
                        json_lines=True,
                    )
                    return 0
                _json_print(
                    recovery_list_payload(
                        config,
                        include_closed=not args.without_closed,
                        include_projection_inputs=args.projection_inputs,
                    )
                )
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
        if args.command == "recovery-selectors":
            state_dir = _state_paths(config)["root"]
            if args.printed_action == "remember":
                _json_print(
                    _printed.remember_printed(
                        state_dir,
                        _printed_rows_from(args.payload, json_lines=args.json_lines),
                    )
                )
                return 0
            verdict = _printed.check_printed(
                state_dir, args.selector, args.provider, args.uuid
            )
            _json_print(verdict)
            # A word that was printed for a different conversation is not a
            # near miss to be reported and ridden past: the caller must be able
            # to tell it apart from agreement without reading the document.
            return 4 if verdict["verdict"] == _printed.DISAGREES else 0
        if args.command == "action-log":
            _json_print(
                _append_action_record(
                    _state_paths(config)["root"] / "action-events.jsonl",
                    args.action,
                    args.outcome,
                    args.session,
                )
            )
            return 0
        if args.command == "origin":
            state_dir = _state_paths(config)["root"]
            if args.origin_action == "list":
                _json_print(_origins.load_origins(state_dir))
                return 0
            _origins.record_origin(
                state_dir,
                shpool_id=args.shpool_id,
                origin=args.origin,
            )
            _json_print(
                {
                    "recorded": True,
                    "shpool_id": args.shpool_id,
                    "origin": args.origin,
                }
            )
            return 0
        if args.command == "transcript":
            if args.transcript_action == "locate":
                found = _transcripts.locate_transcript(args.provider, args.uuid)
                if found is None:
                    raise CollectionError(
                        f"no {args.provider} transcript on this machine for that"
                        " conversation"
                    )
                _json_print({"provider": args.provider, "path": os.fspath(found)})
                return 0
            _json_print(_transcript_reachability(config))
            return 0
        if args.command == "closed-sessions":
            if args.closed_action in ("list", "stream"):
                _write_closed_sessions(
                    limit=args.limit,
                    allow_large=args.allow_large_ledger,
                    json_lines=args.closed_action == "stream",
                )
                return 0
            if args.closed_action == "forget":
                _json_print(
                    {"forgotten": _closed_sessions.forget(args.provider, args.uuid)}
                )
                return 0
            row = _record_closed_session(
                config,
                provider=args.provider,
                uuid=args.uuid,
                shpool_id=args.session,
                title=args.title,
                cwd=args.cwd,
                origin=args.origin,
                account_alias=args.account,
            )
            _json_print(row)
            # The answer has to be in the EXIT STATUS as well, because that is
            # the only part the close paths read: both spell this verb
            # `>/dev/null 2>&1 && return 0`. Exiting zero on a failed append
            # made a lost row indistinguishable from a filed one and printed
            # `Closed ...` over a session that reached no list at all -- the
            # same shape as the sibling verb below, in the branch that handles
            # a plain managed shell (found in review, 2026-08-15).
            if isinstance(row, Mapping) and row.get("recorded") is False:
                return 1
            return 0
        if args.command == "close-intent":
            state_dir = _state_paths(config)["root"]
            if args.intent_action == "list":
                _json_print(_lifecycle.load_close_intents(state_dir))
                return 0
            # Refuse a key the tombstone store would reject, BEFORE writing
            # anything. The ledger goes first from here on, so an argument that
            # only the tombstone validates would otherwise leave a closed-list
            # row for a conversation that can never be tombstoned or found.
            _lifecycle.close_intent_key(args.provider, args.uuid)
            # THE LEDGER ROW FIRST, AND THE TOMBSTONE ONLY IF IT LANDED.
            #
            # A tombstone says "not a crash"; the ledger row says "here it is".
            # Written in the old order -- tombstone first, ledger second,
            # neither checked -- a ledger that cannot be written produced the
            # one state with no way back: no row to restore from, and recovery
            # told not to offer it. This verb is the shared one, so BOTH live
            # close paths ran it after they had already killed the session
            # (`sp close` in lib/sh/sp_commands.sh, `k` in lib/sh/sp_picker.sh)
            # and both were told `recorded: true`. The conversation was on
            # neither surface, and nothing on screen said so.
            #
            # So the findable record is written first and its result is read.
            # No row means no tombstone and no close: the crash queue keeps
            # offering the conversation, which is the one remaining way back
            # to it (found in review, 2026-08-15).
            row = _record_closed_session(config, provider=args.provider, uuid=args.uuid)
            if isinstance(row, Mapping) and row.get("recorded") is False:
                # Non-zero on purpose. Both callers branch on the exit status,
                # and this is the status that makes them say so out loud.
                _json_print(
                    {
                        "recorded": False,
                        "provider": args.provider,
                        "uuid": args.uuid,
                        "reason": row.get("reason")
                        or "the closed-sessions ledger could not be written",
                    }
                )
                return 1
            # The row landed, so the conversation is findable and suppressing
            # the crash offer is safe. A tombstone that cannot be written after
            # that costs one wrong offer from the crash queue -- reported, not
            # hidden, and strictly better than losing the conversation.
            tombstoned = True
            try:
                _lifecycle.record_close_intent(
                    state_dir,
                    provider=args.provider,
                    uuid=args.uuid,
                )
            except (CollectionError, OSError):
                tombstoned = False
            _json_print(
                {
                    "recorded": True,
                    "provider": args.provider,
                    "uuid": args.uuid,
                    "tombstoned": tombstoned,
                }
            )
            return 0
        if args.command == "recovery-manifest":
            if args.manifest_action == "plan-legacy":
                plan = plan_legacy_recovery_manifest(
                    config,
                    args.continuity_evidence,
                    args.release_sha,
                )
                _json_print(publish_legacy_migration_plan(config, args.output, plan))
            elif args.manifest_action == "apply-legacy":
                _json_print(
                    apply_legacy_recovery_manifest(config, args.plan, args.release_sha)
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
                    args.command == "render"
                    or (
                        args.command == "snapshot"
                        and (args.no_write or args.guard_live)
                    )
                ),
                config=config,
            )
        # The state file keeps the collectors' own evidence; what leaves here
        # for a screen carries the published view as well. Publishing at the
        # boundary is why a stall flag can fold into `needs_you` for every
        # reader without rewriting what the next collection compares against.
        inventory = publish_view_fields(inventory)
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
            print(
                sum(
                    1
                    for item in inventory["sessions"]
                    if item.get("blocking_question") or item.get("needs_you")
                )
            )
        elif args.command == "lookup":
            item = lookup(inventory, args.selector)
            if item is None:
                print(
                    f"no unique shpool session matches {args.selector!r}",
                    file=sys.stderr,
                )
                return 2
            _json_print(item)
        elif args.command == "detail":
            # `lookup` is the machine mode and carries every identifier.
            # This is what a person is shown instead.
            if lookup(inventory, args.selector) is None:
                print(
                    "session-kit: no session matches that selector",
                    file=sys.stderr,
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
    except (CollectionError, OSError, ValueError) as exc:
        print(f"session inventory: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
