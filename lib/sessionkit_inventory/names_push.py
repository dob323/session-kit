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
import errno
import json
import os
from pathlib import Path
import re
import stat as statmod
import sys
import time
from typing import Any, Callable, Mapping, Protocol

from .common import (
    _valid_name_since,
    automatic_naming_enabled,
    clean_text,
    valid_uuid,
)
from .providers_codex import _codex_state_databases
from .transcripts import claude_roots


MAX_CLAUDE_SESSION_RECORDS = 512


class LiveRename(Protocol):
    def __call__(
        self,
        state_dir: Path,
        uuid: str,
        title: str,
        /,
        *,
        timeout_seconds: float | None = None,
        still_automatic: Callable[[], bool] | None = None,
    ) -> tuple[list[str], list[str]]: ...


def _claude_sessions_directories(
    home: Path, environ: Mapping[str, str] | None = None
) -> tuple[list[Path], list[str]]:
    """Verified Claude registry directories which cannot escape a profile."""
    env = dict(environ or {})
    env["HOME"] = os.fspath(home)
    found: list[Path] = []
    warnings: list[str] = []
    seen: set[str] = set()
    account_root = Path(
        env.get("SESSION_KIT_ACCOUNT_ROOT")
        or home / ".local/share/session-kit/accounts"
    )
    account_profiles = (account_root / "claude").absolute()
    for root in claude_roots(env):
        sessions = root / "sessions"
        try:
            # An enrolled profile name is a trust boundary, not a redirect.
            # Refuse it before resolve() can erase the evidence that the
            # account-profile component itself was a symlink.
            if root.absolute().parent == account_profiles and root.is_symlink():
                warnings.append(
                    f"Claude sessions directory refused: {sessions}: "
                    "account profile is a symlink"
                )
                continue
            if not sessions.exists() and not sessions.is_symlink():
                continue
            resolved_root = root.resolve(strict=True)
            resolved_sessions = sessions.resolve(strict=True)
            try:
                resolved_sessions.relative_to(resolved_root)
            except ValueError:
                warnings.append(
                    f"Claude sessions directory refused: {sessions}: "
                    "resolved path escapes its profile root"
                )
                continue
            descriptor = os.open(
                resolved_sessions,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                if not statmod.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise OSError("not a directory")
            finally:
                os.close(descriptor)
        except (OSError, RuntimeError) as exc:
            warnings.append(f"Claude sessions directory refused: {sessions}: {exc}")
            continue
        key = os.fspath(resolved_sessions)
        if key in seen:
            continue
        seen.add(key)
        found.append(resolved_sessions)
    return found, warnings


def _open_sessions_directory(sessions: Path) -> int:
    descriptor = os.open(
        sessions,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        is_directory = statmod.S_ISDIR(os.fstat(descriptor).st_mode)
    except OSError:
        os.close(descriptor)
        raise
    if not is_directory:
        os.close(descriptor)
        raise OSError("not a directory")
    return descriptor


def _read_json_at(directory: int, name: str) -> dict[str, Any] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory)
    try:
        if not statmod.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        chunks: list[bytes] = []
        remaining = 1024 * 1024 + 1
        while remaining:
            block = os.read(descriptor, min(remaining, 65536))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        payload = b"".join(chunks)
        if len(payload) > 1024 * 1024:
            return None
        value = json.loads(payload.decode("utf-8", "strict"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    finally:
        os.close(descriptor)


def _atomic_bytes_at(directory: int, name: str, payload: bytes) -> None:
    """Publish one private file relative to an already verified directory."""
    try:
        existing = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
    except FileNotFoundError:
        existing = -1
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            label = "name intent" if name.endswith(".nameintent") else f"target {name}"
            raise OSError(f"refusing symlinked {label}") from exc
        raise
    if existing >= 0:
        try:
            if not statmod.S_ISREG(os.fstat(existing).st_mode):
                raise OSError(f"refusing non-regular target {name}")
        finally:
            os.close(existing)

    temporary = ""
    descriptor = -1
    for attempt in range(100):
        candidate = f".{name}.{os.getpid()}.{time.time_ns()}.{attempt}"
        try:
            descriptor = os.open(
                candidate,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory,
            )
            temporary = candidate
            break
        except FileExistsError:
            continue
    if descriptor < 0:
        raise OSError("cannot allocate temporary registry file")
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short registry write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary,
            name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        temporary = ""
        os.fsync(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)


def _claude_session_records(
    home: Path,
    uuid: str,
    *,
    environ: Mapping[str, str] | None,
    max_session_records: int,
    session_directories: list[Path] | None = None,
) -> tuple[list[tuple[Path, str, dict[str, Any]]], list[str]]:
    """Readable, regular PID records for one exact conversation."""
    found: list[tuple[Path, str, dict[str, Any]]] = []
    if session_directories is None:
        directories, warnings = _claude_sessions_directories(home, environ)
    else:
        directories, warnings = session_directories, []
    for sessions in directories:
        directory = -1
        try:
            directory = _open_sessions_directory(sessions)
            records = sorted(
                name
                for name in os.listdir(directory)
                if re.fullmatch(r"\d+\.json", name)
            )[:max_session_records]
        except OSError as exc:
            warnings.append(f"Claude session records unreadable: {sessions}: {exc}")
            if directory >= 0:
                os.close(directory)
            continue
        try:
            for record in records:
                try:
                    data = _read_json_at(directory, record)
                except OSError:
                    continue
                if data is not None and data.get("sessionId") == uuid:
                    found.append((sessions, record, data))
        finally:
            os.close(directory)
    return found, warnings


def _claude_pre_push_title(
    home: Path,
    uuid: str,
    *,
    environ: Mapping[str, str] | None,
    max_session_records: int,
) -> dict[str, Any] | None:
    """The one unambiguous registry name observation before a Claude push.

    ``None`` means the registry supplied no safe evidence.  Multiple matching
    records count only when every readable copy agrees; uncertainty must keep
    the historical human-rename behavior.
    """
    records, _warnings = _claude_session_records(
        home,
        uuid,
        environ=environ,
        max_session_records=max_session_records,
    )
    observations: set[tuple[str, str | int | float, str]] = set()
    for _sessions, _name, data in records:
        title = clean_text(data.get("name"), 120)
        if not title:
            continue
        name_since = _valid_name_since(data.get("nameSince"))
        if name_since is None:
            return None
        observations.add((title, name_since, clean_text(data.get("nameSource"), 20)))
    if len(observations) != 1:
        return None
    title, name_since, name_source = next(iter(observations))
    return {"title": title, "nameSince": name_since, "nameSource": name_source}


def _claude_transcripts(home: Path, uuid: str) -> list[Path]:
    """Every local transcript of one conversation, whichever profile holds it.

    A session started on an enrolled account keeps its transcript under that
    account's profile, not under ``~/.claude``. A push that knew only the
    default root therefore wrote nothing for those sessions and reported no
    error a person would ever see — measured 2026-08-12: six of seven live
    sessions had no colour record at all, so every provider window picked its
    own colour while the picker showed the kit's.
    """
    found: list[Path] = []
    for root in claude_roots({"HOME": os.fspath(home)}):
        try:
            found.extend(sorted((root / "projects").glob(f"*/{uuid}.jsonl")))
        except OSError:
            continue
    return found


def _claude_transcripts_by_age(home: Path, uuid: str) -> list[Path]:
    """`_claude_transcripts`, oldest first, unreadable entries dropped.

    One conversation can exist in more than one profile — an account switch
    copies it — and then "which file describes this session" has to be
    answered by evidence, not by which profile name sorts last. Recency is
    the same rule `transcripts._claude_transcript` already uses.
    """
    dated: list[tuple[float, str, Path]] = []
    for path in _claude_transcripts(home, uuid):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            dated.append((path.stat().st_mtime, os.fspath(path), path))
        except OSError:
            continue
    dated.sort()
    return [path for _, _, path in dated]


def _push_claude_title(
    home: Path,
    uuid: str,
    title: str,
    *,
    environ: Mapping[str, str] | None = None,
    atomic_write_json: Callable[[Path, Any], None],
    max_session_records: int,
) -> tuple[list[str], list[str]]:
    pushed: list[str] = []
    session_directories, warnings = _claude_sessions_directories(home, environ)
    if not session_directories:
        if not warnings:
            warnings.append("Claude sessions directory unavailable; title not pushed")
        return pushed, warnings
    for sessions in session_directories:
        intent_name = f"{uuid}.nameintent"
        directory = -1
        try:
            directory = _open_sessions_directory(sessions)
            _atomic_bytes_at(directory, intent_name, (title + "\n").encode("utf-8"))
            pushed.append("claude-nameintent")
        except OSError as exc:
            warnings.append(f"Claude name intent not written: {sessions}: {exc}")
        finally:
            if directory >= 0:
                os.close(directory)
    # The prompt bar's bottom-right name is a transcript agent-name record 
    # the exact store /rename persists, hydrated at session start/resume,
    # rendered beside the agent-color. Same append discipline as colors.
    # A profile can hold the transcript without a sessions/ registry (fresh
    # or partially migrated account roots), so transcript reach must never be
    # narrowed to the roots that happened to have one.
    transcripts = _claude_transcripts(home, uuid)
    name_entry = json.dumps(
        {"type": "agent-name", "agentName": title, "sessionId": uuid},
        separators=(",", ":"),
    )
    for transcript in transcripts[:4]:
        if transcript.is_symlink():
            continue
        # Same "already says the right thing, leave it alone" rule the colour
        # got: this runs on every pass too, so an unguarded append grew one
        # more identical name record every time.
        if _claude_name_already_set(transcript, uuid, title):
            pushed.append("claude-transcript-name-current")
            continue
        landed, note = _append_transcript_record(transcript, name_entry)
        if note:
            warnings.append(
                f"Claude transcript name{'' if landed else ' not'} appended: {note}"
            )
        if landed:
            pushed.append("claude-transcript-name")
    # The per-PID session record names the thread in Claude's own session
    # picker. Records carry the exact sessionId, so match by content.
    records, record_warnings = _claude_session_records(
        home,
        uuid,
        environ=environ,
        max_session_records=max_session_records,
        session_directories=session_directories,
    )
    warnings.extend(record_warnings)
    for sessions, record_name, data in records:
        directory = -1
        try:
            data["name"] = title
            if data.get("nameSource") == "derived":
                data.pop("nameSource", None)
            directory = _open_sessions_directory(sessions)
            _atomic_bytes_at(
                directory,
                record_name,
                (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            pushed.append("claude-session-record")
        except OSError as exc:
            warnings.append(
                f"Claude session record not updated: {sessions / record_name}: {exc}"
            )
        finally:
            if directory >= 0:
                os.close(directory)
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
    mirror_index: bool = True,
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
    if index_name != chosen and mirror_index:
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
    # Every profile, not just the default root. A session launched on an
    # enrolled account keeps its transcript under that account's profile, so
    # the default-root glob found nothing and returned "defer" forever: the
    # untitled marker was never cleared and the one safe rename bounce was
    # never requested, which is exactly "it didn't rename the session" for a
    # window that got its name after boot and then sat idle.
    transcripts = _claude_transcripts_by_age(home, uuid)
    if not transcripts:
        return "", False
    last_prompt = 0.0
    try:
        with open(transcripts[-1], "rb") as handle:
            for raw in handle:
                try:
                    record = json.loads(raw.decode("utf-8", "strict"))
                except (UnicodeDecodeError, ValueError):
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
    push_live_rename: LiveRename,
    human_named: Callable[[Mapping[str, str]], frozenset[str]],
    name_owner: Callable[..., str],
    claim_name: Callable[..., str],
    release_claim: Callable[..., bool],
    record_pushed: Callable[..., None],
    adopt_native: Callable[..., str],
    budget_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
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

    Ownership is claimed at the thread's first turn, which is the earliest
    moment a Codex thread has an identity at all: before that turn there is
    no thread id and no first_user_message, so nothing here can see it. From
    the claim onward the thread is named, and no later pass renames it — and
    a thread a person has renamed is never a candidate in the first place.

    Nothing here is capped by count or by age any more. The old shape looked
    at the 200 most recent threads, skipped anything older than seven days,
    and stopped after twenty names and twenty repairs per pass — so a thread
    that fell past any of those edges was never named at all, while the
    Claude side named everything. The bound is now TIME: this runs on the
    path that builds a human-facing inventory, so a pass spends at most a
    couple of seconds and the next pass continues where it left off. Progress
    is durable (each name writes the index entry that excludes the thread from
    later passes), so a backlog converges instead of being abandoned.
    """
    import sqlite3

    env = environ if environ is not None else os.environ
    config = load_config()
    reconciled = reconcile_pending_titles(config, "codex", env)
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
        # No thread store at all is not a failure to report: Codex may simply
        # not be installed here. Any earlier complaint is stale, though, and a
        # doctor warning nobody can act on is its own kind of noise.
        _clear_titler_failure(env)
        return reconciled
    if budget_seconds is None:
        budget_seconds = _autotitle_budget_seconds(env)
    deadline = monotonic() + budget_seconds
    store_error = False

    def _remaining() -> float:
        return max(0.0, deadline - monotonic())

    def _set_sqlite_deadline(connection: Any) -> bool:
        remaining = _remaining()
        if remaining <= 0:
            return False
        connection.execute(f"PRAGMA busy_timeout={max(0, int(remaining * 1000))}")
        return True

    def _report_unreadable(exc: Exception) -> None:
        nonlocal store_error
        store_error = True
        # A thread store that cannot be read means no Codex session gets a
        # name, which is exactly the failure nobody could see before.
        print(
            f"session inventory: Codex thread store unreadable; "
            f"threads were not titled: {exc}",
            file=sys.stderr,
        )
        _record_titler_failure(env, f"Codex thread store unreadable: {exc}")

    remaining = _remaining()
    if remaining <= 0:
        # No budget, no work: not even the schema probe. A pass that is out of
        # time before it starts is a pass that does nothing, which is what
        # makes the bound something a caller can rely on.
        return reconciled
    try:
        connection = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=remaining
        )
        try:
            if not _set_sqlite_deadline(connection):
                return reconciled
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(threads)")
            }
            if not {"id", "title", "first_user_message", "updated_at"} <= columns:
                return reconciled
            # Only human root threads: subagent threads inherit their task
            # prompt as first_user_message, and titling them replaces useful
            # agent nicknames with near-duplicate task titles. Non-interactive
            # `codex exec` runs are one-shot jobs nobody reopens, titling
            # them only fills the index with prompt fragments.
            root_filter = ""
            if "thread_source" in columns:
                root_filter = " AND (thread_source IS NULL OR thread_source = 'user')"
            elif "agent_path" in columns:
                root_filter = " AND (agent_path IS NULL OR agent_path = '')"
            if "source" in columns:
                root_filter += " AND (source IS NULL OR source != 'exec')"
        finally:
            connection.close()
    except sqlite3.Error as exc:
        _report_unreadable(exc)
        return reconciled

    # Read a page at a time, newest first, and only while there is budget left.
    # The old shape asked for every matching row and materialized the lot
    # before the first deadline check, so on a large store the query and its
    # result set were outside the bound the pass claims to have. Keyset paging
    # (updated_at, id) keeps the order total -- a thread with no timestamp
    # sorts last rather than vanishing -- and each page is a short read.
    select = (
        "SELECT id, title, first_user_message, updated_at"
        " FROM threads"
        " WHERE first_user_message IS NOT NULL"
        " AND first_user_message != ''" + root_filter
    )

    def _page(after: tuple[float, str] | None) -> list:
        remaining = _remaining()
        if remaining <= 0:
            return []
        connection = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=remaining
        )
        try:
            if not _set_sqlite_deadline(connection):
                return []
            if after is None:
                return connection.execute(
                    select + " ORDER BY COALESCE(updated_at, 0) DESC, id DESC LIMIT ?",
                    (AUTOTITLE_PAGE_ROWS,),
                ).fetchall()
            stamp, last_id = after
            return connection.execute(
                select + " AND (COALESCE(updated_at, 0) < ?"
                " OR (COALESCE(updated_at, 0) = ? AND id < ?))"
                " ORDER BY COALESCE(updated_at, 0) DESC, id DESC LIMIT ?",
                (stamp, stamp, last_id, AUTOTITLE_PAGE_ROWS),
            ).fetchall()
        finally:
            connection.close()

    def _rows():
        after: tuple[float, str] | None = None
        while True:
            if monotonic() >= deadline:
                return
            try:
                page = _page(after)
            except sqlite3.Error as exc:
                _report_unreadable(exc)
                return
            if not page:
                return
            last = page[-1]
            after = (float(last[3] or 0), str(last[0]))
            yield from page

    rows = _rows()
    index = codex_root / "session_index.jsonl"
    # Latest entry per id wins, the file is append-ordered and renames are
    # appended, so later lines overwrite earlier ones here.
    indexed: dict[str, str] = {}
    if index.is_file() and not index.is_symlink():
        try:
            if index.stat().st_size > max_session_index_bytes:
                # Silence here looks exactly like "nothing needed a name",
                # and the effect is that no Codex session is ever named again.
                detail = (
                    "Codex session index is larger than the bounded size "
                    f"({max_session_index_bytes} bytes); threads were not titled"
                )
                print(f"session inventory: {detail}", file=sys.stderr)
                _record_titler_failure(env, detail)
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
        except OSError as exc:
            # Unable to prove which threads already carry deliberate names;
            # titling anyway could overwrite one, so do nothing, but say so,
            # because silence here looks exactly like "nothing needed a name".
            detail = f"Codex session index unreadable; threads were not titled: {exc}"
            print(f"session inventory: {detail}", file=sys.stderr)
            _record_titler_failure(env, detail)
            return reconciled
    results: list[dict[str, str]] = [
        {"uuid": str(item["uuid"]), "title": str(item["title"])} for item in reconciled
    ]
    # Read the recorded name state once for the whole pass. The settled-row
    # fast path is hot when a large indexed head precedes unnamed work, so
    # deciding it from these in-memory maps keeps the check bounded and
    # socket-free.
    human_owned = human_named(env)
    raw_aliases = config.get("aliases")
    aliases = raw_aliases if isinstance(raw_aliases, Mapping) else {}
    raw_automatic = config.get("automatic_titles")
    automatic_titles = raw_automatic if isinstance(raw_automatic, Mapping) else {}
    raw_pushed = config.get("pushed_titles")
    pushed_titles = dict(raw_pushed) if isinstance(raw_pushed, Mapping) else {}
    # Live app-server windows repaint from a socket rename; derive the
    # socket root from the caller's exact environment (no real-home
    # fallback) so sandboxed runs stay inert.
    live_state_dir: Path | None = None
    if env.get("SESSION_KIT_CODEX_LIVE_RENAME") != "0":
        live_home = env.get("HOME")
        if live_home:
            live_state_dir = _session_kit_state_dir(env, Path(live_home))
    for uuid, raw_title, first_message, updated_at in rows:
        if monotonic() >= deadline:
            # Out of budget for this pass. Everything already written stands,
            # and the next pass resumes from the newest thread still unnamed.
            break
        exact = valid_uuid(uuid)
        if not exact:
            continue
        # A settled row is safe to skip only while its native title still
        # equals the last state the kit recorded for that ownership tier. For
        # a human row that is its alias; for an automatic row it is the last
        # provider push. Ownership alone is never enough: a second /rename
        # keeps human ownership but changes the native title, and that news
        # must reach adoption before restore can replay the older alias.
        key = f"codex:{exact}"
        settled = exact in indexed and indexed[exact] == str(raw_title or "").strip()
        recorded_title = (
            aliases.get(key) if key in human_owned else pushed_titles.get(key)
        )
        if settled and recorded_title and recorded_title == indexed[exact]:
            continue
        # Rows created before pushed-title bookkeeping can still prove their
        # history without a provider write: the two native stores agree, no
        # person owns the row, and that exact title is either the retained
        # automatic title or the title this same heuristic derives today.
        # Adopt that evidence once so old stores converge onto the fast path.
        if (
            settled
            and key not in human_owned
            and not pushed_titles.get(key)
            and indexed[exact]
            and (
                automatic_titles.get(key) == indexed[exact]
                or derive_title(str(first_message or "")) == indexed[exact]
            )
        ):
            record_pushed("codex", exact, indexed[exact], environ=env)
            pushed_titles[key] = indexed[exact]
            continue
        # The index name is the deliberate one when it exists, /rename
        # writes it, and threads.title is the fallback evidence. Codex may
        # re-stamp the raw first prompt and drop its index entry; that known
        # seed is not a person renaming the thread away from the kit's push.
        native_title = indexed.get(exact) or str(raw_title or "")
        last_recorded_title = (
            aliases.get(key)
            if key in human_owned
            else pushed_titles.get(key) or automatic_titles.get(key)
        )
        adopted = ""
        if (
            last_recorded_title
            and native_title != last_recorded_title
            and (
                exact in indexed
                or not _codex_title_echoes_prompt(
                    native_title, str(first_message or "")
                )
            )
        ):
            adopted = adopt_native(
                "codex",
                exact,
                native_title,
                environ=env,
            )
        if adopted:
            results.append({"uuid": exact, "title": adopted})
            continue
        if key in human_owned:
            # Human ownership is irreversible. If this snapshot already
            # proves it and adoption found no newer native text, the later
            # repair/name paths are forbidden without another config read.
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
                index_name
                and index_name != title
                and not _codex_title_echoes_prompt(index_name, first)
                and (not title or _codex_title_echoes_prompt(title, first))
            ):
                # This is the only indexed-row path that can still write.
                # Re-read ownership at that boundary; rows with nothing to
                # heal need no per-row config read at all.
                owner = name_owner("codex", exact, environ=env)
                if owner == "human":
                    continue
                remaining = _remaining()
                if remaining > 0:
                    _push_codex_thread_title(
                        codex_root,
                        exact,
                        index_name,
                        timeout_seconds=remaining,
                    )
                remaining = _remaining()
                if live_state_dir is not None and remaining > 0:
                    push_live_rename(
                        live_state_dir,
                        exact,
                        index_name,
                        timeout_seconds=remaining,
                    )
            continue
        owner = name_owner("codex", exact, environ=env)
        if owner == "human":
            # A person renamed this thread. Neither the titler nor the healer
            # touches it again, in this pass or any pass after any restart.
            continue
        if exact == "00000000-0000-0000-0000-000000000000":
            continue
        if owner:
            # The first-turn pass already claimed this thread's name.
            continue
        title = str(raw_title or "").strip()
        first = str(first_message or "").strip()
        if title and not _codex_title_echoes_prompt(title, first):
            # A real name someone wrote; never replace it.
            continue
        derived = derive_title(first)
        if not derived:
            continue
        # The claim is the race decision, so nothing is written before it.
        # `/rename` or `sp name` landing between the ownership read above and
        # this line takes the name; the claim refuses, and refusing has to mean
        # writing nothing at all. Claiming after the index append (as this did)
        # left the person owning the name locally while the provider surfaces
        # carried the automatic one -- the two disagreeing about the same
        # session is the whole defect class this file exists to close.
        refusal = claim_name("codex", exact, environ=env)
        if refusal:
            continue

        def still_automatic() -> bool:
            return name_owner("codex", exact, environ=env) == "automatic"

        # The index is append-only, so its only safe race boundary is the
        # instant immediately before the append. A human claim that landed
        # after our automatic claim therefore suppresses this write entirely.
        if not still_automatic():
            continue
        try:
            _append_codex_index_entry(index, exact, derived[:100])
        except OSError:
            release_claim("codex", exact, environ=env)
            continue
        # The append is the titler's durable provider-side write. Remember
        # its exact value so a later settled pass can distinguish the kit's
        # own title from a native /rename without reopening this row forever.
        record_pushed("codex", exact, derived, environ=env)
        # Both writes target the exact store tree the candidates were read
        # from, pushing anywhere else would split reads and writes across
        # different Codex homes. Each is checked against the budget: one
        # SQLite update plus eight socket attempts at a two-second timeout can
        # outlast the whole pass on its own.
        remaining = _remaining()
        if remaining > 0:
            _push_codex_thread_title(
                codex_root,
                exact,
                derived,
                timeout_seconds=remaining,
                still_automatic=still_automatic,
            )
        remaining = _remaining()
        if live_state_dir is not None and remaining > 0:
            push_live_rename(
                live_state_dir,
                exact,
                derived,
                timeout_seconds=remaining,
                still_automatic=still_automatic,
            )
        results.append({"uuid": exact, "title": derived})
    if not store_error:
        _clear_titler_failure(env)
    return results


DEFAULT_AUTOTITLE_BUDGET_SECONDS = 2.0

# One page is a short read even on a large store, and small enough that the
# deadline is re-checked often.
AUTOTITLE_PAGE_ROWS = 200


def _record_titler_failure(env: Mapping[str, str], detail: str) -> None:
    """Leave a failure where a person will actually meet it.

    Printing to stderr is not enough on its own: every human-facing inventory
    build calls this through `shpool_status`, which sends both streams to
    /dev/null, so the one thing worth saying -- no Codex session can be named
    right now -- reached nobody. `session-kit doctor` reads this file.
    """
    home = env.get("HOME")
    if not home:
        return
    try:
        state = _session_kit_state_dir(env, Path(home))
        state.mkdir(parents=True, exist_ok=True)
        path = state / "codex-autotitle-error.json"
        if path.is_symlink():
            return
        payload = json.dumps(
            {
                "detail": str(detail)[:400],
                "at": dt.datetime.now(dt.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            },
            separators=(",", ":"),
        )
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    except OSError:
        return


def _clear_titler_failure(env: Mapping[str, str]) -> None:
    """A pass that read the store fine retires the last complaint."""
    home = env.get("HOME")
    if not home:
        return
    try:
        path = _session_kit_state_dir(env, Path(home)) / "codex-autotitle-error.json"
        if not path.is_symlink():
            path.unlink()
    except OSError:
        return


def _autotitle_budget_seconds(env: Mapping[str, str]) -> float:
    """How long one titling pass may spend, in seconds.

    This runs before a human-facing inventory build, so the pass has to end
    while somebody is waiting for a list. Two seconds names a large backlog
    over a handful of refreshes and is invisible on an ordinary one, where
    every recent thread already carries an index entry and the loop does
    nothing at all.
    """
    raw = str(env.get("SESSION_KIT_CODEX_AUTOTITLE_BUDGET_SECONDS", "")).strip()
    if not raw:
        return DEFAULT_AUTOTITLE_BUDGET_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_AUTOTITLE_BUDGET_SECONDS
    if value <= 0 or value > 60:
        return DEFAULT_AUTOTITLE_BUDGET_SECONDS
    return value


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
    codex_root: Path,
    uuid: str,
    title: str,
    *,
    timeout_seconds: float = 1.0,
    still_automatic: Callable[[], bool] | None = None,
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
        timeout_seconds = max(0.0, float(timeout_seconds))
        deadline = time.monotonic() + timeout_seconds

        def set_busy_deadline(connection: Any) -> bool:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                return False
            connection.execute(f"PRAGMA busy_timeout={max(0, int(remaining * 1000))}")
            return True

        connection = sqlite3.connect(database, timeout=timeout_seconds)
        try:
            if not set_busy_deadline(connection):
                return [], []
            cursor = connection.execute(
                "UPDATE threads SET title = ? WHERE id = ?", (title, uuid)
            )
            # SQLite lets us hold the UPDATE uncommitted. Re-read ownership
            # after the blocking write and finalize only while the claim is
            # still automatic; a human rename that landed meanwhile wins.
            if still_automatic is not None and not still_automatic():
                connection.rollback()
                return [], []
            if not set_busy_deadline(connection):
                connection.rollback()
                return [], []
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


def _socket_time_left(deadline: float | None, cap: float = 2.0) -> float:
    if deadline is None:
        return cap
    return min(cap, max(0.0, deadline - time.monotonic()))


def _set_socket_deadline(connection: Any, deadline: float | None) -> None:
    remaining = _socket_time_left(deadline)
    if remaining <= 0:
        raise TimeoutError("automatic-title deadline expired")
    connection.settimeout(remaining)


def _ws_send_frame(
    connection: Any,
    payload: bytes,
    opcode: int = 1,
    *,
    deadline: float | None = None,
) -> None:
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
    _set_socket_deadline(connection, deadline)
    connection.sendall(header + mask + masked)


def _ws_recv_frame(
    connection: Any, *, max_frame: int, deadline: float | None = None
) -> tuple[int, bytes]:
    import struct

    def read_exact(count: int) -> bytes:
        data = b""
        while len(data) < count:
            _set_socket_deadline(connection, deadline)
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
    deadline: float | None = None,
) -> None:
    _ws_send_frame(
        connection,
        json.dumps(
            {"method": method, "id": request_id, "params": params},
            separators=(",", ":"),
        ).encode(),
        deadline=deadline,
    )
    while True:
        opcode, payload = _ws_recv_frame(
            connection, max_frame=max_frame, deadline=deadline
        )
        if opcode == 8:
            raise OSError("WebSocket closed by server")
        if opcode == 9:
            _ws_send_frame(connection, payload, opcode=10, deadline=deadline)
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
    timeout_seconds: float | None = None,
    still_automatic: Callable[[], bool] | None = None,
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

    deadline = (
        None
        if timeout_seconds is None
        else time.monotonic() + max(0.0, float(timeout_seconds))
    )

    app_root = kit_state_dir / "app-server"
    try:
        entries = list(app_root.iterdir())
    except OSError:
        return [], []
    # Newest directory first. A socket directory outlives the session that
    # made it, so the oldest entries are the least likely to answer, and a
    # name-ordered walk (the directory names begin with their creation date)
    # reached exactly the wrong end of the list: nine dead 2026-08-04 dirs
    # consumed the cap while three live windows behind them were never
    # offered the rename. The cap below counts sockets that ACCEPTED a
    # connection, never directories considered, so any number of dead ones
    # may precede the live ones.
    ordered: list[tuple[float, Path]] = []
    for entry in entries:
        try:
            ordered.append((entry.stat().st_mtime, entry))
        except OSError:
            continue
    ordered.sort(key=lambda item: item[0], reverse=True)
    delivered = False
    connected = 0
    for _, directory in ordered:
        if connected >= max_sockets or _socket_time_left(deadline) <= 0:
            break
        socket_path = directory / "app.sock"
        try:
            if socket_path.is_symlink() or not statmod.S_ISSOCK(
                os.stat(socket_path).st_mode
            ):
                continue
        except OSError:
            continue
        connection = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        try:
            try:
                _set_socket_deadline(connection, deadline)
                connection.connect(os.fspath(socket_path))
            except OSError:
                # An abandoned socket file refuses the connection at once;
                # skipping it costs nothing and never spends the cap.
                continue
            connected += 1
            key = base64.b64encode(os.urandom(16)).decode()
            _set_socket_deadline(connection, deadline)
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
                _set_socket_deadline(connection, deadline)
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
                deadline=deadline,
            )
            _ws_send_frame(
                connection,
                json.dumps(
                    {"method": "initialized", "params": {}},
                    separators=(",", ":"),
                ).encode(),
                deadline=deadline,
            )
            # The protocol call below is irreversible. Check the canonical
            # owner at its last safe boundary so a human claim suppresses it.
            if still_automatic is not None and not still_automatic():
                continue
            _ws_request(
                connection,
                2,
                "thread/name/set",
                {"threadId": uuid, "name": title},
                max_frame=max_frame,
                deadline=deadline,
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


CODEX_LIVE_CAPABILITY_FILE = "capabilities.json"


def codex_live_capabilities(kit_state_dir: Path) -> dict[str, bool]:
    """What a live Codex app-server in this installation can be asked to do.

    The kit already renames a live window through ``thread/name/set``. A live
    RECOLOUR has no equivalent in the installed build, and the honest handling
    of that is to say so rather than to guess a method name: the caller then
    tells the person their colour lands at the window's next start, which is
    true today and stops being printed the moment the capability appears.

    The file is written by the app-server client after it negotiates
    ``initialize`` -- that client is WS-F's work. Until it exists, every
    capability reads false, which is exactly the current truth.
    """
    path = kit_state_dir / "app-server" / CODEX_LIVE_CAPABILITY_FILE
    try:
        if path.is_symlink():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): bool(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, bool)
    }


def push_codex_live_color(
    kit_state_dir: Path,
    uuid: str,
    color: str,
    *,
    capabilities: Callable[[Path], Mapping[str, bool]] = codex_live_capabilities,
    send: Callable[[Path, str, str], tuple[list[str], list[str]]] | None = None,
) -> tuple[list[str], list[str]]:
    """Repaint a live Codex window in a new colour, when that becomes possible.

    Codex takes its theme from the command line at launch, so today a recolour
    reaches a window only through the one safe provider restart. The seam is
    here so the recolour path has one place to ask, and one answer that is
    never a false success: a warning naming the real reason, which the caller
    turns into the line the person reads.
    """
    exact = valid_uuid(uuid)
    if not exact or not color:
        return [], ["invalid Codex live recolor request"]
    if not capabilities(kit_state_dir).get("theme_set"):
        return [], [
            "Codex has no live theme control in this build; "
            "the color applies when the window next starts"
        ]
    if send is None:
        return [], [
            "Codex live theme control is available but no app-server client is wired"
        ]
    return send(kit_state_dir, exact, color)


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


def _claude_last_record_value(
    transcript: Path, uuid: str, kind: str, field: str
) -> str | None:
    """The value this transcript's LAST `kind` record carries for this uuid.

    The last one is what counts: Claude replays the records in order, so a
    later record overrides an earlier one. Any unreadable or malformed line
    is skipped rather than treated as an answer, and a transcript that cannot
    be read at all returns None -- every caller reads that as "no answer" and
    falls through to the append, which is the behaviour that existed before
    the guard.

    The file is read as BYTES and each candidate line is decoded on its own.
    Text mode raises UnicodeDecodeError -- a ValueError, not an OSError --
    from the iteration itself, so one half-written multi-byte character at
    the end of a transcript a live provider is writing (the normal shape of a
    killed writer) escaped this function, its caller, and the hydration loop
    that walks every session, and silently stopped colour and name for every
    session in the pass. Decoding per line keeps one torn line from costing
    the other forty thousand.
    """
    current: str | None = None
    marker = f'"{kind}"'.encode()
    try:
        with open(transcript, "rb") as handle:
            for raw in handle:
                if marker not in raw:
                    continue
                try:
                    item = json.loads(raw.decode("utf-8", "strict"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if (
                    isinstance(item, Mapping)
                    and item.get("type") == kind
                    and item.get("sessionId") == uuid
                ):
                    value = item.get(field)
                    current = value if isinstance(value, str) else None
    except OSError:
        return None
    return current


def _claude_color_already_set(transcript: Path, uuid: str, color: str) -> bool:
    """Whether this transcript's LAST color record already says `color`."""
    return (
        _claude_last_record_value(transcript, uuid, "agent-color", "agentColor")
        == color
    )


def _claude_name_already_set(transcript: Path, uuid: str, name: str) -> bool:
    """Whether this transcript's LAST name record already says `name`."""
    return (
        _claude_last_record_value(transcript, uuid, "agent-name", "agentName") == name
    )


# How long a transcript must sit untouched before an unterminated last line
# counts as wreckage left by a killed writer rather than a record being
# written this instant.
TRANSCRIPT_QUIET_SECONDS = 30.0


def _write_whole_line(descriptor: int, payload: bytes) -> bool:
    """One O_APPEND write. True when the record ended up on its own line.

    O_APPEND makes the kernel place the whole buffer contiguously at the end
    of the file, so this record can never be cut in half by another writer.
    Where it landed is read back from the file offset, which O_APPEND leaves
    at the end of OUR bytes -- so it stays true even if the provider appends
    again immediately afterwards.

    A payload that already begins with a newline starts a line whatever
    precedes it, so it is standalone by construction; anything else has to be
    proven by the byte in front of it.
    """
    written = os.write(descriptor, payload)
    if written != len(payload):
        raise OSError(f"short write ({written} of {len(payload)} bytes)")
    start = os.lseek(descriptor, 0, os.SEEK_CUR) - written
    if os.pread(descriptor, written, start) != payload:
        raise OSError("the record did not read back as it was written")
    if payload.startswith(b"\n") or start == 0:
        return True
    return os.pread(descriptor, 1, start - 1) == b"\n"


def _append_transcript_record(transcript: Path, entry: str) -> tuple[bool, str]:
    """Append one whole JSONL record. Returns (it landed, what to say).

    Appending is the only safe edit to a transcript a live provider owns, and
    the danger is not the write being torn -- O_APPEND cannot tear it -- but
    WHERE it lands. A provider writes a record; if it does that in more than
    one write, there is an instant when the file ends part-way through its
    record, and a record appended in that instant lands INSIDE the provider's,
    making one invalid line out of two records. The old code checked the tail
    and then wrote, and a provider that began a record between those two steps
    got its record destroyed while the caller was told the colour had landed.

    Nothing closes that window from this side: the provider does not
    coordinate, and there is no check-and-append syscall. What is guaranteed
    instead:

    * a record already visibly in flight is never written onto at all;
    * where the write lands is VERIFIED, not assumed;
    * if it landed inside another record anyway, the colour is written again
      behind a leading newline, which starts a line whatever precedes it, so
      OUR record always ends up whole and readable;
    * success is reported only for a record read back from the file;
    * and damage to the provider's record is said out loud instead of being
      swallowed.
    """
    payload = (entry + "\n").encode("utf-8")
    try:
        descriptor = os.open(transcript, os.O_RDWR | os.O_APPEND)
    except OSError as exc:
        return False, str(exc)
    try:
        status = os.fstat(descriptor)
        if not statmod.S_ISREG(status.st_mode):
            return False, "not a regular file"
        if status.st_size and os.pread(descriptor, 1, status.st_size - 1) != b"\n":
            if time.time() - status.st_mtime < TRANSCRIPT_QUIET_SECONDS:
                # A record is visibly in flight. Refuse: the next pass tries
                # again, and a colour that arrives a few seconds later beats
                # one that costs the provider an event.
                return False, "a record is being written to this transcript right now"
            # Untouched long enough that the half-line is wreckage from a
            # killed writer, not work in progress. The leading newline closes
            # it, so the file is never left worse than it was found.
            payload = b"\n" + payload
        if not _write_whole_line(descriptor, payload):
            # The provider began a record between the check above and the
            # write, and this one landed inside it. Both records are now one
            # invalid line. Write the colour again behind a leading newline --
            # that starts a line no matter what precedes it, so this copy is
            # whole and is the one every reader will find.
            _write_whole_line(descriptor, b"\n" + payload)
            os.fsync(descriptor)
            return True, (
                "a Claude record was being written at the same instant and was "
                "damaged; the record was written again on its own line"
            )
        os.fsync(descriptor)
    except OSError as exc:
        return False, str(exc)
    finally:
        os.close(descriptor)
    return True, ""


def _push_claude_color(
    home: Path, uuid: str, color: str
) -> tuple[list[str], list[str]]:
    """Write the exact agent-color record /color itself writes, once.

    Claude Code reads it at session start/resume; nothing is ever typed into
    a live terminal. Missing transcripts fail open.

    Written once per conversation, not once per pass. This runs on every
    attach (`sk_push_session_color`), and it used to append unconditionally,
    so a session a person opened eight times carried eight identical color
    records and grew one more on every reattach. Claude honours the last
    record either way, so the repeats never changed what was shown -- they
    only grew a file the kit does not own and cannot compact. A record that
    already says the right thing is left alone; a DIFFERENT color still
    appends, because that is a real change and appending is the only safe
    edit to a transcript a live provider is writing to.
    """
    transcripts = _claude_transcripts(home, uuid)
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
        if _claude_color_already_set(transcript, uuid, color):
            pushed.append("claude-transcript-color-current")
            continue
        landed, note = _append_transcript_record(transcript, entry)
        if note:
            warnings.append(
                f"Claude transcript{'' if landed else ' not'} appended: {note}"
            )
        if landed:
            pushed.append("claude-transcript-color")
    return pushed, warnings
