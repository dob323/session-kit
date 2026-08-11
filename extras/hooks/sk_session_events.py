#!/usr/bin/env python3
"""Fail-open Claude hook payload for Session Kit's private event store.

Two jobs, both advisory and both silent. Every mapped hook event becomes one
line in the event store. `UserPromptSubmit` additionally offers the prompt to
the intake producer, which records a project the first time a root thread
states one — that is how a project reaches the fleet supervisor with no agent
ever messaging it, so this hook must be registered for `UserPromptSubmit` as
well as the four event kinds.

Nothing here may cost the human's prompt anything: the producer writes a few
small files, asks for a supervisor by starting one detached, and any failure
at all exits 0 with the prompt untouched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import sys
import time
from typing import Any, Mapping


MAX_HOOK_PAYLOAD_BYTES = 1024 * 1024
STDIN_DEADLINE_SECONDS = 2.0
STORE_LOCK_TIMEOUT_SECONDS = 0.25


def _kit_lib() -> Path:
    kit_lib = Path(__file__).resolve().parents[2] / "lib"
    if os.fspath(kit_lib) not in sys.path:
        sys.path.insert(0, os.fspath(kit_lib))
    return kit_lib


def _load_writer() -> Any:
    _kit_lib()
    from sessionkit_events import append_event, thread_key

    return append_event, thread_key


def _load_producer() -> Any:
    _kit_lib()
    from sessionkit_supervisor import intake

    return intake


def _state_dir(environ: Mapping[str, str]) -> Path:
    explicit = environ.get("SESSION_KIT_STATE_DIR") or environ.get("SK_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    home = Path(environ.get("HOME", os.fspath(Path.home()))).expanduser()
    xdg = Path(environ.get("XDG_STATE_HOME", os.fspath(home / ".local" / "state")))
    return xdg.expanduser() / "session-kit"


def _question(payload: Mapping[str, Any]) -> str | None:
    for field in ("message", "question", "prompt"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _mapped_event(payload: Mapping[str, Any]) -> tuple[str, str | None] | None:
    hook_name = str(payload.get("hook_event_name") or "").casefold()
    if hook_name == "notification":
        notification_type = str(payload.get("notification_type") or "").casefold()
        event = "permission_prompt" if "permission" in notification_type else "needs_input"
        return event, _question(payload)
    if hook_name == "stop":
        return "turn_done", None
    if hook_name == "sessionstart":
        return "session_start", None
    if hook_name == "sessionend":
        return "session_end", None
    return None


def produce_intake(
    payload: Mapping[str, Any], *, environ: Mapping[str, str] = os.environ
) -> Any:
    """Offer one submitted prompt to the intake producer.

    The producer decides everything: whether this is a root thread rather than
    a subagent, whether the prompt is a project rather than a slash command or
    a greeting, and whether this thread already has an intake. It is called on
    every prompt precisely so no first prompt can be missed; all but the first
    are recognised and cost two file reads.
    """
    intake = _load_producer()
    return intake.from_hook(
        {
            "provider": "claude",
            "session_id": payload.get("session_id") or payload.get("sessionId"),
            "turn_id": payload.get("turn_id") or payload.get("turnId"),
            "prompt": payload.get("prompt"),
            "cwd": payload.get("cwd"),
            "transcript_path": payload.get("transcript_path")
            or payload.get("transcriptPath"),
            **{
                marker: payload.get(marker)
                for marker in intake.SIDECHAIN_MARKERS
                if marker in payload
            },
        },
        state_dir=_state_dir(environ),
        environ=environ,
    )


def process_payload(
    payload: Mapping[str, Any], *, environ: Mapping[str, str] = os.environ
) -> None:
    if str(payload.get("hook_event_name") or "").casefold() == "userpromptsubmit":
        produce_intake(payload, environ=environ)
        return
    mapped = _mapped_event(payload)
    if mapped is None:
        return
    session_id = payload.get("session_id") or payload.get("sessionId")
    append_event, compose_thread_key = _load_writer()
    key = compose_thread_key("claude", session_id)
    event, question = mapped
    append_event(
        _state_dir(environ),
        key,
        event,
        question=question,
        source="hook",
        lock_timeout_seconds=STORE_LOCK_TIMEOUT_SECONDS,
    )


def _read_stdin() -> bytes | None:
    descriptor = sys.stdin.fileno()
    deadline = time.monotonic() + STDIN_DEADLINE_SECONDS
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([descriptor], [], [], remaining)
        if not ready:
            return None
        chunk = os.read(
            descriptor,
            min(65536, MAX_HOOK_PAYLOAD_BYTES + 1 - total),
        )
        if not chunk:
            return b"".join(chunks) if chunks else None
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_HOOK_PAYLOAD_BYTES:
            return None


def main() -> int:
    try:
        raw = _read_stdin()
        if raw is None:
            return 0
        payload = json.loads(raw)
        if isinstance(payload, Mapping):
            process_payload(payload)
    except BaseException:
        # Claude hooks are advisory. Event storage must never impede a session.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
