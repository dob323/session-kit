"""Read-only Claude Code state readers: agent records and transcripts."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import attention as _attention
from .common import CollectionError, clean_text, valid_uuid
from .transcripts import claude_roots


MAX_CLAUDE_TITLE_SCAN_BYTES = 64 * 1024
MAX_CLAUDE_QUESTION_TAIL_BYTES = 256 * 1024
# How many copies of one conversation are read, newest first.
MAX_CLAUDE_TITLE_TRANSCRIPTS = 4


def _parse_claude_payload(
    payload: Any,
    *,
    palette: Sequence[str],
    attention_records: Mapping[str, Mapping[str, Any]] | None = None,
    attention_source: str = "poll",
) -> list[dict[str, Any]]:
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
        status, needs_you = _attention.poll_attention(raw_status, waiting_for)
        # The poll says what was true when it ran; the Notification hook said
        # so at the moment it happened. Whichever is newer decides -- and the
        # poll's age is the vendor's own statusUpdatedAt, attached during
        # enrichment, never the time we ran the command.
        stamp = item.get("_session_kit_status_updated_at")
        record = (attention_records or {}).get(uuid)
        decided = _attention.merge(
            poll_status=status,
            poll_needs_you=needs_you,
            poll_stamp_ms=stamp if isinstance(stamp, int) and stamp > 0 else None,
            record=record,
            source=attention_source,
        )
        status = decided["agent_status"]
        needs_you = decided["needs_you"]
        permission_recorded = (
            record.get("recorded_at_ms") if isinstance(record, Mapping) else None
        )
        pending_tool_at = item.get("_session_kit_pending_tool_use_at_unix_ms")
        correlated_permission = bool(
            decided.get("notification_type") == "permission_prompt"
            and isinstance(permission_recorded, int)
            and not isinstance(permission_recorded, bool)
            and isinstance(pending_tool_at, int)
            and not isinstance(pending_tool_at, bool)
            and pending_tool_at <= permission_recorded <= pending_tool_at + 30_000
        )
        blocking_question = bool(
            item.get("_session_kit_pending_ask_user_question")
        ) or bool(
            needs_you
            and correlated_permission
            and item.get("_session_kit_pending_tool_use")
        )
        result.append(
            {
                "pid": pid,
                "attention_source": decided["attention_source"],
                "uuid": uuid,
                "cwd": clean_text(item.get("cwd"), 4096),
                "kind": clean_text(item.get("kind"), 40),
                "started_at_unix_ms": item.get("startedAt")
                if isinstance(item.get("startedAt"), int)
                else None,
                "title": clean_text(item.get("name"), 120),
                "ai_title": clean_text(item.get("aiTitle"), 120),
                "agent_name": clean_text(item.get("agentName"), 120),
                "agent_color": item.get("agentColor")
                if item.get("agentColor") in palette
                else "",
                "name_source": clean_text(item.get("nameSource"), 20),
                "status": status or "unknown",
                "needs_you": needs_you,
                "blocking_question": blocking_question,
            }
        )
    return result


def _record_time_ms(record: Mapping[str, Any]) -> int | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _pending_claude_tool_evidence(raw: bytes) -> tuple[bool, bool, int | None]:
    """Whether the bounded tail holds an unanswered picker or tool request.

    Claude writes an assistant ``tool_use`` with an exact id, followed by a
    user ``tool_result`` carrying that id.  The same pair is present for a
    tool waiting at the permission gate; the live attention reading is what
    separates that wait from a tool which is merely still running.
    """
    pending: dict[str, tuple[str, int | None]] = {}
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(record, Mapping):
            continue
        # Subagent/sidechain records can be embedded beside the parent turn;
        # only the interactive conversation can be blocking this row.
        if record.get("isSidechain") is not False:
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "tool_use":
                # Only an assistant turn can open a real tool request. A
                # malformed user record carrying tool-use-shaped content must
                # not invent a visible picker or permission question.
                if (
                    record.get("type") != "assistant"
                    or message.get("role") != "assistant"
                ):
                    continue
                tool_id = part.get("id")
                name = part.get("name")
                if isinstance(tool_id, str) and tool_id and isinstance(name, str):
                    pending[tool_id] = (name, _record_time_ms(record))
            elif part.get("type") == "tool_result":
                tool_id = part.get("tool_use_id")
                if isinstance(tool_id, str):
                    pending.pop(tool_id, None)
    timestamps = [stamp for _, stamp in pending.values() if stamp is not None]
    return (
        any(name == "AskUserQuestion" for name, _ in pending.values()),
        bool(pending),
        max(timestamps) if timestamps else None,
    )


def _pending_claude_tools(raw: bytes) -> tuple[bool, bool]:
    """Compatibility-sized answer used by focused transcript tests."""
    ask, pending, _ = _pending_claude_tool_evidence(raw)
    return ask, pending


def read_claude_transcript_signals(
    uuid: str,
    home: Path | None = None,
    *,
    environ: Mapping[str, str],
    home_factory: Callable[[], Path],
    palette: Sequence[str],
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Return Claude's persisted per-conversation title and color evidence.

    The TUI stores its conversation auto-title as an ``ai-title`` transcript
    record and its session color as an ``agent-color`` record — neither lives
    in the session record, so the derived window label and the visible
    conversation state can disagree. Reads are bounded to the transcript's
    head and tail; the last record of each kind wins. Any problem returns
    empty evidence.

    Every profile is searched, not just the default one. A session launched
    on an enrolled account runs with CLAUDE_CONFIG_DIR set, so its transcript
    lives under that profile and the default root does not have it — this read
    returned "no title, no color" for those sessions and every caller treated
    that as "the provider never set one". The write side already searches all
    roots (`names_push._claude_transcripts`, fixed for the same reason); this
    is that fix on the read side. An explicit `config_dir` still pins the
    search to exactly one root. When the same conversation exists in more
    than one profile the NEWEST copy is believed, never the one whose alias
    happens to sort last.
    """
    signals: dict[str, Any] = {
        "ai_title": "",
        "agent_name": "",
        "agent_color": "",
        "pending_ask_user_question": False,
        "pending_tool_use": False,
        "pending_tool_use_at_unix_ms": None,
    }
    exact_uuid = valid_uuid(uuid)
    if not exact_uuid:
        return signals
    base = home if home is not None else Path(environ.get("HOME") or home_factory())
    if config_dir is not None:
        roots = [config_dir]
    else:
        search_env = {**environ, "HOME": os.fspath(base)}
        if home is not None:
            # An explicit home means "search THAT tree". The caller's own
            # CLAUDE_CONFIG_DIR describes the process doing the reading, not
            # the session being read, so it is not a root here.
            search_env.pop("CLAUDE_CONFIG_DIR", None)
        roots = claude_roots(search_env) or [base / ".claude"]
    # One conversation can exist in more than one profile — an account switch
    # copies it — and "the last record wins" over a root list in alias order
    # handed the answer to whichever profile sorted last, so a stale copy
    # could out-rank the file the live session is writing, and a fixed cut-off
    # taken in that order could drop the current profile entirely. Order by
    # recency instead (the rule `transcripts._claude_transcript` already uses
    # for this same question) and keep the NEWEST few, so the cap can never
    # hide the file that is actually in use.
    dated: list[tuple[float, str, Path]] = []
    for claude_root in roots:
        try:
            found = list((claude_root / "projects").glob(f"*/{exact_uuid}.jsonl"))
        except OSError:
            continue
        for path in found:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                dated.append((path.stat().st_mtime, os.fspath(path), path))
            except OSError:
                continue
    dated.sort()
    transcripts = [path for _, _, path in dated[-MAX_CLAUDE_TITLE_TRANSCRIPTS:]]
    for transcript_index, transcript in enumerate(transcripts):
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
                question_tail = b""
                if transcript_index == len(transcripts) - 1:
                    offset = max(0, size - MAX_CLAUDE_QUESTION_TAIL_BYTES)
                    handle.seek(offset)
                    question_tail = handle.read(MAX_CLAUDE_QUESTION_TAIL_BYTES)
                    if offset:
                        boundary = question_tail.find(b"\n")
                        question_tail = (
                            question_tail[boundary + 1 :] if boundary >= 0 else b""
                        )
        except OSError:
            continue
        if transcript_index == len(transcripts) - 1:
            ask, pending_tool, tool_at = _pending_claude_tool_evidence(question_tail)
            signals["pending_ask_user_question"] = ask
            signals["pending_tool_use"] = pending_tool
            signals["pending_tool_use_at_unix_ms"] = tool_at
        for chunk in chunks:
            for line in chunk.split(b"\n"):
                if not any(
                    marker in line
                    for marker in (b'"ai-title"', b'"agent-name"', b'"agent-color"')
                ):
                    continue
                try:
                    record = json.loads(line.decode("utf-8", "strict"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if (
                    not isinstance(record, Mapping)
                    or record.get("sessionId") != exact_uuid
                ):
                    continue
                if record.get("type") == "ai-title":
                    candidate = clean_text(record.get("aiTitle"), 120)
                    if candidate:
                        signals["ai_title"] = candidate
                elif record.get("type") == "agent-name":
                    candidate = clean_text(record.get("agentName"), 120)
                    if candidate:
                        signals["agent_name"] = candidate
                elif record.get("type") == "agent-color":
                    color = record.get("agentColor")
                    if isinstance(color, str) and color in palette:
                        signals["agent_color"] = color
    return signals


def read_claude_ai_title(
    uuid: str,
    home: Path | None = None,
    *,
    transcript_signals: Callable[[str, Path | None], dict[str, str]],
) -> str:
    """Compatibility wrapper: the auto-title half of the transcript signals."""
    return transcript_signals(uuid, home)["ai_title"]


def _enrich_claude_payload(
    payload: Any,
    *,
    environ: Mapping[str, str],
    home_factory: Callable[[], Path],
    palette: Sequence[str],
    transcript_signals: Callable[[str, Path | None], dict[str, str]],
) -> Any:
    """Attach per-session nameSource and ai-title evidence for the collector."""
    if not isinstance(payload, list):
        return payload
    home = Path(environ.get("HOME") or home_factory())
    default_root = home / ".claude"
    for item in payload:
        if not isinstance(item, dict):
            continue
        pid = item.get("pid")
        uuid = valid_uuid(item.get("sessionId"))
        if not isinstance(pid, int) or pid <= 0 or not uuid:
            continue
        raw_root = item.get("_session_kit_claude_config_dir")
        claude_root = (
            Path(raw_root)
            if isinstance(raw_root, str) and Path(raw_root).is_absolute()
            else default_root
        )
        record_path = claude_root / "sessions" / f"{pid}.json"
        if record_path.is_file():
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = None
            if isinstance(record, Mapping):
                if "nameSource" not in item:
                    item["nameSource"] = record.get("nameSource")
                # When Claude last changed this session's status, by its own
                # clock. It is the only way to tell a poll answer that is
                # older than a hook record from one that supersedes it; a
                # session record without it simply leaves the comparison
                # undecided (see attention.merge).
                stamp = record.get("statusUpdatedAt")
                if isinstance(stamp, int) and stamp > 0:
                    item["_session_kit_status_updated_at"] = stamp
        needs_title = not item.get("aiTitle")
        needs_name = not item.get("agentName")
        needs_color = item.get("agentColor") not in palette
        # Blocking state is live transcript evidence, so it is read on every
        # pass even when all three cosmetic fields are already populated.
        if claude_root == default_root:
            signals = transcript_signals(uuid, home)
        else:
            signals = read_claude_transcript_signals(
                uuid,
                home,
                environ=environ,
                home_factory=home_factory,
                palette=palette,
                config_dir=claude_root,
            )
        if needs_title or needs_name or needs_color:
            if needs_title:
                item["aiTitle"] = signals["ai_title"]
            if needs_name:
                item["agentName"] = signals["agent_name"]
            if needs_color:
                item["agentColor"] = signals["agent_color"]
        item["_session_kit_pending_ask_user_question"] = bool(
            signals.get("pending_ask_user_question")
        )
        item["_session_kit_pending_tool_use"] = bool(
            signals.get("pending_tool_use")
        )
        item["_session_kit_pending_tool_use_at_unix_ms"] = signals.get(
            "pending_tool_use_at_unix_ms"
        )
    return payload


def claude_subagents(
    root_uuid: str,
    process_table: Mapping[int, Mapping[str, Any]],
    *,
    arg_value: Callable[[Sequence[str], str], str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for pid, process in process_table.items():
        argv = process.get("cmdline") or []
        if arg_value(argv, "--parent-session-id").lower() != root_uuid:
            continue
        title = clean_text(arg_value(argv, "--agent-name"), 80) or f"PID {pid}"
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


def _is_native_claude(process: Mapping[str, Any]) -> bool:
    argv = process.get("cmdline") or []
    executable = Path(str(argv[0])).name if argv else ""
    comm = clean_text(process.get("comm"), 128)
    return executable == "claude" or comm in {"claude", "claude.exe"}


def _native_claude_uuid(
    process: Mapping[str, Any],
    *,
    is_native_claude: Callable[[Mapping[str, Any]], bool],
    arg_value: Callable[[Sequence[str], str], str],
) -> str | None:
    if not is_native_claude(process):
        return None
    argv = list(process.get("cmdline") or [])
    if "--parent-session-id" in argv or "--fork-session" in argv:
        return None
    candidate = (
        arg_value(argv, "--session-id")
        or arg_value(argv, "--resume")
        or str(process.get("claude_session_id", ""))
    )
    return valid_uuid(candidate)
