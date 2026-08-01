#!/usr/bin/env python3
"""Exact, read-only shpool/Claude/Codex session inventory.

The collector takes one shpool JSON snapshot and one Claude agents JSON
snapshot, then joins provider identities to a bounded native process table.
Linux Codex identities come from a native Codex process's open root rollout;
the opt-in Darwin preview uses that exact process's ``CODEX_THREAD_ID``. It
never selects a conversation by cwd or recency.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import ctypes.util
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import stat as statmod
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import unicodedata

_SESSION_KIT_LIB_DIR = os.fspath(Path(__file__).resolve().parent)
if _SESSION_KIT_LIB_DIR not in sys.path:
    sys.path.insert(0, _SESSION_KIT_LIB_DIR)

from sessionkit_inventory import common as _common  # noqa: E402
from sessionkit_inventory import lifecycle as _lifecycle  # noqa: E402
from sessionkit_inventory import providers as _providers  # noqa: E402
from sessionkit_inventory.common import (  # noqa: E402, F401
    GENERATED_OPERATIONAL_ID_RE,
    LEGACY_OPERATIONAL_ID_RE,
    MAX_OPERATIONAL_ID_BYTES,
    PROVIDERS,
    UUID_RE,
    CollectionError,
    _positive_float,
    _positive_int,
    automatic_naming_enabled,
    clean_text,
    natural_name_key,
    normalize_automatic_title,
    shpool_id_mutation_policy,
    valid_uuid,
)


SCHEMA_VERSION = 1
PROVIDER_ORDER = {"claude": 0, "codex": 1, "shell": 2, "unknown": 3}
AVAILABILITY_ORDER = {"ready": 0, "attached": 1}
MAX_PRIVATE_JSON_BYTES = 1024 * 1024
MAX_CODEX_SESSION_INDEX_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_PROC_NODES = 16384
ABSENT_ALIAS_CONFIG_BACKUP = b"session-kit-alias-config-absent-v1\n"
Runner = Callable[[Sequence[str], float], str]

DARWIN_PREVIEW_ENV = "SESSION_KIT_MACOS_PREVIEW"
DARWIN_PLATFORM = "darwin"


def _runtime_platform() -> str:
    """Return the real platform, with a test-only override for Linux fixtures."""
    override = os.environ.get("SESSION_KIT_TEST_PLATFORM")
    if os.environ.get("SESSION_KIT_TESTING") == "1" and override:
        return override.casefold()
    return sys.platform


def _darwin_preview_enabled() -> bool:
    return os.environ.get(DARWIN_PREVIEW_ENV) == "1"


def _require_supported_platform() -> str:
    platform = _runtime_platform()
    if platform == DARWIN_PLATFORM and not _darwin_preview_enabled():
        raise CollectionError(
            "macOS support is an experimental preview; set "
            f"{DARWIN_PREVIEW_ENV}=1 to opt in"
        )
    if platform != DARWIN_PLATFORM and not platform.startswith("linux"):
        raise CollectionError(f"unsupported platform: {platform}")
    return platform


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


def _load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


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
    instant = dt.datetime.fromtimestamp(now if now is not None else time.time(), dt.timezone.utc)
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


def _command_from_env(env_name: str, default: str) -> list[str]:
    value = os.environ.get(env_name)
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
    runner: Runner,
    timeout: float,
) -> Any:
    fixture = os.environ.get(fixture_env)
    if fixture:
        return _load_json_file(Path(fixture).expanduser())
    prefix = _command_from_env(command_env, default_command[0])
    return json.loads(runner([*prefix, *default_command[1:]], timeout))


def _parse_shpool_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("sessions"), list):
        raise CollectionError("shpool list --json returned an invalid object")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload["sessions"]:
        if not isinstance(item, Mapping):
            raise CollectionError("shpool list --json contained a non-object session")
        name = item.get("name")
        status_raw = clean_text(item.get("status"), 32).casefold()
        if (
            not isinstance(name, str)
            or not name
            or name in seen
            or status_raw not in {"attached", "disconnected"}
        ):
            raise CollectionError("shpool list --json contained an invalid or duplicate session")
        started = item.get("started_at_unix_ms")
        if not isinstance(started, int) or started < 0:
            raise CollectionError(f"shpool session {name!r} has invalid started_at_unix_ms")
        seen.add(name)
        result.append(
            {
                "name": name,
                "status": "Attached" if status_raw == "attached" else "Disconnected",
                "started_at_unix_ms": started,
            }
        )
    return result


def _parse_claude_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise CollectionError("claude agents --json returned a non-array")
    result: list[dict[str, Any]] = []
    seen_pids: set[int] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        pid = item.get("pid")
        uuid = valid_uuid(item.get("sessionId"))
        if not isinstance(pid, int) or pid <= 0 or not uuid or pid in seen_pids:
            continue
        seen_pids.add(pid)
        raw_status = clean_text(item.get("status"), 40).casefold()
        waiting_for = item.get("waitingFor")
        needs_you = bool(waiting_for) or raw_status in {
            "waiting",
            "needs_input",
            "needs your reply",
        }
        status = "needs your reply" if needs_you else ("working" if raw_status == "busy" else raw_status)
        result.append(
            {
                "pid": pid,
                "uuid": uuid,
                "cwd": clean_text(item.get("cwd"), 4096),
                "kind": clean_text(item.get("kind"), 40),
                "started_at_unix_ms": item.get("startedAt")
                if isinstance(item.get("startedAt"), int)
                else None,
                "title": clean_text(item.get("name"), 120),
                "ai_title": clean_text(item.get("aiTitle"), 120),
                "name_source": clean_text(item.get("nameSource"), 20),
                "status": status or "unknown",
                "needs_you": needs_you,
            }
        )
    return result


MAX_CLAUDE_TITLE_SCAN_BYTES = 64 * 1024


def read_claude_ai_title(uuid: str, home: Path | None = None) -> str:
    """Return Claude's persisted conversation auto-title for one session.

    The TUI stores it as an ``ai-title`` transcript record, not in the session
    record, so the derived window label and the visible conversation title can
    disagree. Reads are bounded to the transcript's head and tail; the last
    record wins. Any problem returns an empty title.
    """
    exact_uuid = valid_uuid(uuid)
    if not exact_uuid:
        return ""
    base = home if home is not None else Path(os.environ.get("HOME") or Path.home())
    projects = base / ".claude" / "projects"
    try:
        transcripts = sorted(projects.glob(f"*/{exact_uuid}.jsonl"))
    except OSError:
        return ""
    title = ""
    for transcript in transcripts[:4]:
        if transcript.is_symlink():
            continue
        try:
            size = transcript.stat().st_size
            with open(transcript, "rb") as handle:
                chunks = [handle.read(MAX_CLAUDE_TITLE_SCAN_BYTES)]
                if size > 2 * MAX_CLAUDE_TITLE_SCAN_BYTES:
                    handle.seek(size - MAX_CLAUDE_TITLE_SCAN_BYTES)
                    chunks.append(handle.read(MAX_CLAUDE_TITLE_SCAN_BYTES))
                elif size > MAX_CLAUDE_TITLE_SCAN_BYTES:
                    chunks.append(handle.read())
        except OSError:
            continue
        for chunk in chunks:
            for line in chunk.split(b"\n"):
                if b'"ai-title"' not in line:
                    continue
                try:
                    record = json.loads(line.decode("utf-8", "strict"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if (
                    isinstance(record, Mapping)
                    and record.get("type") == "ai-title"
                    and record.get("sessionId") == exact_uuid
                ):
                    candidate = clean_text(record.get("aiTitle"), 120)
                    if candidate:
                        title = candidate
    return title


def _enrich_claude_payload(payload: Any) -> Any:
    """Attach per-session nameSource and ai-title evidence for the collector."""
    if not isinstance(payload, list):
        return payload
    home = Path(os.environ.get("HOME") or Path.home())
    for item in payload:
        if not isinstance(item, dict):
            continue
        pid = item.get("pid")
        uuid = valid_uuid(item.get("sessionId"))
        if not isinstance(pid, int) or pid <= 0 or not uuid:
            continue
        record_path = home / ".claude" / "sessions" / f"{pid}.json"
        if "nameSource" not in item and record_path.is_file():
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                if isinstance(record, Mapping):
                    item["nameSource"] = record.get("nameSource")
            except (OSError, ValueError):
                pass
        if "aiTitle" not in item:
            item["aiTitle"] = read_claude_ai_title(uuid, home)
    return payload


def _proc_stat(path: Path) -> tuple[int, int, int]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    left = raw.find("(")
    right = raw.rfind(")")
    if left < 1 or right < left:
        raise ValueError("malformed stat")
    pid = int(raw[:left].strip())
    fields = raw[right + 2 :].split()
    return pid, int(fields[1]), int(fields[19])


def _proc_environ(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for entry in path.read_bytes().split(b"\0"):
        if b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        try:
            values[key.decode("utf-8")] = value.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            continue
    return values


def scan_process_table(proc_root: Path, max_nodes: int) -> dict[int, dict[str, Any]]:
    """Read one bounded process-table view from proc_root."""
    table: dict[int, dict[str, Any]] = {}
    try:
        entries = sorted(
            (entry for entry in proc_root.iterdir() if entry.name.isdigit()),
            key=lambda item: int(item.name),
        )
    except OSError as exc:
        raise CollectionError(f"cannot enumerate {proc_root}: {exc}") from exc
    if len(entries) > max_nodes:
        raise CollectionError(
            f"{proc_root} has {len(entries)} processes, above max_proc_nodes={max_nodes}"
        )
    for entry in entries:
        pid = int(entry.name)
        try:
            before_pid, ppid, start_ticks = _proc_stat(entry / "stat")
            if before_pid != pid:
                continue
            cmdline = [
                value.decode("utf-8", errors="replace")
                for value in (entry / "cmdline").read_bytes().split(b"\0")
                if value
            ]
            comm = clean_text((entry / "comm").read_text(encoding="utf-8", errors="replace"), 128)
        except (OSError, ValueError):
            continue
        try:
            environ = _proc_environ(entry / "environ")
        except OSError:
            environ = {}
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            cwd = ""
        try:
            after_pid, after_ppid, after_start_ticks = _proc_stat(entry / "stat")
        except (OSError, ValueError):
            continue
        if (before_pid, ppid, start_ticks) != (
            after_pid,
            after_ppid,
            after_start_ticks,
        ):
            continue
        table[pid] = {
            "pid": pid,
            "ppid": ppid,
            "start_ticks": start_ticks,
            "cmdline": cmdline,
            "comm": comm,
            "cwd": cwd,
            "session_name": environ.get("SHPOOL_SESSION_NAME", ""),
        }
    return table


class _DarwinBsdInfo(ctypes.Structure):
    """Stable prefix and start fields from Darwin's ``struct proc_bsdinfo``."""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _DarwinTimeval(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_long),
        ("tv_usec", ctypes.c_int),
    ]


def _parse_darwin_procargs2(payload: bytes) -> tuple[list[str], dict[str, str]]:
    """Parse a KERN_PROCARGS2 payload without trusting text delimiters."""
    integer_size = ctypes.sizeof(ctypes.c_int)
    if len(payload) < integer_size:
        raise ValueError("short KERN_PROCARGS2 payload")
    argc = int.from_bytes(payload[:integer_size], sys.byteorder, signed=True)
    if argc < 0 or argc > 65536:
        raise ValueError("invalid KERN_PROCARGS2 argc")
    values = payload[integer_size:].split(b"\0")
    if not values:
        raise ValueError("missing KERN_PROCARGS2 executable")
    # First value is the executable path. Darwin may insert extra NUL padding
    # before argv[0].
    index = 1
    while index < len(values) and not values[index]:
        index += 1
    if len(values) - index < argc:
        raise ValueError("truncated KERN_PROCARGS2 argv")
    argv = [
        value.decode("utf-8", errors="replace")
        for value in values[index : index + argc]
    ]
    environ: dict[str, str] = {}
    for raw in values[index + argc :]:
        if not raw or b"=" not in raw:
            continue
        key, value = raw.split(b"=", 1)
        try:
            name = key.decode("utf-8")
        except UnicodeDecodeError:
            continue
        environ[name] = value.decode("utf-8", errors="replace")
    return argv, environ


def _darwin_libraries() -> tuple[Any, Any]:
    if _runtime_platform() != DARWIN_PLATFORM:
        raise CollectionError("Darwin native process APIs are unavailable")
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    libproc = ctypes.CDLL(
        ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib",
        use_errno=True,
    )
    libc.sysctl.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    libc.sysctl.restype = ctypes.c_int
    libc.sysctlbyname.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    libc.sysctlbyname.restype = ctypes.c_int
    libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libproc.proc_listallpids.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int
    return libc, libproc


def _darwin_procargs2(libc: Any, pid: int) -> bytes:
    # CTL_KERN, KERN_PROCARGS2, pid
    mib = (ctypes.c_int * 3)(1, 49, pid)
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "KERN_PROCARGS2 size")
    if size.value <= 0 or size.value > 16 * 1024 * 1024:
        raise OSError("unsafe KERN_PROCARGS2 size")
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "KERN_PROCARGS2 read")
    return buffer.raw[: size.value]


def _darwin_bsd_info(libproc: Any, pid: int) -> _DarwinBsdInfo:
    info = _DarwinBsdInfo()
    # PROC_PIDTBSDINFO
    received = libproc.proc_pidinfo(
        pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
    )
    if received != ctypes.sizeof(info):
        raise OSError(ctypes.get_errno(), "PROC_PIDTBSDINFO")
    return info


def _darwin_generation(info: _DarwinBsdInfo) -> int:
    seconds = int(info.pbi_start_tvsec)
    microseconds = int(info.pbi_start_tvusec)
    if seconds <= 0 or microseconds < 0 or microseconds >= 1_000_000:
        raise ValueError("invalid Darwin process start time")
    return seconds * 1_000_000 + microseconds


def _decode_c_string(value: bytes) -> str:
    return value.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def scan_darwin_process_table(
    max_nodes: int,
    *,
    pids: Sequence[int] | None = None,
    bsd_reader: Callable[[int], _DarwinBsdInfo] | None = None,
    args_reader: Callable[[int], bytes] | None = None,
) -> dict[int, dict[str, Any]]:
    """Read one bounded Darwin process view using libproc and KERN_PROCARGS2."""
    libc: Any = None
    libproc: Any = None
    if pids is None or bsd_reader is None or args_reader is None:
        libc, libproc = _darwin_libraries()
    if pids is None:
        capacity = max_nodes + 1
        buffer = (ctypes.c_int * capacity)()
        count = libproc.proc_listallpids(buffer, ctypes.sizeof(buffer))
        if count < 0:
            raise CollectionError("cannot enumerate Darwin processes")
        if count > max_nodes:
            raise CollectionError(
                f"Darwin has more than max_proc_nodes={max_nodes} processes"
            )
        pids = [int(buffer[index]) for index in range(count) if buffer[index] > 0]
    unique_pids = sorted(set(pids))
    if len(unique_pids) > max_nodes:
        raise CollectionError(
            f"Darwin has {len(unique_pids)} processes, above "
            f"max_proc_nodes={max_nodes}"
        )
    read_bsd = bsd_reader or (lambda pid: _darwin_bsd_info(libproc, pid))
    read_args = args_reader or (lambda pid: _darwin_procargs2(libc, pid))
    table: dict[int, dict[str, Any]] = {}
    for pid in unique_pids:
        try:
            before = read_bsd(pid)
            generation = _darwin_generation(before)
            argv, environ = _parse_darwin_procargs2(read_args(pid))
            after = read_bsd(pid)
            after_generation = _darwin_generation(after)
        except (OSError, ValueError):
            continue
        before_identity = (int(before.pbi_pid), int(before.pbi_ppid), generation)
        after_identity = (
            int(after.pbi_pid),
            int(after.pbi_ppid),
            after_generation,
        )
        if before_identity != after_identity or before_identity[0] != pid:
            continue
        table[pid] = {
            "pid": pid,
            "ppid": before_identity[1],
            "start_ticks": generation,
            "generation_kind": "darwin-start-usec",
            "cmdline": argv,
            "comm": clean_text(
                _decode_c_string(bytes(before.pbi_name))
                or _decode_c_string(bytes(before.pbi_comm)),
                128,
            ),
            "cwd": (
                environ.get("PWD", "")
                if environ.get("PWD", "").startswith("/")
                else ""
            ),
            "session_name": environ.get("SHPOOL_SESSION_NAME", ""),
            "codex_thread_id": environ.get("CODEX_THREAD_ID", ""),
            "claude_session_id": environ.get("CLAUDE_SESSION_ID", ""),
        }
    return table


def platform_process_table(
    proc_root: Path, max_nodes: int
) -> dict[int, dict[str, Any]]:
    platform = _require_supported_platform()
    if platform == DARWIN_PLATFORM:
        return scan_darwin_process_table(max_nodes)
    return scan_process_table(proc_root, max_nodes)


