"""One session's pending question, latest result, and message exchange.

The picker lists a session's *state*; this is the one screen that shows what
the state is actually about — the question a session is blocked on, the result
it finished with, and the last few lines of the operator's own message thread
with it — without attaching to the session and without changing anything.

Strictly read-only: the attention queue is built with ``mutate=False`` so
looking at a row cannot synthesize an event, and the message ledger is only
read, never marked. Every string that reaches a terminal is control-character
scrubbed here, because all of it (titles, questions, replies) is written by
another process.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import textwrap
import time
from typing import Any, Mapping
import unicodedata

if __package__ in (None, ""):  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sessionkit_events.queue import build_attention_queue
from sessionkit_messages.ledger import Ledger

# A peek is a glance, not a transcript reader: enough of the exchange to know
# what was asked and answered, and no more.
EXCHANGE_LINES = 4
MAX_TEXT = 600


def _clean(value: Any, limit: int = 200) -> str:
    text = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in str(value or "")
    )
    return " ".join(text.split())[:limit]


def _relative(milliseconds: Any, now_ms: int) -> str:
    if (
        isinstance(milliseconds, bool)
        or not isinstance(milliseconds, int)
        or milliseconds <= 0
    ):
        return ""
    seconds = max(0, (now_ms - milliseconds) // 1000)
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


def _duration(milliseconds: Any) -> str:
    if (
        isinstance(milliseconds, bool)
        or not isinstance(milliseconds, int)
        or milliseconds < 0
    ):
        return ""
    minutes = milliseconds // 60000
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} hr {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days} day{'s' if days != 1 else ''} {hours} hr"


def _row_for(view: Mapping[str, Any], number: int) -> Mapping[str, Any] | None:
    matches = [
        row
        for row in view.get("sessions", [])
        if isinstance(row, Mapping) and row.get("terminal_number") == number
    ]
    return matches[0] if len(matches) == 1 else None


def _state_text(row: Mapping[str, Any], item: Mapping[str, Any] | None) -> str:
    if row.get("setup_incomplete"):
        return "setup incomplete"
    bucket = str((item or {}).get("bucket") or "")
    if row.get("needs_you") or bucket == "needs_you":
        return "needs your reply"
    if bucket == "finished_unseen":
        return "finished, not yet opened"
    if row.get("reply_optional"):
        return "reply optional"
    return _clean(row.get("agent_status")) or "status unavailable"


def build_peek(
    view: Mapping[str, Any],
    inventory: Mapping[str, Any] | None,
    state_dir: Path | str,
    number: int,
    *,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Everything one peek card shows, or None when the row is not listed."""
    row = _row_for(view, number)
    if row is None:
        return None
    as_of = int(time.time() * 1000) if now_ms is None else int(now_ms)
    identity = row.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    provider = str(row.get("provider") or "")
    uuid = identity.get("uuid")
    thread_key = (
        f"{provider}:{uuid}"
        if provider in {"claude", "codex"} and isinstance(uuid, str) and uuid
        else ""
    )

    item: Mapping[str, Any] | None = None
    if thread_key and isinstance(inventory, Mapping):
        # Advisory: a queue that cannot be built costs the question line, not
        # the card. The row's own fields still describe the session.
        try:
            queue = build_attention_queue(
                inventory, Path(state_dir), now_ms=as_of, mutate=False
            )
            for candidate in queue.get("items", []):
                if (
                    isinstance(candidate, Mapping)
                    and candidate.get("thread_key") == thread_key
                ):
                    item = candidate
                    break
        except Exception:
            item = None

    exchange: list[dict[str, Any]] = []
    if thread_key:
        try:
            for entry in Ledger(state_dir).read_thread(
                thread_key, limit=EXCHANGE_LINES
            ):
                if not isinstance(entry, Mapping):
                    continue
                direction = entry.get("dir")
                exchange.append(
                    {
                        "direction": "out" if direction == "out" else "in",
                        "text": _clean(entry.get("text"), MAX_TEXT),
                        "ts_unix_ms": entry.get("ts_unix_ms"),
                    }
                )
        except Exception:
            exchange = []

    question = (item or {}).get("question")
    return {
        "number": number,
        "title": _clean(row.get("display_title") or row.get("title"), 120)
        or f"session {number}",
        "provider": provider or "unknown",
        "account": _clean(row.get("account_alias"), 20),
        "availability": str(row.get("availability") or ""),
        "cwd": _clean(row.get("cwd"), 200),
        "project": _clean(row.get("_picker_group_label"), 40),
        "state": _state_text(row, item),
        "bucket": str((item or {}).get("bucket") or ""),
        "waiting_ms": (item or {}).get("waiting_ms"),
        "question": _clean(question, MAX_TEXT) if isinstance(question, str) else "",
        "last_response_at_unix_ms": (item or {}).get("last_response_at_unix_ms"),
        "thread_key": thread_key,
        "exchange": exchange[-EXCHANGE_LINES:],
        "can_reply": bool(thread_key),
        "as_of_unix_ms": as_of,
    }


