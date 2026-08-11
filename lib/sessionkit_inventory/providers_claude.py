"""Read-only Claude Code state readers: agent records and transcripts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .common import CollectionError, clean_text, valid_uuid


MAX_CLAUDE_TITLE_SCAN_BYTES = 64 * 1024


def _parse_claude_payload(
    payload: Any,
    *,
    palette: Sequence[str],
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
        needs_you = bool(waiting_for) or raw_status in {
            "waiting",
            "needs_input",
            "needs your reply",
        }
        status = (
            "needs your reply"
            if needs_you
            else ("working" if raw_status == "busy" else raw_status)
        )
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
                "agent_name": clean_text(item.get("agentName"), 120),
                "agent_color": item.get("agentColor")
                if item.get("agentColor") in palette
                else "",
                "name_source": clean_text(item.get("nameSource"), 20),
                "status": status or "unknown",
                "needs_you": needs_you,
            }
        )
    return result


def read_claude_transcript_signals(
    uuid: str,
    home: Path | None = None,
    *,
    environ: Mapping[str, str],
    home_factory: Callable[[], Path],
    palette: Sequence[str],
    config_dir: Path | None = None,
) -> dict[str, str]:
    """Return Claude's persisted per-conversation title and color evidence.

    The TUI stores its conversation auto-title as an ``ai-title`` transcript
    record and its session color as an ``agent-color`` record — neither lives
    in the session record, so the derived window label and the visible
    conversation state can disagree. Reads are bounded to the transcript's
    head and tail; the last record of each kind wins. Any problem returns
    empty evidence.
    """
    signals = {"ai_title": "", "agent_name": "", "agent_color": ""}
    exact_uuid = valid_uuid(uuid)
    if not exact_uuid:
        return signals
    base = home if home is not None else Path(environ.get("HOME") or home_factory())
    claude_root = config_dir if config_dir is not None else base / ".claude"
    projects = claude_root / "projects"
    try:
        transcripts = sorted(projects.glob(f"*/{exact_uuid}.jsonl"))
    except OSError:
        return signals
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
        if "nameSource" not in item and record_path.is_file():
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                if isinstance(record, Mapping):
                    item["nameSource"] = record.get("nameSource")
            except (OSError, ValueError):
                pass
        needs_title = not item.get("aiTitle")
        needs_name = not item.get("agentName")
        needs_color = item.get("agentColor") not in palette
        if needs_title or needs_name or needs_color:
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
            if needs_title:
                item["aiTitle"] = signals["ai_title"]
            if needs_name:
                item["agentName"] = signals["agent_name"]
            if needs_color:
                item["agentColor"] = signals["agent_color"]
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
