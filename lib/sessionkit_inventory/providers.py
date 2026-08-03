"""Provider-specific structured state readers."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping


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
        # signal only — never the hard "needs your reply".
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
