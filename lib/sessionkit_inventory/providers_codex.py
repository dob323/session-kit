"""Read-only Codex state readers: rollouts, session index, and state store."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Callable, Mapping, Sequence

from .common import CollectionError, clean_text, valid_uuid


ROLLOUT_TAIL_BYTES = 128 * 1024
ROLLOUT_LIFECYCLE_SEARCH_BYTES = 8 * 1024 * 1024


def _aligned_bounded_tail(
    descriptor: int,
    *,
    max_bytes: int,
) -> bytes | None:
    try:
        size = os.fstat(descriptor).st_size
        offset = max(0, size - max_bytes)
        raw = os.pread(descriptor, min(size, max_bytes), offset)
        preceding = os.pread(descriptor, 1, offset - 1) if offset else b"\n"
    except OSError:
        return None
    if not raw or not raw.endswith(b"\n"):
        return None
    if preceding != b"\n":
        boundary = raw.find(b"\n")
        if boundary < 0:
            return None
        raw = raw[boundary + 1 :]
    return raw


def _final_text_asks_a_question(payload: Mapping) -> bool:
    """True when an assistant message ends by asking the human something.

    Codex serializes only explicit request_user_input calls as questions, but
    assistants routinely finish a turn with prose like "which hostname should
    I use?" and then task_complete — the human is being waited on while every
    structured signal says idle. Conservative test: the LAST assistant
    message's final non-empty line ends with a question mark.
    """
    if payload.get("role") != "assistant":
        return False
    content = payload.get("content")
    if not isinstance(content, list):
        return False
    text = ""
    for part in content:
        if isinstance(part, Mapping) and isinstance(part.get("text"), str):
            text = part["text"]
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return bool(lines) and lines[-1].endswith("?")


def _parse_rollout_state(raw: bytes) -> str:
    lifecycle_state = "state unavailable"
    pending_questions: dict[str, bool] = {}
    final_message_asks = False
    for line in raw.splitlines():
        if not line:
            continue
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            return "state unavailable"
        if not isinstance(event, Mapping):
            return "state unavailable"
        payload = event.get("payload")
        event_type = event.get("type")
        if event_type == "event_msg":
            if not isinstance(payload, Mapping):
                return "state unavailable"
            lifecycle = payload.get("type")
            if lifecycle == "task_started":
                pending_questions.clear()
                final_message_asks = False
                lifecycle_state = "working"
            elif lifecycle in {"task_complete", "turn_aborted"}:
                pending_questions.clear()
                lifecycle_state = "idle"
            continue
        if event_type != "response_item":
            continue
        if not isinstance(payload, Mapping):
            return "state unavailable"
        kind = payload.get("type")
        if kind == "function_call_output":
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                return "state unavailable"
            if call_id in pending_questions:
                pending_questions.pop(call_id)
                lifecycle_state = "working"
            continue
        if kind == "message":
            final_message_asks = _final_text_asks_a_question(payload)
            continue
        if kind != "function_call" or payload.get("name") != "request_user_input":
            continue
        call_id = payload.get("call_id")
        arguments = payload.get("arguments")
        if (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(arguments, str)
        ):
            return "state unavailable"
        try:
            request: Any = json.loads(arguments)
        except (TypeError, ValueError):
            return "state unavailable"
        if not isinstance(request, Mapping):
            return "state unavailable"
        auto_resolution = request.get("autoResolutionMs")
        if "autoResolutionMs" in request and (
            isinstance(auto_resolution, bool)
            or not isinstance(auto_resolution, int)
            or auto_resolution <= 0
        ):
            return "state unavailable"
        pending_questions[call_id] = "autoResolutionMs" in request

    if pending_questions:
        if any(not optional for optional in pending_questions.values()):
            return "needs your reply"
        return "reply optional"
    if lifecycle_state == "idle" and final_message_asks:
        # A completed turn whose last words are a question: the human is
        # being waited on even though no structured request exists. Soft
        # signal only, never the hard "needs your reply".
        return "reply optional"
    return lifecycle_state


def rollout_turn_state(descriptor: int) -> str:
    """Classify one Codex turn from bounded, structured rollout events.

    The normal status tail is small, but a single long tool result can push the
    turn boundary out of that tail. Read backward through a larger fixed window
    only when the fast path has no decisive state. This remains bounded and
    fails closed when the most recent decisive event is still unavailable.
    """
    normal_tail = _aligned_bounded_tail(
        descriptor,
        max_bytes=ROLLOUT_TAIL_BYTES,
    )
    if normal_tail is None:
        return "state unavailable"
    state = _parse_rollout_state(normal_tail)
    if state != "state unavailable":
        return state
    lifecycle_tail = _aligned_bounded_tail(
        descriptor,
        max_bytes=ROLLOUT_LIFECYCLE_SEARCH_BYTES,
    )
    if lifecycle_tail is None:
        return "state unavailable"
    return _parse_rollout_state(lifecycle_tail)


def _is_native_codex(process: Mapping[str, Any]) -> bool:
    argv = process.get("cmdline") or []
    if not argv:
        return False
    executable = Path(argv[0]).name
    return executable == "codex" and process.get("comm") != "node"


def _is_codex_app_server(
    process: Mapping[str, Any],
    *,
    is_native_codex: Callable[[Mapping[str, Any]], bool],
) -> bool:
    """Recognize the native Codex process that owns remote TUI threads."""
    if not is_native_codex(process):
        return False
    argv = list(process.get("cmdline") or [])
    return "app-server" in argv[1:]


def codex_refresh_target(
    process_table: Mapping[int, Mapping[str, Any]],
    shell_pid: int,
    shell_generation: int,
    provider_pid: int,
    provider_generation: int,
    *,
    is_native_codex: Callable[[Mapping[str, Any]], bool],
    is_codex_app_server: Callable[[Mapping[str, Any]], bool],
    children_index: Callable[[Mapping[int, Mapping[str, Any]]], dict[int, list[int]]],
    descendants: Callable[..., list[int]],
    arg_value: Callable[[Sequence[str], str], str],
    max_proc_nodes: int,
) -> tuple[int, int]:
    """Prove the exact Codex process a title refresh may restart.

    A direct CLI owns both the rollout and TUI. For a remote TUI the App
    Server owns the rollout, but restarting it would destroy the resume socket;
    select the one native TUI connected to that exact server instead.
    """
    shell = process_table.get(shell_pid, {})
    provider = process_table.get(provider_pid, {})
    if shell.get("start_ticks") != shell_generation:
        raise CollectionError("managed shell generation changed")
    if provider.get("start_ticks") != provider_generation or not is_native_codex(
        provider
    ):
        raise CollectionError("Codex provider generation changed")
    children = children_index(process_table)
    tree = descendants(
        shell_pid,
        children,
        max_nodes=max_proc_nodes,
        max_depth=32,
    )
    if provider_pid not in tree:
        raise CollectionError("Codex provider is outside the managed shell")
    if not is_codex_app_server(provider):
        return provider_pid, provider_generation
    provider_argv = list(provider.get("cmdline") or [])
    listen = arg_value(provider_argv, "--listen")
    if not listen.startswith("unix://"):
        raise CollectionError("Codex App Server socket is unavailable")
    candidates: list[tuple[int, int]] = []
    for pid in tree:
        if pid == provider_pid:
            continue
        process = process_table.get(pid, {})
        if not is_native_codex(process) or is_codex_app_server(process):
            continue
        argv = list(process.get("cmdline") or [])
        generation = process.get("start_ticks")
        if (
            arg_value(argv, "--remote") == listen
            and "--no-alt-screen" in argv
            and isinstance(generation, int)
        ):
            candidates.append((pid, generation))
    if len(candidates) != 1:
        raise CollectionError(f"expected one remote Codex TUI, found {len(candidates)}")
    return candidates[0]


def _rollout_meta_fd(
    descriptor: int,
    *,
    rollout_turn_state: Callable[[int], str],
) -> dict[str, Any] | None:
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
            meta["_turn_state"] = rollout_turn_state(descriptor)
            return meta
    return None


def codex_open_rollouts(
    pid: int,
    proc_root: Path,
    codex_home: Path,
    expected_process: Mapping[str, Any],
    *,
    expected_proc_identity: Callable[
        [Path, int, Mapping[str, Any]], tuple[int, int, int] | None
    ],
    rollout_meta_fd: Callable[[int], dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """Read metadata through stable, native Codex-owned proc descriptors."""
    initial_identity = expected_proc_identity(proc_root, pid, expected_process)
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
        before_identity = expected_proc_identity(proc_root, pid, expected_process)
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
            if not stat.S_ISREG(opened_stat.st_mode):
                continue
            actual_link = Path(os.readlink(f"/proc/self/fd/{opened}"))
            actual_text = str(actual_link)
            if actual_text.endswith(" (deleted)"):
                actual_link = Path(actual_text[: -len(" (deleted)")])
            if "rollout-" not in actual_link.name or actual_link.suffix != ".jsonl":
                continue
            resolved = actual_link.resolve(strict=False)
            resolved.relative_to(home_resolved)
            file_identity = (opened_stat.st_dev, opened_stat.st_ino)
            if file_identity in seen:
                continue
            meta = rollout_meta_fd(opened)
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
        after_identity = expected_proc_identity(proc_root, pid, expected_process)
        if after_identity != before_identity:
            return []
        seen.add(file_identity)
        if meta is not None:
            result.append(meta)
    if expected_proc_identity(proc_root, pid, expected_process) != initial_identity:
        return []
    return result


def codex_rollout_by_uuid(
    codex_home: Path,
    uuid: str,
    *,
    max_files: int = 10_000,
    rollout_meta_fd: Callable[[int], dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """Read one exact Darwin Codex rollout selected only by its process UUID."""
    sessions = codex_home / "sessions"
    try:
        root = sessions.resolve(strict=True)
    except OSError:
        return []
    seen = 0
    candidates: list[Path] = []
    try:
        for directory, names, files in os.walk(root):
            names[:] = sorted(name for name in names if not name.startswith("."))
            for name in sorted(files):
                seen += 1
                if seen > max_files:
                    raise CollectionError("Codex rollout tree exceeds safety bound")
                if (
                    uuid in name
                    and name.startswith("rollout-")
                    and name.endswith(".jsonl")
                ):
                    candidates.append(Path(directory) / name)
    except OSError:
        return []
    if len(candidates) != 1:
        return []
    path = candidates[0]
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        descriptor = os.open(
            resolved,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except (OSError, ValueError):
        return []
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            return []
        meta = rollout_meta_fd(descriptor)
        if meta is None or valid_uuid(meta.get("id")) != uuid:
            return []
        return [meta]
    finally:
        os.close(descriptor)


def codex_rollout_state_by_path(
    codex_home: Path,
    rollout_path: object,
    *,
    rollout_turn_state: Callable[[int], str],
) -> str:
    """Read one child thread's state from an owner-controlled Codex rollout."""
    if not isinstance(rollout_path, str) or not rollout_path.startswith("/"):
        return "state unavailable"
    try:
        root = codex_home.resolve(strict=True)
        raw_path = Path(rollout_path)
        pathname = raw_path.lstat()
        resolved = raw_path.resolve(strict=True)
        resolved.relative_to(root)
        descriptor = os.open(
            raw_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except (OSError, ValueError):
        return "state unavailable"
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (pathname.st_dev, pathname.st_ino)
        ):
            return "state unavailable"
        return rollout_turn_state(descriptor)
    finally:
        os.close(descriptor)


