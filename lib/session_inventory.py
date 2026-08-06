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

from sessionkit_inventory import collector as _collector  # noqa: E402
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
    return _processes.scan_process_table(
        proc_root,
        max_nodes,
        proc_stat=_proc_stat,
        proc_environ=_proc_environ,
    )


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
    return _collector.build_inventory(
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
    return _collector.collect_live(
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
    return _state_io._read_terminal_registry(
        path,
        boot_id,
        epoch_path,
        schema_version=SCHEMA_VERSION,
        max_bytes=MAX_PRIVATE_JSON_BYTES,
        read_bounded_owner_file=_read_bounded_owner_file,
        empty_registry=_empty_terminal_registry,
        validate_registry=_validate_terminal_registry,
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
) -> dict[str, Any]:
    """Apply boot-stable selectors without changing contiguous internal rows."""
    return _terminal.apply_terminal_numbers(
        inventory,
        registry,
        boot_id=boot_id,
        allocate=allocate,
        retired=retired,
        current_time=current_time,
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


def snapshot(*, write_state: bool = True, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return _snapshot.snapshot(
        write_state=write_state,
        config=config,
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




def _short_path(path: str) -> str:
    return _render._short_path(path, home_factory=_home)












def render_inventory(inventory: Mapping[str, Any], rows_only: bool = False) -> str:
    return _render.render_inventory(
        inventory,
        rows_only,
        color_enabled=_color_enabled,
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
            Path(os.environ.get("SESSION_KIT_PROC_ROOT", "/proc")),
            DEFAULT_MAX_PROC_NODES,
        )
        _json_print({"processes": [table[pid] for pid in sorted(table)]})
        return 0
    root = Path(os.environ.get("SESSION_KIT_PROC_ROOT", "/proc"))
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
    recovery_parser = subparsers.add_parser("recovery-command")
    recovery_parser.add_argument("provider", choices=PROVIDERS)
    recovery_parser.add_argument("uuid")
    recovery_parser.add_argument("--cwd")
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
    return parser


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
    proc_root = Path("/proc")
    if (
        os.environ.get("SESSION_KIT_TESTING") == "1"
        and os.environ.get("SESSION_KIT_PROC_ROOT")
    ):
        proc_root = Path(os.environ["SESSION_KIT_PROC_ROOT"])
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
        if input_path and args.command in {"render", "lookup"}:
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
        return 0
    except (CollectionError, OSError, ValueError) as exc:
        print(f"session inventory: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