def _children_index(process_table: Mapping[int, Mapping[str, Any]]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for pid, process in process_table.items():
        children.setdefault(int(process.get("ppid", 0)), []).append(pid)
    for values in children.values():
        values.sort()
    return children


def _is_shpool_daemon(process: Mapping[str, Any]) -> bool:
    argv = process.get("cmdline") or []
    executable = Path(argv[0]).name if argv else process.get("comm", "")
    return executable == "shpool" and "daemon" in argv[1:]


def shpool_roots(
    session_names: Iterable[str], process_table: Mapping[int, Mapping[str, Any]]
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Map a shpool name only through a unique daemon direct child."""
    wanted = set(session_names)
    daemons = [pid for pid, process in process_table.items() if _is_shpool_daemon(process)]
    roots: dict[str, int] = {}
    diagnostics: dict[str, list[str]] = {name: [] for name in wanted}
    if len(daemons) != 1:
        reason = f"expected one shpool daemon in /proc, found {len(daemons)}"
        for name in wanted:
            diagnostics[name].append(reason)
        return roots, diagnostics
    daemon = daemons[0]
    for name in wanted:
        candidates = sorted(
            pid
            for pid, process in process_table.items()
            if process.get("ppid") == daemon and process.get("session_name") == name
        )
        if len(candidates) == 1:
            roots[name] = candidates[0]
        else:
            diagnostics[name].append(
                f"expected one daemon child for {name!r}, found {len(candidates)}"
            )
    return roots, diagnostics


def daemon_generation(
    process_table: Mapping[int, Mapping[str, Any]]
) -> dict[str, int] | None:
    candidates = [
        process for process in process_table.values() if _is_shpool_daemon(process)
    ]
    if len(candidates) != 1:
        return None
    pid = candidates[0].get("pid")
    start_ticks = candidates[0].get("start_ticks")
    if not isinstance(pid, int) or not isinstance(start_ticks, int):
        return None
    return {"pid": pid, "process_start_ticks": start_ticks}


def descendants(
    root_pid: int,
    children: Mapping[int, Sequence[int]],
    *,
    max_nodes: int,
    max_depth: int,
) -> list[int]:
    """Bounded breadth-first descendant walk, including root_pid."""
    found: list[int] = []
    seen: set[int] = set()
    queue: list[tuple[int, int]] = [(root_pid, 0)]
    while queue:
        pid, depth = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        found.append(pid)
        if len(found) > max_nodes:
            raise CollectionError(f"process tree rooted at PID {root_pid} exceeded max_proc_nodes")
        if depth >= max_depth:
            continue
        queue.extend((child, depth + 1) for child in children.get(pid, ()))
    return found


def _arg_value(argv: Sequence[str], option: str) -> str:
    try:
        index = argv.index(option)
    except ValueError:
        return ""
    return argv[index + 1] if index + 1 < len(argv) else ""


def claude_subagents(
    root_uuid: str, process_table: Mapping[int, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for pid, process in process_table.items():
        argv = process.get("cmdline") or []
        if _arg_value(argv, "--parent-session-id").lower() != root_uuid:
            continue
        title = clean_text(_arg_value(argv, "--agent-name"), 80) or f"PID {pid}"
        result.append(
            {
                "provider": "claude",
                "uuid": None,
                "pid": pid,
                "title": title,
                "status": "running",
            }
        )
    return sorted(result, key=lambda item: (item["title"].casefold(), item["pid"]))


def _is_native_codex(process: Mapping[str, Any]) -> bool:
    argv = process.get("cmdline") or []
    if not argv:
        return False
    executable = Path(argv[0]).name
    return executable == "codex" and process.get("comm") != "node"


ROLLOUT_TAIL_BYTES = _providers.ROLLOUT_TAIL_BYTES
ROLLOUT_LIFECYCLE_SEARCH_BYTES = _providers.ROLLOUT_LIFECYCLE_SEARCH_BYTES


def _rollout_turn_state(descriptor: int) -> str:
    """Compatibility facade for the provider-specific structured parser."""
    return _providers.rollout_turn_state(descriptor)


def _rollout_meta_fd(descriptor: int) -> dict[str, Any] | None:
    """Parse bounded rollout metadata from an already-open descriptor."""
    try:
        raw = os.pread(descriptor, 1024 * 1024, 0)
    except OSError:
        return None
    for line in raw.splitlines()[:64]:
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            continue
        if event.get("type") == "session_meta" and isinstance(
            event.get("payload"), Mapping
        ):
            meta = dict(event["payload"])
            meta["_turn_state"] = _rollout_turn_state(descriptor)
            return meta
    return None


def _expected_proc_identity(
    proc_root: Path, pid: int, expected_process: Mapping[str, Any]
) -> tuple[int, int, int] | None:
    try:
        identity = _proc_stat(proc_root / str(pid) / "stat")
    except (OSError, ValueError):
        return None
    expected = (
        pid,
        expected_process.get("ppid"),
        expected_process.get("start_ticks"),
    )
    return identity if identity == expected else None


def codex_open_rollouts(
    pid: int,
    proc_root: Path,
    codex_home: Path,
    expected_process: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Read metadata through stable, native Codex-owned proc descriptors."""
    initial_identity = _expected_proc_identity(
        proc_root, pid, expected_process
    )
    if initial_identity is None:
        return []
    fd_dir = proc_root / str(pid) / "fd"
    try:
        descriptors = sorted(fd_dir.iterdir(), key=lambda item: int(item.name))
    except (OSError, ValueError):
        return []
    home_resolved = codex_home.resolve(strict=False)
    result: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for descriptor in descriptors:
        before_identity = _expected_proc_identity(
            proc_root, pid, expected_process
        )
        if before_identity != initial_identity:
            return []
        try:
            opened = os.open(
                descriptor,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError:
            continue
        try:
            opened_stat = os.fstat(opened)
            if not statmod.S_ISREG(opened_stat.st_mode):
                continue
            actual_link = Path(os.readlink(f"/proc/self/fd/{opened}"))
            actual_text = str(actual_link)
            if actual_text.endswith(" (deleted)"):
                actual_link = Path(actual_text[: -len(" (deleted)")])
            if (
                "rollout-" not in actual_link.name
                or actual_link.suffix != ".jsonl"
            ):
                continue
            resolved = actual_link.resolve(strict=False)
            resolved.relative_to(home_resolved)
            file_identity = (opened_stat.st_dev, opened_stat.st_ino)
            if file_identity in seen:
                continue
            meta = _rollout_meta_fd(opened)
            after_stat = os.fstat(opened)
            if (
                opened_stat.st_dev,
                opened_stat.st_ino,
                opened_stat.st_mode,
            ) != (
                after_stat.st_dev,
                after_stat.st_ino,
                after_stat.st_mode,
            ):
                continue
        except (OSError, ValueError):
            continue
        finally:
            os.close(opened)
        after_identity = _expected_proc_identity(
            proc_root, pid, expected_process
        )
        if after_identity != before_identity:
            return []
        seen.add(file_identity)
        if meta is not None:
            result.append(meta)
    if _expected_proc_identity(proc_root, pid, expected_process) != initial_identity:
        return []
    return result


def index_codex_processes(
    process_table: Mapping[int, Mapping[str, Any]], proc_root: Path, codex_home: Path
) -> dict[int, list[dict[str, Any]]]:
    if _runtime_platform() == DARWIN_PLATFORM:
        result: dict[int, list[dict[str, Any]]] = {}
        for pid, process in process_table.items():
            if not _is_native_codex(process):
                continue
            uuid = valid_uuid(process.get("codex_thread_id"))
            result[pid] = (
                [
                    {
                        "session_id": uuid,
                        "id": uuid,
                        "source": "cli",
                        "_turn_state": "state unavailable",
                    }
                ]
                if uuid
                else []
            )
        return result
    return {
        pid: codex_open_rollouts(
            pid, proc_root, codex_home, process
        )
        for pid, process in process_table.items()
        if _is_native_codex(process)
    }


def _root_codex_uuid(metadata: Sequence[Mapping[str, Any]]) -> list[str]:
    identities: set[str] = set()
    for meta in metadata:
        uuid = valid_uuid(meta.get("session_id"))
        event_id = valid_uuid(meta.get("id"))
        if meta.get("source") == "cli" and uuid and event_id == uuid:
            identities.add(uuid)
    return sorted(identities)


def _codex_turn_state(
    metadata: Sequence[Mapping[str, Any]], wanted_uuid: str
) -> str:
    """Turn state for one exact conversation, from its own rollout metadata."""
    for meta in metadata:
        uuid = valid_uuid(meta.get("session_id"))
        event_id = valid_uuid(meta.get("id"))
        if meta.get("source") == "cli" and uuid == wanted_uuid and event_id == uuid:
            state = meta.get("_turn_state")
            if state in {
                "needs your reply",
                "reply optional",
                "working",
                "idle",
                "state unavailable",
            }:
                return str(state)
    return "state unavailable"


def _codex_paths() -> tuple[Path, Path]:
    codex_home = Path(
        os.environ.get("SESSION_KIT_CODEX_HOME", str(_home() / ".codex"))
    ).expanduser()
    db_path = Path(
        os.environ.get("SESSION_KIT_CODEX_DB", str(codex_home / "state_5.sqlite"))
    ).expanduser()
    return codex_home, db_path


def _read_bounded_owner_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    exact_mode: int | None = None,
    allow_missing: bool = False,
) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise CollectionError(f"{label} does not exist: {path}")
    except OSError as exc:
        raise CollectionError(f"cannot open {label}: {path}") from exc
    try:
        before = os.fstat(descriptor)
        try:
            pathname = path.lstat()
        except OSError as exc:
            raise CollectionError(f"cannot inspect {label}: {path}") from exc
        mode = statmod.S_IMODE(before.st_mode)
        if (
            not statmod.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > max_bytes
            or (exact_mode is not None and mode != exact_mode)
            or (exact_mode is None and mode & 0o022)
            or (before.st_dev, before.st_ino) != (pathname.st_dev, pathname.st_ino)
        ):
            raise CollectionError(f"unsafe {label}: {path}")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, 65536))
            if not block:
                raise CollectionError(f"short read from {label}: {path}")
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise CollectionError(f"{label} grew while reading: {path}")
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable):
            raise CollectionError(f"{label} changed while reading: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_codex_session_index(
    path: Path, warning_sink: list[str] | None = None
) -> dict[str, str]:
    """Read exact UUID-bound Codex names from the append-only local index."""
    payload = _read_bounded_owner_file(
        path,
        label="Codex session index",
        max_bytes=MAX_CODEX_SESSION_INDEX_BYTES,
        allow_missing=True,
    )
    if payload is None:
        return {}
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise CollectionError("Codex session index is not valid UTF-8") from exc
    names: dict[str, str] = {}
    skipped = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(item, Mapping):
            skipped += 1
            continue
        uuid = valid_uuid(item.get("id"))
        title = clean_text(item.get("thread_name"), 120)
        if not uuid or not title:
            skipped += 1
            continue
        names[uuid] = title
    if skipped and warning_sink is not None:
        warning_sink.append(
            f"Codex session index ignored {skipped} malformed or unnamed "
            f"entr{'y' if skipped == 1 else 'ies'}"
        )
    return names