def index_codex_processes(
    process_table: Mapping[int, Mapping[str, Any]],
    proc_root: Path,
    codex_home: Path,
    *,
    runtime_platform: Callable[[], str],
    darwin_platform: str,
    is_native_codex: Callable[[Mapping[str, Any]], bool],
    rollout_by_uuid: Callable[[Path, str], list[dict[str, Any]]],
    open_rollouts: Callable[[int, Path, Path, Mapping[str, Any]], list[dict[str, Any]]],
) -> dict[int, list[dict[str, Any]]]:
    def process_home(process: Mapping[str, Any]) -> Path | None:
        raw = process.get("codex_home")
        if not isinstance(raw, str) or not raw:
            return codex_home
        candidate = Path(raw)
        if not candidate.is_absolute() or ".." in candidate.parts:
            return None
        return candidate

    if runtime_platform() == darwin_platform:
        result: dict[int, list[dict[str, Any]]] = {}
        for pid, process in process_table.items():
            if not is_native_codex(process):
                continue
            profile_home = process_home(process)
            uuid = valid_uuid(process.get("codex_thread_id"))
            result[pid] = (
                rollout_by_uuid(profile_home, uuid)
                if profile_home is not None and uuid
                else []
            )
        return result
    return {
        pid: (
            open_rollouts(pid, proc_root, profile_home, process)
            if (profile_home := process_home(process)) is not None
            else []
        )
        for pid, process in process_table.items()
        if is_native_codex(process)
    }


