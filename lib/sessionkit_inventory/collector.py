"""Bounded read-only joins: session records, recent output, and live collection.

Every function here composes evidence the caller already gathered, or gathers it
through injected readers. Nothing imports the facade.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from .common import (
    PROVIDERS,
    CollectionError,
    _utc_now,
    _valid_aliases,
    _valid_automatic_title_failures,
    _valid_automatic_titles,
    _valid_name_ownership,
    _pending_native_title_matches,
    _valid_pending_native_titles,
    _valid_pushed_titles,
    automatic_naming_enabled,
    clean_text,
    proc_root as gated_proc_root,
    shpool_id_mutation_policy,
    valid_uuid,
)
from .model import (
    SHELL_PROCESS_NAMES,
    _agent_identity,
    _base_agent,
    _empty_recovery,
    _shell_title,
    canonical_session_order_key,
    recovery_spec,
)
from .processes import _children_index, _process_age, table_is_complete
from .providers import _parse_shpool_payload
from .providers_claude import _is_native_claude
from .providers_codex import (
    _codex_state_databases,
    _codex_turn_state,
    _is_native_codex,
)


PROVIDER_ORDER = {"claude": 0, "codex": 1, "shell": 2, "unknown": 3}
AVAILABILITY_ORDER = {"ready": 0, "attached": 1}
AGED_CHILD_SECONDS = 60 * 60


def _aged_shell_children(
    root_pid: int | None,
    tree: Sequence[int],
    process_table: Mapping[int, Mapping[str, Any]],
    current_time: float,
    ignored: frozenset[int],
) -> list[dict[str, Any]]:
    """Known child shells at least an hour old, from this census only.

    The shpool root is the session shell and is intentionally excluded.  A
    churned census may have unrelated holes, but every entry returned here is
    positive evidence already present in the session's proven process tree.
    """
    if not root_pid:
        return []
    result: list[dict[str, Any]] = []
    for pid in tree:
        if pid == root_pid or pid in ignored:
            continue
        process = process_table.get(pid, {})
        comm = clean_text(process.get("comm"), 80)
        if comm.casefold() not in SHELL_PROCESS_NAMES:
            continue
        age = _process_age(pid, process_table, current_time)
        if age is None or age < AGED_CHILD_SECONDS:
            continue
        result.append(
            {
                "kind": "shell",
                "title": comm,
                "age_seconds": age,
            }
        )
    return result


def _safe_worker_title(child: Mapping[str, Any], pid: int | None) -> str:
    provider = clean_text(child.get("provider"), 20).casefold()
    uuid = valid_uuid(child.get("uuid"))
    fallbacks = {
        fallback
        for fallback in (
            f"PID {pid}" if pid is not None else "",
            uuid,
            uuid[:8] if uuid else "",
        )
        if fallback
    }
    title = clean_text(child.get("title"), 120)
    if not title or title in fallbacks:
        return f"{provider.title()} worker" if provider in PROVIDERS else "Worker"
    return title


def _attach_aged_workers(
    rows: Sequence[dict[str, Any]],
    process_table: Mapping[int, Mapping[str, Any]],
    current_time: float,
) -> None:
    """Add hour-old workers without changing subagent count or state.

    A Codex spawn edge may be pid-less while the exact child also has its own
    live session row.  That exact provider+UUID join may reuse the row's
    process age; an edge with no matching live process remains ageless.
    """
    live_by_identity: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        identity = row.get("identity")
        if not isinstance(identity, Mapping):
            continue
        provider = clean_text(row.get("provider"), 20).casefold()
        uuid = valid_uuid(identity.get("uuid"))
        if provider in PROVIDERS and uuid:
            live_by_identity[(provider, uuid)] = row

    for row in rows:
        existing = row.get("aged_children")
        aged = (
            [dict(item) for item in existing if isinstance(item, Mapping)]
            if isinstance(existing, Sequence) and not isinstance(existing, (str, bytes))
            else []
        )
        seen: set[tuple[str, object]] = set()
        subagents = row.get("subagents")
        if isinstance(subagents, Sequence) and not isinstance(subagents, (str, bytes)):
            for raw_child in subagents:
                if not isinstance(raw_child, Mapping):
                    continue
                raw_pid = raw_child.get("pid")
                pid = (
                    raw_pid
                    if isinstance(raw_pid, int)
                    and not isinstance(raw_pid, bool)
                    and raw_pid > 0
                    else None
                )
                provider = clean_text(raw_child.get("provider"), 20).casefold()
                uuid = valid_uuid(raw_child.get("uuid"))
                age = _process_age(pid, process_table, current_time) if pid else None
                if age is None and provider in PROVIDERS and uuid:
                    matched = live_by_identity.get((provider, uuid))
                    matched_age = (
                        matched.get("process_age_seconds") if matched else None
                    )
                    if (
                        isinstance(matched_age, int)
                        and not isinstance(matched_age, bool)
                        and matched_age >= 0
                    ):
                        age = matched_age
                if age is None or age < AGED_CHILD_SECONDS:
                    continue
                key: tuple[str, object]
                if pid is not None:
                    key = ("pid", pid)
                elif provider in PROVIDERS and uuid:
                    key = (provider, uuid)
                else:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                aged.append(
                    {
                        "kind": "worker",
                        "provider": provider if provider in PROVIDERS else "",
                        "title": _safe_worker_title(raw_child, pid),
                        "age_seconds": age,
                    }
                )
        aged.sort(
            key=lambda item: (
                -int(item.get("age_seconds") or 0),
                str(item.get("kind") or ""),
                str(item.get("title") or "").casefold(),
            )
        )
        row["aged_children"] = aged


# Value-taking global options printed by the installed CLI's `codex help`.
# Keep this synchronized with that grammar so option values cannot be mistaken
# for the first positional (the subcommand, when one is present).
CODEX_GLOBAL_OPTIONS_WITH_VALUES = frozenset(
    {
        "-c",
        "--config",
        "--enable",
        "--disable",
        "--remote",
        "--remote-auth-token-env",
        "-i",
        "--image",
        "-m",
        "--model",
        "--local-provider",
        "-p",
        "--profile",
        "-s",
        "--sandbox",
        "-C",
        "--cd",
        "--add-dir",
        "-a",
        "--ask-for-approval",
    }
)
CODEX_GLOBAL_FLAG_OPTIONS = frozenset(
    {
        "--strict-config",
        "--oss",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--search",
        "--no-alt-screen",
        "-h",
        "--help",
        "-V",
        "--version",
    }
)
CODEX_VARIADIC_GLOBAL_OPTIONS = frozenset({"-i", "--image"})
# Subcommands that mean no person converses with this Codex: a headless run,
# a non-interactive review, or the stdio MCP server. From the installed CLI's
# own `codex help` (0.145). Anything not PROVEN machine stays out -- misreading
# a person's session as machine would hide it, which is the worse direction --
# so management/utility verbs (login, mcp, remote-control, sandbox, resume,
# ...) are deliberately absent. ``app-server`` is deliberately absent too: it
# is the kit's OWN plumbing for every managed Codex session, human windows
# included (X17 -- listing it here hid two real sessions). Whether an
# app-server session is machine-driven is a question about its DRIVER, not
# its argv, and needs driver evidence, not this list.
CODEX_MACHINE_SUBCOMMANDS = frozenset({"exec", "review", "mcp-server"})
# The npm wrapper starts Codex as `node .../bin/codex ...`: argv[0] is node
# and the real grammar begins at argv[1]. Resolve only that exact, confident
# shape -- anything else keeps the wrapper-blind reading and errs visible.
_CODEX_WRAPPER_INTERPRETERS = frozenset({"node", "nodejs"})
# The App Server is a socket, and the question X16 asked about it is who holds
# that socket. The kit starts one for EVERY managed Codex session, so its argv
# separates nothing (X17: reading `app-server` as machine hid two of the operator's own
# windows). The client on the other end does separate them: a person's window
# is a Codex TUI attached to that exact socket with `--remote`, and a worker
# nobody types into has only the coordination broker the kit starts with
# `--socket <same path>`. Both are positive readings. Everything else -- no
# client, an argv that will not parse, a server younger than the window that
# follows it -- is not evidence, and an absence of evidence keeps the row in
# the person's list.
CODEX_APP_SERVER_SUBCOMMAND = "app-server"
# The managed login shell starts the server, the broker, and then the window,
# in that order and within the same second. Under this age "no window yet" is
# a session still booting, not a verdict about who drives it.
CODEX_APP_SERVER_WINDOW_GRACE_SECONDS = 60


def _codex_effective_argv(exact_argv: Sequence[Any]) -> Sequence[Any]:
    if (
        len(exact_argv) >= 2
        and isinstance(exact_argv[0], str)
        and isinstance(exact_argv[1], str)
        and Path(exact_argv[0]).name in _CODEX_WRAPPER_INTERPRETERS
        and Path(exact_argv[1]).name == "codex"
    ):
        return exact_argv[1:]
    return exact_argv


def _codex_first_positional(exact_argv: Sequence[Any]) -> str | None:
    """Return Codex's first confidently parsed positional, if there is one.

    Unknown separate-form options and malformed argv are deliberately
    ambiguous.  Callers must keep those sessions visible rather than risk
    hiding an interactive root.
    """
    exact_argv = _codex_effective_argv(exact_argv)
    index = 1
    while index < len(exact_argv):
        argument = exact_argv[index]
        if not isinstance(argument, str) or not argument:
            return None
        if not argument.startswith("-"):
            return argument
        if "=" in argument:
            index += 1
            continue
        if argument in CODEX_GLOBAL_OPTIONS_WITH_VALUES:
            if index + 1 >= len(exact_argv):
                return None
            value = exact_argv[index + 1]
            if not isinstance(value, str) or not value or value.startswith("-"):
                return None
            index += 2
            if argument in CODEX_VARIADIC_GLOBAL_OPTIONS:
                while index < len(exact_argv):
                    value = exact_argv[index]
                    if not isinstance(value, str) or not value:
                        return None
                    if value.startswith("-"):
                        break
                    index += 1
            continue
        if argument in CODEX_GLOBAL_FLAG_OPTIONS:
            index += 1
            continue
        return None
    return None


def _unix_endpoint_path(value: Any) -> str:
    """The filesystem path an endpoint names, with or without the scheme.

    The server is told `unix:///run/x/app.sock` and the broker is told the
    bare path, so the two only compare after the scheme comes off.
    """
    if not isinstance(value, str) or not value:
        return ""
    return value[len("unix://") :] if value.startswith("unix://") else value


def _option_value(argv: Sequence[Any], option: str) -> str:
    """Read one option's value in either the separate or the joined form."""
    joined = f"{option}="
    for index, argument in enumerate(argv):
        if not isinstance(argument, str):
            continue
        if argument == option:
            value = argv[index + 1] if index + 1 < len(argv) else ""
            return value if isinstance(value, str) else ""
        if argument.startswith(joined):
            return argument[len(joined) :]
    return ""


