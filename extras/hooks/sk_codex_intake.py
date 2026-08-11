#!/usr/bin/env python3
"""Fail-open Codex `UserPromptSubmit` hook: a project, taken from the prompt.

Codex discovers user-level hooks in `~/.codex/hooks.json`, and a user-level
hook loads even in a project that was never trusted — which is what makes this
the generic automatic path rather than a per-repository opt-in. The hook is
invoked as a command with its JSON payload on **stdin**, carrying
`session_id`, `turn_id`, `transcript_path`, `cwd`, `model`, and `prompt`:
everything the producer needs, in front of the agent's turn.

`session-kit enable-intake` writes the `hooks.json` entry that registers this
file; `lib/sessionkit_supervisor/hooks.py` holds the exact document.

TRUSTED BY EXACT HASH — read this before editing
------------------------------------------------
Codex trusts a non-managed command hook by the hash of the file it was shown.
Editing this script changes that hash, and a changed hook is SKIPPED until it
is trusted again. A silently skipped hook is precisely the false negative this
whole path exists to remove, so every release that changes this file must
surface the re-trust step (`docs/install.md`, `docs/update-and-rollback.md`).

WHAT IS PROVEN HERE, AND WHAT IS NOT
------------------------------------
The tests feed this script payloads directly and prove its logic: one intake
per root thread, amendments for later prompts, nothing for a subagent, nothing
for a malformed payload, and a prompt that is never delayed. They do NOT prove
that a live Codex process discovers, trusts, and invokes it — no Codex runs in
this suite. That fresh-process proof belongs to the end-to-end drill.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import sys
import time
from typing import Any, Mapping


MAX_PAYLOAD_BYTES = 1024 * 1024
STDIN_DEADLINE_SECONDS = 2.0
PROMPT_EVENT = "userpromptsubmit"


def _load_producer() -> Any:
    kit_lib = Path(__file__).resolve().parents[2] / "lib"
    if os.fspath(kit_lib) not in sys.path:
        sys.path.insert(0, os.fspath(kit_lib))
    from sessionkit_supervisor import intake

    return intake


def _state_dir(environ: Mapping[str, str]) -> Path:
    explicit = environ.get("SESSION_KIT_STATE_DIR") or environ.get("SK_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    home = Path(environ.get("HOME", os.fspath(Path.home()))).expanduser()
    xdg = Path(environ.get("XDG_STATE_HOME", os.fspath(home / ".local" / "state")))
    return xdg.expanduser() / "session-kit"


def _folded(value: object) -> str:
    return "".join(
        character for character in str(value or "") if character.isalnum()
    ).casefold()


def process_payload(
    payload: Mapping[str, Any], *, environ: Mapping[str, str] = os.environ
) -> Any:
    """Offer one submitted Codex prompt to the producer.

    The event name is checked when the payload names one and assumed otherwise:
    a hook registered for exactly one event should not go silent because that
    payload spells its own name differently than expected.
    """
    named_event = payload.get("hook_event_name") or payload.get("event")
    if named_event is not None and _folded(named_event) != PROMPT_EVENT:
        return {"produced": False, "reason": "not a submitted prompt"}
    intake = _load_producer()
    return intake.from_hook(
        {
            "provider": "codex",
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


def _read_stdin() -> bytes | None:
    """The payload, bounded and deadlined; Codex writes it to this pipe."""
    if sys.stdin is None or sys.stdin.closed:
        return None
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
        chunk = os.read(descriptor, min(65536, MAX_PAYLOAD_BYTES + 1 - total))
        if not chunk:
            return b"".join(chunks) if chunks else None
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_PAYLOAD_BYTES:
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
        # A hook is advisory. Recording a project must never be able to
        # disturb, delay, or fail the prompt that stated it.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
