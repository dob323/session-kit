"""Bounded native process discovery, ancestry, generations, and boot identity."""

from __future__ import annotations

import ctypes
import ctypes.util
from enum import Enum
import os
from pathlib import Path
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

from .common import CollectionError, clean_text, proc_root as default_proc_root


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


def _readable_by_us(entry: Path) -> bool | None:
    """Whether this process could read ``entry``'s restricted proc files.

    ``/proc/<pid>/environ`` and ``/proc/<pid>/cwd`` are readable only by the
    process owner, so for any other uid the read always fails and the caller
    falls back to the same empty values this guard returns. Skipping it keeps a
    scan from making one denied syscall per foreign process, which on a busy
    host with auditing on failed opens is a lot of log for no information.

    Root is exempt, so it keeps reading everything. ``None`` preserves a failed
    ownership check as unknown instead of turning it into a foreign process.
    """
    if os.getuid() == 0:
        return True
    try:
        return entry.stat().st_uid == os.getuid()
    except OSError:
        return None


class ProcessTable(dict):
    """A process table that remembers whether it saw everything.

    Most readers ask "is this pid in the table?" and act on the answer. One
    reader asks the opposite -- "is NOTHING in this tree holding that socket?"
    -- and a table with a hole in it answers that question wrongly and
    silently, because a process the scan lost is indistinguishable from a
    process that was never there. Where the parent is known the loss is
    recorded in place (see `_unreadable_process`); where even that could not be
    read, the pid cannot be attributed to any tree, so the only honest record
    is that this reading of the machine is not complete.

    It is a plain dict in every other respect, so a caller that never asks
    about absence is unaffected, and a hand-built fixture with no flag reads as
    complete, which is what a fixture is.
    """

    complete: bool = True


def table_is_complete(process_table: Mapping[int, Mapping[str, Any]]) -> bool:
    """Whether this reading of the process table saw every process."""
    return bool(getattr(process_table, "complete", True))


def _unreadable_process(pid: int, ppid: int) -> dict[str, Any]:
    """A process that exists, placed in the tree, saying nothing about itself.

    Every field a reader might match on is empty, so nothing here can be
    mistaken for evidence; `argv_unreadable` is the one positive statement,
    and it is what a reader needs in order to refuse a verdict that rests on
    absence. The parent is published because placing the process in the right
    tree is the whole point -- a process nobody can attribute is a process
    whose absence somebody will read as proof.

    The start time is deliberately impossible. This entry is published exactly
    when identity could NOT be confirmed, and every verifier in the kit
    compares a recorded start tick against the live one before it acts on a
    process; a stale-but-plausible value would let one of those confirm an
    identity that no longer exists. -1 matches nothing, so they all refuse.
    """
    return {
        "pid": pid,
        "ppid": ppid,
        "start_ticks": -1,
        "cmdline": [],
        "comm": "",
        "cwd": "",
        "argv_unreadable": True,
        "environ_unreadable": True,
        "session_name": "",
        "claude_config_dir": "",
        "codex_home": "",
        "account_alias": "",
        "account_capable": "",
    }


def _unreadable_shpool_process(pid: int, ppid: int) -> dict[str, Any]:
    """An exact shpool name whose argv and generation cannot be trusted."""
    process = _unreadable_process(pid, ppid)
    process["comm"] = "shpool"
    process["args_available"] = False
    return process