def _codex_app_server_driver(
    provider_pid: Any,
    exact_argv: Sequence[Any],
    tree: Iterable[int],
    process_table: Mapping[int, Mapping[str, Any]],
    server_age_seconds: Any,
) -> str:
    """Who holds this Codex App Server's socket: a person, a program, nobody.

    Answers "person", "program", or "unknown", reading only the managed
    shell's own process tree.  Only "program" is a machine verdict; every
    uncertain reading answers "unknown" and leaves the row visible, which is
    the direction X17 proved is the safe one to be wrong in.

    "Program" is reached by NOT finding a window, so it is only ever as good
    as the reading of the tree. A process whose argv the scan could not take
    -- denied, or gone by the time it was asked -- may BE the window, and a
    broker beside a missed window is observationally identical to a broker
    with no window at all. So a tree with a hole in it proves nothing about
    absence and answers "unknown". Finding the window still answers "person":
    a positive reading is not weakened by a hole elsewhere.
    """
    if _codex_first_positional(exact_argv) != CODEX_APP_SERVER_SUBCOMMAND:
        return "unknown"
    socket = _unix_endpoint_path(
        _option_value(_codex_effective_argv(exact_argv), "--listen")
    )
    if not socket:
        return "unknown"
    program = False
    complete = True
    for pid in tree:
        if pid == provider_pid:
            continue
        process = process_table.get(pid)
        argv = process.get("cmdline") if isinstance(process, Mapping) else None
        if (
            not isinstance(process, Mapping)
            or process.get("argv_unreadable") is True
            or not isinstance(argv, Sequence)
            or isinstance(argv, (str, bytes))
        ):
            complete = False
            continue
        argv = list(argv)
        # A window attached to this exact server is a person, whether the npm
        # wrapper or the vendored binary is the one carrying the argument.
        if _unix_endpoint_path(_option_value(argv, "--remote")) == socket:
            return "person"
        if _unix_endpoint_path(_option_value(argv, "--socket")) == socket:
            program = True
    if not program:
        # A server with no client at all has not been driven by anybody yet.
        return "unknown"
    if not complete or not table_is_complete(process_table):
        # Something went unread. The window is the thing whose absence is
        # being claimed, so one unread process is enough to withdraw the
        # claim -- and a process the scan lost before it could read a parent
        # is not even attributable to a tree, so a hole anywhere on the
        # machine could be the window that belongs to THIS one.
        return "unknown"
    if (
        isinstance(server_age_seconds, bool)
        or not isinstance(server_age_seconds, int)
        or server_age_seconds < CODEX_APP_SERVER_WINDOW_GRACE_SECONDS
    ):
        return "unknown"
    return "program"


def _provider_profile_path(raw: Any, fallback: Path) -> Path | None:
    """Return one absolute provider profile path without inventing a target."""
    if not isinstance(raw, str) or not raw:
        return fallback
    candidate = Path(raw)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return None
    return candidate


def _profile_key(path: Path) -> str:
    return os.fspath(path.resolve(strict=False))


def _exact_account_alias(
    pid: Any,
    identity: Mapping[str, Any],
    process_table: Mapping[int, Mapping[str, Any]],
) -> str:
    """Read a display alias only from the exact provider process generation."""
    if (
        not isinstance(pid, int)
        or identity.get("confidence") != "exact"
        or identity.get("pid") != pid
    ):
        return ""
    alias = clean_text(process_table.get(pid, {}).get("account_alias"), 12)
    return alias if re.fullmatch(r"[a-z][a-z0-9_-]{0,11}", alias) else ""


