"""Bounded provider JSONL idle detection adapted from Maniple.

Source: github.com/Martian-Engineering/maniple
Commit: 0987ccf59552989600f6134e6602abe72a3214d0
License: MIT, per the source project's pyproject.toml.

The supervisor's authoritative status is Session Kit ``agent_status``. These
helpers retain Maniple's provider-JSONL detectors for transcript diagnostics;
they are bounded and fail to ``working`` when evidence is absent or malformed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping


MAX_IDLE_TAIL_BYTES = 256 * 1024
MARKER = re.compile(r"\[(?:worker-done|maniple-worker-done):([^\]]+)\]")
DEFAULT_TIMEOUT = 600.0
DEFAULT_POLL_INTERVAL = 2.0


def _records(path: Path) -> list[Mapping[str, Any]]:
    if not path.is_file() or path.is_symlink():
        return []
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            offset = max(0, size - MAX_IDLE_TAIL_BYTES)
            handle.seek(offset)
            raw = handle.read(MAX_IDLE_TAIL_BYTES)
    except OSError:
        return []
    if offset:
        boundary = raw.find(b"\n")
        raw = raw[boundary + 1 :] if boundary >= 0 else b""
    result: list[Mapping[str, Any]] = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, Mapping):
            result.append(value)
    return result


def is_claude_idle(path: Path, session_id: str) -> bool:
    """A matching stop-hook marker must follow the last user/assistant record."""
    last_message = -1
    last_marker = -1
    for index, record in enumerate(_records(path)):
        if record.get("type") in {"user", "assistant"}:
            last_message = index
        if record.get("type") != "system" or record.get("subtype") != "stop_hook_summary":
            continue
        infos = record.get("hookInfos")
        if not isinstance(infos, list):
            continue
        for info in infos:
            if not isinstance(info, Mapping):
                continue
            command = info.get("command")
            if not isinstance(command, str):
                continue
            match = MARKER.search(command)
            if match and match.group(1) == session_id:
                last_marker = index
    return last_marker >= 0 and last_marker >= last_message


def is_codex_idle(path: Path) -> bool:
    """Return true only when the last lifecycle evidence finishes a turn."""
    state = "working"
    decisive = False
    for record in _records(path):
        event_type = record.get("type")
        payload = record.get("payload")
        if event_type == "event_msg" and isinstance(payload, Mapping):
            kind = payload.get("type")
            if kind in {"task_started", "user_message"}:
                state, decisive = "working", True
            elif kind in {"task_complete", "turn_aborted", "agent_message"}:
                state, decisive = "idle", True
        elif event_type == "response_item" and isinstance(payload, Mapping):
            if payload.get("type") == "message":
                role = payload.get("role")
                if role == "user":
                    state, decisive = "working", True
                elif role == "assistant":
                    state, decisive = "idle", True
        elif event_type in {"turn.started"}:
            state, decisive = "working", True
        elif event_type in {"turn.completed", "turn.failed"}:
            state, decisive = "idle", True
    return decisive and state == "idle"


def provider_jsonl_idle(path: Path, provider: str, session_id: str = "") -> bool:
    return (
        is_codex_idle(path)
        if provider == "codex"
        else is_claude_idle(path, session_id)
        if provider == "claude"
        else False
    )


def is_idle(jsonl_path: Path, session_id: str) -> bool:
    """Maniple-compatible name for Claude stop-hook detection."""
    return is_claude_idle(jsonl_path, session_id)


@dataclass(frozen=True)
class SessionInfo:
    """Provider transcript coordinates used by the compatible wait helpers."""

    jsonl_path: Path
    session_id: str
    agent_type: str = "claude"

    def idle(self) -> bool:
        return provider_jsonl_idle(self.jsonl_path, self.agent_type, self.session_id)


async def wait_for_idle(
    jsonl_path: Path,
    session_id: str,
    timeout: float = DEFAULT_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> dict[str, Any]:
    """Bounded compatibility helper for a single Claude transcript."""
    started = time.monotonic()
    while True:
        idle = is_claude_idle(jsonl_path, session_id)
        elapsed = time.monotonic() - started
        if idle or elapsed >= timeout:
            return {
                "idle": idle,
                "session_id": session_id,
                "waited_seconds": elapsed,
                "timed_out": not idle,
            }
        await asyncio.sleep(min(poll_interval, max(0.0, timeout - elapsed)))


async def wait_for_any_idle(
    sessions: list[SessionInfo],
    timeout: float = DEFAULT_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> dict[str, Any]:
    """Bounded compatibility helper that returns on the first idle worker."""
    started = time.monotonic()
    while True:
        idle_session = next((session for session in sessions if session.idle()), None)
        elapsed = time.monotonic() - started
        if idle_session is not None or elapsed >= timeout:
            return {
                "idle_session_id": idle_session.session_id if idle_session else None,
                "idle": idle_session is not None,
                "waited_seconds": elapsed,
                "timed_out": idle_session is None,
            }
        await asyncio.sleep(min(poll_interval, max(0.0, timeout - elapsed)))


async def wait_for_all_idle(
    sessions: list[SessionInfo],
    timeout: float = DEFAULT_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> dict[str, Any]:
    """Bounded compatibility helper that returns when the full cohort is idle."""
    started = time.monotonic()
    while True:
        idle_ids = [session.session_id for session in sessions if session.idle()]
        waiting = [
            session.session_id for session in sessions if session.session_id not in idle_ids
        ]
        elapsed = time.monotonic() - started
        if not waiting or elapsed >= timeout:
            return {
                "idle_session_ids": idle_ids,
                "all_idle": not waiting,
                "waiting_on": waiting,
                "waited_seconds": elapsed,
                "timed_out": bool(waiting),
            }
        await asyncio.sleep(min(poll_interval, max(0.0, timeout - elapsed)))