def read_codex_db(
    db_path: Path,
    session_index_names: Mapping[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Read titles and spawn edges from Codex state without creating journal files."""
    index_names = (
        dict(session_index_names)
        if session_index_names is not None
        else read_codex_session_index(db_path.parent / "session_index.jsonl")
    )
    threads = {
        uuid: {
            "id": uuid,
            "title": "",
            "cwd": "",
            "session_index_name": clean_text(title, 120),
        }
        for raw_uuid, title in index_names.items()
        if (uuid := valid_uuid(raw_uuid)) and clean_text(title, 120)
    }
    if not db_path.is_file():
        return threads, {}
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(threads)").fetchall()
        }
        required = {"id", "title", "cwd"}
        if not required.issubset(columns):
            raise CollectionError("Codex threads table is missing required columns")
        optional = [
            name
            for name in (
                "name",
                "agent_nickname",
                "agent_path",
                "thread_source",
                "first_user_message",
            )
            if name in columns
        ]
        select = ", ".join(["id", "title", "cwd", *optional])
        database_threads = {
            row["id"]: dict(row)
            for row in connection.execute(f"SELECT {select} FROM threads").fetchall()
            if valid_uuid(row["id"])
        }
        for uuid, thread in database_threads.items():
            index_title = threads.get(uuid, {}).get("session_index_name")
            threads[uuid] = thread
            if index_title:
                threads[uuid]["session_index_name"] = index_title
        edges: dict[str, list[dict[str, Any]]] = {}
        edge_tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "thread_spawn_edges" in edge_tables:
            for row in connection.execute(
                "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges"
            ).fetchall():
                child = threads.get(row["child_thread_id"], {})
                edges.setdefault(row["parent_thread_id"], []).append(
                    {
                        "provider": "codex",
                        "uuid": valid_uuid(row["child_thread_id"]),
                        "pid": None,
                        "title": clean_text(
                            child.get("session_index_name")
                            or child.get("name")
                            or child.get("agent_nickname")
                            or child.get("agent_path")
                            or child.get("title"),
                            80,
                        )
                        or str(row["child_thread_id"])[:8],
                        "status": clean_text(row["status"], 40) or "unknown",
                    }
                )
        for items in edges.values():
            items.sort(key=lambda item: (item["title"].casefold(), item["uuid"] or ""))
        return threads, edges
    finally:
        connection.close()


def recovery_spec(provider: str, uuid: str, cwd: str | None = None) -> dict[str, Any]:
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise ValueError("recovery requires provider claude|codex and an exact UUID")
    argv = (
        ["claude", "--resume", exact_uuid]
        if provider == "claude"
        else ["codex", "--no-alt-screen", "resume", exact_uuid]
    )
    clean_cwd = clean_text(cwd, 4096) if cwd else None
    command = shlex.join(argv)
    if clean_cwd:
        command = f"cd -- {shlex.quote(clean_cwd)} && {command}"
    return {
        "available": True,
        "provider": provider,
        "uuid": exact_uuid,
        "cwd": clean_cwd,
        "argv": argv,
        "command": command,
    }


def _empty_recovery() -> dict[str, Any]:
    return {
        "available": False,
        "provider": None,
        "uuid": None,
        "cwd": None,
        "argv": [],
        "command": None,
    }


def _agent_identity(
    *,
    provider: str,
    uuid: str | None,
    pid: int | None,
    process_table: Mapping[int, Mapping[str, Any]],
    provenance: str,
    confidence: str,
) -> dict[str, Any]:
    process = process_table.get(pid or -1, {})
    return {
        "uuid": uuid,
        "pid": pid,
        "process_start_ticks": process.get("start_ticks"),
        "provenance": provenance,
        "confidence": confidence,
    }


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
    return _provider_title_info(
        provider,
        uuid,
        native_title,
        aliases,
        cwd,
        started_at_unix_ms,
        automatic_titles,
        provider_title_is_explicit=provider_title_is_explicit,
    )[0]


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
    key = f"{provider}:{uuid}" if uuid else ""
    if uuid:
        alias = aliases.get(key)
        if alias:
            return alias, "alias"
    if native_title and provider_title_is_explicit:
        return native_title, "native"
    if uuid and automatic_naming_enabled():
        automatic = (automatic_titles or {}).get(key)
        if automatic:
            return automatic, "automatic"
    context = _context_title(provider, cwd, started_at_unix_ms)
    if context:
        return context, "context"
    if uuid:
        return f"{provider.title()} {uuid[:8]}", "uuid"
    return provider.title(), "provider"


def _context_title(
    provider: str, cwd: str, started_at_unix_ms: int | None
) -> str:
    """Return a deterministic project hint without exposing a full path."""
    if (
        isinstance(started_at_unix_ms, bool)
        or not isinstance(started_at_unix_ms, int)
        or started_at_unix_ms <= 0
    ):
        return ""
    try:
        local_start = dt.datetime.fromtimestamp(
            started_at_unix_ms / 1000, tz=dt.timezone.utc
        ).astimezone()
    except (OverflowError, OSError, ValueError):
        return ""
    timestamp = local_start.strftime("%b %d %H:%M")
    clean_cwd = clean_text(cwd, 4096)
    if not clean_cwd or not clean_cwd.startswith("/"):
        return f"{provider.title()} started {timestamp}"
    parts = [
        clean_text(part, 40)
        for part in Path(clean_cwd).parts
        if part not in {"", os.sep}
    ]
    generic = {
        "app",
        "apps",
        "code",
        "current",
        "home",
        "repo",
        "repos",
        "src",
        "srv",
        "v2",
        "var",
        "workspace",
        "workspaces",
    }
    home_name = clean_text(_home().name, 40).casefold()
    for part in reversed(parts):
        folded = part.casefold()
        if (
            part
            and not part.startswith(".")
            and folded not in generic
            and folded != home_name
        ):
            return f"{provider.title()} in {part} at {timestamp}"
    return f"{provider.title()} started {timestamp}"


def _shell_title(
    tree: Sequence[int], root_pid: int, process_table: Mapping[int, Mapping[str, Any]]
) -> tuple[str, str, int | None]:
    for pid in tree:
        if pid == root_pid:
            continue
        process = process_table.get(pid, {})
        comm = clean_text(process.get("comm"), 80)
        if comm and comm not in {"bash", "zsh", "fish", "script"}:
            return comm, clean_text(process.get("cwd"), 4096), pid
    root = process_table.get(root_pid, {})
    return "Idle shell", clean_text(root.get("cwd"), 4096), root_pid


def _base_agent(
    *,
    row: int | None,
    shpool_id: str | None,
    shpool_status: str | None,
    availability: str | None,
    provider: str,
    identity: dict[str, Any],
    title: str,
    title_source: str,
    native_title: str,
    cwd: str,
    started_at_unix_ms: int | None,
    process_age_seconds: int | None,
    agent_status: str,
    needs_you: bool,
    subagents: list[dict[str, Any]],
    recovery: dict[str, Any],
    diagnostics: list[str],
    shpool_shell: dict[str, Any] | None,
) -> dict[str, Any]:
    mutation_allowed, mutation_reason = (
        shpool_id_mutation_policy(shpool_id)
        if shpool_id is not None
        else (False, "outside-shpool")
    )
    return {
        "row": row,
        "terminal_number": None,
        "shpool_id": shpool_id,
        "shpool_id_raw": shpool_id,
        "display_shpool_id": (
            display_shpool_id(shpool_id) if shpool_id is not None else None
        ),
        "mutation_allowed": mutation_allowed,
        "mutation_rejection_reason": mutation_reason,
        "shpool_status": shpool_status,
        "availability": availability,
        "provider": provider,
        "display_provider": provider,
        "setup_incomplete": False,
        "identity": identity,
        "title": clean_text(title, 120),
        "title_source": clean_text(title_source, 20),
        "display_title": clean_text(title, 120),
        "native_title": clean_text(native_title, 120),
        "cwd": clean_text(cwd, 4096),
        "started_at_unix_ms": started_at_unix_ms,
        "process_age_seconds": process_age_seconds,
        "agent_status": clean_text(agent_status, 60) or "unknown",
        "needs_you": bool(needs_you),
        "subagents": subagents,
        "recovery": recovery,
        "diagnostics": diagnostics,
        "shpool_shell": shpool_shell,
    }


def _process_age(pid: int | None, process_table: Mapping[int, Mapping[str, Any]], now: float) -> int | None:
    if not pid:
        return None
    ticks = process_table.get(pid, {}).get("start_ticks")
    if not isinstance(ticks, int):
        return None
    if process_table.get(pid, {}).get("generation_kind") == "darwin-start-usec":
        age = now - (ticks / 1_000_000)
        return max(0, int(age)) if age >= 0 else None
    try:
        hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, KeyError):
        return None
    age = uptime - (ticks / hz)
    return max(0, int(age)) if age >= 0 else None


def _regular_file_mtime_ms(path: Path) -> int | None:
    """Return an exact regular file's mtime without following a symlink."""
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not statmod.S_ISREG(metadata.st_mode):
        return None
    return metadata.st_mtime_ns // 1_000_000


def recent_output_times(
    shpool_ids: Iterable[str],
    *,
    journal_dir: Path | None = None,
    recovery_dir: Path | None = None,
) -> dict[str, int]:
    """Map operational shpool IDs to their exact active journal mtime.

    Legacy sessions may still write through a recovery-map path after their
    original inode was moved.  A valid exact map entry takes precedence, just
    as ``sp history`` does.  Otherwise the collector checks the raw-ID legacy
    file or every regular segment in the raw-ID directory.  Unmanaged IDs,
    symlinks, missing files, and ambiguous map entries have no activity time.
    """
    wanted = {
        value
        for value in shpool_ids
        if shpool_id_mutation_policy(value) == (True, None)
    }
    if not wanted:
        return {}
    active_root = journal_dir or default_journal_dir()
    recovered_root = recovery_dir or default_journal_recovery_dir()
    mapped: dict[str, Path | None] = {}
    ambiguous_mappings: set[str] = set()
    map_path = recovered_root / "current-map.tsv"
    try:
        map_metadata = map_path.lstat()
        if not statmod.S_ISREG(map_metadata.st_mode):
            raise OSError("recovery map is not a regular file")
        with map_path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                raw_id, separator, raw_path = raw_line.rstrip("\r\n").partition("\t")
                if not separator or raw_id not in wanted or not raw_path.startswith("/"):
                    continue
                candidate = Path(raw_path)
                previous = mapped.get(raw_id)
                if raw_id in mapped and previous != candidate:
                    mapped[raw_id] = None
                    ambiguous_mappings.add(raw_id)
                else:
                    mapped[raw_id] = candidate
    except OSError:
        pass

    result: dict[str, int] = {}
    for raw_id in wanted:
        if raw_id in ambiguous_mappings:
            continue
        mapped_path = mapped.get(raw_id)
        if mapped_path is not None:
            mapped_mtime = _regular_file_mtime_ms(mapped_path)
            if mapped_mtime is not None:
                result[raw_id] = mapped_mtime
                continue

        legacy_mtime = _regular_file_mtime_ms(active_root / f"{raw_id}.raw")
        if legacy_mtime is not None:
            result[raw_id] = legacy_mtime
            continue

        segment_dir = active_root / raw_id
        try:
            directory_metadata = segment_dir.lstat()
            if not statmod.S_ISDIR(directory_metadata.st_mode):
                continue
            segment_mtimes = [
                timestamp
                for path in segment_dir.iterdir()
                if re.fullmatch(r"segment-[0-9]+\.raw", path.name)
                for timestamp in [_regular_file_mtime_ms(path)]
                if timestamp is not None
            ]
        except OSError:
            continue
        if segment_mtimes:
            result[raw_id] = max(segment_mtimes)
    return result


def _read_private_launch_file(directory_fd: int, name: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if (
            not statmod.S_ISREG(before.st_mode)
            or statmod.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > 8192
        ):
            raise CollectionError("unsafe retained launch record")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, 4096))
            if not block:
                raise CollectionError("short retained launch record")
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise CollectionError("retained launch record grew while reading")
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable):
            raise CollectionError("retained launch record changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _launch_fields(payload: bytes, count: int) -> list[str] | None:
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None
    if (
        not text.endswith("\n")
        or text.count("\n") != 1
        or "\r" in text
    ):
        return None
    fields = text[:-1].split("\t")
    return fields if len(fields) == count else None


def apply_retained_setup_attributions(
    inventory: dict[str, Any],
    *,
    start_dir: Path | None = None,
    boot_id: str | None = None,
) -> dict[str, Any]:
    """Add display-only provider hints from exact retained startup proofs."""
    root = start_dir or default_start_dir()
    current_boot_id = boot_id if boot_id is not None else _boot_id()
    if not current_boot_id or "\t" in current_boot_id or "\n" in current_boot_id:
        return inventory
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(root, flags)
    except OSError:
        return inventory
    try:
        directory = os.fstat(directory_fd)
        if (
            not statmod.S_ISDIR(directory.st_mode)
            or statmod.S_IMODE(directory.st_mode) != 0o700
            or directory.st_uid != os.geteuid()
        ):
            return inventory
        generation = inventory.get("daemon_generation")
        if not isinstance(generation, Mapping):
            return inventory
        daemon_pid = generation.get("pid")
        daemon_start = generation.get("process_start_ticks")
        if (
            isinstance(daemon_pid, bool)
            or not isinstance(daemon_pid, int)
            or daemon_pid <= 0
            or isinstance(daemon_start, bool)
            or not isinstance(daemon_start, int)
            or daemon_start <= 0
        ):
            return inventory

        for item in inventory.get("sessions", ()):
            if not isinstance(item, dict) or item.get("provider") != "unknown":
                continue
            raw_id = item.get("shpool_id_raw")
            if shpool_id_mutation_policy(raw_id) != (True, None):
                continue
            shell = item.get("shpool_shell")
            identity = item.get("identity")
            started = item.get("started_at_unix_ms")
            if (
                not isinstance(shell, Mapping)
                or not isinstance(identity, Mapping)
                or identity.get("confidence") != "unknown"
                or identity.get("uuid") is not None
                or identity.get("pid") != shell.get("pid")
                or identity.get("process_start_ticks")
                != shell.get("process_start_ticks")
                or isinstance(started, bool)
                or not isinstance(started, int)
                or started <= 0
                or isinstance(shell.get("pid"), bool)
                or not isinstance(shell.get("pid"), int)
                or shell.get("pid", 0) <= 0
                or isinstance(shell.get("process_start_ticks"), bool)
                or not isinstance(shell.get("process_start_ticks"), int)
                or shell.get("process_start_ticks", 0) <= 0
            ):
                continue
            try:
                start_payload = _read_private_launch_file(directory_fd, raw_id)
                expected_payload = _read_private_launch_file(
                    directory_fd, f"{raw_id}.expected"
                )
            except (CollectionError, OSError):
                continue
            start = _launch_fields(start_payload, 4)
            if start is None:
                legacy_start = _launch_fields(start_payload, 3)
                if legacy_start is not None:
                    legacy_uuid = legacy_start[2]
                    start = [
                        *legacy_start,
                        "resume" if legacy_uuid else "new",
                    ]
            expected = _launch_fields(expected_payload, 10)
            if expected is None:
                legacy_expected = _launch_fields(expected_payload, 9)
                if legacy_expected is not None:
                    legacy_uuid = legacy_expected[8]
                    expected = [
                        *legacy_expected,
                        "resume" if legacy_uuid else "new",
                    ]
            if start is None or expected is None:
                continue
            provider, cwd, uuid, launch_mode = start
            (
                side_provider,
                side_cwd,
                side_boot_id,
                side_started,
                side_shell_pid,
                side_shell_start,
                side_daemon_pid,
                side_daemon_start,
                side_uuid,
                side_launch_mode,
            ) = expected
            numeric = (
                side_started,
                side_shell_pid,
                side_shell_start,
                side_daemon_pid,
                side_daemon_start,
            )
            if any(re.fullmatch(r"[1-9][0-9]*", value) is None for value in numeric):
                continue
            if (
                provider not in {"claude", "codex"}
                or launch_mode not in {"new", "resume", "fork"}
                or side_launch_mode != launch_mode
                or (launch_mode == "new" and uuid != "")
                or (
                    launch_mode in {"resume", "fork"}
                    and valid_uuid(uuid) != uuid
                )
                or side_provider != provider
                or side_cwd != cwd
                or item.get("cwd") != cwd
                or side_boot_id != current_boot_id
                or int(side_started) != started
                or int(side_shell_pid) != shell.get("pid")
                or int(side_shell_start) != shell.get("process_start_ticks")
                or int(side_daemon_pid) != daemon_pid
                or int(side_daemon_start) != daemon_start
                or side_uuid != uuid
                or (uuid and valid_uuid(uuid) != uuid)
            ):
                continue
            item["display_provider"] = provider
            item["setup_incomplete"] = True
            item["display_title"] = f"{provider.title()} setup incomplete"
            if uuid and launch_mode == "resume":
                item["_terminal_identity_hint"] = {
                    "provider": provider,
                    "uuid": uuid,
                }
    finally:
        os.close(directory_fd)
    return inventory


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
    current_time = time.time() if now is None else now
    shpool_sessions = _parse_shpool_payload(shpool_payload)
    claude_agents = _parse_claude_payload(claude_payload)
    claude_by_pid = {item["pid"]: item for item in claude_agents}
    aliases = _valid_aliases(config.get("aliases"))
    automatic_titles = _valid_automatic_titles(config.get("automatic_titles"))
    automatic_failures = _valid_automatic_title_failures(
        config.get("automatic_title_failures")
    )
    codex_threads, codex_edges = codex_db_rows
    roots, root_diagnostics = shpool_roots(
        (item["name"] for item in shpool_sessions), process_table
    )
    child_index = _children_index(process_table)
    recent_outputs = recent_output_by_shpool_id or {}
    mapped_claude: set[int] = set()
    mapped_codex: set[int] = set()
    sessions: list[dict[str, Any]] = []

    for shpool in shpool_sessions:
        name = shpool["name"]
        status = shpool["status"]
        availability = "ready" if status == "Disconnected" else "attached"
        diagnostics = list(root_diagnostics.get(name, ()))
        root_pid = roots.get(name)
        tree: list[int] = []
        if root_pid:
            try:
                tree = descendants(
                    root_pid,
                    child_index,
                    max_nodes=int(
                        config.get("max_proc_nodes", DEFAULT_MAX_PROC_NODES)
                    ),
                    max_depth=int(config.get("max_proc_depth", 32)),
                )
            except CollectionError as exc:
                diagnostics.append(str(exc))
        claude_candidates = [
            pid
            for pid in tree
            if pid in claude_by_pid
            and claude_by_pid[pid].get("kind") in {"", "interactive"}
        ]
        codex_candidates: list[tuple[int, str]] = []
        for pid in tree:
            for uuid in _root_codex_uuid(codex_index.get(pid, ())):
                codex_candidates.append((pid, uuid))
        codex_candidates = sorted(set(codex_candidates))

        starting_provider = None
        provider_title_is_explicit = True
        if len(claude_candidates) == 1 and not codex_candidates:
            pid = claude_candidates[0]
            agent = claude_by_pid[pid]
            uuid = agent["uuid"]
            native_title = agent["title"]
            # Claude persists its conversation auto-title as a transcript
            # ai-title record while the session record keeps a derived window
            # label. The visible conversation title outranks the derived
            # label; any explicit rename keeps outranking both.
            if agent.get("ai_title") and agent.get("name_source") == "derived":
                native_title = agent["ai_title"]
            provider = "claude"
            cwd = agent["cwd"] or clean_text(process_table.get(pid, {}).get("cwd"), 4096)
            identity = _agent_identity(
                provider=provider,
                uuid=uuid,
                pid=pid,
                process_table=process_table,
                provenance="claude agents --json",
                confidence="exact",
            )
            subagents = claude_subagents(uuid, process_table)
            recovery = recovery_spec(provider, uuid, cwd or None)
            mapped_claude.add(pid)
            agent_status = agent["status"]
            needs_you = agent["needs_you"]
        elif len(codex_candidates) == 1 and not claude_candidates:
            pid, uuid = codex_candidates[0]
            thread = codex_threads.get(uuid, {})
            native_title = clean_text(
                thread.get("session_index_name")
                or thread.get("name")
                or thread.get("title"),
                120,
            )
            # The session-index name is exact rename evidence. A database
            # title also counts as a real name on current Codex, which
            # auto-titles threads there — recognizable by the schema carrying
            # first_user_message separately. Older stores kept the raw prompt
            # in the title, and a title that still echoes the first prompt is
            # that same behavior on any schema.
            db_title = clean_text(thread.get("title"), 120)
            first_message = clean_text(thread.get("first_user_message"), 200)
            has_split_prompt_schema = "first_user_message" in thread
            title_echoes_prompt = bool(db_title) and bool(first_message) and (
                first_message.casefold().startswith(db_title.casefold())
                or db_title.casefold().startswith(first_message.casefold())
            )
            provider_title_is_explicit = bool(
                thread.get("session_index_name")
            ) or (
                has_split_prompt_schema
                and bool(db_title)
                and not title_echoes_prompt
            )
            provider = "codex"
            cwd = clean_text(
                thread.get("cwd") or process_table.get(pid, {}).get("cwd"), 4096
            )
            identity = _agent_identity(
                provider=provider,
                uuid=uuid,
                pid=pid,
                process_table=process_table,
                provenance="native codex PID open source=cli rollout",
                confidence="exact",
            )
            subagents = list(codex_edges.get(uuid, ()))
            recovery = recovery_spec(provider, uuid, cwd or None)
            mapped_codex.add(pid)
            turn_state = _codex_turn_state(codex_index.get(pid, ()), uuid)
            agent_status = turn_state
            needs_you = turn_state == "needs your reply"
        else:
            known_provider_process = any(
                clean_text(process_table.get(pid, {}).get("comm"), 128) in {"claude", "codex"}
                or _is_native_codex(process_table.get(pid, {}))
                for pid in tree
            )
            ambiguous = len(claude_candidates) > 1 or len(codex_candidates) > 1 or (
                bool(claude_candidates) and bool(codex_candidates)
            )
            if ambiguous or known_provider_process or not root_pid:
                provider = "unknown"
                pid = root_pid
                native_title = "Unresolved provider session"
                cwd = clean_text(process_table.get(root_pid or -1, {}).get("cwd"), 4096)
                diagnostics.append(
                    f"identity candidates: Claude={len(claude_candidates)}, Codex={len(codex_candidates)}"
                )
                agent_status = "unknown"
                # A new Codex session has no rollout until its first message, so
                # it cannot be identified — but calling it "unresolved" reads as
                # broken when it is simply unused. Name what is actually running
                # so the row is recognisable. Identity stays unknown: this is a
                # label, and it grants nothing.
                if not ambiguous:
                    running = {
                        "codex"
                        if _is_native_codex(process_table.get(candidate, {}))
                        or clean_text(
                            process_table.get(candidate, {}).get("comm"), 128
                        )
                        == "codex"
                        else clean_text(
                            process_table.get(candidate, {}).get("comm"), 128
                        )
                        for candidate in tree
                    }
                    for candidate_provider in ("codex", "claude"):
                        if candidate_provider in running:
                            starting_provider = candidate_provider
                            native_title = (
                                f"{candidate_provider.title()} started, no messages yet"
                            )
                            break
            else:
                provider = "shell"
                native_title, cwd, pid = _shell_title(tree, root_pid, process_table)
                agent_status = "idle" if native_title == "Idle shell" else "running"
            uuid = None
            identity = _agent_identity(
                provider=provider,
                uuid=None,
                pid=pid,
                process_table=process_table,
                provenance="process tree" if root_pid else "none",
                confidence="unknown" if provider == "unknown" else "exact",
            )
            subagents = []
            recovery = _empty_recovery()
            needs_you = False

        title, title_source = _provider_title_info(
            provider,
            uuid,
            native_title,
            aliases,
            cwd,
            shpool["started_at_unix_ms"],
            automatic_titles,
            provider_title_is_explicit=provider_title_is_explicit,
        )
        recent_output_at = recent_outputs.get(name)
        if (
            isinstance(recent_output_at, bool)
            or not isinstance(recent_output_at, int)
            or recent_output_at < 0
        ):
            recent_output_at = None
        sessions.append(
            _base_agent(
                row=None,
                shpool_id=name,
                shpool_status=status,
                availability=availability,
                provider=provider,
                identity=identity,
                title=title,
                title_source=title_source,
                native_title=native_title,
                cwd=cwd,
                started_at_unix_ms=shpool["started_at_unix_ms"],
                process_age_seconds=_process_age(identity.get("pid"), process_table, current_time),
                agent_status=agent_status,
                needs_you=needs_you,
                subagents=subagents,
                recovery=recovery,
                diagnostics=diagnostics,
                shpool_shell=(
                    {
                        "pid": root_pid,
                        "process_start_ticks": process_table.get(root_pid, {}).get(
                            "start_ticks"
                        ),
                    }
                    if root_pid
                    else None
                ),
            )
        )
        if provider == "unknown" and starting_provider:
            # Display only. `provider` stays "unknown", so identity, mutation
            # and recovery decisions are completely unaffected.
            sessions[-1]["display_provider"] = starting_provider
        sessions[-1]["recent_output_at_unix_ms"] = recent_output_at
        sessions[-1]["recent_output_age_seconds"] = (
            max(0, int(current_time - (recent_output_at / 1000)))
            if recent_output_at is not None
            else None
        )
        title_key = f"{provider}:{uuid}" if uuid else ""
        attempts = automatic_failures.get(title_key, 0)
        if provider not in PROVIDERS or not uuid:
            automatic_state = "not-applicable"
        elif not automatic_naming_enabled():
            automatic_state = "disabled"
        elif title_source in {"alias", "native"}:
            automatic_state = "not-needed"
        elif title_source == "automatic":
            automatic_state = "ready"
        elif attempts >= 2:
            automatic_state = "failed"
        else:
            automatic_state = "pending"
        sessions[-1]["automatic_name_state"] = automatic_state
        sessions[-1]["automatic_name_attempts"] = attempts

    outside_agents: list[dict[str, Any]] = []
    for agent in claude_agents:
        if agent["pid"] in mapped_claude or agent["kind"] not in {"", "interactive"}:
            continue
        uuid = agent["uuid"]
        recovery = recovery_spec("claude", uuid, agent["cwd"] or None)
        outside_title, outside_title_source = _provider_title_info(
            "claude",
            uuid,
            agent["title"],
            aliases,
            agent["cwd"],
            agent["started_at_unix_ms"],
            automatic_titles,
        )
        outside_agents.append(
            _base_agent(
                row=None,
                shpool_id=None,
                shpool_status=None,
                availability=None,
                provider="claude",
                identity=_agent_identity(
                    provider="claude",
                    uuid=uuid,
                    pid=agent["pid"],
                    process_table=process_table,
                    provenance="claude agents --json",
                    confidence="exact",
                ),
                title=outside_title,
                title_source=outside_title_source,
                native_title=agent["title"],
                cwd=agent["cwd"],
                started_at_unix_ms=agent["started_at_unix_ms"],
                process_age_seconds=_process_age(agent["pid"], process_table, current_time),
                agent_status=agent["status"],
                needs_you=agent["needs_you"],
                subagents=claude_subagents(uuid, process_table),
                recovery=recovery,
                diagnostics=["active provider root is outside shpool"],
                shpool_shell=None,
            )
        )
    for pid, metadata in codex_index.items():
        if pid in mapped_codex:
            continue
        identities = _root_codex_uuid(metadata)
        if len(identities) != 1:
            continue
        uuid = identities[0]
        thread = codex_threads.get(uuid, {})
        native_title = clean_text(
            thread.get("session_index_name")
            or thread.get("name")
            or thread.get("title"),
            120,
        )
        cwd = clean_text(thread.get("cwd") or process_table.get(pid, {}).get("cwd"), 4096)
        outside_title, outside_title_source = _provider_title_info(
            "codex",
            uuid,
            native_title,
            aliases,
            cwd,
            None,
            automatic_titles,
            provider_title_is_explicit=bool(thread.get("session_index_name")),
        )
        turn_state = _codex_turn_state(metadata, uuid)
        outside_agents.append(
            _base_agent(
                row=None,
                shpool_id=None,
                shpool_status=None,
                availability=None,
                provider="codex",
                identity=_agent_identity(
                    provider="codex",
                    uuid=uuid,
                    pid=pid,
                    process_table=process_table,
                    provenance="native codex PID open source=cli rollout",
                    confidence="exact",
                ),
                title=outside_title,
                title_source=outside_title_source,
                native_title=native_title,
                cwd=cwd,
                started_at_unix_ms=None,
                process_age_seconds=_process_age(pid, process_table, current_time),
                agent_status=turn_state,
                needs_you=turn_state == "needs your reply",
                subagents=list(codex_edges.get(uuid, ())),
                recovery=recovery_spec("codex", uuid, cwd or None),
                diagnostics=["active provider root is outside shpool"],
                shpool_shell=None,
            )
        )
    sessions.sort(
        key=lambda item: (
            AVAILABILITY_ORDER.get(item["availability"], 9),
            PROVIDER_ORDER.get(item["provider"], 9),
            not bool(item.get("needs_you")),
            item.get("recent_output_at_unix_ms") is None,
            -(item.get("recent_output_at_unix_ms") or 0),
            natural_name_key(item["shpool_id"]),
        )
    )
    for row, item in enumerate(sessions, start=1):
        item["row"] = row
    outside_agents.sort(
        key=lambda item: (
            PROVIDER_ORDER.get(item["provider"], 9),
            item["title"].casefold(),
            item["identity"].get("uuid") or "",
        )
    )
    color_overrides = _valid_colors(config.get("colors"))
    color_overrides = _adopt_launch_colors(config, sessions, color_overrides)
    for item in sessions + outside_agents:
        item["display_color"] = session_color(
            item.get("provider"),
            (item.get("identity") or {}).get("uuid"),
            color_overrides,
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(current_time),
        "source": "live",
        "stale": False,
        "warnings": [],
        "daemon_generation": daemon_generation(process_table),
        "sessions": sessions,
        "outside_agents": outside_agents,
    }


def _shpool_executable() -> str:
    """Resolve shpool for contexts (systemd user services) whose PATH omits it."""
    found = shutil.which("shpool")
    if found:
        return found
    fallback = Path.home() / ".cargo" / "bin" / "shpool"
    return str(fallback) if fallback.is_file() else "shpool"


def collect_live(
    config: Mapping[str, Any],
    *,
    runner: Runner | None = None,
    proc_root: Path | None = None,
) -> dict[str, Any]:
    """Collect a live inventory using one call per external list command."""
    invoke = runner or default_runner
    timeout = float(config.get("command_timeout_seconds", 6.0))
    root = proc_root or Path(os.environ.get("SESSION_KIT_PROC_ROOT", "/proc"))
    try:
        shpool_payload = _command_json(
            fixture_env="SESSION_KIT_SHPOOL_JSON_FILE",
            command_env="SESSION_KIT_SHPOOL_CMD",
            default_command=(_shpool_executable(), "list", "--json"),
            runner=invoke,
            timeout=timeout,
        )
    except (OSError, ValueError, subprocess.SubprocessError, CollectionError) as exc:
        raise CollectionError(f"cannot collect shpool snapshot: {exc}") from exc
    _parse_shpool_payload(shpool_payload)
    process_table = platform_process_table(
        root, int(config.get("max_proc_nodes", DEFAULT_MAX_PROC_NODES))
    )
    warnings: list[str] = []
    claude_failed = False
    try:
        claude_payload = _command_json(
            fixture_env="SESSION_KIT_CLAUDE_JSON_FILE",
            command_env="SESSION_KIT_CLAUDE_CMD",
            default_command=("claude", "agents", "--json"),
            runner=invoke,
            timeout=timeout,
        )
        claude_payload = _enrich_claude_payload(claude_payload)
        _parse_claude_payload(claude_payload)
    except (OSError, ValueError, subprocess.SubprocessError, CollectionError) as exc:
        claude_payload = []
        claude_failed = True
        warnings.append(f"Claude inventory unavailable: {clean_text(str(exc), 240)}")
    codex_home, db_path = _codex_paths()
    codex_index = index_codex_processes(process_table, root, codex_home)
    codex_visible = bool(codex_index)
    session_index_names: dict[str, str] = {}
    naming_warnings: list[str] = []
    try:
        session_index_names = read_codex_session_index(
            codex_home / "session_index.jsonl", naming_warnings
        )
    except CollectionError as exc:
        naming_warnings.append(
            f"Codex session names unavailable: {clean_text(str(exc), 240)}"
        )
    try:
        codex_rows = read_codex_db(db_path, session_index_names)
    except (OSError, sqlite3.Error, CollectionError) as exc:
        codex_rows = (
            {
                uuid: {
                    "id": uuid,
                    "title": "",
                    "cwd": "",
                    "session_index_name": title,
                }
                for uuid, title in session_index_names.items()
            },
            {},
        )
        codex_failed = True
        if codex_visible:
            warnings.append(
                f"Codex metadata unavailable: {clean_text(str(exc), 240)}"
            )
    else:
        codex_failed = not db_path.is_file()
        if codex_failed and codex_visible:
            warnings.append(
                "Codex metadata unavailable: database does not exist: "
                f"{clean_text(str(db_path), 240)}"
            )
    recent_outputs = recent_output_times(
        item["name"] for item in _parse_shpool_payload(shpool_payload)
    )
    inventory = build_inventory(
        shpool_payload,
        claude_payload,
        process_table,
        codex_index,
        codex_rows,
        config,
        recent_output_by_shpool_id=recent_outputs,
    )
    apply_retained_setup_attributions(inventory)
    inventory["warnings"].extend(warnings)
    inventory["naming_warnings"] = naming_warnings
    # Optional providers may be absent on a general-purpose installation.
    # A query failure is material only when that provider is visibly running.
    claude_visible = any(
        clean_text(process.get("comm"), 128) == "claude"
        for process in process_table.values()
    )
    inventory["_complete"] = not (
        (claude_failed and claude_visible) or (codex_failed and codex_visible)
    )
    return inventory


def _state_paths(config: Mapping[str, Any]) -> dict[str, Path]:
    root = Path(config["state_dir"])
    return {
        "root": root,
        "inventory": root / "inventory.json",
        "manifest": root / "recovery-manifest.json",
        "pending": root / "recovery-pending.json",
        "aliases": root / "aliases.json",
        "aliases_archive": root / "aliases.json.migrated-v1",
        "aliases_source_backup": root / "aliases.json.pre-migration-v1",
        "terminal_numbers": root / "terminal-numbers.json",
        "terminal_numbers_epoch": root / "terminal-numbers.initialized.json",
        "config_lock": root / "config.lock",
        "lock": root / "inventory.lock",
    }


class StateLock:
    def __init__(self, root: Path, lock_path: Path):
        self.root = root
        self.lock_path = lock_path
        self.fd: int | None = None

    def __enter__(self) -> "StateLock":
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            root_stat = self.root.lstat()
        except OSError as exc:
            raise CollectionError(f"cannot inspect state directory {self.root}") from exc
        if (
            not statmod.S_ISDIR(root_stat.st_mode)
            or statmod.S_IMODE(root_stat.st_mode) != 0o700
            or root_stat.st_uid != os.geteuid()
        ):
            raise CollectionError(
                f"state directory must be a mode-0700 current-owner real directory: {self.root}"
            )
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
            opened = os.fstat(descriptor)
            before = self.lock_path.lstat()
            if (
                not statmod.S_ISREG(opened.st_mode)
                or statmod.S_IMODE(opened.st_mode) != 0o600
                or opened.st_uid != os.geteuid()
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise CollectionError(
                    f"state lock must be a mode-0600 current-owner regular file: {self.lock_path}"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            after = self.lock_path.lstat()
            if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
                raise CollectionError(f"state lock changed while locking: {self.lock_path}")
        except BaseException:
            if "descriptor" in locals():
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            raise
        self.fd = descriptor
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON with mode 0600, fsync, and same-directory atomic replace."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
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


def _read_state_json(path: Path) -> Any:
    try:
        value = _load_json_file(path)
    except (OSError, ValueError):
        return None
    return value


def _alias_document_from_bytes(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectionError(f"{label} is invalid JSON") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION
        or ("aliases" in raw and not isinstance(raw["aliases"], Mapping))
        or (
            "automatic_titles" in raw
            and not isinstance(raw["automatic_titles"], Mapping)
        )
        or (
            "automatic_title_failures" in raw
            and not isinstance(raw["automatic_title_failures"], Mapping)
        )
    ):
        raise CollectionError(f"{label} has an invalid schema")
    aliases = raw.get("aliases", {})
    normalized = _valid_aliases(aliases)
    if len(normalized) != len(aliases):
        raise CollectionError(f"{label} contains an invalid alias")
    raw["schema_version"] = SCHEMA_VERSION
    raw["aliases"] = dict(sorted(normalized.items()))
    if "automatic_titles" in raw:
        titles = _valid_automatic_titles(raw["automatic_titles"])
        if len(titles) != len(raw["automatic_titles"]):
            raise CollectionError(f"{label} contains an invalid automatic title")
        raw["automatic_titles"] = dict(sorted(titles.items()))
    if "automatic_title_failures" in raw:
        failures = _valid_automatic_title_failures(raw["automatic_title_failures"])
        if len(failures) != len(raw["automatic_title_failures"]):
            raise CollectionError(
                f"{label} contains an invalid automatic title failure"
            )
        raw["automatic_title_failures"] = dict(sorted(failures.items()))
    return raw


def _private_alias_document(path: Path, *, allow_missing: bool) -> dict[str, Any]:
    payload = _read_bounded_owner_file(
        path,
        label="canonical alias config",
        max_bytes=MAX_PRIVATE_JSON_BYTES,
        exact_mode=0o600,
        allow_missing=allow_missing,
    )
    if payload is None:
        return {"schema_version": SCHEMA_VERSION, "aliases": {}}
    return _alias_document_from_bytes(payload, label="canonical alias config")


def _atomic_json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _private_alias_parent(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.parent.lstat()
    if (
        not statmod.S_ISDIR(metadata.st_mode)
        or statmod.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or statmod.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise CollectionError(
            f"alias config directory must be mode-0700 current-owner: {path.parent}"
        )


def canonical_aliases(config: Mapping[str, Any]) -> dict[str, str]:
    return dict(_private_alias_document(config_path(), allow_missing=True)["aliases"])


def mutate_canonical_alias(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    title: str | None,
) -> dict[str, str]:
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise CollectionError("alias requires provider claude|codex and an exact UUID")
    path = config_path()
    paths = _state_paths(config)
    # config.lock is the alias-write lock used by pinned schema-v1 releases.
    # Keep it outermost so an old picker and this core cannot lose each
    # other's config update during a mixed-release rollback window.
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            _private_alias_parent(path)
            document = _private_alias_document(path, allow_missing=True)
            aliases = dict(document["aliases"])
            key = f"{provider}:{exact_uuid}"
            if title is None:
                aliases.pop(key, None)
            else:
                clean_title = clean_text(title, 100)
                if not clean_title:
                    raise CollectionError("alias title must contain visible text")
                aliases[key] = clean_title
            document["aliases"] = dict(sorted(aliases.items()))
            atomic_write_json(path, document)
            return dict(document["aliases"])


def canonical_automatic_titles(config: Mapping[str, Any]) -> dict[str, str]:
    document = _private_alias_document(config_path(), allow_missing=True)
    return _valid_automatic_titles(document.get("automatic_titles"))


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
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise CollectionError(
            "automatic title requires provider claude|codex and an exact UUID"
        )
    if title is not None and not automatic_naming_enabled():
        raise CollectionError("automatic naming is disabled")
    clean_title = normalize_automatic_title(title) if title is not None else None
    path = config_path()
    paths = _state_paths(config)
    key = f"{provider}:{exact_uuid}"
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            if revalidate is not None:
                revalidate()
            _private_alias_parent(path)
            document = _private_alias_document(path, allow_missing=True)
            aliases = dict(document["aliases"])
            if clean_title is not None and key in aliases:
                raise CollectionError("explicit local alias already owns this title")
            titles = _valid_automatic_titles(document.get("automatic_titles"))
            failures = _valid_automatic_title_failures(
                document.get("automatic_title_failures")
            )
            existing = titles.get(key)
            if clean_title is None:
                titles.pop(key, None)
            elif existing and existing != clean_title and not overwrite:
                raise CollectionError("automatic title is already set")
            else:
                titles[key] = clean_title
            failures.pop(key, None)
            document["automatic_titles"] = dict(sorted(titles.items()))
            if failures:
                document["automatic_title_failures"] = dict(
                    sorted(failures.items())
                )
            else:
                document.pop("automatic_title_failures", None)
            atomic_write_json(path, document)
            verified = _private_alias_document(path, allow_missing=False)
            verified_titles = _valid_automatic_titles(
                verified.get("automatic_titles")
            )
            if verified_titles.get(key) != clean_title:
                if not (clean_title is None and key not in verified_titles):
                    raise CollectionError("automatic title atomic verification failed")
            if clean_title is not None and key in verified["aliases"]:
                raise CollectionError(
                    "explicit alias appeared during automatic title write"
                )
            return {
                "schema_version": SCHEMA_VERSION,
                "provider": provider,
                "uuid": exact_uuid,
                "title": clean_title,
                "automatic_titles": verified_titles,
            }


def record_automatic_title_failure(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    *,
    revalidate: Callable[[], None] | None = None,
) -> int:
    """Record at most two proved root-turn naming failures."""
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise CollectionError("automatic title failure requires an exact identity")
    path = config_path()
    paths = _state_paths(config)
    key = f"{provider}:{exact_uuid}"
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            if revalidate is not None:
                revalidate()
            _private_alias_parent(path)
            document = _private_alias_document(path, allow_missing=True)
            if key in document["aliases"]:
                return 0
            titles = _valid_automatic_titles(document.get("automatic_titles"))
            if key in titles:
                return 0
            failures = _valid_automatic_title_failures(
                document.get("automatic_title_failures")
            )
            failures[key] = min(2, failures.get(key, 0) + 1)
            document["automatic_title_failures"] = dict(sorted(failures.items()))
            atomic_write_json(path, document)
            verified = _private_alias_document(path, allow_missing=False)
            attempts = _valid_automatic_title_failures(
                verified.get("automatic_title_failures")
            ).get(key)
            if attempts != failures[key]:
                raise CollectionError(
                    "automatic title failure atomic verification failed"
                )
            return attempts


def _exact_active_title_keys(inventory: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for item in [*inventory.get("sessions", ()), *inventory.get("outside_agents", ())]:
        if not isinstance(item, Mapping):
            continue
        provider = item.get("provider")
        identity = item.get("identity")
        uuid = identity.get("uuid") if isinstance(identity, Mapping) else None
        if (
            provider in PROVIDERS
            and isinstance(identity, Mapping)
            and identity.get("confidence") == "exact"
            and valid_uuid(uuid)
        ):
            keys.add(f"{provider}:{valid_uuid(uuid)}")
    return keys


def _automatic_title_prune_plan(
    titles: Mapping[str, str], active_keys: set[str]
) -> tuple[list[str], str]:
    orphans = sorted(set(titles) - active_keys)
    evidence = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "automatic_titles": dict(sorted(titles.items())),
            "active_keys": sorted(active_keys),
            "orphans": orphans,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return orphans, hashlib.sha256(evidence).hexdigest()


def audit_automatic_titles(
    config: Mapping[str, Any], inventory: Mapping[str, Any]
) -> dict[str, Any]:
    titles = canonical_automatic_titles(config)
    orphans, token = _automatic_title_prune_plan(
        titles, _exact_active_title_keys(inventory)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "automatic_title_count": len(titles),
        "orphan_count": len(orphans),
        "orphans": orphans,
        "prune_token": token,
        "dry_run": True,
    }


def prune_automatic_titles(
    config: Mapping[str, Any],
    inventory: Mapping[str, Any],
    prune_token: str,
) -> dict[str, Any]:
    """Apply only the exact orphan set previously exposed by a dry-run token."""
    path = config_path()
    paths = _state_paths(config)
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            _private_alias_parent(path)
            document = _private_alias_document(path, allow_missing=True)
            titles = _valid_automatic_titles(document.get("automatic_titles"))
            active_keys = _exact_active_title_keys(inventory)
            orphans, expected = _automatic_title_prune_plan(titles, active_keys)
            if not re.fullmatch(r"[0-9a-f]{64}", prune_token) or prune_token != expected:
                raise CollectionError(
                    "automatic title prune token is stale or does not match dry run"
                )
            for key in orphans:
                titles.pop(key, None)
            document["automatic_titles"] = dict(sorted(titles.items()))
            failures = _valid_automatic_title_failures(
                document.get("automatic_title_failures")
            )
            for key in orphans:
                failures.pop(key, None)
            if failures:
                document["automatic_title_failures"] = dict(
                    sorted(failures.items())
                )
            else:
                document.pop("automatic_title_failures", None)
            atomic_write_json(path, document)
            return {
                "schema_version": SCHEMA_VERSION,
                "pruned": orphans,
                "automatic_title_count": len(titles),
            }


MAX_CLAUDE_SESSION_RECORDS = 512


def _push_claude_title(home: Path, uuid: str, title: str) -> tuple[list[str], list[str]]:
    pushed: list[str] = []
    warnings: list[str] = []
    sessions = home / ".claude" / "sessions"
    if not sessions.is_dir():
        return pushed, ["Claude sessions directory unavailable; title not pushed"]
    intent = sessions / f"{uuid}.nameintent"
    try:
        if intent.is_symlink():
            raise OSError("refusing symlinked name intent")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{intent.name}.", dir=sessions
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(title + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, intent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
        pushed.append("claude-nameintent")
    except OSError as exc:
        warnings.append(f"Claude name intent not written: {exc}")
    # The prompt bar's bottom-right name is a transcript agent-name record —
    # the exact store /rename persists, hydrated at session start/resume,
    # rendered beside the agent-color. Same append discipline as colors.
    projects = home / ".claude" / "projects"
    try:
        transcripts = sorted(projects.glob(f"*/{uuid}.jsonl"))
    except OSError:
        transcripts = []
    name_entry = json.dumps(
        {"type": "agent-name", "agentName": title, "sessionId": uuid},
        separators=(",", ":"),
    )
    for transcript in transcripts[:4]:
        if transcript.is_symlink():
            continue
        try:
            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write(name_entry + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            pushed.append("claude-transcript-name")
        except OSError as exc:
            warnings.append(f"Claude transcript name not appended: {exc}")
    # The per-PID session record names the thread in Claude's own session
    # picker. Records carry the exact sessionId, so match by content.
    try:
        records = sorted(sessions.glob("*.json"))[:MAX_CLAUDE_SESSION_RECORDS]
    except OSError as exc:
        return pushed, warnings + [f"Claude session records unreadable: {exc}"]
    for record in records:
        if record.is_symlink() or not re.fullmatch(r"\d+\.json", record.name):
            continue
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("sessionId") != uuid:
            continue
        try:
            data["name"] = title
            atomic_write_json(record, data)
            pushed.append("claude-session-record")
        except OSError as exc:
            warnings.append(f"Claude session record not updated: {exc}")
    return pushed, warnings


def _push_codex_thread_title(
    codex_root: Path, uuid: str, title: str
) -> tuple[list[str], list[str]]:
    """Set threads.title in Codex's own state database.

    The Codex TUI's thread-title status item and its rename flow read and
    write this column (the session index alone never reaches the status
    bar). Update-only — a missing row is reported, never created — and every
    failure is fail-open.
    """
    import sqlite3

    candidates = sorted(
        codex_root.glob("state_*.sqlite"),
        key=lambda p: p.name,
    )
    if not candidates:
        # Older Codex builds have no thread store; nothing to report.
        return [], []
    database = candidates[-1]
    try:
        connection = sqlite3.connect(database, timeout=1.0)
        try:
            cursor = connection.execute(
                "UPDATE threads SET title = ? WHERE id = ?", (title, uuid)
            )
            connection.commit()
            if cursor.rowcount > 0:
                return ["codex-thread-title"], []
            return [], ["Codex thread row not found; thread title not set"]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return [], [f"Codex thread title not set: {exc}"]


def _push_codex_title(home: Path, uuid: str, title: str) -> tuple[list[str], list[str]]:
    codex_root = home / ".codex"
    if not codex_root.is_dir():
        return [], ["Codex home unavailable; title not pushed"]
    index = codex_root / "session_index.jsonl"
    if index.is_symlink():
        return [], ["Codex session index is a symlink; title not pushed"]
    entry = json.dumps(
        {
            "id": uuid,
            "thread_name": title,
            "updated_at": dt.datetime.now(dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        },
        separators=(",", ":"),
    )
    try:
        if index.exists() and index.stat().st_size > MAX_CODEX_SESSION_INDEX_BYTES:
            return [], ["Codex session index exceeds the bounded size; title not pushed"]
        descriptor = os.open(
            index, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
        )
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(entry + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        return [], [f"Codex session index not appended: {exc}"]
    thread_pushes, thread_warnings = _push_codex_thread_title(
        codex_root, uuid, title
    )
    return ["codex-session-index", *thread_pushes], thread_warnings


SESSION_COLORS = (
    "red",
    "blue",
    "green",
    "yellow",
    "purple",
    "orange",
    "pink",
    "cyan",
)


def _valid_colors(raw: Any) -> dict[str, str]:
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
            and value in SESSION_COLORS
        ):
            colors[f"{provider}:{uuid.lower()}"] = value
    return colors


def canonical_colors(config: Mapping[str, Any]) -> dict[str, str]:
    document = _private_alias_document(config_path(), allow_missing=True)
    return _valid_colors(document.get("colors"))


def session_color(
    provider: str,
    uuid: str | None,
    overrides: Mapping[str, str] | None = None,
) -> str | None:
    """Stable per-conversation color: explicit override, else identity hash."""
    if provider not in PROVIDERS or not uuid:
        return None
    exact_uuid = valid_uuid(uuid)
    if not exact_uuid:
        return None
    override = (overrides or {}).get(f"{provider}:{exact_uuid}")
    if override in SESSION_COLORS:
        return override
    digest = hashlib.sha256(f"{provider}:{exact_uuid}".encode("utf-8")).digest()
    return SESSION_COLORS[digest[0] % len(SESSION_COLORS)]


# A brand-new Codex session has no conversation ID until Codex boots, so its
# launch theme cannot come from the identity hash. The launch color is picked
# deterministically from the shpool session name, recorded as a marker, and
# adopted as that conversation's explicit override the first time the live
# collector sees the session's real ID — from then on the window theme, the
# picker row, and every future resume agree.
LAUNCH_COLOR_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def launch_color_for(shpool_id: str) -> str:
    digest = hashlib.sha256(f"launch:{shpool_id}".encode("utf-8")).digest()
    return SESSION_COLORS[digest[0] % len(SESSION_COLORS)]


def _launch_color_dir(config: Mapping[str, Any]) -> Path | None:
    state_dir = config.get("state_dir")
    if not state_dir:
        return None
    return Path(state_dir) / "launch-color"


def record_launch_color(
    config: Mapping[str, Any], shpool_id: str
) -> str | None:
    """Pick and persist a launch color for a session with no conversation ID."""
    if (
        not shpool_id
        or "/" in shpool_id
        or shpool_id.startswith(".")
        or len(shpool_id) > 128
    ):
        return None
    color = launch_color_for(shpool_id)
    directory = _launch_color_dir(config)
    if directory is None:
        return color
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(directory / shpool_id, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(color + "\n")
    except OSError:
        # The theme still applies this boot; only later adoption is lost.
        pass
    return color


def _adopt_launch_colors(
    config: Mapping[str, Any],
    sessions: Sequence[Mapping[str, Any]],
    overrides: Mapping[str, str],
) -> dict[str, str]:
    """Turn launch-color markers into explicit overrides once IDs are known."""
    current = dict(overrides)
    directory = _launch_color_dir(config)
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
            if now - stat_result.st_mtime > LAUNCH_COLOR_MAX_AGE_SECONDS:
                marker.unlink(missing_ok=True)
                continue
            item = by_shpool_id.get(marker.name)
            if item is None:
                continue
            uuid = valid_uuid(str((item.get("identity") or {}).get("uuid") or ""))
            if not uuid:
                continue
            color = marker.read_text(encoding="utf-8", errors="strict").strip()
            if color not in SESSION_COLORS:
                marker.unlink(missing_ok=True)
                continue
            key = f"codex:{uuid}"
            if key not in current:
                current = mutate_canonical_color(config, "codex", uuid, color)
            marker.unlink(missing_ok=True)
        except (OSError, ValueError, CollectionError):
            continue
    return current


def mutate_canonical_color(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    color: str | None,
) -> dict[str, str]:
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid:
        raise CollectionError("color requires provider claude|codex and an exact UUID")
    if color is not None and color not in SESSION_COLORS:
        raise CollectionError(
            "color must be one of: " + ", ".join(SESSION_COLORS)
        )
    path = config_path()
    paths = _state_paths(config)
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            _private_alias_parent(path)
            document = _private_alias_document(path, allow_missing=True)
            colors = _valid_colors(document.get("colors"))
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


def _push_claude_color(
    home: Path, uuid: str, color: str
) -> tuple[list[str], list[str]]:
    """Append the exact agent-color record /color itself writes.

    Claude Code reads it at session start/resume; nothing is ever typed into
    a live terminal. Missing transcripts fail open.
    """
    projects = home / ".claude" / "projects"
    if not projects.is_dir():
        return [], ["Claude projects directory unavailable; color not pushed"]
    try:
        transcripts = sorted(projects.glob(f"*/{uuid}.jsonl"))
    except OSError as exc:
        return [], [f"Claude transcripts unreadable: {exc}"]
    if not transcripts:
        return [], ["no Claude transcript for this conversation; color not pushed"]
    entry = json.dumps(
        {"type": "agent-color", "agentColor": color, "sessionId": uuid},
        separators=(",", ":"),
    )
    pushed: list[str] = []
    warnings: list[str] = []
    for transcript in transcripts[:4]:
        if transcript.is_symlink():
            warnings.append(f"refusing symlinked transcript: {transcript.name}")
            continue
        try:
            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write(entry + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            pushed.append("claude-transcript-color")
        except OSError as exc:
            warnings.append(f"Claude transcript not appended: {exc}")
    return pushed, warnings


def propagate_provider_color(
    provider: str,
    uuid: str,
    color: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One-shot push of a session color into provider-native storage.

    Codex has no native color surface; its rows carry the kit-side color only.
    """
    exact_uuid = valid_uuid(uuid)
    if provider not in PROVIDERS or not exact_uuid or color not in SESSION_COLORS:
        return {
            "provider_color_pushes": [],
            "provider_color_warnings": ["invalid provider color push request"],
        }
    if provider != "claude":
        return {"provider_color_pushes": [], "provider_color_warnings": []}
    env = environ if environ is not None else os.environ
    home = Path(env.get("HOME") or os.fspath(Path.home()))
    pushed, warnings = _push_claude_color(home, exact_uuid, color)
    for warning in warnings:
        print(f"session inventory: {warning}", file=sys.stderr)
    return {
        "provider_color_pushes": pushed,
        "provider_color_warnings": warnings,
    }


def propagate_provider_title(
    provider: str,
    uuid: str,
    title: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One-shot push of an assigned name into the provider's own surfaces.

    Assignment semantics are last-writer-wins: the push happens once at
    assignment time and later provider-side renames stand until the next
    assignment. Every failure is fail-open — naming never breaks because a
    provider surface is unavailable — but each skipped surface is reported.
    """
    exact_uuid = valid_uuid(uuid)
    clean_title = clean_text(title, 100)
    if (
        provider not in PROVIDERS
        or not exact_uuid
        or not clean_title
        # A placeholder identity must never be written into provider stores.
        or set(exact_uuid.replace("-", "")) <= {"0"}
    ):
        return {
            "provider_title_pushes": [],
            "provider_title_warnings": ["invalid provider title push request"],
        }
    env = environ if environ is not None else os.environ
    home_raw = env.get("HOME") or os.fspath(Path.home())
    home = Path(home_raw)
    if provider == "claude":
        pushed, warnings = _push_claude_title(home, exact_uuid, clean_title)
    else:
        pushed, warnings = _push_codex_title(home, exact_uuid, clean_title)
    for warning in warnings:
        print(f"session inventory: {warning}", file=sys.stderr)
    return {
        "provider_title_pushes": pushed,
        "provider_title_warnings": warnings,
    }


# A conversation launched through the first-window color pre-bake opens as a
# resume, and Claude Code never auto-titles a resumed conversation (its
# once-only guard initializes from the loaded message count, which the
# resume-synthesized continuation pair makes non-zero). These sessions carry a
# unique signature: a synthetic isMeta user record holding the resume
# continuation text, written at load before the human's first prompt. For
# exactly those sessions the kit derives a title from the first real prompt
# and writes it through the same native records /rename persists. Sessions
# outside that signature are never touched, so Claude's own (better) auto
# titles are never masked.
RESUME_CONTINUATION_TEXT = "Continue from where you left off."
MAX_AUTO_TITLE_TRANSCRIPT_BYTES = 8 * 1024 * 1024
_TITLE_TRAILING_STOPWORDS = frozenset(
    "a an and any are at be but by each every for from how if in is it of on "
    "or so that the then there this to was were what when which who why will "
    "with".split()
)


def derive_prompt_title(prompt: Any) -> str | None:
    """Derive a short display title from a first user prompt.

    Pure heuristic — no model call: first sentence, at most seven words,
    trailing function words trimmed, sentence-cased, capped at 64 characters.
    """
    if not isinstance(prompt, str):
        return None
    text = re.sub(r"\s+", " ", prompt).strip()
    if not text or text.startswith("/"):
        return None
    text = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    words = text.split(" ")[:7]
    trimmed = list(words)
    while trimmed and trimmed[-1].lower().strip("?,.:;!\"'") in _TITLE_TRAILING_STOPWORDS:
        trimmed.pop()
    if not trimmed:
        trimmed = words
    title = " ".join(trimmed).rstrip(" ,;:.!?\"'")
    if len(title) < 4:
        return None
    title = title[:64].rstrip()
    return title[0].upper() + title[1:]


def _first_text_block(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
    return ""


def auto_title_from_hook(
    raw: Any, *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Title a pre-baked Claude conversation from its first real prompt.

    Input is the UserPromptSubmit hook payload (JSON text). Eligibility is
    strict and every miss fails open with title=null: the transcript must
    carry the resume-continuation signature, no real conversation beyond the
    triggering prompt, and no title of any kind (ai, /rename, agent-name,
    kit name intent) may already exist.
    """
    out: dict[str, Any] = {"title": None, "pushes": [], "warnings": []}

    def skip(reason: str) -> dict[str, Any]:
        out["reason"] = reason
        return out

    try:
        payload = json.loads(raw if isinstance(raw, str) else "")
    except ValueError:
        return skip("hook payload is not valid JSON")
    if not isinstance(payload, Mapping):
        return skip("hook payload is not an object")
    # UserPromptSubmit fires before the first turn is flushed to disk (the
    # pre-bake signature is not yet visible then); Stop fires right after the
    # first answer with everything durable. Supporting both means the title
    # lands at the end of the first exchange.
    if payload.get("hook_event_name") not in ("UserPromptSubmit", "Stop"):
        return skip("not a UserPromptSubmit or Stop event")
    uuid = valid_uuid(str(payload.get("session_id") or ""))
    if not uuid:
        return skip("missing or invalid session_id")

    env = environ if environ is not None else os.environ
    home = Path(env.get("HOME") or os.fspath(Path.home()))
    transcript_raw = payload.get("transcript_path")
    if not isinstance(transcript_raw, str) or not transcript_raw:
        return skip("missing transcript_path")
    transcript = Path(transcript_raw)
    projects = (home / ".claude" / "projects").resolve()
    try:
        resolved = transcript.resolve(strict=True)
    except OSError:
        return skip("transcript unavailable")
    if (
        transcript.is_symlink()
        or resolved.name != f"{uuid}.jsonl"
        or resolved.parent.parent != projects
    ):
        return skip("transcript path is not this session's own transcript")
    if (home / ".claude" / "sessions" / f"{uuid}.nameintent").exists():
        return skip("a kit name intent already exists")
    try:
        if resolved.stat().st_size > MAX_AUTO_TITLE_TRANSCRIPT_BYTES:
            return skip("transcript exceeds the bounded size")
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return skip("transcript unreadable")

    signature = False
    real_users = 0
    first_prompt: str | None = None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, Mapping):
            continue
        kind = record.get("type")
        if kind in ("ai-title", "agent-name", "custom-title"):
            return skip("the session already has a title")
        if kind != "user":
            continue
        if record.get("isSidechain"):
            continue
        content = (
            record.get("message", {}).get("content")
            if isinstance(record.get("message"), Mapping)
            else None
        )
        text = _first_text_block(content)
        if record.get("isMeta"):
            if text.startswith(RESUME_CONTINUATION_TEXT):
                signature = True
            continue
        # Tool results are stored as user-typed records too; only records
        # carrying prompt text count as human prompts.
        if isinstance(content, str) or text:
            real_users += 1
            if first_prompt is None:
                first_prompt = content if isinstance(content, str) else text
    if not signature:
        return skip("no pre-bake resume signature")
    if real_users > 1:
        return skip("conversation already has prior prompts")
    # The conversation's own first prompt names the topic; the live payload
    # prompt is the fallback for the pre-flush window.
    title = derive_prompt_title(first_prompt) or derive_prompt_title(
        payload.get("prompt")
    )
    if not title:
        return skip("prompt does not yield a title")

    entry = json.dumps(
        {"type": "ai-title", "aiTitle": title, "sessionId": uuid},
        separators=(",", ":"),
    )
    try:
        with open(resolved, "a", encoding="utf-8") as handle:
            handle.write(entry + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        out["pushes"].append("claude-transcript-ai-title")
    except OSError as exc:
        return skip(f"transcript not appended: {exc}")
    pushed = propagate_provider_title("claude", uuid, title, environ=env)
    out["title"] = title
    out["pushes"].extend(pushed.get("provider_title_pushes", []))
    out["warnings"].extend(pushed.get("provider_title_warnings", []))
    return out


def _strict_legacy_aliases(path: Path) -> tuple[bytes, dict[str, str]] | None:
    payload = _read_bounded_owner_file(
        path,
        label="legacy runtime alias file",
        max_bytes=MAX_PRIVATE_JSON_BYTES,
        exact_mode=0o600,
        allow_missing=True,
    )
    if payload is None:
        return None
    try:
        raw = json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectionError("legacy runtime alias file is invalid JSON") from exc
    aliases = raw.get("aliases") if isinstance(raw, Mapping) else None
    normalized = _valid_aliases(aliases)
    if (
        not isinstance(raw, Mapping)
        or raw.get("schema_version") != SCHEMA_VERSION
        or not isinstance(aliases, Mapping)
        or len(normalized) != len(aliases)
    ):
        raise CollectionError("legacy runtime alias file has an invalid schema")
    return payload, normalized


def _create_private_backup(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _read_bounded_owner_file(
            path,
            label="alias migration backup",
            max_bytes=MAX_PRIVATE_JSON_BYTES,
            exact_mode=0o600,
        )
        if existing != payload:
            raise CollectionError("alias migration backup already exists with other bytes")
        return
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise CollectionError("could not write alias migration backup")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _alias_migration_images(
    backup_bytes: bytes, runtime_aliases: Mapping[str, str]
) -> tuple[bytes | None, dict[str, Any], bytes]:
    if backup_bytes == ABSENT_ALIAS_CONFIG_BACKUP:
        preimage = {
            "schema_version": SCHEMA_VERSION,
            "aliases": {},
        }
        preimage_bytes = None
    else:
        preimage = _alias_document_from_bytes(
            backup_bytes, label="alias migration backup"
        )
        preimage_bytes = backup_bytes
    merged = {**preimage["aliases"], **runtime_aliases}
    postimage = dict(preimage)
    postimage["aliases"] = dict(sorted(merged.items()))
    return preimage_bytes, postimage, _atomic_json_bytes(postimage)


def migrate_runtime_aliases(config: Mapping[str, Any]) -> dict[str, Any]:
    """Explicitly preserve the old effective runtime-wins values in config."""
    paths = _state_paths(config)
    config_file = config_path()
    backup = config_file.with_name(f"{config_file.name}.pre-runtime-alias-migration-v1")
    with StateLock(paths["root"], paths["config_lock"]):
        with StateLock(paths["root"], paths["lock"]):
            legacy = _strict_legacy_aliases(paths["aliases"])
            archived = _strict_legacy_aliases(paths["aliases_archive"])
            if legacy is None:
                if archived is not None:
                    source_backup = _read_bounded_owner_file(
                        paths["aliases_source_backup"],
                        label="legacy runtime alias source backup",
                        max_bytes=MAX_PRIVATE_JSON_BYTES,
                        exact_mode=0o600,
                    )
                    if source_backup != archived[0]:
                        raise CollectionError(
                            "archived runtime aliases diverge from migration evidence"
                        )
                    backup_bytes = _read_bounded_owner_file(
                        backup,
                        label="alias migration backup",
                        max_bytes=MAX_PRIVATE_JSON_BYTES,
                        exact_mode=0o600,
                    )
                    before = _read_bounded_owner_file(
                        config_file,
                        label="canonical alias config",
                        max_bytes=MAX_PRIVATE_JSON_BYTES,
                        exact_mode=0o600,
                        allow_missing=True,
                    )
                    (
                        preimage_bytes,
                        postimage,
                        postimage_bytes,
                    ) = _alias_migration_images(
                        backup_bytes, archived[1]
                    )
                    repaired = False
                    if before == postimage_bytes:
                        pass
                    elif before == preimage_bytes:
                        _private_alias_parent(config_file)
                        atomic_write_json(config_file, postimage)
                        repaired = True
                    else:
                        raise CollectionError(
                            "canonical alias config diverged from the migration postimage"
                        )
                    directory = os.open(
                        paths["root"], os.O_RDONLY | os.O_DIRECTORY
                    )
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                return {
                    "schema_version": SCHEMA_VERSION,
                    "migrated": bool(archived is not None and repaired),
                    "already_migrated": bool(
                        archived is not None and not repaired
                    ),
                    "aliases": canonical_aliases(config),
                }
            rollback_retry = archived is not None
            if rollback_retry and archived[0] != legacy[0]:
                raise CollectionError(
                    "active and archived runtime aliases differ"
                )
            _private_alias_parent(config_file)
            before = _read_bounded_owner_file(
                config_file,
                label="canonical alias config",
                max_bytes=MAX_PRIVATE_JSON_BYTES,
                exact_mode=0o600,
                allow_missing=True,
            )
            backup_bytes = _read_bounded_owner_file(
                backup,
                label="alias migration backup",
                max_bytes=MAX_PRIVATE_JSON_BYTES,
                exact_mode=0o600,
                allow_missing=True,
            )
            if backup_bytes is None:
                backup_bytes = (
                    before
                    if before is not None
                    else ABSENT_ALIAS_CONFIG_BACKUP
                )
                _create_private_backup(backup, backup_bytes)
            _create_private_backup(
                paths["aliases_source_backup"], legacy[0]
            )
            (
                preimage_bytes,
                postimage,
                postimage_bytes,
            ) = _alias_migration_images(backup_bytes, legacy[1])
            if before == postimage_bytes:
                pass
            elif before == preimage_bytes:
                atomic_write_json(config_file, postimage)
            else:
                raise CollectionError(
                    "canonical alias config diverged during migration"
                )
            if rollback_retry:
                os.unlink(paths["aliases"])
            else:
                os.replace(paths["aliases"], paths["aliases_archive"])
            directory = os.open(paths["root"], os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            durable_archive = _strict_legacy_aliases(paths["aliases_archive"])
            source_backup = _read_bounded_owner_file(
                paths["aliases_source_backup"],
                label="legacy runtime alias source backup",
                max_bytes=MAX_PRIVATE_JSON_BYTES,
                exact_mode=0o600,
            )
            if (
                durable_archive is None
                or durable_archive[0] != legacy[0]
                or source_backup != legacy[0]
                or paths["aliases"].exists()
            ):
                raise CollectionError(
                    "runtime alias archive did not reach its durable postcondition"
                )
            return {
                "schema_version": SCHEMA_VERSION,
                "migrated": True,
                "already_migrated": False,
                "aliases": dict(postimage["aliases"]),
            }


def _boot_id() -> str | None:
    override = os.environ.get("SESSION_KIT_BOOT_ID_FILE")
    if override:
        try:
            value = Path(override).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None
    if _runtime_platform() == DARWIN_PLATFORM:
        if not _darwin_preview_enabled():
            return None
        try:
            libc, _ = _darwin_libraries()
            value = _DarwinTimeval()
            size = ctypes.c_size_t(ctypes.sizeof(value))
            if libc.sysctlbyname(
                b"kern.boottime",
                ctypes.byref(value),
                ctypes.byref(size),
                None,
                0,
            ) != 0:
                return None
            if value.tv_sec <= 0 or value.tv_usec < 0:
                return None
            return f"darwin:{value.tv_sec}:{value.tv_usec}"
        except (OSError, ValueError):
            return None
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return None
    return value or None


def _empty_terminal_registry(boot_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "boot_id": boot_id,
        "next_number": 1,
        "bindings": {},
    }


def _validate_terminal_registry(raw: Any, boot_id: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "schema_version",
        "boot_id",
        "next_number",
        "bindings",
    }:
        raise CollectionError("terminal number registry has an invalid schema")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise CollectionError("terminal number registry has an unsupported schema")
    stored_boot = raw.get("boot_id")
    next_number = raw.get("next_number")
    bindings = raw.get("bindings")
    if (
        not isinstance(stored_boot, str)
        or not stored_boot
        or isinstance(next_number, bool)
        or not isinstance(next_number, int)
        or next_number <= 0
        or not isinstance(bindings, Mapping)
    ):
        raise CollectionError("terminal number registry has invalid fields")
    checked: dict[str, int] = {}
    ai_by_number: dict[int, str] = {}
    generation_by_number: dict[int, str] = {}
    for key, value in bindings.items():
        if (
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
        ):
            raise CollectionError("terminal number registry has an invalid binding")
        if key.startswith("ai:"):
            _, separator, remainder = key.partition(":")
            provider, separator, uuid = remainder.partition(":")
            if (
                not separator
                or provider not in PROVIDERS
                or valid_uuid(uuid) != uuid
                or (value in ai_by_number and ai_by_number[value] != key)
            ):
                raise CollectionError(
                    "terminal number registry has a conflicting AI binding"
                )
            ai_by_number[value] = key
        elif re.fullmatch(r"generation:[0-9a-f]{64}", key):
            if (
                value in generation_by_number
                and generation_by_number[value] != key
            ):
                raise CollectionError(
                    "terminal number registry has duplicate generation bindings"
                )
            generation_by_number[value] = key
        else:
            raise CollectionError("terminal number registry has an invalid key")
        checked[key] = value
    if checked and next_number <= max(checked.values()):
        raise CollectionError("terminal number registry would reuse a number")
    if stored_boot != boot_id:
        return _empty_terminal_registry(boot_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "boot_id": stored_boot,
        "next_number": next_number,
        "bindings": checked,
    }


def _read_terminal_registry(
    path: Path, boot_id: str, epoch_path: Path | None = None
) -> dict[str, Any]:
    payload = _read_bounded_owner_file(
        path,
        label="terminal number registry",
        max_bytes=MAX_PRIVATE_JSON_BYTES,
        exact_mode=0o600,
        allow_missing=True,
    )
    if payload is None:
        if epoch_path is not None:
            epoch_payload = _read_bounded_owner_file(
                epoch_path,
                label="terminal number initialization receipt",
                max_bytes=4096,
                exact_mode=0o600,
                allow_missing=True,
            )
            if epoch_payload is not None:
                try:
                    epoch = json.loads(epoch_payload.decode("utf-8", "strict"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise CollectionError(
                        "terminal number initialization receipt is invalid"
                    ) from exc
                if (
                    not isinstance(epoch, Mapping)
                    or set(epoch) != {"schema_version", "boot_id"}
                    or epoch.get("schema_version") != SCHEMA_VERSION
                    or not isinstance(epoch.get("boot_id"), str)
                    or not epoch["boot_id"]
                ):
                    raise CollectionError(
                        "terminal number initialization receipt has an invalid schema"
                    )
                if epoch["boot_id"] == boot_id:
                    raise CollectionError(
                        "same-boot terminal number registry disappeared"
                    )
        return _empty_terminal_registry(boot_id)
    try:
        raw = json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectionError("terminal number registry is invalid JSON") from exc
    return _validate_terminal_registry(raw, boot_id)


def _terminal_generation_key(
    inventory: Mapping[str, Any], item: Mapping[str, Any], boot_id: str
) -> str | None:
    shell = item.get("shpool_shell")
    values = (
        item.get("started_at_unix_ms"),
        shell.get("pid") if isinstance(shell, Mapping) else None,
        shell.get("process_start_ticks") if isinstance(shell, Mapping) else None,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in values
    ):
        return None
    raw_id = item.get("shpool_id_raw")
    if not isinstance(raw_id, str) or not raw_id:
        return None
    payload = json.dumps(
        [boot_id, raw_id, values[0], values[1], values[2]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"generation:{hashlib.sha256(payload).hexdigest()}"


def _terminal_ai_key(item: Mapping[str, Any]) -> str | None:
    identity = item.get("identity")
    provider = item.get("provider")
    if (
        provider in PROVIDERS
        and isinstance(identity, Mapping)
        and identity.get("confidence") == "exact"
    ):
        uuid = valid_uuid(identity.get("uuid"))
        if uuid:
            return f"ai:{provider}:{uuid}"
    hint = item.get("_terminal_identity_hint")
    if isinstance(hint, Mapping) and hint.get("provider") in PROVIDERS:
        uuid = valid_uuid(hint.get("uuid"))
        if uuid:
            return f"ai:{hint['provider']}:{uuid}"
    return None


def _missing_shell_generation_is_quarantinable(
    item: Mapping[str, Any],
) -> bool:
    """Recognize only an inert disconnected shpool row with no live shell."""
    identity = item.get("identity")
    recovery = item.get("recovery")
    raw_id = item.get("shpool_id_raw")
    started = item.get("started_at_unix_ms")
    diagnostics = item.get("diagnostics")
    expected_diagnostic = (
        f"expected one daemon child for {raw_id!r}, found 0"
        if isinstance(raw_id, str)
        else ""
    )
    return (
        item.get("provider") == "unknown"
        and item.get("display_provider") == "unknown"
        and item.get("availability") == "ready"
        and clean_text(item.get("shpool_status"), 32).casefold() == "disconnected"
        and isinstance(raw_id, str)
        and bool(raw_id)
        and raw_id == item.get("shpool_id")
        and shpool_id_mutation_policy(raw_id) == (True, None)
        and isinstance(started, int)
        and not isinstance(started, bool)
        and started > 0
        and item.get("shpool_shell") is None
        and isinstance(identity, Mapping)
        and identity.get("confidence") == "unknown"
        and identity.get("uuid") is None
        and identity.get("pid") is None
        and identity.get("process_start_ticks") is None
        and identity.get("provenance") == "none"
        and isinstance(recovery, Mapping)
        and recovery.get("available") is False
        and recovery.get("provider") is None
        and recovery.get("uuid") is None
        and not isinstance(item.get("_terminal_identity_hint"), Mapping)
        and isinstance(diagnostics, list)
        and expected_diagnostic in diagnostics
    )


def _missing_shell_generation_is_quarantined(
    item: Mapping[str, Any],
) -> bool:
    return (
        _missing_shell_generation_is_quarantinable(item)
        and item.get("terminal_number") is None
        and item.get("mutation_allowed") is False
        and item.get("mutation_rejection_reason") == "missing-shell-generation"
    )


def apply_terminal_numbers(
    inventory: dict[str, Any],
    registry: dict[str, Any],
    *,
    boot_id: str,
    allocate: bool,
) -> dict[str, Any]:
    """Apply boot-stable selectors without changing contiguous internal rows."""
    checked = _validate_terminal_registry(registry, boot_id)
    bindings: dict[str, int] = dict(checked["bindings"])
    next_number = checked["next_number"]
    active_numbers: set[int] = set()
    for item in inventory.get("sessions", ()):
        if not isinstance(item, dict):
            raise CollectionError("cannot number a non-object managed session")
        generation_key = _terminal_generation_key(inventory, item, boot_id)
        ai_key = _terminal_ai_key(item)
        if generation_key is None:
            if _missing_shell_generation_is_quarantinable(item):
                item["terminal_number"] = None
                item["mutation_allowed"] = False
                item["mutation_rejection_reason"] = "missing-shell-generation"
                item.pop("_terminal_identity_hint", None)
                continue
            if allocate:
                raise CollectionError(
                    "managed session lacks an exact generation for numbering"
                )
        ai_number = bindings.get(ai_key) if ai_key else None
        generation_number = (
            bindings.get(generation_key) if generation_key else None
        )
        generation_ai_key = (
            next(
                (
                    key
                    for key, value in bindings.items()
                    if key.startswith("ai:") and value == generation_number
                ),
                None,
            )
            if generation_number is not None
            else None
        )
        if ai_number is not None:
            # Exact AI identity is the continuity proof when a recovered
            # conversation appears in a newly created shpool generation.  The
            # provisional number remains consumed, but the new generation is
            # rebound to the conversation's established terminal number.
            number = ai_number
        elif generation_number is not None and (
            ai_key is None or generation_ai_key is None
        ):
            number = generation_number
        elif generation_number is not None and not allocate:
            # The generation was already promoted to another exact
            # conversation. A read-only guard must not expose that number as
            # belonging to this unrelated identity.
            number = None
        elif allocate:
            number = next_number
            next_number += 1
        else:
            number = None
        if number is not None:
            if number in active_numbers:
                raise CollectionError(
                    "terminal number registry maps two active sessions to one number"
                )
            active_numbers.add(number)
            if allocate:
                assert generation_key is not None
                for key, value in tuple(bindings.items()):
                    if (
                        key.startswith("generation:")
                        and value == number
                        and key != generation_key
                    ):
                        del bindings[key]
                bindings[generation_key] = number
                if ai_key:
                    bindings[ai_key] = number
        item["terminal_number"] = number
        item.pop("_terminal_identity_hint", None)
    return {
        "schema_version": SCHEMA_VERSION,
        "boot_id": boot_id,
        "next_number": next_number,
        "bindings": dict(sorted(bindings.items())),
    }


def recovery_manifest(inventory: Mapping[str, Any]) -> dict[str, Any]:
    sessions: dict[str, dict[str, Any]] = {}
    for item in inventory.get("sessions", ()):
        recovery = item.get("recovery", {})
        shpool_id = item.get("shpool_id_raw") or item.get("shpool_id")
        if not shpool_id or not recovery.get("available"):
            continue
        sessions[shpool_id] = {
            "scope": "shpool",
            "provider": recovery["provider"],
            "uuid": recovery["uuid"],
            "title": item.get("title", ""),
            "cwd": recovery.get("cwd"),
            "argv": recovery.get("argv", []),
            "command": recovery.get("command"),
        }
    outside_agents: dict[str, dict[str, Any]] = {}
    for item in inventory.get("outside_agents", ()):
        recovery = item.get("recovery", {})
        provider = recovery.get("provider")
        uuid = valid_uuid(recovery.get("uuid"))
        if not recovery.get("available") or provider not in PROVIDERS or not uuid:
            continue
        key = f"outside:{provider}:{uuid}"
        outside_agents[key] = {
            "scope": "outside",
            "provider": provider,
            "uuid": uuid,
            "title": item.get("title", ""),
            "cwd": recovery.get("cwd"),
            "argv": recovery.get("argv", []),
            "command": recovery.get("command"),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": inventory.get("generated_at"),
        "boot_id": _boot_id(),
        "daemon_generation": inventory.get("daemon_generation"),
        "sessions": sessions,
        "outside_agents": outside_agents,
    }


def _generation_key(value: Mapping[str, Any]) -> tuple[str, int, int] | None:
    generation = value.get("daemon_generation")
    if not isinstance(generation, Mapping):
        return None
    boot_id = value.get("boot_id")
    pid = generation.get("pid")
    start_ticks = generation.get("process_start_ticks")
    if (
        not isinstance(boot_id, str)
        or not boot_id
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(start_ticks, int)
        or start_ticks <= 0
    ):
        return None
    return boot_id, pid, start_ticks


def _valid_recovery_state(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("schema_version") == SCHEMA_VERSION
        and isinstance(value.get("sessions"), Mapping)
        and (
            "outside_agents" not in value
            or isinstance(value.get("outside_agents"), Mapping)
        )
    )


def _has_recovery_entries(value: Any) -> bool:
    return bool(
        _valid_recovery_state(value)
        and (value.get("sessions") or value.get("outside_agents"))
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


def _release_sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise CollectionError("release SHA must be one full lowercase 40-character commit")
    release_root = Path(__file__).resolve().parent.parent
    release_metadata = release_root / "RELEASE.json"
    configured_root = os.environ.get("SESSION_KIT_RELEASE_DIR")
    if configured_root and Path(configured_root).resolve() != release_root:
        raise CollectionError("executing release does not match SESSION_KIT_RELEASE_DIR")
    if release_metadata.exists() or configured_root:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(release_metadata, flags)
            metadata = os.fstat(descriptor)
            content = b""
            while True:
                block = os.read(descriptor, 64 * 1024)
                if not block:
                    break
                content += block
        except OSError as exc:
            raise CollectionError("cannot verify executing immutable release") from exc
        finally:
            if "descriptor" in locals():
                os.close(descriptor)
        try:
            document = json.loads(content)
        except (UnicodeDecodeError, ValueError) as exc:
            raise CollectionError("executing RELEASE.json is invalid") from exc
        if (
            not statmod.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or statmod.S_IMODE(metadata.st_mode) & 0o222
            or metadata.st_size > 64 * 1024
            or release_root.name != value
            or not isinstance(document, Mapping)
            or document.get("commit") != value
        ):
            raise CollectionError("release SHA is not bound to the executing immutable release")
    return value


def _daemon_start_epoch(
    proc_root: Path, generation: Mapping[str, Any]
) -> float:
    pid = generation.get("pid")
    expected_ticks = generation.get("process_start_ticks")
    if (
        not isinstance(pid, int)
        or pid <= 0
        or not isinstance(expected_ticks, int)
        or expected_ticks <= 0
    ):
        raise CollectionError("current daemon generation is not exact")
    try:
        before = _proc_stat(proc_root / str(pid) / "stat")
        lines = (proc_root / "stat").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        boot_values = [
            int(line.split()[1])
            for line in lines
            if len(line.split()) == 2 and line.split()[0] == "btime"
        ]
        ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
        after = _proc_stat(proc_root / str(pid) / "stat")
    except (OSError, ValueError) as exc:
        raise CollectionError("cannot prove current daemon wall-clock start") from exc
    if (
        len(boot_values) != 1
        or boot_values[0] <= 0
        or ticks_per_second <= 0
        or before != after
        or before[0] != pid
        or before[2] != expected_ticks
    ):
        raise CollectionError("current daemon changed while proving continuity")
    return boot_values[0] + (expected_ticks / ticks_per_second)


def _legacy_identities(value: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    sessions = value.get("sessions")
    outside = value.get("outside_agents", {})
    if not isinstance(sessions, Mapping) or not sessions or outside:
        raise CollectionError(
            "legacy migration requires nonempty shpool sessions and no outside roots"
        )
    result: dict[str, tuple[str, str]] = {}
    uuids: set[str] = set()
    for shpool_id, item in sessions.items():
        if not isinstance(shpool_id, str) or not shpool_id or not isinstance(item, Mapping):
            raise CollectionError("legacy recovery manifest contains an invalid session")
        provider = item.get("provider")
        uuid = valid_uuid(item.get("uuid"))
        if provider not in PROVIDERS or not uuid or uuid in uuids:
            raise CollectionError("legacy recovery identities must be exact and unique")
        result[shpool_id] = (provider, uuid)
        uuids.add(uuid)
    return result


def _evidence_identities(value: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    rows = value.get("sessions")
    if not isinstance(rows, list) or not rows:
        raise CollectionError("continuity evidence must contain a nonempty session list")
    result: dict[str, tuple[str, str]] = {}
    uuids: set[str] = set()
    for item in rows:
        if not isinstance(item, Mapping):
            raise CollectionError("continuity evidence contains a non-object session")
        shpool_id = item.get("shpool_name")
        provider = item.get("provider")
        uuid = valid_uuid(item.get("provider_uuid"))
        if (
            not isinstance(shpool_id, str)
            or not shpool_id
            or provider not in PROVIDERS
            or not uuid
            or shpool_id in result
            or uuid in uuids
        ):
            raise CollectionError("continuity evidence identities must be exact and unique")
        result[shpool_id] = (provider, uuid)
        uuids.add(uuid)
    return result


def _current_identities(
    inventory: Mapping[str, Any],
) -> tuple[dict[str, tuple[str, str]], set[tuple[str, str]]]:
    sessions: dict[str, tuple[str, str]] = {}
    all_roots: set[tuple[str, str]] = set()
    seen_uuids: set[str] = set()
    for item in inventory.get("sessions", ()):
        identity = item.get("identity")
        recovery = item.get("recovery")
        shpool_id = item.get("shpool_id_raw")
        provider = item.get("provider")
        uuid = valid_uuid(identity.get("uuid")) if isinstance(identity, Mapping) else None
        recovery_uuid = (
            valid_uuid(recovery.get("uuid")) if isinstance(recovery, Mapping) else None
        )
        if (
            not isinstance(shpool_id, str)
            or provider not in PROVIDERS
            or not uuid
            or not isinstance(recovery, Mapping)
            or recovery.get("available") is not True
            or recovery.get("provider") != provider
            or recovery_uuid != uuid
            or shpool_id in sessions
            or uuid in seen_uuids
        ):
            raise CollectionError("current shpool identities are not exact and unique")
        sessions[shpool_id] = (provider, uuid)
        all_roots.add((provider, uuid))
        seen_uuids.add(uuid)
    outside = inventory.get("outside_agents", ())
    if outside:
        # This one-time migration has no trusted historical evidence for
        # outside roots, so including or excluding them would be an assumption.
        raise CollectionError("legacy migration refuses current outside provider roots")
    return sessions, all_roots


def _migration_context(
    config: Mapping[str, Any],
    *,
    legacy_bytes: bytes,
    evidence_path: Path,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    proc_root: Path | None = None,
) -> dict[str, Any]:
    collect = collector or collect_live
    try:
        legacy = json.loads(legacy_bytes)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectionError("legacy recovery manifest is invalid JSON") from exc
    if (
        not isinstance(legacy, Mapping)
        or not _has_recovery_entries(legacy)
        or legacy.get("daemon_generation") is not None
        or not isinstance(legacy.get("boot_id"), str)
        or not legacy.get("boot_id")
    ):
        raise CollectionError("source is not a null-generation legacy manifest")
    legacy_generated = _parse_utc_timestamp(
        legacy.get("generated_at"), "legacy generated_at"
    )
    evidence_bytes, evidence = _parse_private_json(
        evidence_path, "continuity evidence"
    )
    evidence_captured = _parse_utc_timestamp(
        evidence.get("captured_at"), "continuity evidence captured_at"
    )
    if evidence_captured > legacy_generated:
        raise CollectionError("continuity evidence was captured after the legacy manifest")
    legacy_ids = _legacy_identities(legacy)
    if _evidence_identities(evidence) != legacy_ids:
        raise CollectionError(
            "continuity evidence does not exactly match every legacy identity"
        )
    settings = dict(config)
    live = collect(settings)
    if not isinstance(live, Mapping):
        raise CollectionError("collector returned a non-object inventory")
    live = dict(live)
    complete = bool(live.pop("_complete", True))
    if not complete or not strict_live_inventory(live):
        raise CollectionError(
            "legacy recovery migration requires a complete strict live inventory"
        )
    detected = recovery_manifest(live)
    detected_key = _generation_key(detected)
    if (
        detected_key is None
        or not _has_recovery_entries(detected)
        or detected.get("boot_id") != legacy.get("boot_id")
    ):
        raise CollectionError(
            "legacy and current manifests do not have one exact same-boot generation"
        )
    generation = detected["daemon_generation"]
    if evidence.get("daemon_pid") != generation.get("pid"):
        raise CollectionError("current daemon PID does not match continuity evidence")
    root = proc_root or Path(os.environ.get("SESSION_KIT_PROC_ROOT", "/proc"))
    daemon_started = _daemon_start_epoch(root, generation)
    if daemon_started > evidence_captured.timestamp():
        raise CollectionError("current daemon did not predate continuity evidence")
    current_ids, all_current_roots = _current_identities(live)
    if not set(current_ids).issubset(legacy_ids):
        raise CollectionError("current inventory contains a root absent from legacy evidence")
    reconciliation: list[dict[str, Any]] = []
    for shpool_id in sorted(legacy_ids, key=natural_name_key):
        provider, uuid = legacy_ids[shpool_id]
        current = current_ids.get(shpool_id)
        if current is not None:
            if current != (provider, uuid):
                raise CollectionError(
                    f"current identity changed under legacy shpool ID {shpool_id!r}"
                )
            disposition = "carried"
        else:
            if (provider, uuid) in all_current_roots:
                raise CollectionError(
                    f"legacy identity moved from shpool ID {shpool_id!r}"
                )
            disposition = "ended"
        reconciliation.append(
            {
                "shpool_id": shpool_id,
                "provider": provider,
                "uuid": uuid,
                "disposition": disposition,
            }
        )
    return {
        "legacy": dict(legacy),
        "legacy_sha256": _sha256(legacy_bytes),
        "evidence_sha256": _sha256(evidence_bytes),
        "evidence_captured_at": evidence_captured.isoformat().replace("+00:00", "Z"),
        "daemon_started_at": dt.datetime.fromtimestamp(
            daemon_started, dt.timezone.utc
        ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "generation": dict(generation),
        "target_manifest": detected,
        "current_roots": [
            {"shpool_id": key, "provider": value[0], "uuid": value[1]}
            for key, value in sorted(current_ids.items(), key=lambda item: natural_name_key(item[0]))
        ],
        "reconciliation": reconciliation,
    }


def _plan_token(plan: Mapping[str, Any]) -> str:
    body = dict(plan)
    body.pop("plan_token", None)
    return _sha256(_json_bytes(body))


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
    paths = _state_paths(config)
    if paths["pending"].exists() or paths["pending"].is_symlink():
        raise CollectionError("legacy recovery migration refuses an existing pending queue")
    source_before = _read_private_regular_bytes(paths["manifest"])
    evidence_path = Path(continuity_evidence).expanduser()
    if not evidence_path.is_absolute():
        evidence_path = Path.cwd() / evidence_path
    context = _migration_context(
        config,
        legacy_bytes=source_before,
        evidence_path=evidence_path,
        collector=collector,
        proc_root=proc_root,
    )
    if _read_private_regular_bytes(paths["manifest"]) != source_before:
        raise CollectionError("legacy manifest changed while planning")
    release = _release_sha(release_sha)
    source_hash = context["legacy_sha256"]
    archive = paths["root"] / f"recovery-manifest.legacy.{source_hash}.json"
    target = context["target_manifest"]
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "plan_version": 1,
        "action": "legacy-recovery-manifest-migration",
        "created_at": _utc_now(now),
        "release_sha": release,
        "source_manifest": {
            "path": str(paths["manifest"].resolve()),
            "sha256": source_hash,
            "boot_id": context["legacy"]["boot_id"],
            "generated_at": context["legacy"]["generated_at"],
        },
        "continuity_evidence": {
            "path": str(evidence_path.resolve()),
            "sha256": context["evidence_sha256"],
            "captured_at": context["evidence_captured_at"],
            "daemon_pid": context["generation"]["pid"],
            "daemon_started_at": context["daemon_started_at"],
        },
        "daemon_generation": context["generation"],
        "current_roots": context["current_roots"],
        "reconciliation": context["reconciliation"],
        "target_manifest": target,
        "target_manifest_sha256": _sha256(_json_bytes(target)),
        "archive_path": str(archive.resolve()),
    }
    plan["plan_token"] = _plan_token(plan)
    return plan


def publish_legacy_migration_plan(
    config: Mapping[str, Any], output: str | Path, plan: Mapping[str, Any]
) -> dict[str, Any]:
    """Durably create a reviewed plan as a private, non-overwriting state file."""
    paths = _state_paths(config)
    destination = Path(output).expanduser()
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    with StateLock(paths["root"], paths["lock"]):
        if destination.parent.resolve() != paths["root"].resolve():
            raise CollectionError(
                "migration plan output must be inside the owner-only state directory"
            )
        if destination.exists() or destination.is_symlink():
            raise CollectionError(f"migration plan output already exists: {destination}")
        content = _json_bytes(plan)
        _write_legacy_manifest_backup(destination, content)
        if _read_private_regular_bytes(destination) != content:
            raise CollectionError("durable migration plan verification failed")
    dispositions = {
        "carried": sum(
            item.get("disposition") == "carried"
            for item in plan.get("reconciliation", ())
            if isinstance(item, Mapping)
        ),
        "ended": sum(
            item.get("disposition") == "ended"
            for item in plan.get("reconciliation", ())
            if isinstance(item, Mapping)
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "planned",
        "plan": str(destination),
        "plan_token": plan.get("plan_token"),
        "release_sha": plan.get("release_sha"),
        "source_manifest_sha256": plan.get("source_manifest", {}).get("sha256"),
        "target_manifest_sha256": plan.get("target_manifest_sha256"),
        "reconciliation": dispositions,
    }


def _validate_migration_plan(
    plan: Mapping[str, Any], paths: Mapping[str, Path], release_sha: str
) -> None:
    expected_top_level = {
        "schema_version",
        "plan_version",
        "action",
        "created_at",
        "release_sha",
        "source_manifest",
        "continuity_evidence",
        "daemon_generation",
        "current_roots",
        "reconciliation",
        "target_manifest",
        "target_manifest_sha256",
        "archive_path",
        "plan_token",
    }
    if (
        set(plan) != expected_top_level
        or plan.get("schema_version") != SCHEMA_VERSION
        or plan.get("plan_version") != 1
        or plan.get("action") != "legacy-recovery-manifest-migration"
        or plan.get("release_sha") != _release_sha(release_sha)
        or plan.get("plan_token") != _plan_token(plan)
        or not isinstance(plan.get("source_manifest"), Mapping)
        or not isinstance(plan.get("continuity_evidence"), Mapping)
        or not isinstance(plan.get("target_manifest"), Mapping)
        or not isinstance(plan.get("reconciliation"), list)
        or not isinstance(plan.get("current_roots"), list)
        or not isinstance(plan.get("daemon_generation"), Mapping)
    ):
        raise CollectionError("migration plan is invalid, altered, or for another release")
    source = plan["source_manifest"]
    evidence = plan["continuity_evidence"]
    target = plan["target_manifest"]
    if (
        set(source) != {"path", "sha256", "boot_id", "generated_at"}
        or set(evidence)
        != {"path", "sha256", "captured_at", "daemon_pid", "daemon_started_at"}
        or source.get("path") != str(paths["manifest"].resolve())
        or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("sha256", "")))
        or plan.get("archive_path")
        != str(
            (
                paths["root"]
                / f"recovery-manifest.legacy.{source.get('sha256')}.json"
            ).resolve()
        )
        or plan.get("target_manifest_sha256")
        != _sha256(_json_bytes(target))
        or _generation_key(target)
        != (
            source.get("boot_id"),
            plan.get("daemon_generation", {}).get("pid"),
            plan.get("daemon_generation", {}).get("process_start_ticks"),
        )
        or evidence.get("daemon_pid")
        != plan.get("daemon_generation", {}).get("pid")
    ):
        raise CollectionError("migration plan paths or hashes are invalid")


def _receipt_path(paths: Mapping[str, Path]) -> Path:
    return paths["root"] / "recovery-manifest-migration-receipt.json"


def _migration_receipt(
    plan: Mapping[str, Any], phase: str, *, now: float | None = None
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "legacy-recovery-manifest-migration",
        "phase": phase,
        "updated_at": _utc_now(now),
        "plan_token": plan["plan_token"],
        "release_sha": plan["release_sha"],
        "source_manifest_sha256": plan["source_manifest"]["sha256"],
        "target_manifest_sha256": plan["target_manifest_sha256"],
        "continuity_evidence_sha256": plan["continuity_evidence"]["sha256"],
        "daemon_generation": plan["daemon_generation"],
        "reconciliation": plan["reconciliation"],
        "archive_path": plan["archive_path"],
    }


def _read_matching_receipt(
    path: Path, plan: Mapping[str, Any], *, required: bool
) -> Mapping[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        if required:
            raise CollectionError("migration receipt is missing")
        return None
    _, receipt = _parse_private_json(path, "migration receipt")
    expected_fields = {
        "schema_version",
        "action",
        "phase",
        "updated_at",
        "plan_token",
        "release_sha",
        "source_manifest_sha256",
        "target_manifest_sha256",
        "continuity_evidence_sha256",
        "daemon_generation",
        "reconciliation",
        "archive_path",
    }
    _parse_utc_timestamp(receipt.get("updated_at"), "migration receipt updated_at")
    if (
        set(receipt) != expected_fields
        or receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("action") != "legacy-recovery-manifest-migration"
        or receipt.get("plan_token") != plan.get("plan_token")
        or receipt.get("release_sha") != plan.get("release_sha")
        or receipt.get("source_manifest_sha256")
        != plan["source_manifest"]["sha256"]
        or receipt.get("target_manifest_sha256")
        != plan.get("target_manifest_sha256")
        or receipt.get("continuity_evidence_sha256")
        != plan["continuity_evidence"]["sha256"]
        or receipt.get("daemon_generation") != plan.get("daemon_generation")
        or receipt.get("reconciliation") != plan.get("reconciliation")
        or receipt.get("archive_path") != plan.get("archive_path")
        or receipt.get("phase") not in {"archive-published", "applied", "rolled-back"}
    ):
        raise CollectionError("migration receipt does not match the reviewed plan")
    return receipt


def _revalidate_plan_context(
    config: Mapping[str, Any],
    plan: Mapping[str, Any],
    legacy_bytes: bytes,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None,
    proc_root: Path | None,
) -> None:
    evidence_path = Path(str(plan["continuity_evidence"]["path"]))
    context = _migration_context(
        config,
        legacy_bytes=legacy_bytes,
        evidence_path=evidence_path,
        collector=collector,
        proc_root=proc_root,
    )
    semantic_target = dict(context["target_manifest"])
    semantic_target["generated_at"] = plan["target_manifest"].get("generated_at")
    if (
        context["legacy_sha256"] != plan["source_manifest"]["sha256"]
        or context["evidence_sha256"] != plan["continuity_evidence"]["sha256"]
        or context["generation"] != plan["daemon_generation"]
        or context["current_roots"] != plan["current_roots"]
        or context["reconciliation"] != plan["reconciliation"]
        or semantic_target != plan["target_manifest"]
        or _sha256(_json_bytes(plan["target_manifest"]))
        != plan["target_manifest_sha256"]
    ):
        raise CollectionError("live state no longer matches the reviewed migration plan")


def apply_legacy_recovery_manifest(
    config: Mapping[str, Any],
    plan_path: str | Path,
    release_sha: str,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    proc_root: Path | None = None,
) -> dict[str, Any]:
    """Apply or safely resume one reviewed legacy migration plan."""
    paths = _state_paths(config)
    _, plan = _parse_private_json(Path(plan_path).expanduser(), "migration plan")
    _validate_migration_plan(plan, paths, release_sha)
    source_hash = plan["source_manifest"]["sha256"]
    target_hash = plan["target_manifest_sha256"]
    archive = Path(str(plan["archive_path"]))
    receipt_path = _receipt_path(paths)
    with StateLock(paths["root"], paths["lock"]):
        if paths["pending"].exists() or paths["pending"].is_symlink():
            raise CollectionError("legacy recovery migration refuses an existing pending queue")
        canonical = _read_private_regular_bytes(paths["manifest"])
        canonical_hash = _sha256(canonical)
        receipt = _read_matching_receipt(
            receipt_path, plan, required=canonical_hash == target_hash
        )
        if receipt and receipt.get("phase") == "rolled-back":
            raise CollectionError("reviewed migration plan was already rolled back")
        if canonical_hash not in {source_hash, target_hash}:
            raise CollectionError("recovery manifest no longer matches plan source or target")
        if archive.exists() or archive.is_symlink():
            legacy_bytes = _read_private_regular_bytes(archive)
            if _sha256(legacy_bytes) != source_hash:
                raise CollectionError("legacy archive does not match the reviewed source")
        else:
            if canonical_hash != source_hash:
                raise CollectionError("legacy archive is missing after target publication")
            legacy_bytes = canonical
        if canonical_hash == target_hash:
            if receipt is None:
                raise CollectionError("target manifest has no matching migration receipt")
            if receipt.get("phase") != "applied":
                atomic_write_json(
                    receipt_path, _migration_receipt(plan, "applied")
                )
            return {
                "schema_version": SCHEMA_VERSION,
                "result": "already-applied",
                "plan_token": plan["plan_token"],
                "receipt": str(receipt_path),
            }
        _revalidate_plan_context(
            config,
            plan,
            legacy_bytes,
            collector=collector,
            proc_root=proc_root,
        )
        if not archive.exists():
            _write_legacy_manifest_backup(archive, canonical)
        if _sha256(_read_private_regular_bytes(archive)) != source_hash:
            raise CollectionError("durable legacy archive verification failed")
        atomic_write_json(
            receipt_path, _migration_receipt(plan, "archive-published")
        )
        if _sha256(_read_private_regular_bytes(paths["manifest"])) != source_hash:
            raise CollectionError("legacy manifest changed before migration publication")
        atomic_write_json(paths["manifest"], plan["target_manifest"])
        if _sha256(_read_private_regular_bytes(paths["manifest"])) != target_hash:
            raise CollectionError("target recovery manifest verification failed")
        atomic_write_json(receipt_path, _migration_receipt(plan, "applied"))
        return {
            "schema_version": SCHEMA_VERSION,
            "result": "applied",
            "plan_token": plan["plan_token"],
            "archive": str(archive),
            "receipt": str(receipt_path),
        }


def rollback_legacy_recovery_manifest(
    config: Mapping[str, Any],
    plan_path: str | Path,
    release_sha: str,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    proc_root: Path | None = None,
) -> dict[str, Any]:
    """Restore exact legacy bytes while every plan and generation guard holds."""
    paths = _state_paths(config)
    _, plan = _parse_private_json(Path(plan_path).expanduser(), "migration plan")
    _validate_migration_plan(plan, paths, release_sha)
    source_hash = plan["source_manifest"]["sha256"]
    target_hash = plan["target_manifest_sha256"]
    archive = Path(str(plan["archive_path"]))
    receipt_path = _receipt_path(paths)
    with StateLock(paths["root"], paths["lock"]):
        if paths["pending"].exists() or paths["pending"].is_symlink():
            raise CollectionError("legacy migration rollback refuses an existing pending queue")
        receipt = _read_matching_receipt(receipt_path, plan, required=True)
        assert receipt is not None
        legacy_bytes = _read_private_regular_bytes(archive)
        if _sha256(legacy_bytes) != source_hash:
            raise CollectionError("legacy archive does not match the reviewed source")
        canonical = _read_private_regular_bytes(paths["manifest"])
        canonical_hash = _sha256(canonical)
        if canonical_hash not in {source_hash, target_hash}:
            raise CollectionError("rollback refuses a changed recovery manifest")
        _revalidate_plan_context(
            config,
            plan,
            legacy_bytes,
            collector=collector,
            proc_root=proc_root,
        )
        if canonical_hash == source_hash:
            if receipt.get("phase") != "rolled-back":
                atomic_write_json(
                    receipt_path, _migration_receipt(plan, "rolled-back")
                )
            return {
                "schema_version": SCHEMA_VERSION,
                "result": "already-rolled-back",
                "plan_token": plan["plan_token"],
                "receipt": str(receipt_path),
            }
        _atomic_write_private_bytes(paths["manifest"], legacy_bytes)
        if _sha256(_read_private_regular_bytes(paths["manifest"])) != source_hash:
            raise CollectionError("exact legacy rollback verification failed")
        atomic_write_json(receipt_path, _migration_receipt(plan, "rolled-back"))
        return {
            "schema_version": SCHEMA_VERSION,
            "result": "rolled-back",
            "plan_token": plan["plan_token"],
            "receipt": str(receipt_path),
        }


def update_recovery_state(paths: Mapping[str, Path], inventory: Mapping[str, Any]) -> None:
    """Preserve exact pre-generation recovery data until explicitly resolved.

    ``recovery-manifest.json`` is the latest nonempty generation.  When a new
    boot or daemon generation appears, the prior manifest is copied to
    ``recovery-pending.json`` before the current manifest can advance.  Empty
    and partial post-reboot inventories therefore cannot erase the queue.
    """
    detected = recovery_manifest(inventory)
    existing = _read_state_json(paths["manifest"])
    pending = _read_state_json(paths["pending"])
    existing_valid = _has_recovery_entries(existing)
    pending_valid = _has_recovery_entries(pending)
    detected_key = _generation_key(detected)
    existing_key = _generation_key(existing) if existing_valid else None

    # An unavailable/ambiguous daemon identity is not evidence of a restart.
    # Do not advance or queue anything until both sides have exact generation
    # keys; a later complete refresh can make the transition safely.
    if detected_key is None or (existing_valid and existing_key is None):
        return

    if existing_valid and existing_key == detected_key:
        existing_semantic = dict(existing)
        detected_semantic = dict(detected)
        existing_semantic.pop("generated_at", None)
        detected_semantic.pop("generated_at", None)
        if existing_semantic == detected_semantic:
            # Timestamp-only refreshes must not invalidate an exact migration
            # postimage or create needless state churn.
            return

    if existing_valid and existing_key != detected_key:
        boot_changed = existing_key[0] != detected_key[0]
        new_pending = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": inventory.get("generated_at"),
            "source_boot_id": existing.get("boot_id"),
            "source_daemon_generation": existing.get("daemon_generation"),
            "detected_boot_id": detected.get("boot_id"),
            "detected_daemon_generation": detected.get("daemon_generation"),
            "sessions": dict(existing["sessions"]),
            # Outside-provider roots survive a daemon-only restart; they are
            # recovery work only when the host boot itself changed.
            "outside_agents": (
                dict(existing.get("outside_agents", {})) if boot_changed else {}
            ),
        }
        if pending_valid:
            pending_source = (
                pending.get("source_boot_id"),
                json.dumps(pending.get("source_daemon_generation"), sort_keys=True),
            )
            new_source = (
                new_pending.get("source_boot_id"),
                json.dumps(new_pending.get("source_daemon_generation"), sort_keys=True),
            )
            if pending_source == new_source:
                # Never shrink an unresolved queue during a partial restore.
                new_pending["sessions"] = {
                    **new_pending["sessions"],
                    **dict(pending["sessions"]),
                }
                new_pending["outside_agents"] = {
                    **new_pending.get("outside_agents", {}),
                    **dict(pending.get("outside_agents", {})),
                }
            else:
                # A second generation arrived before the first was reviewed.
                # Keep the oldest queue primary and retain the later one
                # explicitly instead of silently overwriting either.
                new_pending = dict(pending)
                queued = list(new_pending.get("queued_generations", ()))
                candidate = {
                    "source_boot_id": existing.get("boot_id"),
                    "source_daemon_generation": existing.get("daemon_generation"),
                    "sessions": dict(existing["sessions"]),
                    "outside_agents": (
                        dict(existing.get("outside_agents", {}))
                        if boot_changed
                        else {}
                    ),
                }
                candidate_key = (
                    candidate["source_boot_id"],
                    json.dumps(candidate["source_daemon_generation"], sort_keys=True),
                )
                existing_keys = {
                    (
                        item.get("source_boot_id"),
                        json.dumps(item.get("source_daemon_generation"), sort_keys=True),
                    )
                    for item in queued
                    if isinstance(item, Mapping)
                }
                if candidate_key not in existing_keys:
                    queued.append(candidate)
                new_pending["queued_generations"] = queued
                new_pending["detected_boot_id"] = detected.get("boot_id")
                new_pending["detected_daemon_generation"] = detected.get(
                    "daemon_generation"
                )
        if _has_recovery_entries(new_pending):
            atomic_write_json(paths["pending"], new_pending)

    # Never replace the latest nonempty exact manifest with an empty snapshot.
    if detected["sessions"] or detected["outside_agents"]:
        atomic_write_json(paths["manifest"], detected)


def source_generation_key(boot_id: Any, generation: Any) -> str | None:
    if not isinstance(generation, Mapping):
        return None
    candidate = {
        "boot_id": boot_id,
        "daemon_generation": generation,
    }
    exact = _generation_key(candidate)
    if exact is None:
        return None
    return f"{exact[0]}:{exact[1]}:{exact[2]}"


def flatten_pending(value: Any) -> dict[str, Any]:
    """Return all pending generations as stable, directly actionable entries."""
    entries: list[dict[str, Any]] = []
    if not _valid_recovery_state(value):
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": None,
            "entries": [],
        }
    generations: list[
        tuple[
            str,
            int | None,
            Any,
            Any,
            Mapping[str, Any],
            Mapping[str, Any],
        ]
    ] = [
        (
            "primary",
            None,
            value.get("source_boot_id"),
            value.get("source_daemon_generation"),
            value.get("sessions", {}),
            value.get("outside_agents", {}),
        )
    ]
    for index, queued in enumerate(value.get("queued_generations", ())):
        if not isinstance(queued, Mapping) or not isinstance(queued.get("sessions"), Mapping):
            continue
        generations.append(
            (
                "queued",
                index,
                queued.get("source_boot_id"),
                queued.get("source_daemon_generation"),
                queued["sessions"],
                queued.get("outside_agents", {})
                if isinstance(queued.get("outside_agents", {}), Mapping)
                else {},
            )
        )
    for (
        queue,
        queue_index,
        boot_id,
        generation,
        sessions,
        outside_agents,
    ) in generations:
        key = source_generation_key(boot_id, generation)
        scoped_entries = (
            ("shpool", sessions),
            ("outside", outside_agents),
        )
        for scope, stored_entries in scoped_entries:
            for old_shpool_id, raw_session in stored_entries.items():
                if not isinstance(old_shpool_id, str) or not isinstance(
                    raw_session, Mapping
                ):
                    continue
                uuid = valid_uuid(raw_session.get("uuid"))
                provider = raw_session.get("provider")
                entries.append(
                    {
                        "source_generation_key": key,
                        "source_boot_id": boot_id,
                        "source_daemon_generation": generation,
                        "queue": queue,
                        "queue_index": queue_index,
                        "scope": scope,
                        "old_shpool_id": old_shpool_id,
                        "display_old_shpool_id": display_shpool_id(old_shpool_id),
                        "provider": provider if provider in PROVIDERS else None,
                        "uuid": uuid,
                        "title": clean_text(raw_session.get("title"), 120),
                        "cwd": clean_text(raw_session.get("cwd"), 4096) or None,
                        "argv": list(raw_session.get("argv", ()))
                        if isinstance(raw_session.get("argv"), list)
                        else [],
                        "command": clean_text(raw_session.get("command"), 8192)
                        or None,
                        "actionable": bool(key and uuid and provider in PROVIDERS),
                    }
                )
    entries.sort(
        key=lambda item: (
            item["source_generation_key"] or "",
            natural_name_key(item["old_shpool_id"]),
            item["uuid"] or "",
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": value.get("generated_at"),
        "detected_boot_id": value.get("detected_boot_id"),
        "detected_daemon_generation": value.get("detected_daemon_generation"),
        "entries": entries,
    }


def list_pending(config: Mapping[str, Any]) -> dict[str, Any]:
    paths = _state_paths(config)
    with StateLock(paths["root"], paths["lock"]):
        return flatten_pending(_read_state_json(paths["pending"]))


def _remove_pending_entry(
    pending: dict[str, Any],
    *,
    queue: str,
    queue_index: int | None,
    scope: str,
    old_shpool_id: str,
) -> None:
    container = "outside_agents" if scope == "outside" else "sessions"
    if queue == "primary":
        sessions = pending.get(container)
        if isinstance(sessions, dict):
            sessions.pop(old_shpool_id, None)
        return
    queued = pending.get("queued_generations")
    if not isinstance(queued, list) or not isinstance(queue_index, int):
        raise CollectionError("pending queue changed during acknowledgment")
    if queue_index < 0 or queue_index >= len(queued):
        raise CollectionError("pending generation changed during acknowledgment")
    generation = queued[queue_index]
    if not isinstance(generation, dict) or not isinstance(
        generation.get(container), dict
    ):
        raise CollectionError("pending generation is malformed")
    generation[container].pop(old_shpool_id, None)
    if not generation.get("sessions") and not generation.get("outside_agents"):
        queued.pop(queue_index)


def acknowledge_pending(
    config: Mapping[str, Any],
    generation_key: str,
    old_shpool_id: str,
    uuid: str,
    *,
    collector: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare-and-ack one restored entry under the inventory state lock."""
    exact_uuid = valid_uuid(uuid)
    if not generation_key or not old_shpool_id or not exact_uuid:
        raise CollectionError("pending ack requires generation key, old shpool ID, and exact UUID")
    paths = _state_paths(config)
    with StateLock(paths["root"], paths["lock"]):
        pending_raw = _read_state_json(paths["pending"])
        if not _valid_recovery_state(pending_raw):
            raise CollectionError("no valid pending recovery queue exists")
        pending = json.loads(json.dumps(pending_raw))
        flattened = flatten_pending(pending)
        matches = [
            item
            for item in flattened["entries"]
            if item["source_generation_key"] == generation_key
            and item["old_shpool_id"] == old_shpool_id
            and item["uuid"] == exact_uuid
            and item["actionable"]
        ]
        if len(matches) != 1:
            raise CollectionError("pending entry no longer matches generation, shpool ID, and UUID")
        target = matches[0]

        settings = dict(config)
        live = (
            collector(settings)
            if collector is not None
            else collect_live(settings)
        )
        complete = bool(live.pop("_complete", True))
        if not complete or not strict_live_inventory(live):
            raise CollectionError("strict live inventory unavailable; pending entry was not acknowledged")
        active_matches = [
            item
            for item in [*live.get("sessions", ()), *live.get("outside_agents", ())]
            if item.get("provider") == target["provider"]
            and item.get("identity", {}).get("uuid") == exact_uuid
            and item.get("identity", {}).get("confidence") == "exact"
        ]
        if len(active_matches) != 1:
            raise CollectionError(
                "exact pending provider UUID is not uniquely active; pending entry was not acknowledged"
            )
        _remove_pending_entry(
            pending,
            queue=target["queue"],
            queue_index=target["queue_index"],
            scope=target["scope"],
            old_shpool_id=old_shpool_id,
        )
        pending["updated_at"] = _utc_now()
        atomic_write_json(paths["pending"], pending)
        return {
            "schema_version": SCHEMA_VERSION,
            "acknowledged": target,
            "remaining": flatten_pending(pending),
        }


def _cold_inventory(error: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "source": "cold",
        "stale": True,
        "warnings": [clean_text(error, 400)],
        "daemon_generation": None,
        "sessions": [],
        "outside_agents": [],
    }


def snapshot(*, write_state: bool = True, config: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = config or load_config()
    paths = _state_paths(settings)
    if write_state:
        with StateLock(paths["root"], paths["lock"]):
            cached = _read_state_json(paths["inventory"])
    else:
        # --no-write is also useful in deployment validation.  Do not create a
        # state directory or lock file merely to inspect existing state.
        cached = _read_state_json(paths["inventory"])
    try:
        live = collect_live(settings)
    except CollectionError as exc:
        live = None
        failure = str(exc)
    else:
        failure = "; ".join(live.get("warnings", ()))
    complete = bool(live and live.pop("_complete", True))
    if complete:
        result = live
        try:
            boot_id = _boot_id()
            if not boot_id:
                raise CollectionError("current boot identity is unavailable")
            if write_state:
                with StateLock(paths["root"], paths["lock"]):
                    locked_cached = _read_state_json(paths["inventory"])
                    _lifecycle.persist_last_exact(
                        result,
                        (
                            locked_cached
                            if isinstance(locked_cached, Mapping)
                            else None
                        ),
                        state_dir=paths["root"],
                        boot_id=boot_id,
                    )
                    _lifecycle.apply_provider_exit_states(
                        result,
                        (
                            locked_cached
                            if isinstance(locked_cached, Mapping)
                            else None
                        ),
                        state_dir=paths["root"],
                        boot_id=boot_id,
                    )
                    registry = _read_terminal_registry(
                        paths["terminal_numbers"],
                        boot_id,
                        paths["terminal_numbers_epoch"],
                    )
                    updated_registry = apply_terminal_numbers(
                        result, registry, boot_id=boot_id, allocate=True
                    )
                    atomic_write_json(
                        paths["terminal_numbers"], updated_registry
                    )
                    atomic_write_json(
                        paths["terminal_numbers_epoch"],
                        {
                            "schema_version": SCHEMA_VERSION,
                            "boot_id": boot_id,
                        },
                    )
                    atomic_write_json(paths["inventory"], result)
                    update_recovery_state(paths, result)
                    _lifecycle.prune_inactive_state(
                        paths["root"],
                        [
                            item["shpool_id_raw"]
                            for item in result.get("sessions", ())
                            if isinstance(item, Mapping)
                            and isinstance(item.get("shpool_id_raw"), str)
                        ],
                    )
            else:
                _lifecycle.apply_provider_exit_states(
                    result,
                    cached if isinstance(cached, Mapping) else None,
                    state_dir=paths["root"],
                    boot_id=boot_id,
                )
                registry = _read_terminal_registry(
                    paths["terminal_numbers"],
                    boot_id,
                    paths["terminal_numbers_epoch"],
                )
                apply_terminal_numbers(
                    result, registry, boot_id=boot_id, allocate=False
                )
        except CollectionError as exc:
            failure = str(exc)
            warnings = list(result.get("warnings", ()))
            warnings.append(f"terminal numbering unavailable: {failure}")
            result["warnings"] = warnings
            complete = False
        if complete:
            return result
    if isinstance(cached, Mapping) and cached.get("schema_version") == SCHEMA_VERSION:
        result = dict(cached)
        result["source"] = "cache"
        result["stale"] = True
        warnings = list(result.get("warnings", ()))
        warnings.append(f"live refresh failed; showing last-known-good: {failure}")
        result["warnings"] = warnings
        return result
    if live is not None:
        live["source"] = "cold"
        live["stale"] = True
        return live
    return _cold_inventory(f"inventory unavailable and no last-known-good cache exists: {failure}")


def _format_age(seconds: int | None) -> str:
    if seconds is None:
        return ""
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _short_path(path: str) -> str:
    home = str(_home())
    return f"~{path[len(home):]}" if path == home or path.startswith(home + os.sep) else path


def _display_width(value: str) -> int:
    """Conservative terminal-cell width without adding a runtime dependency."""
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in value
    )


def _display_title(value: Any, limit: int = 48) -> str:
    """Control-safe, terminal-cell-aware truncation for display only."""
    text = clean_text(value, 10000)
    if _display_width(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    kept: list[str] = []
    used = 0
    for character in text:
        cells = _display_width(character)
        if used + cells > limit - 1:
            break
        kept.append(character)
        used += cells
    return f"{''.join(kept)}…"


def _color_enabled() -> bool:
    return (
        sys.stdout.isatty()
        and not os.environ.get("NO_COLOR")
        and not os.environ.get("SESSION_KIT_NO_COLOR")
    )


DEFAULT_STALL_SECONDS = 2700


def stall_threshold_seconds() -> int:
    """Silence after which a session stops being described as running.

    Deliberately far above any normal pause. Codex sessions report "running"
    for their whole life, working or waiting, so a short threshold would label
    every parked session and the warning would stop meaning anything. Override
    with SESSION_KIT_STALL_SECONDS.
    """
    return _positive_int(
        os.environ.get("SESSION_KIT_STALL_SECONDS"),
        DEFAULT_STALL_SECONDS,
        60,
        86400,
    )


def render_inventory(inventory: Mapping[str, Any], rows_only: bool = False) -> str:
    """Render action-first/provider groups with sequential visible row numbers."""
    color = _color_enabled()
    bold = "\033[1m" if color else ""
    dim = "\033[2m" if color else ""
    cyan = "\033[36m" if color else ""
    green = "\033[32m" if color else ""
    yellow = "\033[33m" if color else ""
    reset = "\033[0m" if color else ""
    session_palette = (
        {
            "red": "\033[31m",
            "blue": "\033[34m",
            "green": "\033[32m",
            "yellow": "\033[33m",
            "purple": "\033[35m",
            "orange": "\033[38;5;208m",
            "pink": "\033[38;5;205m",
            "cyan": "\033[36m",
        }
        if color
        else {}
    )

    def tint(item: Mapping[str, Any], text: str) -> str:
        code = session_palette.get(item.get("display_color") or "")
        return f"{code}{text}{reset}" if code else text
    terminal_columns = shutil.get_terminal_size(fallback=(101, 24)).columns
    columns = _positive_int(
        os.environ.get("COLUMNS"),
        max(2, min(240, terminal_columns)),
        2,
        240,
    )
    width = max(1, columns - 1)
    lines: list[str] = []
    if inventory.get("stale"):
        lines.append(f"{yellow}  Warning: showing {inventory.get('source')} inventory.{reset}")
    sessions = list(inventory.get("sessions", ()))
    has_terminal_numbers = any(
        isinstance(item, Mapping)
        and isinstance(item.get("terminal_number"), int)
        and not isinstance(item.get("terminal_number"), bool)
        and item.get("terminal_number", 0) > 0
        for item in sessions
    )
    available_count = sum(
        1 for item in sessions if item.get("availability") == "ready"
    )
    attached_count = sum(
        1 for item in sessions if item.get("availability") == "attached"
    )
    session_word = "session" if len(sessions) == 1 else "sessions"
    lines.append(
        f"  {len(sessions)} {session_word}: {available_count} available, "
        f"{attached_count} open in another window"
    )
    if sessions:
        lines.append("")
    for availability in ("ready", "attached"):
        selected = [item for item in sessions if item.get("availability") == availability]
        if not selected:
            continue
        heading = (
            "Available to open"
            if availability == "ready"
            else "Already open in another window"
        )
        lines.append(f"  {bold}{heading}{reset}")
        for provider in ("claude", "codex", "shell", "unknown"):
            # Group by the display provider so this view agrees with the
            # chooser: a started-but-unused Codex session belongs under Codex,
            # not under Unknown, even though its identity is not yet resolvable.
            group = [
                item
                for item in selected
                if (item.get("display_provider") or item.get("provider")) == provider
            ]
            if not group:
                continue
            lines.append(f"    {cyan}{provider.title()}{reset}")
            for item in group:
                process_age = _format_age(item.get("process_age_seconds"))
                recent_output_age = _format_age(
                    item.get("recent_output_age_seconds")
                )
                status = clean_text(item.get("agent_status") or "unknown", 64)
                recent_seconds = item.get("recent_output_age_seconds")
                # "running" is not evidence for Codex, which reports it whether
                # it is working or waiting. State the silence instead.
                if (
                    status.casefold() == "running"
                    and isinstance(recent_seconds, int)
                    and not isinstance(recent_seconds, bool)
                    and recent_seconds >= stall_threshold_seconds()
                    and recent_output_age
                ):
                    status = f"quiet {recent_output_age}"
                status_parts = (
                    ["needs your reply"]
                    if item.get("needs_you")
                    else [status]
                )
                if recent_output_age:
                    status_parts.append(f"recent output {recent_output_age} ago")
                if process_age:
                    status_parts.append(f"process age {process_age}")
                agent_count = len(item.get("subagents", ()))
                if agent_count:
                    status_parts.append(
                        f"{agent_count} subagent{'s' if agent_count != 1 else ''}"
                    )
                selector = item.get("terminal_number")
                if (
                    isinstance(selector, bool)
                    or not isinstance(selector, int)
                    or selector <= 0
                ):
                    selector = "-" if has_terminal_numbers else item.get("row")
                prefix = f"      {selector:>2}  "
                title_room = max(1, width - _display_width(prefix))
                title = _display_title(item.get("title"), title_room)
                lines.append(
                    f"      {green}{selector:>2}{reset}  {tint(item, title)}"
                )
                detail_prefix = "          "
                detail_room = max(1, width - _display_width(detail_prefix))
                details = _display_title(" | ".join(status_parts), detail_room)
                detail_color = (
                    yellow
                    if item.get("needs_you")
                    or status.casefold() == "provider exited"
                    else dim
                )
                lines.append(f"{detail_prefix}{detail_color}{details}{reset}")
    outside = list(inventory.get("outside_agents", ()))
    if outside:
        lines.append(f"  {bold}Outside shpool{reset}")
        for item in outside:
            uuid = item.get("identity", {}).get("uuid") or ""
            agent_count = len(item.get("subagents", ()))
            prefix = f"      -  [{item.get('provider')}] "
            title = _display_title(
                item.get("title"), max(1, width - _display_width(prefix))
            )
            lines.append(f"{prefix}{tint(item, title)}")
            detail_parts = [clean_text(item.get("agent_status") or "unknown", 64)]
            if uuid:
                detail_parts.append(uuid[:8])
            if agent_count:
                detail_parts.append(
                    f"{agent_count} subagent{'s' if agent_count != 1 else ''}"
                )
            detail_prefix = "          "
            details = _display_title(
                " | ".join(detail_parts),
                max(1, width - _display_width(detail_prefix)),
            )
            lines.append(f"{detail_prefix}{dim}{details}{reset}")
    if not sessions and not outside:
        lines.append("  No sessions found.")
    if not rows_only:
        lines.extend(
            [
                "",
                f"  {dim}sp go <n>: open | sp new: start{reset}",
                f"  {dim}sp close <n>: close | sp find \"text\": search history{reset}",
            ]
        )
    return "\n".join(lines)


def lookup(inventory: Mapping[str, Any], selector: str) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    if selector.isdigit():
        number = int(selector)
        sessions = list(inventory.get("sessions", ()))
        if any(
            isinstance(item.get("terminal_number"), int)
            and not isinstance(item.get("terminal_number"), bool)
            and item.get("terminal_number", 0) > 0
            for item in sessions
            if isinstance(item, Mapping)
        ):
            matches = [
                item
                for item in sessions
                if item.get("terminal_number") == number
            ]
        else:
            matches = [item for item in sessions if item.get("row") == number]
    else:
        matches = [
            item
            for item in inventory.get("sessions", ())
            if (item.get("shpool_id_raw") or item.get("shpool_id")) == selector
        ]
    return matches[0] if len(matches) == 1 else None


def strict_live_inventory(inventory: Mapping[str, Any]) -> bool:
    """True only for a complete, unambiguous live mutation guard snapshot."""
    if not guard_live_inventory(inventory):
        return False
    generation = inventory.get("daemon_generation")
    outside_agents = inventory.get("outside_agents")
    if (
        inventory.get("source") != "live"
        or inventory.get("stale") is not False
        or inventory.get("warnings")
        or not isinstance(generation, Mapping)
        or not isinstance(generation.get("pid"), int)
        or generation.get("pid", 0) <= 0
        or not isinstance(generation.get("process_start_ticks"), int)
        or generation.get("process_start_ticks", 0) <= 0
        or not isinstance(outside_agents, list)
    ):
        return False
    exact_provider_uuids: set[tuple[str, str]] = set()
    for item in inventory.get("sessions", ()):
        identity = item.get("identity")
        shell = item.get("shpool_shell")
        raw_id = item.get("shpool_id_raw")
        provider = item.get("provider")
        mutation_allowed, mutation_reason = shpool_id_mutation_policy(raw_id)
        if (
            not isinstance(raw_id, str)
            or raw_id != item.get("shpool_id")
            or item.get("mutation_allowed") is not mutation_allowed
            or item.get("mutation_rejection_reason") != mutation_reason
            or provider not in {"claude", "codex", "shell"}
            or not isinstance(identity, Mapping)
            or identity.get("confidence") != "exact"
            or not isinstance(identity.get("pid"), int)
            or not isinstance(identity.get("process_start_ticks"), int)
            or not isinstance(shell, Mapping)
            or not isinstance(shell.get("pid"), int)
            or not isinstance(shell.get("process_start_ticks"), int)
        ):
            return False
        if provider in PROVIDERS:
            uuid = identity.get("uuid")
            key = (provider, uuid)
            if valid_uuid(uuid) != uuid or key in exact_provider_uuids:
                return False
            exact_provider_uuids.add(key)
        elif identity.get("uuid") is not None:
            return False
    for item in outside_agents:
        if not isinstance(item, Mapping):
            return False
        provider = item.get("provider")
        identity = item.get("identity")
        if (
            provider not in PROVIDERS
            or not isinstance(identity, Mapping)
            or identity.get("confidence") != "exact"
            or isinstance(identity.get("pid"), bool)
            or not isinstance(identity.get("pid"), int)
            or identity.get("pid", 0) <= 0
            or isinstance(identity.get("process_start_ticks"), bool)
            or not isinstance(identity.get("process_start_ticks"), int)
            or identity.get("process_start_ticks", 0) <= 0
        ):
            return False
        uuid = identity.get("uuid")
        key = (provider, uuid)
        if valid_uuid(uuid) != uuid or key in exact_provider_uuids:
            return False
        exact_provider_uuids.add(key)
    return True


def guard_live_inventory(inventory: Mapping[str, Any]) -> bool:
    """Validate a live mutation snapshot without rejecting unrelated unknown rows.

    Unlike ``strict_live_inventory``, this predicate permits an unresolved
    provider session.  It still requires an exact daemon generation, an exact
    shell generation for every managed row, and exact provider identity for
    every row whose provider is known.
    """

    def positive_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    if not isinstance(inventory, Mapping):
        return False
    generation = inventory.get("daemon_generation")
    warnings = inventory.get("warnings")
    sessions = inventory.get("sessions")
    outside_agents = inventory.get("outside_agents")
    if (
        inventory.get("schema_version") != SCHEMA_VERSION
        or inventory.get("source") != "live"
        or inventory.get("stale") is not False
        or not isinstance(warnings, list)
        or warnings
        or not isinstance(generation, Mapping)
        or not positive_int(generation.get("pid"))
        or not positive_int(generation.get("process_start_ticks"))
        or not isinstance(sessions, list)
        or not isinstance(outside_agents, list)
    ):
        return False

    rows: set[int] = set()
    terminal_numbers: set[int] = set()
    raw_ids: set[str] = set()
    exact_provider_uuids: set[tuple[str, str]] = set()
    for item in sessions:
        if not isinstance(item, Mapping):
            return False
        row = item.get("row")
        raw_id = item.get("shpool_id_raw")
        identity = item.get("identity")
        shell = item.get("shpool_shell")
        provider = item.get("provider")
        terminal_number = item.get("terminal_number")
        if _missing_shell_generation_is_quarantined(item):
            if (
                not positive_int(row)
                or row in rows
                or not isinstance(raw_id, str)
                or raw_id in raw_ids
            ):
                return False
            rows.add(row)
            raw_ids.add(raw_id)
            continue
        mutation_allowed, mutation_reason = shpool_id_mutation_policy(raw_id)
        if (
            not positive_int(row)
            or row in rows
            or not isinstance(raw_id, str)
            or not raw_id
            or raw_id in raw_ids
            or raw_id != item.get("shpool_id")
            or item.get("mutation_allowed") is not mutation_allowed
            or item.get("mutation_rejection_reason") != mutation_reason
            or not isinstance(identity, Mapping)
            or not isinstance(shell, Mapping)
            or not positive_int(shell.get("pid"))
            or not positive_int(shell.get("process_start_ticks"))
        ):
            return False
        if terminal_number is not None and (
            isinstance(terminal_number, bool)
            or not isinstance(terminal_number, int)
            or terminal_number <= 0
            or terminal_number in terminal_numbers
        ):
            return False
        if isinstance(terminal_number, int):
            terminal_numbers.add(terminal_number)
        rows.add(row)
        raw_ids.add(raw_id)

        if provider == "unknown":
            if (
                identity.get("confidence") != "unknown"
                or identity.get("uuid") is not None
                or identity.get("pid") != shell.get("pid")
                or identity.get("process_start_ticks")
                != shell.get("process_start_ticks")
            ):
                return False
            continue
        if provider not in {"claude", "codex", "shell"}:
            return False
        if (
            identity.get("confidence") != "exact"
            or not positive_int(identity.get("pid"))
            or not positive_int(identity.get("process_start_ticks"))
        ):
            return False
        if provider in {"claude", "codex"}:
            uuid = identity.get("uuid")
            key = (provider, uuid)
            if (
                valid_uuid(uuid) != uuid
                or key in exact_provider_uuids
            ):
                return False
            exact_provider_uuids.add(key)
        elif identity.get("uuid") is not None:
            return False

    for item in outside_agents:
        if not isinstance(item, Mapping):
            return False
        provider = item.get("provider")
        identity = item.get("identity")
        if (
            provider not in PROVIDERS
            or not isinstance(identity, Mapping)
            or identity.get("confidence") != "exact"
            or not positive_int(identity.get("pid"))
            or not positive_int(identity.get("process_start_ticks"))
        ):
            return False
        uuid = identity.get("uuid")
        key = (provider, uuid)
        if (
            valid_uuid(uuid) != uuid
            or key in exact_provider_uuids
        ):
            return False
        exact_provider_uuids.add(key)

    return rows == set(range(1, len(sessions) + 1))


def load_inventory_input(path: str | Path) -> dict[str, Any]:
    """Load a previously frozen v1 snapshot for TOCTOU-safe render/lookup."""
    source = Path(path).expanduser()
    try:
        value = _load_json_file(source)
    except (OSError, ValueError) as exc:
        raise CollectionError(f"cannot read inventory input {source}: {exc}") from exc
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != SCHEMA_VERSION
        or not isinstance(value.get("sessions"), list)
        or not isinstance(value.get("outside_agents"), list)
    ):
        raise CollectionError(f"inventory input {source} is not a valid v1 snapshot")
    names: set[str] = set()
    rows: set[int] = set()
    terminal_numbers: set[int] = set()
    has_terminal_numbers = any(
        isinstance(item, Mapping)
        and isinstance(item.get("terminal_number"), int)
        and not isinstance(item.get("terminal_number"), bool)
        and item.get("terminal_number", 0) > 0
        for item in value["sessions"]
    )
    for item in value["sessions"]:
        if not isinstance(item, Mapping):
            raise CollectionError(f"inventory input {source} has a non-object session")
        name = item.get("shpool_id")
        raw_name = item.get("shpool_id_raw", name)
        row = item.get("row")
        terminal_number = item.get("terminal_number")
        if (
            not isinstance(name, str)
            or not name
            or raw_name != name
            or not isinstance(row, int)
            or row <= 0
            or name in names
            or row in rows
            or (
                has_terminal_numbers
                and _missing_shell_generation_is_quarantinable(item)
                and not _missing_shell_generation_is_quarantined(item)
            )
            or (
                has_terminal_numbers
                and terminal_number is None
                and not _missing_shell_generation_is_quarantined(item)
            )
            or (
                has_terminal_numbers
                and terminal_number is not None
                and (
                    isinstance(terminal_number, bool)
                    or not isinstance(terminal_number, int)
                    or terminal_number <= 0
                    or terminal_number in terminal_numbers
                )
            )
        ):
            raise CollectionError(f"inventory input {source} has invalid or duplicate selectors")
        names.add(name)
        rows.add(row)
        if has_terminal_numbers and terminal_number is not None:
            terminal_numbers.add(terminal_number)
    return dict(value)


def _process_ancestor_chain(
    process_table: Mapping[int, Mapping[str, Any]], current_pid: int
) -> list[int]:
    chain: list[int] = []
    seen: set[int] = set()
    pid = current_pid
    for _ in range(128):
        if pid in seen or pid <= 0:
            raise CollectionError("automatic name caller ancestry is ambiguous")
        process = process_table.get(pid)
        if not isinstance(process, Mapping):
            raise CollectionError("automatic name caller process is stale")
        chain.append(pid)
        seen.add(pid)
        parent = process.get("ppid")
        if isinstance(parent, bool) or not isinstance(parent, int) or parent <= 0:
            break
        if parent not in process_table:
            break
        pid = parent
    else:
        raise CollectionError("automatic name caller ancestry exceeds safety bound")
    return chain


def prove_self_name_caller(
    inventory: Mapping[str, Any],
    process_table: Mapping[int, Mapping[str, Any]],
    environ: Mapping[str, str],
    current_pid: int,
) -> dict[str, Any]:
    """Prove one exact managed root conversation and its current shell generation."""
    if inventory.get("source") != "live" or inventory.get("stale") is not False:
        raise CollectionError("automatic naming requires a fresh live inventory")
    shpool_id = environ.get("SHPOOL_SESSION_NAME", "")
    if not shpool_id:
        raise CollectionError("automatic naming is unavailable outside a managed shell")
    matches = [
        item
        for item in inventory.get("sessions", ())
        if isinstance(item, Mapping) and item.get("shpool_id_raw") == shpool_id
    ]
    if len(matches) != 1:
        raise CollectionError("automatic name caller session is ambiguous or stale")
    item = matches[0]
    provider = item.get("provider")
    identity = item.get("identity")
    shell = item.get("shpool_shell")
    if (
        provider not in PROVIDERS
        or item.get("setup_incomplete") is not False
        or not isinstance(identity, Mapping)
        or identity.get("confidence") != "exact"
        or not isinstance(shell, Mapping)
    ):
        raise CollectionError("automatic name caller setup is incomplete")
    uuid = valid_uuid(identity.get("uuid"))
    provider_pid = identity.get("pid")
    provider_start = identity.get("process_start_ticks")
    shell_pid = shell.get("pid")
    shell_start = shell.get("process_start_ticks")
    if (
        not uuid
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (provider_pid, provider_start, shell_pid, shell_start)
        )
    ):
        raise CollectionError("automatic name caller lacks an exact generation")
    chain = _process_ancestor_chain(process_table, current_pid)
    if provider_pid not in chain or shell_pid not in chain:
        raise CollectionError("automatic name caller is outside the exact provider root")
    if chain.index(provider_pid) >= chain.index(shell_pid):
        raise CollectionError("automatic name caller ancestry is not a managed root")
    if process_table[provider_pid].get("start_ticks") != provider_start:
        raise CollectionError("automatic name provider generation changed")
    if process_table[shell_pid].get("start_ticks") != shell_start:
        raise CollectionError("automatic name shell generation changed")
    for pid in chain[: chain.index(provider_pid) + 1]:
        argv = process_table[pid].get("cmdline") or ()
        if "--parent-session-id" in argv or "--agent-name" in argv:
            raise CollectionError("automatic naming is refused for a subagent")
    if provider == "codex":
        caller_uuid = valid_uuid(environ.get("CODEX_THREAD_ID"))
        if any(
            isinstance(child, Mapping)
            and child.get("provider") == "codex"
            and valid_uuid(child.get("uuid")) == caller_uuid
            for child in item.get("subagents", ())
        ):
            raise CollectionError("automatic naming is refused for a subagent")
        if caller_uuid != uuid:
            raise CollectionError("Codex caller is not the managed root conversation")
    else:
        claude_uuid = environ.get("CLAUDE_SESSION_ID")
        if claude_uuid and valid_uuid(claude_uuid) != uuid:
            raise CollectionError("Claude caller is not the managed root conversation")
    return {
        "provider": provider,
        "uuid": uuid,
        "shpool_id": shpool_id,
        "provider_pid": provider_pid,
        "provider_start_ticks": provider_start,
        "shell_pid": shell_pid,
        "shell_start_ticks": shell_start,
    }


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
    caller_environ = environ if environ is not None else os.environ
    if not automatic_naming_enabled(caller_environ):
        raise CollectionError("automatic naming is disabled")
    proc_root = Path(caller_environ.get("SESSION_KIT_PROC_ROOT", "/proc"))
    supplied_evidence = inventory is not None or process_table is not None
    live = inventory or snapshot(write_state=False, config=dict(config))
    table = process_table or platform_process_table(
        proc_root, int(config.get("max_proc_nodes", DEFAULT_MAX_PROC_NODES))
    )
    pid = current_pid if current_pid is not None else os.getpid()
    evidence = prove_self_name_caller(live, table, caller_environ, pid)

    def revalidate() -> None:
        if supplied_evidence:
            new_live, new_table = live, table
        else:
            new_live = snapshot(write_state=False, config=dict(config))
            new_table = platform_process_table(
                proc_root,
                int(config.get("max_proc_nodes", DEFAULT_MAX_PROC_NODES)),
            )
        repeated = prove_self_name_caller(
            new_live, new_table, caller_environ, pid
        )
        if repeated != evidence:
            raise CollectionError("automatic name caller changed before write")

    try:
        normalized = normalize_automatic_title(title)
    except CollectionError as reason:
        attempts = record_automatic_title_failure(
            config,
            str(evidence["provider"]),
            str(evidence["uuid"]),
            revalidate=revalidate,
        )
        suffix = "name failed" if attempts >= 2 else "name pending"
        raise CollectionError(
            f"automatic title rejected ({reason}); {suffix}"
        ) from reason
    result = mutate_canonical_automatic_title(
        config,
        str(evidence["provider"]),
        str(evidence["uuid"]),
        normalized,
        revalidate=revalidate,
    )
    result["caller"] = evidence
    result["automatic_name_state"] = "ready"
    result.update(
        propagate_provider_title(
            str(evidence["provider"]),
            str(evidence["uuid"]),
            normalized,
            environ=caller_environ,
        )
    )
    effective_color = session_color(
        str(evidence["provider"]),
        str(evidence["uuid"]),
        canonical_colors(config),
    )
    if effective_color:
        result.update(
            propagate_provider_color(
                str(evidence["provider"]),
                str(evidence["uuid"]),
                effective_color,
                environ=caller_environ,
            )
        )
    return result


def _json_print(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _platform_command(args: argparse.Namespace) -> int:
    platform = _require_supported_platform()
    if args.platform_action == "boot-id":
        value = _boot_id()
        if not value:
            raise CollectionError("current boot identity is unavailable")
        print(value)
        return 0
    root = Path(os.environ.get("SESSION_KIT_PROC_ROOT", "/proc"))
    if platform == DARWIN_PLATFORM and args.platform_action == "process-info":
        table = scan_darwin_process_table(1, pids=[args.pid])
    else:
        table = platform_process_table(root, DEFAULT_MAX_PROC_NODES)
    process = table.get(args.pid)
    if process is None:
        raise CollectionError("process generation is unavailable")
    if args.platform_action == "process-info":
        print(f"{process.get('ppid', 0)}\t{process.get('start_ticks', 0)}")
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
    title = args.title if args.alias_action == "set" else None
    aliases = mutate_canonical_alias(config, args.provider, args.uuid, title)
    payload: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "aliases": aliases}
    if args.alias_action == "set":
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


def _color_command(args: argparse.Namespace, config: dict[str, Any]) -> int:
    if args.color_action == "launch-pick":
        color = record_launch_color(config, args.shpool_id)
        if not color:
            return 1
        print(color)
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
    platform_parser = subparsers.add_parser("platform")
    platform_subparsers = platform_parser.add_subparsers(
        dest="platform_action", required=True
    )
    platform_subparsers.add_parser("boot-id")
    platform_process = platform_subparsers.add_parser("process-info")
    platform_process.add_argument("pid", type=int)
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
    automatic_parser = subparsers.add_parser("automatic-title")
    automatic_subparsers = automatic_parser.add_subparsers(
        dest="automatic_title_action", required=True
    )
    automatic_subparsers.add_parser("list")
    automatic_subparsers.add_parser("from-hook")
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
    if _require_supported_platform() != "linux":
        raise CollectionError("lifecycle state is supported on Linux only")
    proc_root = Path("/proc")
    if (
        os.environ.get("SESSION_KIT_TESTING") == "1"
        and os.environ.get("SESSION_KIT_PROC_ROOT")
    ):
        proc_root = Path(os.environ["SESSION_KIT_PROC_ROOT"])
    process_table = scan_process_table(proc_root, DEFAULT_MAX_PROC_NODES)
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
    common = {
        "state_dir": state_dir,
        "session_id": session_id,
        "boot_id": boot_id,
        "shell_pid": shell_pid,
        "shell_start_ticks": shell_start,
    }
    if args.lifecycle_action == "provider-exited":
        provider = os.environ.get("SESSION_KIT_LIFECYCLE_PROVIDER", "")
        try:
            exit_code = int(
                os.environ.get("SESSION_KIT_LIFECYCLE_EXIT_CODE", "")
            )
        except ValueError as exc:
            raise CollectionError("lifecycle provider exit code is invalid") from exc
        value = _lifecycle.record_provider_exit(
            **common,
            provider=provider,
            exit_code=exit_code,
            input_tracking=True,
        )
    elif args.lifecycle_action == "user-input":
        value = _lifecycle.update_state(**common, event="user-input")
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
        cwd = expected.get("cwd")
        if cwd is not None and (not os.path.isabs(cwd) or not os.path.isdir(cwd)):
            raise CollectionError(
                "provider recovery directory is unavailable; nothing reopened"
            )
        completed = subprocess.run(argv, cwd=cwd, check=False)
        value = _lifecycle.record_provider_exit(
            **common,
            provider=provider,
            exit_code=completed.returncode % 256,
            input_tracking=True,
        )
        return 0
    else:
        value = _lifecycle.update_state(
            **common,
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
