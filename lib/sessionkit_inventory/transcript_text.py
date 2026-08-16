"""Render a provider transcript (.jsonl) into clean readable text.

The conversation transcript is the one history that survives everything: it
is written by the provider itself, never braided by terminal repaints, and
exists even for sessions whose terminal output was never recorded. `sp
history` falls back to this view when a session has no journal (2026-08-12:
recording was silently off for two weeks, so every session from that window
recalls through here).

Both providers are read here. Claude writes one transcript per conversation;
Codex writes a rollout of the same conversation in its own shape. The fallback
existed for Claude only, so a Codex session with no recording answered "no
history" while its rollout sat on disk unread.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

USER_RULE = "═" * 60
MAX_RESULT_LINES = 12
MAX_AGENT_RESULT_CHARS = 8192
SCAFFOLD_TAGS = (
    "recommended_plugins",
    "permissions instructions",
    "apps_instructions",
    "skills_instructions",
    "environment_context",
    "INSTRUCTIONS",
    "codex_internal_context",
    "hook_prompt",
)


def _user_text(body: str) -> list[str]:
    return ["\n══ OPERATOR " + USER_RULE, body.strip()]


def _text_parts(content: Any) -> Iterator[str]:
    """Yield readable text without exposing encrypted or metadata blocks."""
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for inner in content:
            if isinstance(inner, dict) and inner.get("type") in (
                "input_text", "output_text", "text"
            ):
                text = inner.get("text")
                if isinstance(text, str):
                    yield text
    elif isinstance(content, dict):
        yield from _text_parts(content.get("content") or content.get("output"))


def _result_text(content: Any, *, max_chars: int | None = None) -> str | None:
    parts: list[str] = []
    retained = 0
    clipped = False
    for part in _text_parts(content):
        if max_chars is None:
            parts.append(part)
            continue
        available = max_chars - retained
        if available <= 0:
            clipped = True
            break
        if len(part) > available:
            parts.append(part[:available])
            clipped = True
            break
        parts.append(part)
        retained += len(part)
    text = "\n".join(parts).rstrip()
    if not text:
        return None
    if clipped:
        text += "\n  … (more text)"
    lines = text.split("\n")
    if len(lines) > MAX_RESULT_LINES:
        hidden = len(lines) - MAX_RESULT_LINES
        lines = lines[:MAX_RESULT_LINES] + [f"  … ({hidden} more lines)"]
    return "\n".join("  │ " + line for line in lines)


def _tool_text(name: Any, tool_input: Any) -> str:
    clean_name = name if isinstance(name, str) and name.strip() else "?"
    detail = ""
    if isinstance(tool_input, str):
        try:
            decoded = json.loads(tool_input)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            tool_input = decoded
        elif tool_input.strip():
            detail = tool_input.strip().replace("\n", " ")[:160]
    if isinstance(tool_input, dict):
        for key in (
            "command", "cmd", "description", "file_path", "pattern",
            "prompt", "query", "question",
        ):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip().replace("\n", " ")[:160]
                break
    return f"⏺ {clean_name}: {detail}" if detail else f"⏺ {clean_name}"


def _block_text(block: dict) -> str | None:
    kind = block.get("type")
    if kind == "text":
        return block.get("text", "")
    if kind == "tool_use":
        return _tool_text(block.get("name"), block.get("input"))
    if kind == "tool_result":
        return _result_text(block.get("content"))
    return None


def render_transcript_entry(entry: object) -> list[str]:
    """Render one decoded Claude JSONL record, or reject its shape."""
    out: list[str] = []
    if not isinstance(entry, dict):
        return out
    message = entry.get("message") or {}
    if not isinstance(message, dict):
        return out
    content = message.get("content")
    etype = entry.get("type")
    if etype == "user":
        if isinstance(content, str) and content.strip():
            out.extend(_user_text(content))
        elif isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if any(t.strip() for t in texts):
                out.extend(_user_text("\n".join(texts)))
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = _block_text(block)
                    if text:
                        out.append(text)
    elif etype == "assistant" and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            text = _block_text(block)
            if text is None:
                continue
            if block.get("type") == "text":
                out.append("\n● " + text.strip())
            else:
                out.append(text)
    return out


def render_transcript(path: Path) -> list[str]:
    out: list[str] = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            out.extend(render_transcript_entry(entry))
    return out


def _strip_codex_scaffolding(body: str) -> str:
    """Remove harness preambles without discarding the person's prompt."""
    text = body.lstrip()
    while text:
        if text.startswith("# AGENTS.md instructions"):
            heading = re.match(
                r"^# AGENTS\.md instructions(?: for [^\n]*)?[ \t]*\n+[ \t]*",
                text,
            )
            if heading:
                instructions = re.match(
                    r"^<INSTRUCTIONS>.*?</INSTRUCTIONS>\s*",
                    text[heading.end():],
                    flags=re.DOTALL,
                )
                if instructions:
                    text = text[heading.end() + instructions.end():].lstrip()
                    continue
        before = text
        for tag in SCAFFOLD_TAGS:
            pattern = (
                rf"^<{re.escape(tag)}(?:[ \t][^\n>]*)?>"
                rf".*?</{re.escape(tag)}>\s*"
            )
            text, count = re.subn(pattern, "", text, count=1, flags=re.DOTALL)
            if count:
                text = text.lstrip()
                break
        if text == before:
            break
    return text.strip()