def _wrapped(text: str, width: int, indent: str) -> list[str]:
    room = max(20, width - len(indent))
    return [
        indent + line
        for line in textwrap.wrap(text, room) or [""]
    ]


def render_peek(peek: Mapping[str, Any], width: int = 100) -> list[str]:
    """The card as printable lines, already wrapped and already scrubbed."""
    width = max(40, min(200, int(width)))
    now_ms = int(peek.get("as_of_unix_ms") or time.time() * 1000)
    header_parts = [
        f"Session {peek['number']}",
        str(peek.get("provider") or "unknown").title(),
    ]
    account = peek.get("account")
    if account:
        header_parts.append(str(account))
    header_parts.append(str(peek.get("state") or ""))
    lines = ["", "  " + " · ".join(part for part in header_parts if part)]
    lines.extend(_wrapped(str(peek.get("title") or ""), width, "  "))
    where = str(peek.get("cwd") or "")
    if where:
        lines.extend(_wrapped(where, width, "  "))

    waiting = _duration(peek.get("waiting_ms"))
    if waiting and peek.get("bucket") == "needs_you":
        lines.append(f"  Waiting {waiting} for you.")
    finished = _relative(peek.get("last_response_at_unix_ms"), now_ms)
    if finished and peek.get("bucket") == "finished_unseen":
        lines.append(f"  Finished {finished}; nobody has opened it since.")

    question = str(peek.get("question") or "")
    if question:
        lines.append("")
        lines.append("  It asked")
        lines.extend(_wrapped(question, width, "    "))

    exchange = peek.get("exchange") or []
    if exchange:
        lines.append("")
        lines.append("  Latest messages")
        for entry in exchange:
            if not isinstance(entry, Mapping):
                continue
            who = "you" if entry.get("direction") == "out" else "it"
            when = _relative(entry.get("ts_unix_ms"), now_ms)
            stamp = f"{who} · {when}" if when else who
            lines.append(f"    {stamp}")
            lines.extend(_wrapped(str(entry.get("text") or ""), width, "      "))
    if not question and not exchange:
        lines.append("")
        lines.append("  No question and no messages are recorded for this session.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--view", required=True)
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--state", required=True)
    parser.add_argument("--number", required=True, type=int)
    parser.add_argument("--width", default="100")
    parser.add_argument("--json", action="store_true")
    options = parser.parse_args(argv)

    try:
        with open(options.view, encoding="utf-8") as handle:
            view = json.load(handle)
    except (OSError, ValueError):
        return 1
    inventory: Mapping[str, Any] | None = None
    if options.snapshot:
        try:
            with open(options.snapshot, encoding="utf-8") as handle:
                inventory = json.load(handle)
        except (OSError, ValueError):
            inventory = None

    peek = build_peek(view, inventory, options.state, options.number)
    if peek is None:
        return 2
    if options.json:
        json.dump(peek, sys.stdout, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    width = int(options.width) if str(options.width).isdigit() else 100
    for line in render_peek(peek, width):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the picker
    raise SystemExit(main())
