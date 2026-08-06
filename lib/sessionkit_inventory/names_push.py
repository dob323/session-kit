"""Provider-native name and colour writing.

Everything here reaches into a provider's own storage: Claude's name-intent
file, transcript records and session records; Codex's session index, thread
store and live app-server sockets. Nothing here decides WHAT a session should
be called — that is ``names`` — and nothing here reads inventory state.

The two halves depend on each other in both directions (this module reconciles
pending titles, ``names`` dispatches pushes), so neither imports the other. The
facade injects across the seam, which is what keeps the package graph acyclic.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import stat as statmod
import tempfile
import time
from typing import Any, Callable, Mapping

from .common import automatic_naming_enabled, valid_uuid
from .providers_codex import _codex_state_databases


MAX_CLAUDE_SESSION_RECORDS = 512


def _push_claude_title(
    home: Path,
    uuid: str,
    title: str,
    *,
    atomic_write_json: Callable[[Path, Any], None],
    max_session_records: int,
) -> tuple[list[str], list[str]]:
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
        records = sorted(sessions.glob("*.json"))[:max_session_records]
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


def _codex_title_echoes_prompt(title: str, first_message: str) -> bool:
    """True when a stored title is just the first prompt (or a prefix cut)."""
    return (
        bool(title)
        and bool(first_message)
        and (
            first_message.casefold().startswith(title.casefold())
            or title.casefold().startswith(first_message.casefold())
        )
    )


def codex_bounce_prepare(
    uuid: str,
    codex_root: Path | None = None,
    now: float | None = None,
    *,
    codex_home: Callable[..., Path],
    max_session_index_bytes: int,
) -> str:
    """Resolve the real name a bounced Codex process should boot under.

    Session-index rename evidence wins; otherwise a database title that does
    not merely echo the first prompt. Codex seeds threads.title with the raw
    prompt and replaces it within about two minutes of the first answer, so
    an echo-shaped title only counts once the thread has been quiet long
    enough that it must be the final title (a terse prompt can BE the real
    title). The chosen name is mirrored into the session index when its
    latest entry differs, because the relaunched status bar reads only the
    index. Returns "" when the thread has no real name yet — the caller must
    then defer the bounce and keep its one-shot marker so a later reopen
    retries after the title exists.
    """
    import sqlite3

    if codex_root is None:
        codex_root = codex_home()
    title = ""
    first_message = ""
    has_split_prompt_schema = False
    settled = False
    databases = _codex_state_databases(codex_root)
    if databases:
        fetched = None
        selected_columns: list[str] = []
        try:
            connection = sqlite3.connect(f"file:{databases[-1]}?mode=ro", uri=True)
            try:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(threads)")
                }
                has_split_prompt_schema = "first_user_message" in columns
                selected_columns = ["title"]
                if has_split_prompt_schema:
                    selected_columns.append("first_user_message")
                if "updated_at" in columns:
                    selected_columns.append("updated_at")
                fetched = connection.execute(
                    "SELECT "
                    + ", ".join(selected_columns)
                    + " FROM threads WHERE id = ?",
                    (uuid,),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            fetched = None
        if fetched:
            values = dict(zip(selected_columns, fetched))
            title = str(values.get("title") or "").strip()
            if has_split_prompt_schema:
                first_message = str(values.get("first_user_message") or "").strip()
            updated_at = values.get("updated_at")
            if isinstance(updated_at, (int, float)) and not isinstance(
                updated_at, bool
            ):
                current = time.time() if now is None else now
                settled = current - float(updated_at) > 300
    index = codex_root / "session_index.jsonl"
    index_name = ""
    if index.is_file() and not index.is_symlink():
        try:
            if index.stat().st_size <= max_session_index_bytes:
                for line in index.read_text(encoding="utf-8").splitlines():
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and entry.get("id") == uuid:
                        candidate = str(entry.get("thread_name") or "").strip()
                        if candidate:
                            index_name = candidate
        except OSError:
            index_name = ""
    # Codex never writes the session index itself: every entry is a
    # deliberate push (kit title, auto-titler, rename mirror), so any entry
    # counts as a real name even when it is echo-shaped. Only the database
    # title needs the seed-vs-final test.
    index_is_explicit = bool(index_name)
    if has_split_prompt_schema:
        title_is_explicit = bool(title) and (
            settled or not _codex_title_echoes_prompt(title, first_message)
        )
    else:
        title_is_explicit = bool(title)
    if index_is_explicit:
        chosen = index_name
    elif title_is_explicit:
        chosen = title
    else:
        return ""
    if index_name != chosen:
        # A failed mirror would relaunch under the stale bar name and burn
        # the one-shot bounce on nothing, so it defers instead.
        if index.is_symlink():
            return ""
        try:
            _append_codex_index_entry(index, uuid, chosen[:100])
        except OSError:
            return ""
    return chosen


def claude_bounce_prepare(
    uuid: str, home: Path | None = None, now: float | None = None
) -> tuple[str, bool]:
    """Decide whether a Claude window can only be named by a restart.

    Claude applies a kit name intent through its SessionStart and prompt
    hooks — a living window renames at the next prompt, and a fresh boot
    renames immediately. A session whose name arrived AFTER boot and that
    never saw another prompt (ask, read, close) therefore shows its stale
    title until the provider restarts. Returns (title, clear):
    title != "" — bounce is warranted; "" with clear=True — the live window
    already renamed (a real prompt followed the intent), so the caller
    should drop its untitled marker; "" with clear=False — defer, a name
    may still arrive.
    """
    if home is None:
        home = Path.home()
    intent = home / ".claude" / "sessions" / f"{uuid}.nameintent"
    try:
        if intent.is_symlink() or not intent.is_file():
            return "", False
        intent_mtime = intent.stat().st_mtime
        title = intent.read_text(encoding="utf-8").splitlines()[0].strip()[:64]
    except (OSError, IndexError):
        return "", False
    if not title:
        return "", False
    try:
        transcripts = sorted(
            (home / ".claude" / "projects").glob(f"*/{uuid}.jsonl"),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        transcripts = []
    if not transcripts:
        return "", False
    last_prompt = 0.0
    try:
        with open(transcripts[-1], encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, Mapping) or record.get("type") != "user":
                    continue
                if record.get("isMeta"):
                    continue
                message = record.get("message")
                if not isinstance(message, Mapping):
                    continue
                content = message.get("content")
                # Real prompts are strings; tool results arrive as lists.
                # The synthesized resume-mount pair is not a human prompt.
                if not isinstance(content, str):
                    continue
                if content.startswith("Continue from where you left off."):
                    continue
                stamp = record.get("timestamp")
                if not isinstance(stamp, str):
                    continue
                try:
                    parsed = dt.datetime.fromisoformat(
                        stamp.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    continue
                last_prompt = max(last_prompt, parsed)
    except OSError:
        return "", False
    if last_prompt > intent_mtime + 1:
        # The hook applied the name at that prompt; the window is current.
        return "", True
    return title, False


def codex_pending_auto_titles(
    environ: Mapping[str, str] | None = None,
    *,
    codex_paths: Callable[[], tuple[Path, Path]],
    reconcile_pending_titles: Callable[..., Any],
    derive_title: Callable[[Any], str | None],
    load_config: Callable[[], dict[str, Any]],
    max_session_index_bytes: int,
    push_live_rename: Callable[[Path, str, str], tuple[list[str], list[str]]],
) -> list[dict[str, str]]:
    """Kit-side auto-titler for Codex threads nobody has named.

    Codex seeds threads.title with the raw first prompt and has no title
    hook, and agents only self-name work they judge substantive — so a plain
    question thread stays unnamed on every surface. Recent split-schema
    threads whose title is empty or still echoes the first prompt, and which
    have no session-index entry (the deliberate-name evidence), get the same
    seven-word heuristic title Claude sessions get, pushed into both Codex
    stores. Self-terminating: the push writes the index entry that excludes
    the thread from every later pass, and a later agent self-name simply
    overwrites it (last-writer-wins).
    """
    import sqlite3

    env = environ if environ is not None else os.environ
    reconciled = reconcile_pending_titles(load_config(), "codex", env)
    if not automatic_naming_enabled(env):
        return reconciled
    if str(env.get("SESSION_KIT_CODEX_AUTOTITLE", "")).strip().casefold() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return reconciled
    codex_root, database = codex_paths()
    if not database.is_file():
        return reconciled
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(threads)")
            }
            if not {"id", "title", "first_user_message", "updated_at"} <= columns:
                return reconciled
            # Only human root threads: subagent threads inherit their task
            # prompt as first_user_message, and titling them replaces useful
            # agent nicknames with near-duplicate task titles. Non-interactive
            # `codex exec` runs are one-shot jobs nobody reopens — titling
            # them only fills the index with prompt fragments.
            root_filter = ""
            if "thread_source" in columns:
                root_filter = " AND (thread_source IS NULL OR thread_source = 'user')"
            elif "agent_path" in columns:
                root_filter = " AND (agent_path IS NULL OR agent_path = '')"
            if "source" in columns:
                root_filter += " AND (source IS NULL OR source != 'exec')"
            rows = connection.execute(
                "SELECT id, title, first_user_message, updated_at"
                " FROM threads"
                " WHERE first_user_message IS NOT NULL"
                " AND first_user_message != ''"
                + root_filter
                + " ORDER BY updated_at DESC LIMIT 200"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return reconciled
    index = codex_root / "session_index.jsonl"
    # Latest entry per id wins — the file is append-ordered and renames are
    # appended, so later lines overwrite earlier ones here.
    indexed: dict[str, str] = {}
    if index.is_file() and not index.is_symlink():
        try:
            if index.stat().st_size > max_session_index_bytes:
                return reconciled
            for line in index.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, Mapping) and entry.get("id"):
                    indexed[str(entry["id"])] = str(
                        entry.get("thread_name") or ""
                    ).strip()
        except OSError:
            # Unable to prove which threads already carry deliberate names;
            # titling anyway could overwrite one, so do nothing.
            return reconciled
    cutoff = time.time() - 7 * 86400
    results: list[dict[str, str]] = [
        {"uuid": str(item["uuid"]), "title": str(item["title"])} for item in reconciled
    ]
    # Live app-server windows repaint from a socket rename; derive the
    # socket root from the caller's exact environment (no real-home
    # fallback) so sandboxed runs stay inert.
    live_state_dir: Path | None = None
    if env.get("SESSION_KIT_CODEX_LIVE_RENAME") != "0":
        live_home = env.get("HOME")
        if live_home:
            live_state_dir = _session_kit_state_dir(env, Path(live_home))
    healed = 0
    for uuid, raw_title, first_message, updated_at in rows:
        exact = valid_uuid(uuid)
        if not exact:
            continue
        if exact in indexed:
            # Heal the database half of the dual write: the status bar reads
            # threads.title, and a curated index name next to a prompt-echo
            # (or empty) database title means the earlier UPDATE never landed
            # or Codex re-stamped the seed over it. Re-assert, bounded and
            # fail-open; each inventory build converges the two stores.
            index_name = indexed[exact]
            title = str(raw_title or "").strip()
            first = str(first_message or "").strip()
            if (
                healed < 20
                and index_name
                and index_name != title
                and not _codex_title_echoes_prompt(index_name, first)
                and (not title or _codex_title_echoes_prompt(title, first))
            ):
                _push_codex_thread_title(codex_root, exact, index_name)
                if live_state_dir is not None:
                    push_live_rename(live_state_dir, exact, index_name)
                healed += 1
            continue
        if exact == "00000000-0000-0000-0000-000000000000":
            continue
        if (
            not isinstance(updated_at, (int, float))
            or isinstance(updated_at, bool)
            or float(updated_at) < cutoff
        ):
            continue
        title = str(raw_title or "").strip()
        first = str(first_message or "").strip()
        if title and not _codex_title_echoes_prompt(title, first):
            # A real name someone wrote; never replace it.
            continue
        derived = derive_title(first)
        if not derived:
            continue
        # Both writes target the exact store tree the candidates were read
        # from — pushing anywhere else would split reads and writes across
        # different Codex homes.
        try:
            _append_codex_index_entry(index, exact, derived[:100])
        except OSError:
            continue
        _push_codex_thread_title(codex_root, exact, derived)
        if live_state_dir is not None:
            push_live_rename(live_state_dir, exact, derived)
        results.append({"uuid": exact, "title": derived})
        if len(results) >= 20:
            break
    return results


def _append_codex_index_entry(index: Path, uuid: str, title: str) -> None:
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
    descriptor = os.open(index, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(entry + "\n")
        handle.flush()
        os.fsync(handle.fileno())


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

    candidates = _codex_state_databases(codex_root)
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


MAX_CODEX_LIVE_RENAME_SOCKETS = 8

MAX_CODEX_LIVE_RENAME_FRAME = 1024 * 1024


def _session_kit_state_dir(env: Mapping[str, str], home: Path) -> Path:
    """The session-kit state directory under the caller's exact sandbox.

    Same precedence as the shell helpers: SESSION_KIT_STATE_DIR names the
    kit directory itself; otherwise it lives under XDG_STATE_HOME or the
    sandbox home. Never falls back to the real home — an explicit
    environment IS the caller's sandbox.
    """
    explicit = env.get("SESSION_KIT_STATE_DIR")
    if explicit:
        return Path(explicit)
    xdg = env.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "session-kit"
    return home / ".local" / "state" / "session-kit"


def _ws_send_frame(connection: Any, payload: bytes, opcode: int = 1) -> None:
    import struct

    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([length | 0x80])
    elif length < 65536:
        header += bytes([126 | 0x80]) + struct.pack("!H", length)
    else:
        header += bytes([127 | 0x80]) + struct.pack("!Q", length)
    mask = os.urandom(4)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    connection.sendall(header + mask + masked)


def _ws_recv_frame(connection: Any, *, max_frame: int) -> tuple[int, bytes]:
    import struct

    def read_exact(count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = connection.recv(count - len(data))
            if not chunk:
                raise OSError("WebSocket closed mid-frame")
            data += chunk
        return data

    first, second = read_exact(2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", read_exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(8))[0]
    if length > max_frame:
        raise OSError("oversized WebSocket frame")
    mask = read_exact(4) if second & 0x80 else b""
    payload = read_exact(length)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, payload


def _ws_request(
    connection: Any,
    request_id: int,
    method: str,
    params: dict[str, Any],
    *,
    max_frame: int,
) -> None:
    _ws_send_frame(
        connection,
        json.dumps(
            {"method": method, "id": request_id, "params": params},
            separators=(",", ":"),
        ).encode(),
    )
    while True:
        opcode, payload = _ws_recv_frame(connection, max_frame=max_frame)
        if opcode == 8:
            raise OSError("WebSocket closed by server")
        if opcode == 9:
            _ws_send_frame(connection, payload, opcode=10)
            continue
        if opcode != 1:
            continue
        try:
            message = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if message.get("error") is not None:
            raise OSError(f"app-server {method} refused: {message['error']}")
        return


def _push_codex_live_rename(
    kit_state_dir: Path,
    uuid: str,
    title: str,
    *,
    max_sockets: int,
    max_frame: int,
) -> tuple[list[str], list[str]]:
    """Repaint live app-server-backed Codex windows with the new name.

    A direct-TUI Codex bar repaints only at process start, but a remote TUI
    attached to a kit app-server repaints its thread-title item from the
    thread/name/updated broadcast — so a rename through the socket names a
    LIVE window with no provider restart. Every kit app-server shares one
    thread store, so the rename is offered to each live socket; a server
    not hosting the thread writes the same value the direct push already
    wrote, harmlessly. Entirely fail-open: absent, dead, or refusing
    sockets never block the store push and never warn — most sessions are
    direct TUIs with no socket at all.
    """
    import base64
    import socket as socketmod

    app_root = kit_state_dir / "app-server"
    try:
        candidates = sorted(app_root.iterdir())[:max_sockets]
    except OSError:
        return [], []
    delivered = False
    for directory in candidates:
        socket_path = directory / "app.sock"
        try:
            if socket_path.is_symlink() or not statmod.S_ISSOCK(
                os.stat(socket_path).st_mode
            ):
                continue
        except OSError:
            continue
        connection = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        connection.settimeout(2.0)
        try:
            connection.connect(os.fspath(socket_path))
            key = base64.b64encode(os.urandom(16)).decode()
            connection.sendall(
                (
                    "GET / HTTP/1.1\r\nHost: localhost\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            response = b""
            while not response.endswith(b"\r\n\r\n"):
                if len(response) > 65536:
                    raise OSError("oversized upgrade response")
                byte = connection.recv(1)
                if not byte:
                    raise OSError("app-server closed during upgrade")
                response += byte
            if b" 101 " not in response.split(b"\r\n", 1)[0]:
                raise OSError("WebSocket upgrade refused")
            _ws_request(
                connection,
                1,
                "initialize",
                {
                    "clientInfo": {
                        "name": "session-kit-live-rename",
                        "title": "Session Kit live rename",
                        "version": "1.0",
                    }
                },
                max_frame=max_frame,
            )
            _ws_send_frame(
                connection,
                json.dumps(
                    {"method": "initialized", "params": {}},
                    separators=(",", ":"),
                ).encode(),
            )
            _ws_request(
                connection,
                2,
                "thread/name/set",
                {"threadId": uuid, "name": title},
                max_frame=max_frame,
            )
            delivered = True
        except (OSError, ValueError):
            continue
        finally:
            with contextlib.suppress(OSError):
                connection.close()
    if delivered:
        return ["codex-live-rename"], []
    return [], []


def _push_codex_title(
    codex_root: Path,
    uuid: str,
    title: str,
    kit_state_dir: Path | None = None,
    *,
    max_session_index_bytes: int,
    push_live_rename: Callable[[Path, str, str], tuple[list[str], list[str]]],
) -> tuple[list[str], list[str]]:
    if not codex_root.is_dir():
        return [], ["Codex home unavailable; title not pushed"]
    index = codex_root / "session_index.jsonl"
    if index.is_symlink():
        return [], ["Codex session index is a symlink; title not pushed"]
    try:
        if index.exists() and index.stat().st_size > max_session_index_bytes:
            return [], [
                "Codex session index exceeds the bounded size; title not pushed"
            ]
        _append_codex_index_entry(index, uuid, title)
    except OSError as exc:
        return [], [f"Codex session index not appended: {exc}"]
    thread_pushes, thread_warnings = _push_codex_thread_title(codex_root, uuid, title)
    live_pushes: list[str] = []
    if kit_state_dir is not None:
        live_pushes, _ = push_live_rename(kit_state_dir, uuid, title)
    return (
        ["codex-session-index", *thread_pushes, *live_pushes],
        thread_warnings,
    )


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