def _rollout_text(payload: dict) -> list[str]:
    """One readable line-group from a Codex rollout record."""
    if payload.get("type") != "message":
        return []
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return []
    parts: list[str] = []
    for block in payload.get("content") or ():
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("input_text", "output_text", "text"):
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    body = "\n".join(parts).strip()
    if not body:
        return []
    if role == "user":
        body = _strip_codex_scaffolding(body)
        return _user_text(body) if body else []
    return ["\n● " + body]


def _rollout_tool(payload: dict) -> str | None:
    kind = payload.get("type")
    if kind in ("function_call", "custom_tool_call"):
        tool_input = payload.get("arguments")
        if kind == "custom_tool_call":
            tool_input = payload.get("input")
        return _tool_text(payload.get("name"), tool_input)
    if kind in ("function_call_output", "custom_tool_call_output"):
        return _result_text(payload.get("output"))
    if isinstance(kind, str) and kind.endswith("_call"):
        return _tool_text(payload.get("name") or kind.removesuffix("_call"), payload)
    if isinstance(kind, str) and kind.endswith("_call_output"):
        return _result_text(payload.get("output"))
    return None


def _rollout_agent_result(payload: dict) -> list[str]:
    """Render the readable result a sub-agent sent back to its parent."""
    if payload.get("type") != "agent_message":
        return []
    readable = "\n".join(_text_parts(payload.get("content"))).strip()
    if not readable:
        return []
    envelope = re.match(
        r"^Message Type: FINAL_ANSWER\n.*?\nPayload:\n(.*)$",
        readable,
        flags=re.DOTALL,
    )
    if not envelope or not envelope.group(1).strip():
        return []
    result = _result_text(envelope.group(1), max_chars=MAX_AGENT_RESULT_CHARS)
    if not result:
        return []
    author = payload.get("author")
    label = "⏺ sub-agent result"
    if isinstance(author, str) and author.strip():
        label += f": {author.strip()}"
    return [label, result]


def _rollout_payload(payload: dict) -> list[str]:
    rendered = _rollout_text(payload)
    if rendered:
        return rendered
    rendered = _rollout_agent_result(payload)
    if rendered:
        return rendered
    tool = _rollout_tool(payload)
    return [tool] if tool else []