def scan_process_table(
    proc_root: Path,
    max_nodes: int,
    *,
    proc_stat: Callable[[Path], tuple[int, int, int]],
    proc_environ: Callable[[Path], dict[str, str]],
) -> dict[int, dict[str, Any]]:
    """Read one bounded process-table view from proc_root."""
    table = ProcessTable()
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
                # The directory entry and the file disagree about which
                # process this is; nothing read here can be attributed.
                table.complete = False
                continue
        except (OSError, ValueError):
            # It was listed a moment ago and its stat will not read, so its
            # parent is unknown and it cannot be placed in any tree. Unlike
            # the cases below there is no slot to leave behind -- the only
            # honest record is that this reading of the machine has a hole in
            # it, and a reader that would conclude "nothing in this tree holds
            # that socket" must not conclude it from a table with a hole.
            table.complete = False
            continue
        try:
            comm = clean_text(
                (entry / "comm").read_text(encoding="utf-8", errors="replace"), 128
            )
        except (OSError, ValueError):
            # A process whose stat read but whose argv did not is one that
            # EXISTS and will not say what it is. Dropped, it left the table
            # reading "no such process", so a reader asking "is anything in
            # this tree holding that socket?" got "no" about a process it
            # never saw, and answering that question with "no" is what puts
            # a person's session behind the machine count. It stays, placed by
            # its parent, with the ambiguity recorded the same way an
            # unreadable environment is.
            table[pid] = _unreadable_process(pid, ppid)
            continue
        try:
            cmdline = [
                value.decode("utf-8", errors="replace")
                for value in (entry / "cmdline").read_bytes().split(b"\0")
                if value
            ]
            args_available = True
        except OSError:
            # Keep only the one unreadable-argv process shape that affects
            # daemon selection. Retaining every process whose arguments are
            # hidden would turn an ordinary large process table into a field
            # of false daemon candidates; an exact comm=shpool row is the
            # bounded uncertainty this collector must carry forward.
            if comm != "shpool":
                table[pid] = _unreadable_process(pid, ppid)
                continue
            cmdline = []
            args_available = False
        # An own-uid environment that will not read is not the same fact as an
        # empty one: `environ` needs PTRACE_MODE_READ, so a hardening change
        # (yama, a container policy) can deny it for a live managed shell. Left
        # indistinguishable, that shell looks like a session with no process at
        # all, the exact state the auto-close engine treats as "nothing to
        # preserve". Record the ambiguity and let the readers refuse.
        environ_unreadable = False
        try:
            owner_uid: int | None = entry.stat().st_uid
        except OSError:
            owner_uid = None
        readable = _readable_by_us(entry)
        if readable:
            try:
                environ = proc_environ(entry / "environ")
            except OSError:
                environ = {}
                environ_unreadable = True
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
            if readable is None:
                environ_unreadable = True
        try:
            after_pid, after_ppid, after_start_ticks = proc_stat(entry / "stat")
        except (OSError, ValueError):
            # It was there when the walk started and it is not answering now.
            # The fields already read may describe a process that has since
            # exec'd into something else, so they are not published, but the
            # slot is, because "it exited" and "it exec'd into the window" are
            # the same reading from here.
            table[pid] = (
                _unreadable_shpool_process(pid, ppid)
                if not args_available
                else _unreadable_process(pid, ppid)
            )
            continue
        if (before_pid, ppid, start_ticks) != (
            after_pid,
            after_ppid,
            after_start_ticks,
        ):
            table[pid] = (
                _unreadable_shpool_process(pid, ppid)
                if not args_available
                else _unreadable_process(pid, ppid)
            )
            continue
        table[pid] = {
            "pid": pid,
            "ppid": ppid,
            "start_ticks": start_ticks,
            "cmdline": cmdline,
            "comm": comm,
            "cwd": cwd,
            # Which account owns the process. Read here, while /proc/<pid> is
            # in hand, so daemon selection can rule out another account's
            # shpool without a second pass over the process table.
            "uid": owner_uid,
            "environ_unreadable": environ_unreadable,
            "session_name": environ.get("SHPOOL_SESSION_NAME", ""),
            "claude_config_dir": environ.get("CLAUDE_CONFIG_DIR", ""),
            "codex_home": environ.get("CODEX_HOME", ""),
            "account_alias": environ.get("SESSION_KIT_ACCOUNT_ALIAS", ""),
            "account_capable": environ.get("SESSION_KIT_ACCOUNT_CAPABLE", ""),
        }
        if not args_available:
            table[pid]["args_available"] = False
            table[pid]["argv_unreadable"] = True
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
    table = ProcessTable()
    for pid in unique_pids:
        try:
            before = read_bsd(pid)
            generation = darwin_generation(before)
        except (OSError, ValueError):
            # Same rule as the Linux scan: with no parent there is no tree to
            # place this in, so the reading of the machine is incomplete and
            # nobody may conclude an absence from it.
            table.complete = False
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
            table[pid] = _unreadable_process(pid, int(before.pbi_ppid))
            continue
        before_identity = (int(before.pbi_pid), int(before.pbi_ppid), generation)
        after_identity = (
            int(after.pbi_pid),
            int(after.pbi_ppid),
            after_generation,
        )
        if before_identity != after_identity or before_identity[0] != pid:
            table[pid] = _unreadable_process(pid, int(before.pbi_ppid))
            continue
        table[pid] = {
            "pid": pid,
            "ppid": before_identity[1],
            "start_ticks": generation,
            "generation_kind": "darwin-start-usec",
            "args_available": args_available,
            # Darwin's argv read is denied for other uids and for hardened
            # processes, so "no argv" here is routine. It is still an argv
            # nobody read, and a reader deciding an absence must refuse it for
            # the same reason it refuses one on Linux.
            "argv_unreadable": not args_available,
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


def _is_unknown_shpool_daemon(process: Mapping[str, Any]) -> bool:
    """Whether unreadable argv leaves an exact shpool comm as a candidate."""
    return (
        process.get("args_available") is False
        and not process.get("cmdline")
        and clean_text(process.get("comm"), 128) == "shpool"
    )


def _kit_socket_path(environ: Mapping[str, str]) -> str | None:
    """Where a client started the way this kit starts one connects.

    The pinned shpool resolves ``--socket``, then
    ``$XDG_RUNTIME_DIR/shpool/shpool.socket``, then
    ``~/.local/run/shpool/shpool.socket``. It does NOT read ``SHPOOL_SOCKET``:
    that was checked against the pinned source and the installed binary after a
    reviewer showed an earlier version of this code inventing support for it.
    This kit passes no ``--socket``, so its client lands on one of the last two.
    """
    runtime_dir = environ.get("XDG_RUNTIME_DIR") or ""
    if runtime_dir:
        return os.path.normpath(f"{runtime_dir}/shpool/shpool.socket")
    home = environ.get("HOME") or ""
    if home:
        return os.path.normpath(f"{home}/.local/run/shpool/shpool.socket")
    return None


# `/proc/net/unix` flags carry SO_ACCEPTCON for a socket that is listening.
# Connected peers of a unix socket appear on their own rows carrying the same
# pathname, so without this the first row for a path is as likely to be a
# conversation with the daemon as the daemon's own listener.
_ACCEPTCON = 0x10000


def _listening_inodes(path: str, proc_root: Path) -> set[int] | None:
    """Every listening unix socket bound at ``path``, per the kernel.

    A pathname is not unique in this table. A daemon whose socket file was
    replaced keeps its row while a new daemon binds the same path, and every
    connected peer repeats the pathname too. Reading only the first match let a
    stranger's inode stand in for ours, so all of them are collected and the
    caller refuses if they do not agree on one holder. ``None`` means the
    kernel table could not be read completely.
    """
    inodes: set[int] = set()
    try:
        raw = (proc_root / "net" / "unix").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # proc's sequence-file records are newline terminated. An unterminated
    # final record means the table was not read whole, so an earlier matching
    # row cannot be treated as the only listener.
    if not raw or not raw.endswith("\n"):
        return None
    lines = raw.splitlines()
    if not lines or len(lines[0].split()) < 7:
        return None
    for line in lines[1:]:
        fields = line.split(None, 7)
        if len(fields) < 7:
            return None
        # Abstract and unnamed sockets legitimately have no pathname column.
        if len(fields) == 7:
            continue
        # The pathname is the remainder of the line: it may contain spaces.
        if os.path.normpath(fields[7].rstrip("\n")) != path:
            continue
        try:
            if not int(fields[3], 16) & _ACCEPTCON:
                continue
            inodes.add(int(fields[6]))
        except ValueError:
            return None
    return inodes


class _SocketHolding(Enum):
    HOLDS = "holds"
    DOES_NOT_HOLD = "does-not-hold"
    UNKNOWN = "unknown"


def _holds_any_socket(pid: int, inodes: set[int], proc_root: Path) -> _SocketHolding:
    targets = {f"socket:[{inode}]" for inode in inodes}
    try:
        entries = list((proc_root / str(pid) / "fd").iterdir())
    except OSError:
        return _SocketHolding.UNKNOWN
    unreadable = False
    for entry in entries:
        try:
            if os.readlink(entry) in targets:
                return _SocketHolding.HOLDS
        except OSError:
            unreadable = True
    return _SocketHolding.UNKNOWN if unreadable else _SocketHolding.DOES_NOT_HOLD


def _owned_by_this_account(process: Mapping[str, Any]) -> bool:
    """Whether this process could possibly be serving THIS account's kit.

    A shpool daemon answers on a listener under ``/run/user/<uid>``, which the
    kernel keeps mode 0700 for that uid alone, so a daemon running as another
    account is never ours no matter what it is called. Without this question,
    a second account's daemon on the same host was a permanent outage: its
    ``/proc/<pid>/fd`` is unreadable, the socket-holder check answered
    "unknown", the uniqueness rule then refused to name any daemon, and every
    session on the board became unprovable (found live on a shared host,
    2026-08-17).

    The uid comes from the census row, which read it from ``/proc/<pid>``
    while it was there. A row without one, an old snapshot, a hand-built
    table, is a candidate as before: an unproven guess about a process is
    not evidence against it.
    """
    uid = process.get("uid")
    if isinstance(uid, bool) or not isinstance(uid, int):
        return True
    return uid == os.getuid()


def _kit_daemons(
    process_table: Mapping[int, Mapping[str, Any]],
    is_shpool_daemon: Callable[[Mapping[str, Any]], bool],
    *,
    environ: Mapping[str, str] | None = None,
    proc_root: Path | None = None,
    require_socket_holder: bool = False,
) -> list[int]:
    """Which of the running daemons is the one serving this kit.

    Counting every process named ``shpool`` was the wrong question: on 15 August
    a review lane's own sandboxed daemon, on its own socket and holding nothing,
    left the kit unable to identify a single one of the operator's ten live
    sessions.

    Two earlier answers were wrong in ways reviewers proved. Resolving a name
    among the children of ANY daemon let a stranger's process take over one of
    their rows. Inferring each daemon's socket from its command line and
    environment is not authoritative either: the pinned shpool ignores
    ``SHPOOL_SOCKET`` entirely, and under systemd socket activation it
    disregards ``--socket`` and serves the listener it inherited, so both
    strings can say the opposite of the truth.

    The kernel is asked instead. The socket this kit's client connects to has an
    inode in ``/proc/net/unix``; whichever daemon holds that inode open is the
    one answering us. Nothing a command line or an environment says can change
    that. When the question cannot be answered -- no ``/proc/net/unix``, no such
    path, no holder, or more than one -- every daemon is returned and the caller
    is exactly as strict as it was before any of this existed.
    """
    if not table_is_complete(process_table) and not require_socket_holder:
        # A hole in the census invalidates every conclusion built on absence,
        # so the census fast paths below refuse. The socket-holder question is
        # different: its answer is the kernel's positive statement that one
        # daemon holds the one listener inode at our path, and a process that
        # vanished between readdir and stat cannot un-hold that listener. On a
        # busy host some scan is nearly always churned, and treating every
        # churned scan as "no daemon" unresolved every session on the board.
        return []
    # Ownership first, and before every later question. Another account's
    # daemon is not a candidate this kit has to rule out; it is one the kernel
    # already ruled out.
    daemons = sorted(
        pid
        for pid, process in process_table.items()
        if is_shpool_daemon(process) and _owned_by_this_account(process)
    )
    unknown = sorted(
        pid
        for pid, process in process_table.items()
        if pid not in daemons
        and _is_unknown_shpool_daemon(process)
        and _owned_by_this_account(process)
    )
    if unknown:
        # An unreadable exact shpool command line is the same kind of evidence
        # gap as an unreadable daemon fd directory: it cannot promote another
        # namesake to the unique answer. A sole unknown is also not proof of a
        # daemon, so return no selection rather than laundering it through the
        # one-element fast path.
        candidates = sorted((*daemons, *unknown))
        if require_socket_holder:
            return []
        return candidates if len(candidates) > 1 else []
    if len(daemons) <= 1 and not require_socket_holder:
        return daemons
    values = os.environ if environ is None else environ
    path = _kit_socket_path(values)
    if path is None:
        return [] if require_socket_holder else daemons
    root = default_proc_root(values) if proc_root is None else proc_root
    inodes = _listening_inodes(path, root)
    # More than one listener bound at our path means an old daemon's socket was
    # replaced under it and both rows survive. Which one our client reached is
    # not knowable from here, and three reviewers built the same attack out of
    # guessing: a stranger holding the stale row got selected outright. One row
    # or none.
    if inodes is None or len(inodes) != 1:
        return [] if require_socket_holder else daemons
    holding = {pid: _holds_any_socket(pid, inodes, root) for pid in daemons}
    # An unreadable descriptor directory or entry is not evidence that a
    # daemon does not hold the listener. Until every candidate can be checked,
    # uniqueness has not been established.
    if _SocketHolding.UNKNOWN in holding.values():
        return [] if require_socket_holder else daemons
    holders = [pid for pid, state in holding.items() if state is _SocketHolding.HOLDS]
    # More than one daemon holding a listener on our path is a real state -- an
    # old daemon whose socket file was replaced, or one that inherited the
    # descriptor -- and there is no way to tell from here which one answered us.
    # That is refusal, not a coin toss.
    if len(holders) == 1:
        return holders
    return [] if require_socket_holder else daemons


def shpool_roots(
    session_names: Iterable[str],
    process_table: Mapping[int, Mapping[str, Any]],
    *,
    is_shpool_daemon: Callable[[Mapping[str, Any]], bool],
    daemon_pids: Sequence[int] | None = None,
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Map a shpool name only through a unique daemon direct child."""
    wanted = set(session_names)
    daemons = (
        _kit_daemons(process_table, is_shpool_daemon)
        if daemon_pids is None
        else list(daemon_pids)
    )
    roots: dict[str, int] = {}
    diagnostics: dict[str, list[str]] = {name: [] for name in wanted}
    if len(daemons) != 1:
        reason = (
            "expected one shpool daemon serving this kit in the process table, "
            f"found {len(daemons)}"
        )
        for name in wanted:
            diagnostics[name].append(reason)
        return roots, diagnostics
    daemon = daemons[0]
    # A daemon child whose environment would not read cannot state its session
    # name, so every name that came up empty may in truth belong to it. The
    # ambiguity travels with the row instead of being read as absence.
    unreadable = sorted(
        pid
        for pid, process in process_table.items()
        if process.get("ppid") == daemon and process.get("environ_unreadable")
    )
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
            if unreadable:
                diagnostics[name].append(
                    f"{len(unreadable)} daemon child process(es) have an "
                    "unreadable environment; session names cannot be proven"
                )
    return roots, diagnostics


def daemon_generation(
    process_table: Mapping[int, Mapping[str, Any]],
    *,
    is_shpool_daemon: Callable[[Mapping[str, Any]], bool],
    daemon_pids: Sequence[int] | None = None,
    require_socket_holder: bool = False,
) -> dict[str, int] | None:
    candidates = (
        _kit_daemons(
            process_table,
            is_shpool_daemon,
            require_socket_holder=require_socket_holder,
        )
        if daemon_pids is None
        else list(daemon_pids)
    )
    if len(candidates) != 1:
        return None
    process = process_table[candidates[0]]
    pid = process.get("pid")
    start_ticks = process.get("start_ticks")
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