def _root_codex_uuid(
    process: Mapping[str, Any],
    metadata: Sequence[Mapping[str, Any]],
    *,
    is_codex_app_server: Callable[[Mapping[str, Any]], bool],
) -> list[str]:
    """Return exact root threads owned by one native Codex process.

    Direct Codex writes ``source=cli``. A remote TUI delegates its thread to
    the native ``codex app-server`` process, whose open rollout is currently
    marked ``source=vscode``. Only that validated process type may claim the
    second source; an ordinary Codex process cannot turn arbitrary editor
    rollouts into managed-session identity evidence.
    """
    identities: set[str] = set()
    app_server = is_codex_app_server(process)
    for meta in metadata:
        uuid = valid_uuid(meta.get("session_id"))
        event_id = valid_uuid(meta.get("id"))
        source = meta.get("source")
        accepted_source = source == "cli" or (app_server and source == "vscode")
        if accepted_source and uuid and event_id == uuid:
            identities.add(uuid)
    return sorted(identities)


def _codex_turn_state(metadata: Sequence[Mapping[str, Any]], wanted_uuid: str) -> str:
    """Turn state for one exact conversation, from its own rollout metadata."""
    for meta in metadata:
        uuid = valid_uuid(meta.get("session_id"))
        event_id = valid_uuid(meta.get("id"))
        if uuid == wanted_uuid and event_id == uuid:
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


