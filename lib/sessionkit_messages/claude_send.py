"""Deliver one operator envelope to live Claude sessions through one headless run.

Claude Code has no socket to speak to, so the delivery agent IS a Claude
session: one ``claude -p`` run with only ListAgents and SendMessage allowed,
told exactly which display names it may touch. Phase 0 established the two
rules this module enforces:

* gotcha 1 — a session missing from ``claude agents --json`` is unreachable
  (it is usually parked on a trust prompt), so the registry is checked first
  and no delivery is attempted for it;
* gotcha 2 — the sender's own report is a claim, not a receipt. The receipt
  of record is the message id appearing in the TARGET's transcript, so every
  status is decided by reading the target's own ``.jsonl``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence

DEFAULT_SENDER_MODEL = "claude-sonnet-5"
SENDER_TIMEOUT_SECONDS = 240.0
VERIFY_SECONDS = 60.0
VERIFY_POLL_SECONDS = 2.0
MAX_TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024
NOT_REGISTERED_DETAIL = "not registered (possible trust prompt)"
ALLOWED_TOOLS = "ListAgents,SendMessage"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def default_runner(
    argv: Sequence[str], timeout: float, cwd: Path | None = None
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv),
        cwd=os.fspath(cwd) if cwd is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def registry_entries(
    environ: Mapping[str, str],
    *,
    runner: Runner = default_runner,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Read ``claude agents --json`` through the kit's existing fixture hooks.

    ``SESSION_KIT_CLAUDE_JSON_FILE`` and ``SESSION_KIT_CLAUDE_CMD`` are the
    same overrides the inventory collector honours, so a sandboxed test drives
    this without a Claude binary anywhere near it. The fixture file is honoured
    only under ``SESSION_KIT_TESTING=1``, for the same reason as there: it
    substitutes an attacker-chosen registry for what Claude actually reports.
    """
    fixture = environ.get("SESSION_KIT_CLAUDE_JSON_FILE")
    if fixture and environ.get("SESSION_KIT_TESTING") == "1":
        try:
            payload = json.loads(Path(fixture).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    else:
        prefix = shlex.split(environ.get("SESSION_KIT_CLAUDE_CMD", "")) or ["claude"]
        try:
            completed = runner([*prefix, "agents", "--json"], timeout)
        except (OSError, subprocess.SubprocessError):
            return []
        if completed.returncode != 0:
            return []
        try:
            payload = json.loads(completed.stdout)
        except ValueError:
            return []
    if not isinstance(payload, list):
        return []
    return [dict(item) for item in payload if isinstance(item, Mapping)]


def registry_index(entries: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """Map each registered session id to its display name and cwd."""
    index: dict[str, dict[str, str]] = {}
    for entry in entries:
        session_id = entry.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            continue
        name = entry.get("name")
        cwd = entry.get("cwd")
        index[session_id.lower()] = {
            "name": name if isinstance(name, str) else "",
            "cwd": cwd if isinstance(cwd, str) else "",
        }
    return index


def project_dir_name(cwd: str) -> str:
    """Claude's per-project transcript directory name for a working directory."""
    return "-" + str(cwd).strip("/").replace("/", "-") if cwd else ""


def transcript_path(home: Path, cwd: str, uuid: str) -> Path | None:
    """The target's own transcript, by derived project dir then by search.

    The derived name covers the normal case; the glob covers a session whose
    recorded cwd has since moved or contains characters Claude rewrites.
    """
    projects = Path(home) / ".claude" / "projects"
    derived = project_dir_name(cwd)
    if derived:
        candidate = projects / derived / f"{uuid}.jsonl"
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    try:
        matches = sorted(projects.glob(f"*/{uuid}.jsonl"))
    except OSError:
        return None
    for match in matches:
        if match.is_file() and not match.is_symlink():
            return match
    return None


def transcript_has_message(path: Path | None, msg_id: str, uuid: str) -> bool:
    """True when this message id has landed in the target's own transcript."""
    if path is None:
        return False
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > MAX_TRANSCRIPT_TAIL_BYTES:
                handle.seek(size - MAX_TRANSCRIPT_TAIL_BYTES)
            raw = handle.read(MAX_TRANSCRIPT_TAIL_BYTES)
    except OSError:
        return False
    marker = msg_id.encode("utf-8")
    if marker not in raw:
        return False
    for line in raw.split(b"\n"):
        if marker not in line:
            continue
        try:
            record = json.loads(line.decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError):
            # The id is unique to this send; an unparsable partial line that
            # carries it is still that message arriving.
            return True
        if not isinstance(record, Mapping):
            return True
        session_id = record.get("sessionId")
        if not isinstance(session_id, str) or session_id.lower() == uuid.lower():
            return True
    return False


def compose_instructions(
    targets: Sequence[Mapping[str, Any]], envelope: str, msg_id: str
) -> str:
    """The headless sender's whole brief: who, what, and what not to touch."""
    listed = "\n".join(
        f"{index + 1}. {json.dumps(str(target.get('display_name', '')))}"
        for index, target in enumerate(targets)
    )
    return (
        "You are a Session Kit delivery runner. Deliver one operator message, "
        "verbatim, to the sessions named below and to nobody else.\n\n"
        f"Expected ListAgents display names ({len(targets)}):\n{listed}\n\n"
        "Steps:\n"
        "1. Run ListAgents once.\n"
        "2. For each expected name, find the row whose display name matches it "
        "EXACTLY. If the name is missing, or more than one row still matches "
        "after you take refs into account, SKIP that target: send it nothing "
        "and report send_ok false with reason \"ambiguous\".\n"
        "3. For each matched target, call SendMessage with `to` set to that "
        "row's exact name (add its [ref] only if the listing itself shows one "
        "and you need it to disambiguate) and `message` set to the envelope "
        "below, copied VERBATIM with no preamble, summary, or edits.\n"
        "4. Never message any session that is not in the expected list. Never "
        "message yourself. Do not use any other tool.\n"
        "5. Your FINAL message must be one JSON array and nothing else:\n"
        '   [{"name": "<expected name>", "ref": "<ref or empty>", '
        '"send_ok": true, "reason": ""}]\n'
        "   One object per expected name, in the order listed above.\n\n"
        f"Envelope for message {msg_id} — everything between the markers, "
        "excluding the marker lines themselves:\n"
        "<<<SESSION_KIT_ENVELOPE\n"
        f"{envelope}\n"
        "SESSION_KIT_ENVELOPE\n"
    )


def sender_argv(instructions: str, model: str) -> list[str]:
    return [
        "claude",
        "-p",
        instructions,
        "--allowedTools",
        ALLOWED_TOOLS,
        "--model",
        model,
    ]


def parse_sender_output(text: str) -> list[dict[str, Any]]:
    """Take the last complete JSON array of objects the sender printed."""
    if not isinstance(text, str):
        return []
    decoder = json.JSONDecoder()
    best: list[dict[str, Any]] = []
    for index, character in enumerate(text):
        if character != "[":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if not isinstance(value, list):
            continue
        rows = [item for item in value if isinstance(item, Mapping)]
        if rows:
            best = [dict(row) for row in rows]
    return best


def sender_report(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index the sender's claims by the display name it was given."""
    report: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row.get("name")
        if isinstance(name, str) and name:
            report[name] = dict(row)
    return report


def run_sender(
    instructions: str,
    *,
    model: str,
    cwd: Path,
    runner: Runner = default_runner,
    timeout: float = SENDER_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the one headless sender for a whole send."""
    argv = sender_argv(instructions, model)
    try:
        completed = runner(argv, timeout, cwd)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "sender timed out", "rows": []}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "detail": f"sender did not run: {exc}", "rows": []}
    rows = parse_sender_output(completed.stdout)
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[:200] or f"exit {completed.returncode}"
        return {"ok": False, "detail": f"sender failed: {detail}", "rows": rows}
    return {"ok": True, "detail": "sender completed", "rows": rows}


def sender_model(environ: Mapping[str, str]) -> str:
    value = environ.get("SESSION_KIT_MSG_SENDER_MODEL", "").strip()
    return value or DEFAULT_SENDER_MODEL


def verify_delivery(
    targets: Sequence[Mapping[str, Any]],
    *,
    msg_id: str,
    home: Path,
    deadline_seconds: float = VERIFY_SECONDS,
    poll_seconds: float = VERIFY_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, bool]:
    """Poll every target's transcript until the id lands or the window closes."""
    pending = {str(target["thread_key"]): target for target in targets}
    found: dict[str, bool] = {key: False for key in pending}
    end = monotonic() + deadline_seconds
    while pending:
        for key in list(pending):
            target = pending[key]
            path = transcript_path(
                home, str(target.get("cwd", "")), str(target.get("uuid", ""))
            )
            if transcript_has_message(path, msg_id, str(target.get("uuid", ""))):
                found[key] = True
                del pending[key]
        if not pending or monotonic() >= end:
            break
        sleep(poll_seconds)
    return found