def _message_key(payload: dict) -> bytes | None:
    """Identity used only to avoid replaying a compacted message twice."""
    if payload.get("type") != "message":
        return None
    role = payload.get("role")
    if not isinstance(role, str):
        return None
    digest = hashlib.blake2b(digest_size=16)
    digest.update(role.encode("utf-8", "replace"))
    found = False
    for text in _text_parts(payload.get("content")):
        encoded = text.encode("utf-8", "replace")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        found = True
    return digest.digest() if found else None


def render_rollout(path: Path) -> list[str]:
    """Codex's rollout, rendered in the same shape as a Claude transcript."""
    out: list[str] = []
    precompact_spans: dict[bytes, list[tuple[int, int]]] = {}
    compacted_seen = False
    continuation_pending = False
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") == "compacted" and not compacted_seen:
                compact_payload = record.get("payload")
                history = (
                    compact_payload.get("replacement_history")
                    if isinstance(compact_payload, dict) else None
                )
                if not isinstance(history, list):
                    continue
                compacted: list[str] = []
                duplicate_spans: list[tuple[int, int]] = []
                for payload in history:
                    if not isinstance(payload, dict):
                        continue
                    rendered = _rollout_payload(payload)
                    if rendered:
                        compacted.extend(rendered)
                    key = _message_key(payload)
                    spans = precompact_spans.get(key) if key else None
                    if spans:
                        duplicate_spans.append(spans.pop(0))
                if not compacted:
                    continue
                removed = {
                    index
                    for start, end in duplicate_spans
                    for index in range(start, end)
                }
                remaining = [
                    line for index, line in enumerate(out) if index not in removed
                ]
                out = ["\n══ COMPACTED HISTORY " + USER_RULE, *compacted]
                if remaining:
                    out.extend(["\n══ CONTINUED HISTORY " + USER_RULE, *remaining])
                else:
                    continuation_pending = True
                compacted_seen = True
                continue
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            rendered = _rollout_payload(payload)
            if not rendered:
                continue
            if continuation_pending:
                out.append("\n══ CONTINUED HISTORY " + USER_RULE)
                continuation_pending = False
            start = len(out)
            out.extend(rendered)
            if not compacted_seen:
                key = _message_key(payload)
                if key:
                    precompact_spans.setdefault(key, []).append((start, len(out)))
    return out


def find_rollout(uuid: str) -> Path | None:
    """Locate a Codex rollout across every local profile root."""
    # `sp history` runs this file as a script, so the package it lives in is
    # not importable by name until its parent is on the path.
    if __package__:
        from . import transcripts
    else:
        sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))
        from sessionkit_inventory import transcripts

    return transcripts.locate_transcript("codex", uuid)


def find_transcript(uuid: str) -> Path | None:
    """Locate a conversation transcript across every local profile root."""
    home = Path.home()
    roots = [home / ".claude"]
    account_root = Path(
        os.environ.get("SESSION_KIT_ACCOUNT_ROOT",
                       home / ".local/share/session-kit/accounts")
    )
    roots.extend(sorted(account_root.glob("claude/*")))
    best: Path | None = None
    for root in roots:
        for candidate in glob.glob(str(root / "projects" / "*" / f"{uuid}.jsonl")):
            path = Path(candidate)
            if best is None or path.stat().st_mtime > best.stat().st_mtime:
                best = path
    return best


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    provider = "claude"
    if len(args) == 2 and args[0] in ("claude", "codex"):
        provider, args = args[0], args[1:]
    if len(args) != 1:
        print(
            "usage: transcript_text.py [claude|codex] <conversation-uuid-or-jsonl-path>",
            file=sys.stderr,
        )
        return 2
    target = args[0]
    path = Path(target)
    if not path.is_file():
        found = find_rollout(target) if provider == "codex" else find_transcript(target)
        if found is None:
            print(f"no transcript found for {target}", file=sys.stderr)
            return 1
        path = found
    render = render_rollout if provider == "codex" else render_transcript
    for line in render(path):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