def _exact_account_capable(
    pid: Any,
    identity: Mapping[str, Any],
    process_table: Mapping[int, Mapping[str, Any]],
) -> bool:
    return bool(
        isinstance(pid, int)
        and identity.get("confidence") == "exact"
        and identity.get("pid") == pid
        and process_table.get(pid, {}).get("account_capable") == "1"
    )


def regular_file_mtime_ms(path: Path) -> int | None:
    """Return an exact regular file's mtime without following a symlink."""
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        return None
    return metadata.st_mtime_ns // 1_000_000


def recent_output_times(
    shpool_ids: Iterable[str],
    *,
    journal_dir: Path | None = None,
    recovery_dir: Path | None = None,
    journal_dir_factory: Callable[[], Path],
    journal_recovery_dir_factory: Callable[[], Path],
    regular_file_mtime_ms: Callable[[Path], int | None],
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
    active_root = journal_dir or journal_dir_factory()
    recovered_root = recovery_dir or journal_recovery_dir_factory()
    mapped: dict[str, Path | None] = {}
    ambiguous_mappings: set[str] = set()
    map_path = recovered_root / "current-map.tsv"
    try:
        map_metadata = map_path.lstat()
        if not stat.S_ISREG(map_metadata.st_mode):
            raise OSError("recovery map is not a regular file")
        with map_path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                raw_id, separator, raw_path = raw_line.rstrip("\r\n").partition("\t")
                if (
                    not separator
                    or raw_id not in wanted
                    or not raw_path.startswith("/")
                ):
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
            mapped_mtime = regular_file_mtime_ms(mapped_path)
            if mapped_mtime is not None:
                result[raw_id] = mapped_mtime
                continue

        legacy_mtime = regular_file_mtime_ms(active_root / f"{raw_id}.raw")
        if legacy_mtime is not None:
            result[raw_id] = legacy_mtime
            continue

        segment_dir = active_root / raw_id
        try:
            directory_metadata = segment_dir.lstat()
            if not stat.S_ISDIR(directory_metadata.st_mode):
                continue
            segment_mtimes = [
                timestamp
                for path in segment_dir.iterdir()
                if re.fullmatch(r"segment-[0-9]+\.raw", path.name)
                for timestamp in [regular_file_mtime_ms(path)]
                if timestamp is not None
            ]
        except OSError:
            continue
        if segment_mtimes:
            result[raw_id] = max(segment_mtimes)
    return result


def apply_retained_setup_attributions(
    inventory: dict[str, Any],
    *,
    start_dir: Path | None = None,
    boot_id: str | None = None,
    start_dir_factory: Callable[[], Path],
    boot_id_reader: Callable[[], str | None],
    launch_fields: Callable[[bytes, int], list[str] | None],
    read_private_launch_file: Callable[[int, str], bytes],
) -> dict[str, Any]:
    """Add display-only provider hints from exact retained startup proofs."""
    root = start_dir or start_dir_factory()
    current_boot_id = boot_id if boot_id is not None else boot_id_reader()
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
            not stat.S_ISDIR(directory.st_mode)
            or stat.S_IMODE(directory.st_mode) != 0o700
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
            if not isinstance(raw_id, str) or shpool_id_mutation_policy(raw_id) != (
                True,
                None,
            ):
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
                start_payload = read_private_launch_file(directory_fd, raw_id)
                expected_payload = read_private_launch_file(
                    directory_fd, f"{raw_id}.expected"
                )
            except (CollectionError, OSError):
                continue
            start = launch_fields(start_payload, 4)
            if start is None:
                legacy_start = launch_fields(start_payload, 3)
                if legacy_start is not None:
                    legacy_uuid = legacy_start[2]
                    start = [
                        *legacy_start,
                        "resume" if legacy_uuid else "new",
                    ]
            expected = launch_fields(expected_payload, 10)
            if expected is None:
                legacy_expected = launch_fields(expected_payload, 9)
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
                or (launch_mode in {"resume", "fork"} and valid_uuid(uuid) != uuid)
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
            item["display_title"] = f"{provider.title()} pending"
            if uuid and launch_mode == "resume":
                item["_terminal_identity_hint"] = {
                    "provider": provider,
                    "uuid": uuid,
                }
    finally:
        os.close(directory_fd)
    return inventory


def _census_chain(
    process_table: Mapping[int, Mapping[str, Any]],
    children: Mapping[int, Sequence[int]] | None = None,
) -> frozenset[int]:
    """This process, what spawned it, and what it spawned.

    The census can run inside the very shell it is titling; its own chain
    must never count as that session's running program. Its own children are
    the same thing one step further out: a collection verb that shells out to
    a helper would otherwise report that helper as the session's work for as
    long as it runs."""
    chain: set[int] = set()
    pid = os.getpid()
    for _ in range(64):
        if pid in chain or pid <= 1:
            break
        chain.add(pid)
        parent = process_table.get(pid, {}).get("ppid")
        if not isinstance(parent, int):
            break
        pid = parent
    if children is not None:
        pending = [os.getpid()]
        while pending:
            current = pending.pop()
            for child in children.get(current, ()):  # type: ignore[arg-type]
                if child not in chain:
                    chain.add(child)
                    pending.append(child)
    return frozenset(chain)


ProviderClaim = tuple[str, int, str]


def _proven_ancestor(
    ancestor_pid: int,
    descendant_pid: int,
    process_table: Mapping[int, Mapping[str, Any]],
) -> bool:
    """Prove one same-snapshot process lineage without trusting a bare PID.

    Every link must carry a readable generation, and a parent cannot have
    started after its alleged child.  That last check closes the snapshot race
    where an exited parent PID is recycled after the child row was read.
    """
    current_pid = descendant_pid
    seen: set[int] = set()
    for _ in range(128):
        if current_pid in seen or current_pid <= 0:
            return False
        current = process_table.get(current_pid)
        if not isinstance(current, Mapping):
            return False
        recorded_pid = current.get("pid", current_pid)
        current_start = current.get("start_ticks")
        if (
            isinstance(recorded_pid, bool)
            or recorded_pid != current_pid
            or isinstance(current_start, bool)
            or not isinstance(current_start, int)
            or current_start <= 0
        ):
            return False
        if current_pid == ancestor_pid:
            return True
        if current.get("argv_unreadable") is True:
            return False
        parent_pid = current.get("ppid")
        if (
            isinstance(parent_pid, bool)
            or not isinstance(parent_pid, int)
            or parent_pid <= 0
        ):
            return False
        parent = process_table.get(parent_pid)
        if not isinstance(parent, Mapping):
            return False
        parent_recorded_pid = parent.get("pid", parent_pid)
        parent_start = parent.get("start_ticks")
        if (
            isinstance(parent_recorded_pid, bool)
            or parent_recorded_pid != parent_pid
            or isinstance(parent_start, bool)
            or not isinstance(parent_start, int)
            or parent_start <= 0
            or parent_start > current_start
            or parent.get("argv_unreadable") is True
        ):
            return False
        seen.add(current_pid)
        current_pid = parent_pid
    return False


def _lineage_owner(
    root_pid: int | None,
    claims: Sequence[ProviderClaim],
    process_table: Mapping[int, Mapping[str, Any]],
) -> tuple[ProviderClaim, list[ProviderClaim]] | None:
    """Choose an exact owner only when all rival-looking claims are workers."""
    if not isinstance(root_pid, int) or not claims:
        return None
    if len(claims) == 1:
        # A sole claim inside this session's own subtree is the answer, not an
        # adjudication among rivals. Requiring a hole-free whole-machine census
        # before returning it let any unrelated process dying mid-scan
        # unresolve every session on the board at once.
        return claims[0], []
    if not table_is_complete(process_table):
        return None
    if any(
        not _proven_ancestor(root_pid, candidate_pid, process_table)
        for _, candidate_pid, _ in claims
    ):
        return None
    roots = [
        claim
        for claim in claims
        if not any(
            other_pid != claim[1]
            and _proven_ancestor(other_pid, claim[1], process_table)
            for _, other_pid, _ in claims
        )
    ]
    if len(roots) != 1:
        return None
    owner = roots[0]
    workers = [claim for claim in claims if claim != owner]
    if not all(
        worker_pid != owner[1] and _proven_ancestor(owner[1], worker_pid, process_table)
        for _, worker_pid, _ in workers
    ):
        return None
    return owner, workers


def build_inventory(
    shpool_payload: Any,
    claude_payload: Any,
    process_table: Mapping[int, Mapping[str, Any]],
    codex_index: Mapping[int, Sequence[Mapping[str, Any]]],
    codex_db_rows: Sequence[Any],
    config: Mapping[str, Any],
    now: float | None = None,
    recent_output_by_shpool_id: Mapping[str, int] | None = None,
    *,
    descendants: Callable[..., list[int]],
    shpool_roots: Callable[
        [Iterable[str], Mapping[int, Mapping[str, Any]]],
        tuple[dict[str, int], dict[str, list[str]]],
    ],
    daemon_generation: Callable[
        [Mapping[int, Mapping[str, Any]]], dict[str, int] | None
    ],
    parse_claude_payload: Callable[[Any], list[dict[str, Any]]],
    native_claude_uuid: Callable[[Mapping[str, Any]], str | None],
    claude_subagents: Callable[
        [str, Mapping[int, Mapping[str, Any]]], list[dict[str, Any]]
    ],
    root_codex_uuid: Callable[
        [Mapping[str, Any], Sequence[Mapping[str, Any]]], list[str]
    ],
    provider_title_info: Callable[..., tuple[str, str]],
    session_color: Callable[..., str | None],
    valid_colors: Callable[[Any], dict[str, str]],
    adopt_launch_colors: Callable[..., Any],
    codex_title_echoes_prompt: Callable[[str, str], bool],
    schema_version: int,
    claude_palette: Sequence[str],
    default_max_proc_nodes: int,
) -> dict[str, Any]:
    """Pure inventory composition for fixture tests and the live collector."""
    current_time = time.time() if now is None else now
    shpool_sessions = _parse_shpool_payload(shpool_payload)
    claude_agents = parse_claude_payload(claude_payload)
    claude_by_pid = {item["pid"]: item for item in claude_agents}
    aliases = _valid_aliases(config.get("aliases"))
    automatic_titles = _valid_automatic_titles(config.get("automatic_titles"))
    pushed_titles = _valid_pushed_titles(config.get("pushed_titles"))
    pending_native_titles = _valid_pending_native_titles(
        config.get("pending_native_titles")
    )
    name_ownership = _valid_name_ownership(config.get("name_ownership"))
    automatic_failures = _valid_automatic_title_failures(
        config.get("automatic_title_failures")
    )
    if len(codex_db_rows) < 2:
        raise CollectionError("Codex metadata bundle is incomplete")
    codex_threads, codex_edges = codex_db_rows[:2]
    codex_profiles: Mapping[str, Any] = (
        codex_db_rows[2]
        if len(codex_db_rows) >= 3 and isinstance(codex_db_rows[2], Mapping)
        else {}
    )
    codex_default_home = codex_profiles.get("default_home")
    codex_profile_rows = codex_profiles.get("profiles")
    if not isinstance(codex_default_home, str) or not isinstance(
        codex_profile_rows, Mapping
    ):
        codex_default_home = ""
        codex_profile_rows = {}

    codex_parent_by_child: dict[str, set[str]] = {}

    def remember_codex_edges(edges: Mapping[str, Any]) -> None:
        for raw_parent, raw_children in edges.items():
            parent = valid_uuid(raw_parent)
            if not parent or not isinstance(raw_children, Sequence):
                continue
            for child in raw_children:
                if not isinstance(child, Mapping):
                    continue
                child_uuid = valid_uuid(child.get("uuid"))
                if child_uuid:
                    codex_parent_by_child.setdefault(child_uuid, set()).add(parent)

    remember_codex_edges(codex_edges)
    for profile_rows in codex_profile_rows.values():
        if (
            isinstance(profile_rows, Sequence)
            and not isinstance(profile_rows, (str, bytes))
            and len(profile_rows) >= 2
            and isinstance(profile_rows[1], Mapping)
        ):
            remember_codex_edges(profile_rows[1])

    def codex_rows_for_pid(
        candidate_pid: int,
    ) -> tuple[Mapping[str, Mapping[str, Any]], Mapping[str, Sequence[dict[str, Any]]]]:
        if not codex_default_home:
            return codex_threads, codex_edges
        default_path = Path(codex_default_home)
        profile = _provider_profile_path(
            process_table.get(candidate_pid, {}).get("codex_home"), default_path
        )
        if profile is None:
            return {}, {}
        selected = codex_profile_rows.get(_profile_key(profile))
        if (
            isinstance(selected, Sequence)
            and not isinstance(selected, (str, bytes))
            and len(selected) >= 2
            and isinstance(selected[0], Mapping)
            and isinstance(selected[1], Mapping)
        ):
            return selected[0], selected[1]
        return {}, {}

    roots, root_diagnostics = shpool_roots(
        (item["name"] for item in shpool_sessions), process_table
    )
    child_index = _children_index(process_table)
    own_processes = _census_chain(process_table, child_index)
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
                    max_nodes=int(config.get("max_proc_nodes", default_max_proc_nodes)),
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
        native_claude_candidates = [
            (pid, native_uuid)
            for pid in tree
            if pid not in claude_by_pid
            and (native_uuid := native_claude_uuid(process_table.get(pid, {})))
        ]
        codex_candidates: list[tuple[int, str]] = []
        for candidate_pid in tree:
            for candidate_uuid in root_codex_uuid(
                process_table.get(candidate_pid, {}),
                codex_index.get(candidate_pid, ()),
            ):
                codex_candidates.append((candidate_pid, candidate_uuid))
        codex_candidates = sorted(set(codex_candidates))
        provider_claims: list[ProviderClaim] = [
            ("claude-agent", candidate_pid, claude_by_pid[candidate_pid]["uuid"])
            for candidate_pid in claude_candidates
        ]
        provider_claims.extend(
            ("claude-native", candidate_pid, candidate_uuid)
            for candidate_pid, candidate_uuid in native_claude_candidates
        )
        provider_claims.extend(
            ("codex", candidate_pid, candidate_uuid)
            for candidate_pid, candidate_uuid in codex_candidates
        )
        lineage = _lineage_owner(root_pid, provider_claims, process_table)
        owner_claim = lineage[0] if lineage else None
        worker_claims = lineage[1] if lineage else []

        lineage_subagents: list[dict[str, Any]] = []
        for worker_kind, worker_pid, worker_uuid in worker_claims:
            if worker_kind == "codex":
                worker_threads, _ = codex_rows_for_pid(worker_pid)
                worker_thread = worker_threads.get(worker_uuid, {})
                worker_title = (
                    clean_text(
                        worker_thread.get("session_index_name")
                        or worker_thread.get("name")
                        or worker_thread.get("title"),
                        120,
                    )
                    or f"PID {worker_pid}"
                )
                worker_status = _codex_turn_state(
                    codex_index.get(worker_pid, ()), worker_uuid
                )
                mapped_codex.add(worker_pid)
            elif worker_kind == "claude-agent":
                worker_agent = claude_by_pid[worker_pid]
                worker_title = (
                    clean_text(
                        worker_agent.get("agent_name") or worker_agent.get("title"),
                        120,
                    )
                    or f"PID {worker_pid}"
                )
                worker_status = clean_text(worker_agent.get("status"), 60) or "unknown"
                mapped_claude.add(worker_pid)
            else:
                worker_title = "Claude worker"
                worker_status = "running"
            lineage_subagents.append(
                {
                    "provider": ("codex" if worker_kind == "codex" else "claude"),
                    "uuid": worker_uuid,
                    "pid": worker_pid,
                    "title": worker_title,
                    "status": worker_status,
                }
            )
        lineage_subagents.sort(
            key=lambda item: (
                str(item["provider"]),
                str(item["title"]).casefold(),
                int(item["pid"]),
            )
        )

        def include_lineage_subagents(
            existing: Sequence[Any],
        ) -> list[dict[str, Any]]:
            combined = [dict(item) for item in existing if isinstance(item, Mapping)]
            for worker in lineage_subagents:
                if any(
                    item.get("pid") == worker["pid"]
                    or (
                        item.get("provider") == worker["provider"]
                        and item.get("uuid") == worker["uuid"]
                    )
                    for item in combined
                ):
                    continue
                combined.append(dict(worker))
            return combined

        claude_parent_ids: set[str] = set()
        claude_parent_evidence = False

        pid: int | None
        uuid: str | None
        starting_provider = None
        provider_title_is_explicit = True
        native_name_source = ""
        native_name_since: Any = None
        provider_title_state = "ready"
        claude_color_evidence = ""
        if owner_claim and owner_claim[0] == "claude-agent":
            pid = owner_claim[1]
            agent = claude_by_pid[pid]
            uuid = agent["uuid"]
            native_title = agent["title"]
            native_name_source = agent.get("name_source") or ""
            native_name_since = agent.get("name_since")
            claude_color_evidence = agent.get("agent_color") or ""
            # Claude persists its conversation auto-title as a transcript
            # ai-title record while the session record keeps a derived window
            # label. The visible conversation title outranks the derived
            # label; any explicit rename keeps outranking both.
            if agent.get("agent_name"):
                native_title = agent["agent_name"]
                native_name_source = ""
            elif agent.get("name_source") == "derived":
                if agent.get("ai_title"):
                    native_title = agent["ai_title"]
                # Transcript hydration prepares the next start, but only the
                # live structured agent name proves this process rendered it.
                # External writes cannot repaint an already-running TUI.
                if agent.get("title") != native_title:
                    provider_title_state = "pending"
            provider = "claude"
            cwd = agent["cwd"] or clean_text(
                process_table.get(pid, {}).get("cwd"), 4096
            )
            identity = _agent_identity(
                provider=provider,
                uuid=uuid,
                pid=pid,
                process_table=process_table,
                provenance="claude agents --json",
                confidence="exact",
            )
            subagents = include_lineage_subagents(claude_subagents(uuid, process_table))
            recovery = recovery_spec(provider, uuid, cwd or None)
            mapped_claude.add(pid)
            agent_status = agent["status"]
            needs_you = agent["needs_you"]
        elif owner_claim and owner_claim[0] == "claude-native":
            _, pid, uuid = owner_claim
            provider = "claude"
            native_title = "Claude started"
            cwd = clean_text(process_table.get(pid, {}).get("cwd"), 4096)
            identity = _agent_identity(
                provider=provider,
                uuid=uuid,
                pid=pid,
                process_table=process_table,
                provenance="native Claude CLI session argument",
                confidence="exact",
            )
            subagents = include_lineage_subagents(claude_subagents(uuid, process_table))
            recovery = recovery_spec(provider, uuid, cwd or None)
            mapped_claude.add(pid)
            agent_status = "running"
            needs_you = False
            provider_title_is_explicit = False
        elif owner_claim and owner_claim[0] == "codex":
            _, pid, uuid = owner_claim
            profile_threads, profile_edges = codex_rows_for_pid(pid)
            thread = profile_threads.get(uuid, {})
            native_title = clean_text(
                thread.get("session_index_name")
                or thread.get("name")
                or thread.get("title"),
                120,
            )
            # The session-index name is exact rename evidence. A database
            # title also counts as a real name on current Codex, which
            # auto-titles threads there, recognizable by the schema carrying
            # first_user_message separately. Older stores kept the raw prompt
            # in the title, and a title that still echoes the first prompt is
            # that same behavior on any schema.
            db_title = clean_text(thread.get("title"), 120)
            first_message = clean_text(thread.get("first_user_message"), 200)
            has_split_prompt_schema = "first_user_message" in thread
            title_echoes_prompt = codex_title_echoes_prompt(db_title, first_message)
            provider_title_is_explicit = bool(thread.get("session_index_name")) or (
                has_split_prompt_schema and bool(db_title) and not title_echoes_prompt
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
                provenance="native Codex PID open exact rollout",
                confidence="exact",
            )
            subagents = include_lineage_subagents(profile_edges.get(uuid, ()))
            recovery = recovery_spec(provider, uuid, cwd or None)
            mapped_codex.add(pid)
            turn_state = _codex_turn_state(codex_index.get(pid, ()), uuid)
            agent_status = turn_state
            needs_you = turn_state == "needs your reply"
        else:
            known_provider_process = any(
                _is_native_claude(process_table.get(pid, {}))
                or _is_native_codex(process_table.get(pid, {}))
                for pid in tree
            )
            claude_count = len(claude_candidates) + len(native_claude_candidates)
            ambiguous = (
                claude_count > 1
                or len(codex_candidates) > 1
                or (bool(claude_count) and bool(codex_candidates))
            )
            if ambiguous or known_provider_process or not root_pid:
                provider = "unknown"
                pid = root_pid
                native_title = "Unresolved provider session"
                cwd = clean_text(process_table.get(root_pid or -1, {}).get("cwd"), 4096)
                provider_cwds = {
                    candidate_cwd
                    for candidate in tree
                    if (
                        _is_native_claude(process_table.get(candidate, {}))
                        or _is_native_codex(process_table.get(candidate, {}))
                    )
                    and (
                        candidate_cwd := clean_text(
                            process_table.get(candidate, {}).get("cwd"), 4096
                        )
                    ).startswith("/")
                }
                if not cwd and len(provider_cwds) == 1:
                    cwd = next(iter(provider_cwds))
                diagnostics.append(
                    f"identity candidates: Claude={claude_count}, Codex={len(codex_candidates)}"
                )
                agent_status = "unknown"
                # A new Codex session has no rollout until its first message, so
                # it cannot be identified, but calling it "unresolved" reads as
                # broken when it is simply unused. Name what is actually running
                # so the row is recognisable. Identity stays unknown: this is a
                # label, and it grants nothing.
                if not ambiguous:
                    running = {
                        "codex"
                        if _is_native_codex(process_table.get(candidate, {}))
                        else "claude"
                        if _is_native_claude(process_table.get(candidate, {}))
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
                native_title, cwd, pid = _shell_title(
                    tree, root_pid, process_table, own_processes
                )
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

        exact_argv = process_table.get(pid or -1, {}).get("cmdline")
        exact_argv = (
            list(exact_argv)
            if isinstance(exact_argv, Sequence)
            and not isinstance(exact_argv, (str, bytes))
            else []
        )
        for index, argument in enumerate(exact_argv):
            if argument == "--parent-session-id":
                claude_parent_evidence = True
                raw_parent = (
                    exact_argv[index + 1] if index + 1 < len(exact_argv) else ""
                )
            elif isinstance(argument, str) and argument.startswith(
                "--parent-session-id="
            ):
                claude_parent_evidence = True
                raw_parent = argument.partition("=")[2]
            else:
                continue
            parent_uuid = valid_uuid(raw_parent)
            if parent_uuid:
                claude_parent_ids.add(parent_uuid)
        headless_codex_machine = (
            provider == "codex"
            and _codex_first_positional(exact_argv) in CODEX_MACHINE_SUBCOMMANDS
        )
        codex_app_server_driver = (
            _codex_app_server_driver(
                pid,
                exact_argv,
                tree,
                process_table,
                _process_age(pid, process_table, current_time),
            )
            if provider == "codex"
            else "unknown"
        )

        title_key = f"{provider}:{uuid}" if uuid else ""
        if (
            provider == "claude"
            and title_key
            and native_title
            and _pending_native_title_matches(
                pending_native_titles.get(title_key),
                native_title,
                native_name_since,
                native_name_source,
            )
            and pushed_titles.get(title_key)
        ):
            provider_title_state = "pending"

        title, title_source = provider_title_info(
            provider,
            uuid,
            native_title,
            aliases,
            cwd,
            shpool["started_at_unix_ms"],
            automatic_titles,
            provider_title_is_explicit=provider_title_is_explicit,
            native_name_source=native_name_source,
            native_name_since=native_name_since,
            pushed_titles=pushed_titles,
            pending_native_titles=pending_native_titles,
            name_ownership=name_ownership,
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
                process_age_seconds=_process_age(
                    identity.get("pid"), process_table, current_time
                ),
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
        sessions[-1]["aged_children"] = _aged_shell_children(
            root_pid,
            tree,
            process_table,
            current_time,
            own_processes,
        )
        account_alias = _exact_account_alias(pid, identity, process_table)
        if account_alias:
            sessions[-1]["account_alias"] = account_alias
        sessions[-1]["account_switch_capable"] = _exact_account_capable(
            pid, identity, process_table
        )
        codex_parents = codex_parent_by_child.get(uuid or "", set())
        if claude_parent_evidence:
            sessions[-1]["is_subagent"] = True
            if len(claude_parent_ids) == 1:
                sessions[-1]["parent_session"] = {
                    "provider": "claude",
                    "uuid": next(iter(claude_parent_ids)),
                    "provenance": "Claude --parent-session-id",
                }
        elif len(codex_parents) == 1:
            sessions[-1]["is_subagent"] = True
            sessions[-1]["parent_session"] = {
                "provider": "codex",
                "uuid": next(iter(codex_parents)),
                "provenance": "Codex thread_spawn_edges",
            }
        elif codex_parents:
            sessions[-1]["is_subagent"] = True
        elif headless_codex_machine:
            # ``codex exec``, ``codex review``, and the server modes
            # (``app-server``, ``mcp-server``) are machine-driven -- no person
            # converses with them. Their cross-provider caller is only
            # process-ancestry evidence; if no durable provider edge names the
            # parent, projection treats it as an orphan child behind the
            # machine count.
            sessions[-1]["is_subagent"] = True
        elif codex_app_server_driver == "program":
            # X16: a program holds this conversation and no window does, so it
            # is nobody's top-level row. It is marked machine rather than made
            # a child: nothing in the process tree names the DRIVING session,
            # and a child whose parent cannot be named is projected into the
            # orphan class, which no list and no count shows. Machine keeps it
            # counted, and one keystroke away.
            sessions[-1]["machine_driven"] = True
        if codex_app_server_driver == "person":
            # The other positive verdict, published because something has to
            # be able to say that a provider restart FINISHED. The shell that
            # relaunches a provider blocks on it and can never report that,
            # so the bounce marker it leaves behind is cleared here instead --
            # by a sighting of the window, not by a timer.
            sessions[-1]["app_server_window"] = True
        if claude_color_evidence:
            sessions[-1]["_claude_agent_color"] = claude_color_evidence
        if provider == "claude":
            sessions[-1]["provider_title_state"] = provider_title_state
            sessions[-1]["blocking_question"] = bool(
                agent.get("blocking_question")
                if owner_claim and owner_claim[0] == "claude-agent"
                else False
            )
        elif provider == "codex":
            # The rollout proves Codex needs a reply, but the app-server
            # protocol records the kit reads expose no separately proven
            # currently-open picker/approval marker. False `question` is the
            # dangerous direction, so Codex remains `needs you`.
            sessions[-1]["blocking_question"] = False
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
        elif provider == "claude" and provider_title_state == "pending":
            automatic_state = "pending"
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
        if (
            agent["pid"] in mapped_claude
            or agent["kind"] not in {"", "interactive"}
            or agent["pid"] not in process_table
        ):
            continue
        uuid = agent["uuid"]
        recovery = recovery_spec("claude", uuid, agent["cwd"] or None)
        outside_native_title = agent["title"]
        outside_name_source = agent.get("name_source") or ""
        if agent.get("agent_name"):
            outside_native_title = agent["agent_name"]
            outside_name_source = ""
        elif agent.get("ai_title") and outside_name_source == "derived":
            outside_native_title = agent["ai_title"]
        outside_title, outside_title_source = provider_title_info(
            "claude",
            uuid,
            outside_native_title,
            aliases,
            agent["cwd"],
            agent["started_at_unix_ms"],
            automatic_titles,
            native_name_source=outside_name_source,
            pushed_titles=pushed_titles,
            pending_native_titles=pending_native_titles,
            name_ownership=name_ownership,
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
                native_title=outside_native_title,
                cwd=agent["cwd"],
                started_at_unix_ms=agent["started_at_unix_ms"],
                process_age_seconds=_process_age(
                    agent["pid"], process_table, current_time
                ),
                agent_status=agent["status"],
                needs_you=agent["needs_you"],
                subagents=claude_subagents(uuid, process_table),
                recovery=recovery,
                diagnostics=["active provider root is outside shpool"],
                shpool_shell=None,
            )
        )
        account_alias = _exact_account_alias(
            agent["pid"], outside_agents[-1]["identity"], process_table
        )
        if account_alias:
            outside_agents[-1]["account_alias"] = account_alias
        outside_agents[-1]["account_switch_capable"] = _exact_account_capable(
            agent["pid"], outside_agents[-1]["identity"], process_table
        )
        if agent.get("agent_color"):
            outside_agents[-1]["_claude_agent_color"] = agent["agent_color"]
    for pid, metadata in codex_index.items():
        if pid in mapped_codex:
            continue
        identities = root_codex_uuid(process_table.get(pid, {}), metadata)
        if len(identities) != 1:
            continue
        uuid = identities[0]
        profile_threads, profile_edges = codex_rows_for_pid(pid)
        thread = profile_threads.get(uuid, {})
        native_title = clean_text(
            thread.get("session_index_name")
            or thread.get("name")
            or thread.get("title"),
            120,
        )
        cwd = clean_text(
            thread.get("cwd") or process_table.get(pid, {}).get("cwd"), 4096
        )
        outside_title, outside_title_source = provider_title_info(
            "codex",
            uuid,
            native_title,
            aliases,
            cwd,
            None,
            automatic_titles,
            provider_title_is_explicit=bool(thread.get("session_index_name")),
            pushed_titles=pushed_titles,
            name_ownership=name_ownership,
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
                    provenance="native Codex PID open exact rollout",
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
                subagents=list(profile_edges.get(uuid, ())),
                recovery=recovery_spec("codex", uuid, cwd or None),
                diagnostics=["active provider root is outside shpool"],
                shpool_shell=None,
            )
        )
        account_alias = _exact_account_alias(
            pid, outside_agents[-1]["identity"], process_table
        )
        if account_alias:
            outside_agents[-1]["account_alias"] = account_alias
        outside_agents[-1]["account_switch_capable"] = _exact_account_capable(
            pid, outside_agents[-1]["identity"], process_table
        )
    _attach_aged_workers(sessions + outside_agents, process_table, current_time)

    # A spawn edge is history, not liveness. A sub-agent entry with no pid is
    # only a recorded parent->child edge; once the child finished it kept
    # rendering as an open sub-agent for days (operator finding X20,
    # 2026-08-14: rows idle since the previous day, pid null). Completion
    # means closure: a pid-less entry whose thread is not alive anywhere in
    # this snapshot and whose observed turn state is "idle" -- the completed
    # state -- is dropped. Everything else stays: a pid is a real process, a
    # live session row is a worker process, "working" is a Codex child
    # executing inside the parent process (no pid of its own), and the
    # question states plus "state unavailable" err VISIBLE rather than hide
    # a pending reply or an unreadable rollout (the X16/X17 lesson).
    live_thread_uuids = {
        (item.get("identity") or {}).get("uuid") for item in sessions + outside_agents
    }
    live_thread_uuids.discard(None)
    for item in sessions + outside_agents:
        item["subagents"] = [
            child
            for child in item.get("subagents") or []
            if child.get("pid") is not None
            or (child.get("uuid") and child["uuid"] in live_thread_uuids)
            or str(child.get("status") or "").casefold() != "idle"
        ]
    sessions.sort(key=canonical_session_order_key)
    for row, item in enumerate(sessions, start=1):
        item["row"] = row
    outside_agents.sort(
        key=lambda item: (
            PROVIDER_ORDER.get(item["provider"], 9),
            item["title"].casefold(),
            item["identity"].get("uuid") or "",
        )
    )
    color_overrides = valid_colors(config.get("colors"))
    color_overrides = adopt_launch_colors(config, sessions, color_overrides)
    for item in sessions + outside_agents:
        # A Claude session's transcript agent-color is what the session
        # actually renders in its own prompt chip; it must beat the identity
        # hash or the list shows a different color than the session itself
        # (a stored pink rendered yellow, proven live 2026-08-02). Explicit
        # config overrides still win over both.
        raw_uuid = (item.get("identity") or {}).get("uuid")
        display_uuid = valid_uuid(raw_uuid)
        item_provider = item.get("provider")
        provider = item_provider if isinstance(item_provider, str) else "unknown"
        evidence = item.get("_claude_agent_color")
        if (
            item.get("provider") == "claude"
            and display_uuid
            and evidence in claude_palette
            and f"claude:{display_uuid}" not in color_overrides
        ):
            item["display_color"] = evidence
        else:
            item["display_color"] = session_color(
                provider,
                display_uuid,
                color_overrides,
            )
        item.pop("_claude_agent_color", None)
    return {
        "schema_version": schema_version,
        "generated_at": _utc_now(current_time),
        "source": "live",
        "stale": False,
        "warnings": [],
        "daemon_generation": daemon_generation(process_table),
        "sessions": sessions,
        "outside_agents": outside_agents,
    }


def collect_live(
    config: Mapping[str, Any],
    *,
    runner: Callable[[Sequence[str], float], str] | None = None,
    proc_root: Path | None = None,
    environ: Mapping[str, str],
    default_runner: Callable[[Sequence[str], float], str],
    command_json: Callable[..., Any],
    shpool_executable: Callable[[], str],
    platform_process_table: Callable[[Path, int], dict[int, dict[str, Any]]],
    default_max_proc_nodes: int,
    enrich_claude_payload: Callable[[Any], Any],
    parse_claude_payload: Callable[[Any], list[dict[str, Any]]],
    observe_claude_name: Callable[..., str],
    codex_paths: Callable[[], tuple[Path, Path]],
    index_codex_processes: Callable[
        [Mapping[int, Mapping[str, Any]], Path, Path],
        dict[int, list[dict[str, Any]]],
    ],
    read_codex_session_index: Callable[[Path, list[str]], dict[str, str]],
    read_codex_db: Callable[
        [Path, Mapping[str, str]],
        tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]],
    ],
    recent_output_times: Callable[..., dict[str, int]],
    build_inventory: Callable[..., dict[str, Any]],
    daemon_identity: Callable[[Mapping[int, Mapping[str, Any]]], dict[str, int] | None],
    apply_provider_title_states: Callable[[dict[str, Any], Mapping[str, Any]], Any],
    apply_retained_setup_attributions: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """Collect a live inventory using one call per external list command."""
    invoke = runner or default_runner
    timeout = float(config.get("command_timeout_seconds", 6.0))
    root = proc_root or gated_proc_root(environ)
    max_proc_nodes = int(config.get("max_proc_nodes", default_max_proc_nodes))
    before_table = platform_process_table(root, max_proc_nodes)
    before_identity = daemon_identity(before_table)
    try:
        shpool_payload = command_json(
            fixture_env="SESSION_KIT_SHPOOL_JSON_FILE",
            command_env="SESSION_KIT_SHPOOL_CMD",
            default_command=(shpool_executable(), "list", "--json"),
            runner=invoke,
            timeout=timeout,
        )
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        CollectionError,
    ) as exc:
        raise CollectionError(f"cannot collect shpool snapshot: {exc}") from exc
    process_table = platform_process_table(root, max_proc_nodes)
    after_identity = daemon_identity(process_table)
    payload_binding = (
        before_identity
        if before_identity is not None and before_identity == after_identity
        else None
    )
    _parse_shpool_payload(shpool_payload)
    warnings: list[str] = []
    account_home = Path(environ.get("HOME") or Path.home())
    configured_claude_root = environ.get("CLAUDE_CONFIG_DIR", "")
    default_claude_root = _provider_profile_path(
        configured_claude_root, account_home / ".claude"
    )
    if default_claude_root is None:
        default_claude_root = account_home / ".claude"
    default_claude_key = _profile_key(default_claude_root)
    claude_profiles: dict[str, Path] = {default_claude_key: default_claude_root}
    claude_process_profiles: dict[int, str | None] = {}
    for pid, process in process_table.items():
        if not _is_native_claude(process):
            continue
        profile = _provider_profile_path(
            process.get("claude_config_dir"), default_claude_root
        )
        if profile is None:
            claude_process_profiles[pid] = None
            continue
        key = _profile_key(profile)
        claude_process_profiles[pid] = key
        claude_profiles[key] = profile

    claude_payload: list[Any] = []
    failed_claude_profiles: set[str | None] = set()
    ordered_claude_profiles = [
        default_claude_key,
        *sorted(key for key in claude_profiles if key != default_claude_key),
    ]
    for profile_key in ordered_claude_profiles:
        profile = claude_profiles[profile_key]
        command = (
            ("claude", "agents", "--json")
            if profile_key == default_claude_key
            else (
                "env",
                f"CLAUDE_CONFIG_DIR={profile}",
                "claude",
                "agents",
                "--json",
            )
        )
        try:
            profile_payload = command_json(
                fixture_env="SESSION_KIT_CLAUDE_JSON_FILE",
                command_env="SESSION_KIT_CLAUDE_CMD",
                default_command=command,
                runner=invoke,
                timeout=timeout,
            )
            if not isinstance(profile_payload, list):
                raise CollectionError("claude agents --json returned a non-array")
            for item in profile_payload:
                if not isinstance(item, Mapping):
                    continue
                agent_pid = item.get("pid")
                if not isinstance(agent_pid, int):
                    continue
                if claude_process_profiles.get(agent_pid) != profile_key:
                    continue
                retained = dict(item)
                retained["_session_kit_claude_config_dir"] = os.fspath(profile)
                claude_payload.append(retained)
        except (
            OSError,
            ValueError,
            subprocess.SubprocessError,
            CollectionError,
        ) as exc:
            failed_claude_profiles.add(profile_key)
            warnings.append(
                f"Claude inventory unavailable: {clean_text(str(exc), 240)}"
            )
    if any(value is None for value in claude_process_profiles.values()):
        failed_claude_profiles.add(None)
        warnings.append("Claude inventory unavailable: invalid live profile path")
    try:
        claude_payload = enrich_claude_payload(claude_payload)
        parsed_claude = parse_claude_payload(claude_payload)
        for agent in parsed_claude:
            # This is the PID registry observation itself. Agreement retires
            # a lag exception; changed nameSince makes divergence a later
            # human act even when the person reused the exact pre-push text.
            observe_claude_name(
                "claude",
                agent["uuid"],
                agent["title"],
                native_name_source=agent.get("name_source") or "",
                native_name_since=agent.get("name_since"),
                environ=environ,
            )
    except (OSError, ValueError, subprocess.SubprocessError, CollectionError) as exc:
        claude_payload = []
        failed_claude_profiles.update(ordered_claude_profiles)
        warnings.append(f"Claude inventory unavailable: {clean_text(str(exc), 240)}")

    codex_home, db_path = codex_paths()
    codex_index = index_codex_processes(process_table, root, codex_home)
    default_codex_key = _profile_key(codex_home)
    codex_profiles: dict[str, Path] = {default_codex_key: codex_home}
    codex_process_profiles: dict[int, str | None] = {}
    for pid, process in process_table.items():
        if not _is_native_codex(process):
            continue
        profile = _provider_profile_path(process.get("codex_home"), codex_home)
        if profile is None:
            codex_process_profiles[pid] = None
            continue
        key = _profile_key(profile)
        codex_process_profiles[pid] = key
        codex_profiles[key] = profile

    naming_warnings: list[str] = []
    codex_profile_rows: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    failed_codex_profiles: set[str | None] = set()
    visible_codex_profiles = {
        profile_key
        for pid, profile_key in codex_process_profiles.items()
        if codex_index.get(pid)
    }
    ordered_codex_profiles = [
        default_codex_key,
        *sorted(key for key in codex_profiles if key != default_codex_key),
    ]
    for profile_key in ordered_codex_profiles:
        profile = codex_profiles[profile_key]
        profile_db = db_path
        if profile_key != default_codex_key:
            databases = _codex_state_databases(profile)
            profile_db = databases[-1] if databases else profile / "state_5.sqlite"
        session_index_names: dict[str, str] = {}
        try:
            session_index_names = read_codex_session_index(
                profile / "session_index.jsonl", naming_warnings
            )
        except CollectionError as exc:
            naming_warnings.append(
                f"Codex session names unavailable: {clean_text(str(exc), 240)}"
            )
        try:
            profile_rows = read_codex_db(profile_db, session_index_names)
        except (OSError, sqlite3.Error, CollectionError) as exc:
            profile_rows = (
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
            failed_codex_profiles.add(profile_key)
            if profile_key in visible_codex_profiles:
                warnings.append(
                    f"Codex metadata unavailable: {clean_text(str(exc), 240)}"
                )
        else:
            if not profile_db.is_file():
                failed_codex_profiles.add(profile_key)
                if profile_key in visible_codex_profiles:
                    warnings.append(
                        "Codex metadata unavailable: database does not exist: "
                        f"{clean_text(str(profile_db), 240)}"
                    )
        codex_profile_rows[profile_key] = profile_rows
    if any(value is None for value in codex_process_profiles.values()):
        failed_codex_profiles.add(None)
        warnings.append("Codex metadata unavailable: invalid live profile path")

    merged_codex_threads: dict[str, Any] = {}
    merged_codex_edges: dict[str, Any] = {}
    for profile_key in ordered_codex_profiles:
        profile_threads, profile_edges = codex_profile_rows[profile_key]
        for uuid, thread in profile_threads.items():
            merged_codex_threads.setdefault(uuid, thread)
        for uuid, edges in profile_edges.items():
            merged_codex_edges.setdefault(uuid, edges)
    codex_rows = (
        merged_codex_threads,
        merged_codex_edges,
        {
            "default_home": os.fspath(codex_home),
            "profiles": codex_profile_rows,
        },
    )
    recent_outputs = recent_output_times(
        item["name"] for item in _parse_shpool_payload(shpool_payload)
    )
    publish_table = platform_process_table(root, max_proc_nodes)
    publish_identity = daemon_identity(publish_table)
    publish_binding = (
        payload_binding
        if payload_binding is not None and payload_binding == publish_identity
        else None
    )
    inventory = build_inventory(
        shpool_payload,
        claude_payload,
        process_table,
        codex_index,
        codex_rows,
        config,
        recent_output_by_shpool_id=recent_outputs,
        daemon_binding=publish_binding,
    )
    apply_provider_title_states(inventory, config)
    apply_retained_setup_attributions(inventory)
    inventory["warnings"].extend(warnings)
    inventory["naming_warnings"] = naming_warnings
    # Optional providers may be absent on a general-purpose installation.
    # A query failure is material only when that provider is visibly running.
    claude_failed = any(
        profile_key in failed_claude_profiles
        for profile_key in claude_process_profiles.values()
    )
    codex_failed = any(
        profile_key in failed_codex_profiles
        for pid, profile_key in codex_process_profiles.items()
        if codex_index.get(pid)
    )
    inventory["_complete"] = not (claude_failed or codex_failed)
    return inventory
