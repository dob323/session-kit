"""Bounded native process discovery, ancestry, generations, and boot identity."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from pathlib import Path
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

from .common import CollectionError, clean_text


DARWIN_PLATFORM = "darwin"


def _runtime_platform(
    *,
    environ: Mapping[str, str],
    platform_name: str,
) -> str:
    """Return the real platform, with a test-only override for Linux fixtures."""
    override = environ.get("SESSION_KIT_TEST_PLATFORM")
    if environ.get("SESSION_KIT_TESTING") == "1" and override:
        return override.casefold()
    return platform_name


def _require_supported_platform(*, runtime_platform: Callable[[], str]) -> str:
    platform = runtime_platform()
    if platform != DARWIN_PLATFORM and not platform.startswith("linux"):
        raise CollectionError(f"unsupported platform: {platform}")
    return platform


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


def _readable_by_us(entry: Path) -> bool:
    """True when this process could read ``entry``'s restricted proc files.

    ``/proc/<pid>/environ`` and ``/proc/<pid>/cwd`` are readable only by the
    process owner, so for any other uid the read always fails and the caller
    falls back to the same empty values this guard returns. Skipping it keeps a
    scan from making one denied syscall per foreign process, which on a busy
    host with auditing on failed opens is a lot of log for no information.

    Root is exempt, so it keeps reading everything.
    """
    if os.getuid() == 0:
        return True
    try:
        return entry.stat().st_uid == os.getuid()
    except OSError:
        return False


def scan_process_table(
    proc_root: Path,
    max_nodes: int,
    *,
    proc_stat: Callable[[Path], tuple[int, int, int]],
    proc_environ: Callable[[Path], dict[str, str]],
) -> dict[int, dict[str, Any]]:
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
            f"{proc_root} has {len(entries)} processes, "
            f"above max_proc_nodes={max_nodes}"
        )
    for entry in entries:
        pid = int(entry.name)
        try:
            before_pid, ppid, start_ticks = proc_stat(entry / "stat")
            if before_pid != pid:
                continue
            cmdline = [
                value.decode("utf-8", errors="replace")
                for value in (entry / "cmdline").read_bytes().split(b"\0")
                if value
            ]
            comm = clean_text(
                (entry / "comm").read_text(encoding="utf-8", errors="replace"), 128
            )
        except (OSError, ValueError):
            continue
        if _readable_by_us(entry):
            try:
                environ = proc_environ(entry / "environ")
            except OSError:
                environ = {}
            try:
                cwd = os.readlink(entry / "cwd")
            except OSError:
                cwd = ""
        else:
            # Same result the OSError branches produce, without the denied
            # syscall. A shpool session is always our own uid, so a foreign
            # process can never supply a session name anyway.
            environ = {}
            cwd = ""
        try:
            after_pid, after_ppid, after_start_ticks = proc_stat(entry / "stat")
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
            "claude_config_dir": environ.get("CLAUDE_CONFIG_DIR", ""),
            "codex_home": environ.get("CODEX_HOME", ""),
            "account_alias": environ.get("SESSION_KIT_ACCOUNT_ALIAS", ""),
            "account_capable": environ.get("SESSION_KIT_ACCOUNT_CAPABLE", ""),
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


def _darwin_libraries(*, runtime_platform: Callable[[], str]) -> tuple[Any, Any]:
    if runtime_platform() != DARWIN_PLATFORM:
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
    received = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
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
    darwin_libraries: Callable[[], tuple[Any, Any]],
    darwin_bsd_info: Callable[[Any, int], _DarwinBsdInfo],
    darwin_procargs2: Callable[[Any, int], bytes],
    darwin_generation: Callable[[_DarwinBsdInfo], int],
    parse_darwin_procargs2: Callable[[bytes], tuple[list[str], dict[str, str]]],
    decode_c_string: Callable[[bytes], str],
) -> dict[int, dict[str, Any]]:
    """Read one bounded Darwin process view using libproc and KERN_PROCARGS2."""
    libc: Any = None
    libproc: Any = None
    if pids is None or bsd_reader is None or args_reader is None:
        libc, libproc = darwin_libraries()
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
            f"Darwin has {len(unique_pids)} processes, above max_proc_nodes={max_nodes}"
        )
    read_bsd = bsd_reader or (lambda pid: darwin_bsd_info(libproc, pid))
    read_args = args_reader or (lambda pid: darwin_procargs2(libc, pid))
    table: dict[int, dict[str, Any]] = {}
    for pid in unique_pids:
        try:
            before = read_bsd(pid)
            generation = darwin_generation(before)
        except (OSError, ValueError):
            continue
        args_available = True
        try:
            argv, environ = parse_darwin_procargs2(read_args(pid))
        except (OSError, ValueError):
            argv, environ = [], {}
            args_available = False
        try:
            after = read_bsd(pid)
            after_generation = darwin_generation(after)
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
            "args_available": args_available,
            "cmdline": argv,
            "comm": clean_text(
                decode_c_string(bytes(before.pbi_name))
                or decode_c_string(bytes(before.pbi_comm)),
                128,
            ),
            "cwd": (
                environ.get("PWD", "") if environ.get("PWD", "").startswith("/") else ""
            ),
            "session_name": environ.get("SHPOOL_SESSION_NAME", ""),
            "codex_thread_id": environ.get("CODEX_THREAD_ID", ""),
            "claude_session_id": environ.get("CLAUDE_SESSION_ID", ""),
            "claude_config_dir": environ.get("CLAUDE_CONFIG_DIR", ""),
            "codex_home": environ.get("CODEX_HOME", ""),
            "account_alias": environ.get("SESSION_KIT_ACCOUNT_ALIAS", ""),
            "account_capable": environ.get("SESSION_KIT_ACCOUNT_CAPABLE", ""),
        }
    return table


def platform_process_table(
    proc_root: Path,
    max_nodes: int,
    *,
    require_supported_platform: Callable[[], str],
    darwin_process_table: Callable[[int], dict[int, dict[str, Any]]],
    linux_process_table: Callable[[Path, int], dict[int, dict[str, Any]]],
) -> dict[int, dict[str, Any]]:
    platform = require_supported_platform()
    if platform == DARWIN_PLATFORM:
        return darwin_process_table(max_nodes)
    return linux_process_table(proc_root, max_nodes)


def _children_index(
    process_table: Mapping[int, Mapping[str, Any]],
) -> dict[int, list[int]]:
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
    session_names: Iterable[str],
    process_table: Mapping[int, Mapping[str, Any]],
    *,
    is_shpool_daemon: Callable[[Mapping[str, Any]], bool],
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Map a shpool name only through a unique daemon direct child."""
    wanted = set(session_names)
    daemons = [
        pid for pid, process in process_table.items() if is_shpool_daemon(process)
    ]
    roots: dict[str, int] = {}
    diagnostics: dict[str, list[str]] = {name: [] for name in wanted}
    if len(daemons) != 1:
        reason = (
            f"expected one shpool daemon in the process table, found {len(daemons)}"
        )
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
    process_table: Mapping[int, Mapping[str, Any]],
    *,
    is_shpool_daemon: Callable[[Mapping[str, Any]], bool],
) -> dict[str, int] | None:
    candidates = [
        process for process in process_table.values() if is_shpool_daemon(process)
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
            raise CollectionError(
                f"process tree rooted at PID {root_pid} exceeded max_proc_nodes"
            )
        if depth >= max_depth:
            continue
        queue.extend((child, depth + 1) for child in children.get(pid, ()))
    return found


def _expected_proc_identity(
    proc_root: Path,
    pid: int,
    expected_process: Mapping[str, Any],
    *,
    proc_stat: Callable[[Path], tuple[int, int, int]],
) -> tuple[int, int, int] | None:
    try:
        identity = proc_stat(proc_root / str(pid) / "stat")
    except (OSError, ValueError):
        return None
    expected = (
        pid,
        expected_process.get("ppid"),
        expected_process.get("start_ticks"),
    )
    return identity if identity == expected else None


def _process_age(
    pid: int | None, process_table: Mapping[int, Mapping[str, Any]], now: float
) -> int | None:
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


def _boot_id(
    *,
    environ: Mapping[str, str],
    runtime_platform: Callable[[], str],
    darwin_libraries: Callable[[], tuple[Any, Any]],
) -> str | None:
    override = environ.get("SESSION_KIT_BOOT_ID_FILE")
    if override:
        try:
            value = Path(override).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None
    if runtime_platform() == DARWIN_PLATFORM:
        try:
            libc, _ = darwin_libraries()
            boot_time = _DarwinTimeval()
            size = ctypes.c_size_t(ctypes.sizeof(boot_time))
            if (
                libc.sysctlbyname(
                    b"kern.boottime",
                    ctypes.byref(boot_time),
                    ctypes.byref(size),
                    None,
                    0,
                )
                != 0
            ):
                return None
            if boot_time.tv_sec <= 0 or boot_time.tv_usec < 0:
                return None
            return f"darwin:{boot_time.tv_sec}:{boot_time.tv_usec}"
        except (OSError, ValueError):
            return None
    try:
        value = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
    except OSError:
        return None
    return value or None