def _codex_state_databases(codex_root: Path) -> list[Path]:
    """Codex state stores ordered oldest-to-newest by schema number.

    Lexical order breaks at two digits (state_10 sorts before state_5), so
    the numeric suffix is the key. Empty files are placeholder artifacts,
    never the live store.
    """

    def version(path: Path) -> tuple[int, str]:
        suffix = path.stem.rsplit("_", 1)[-1]
        return (int(suffix) if suffix.isdigit() else -1, path.name)

    try:
        candidates = [
            path
            for path in codex_root.glob("state_*.sqlite")
            if path.stat().st_size > 0
        ]
    except OSError:
        return []
    return sorted(candidates, key=version)


def _codex_home(
    environ: Mapping[str, str] | None = None,
    *,
    default_home: Path | None = None,
    process_environ: Mapping[str, str],
    home: Callable[[], Path],
) -> Path:
    """Resolve one Codex home consistently for reads and writes."""
    env = environ if environ is not None else process_environ
    base = default_home if default_home is not None else home()
    return Path(
        env.get("SESSION_KIT_CODEX_HOME") or env.get("CODEX_HOME") or (base / ".codex")
    ).expanduser()


def _codex_paths(
    *,
    environ: Mapping[str, str],
    codex_home: Callable[[], Path],
    codex_state_databases: Callable[[Path], list[Path]],
) -> tuple[Path, Path]:
    home = codex_home()
    db_override = environ.get("SESSION_KIT_CODEX_DB")
    if db_override:
        return home, Path(db_override).expanduser()
    databases = codex_state_databases(home)
    if databases:
        return home, databases[-1]
    return home, home / "state_5.sqlite"


def read_codex_session_index(
    path: Path,
    warning_sink: list[str] | None = None,
    *,
    read_bounded_owner_file: Callable[..., bytes | None],
    max_bytes: int,
) -> dict[str, str]:
    """Read exact UUID-bound Codex names from the append-only local index."""
    payload = read_bounded_owner_file(
        path,
        label="Codex session index",
        max_bytes=max_bytes,
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
    *,
    session_index_reader: Callable[[Path], dict[str, str]],
    rollout_state_by_path: Callable[[Path, object], str],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Read titles and spawn edges from Codex state without creating journal files."""
    index_names = (
        dict(session_index_names)
        if session_index_names is not None
        else session_index_reader(db_path.parent / "session_index.jsonl")
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
            row["name"]
            for row in connection.execute("PRAGMA table_info(threads)").fetchall()
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
                "rollout_path",
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
                edge_status = clean_text(row["status"], 40) or "unknown"
                if edge_status.casefold() == "open":
                    observed = rollout_state_by_path(
                        db_path.parent, child.get("rollout_path")
                    )
                    if observed != "state unavailable":
                        edge_status = observed
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
                        "status": edge_status,
                    }
                )
        for items in edges.values():
            items.sort(key=lambda item: (item["title"].casefold(), item["uuid"] or ""))
        return threads, edges
    finally:
        connection.close()
