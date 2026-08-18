"""Automatic naming: prompt-derived titles and the self-name caller proof.

Two entry points share this module. ``auto_title_from_hook`` turns a session's
first prompt into a title; ``self_name_automatic_title`` lets a managed root
name itself, but only after ``prove_self_name_caller`` has proved the caller is
the exact conversation it claims to be.

That proof is a patch point the suite relies on, so the caller proof arrives
injected rather than resolved here: a self-naming path that could not be
intercepted in a test would be a poor thing to leave behind.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from .common import (
    PROVIDERS,
    CollectionError,
    automatic_naming_enabled,
    proc_root as gated_proc_root,
    valid_uuid,
)
from .processes import _process_ancestor_chain


# A conversation launched through the first-window color pre-bake opens as a
# resume, and Claude Code never auto-titles a resumed conversation (its
# once-only guard initializes from the loaded message count, which the
# resume-synthesized continuation pair makes non-zero). These sessions carry a
# unique signature: a synthetic isMeta user record holding the resume
# continuation text, written at load before the human's first prompt. For
# exactly those sessions the kit derives a title from the first real prompt
# and writes it through the same native records /rename persists. Sessions
# outside that signature are never touched, so Claude's own (better) auto
# titles are never masked.
RESUME_CONTINUATION_TEXT = "Continue from where you left off."


MAX_AUTO_TITLE_TRANSCRIPT_BYTES = 8 * 1024 * 1024


_TITLE_TRAILING_STOPWORDS = frozenset(
    "a an and any are at be but by each every for from how if in is it of on "
    "or so that the then there this to was were what when which who why will "
    "with".split()
)

_MACHINE_PROMPT_PREFIXES = (
    "<cross-session-message",
    "[session-kit operator message ",
    "Session Kit initialized this managed worker.",
    "You are a Session Kit delivery runner.",
    "RUNTIME FOR THIS WAKE: You are a fixed Session Kit delivery bot.",
)


def derive_prompt_title(prompt: Any, *, stopwords: frozenset[str]) -> str | None:
    """Derive a short display title from a first user prompt.

    Pure heuristic, no model call: first sentence, at most seven words,
    trailing function words trimmed, sentence-cased, capped at 64 characters.
    """
    if not isinstance(prompt, str):
        return None
    text = re.sub(r"\s+", " ", prompt).strip()
    if (
        not text
        or text.startswith("/")
        or any(text.startswith(prefix) for prefix in _MACHINE_PROMPT_PREFIXES)
    ):
        return None
    text = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    words = text.split(" ")[:7]
    trimmed = list(words)
    while trimmed and trimmed[-1].lower().strip("?,.:;!\"'") in stopwords:
        trimmed.pop()
    if not trimmed:
        trimmed = words
    title = " ".join(trimmed).rstrip(" ,;:.!?\"'")
    if len(title) < 4:
        return None
    title = title[:64].rstrip()
    return title[0].upper() + title[1:]


def _first_text_block(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
    return ""


def auto_title_from_hook(
    raw: Any,
    *,
    environ: Mapping[str, str] | None = None,
    derive_title: Callable[..., str | None],
    max_transcript_bytes: int,
    resume_continuation_text: str,
    propagate_title: Callable[..., dict[str, Any]],
    name_owner: Callable[..., str],
    claim_name: Callable[..., str],
) -> dict[str, Any]:
    """Title a pre-baked Claude conversation from its first real prompt.

    Input is the UserPromptSubmit hook payload (JSON text). Eligibility is
    strict and every miss fails open with title=null: the transcript must
    carry the resume-continuation signature, no real conversation beyond the
    triggering prompt, and no title of any kind (ai, /rename, agent-name,
    kit name intent) may already exist.

    The name is claimed once, at the conversation's first prompt: a thread
    whose name is already owned, by a person, or by the pass that ran at
    that first prompt, is left exactly as it is. The hook fires twice for a
    brand-new session (UserPromptSubmit, then Stop), and that claim is what
    makes the second firing a no-op rather than a second rename.
    """
    out: dict[str, Any] = {"title": None, "pushes": [], "warnings": []}

    def skip(reason: str) -> dict[str, Any]:
        out["reason"] = reason
        return out

    try:
        payload = json.loads(raw if isinstance(raw, str) else "")
    except ValueError:
        return skip("hook payload is not valid JSON")
    if not isinstance(payload, Mapping):
        return skip("hook payload is not an object")
    # UserPromptSubmit fires before the first turn is flushed to disk (the
    # pre-bake signature is not yet visible then); Stop fires right after the
    # first answer with everything durable. Supporting both means the title
    # lands at the end of the first exchange.
    if payload.get("hook_event_name") not in ("UserPromptSubmit", "Stop"):
        return skip("not a UserPromptSubmit or Stop event")
    uuid = valid_uuid(str(payload.get("session_id") or ""))
    if not uuid:
        return skip("missing or invalid session_id")

    env = environ if environ is not None else os.environ
    owner = name_owner("claude", uuid, environ=env)
    if owner == "human":
        return skip("a human name owns this session")
    if owner == "automatic":
        return skip("an automatic name already owns this session")
    home = Path(env.get("HOME") or os.fspath(Path.home()))
    transcript_raw = payload.get("transcript_path")
    if not isinstance(transcript_raw, str) or not transcript_raw:
        return skip("missing transcript_path")
    transcript = Path(transcript_raw)
    projects = (home / ".claude" / "projects").resolve()
    try:
        resolved = transcript.resolve(strict=True)
    except OSError:
        return skip("transcript unavailable")
    if (
        transcript.is_symlink()
        or resolved.name != f"{uuid}.jsonl"
        or resolved.parent.parent != projects
    ):
        return skip("transcript path is not this session's own transcript")
    if (home / ".claude" / "sessions" / f"{uuid}.nameintent").exists():
        return skip("a kit name intent already exists")
    try:
        if resolved.stat().st_size > max_transcript_bytes:
            return skip("transcript exceeds the bounded size")
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return skip("transcript unreadable")

    signature = False
    real_users = 0
    first_prompt: str | None = None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, Mapping):
            continue
        kind = record.get("type")
        if kind in ("ai-title", "agent-name", "custom-title"):
            return skip("the session already has a title")
        if kind != "user":
            continue
        if record.get("isSidechain"):
            continue
        content = (
            record.get("message", {}).get("content")
            if isinstance(record.get("message"), Mapping)
            else None
        )
        text = _first_text_block(content)
        if record.get("isMeta"):
            if text.startswith(resume_continuation_text):
                signature = True
            continue
        # Tool results are stored as user-typed records too; only records
        # carrying prompt text count as human prompts.
        if isinstance(content, str) or text:
            real_users += 1
            if first_prompt is None:
                first_prompt = content if isinstance(content, str) else text
    if not signature:
        return skip("no pre-bake resume signature")
    if real_users > 1:
        return skip("conversation already has prior prompts")
    # The conversation's own first prompt names the topic; the live payload
    # prompt is the fallback for the pre-flush window.
    title = derive_title(first_prompt) or derive_title(payload.get("prompt"))
    if not title:
        return skip("prompt does not yield a title")

    entry = json.dumps(
        {"type": "ai-title", "aiTitle": title, "sessionId": uuid},
        separators=(",", ":"),
    )
    try:
        with open(resolved, "a", encoding="utf-8") as handle:
            handle.write(entry + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        out["pushes"].append("claude-transcript-ai-title")
    except OSError as exc:
        return skip(f"transcript not appended: {exc}")
    # Claim before pushing: the record is what stops a second pass, and a
    # push that half-succeeds must not leave the thread open to renaming.
    claimed = claim_name("claude", uuid, environ=env)
    if claimed:
        out["warnings"].append(claimed)
    pushed = propagate_title("claude", uuid, title, environ=env)
    out["title"] = title
    out["pushes"].extend(pushed.get("provider_title_pushes", []))
    out["warnings"].extend(pushed.get("provider_title_warnings", []))
    return out


def prove_self_name_caller(
    inventory: Mapping[str, Any],
    process_table: Mapping[int, Mapping[str, Any]],
    environ: Mapping[str, str],
    current_pid: int,
) -> dict[str, Any]:
    """Prove one exact managed root conversation and its current shell generation."""
    if inventory.get("source") != "live" or inventory.get("stale") is not False:
        raise CollectionError("automatic naming requires a fresh live inventory")
    shpool_id = environ.get("SHPOOL_SESSION_NAME", "")
    if not shpool_id:
        raise CollectionError("automatic naming is unavailable outside a managed shell")
    matches = [
        item
        for item in inventory.get("sessions", ())
        if isinstance(item, Mapping) and item.get("shpool_id_raw") == shpool_id
    ]
    if len(matches) != 1:
        raise CollectionError("automatic name caller session is ambiguous or stale")
    item = matches[0]
    provider = item.get("provider")
    identity = item.get("identity")
    shell = item.get("shpool_shell")
    if (
        provider not in PROVIDERS
        or item.get("setup_incomplete") is not False
        or not isinstance(identity, Mapping)
        or identity.get("confidence") != "exact"
        or not isinstance(shell, Mapping)
    ):
        raise CollectionError("automatic name caller setup is incomplete")
    uuid = valid_uuid(identity.get("uuid"))
    provider_pid = identity.get("pid")
    provider_start = identity.get("process_start_ticks")
    shell_pid = shell.get("pid")
    shell_start = shell.get("process_start_ticks")
    if not uuid or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (provider_pid, provider_start, shell_pid, shell_start)
    ):
        raise CollectionError("automatic name caller lacks an exact generation")
    chain = _process_ancestor_chain(process_table, current_pid)
    if provider_pid not in chain or shell_pid not in chain:
        raise CollectionError(
            "automatic name caller is outside the exact provider root"
        )
    if chain.index(provider_pid) >= chain.index(shell_pid):
        raise CollectionError("automatic name caller ancestry is not a managed root")
    if process_table[provider_pid].get("start_ticks") != provider_start:
        raise CollectionError("automatic name provider generation changed")
    if process_table[shell_pid].get("start_ticks") != shell_start:
        raise CollectionError("automatic name shell generation changed")
    for pid in chain[: chain.index(provider_pid) + 1]:
        argv = process_table[pid].get("cmdline") or ()
        if "--parent-session-id" in argv or "--agent-name" in argv:
            raise CollectionError("automatic naming is refused for a subagent")
    if provider == "codex":
        caller_uuid = valid_uuid(environ.get("CODEX_THREAD_ID"))
        if any(
            isinstance(child, Mapping)
            and child.get("provider") == "codex"
            and valid_uuid(child.get("uuid")) == caller_uuid
            for child in item.get("subagents", ())
        ):
            raise CollectionError("automatic naming is refused for a subagent")
        if caller_uuid != uuid:
            raise CollectionError("Codex caller is not the managed root conversation")
    else:
        claude_uuid = environ.get("CLAUDE_SESSION_ID")
        if claude_uuid and valid_uuid(claude_uuid) != uuid:
            raise CollectionError("Claude caller is not the managed root conversation")
    return {
        "provider": provider,
        "uuid": uuid,
        "shpool_id": shpool_id,
        "provider_pid": provider_pid,
        "provider_start_ticks": provider_start,
        "shell_pid": shell_pid,
        "shell_start_ticks": shell_start,
    }


def self_name_automatic_title(
    config: Mapping[str, Any],
    title: str,
    *,
    inventory: Mapping[str, Any] | None = None,
    process_table: Mapping[int, Mapping[str, Any]] | None = None,
    environ: Mapping[str, str] | None = None,
    current_pid: int | None = None,
    default_max_proc_nodes: int,
    record_retry: Callable[..., None],
    canonical_colors: Callable[[Mapping[str, Any]], dict[str, str]],
    mutate_self_name: Callable[..., Any],
    normalize_title: Callable[..., str],
    process_table_reader: Callable[[Path, int], dict[int, dict[str, Any]]],
    propagate_color: Callable[..., dict[str, Any]],
    propagate_title: Callable[..., dict[str, Any]],
    prove_caller: Callable[..., dict[str, Any]],
    record_title_failure: Callable[..., Any],
    session_color: Callable[..., str | None],
    snapshot_inventory: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Provider-neutral exact self-name entry point used by root agents."""
    caller_environ = environ if environ is not None else os.environ
    if not automatic_naming_enabled(caller_environ):
        raise CollectionError("automatic naming is disabled")
    proc_root = gated_proc_root(caller_environ)
    supplied_evidence = inventory is not None or process_table is not None
    live = inventory or snapshot_inventory(write_state=False, config=dict(config))
    table = process_table or process_table_reader(
        proc_root, int(config.get("max_proc_nodes", default_max_proc_nodes))
    )
    pid = current_pid if current_pid is not None else os.getpid()
    evidence = prove_caller(live, table, caller_environ, pid)

    def revalidate() -> None:
        # Runs INSIDE the name-store write locks. A fresh collection here is
        # forbidden: collecting can itself take config.lock through a second
        # descriptor, and a process cannot pass its own flock on a new handle:
        # the self-name that jammed every collector on the machine for
        # twenty minutes on 2026-08-17 was exactly this call re-collecting
        # under the lock. The caller proof is repeated against a fresh
        # process table (lock-free, and the part that can actually change:
        # pid death and reuse); the inventory evidence stays the snapshot
        # taken moments ago, which the held lock now protects from writers.
        if supplied_evidence:
            new_table = table
        else:
            new_table = process_table_reader(
                proc_root,
                int(config.get("max_proc_nodes", default_max_proc_nodes)),
            )
        repeated = prove_caller(live, new_table, caller_environ, pid)
        if repeated != evidence:
            raise CollectionError("automatic name caller changed before write")

    try:
        normalized = normalize_title(title)
    except CollectionError as reason:
        attempts = record_title_failure(
            config,
            str(evidence["provider"]),
            str(evidence["uuid"]),
            revalidate=revalidate,
        )
        suffix = "name failed" if attempts >= 2 else "name pending"
        raise CollectionError(
            f"automatic title rejected ({reason}); {suffix}"
        ) from reason
    # A self-name owns both display tiers. Write them in one transaction so a
    # repeated rename cannot trip over the alias created by the previous one.
    result = mutate_self_name(
        config,
        str(evidence["provider"]),
        str(evidence["uuid"]),
        normalized,
        revalidate=revalidate,
    )
    result["caller"] = evidence
    # A self-name is an explicit rename, not background bookkeeping: it also
    # becomes the alias, the top display tier. In the automatic tier alone it
    # lost to the provider's own ai-title, so the picker kept showing a stale
    # native name after an agent explicitly renamed itself. `sp name reset`
    # still demotes it like any alias.
    native_push = propagate_title(
        str(evidence["provider"]),
        str(evidence["uuid"]),
        normalized,
        environ=caller_environ,
    )
    result.update(native_push)
    if native_push.get("provider_title_warnings"):
        record_retry(
            config,
            str(evidence["provider"]),
            str(evidence["uuid"]),
            normalized,
        )
        result["automatic_name_state"] = "pending"
    else:
        result["automatic_name_state"] = "ready"
    effective_color = session_color(
        str(evidence["provider"]),
        str(evidence["uuid"]),
        canonical_colors(config),
    )
    if effective_color:
        result.update(
            propagate_color(
                str(evidence["provider"]),
                str(evidence["uuid"]),
                effective_color,
                environ=caller_environ,
            )
        )
    return result
