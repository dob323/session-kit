"""Per-plan cost caps and per-run receipts for delegated work.

A delegated plan currently ends the way the worker says it ends. Nothing on
disk states what it cost, whether anything verified it, which files it touched,
or why it stopped — so a worker that burns an account's quota and a worker that
finishes cleanly leave the same trace: none.

Two records fix that, and they are deliberately separate:

* A **cap**, one per intake, written when the operator approves the plan. It is
  the number shown at approval time and the number enforced afterwards. Reaching
  it is a hard stop: `record_spend` closes the run as ``cap_breached`` and
  `gate` refuses to launch anything else for that plan until the cap is raised.
* A **receipt**, one per run, holding the spend recorded against it, the
  verifier's result, the files that changed, and the stop reason.

Spend is *reported*, never guessed. Every sample carries the source that
claimed it (a supervisor lane, a transcript reader, the operator), and the
receipt totals estimates as estimates — the field is ``usd_est``, the same
vocabulary the ratchet's day budget already uses. A receipt that says $0.00
means nobody reported spend, and it says so by carrying no samples.

Each write recomputes a SHA-256 digest over the canonical record, so a receipt
edited outside these verbs reads back as ``tamper_detected`` instead of as
evidence. That is integrity, not authentication: the digest proves the file
was not changed behind the kit's back, and nothing here claims to prove who
wrote it.

Schema modeled on MartinLoop's run receipts (Apache-2.0, budget caps + stop
reason + verifier evidence + changed files); no code was taken from it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from typing import Any

from .private_store import (
    PrivateStoreError,
    canonical_bytes,
    private_directory,
    private_names,
    read_private_json,
    write_private_json,
)

SCHEMA_VERSION = 1
MSG_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
RECEIPT_ID_RE = re.compile(r"[0-9a-f]{32}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
MAX_RECORD_BYTES = 262_144
MAX_RECEIPTS = 512
MAX_SAMPLES = 200
MAX_CHANGED_FILES = 500
MAX_EVIDENCE = 2000
GIT_TIMEOUT_SECONDS = 60

VERIFIER_RESULTS = ("passed", "failed", "unverified")
STOP_REASONS = (
    "running",
    "completed",
    "cap_breached",
    "failed",
    "abandoned",
    "cancelled",
)
ISOLATION_MODES = ("worktree", "none")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class ReceiptError(ValueError):
    """A cap or receipt request is invalid."""


def now_unix_ms(clock: Callable[[], float] = time.time) -> int:
    return int(clock() * 1000)


# ---- paths --------------------------------------------------------------


def receipts_root(state_dir: Path | str) -> Path:
    return private_directory(Path(state_dir) / "receipts", label="receipt store")


def _caps_dir(state_dir: Path | str) -> Path:
    return private_directory(receipts_root(state_dir) / "caps", label="cap store")


def _runs_dir(state_dir: Path | str) -> Path:
    return private_directory(receipts_root(state_dir) / "runs", label="receipt store")


# ---- validation ---------------------------------------------------------


def valid_msg_id(value: object) -> str:
    if not isinstance(value, str) or not MSG_ID_RE.fullmatch(value.strip()):
        raise ReceiptError("an intake id must be 1-128 characters of A-Z a-z 0-9 . _ : -")
    return value.strip()


def valid_receipt_id(value: object) -> str:
    if not isinstance(value, str) or not RECEIPT_ID_RE.fullmatch(value.strip()):
        raise ReceiptError("a receipt id is 32 lowercase hexadecimal characters")
    return value.strip()


def _text(value: object, *, limit: int, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReceiptError(f"{label} must be text")
    cleaned = " ".join(value.split())
    if len(cleaned) > limit:
        raise ReceiptError(f"{label} exceeds {limit} characters")
    return cleaned


def _amount(value: object, *, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReceiptError(f"{label} must be a number")
    amount = float(value)
    if amount < 0:
        raise ReceiptError(f"{label} cannot be negative")
    return round(amount, 6)


def _count(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReceiptError(f"{label} must be a whole number")
    if value < 0:
        raise ReceiptError(f"{label} cannot be negative")
    return value


# ---- integrity ----------------------------------------------------------


def _digest(record: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "integrity"}
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _sealed(record: dict[str, Any]) -> dict[str, Any]:
    record["integrity"] = {"algorithm": "sha256", "digest": _digest(record)}
    return record


def integrity_of(record: Mapping[str, Any]) -> str:
    """``verified``, ``tamper_detected``, or ``unsigned`` for one receipt."""
    integrity = record.get("integrity")
    if not isinstance(integrity, Mapping) or not integrity.get("digest"):
        return "unsigned"
    return "verified" if integrity["digest"] == _digest(record) else "tamper_detected"


# ---- caps ---------------------------------------------------------------


def _cap_path(state_dir: Path | str, msg_id: str) -> Path:
    return _caps_dir(state_dir) / f"{msg_id}.json"


def read_cap(state_dir: Path | str, msg_id: str) -> dict[str, Any] | None:
    """The approved cap for one plan, or None when the plan has no cap."""
    document = read_private_json(
        _cap_path(state_dir, valid_msg_id(msg_id)),
        limit=MAX_RECORD_BYTES,
        label="plan cap",
    )
    if document is None:
        return None
    return document


def set_cap(
    *,
    state_dir: Path | str,
    msg_id: str,
    max_usd_est: float | None = None,
    soft_usd_est: float | None = None,
    max_tokens: int | None = None,
    max_iterations: int | None = None,
    note: str = "",
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Record the cap the operator sees at approval time.

    At least one limit is required: a cap document with nothing in it would
    read like a cap at approval and enforce nothing afterwards.
    """
    identifier = valid_msg_id(msg_id)
    hard = _amount(max_usd_est, label="hard USD cap")
    soft = _amount(soft_usd_est, label="soft USD limit")
    tokens = _count(max_tokens, label="token cap")
    iterations = _count(max_iterations, label="iteration cap")
    if hard is None and tokens is None and iterations is None:
        raise ReceiptError(
            "a plan cap needs at least one of --max-usd, --max-tokens, --max-iterations"
        )
    if soft is not None and hard is not None and soft > hard:
        raise ReceiptError("the soft USD limit cannot exceed the hard cap")
    existing = read_cap(state_dir, identifier)
    revision = int(existing.get("revision", 0)) + 1 if existing else 1
    record = {
        "schema_version": SCHEMA_VERSION,
        "intake_msg_id": identifier,
        "max_usd_est": hard,
        "soft_usd_est": soft,
        "max_tokens": tokens,
        "max_iterations": iterations,
        "note": _text(note, limit=300, label="cap note"),
        "revision": revision,
        "recorded_unix_ms": now_unix_ms(clock),
    }
    write_private_json(_cap_path(state_dir, identifier), record)
    return record


def format_cap(cap: Mapping[str, Any] | None) -> str:
    """The one line an approval prompt shows about cost."""
    if not cap:
        return "Cost cap: none recorded for this plan."
    parts: list[str] = []
    hard = cap.get("max_usd_est")
    soft = cap.get("soft_usd_est")
    if isinstance(hard, (int, float)) and not isinstance(hard, bool):
        line = f"${float(hard):.2f} estimated spend"
        if isinstance(soft, (int, float)) and not isinstance(soft, bool):
            line += f" (warn at ${float(soft):.2f})"
        parts.append(line)
    tokens = cap.get("max_tokens")
    if isinstance(tokens, int) and not isinstance(tokens, bool):
        parts.append(f"{tokens:,} tokens")
    iterations = cap.get("max_iterations")
    if isinstance(iterations, int) and not isinstance(iterations, bool):
        parts.append(f"{iterations} iteration{'s' if iterations != 1 else ''}")
    return "Cost cap for this plan: " + " · ".join(parts) + " (hard stop)"


# ---- receipts -----------------------------------------------------------


def _run_path(state_dir: Path | str, receipt_id: str) -> Path:
    return _runs_dir(state_dir) / f"{receipt_id}.json"


def read_run(state_dir: Path | str, receipt_id: str) -> dict[str, Any] | None:
    document = read_private_json(
        _run_path(state_dir, valid_receipt_id(receipt_id)),
        limit=MAX_RECORD_BYTES,
        label="run receipt",
    )
    return document


def _require_run(state_dir: Path | str, receipt_id: str) -> dict[str, Any]:
    record = read_run(state_dir, receipt_id)
    if record is None:
        raise ReceiptError(f"no run receipt {receipt_id}")
    if integrity_of(record) == "tamper_detected":
        raise ReceiptError(
            f"run receipt {receipt_id} was edited outside the kit; refusing to build on it"
        )
    return record


def open_run(
    *,
    state_dir: Path | str,
    msg_id: str,
    branch: str = "",
    provider: str = "",
    model: str = "",
    launch_key: str = "",
    isolation_mode: str = "none",
    isolation_path: str = "",
    isolation_reason: str = "",
    receipt_id: str | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Open one run receipt, with the plan's cap snapshotted into it."""
    identifier = valid_msg_id(msg_id)
    if branch and not BRANCH_RE.fullmatch(branch.strip()):
        raise ReceiptError("a receipt branch is not a usable git ref name")
    if isolation_mode not in ISOLATION_MODES:
        raise ReceiptError(f"isolation mode must be one of {', '.join(ISOLATION_MODES)}")
    if provider and provider not in ("claude", "codex", "shell"):
        raise ReceiptError("a receipt provider must be claude, codex, or shell")
    token = valid_receipt_id(receipt_id) if receipt_id else secrets.token_hex(16)
    if read_run(state_dir, token) is not None:
        raise ReceiptError(f"run receipt {token} already exists")
    opened = now_unix_ms(clock)
    record = _sealed(
        {
            "schema_version": SCHEMA_VERSION,
            "receipt_id": token,
            "intake_msg_id": identifier,
            "branch": branch.strip(),
            "provider": provider,
            "model": _text(model, limit=128, label="model"),
            "launch_key": _text(launch_key, limit=128, label="launch key"),
            "worker_identity": "",
            "opened_unix_ms": opened,
            "updated_unix_ms": opened,
            "closed_unix_ms": None,
            "cap": read_cap(state_dir, identifier),
            "spend": {"usd_est": 0.0, "tokens": 0, "iterations": 0, "samples": []},
            "verifier": {
                "result": "unverified",
                "command": "",
                "exit_code": None,
                "evidence": "",
                "recorded_unix_ms": None,
            },
            "changed_files": {
                "entries": [],
                "truncated": False,
                "recorded_unix_ms": None,
            },
            "isolation": {
                "mode": isolation_mode,
                "path": isolation_path,
                "reason": _text(isolation_reason, limit=300, label="isolation reason"),
            },
            "stop_reason": "running",
            "stop_detail": "",
        }
    )
    write_private_json(_run_path(state_dir, token), record)
    return record


def runs_for(state_dir: Path | str, msg_id: str) -> list[dict[str, Any]]:
    """Every receipt belonging to one plan, oldest first."""
    identifier = valid_msg_id(msg_id)
    return [
        record
        for record in all_runs(state_dir)
        if record.get("intake_msg_id") == identifier
    ]


def all_runs(state_dir: Path | str) -> list[dict[str, Any]]:
    runs = Path(state_dir) / "receipts" / "runs"
    if not runs.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for token in private_names(runs, ".json", limit=MAX_RECEIPTS, strict=True):
        try:
            record = read_run(state_dir, token)
        except (ReceiptError, PrivateStoreError):
            continue
        if record is not None:
            found.append(record)
    found.sort(key=lambda item: int(item.get("opened_unix_ms") or 0))
    return found


def plan_spend(state_dir: Path | str, msg_id: str) -> dict[str, float | int]:
    """What the whole plan has recorded so far, across all its runs."""
    totals: dict[str, float | int] = {"usd_est": 0.0, "tokens": 0, "iterations": 0}
    for record in runs_for(state_dir, msg_id):
        spend = record.get("spend")
        if not isinstance(spend, Mapping):
            continue
        totals["usd_est"] = round(
            float(totals["usd_est"]) + float(spend.get("usd_est") or 0.0), 6
        )
        totals["tokens"] = int(totals["tokens"]) + int(spend.get("tokens") or 0)
        totals["iterations"] = int(totals["iterations"]) + int(
            spend.get("iterations") or 0
        )
    return totals


def cap_state(
    state_dir: Path | str, msg_id: str, *, projected: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Where a plan stands against its cap, optionally including new spend."""
    cap = read_cap(state_dir, msg_id)
    totals = plan_spend(state_dir, msg_id)
    if projected:
        totals = {
            "usd_est": round(
                float(totals["usd_est"]) + float(projected.get("usd_est") or 0.0), 6
            ),
            "tokens": int(totals["tokens"]) + int(projected.get("tokens") or 0),
            "iterations": int(totals["iterations"]) + int(projected.get("iterations") or 0),
        }
    breaches: list[str] = []
    warnings: list[str] = []
    if cap:
        hard = cap.get("max_usd_est")
        if isinstance(hard, (int, float)) and not isinstance(hard, bool):
            if float(totals["usd_est"]) >= float(hard):
                breaches.append(
                    f"estimated spend ${float(totals['usd_est']):.2f} reached the "
                    f"${float(hard):.2f} cap"
                )
            else:
                soft = cap.get("soft_usd_est")
                if (
                    isinstance(soft, (int, float))
                    and not isinstance(soft, bool)
                    and float(totals["usd_est"]) >= float(soft)
                ):
                    warnings.append(
                        f"estimated spend ${float(totals['usd_est']):.2f} passed the "
                        f"${float(soft):.2f} soft limit"
                    )
        tokens = cap.get("max_tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            if int(totals["tokens"]) >= tokens:
                breaches.append(
                    f"{int(totals['tokens']):,} tokens reached the {tokens:,} token cap"
                )
        iterations = cap.get("max_iterations")
        if isinstance(iterations, int) and not isinstance(iterations, bool):
            if int(totals["iterations"]) >= iterations:
                breaches.append(
                    f"{int(totals['iterations'])} iterations reached the "
                    f"{iterations} iteration cap"
                )
    return {
        "intake_msg_id": valid_msg_id(msg_id),
        "cap": cap,
        "totals": totals,
        "breached": bool(breaches),
        "breaches": breaches,
        "warnings": warnings,
    }


def record_spend(
    *,
    state_dir: Path | str,
    receipt_id: str,
    usd_est: float = 0.0,
    tokens: int = 0,
    iterations: int = 0,
    source: str,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Add one reported spend sample and enforce the plan's cap.

    Returns the receipt with a ``cap_state``. When the plan's hard cap is
    reached the receipt is closed as ``cap_breached`` before returning, so the
    stop is recorded even if the caller ignores the answer.
    """
    record = _require_run(state_dir, receipt_id)
    amount = _amount(usd_est, label="reported USD") or 0.0
    token_count = _count(tokens, label="reported tokens") or 0
    iteration_count = _count(iterations, label="reported iterations") or 0
    if amount == 0.0 and token_count == 0 and iteration_count == 0:
        raise ReceiptError("a spend sample needs a non-zero amount, tokens, or iterations")
    origin = _text(source, limit=120, label="spend source")
    if not origin:
        raise ReceiptError("a spend sample must name the source that reported it")
    if record.get("stop_reason") != "running":
        raise ReceiptError(
            f"receipt {record['receipt_id']} is closed as {record['stop_reason']}"
        )
    spend = dict(record["spend"])
    samples = list(spend.get("samples") or [])
    if len(samples) >= MAX_SAMPLES:
        raise ReceiptError(f"receipt {record['receipt_id']} already holds {MAX_SAMPLES} samples")
    stamped = now_unix_ms(clock)
    samples.append(
        {
            "usd_est": amount,
            "tokens": token_count,
            "iterations": iteration_count,
            "source": origin,
            "recorded_unix_ms": stamped,
        }
    )
    spend = {
        "usd_est": round(float(spend.get("usd_est") or 0.0) + amount, 6),
        "tokens": int(spend.get("tokens") or 0) + token_count,
        "iterations": int(spend.get("iterations") or 0) + iteration_count,
        "samples": samples,
    }
    record["spend"] = spend
    record["updated_unix_ms"] = stamped
    write_private_json(_run_path(state_dir, record["receipt_id"]), _sealed(record))
    state = cap_state(state_dir, str(record["intake_msg_id"]))
    if state["breached"]:
        record = close_run(
            state_dir=state_dir,
            receipt_id=str(record["receipt_id"]),
            stop_reason="cap_breached",
            stop_detail="; ".join(state["breaches"]),
            allow_unverified=True,
            clock=clock,
        )
    return {**record, "cap_state": state}


def record_verifier(
    *,
    state_dir: Path | str,
    receipt_id: str,
    result: str,
    command: str = "",
    exit_code: int | None = None,
    evidence: str = "",
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Record what checked the work, and what it said."""
    record = _require_run(state_dir, receipt_id)
    if result not in VERIFIER_RESULTS:
        raise ReceiptError(f"a verifier result must be one of {', '.join(VERIFIER_RESULTS)}")
    record["verifier"] = {
        "result": result,
        "command": _text(command, limit=500, label="verifier command"),
        "exit_code": _count(exit_code, label="verifier exit code"),
        "evidence": _text(evidence, limit=MAX_EVIDENCE, label="verifier evidence"),
        "recorded_unix_ms": now_unix_ms(clock),
    }
    record["updated_unix_ms"] = now_unix_ms(clock)
    write_private_json(_run_path(state_dir, record["receipt_id"]), _sealed(record))
    return record


def _git_lines(
    repo: Path | str, arguments: Sequence[str], *, runner: Runner
) -> list[str]:
    completed = runner(
        ["git", "-C", os.fspath(repo), *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        detail = " ".join((completed.stderr or "").split())[:200]
        raise ReceiptError(
            f"cannot read changed files in {repo} ({' '.join(arguments)}): {detail}"
        )
    return [line for line in (completed.stdout or "").splitlines() if line.strip()]


def changed_files(
    *, repo: Path | str, since: str = "", runner: Runner = subprocess.run
) -> dict[str, Any]:
    """The files a run touched, straight from git.

    Uncommitted and untracked entries always count. ``since`` adds everything
    committed on top of that ref, which is the usual case for a delegated
    worker: it commits its work, so `git status` alone would report a clean
    tree and the receipt would claim the run changed nothing.
    """
    found: dict[str, str] = {}
    if since:
        for line in _git_lines(
            repo, ("diff", "--name-status", f"{since}...HEAD"), runner=runner
        ):
            parts = line.split("\t")
            if len(parts) >= 2 and parts[-1]:
                found[parts[-1][:512]] = parts[0][:4] or "?"
    for line in _git_lines(repo, ("status", "--porcelain"), runner=runner):
        path = line[3:].strip()
        if path:
            found[path[:512]] = line[:2].strip() or "?"
    entries = [
        {"status": status, "path": path} for path, status in sorted(found.items())
    ]
    truncated = len(entries) > MAX_CHANGED_FILES
    return {
        "entries": entries[:MAX_CHANGED_FILES],
        "truncated": truncated,
        "since": since,
    }


def record_changed_files(
    *,
    state_dir: Path | str,
    receipt_id: str,
    repo: Path | str | None = None,
    since: str = "",
    entries: Sequence[Mapping[str, Any]] | None = None,
    runner: Runner = subprocess.run,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Record which files the run touched, read from git or handed in."""
    record = _require_run(state_dir, receipt_id)
    if entries is None:
        target = repo if repo is not None else record.get("isolation", {}).get("path")
        if not target:
            raise ReceiptError("recording changed files needs a repository or worktree path")
        collected = changed_files(repo=target, since=since, runner=runner)
    else:
        cleaned = [
            {
                "status": _text(item.get("status"), limit=4, label="file status") or "?",
                "path": _text(item.get("path"), limit=512, label="file path"),
            }
            for item in entries
            if _text(item.get("path"), limit=512, label="file path")
        ]
        collected = {
            "entries": cleaned[:MAX_CHANGED_FILES],
            "truncated": len(cleaned) > MAX_CHANGED_FILES,
            "since": since,
        }
    record["changed_files"] = {**collected, "recorded_unix_ms": now_unix_ms(clock)}
    record["updated_unix_ms"] = now_unix_ms(clock)
    write_private_json(_run_path(state_dir, record["receipt_id"]), _sealed(record))
    return record


def close_run(
    *,
    state_dir: Path | str,
    receipt_id: str,
    stop_reason: str,
    stop_detail: str = "",
    worker_identity: str = "",
    allow_unverified: bool = False,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Close one run with the reason it stopped.

    ``completed`` needs a verifier result. A finished-but-unchecked worker is
    the exact failure this whole record exists to make visible, so claiming
    completion without one takes an explicit ``allow_unverified``.
    """
    record = _require_run(state_dir, receipt_id)
    if stop_reason not in STOP_REASONS or stop_reason == "running":
        closable = ", ".join(reason for reason in STOP_REASONS if reason != "running")
        raise ReceiptError(f"a stop reason must be one of {closable}")
    verifier = record.get("verifier")
    verified = (
        isinstance(verifier, Mapping) and verifier.get("result") == "passed"
    )
    if stop_reason == "completed" and not verified and not allow_unverified:
        raise ReceiptError(
            "closing a run as completed needs a passing verifier result "
            "(record one, or pass --allow-unverified to state that nothing checked it)"
        )
    stamped = now_unix_ms(clock)
    record["stop_reason"] = stop_reason
    record["stop_detail"] = _text(stop_detail, limit=500, label="stop detail")
    if worker_identity:
        record["worker_identity"] = _text(
            worker_identity, limit=300, label="worker identity"
        )
    record["closed_unix_ms"] = stamped
    record["updated_unix_ms"] = stamped
    write_private_json(_run_path(state_dir, record["receipt_id"]), _sealed(record))
    return record


def gate(state_dir: Path | str, msg_id: str) -> dict[str, Any]:
    """Whether a plan may still start work, and why not when it may not."""
    state = cap_state(state_dir, msg_id)
    return {
        **state,
        "allowed": not state["breached"],
        "reason": "; ".join(state["breaches"]) if state["breached"] else "",
    }


# ---- rendering ----------------------------------------------------------


def render_run(record: Mapping[str, Any]) -> str:
    """One receipt as a person reads it."""
    spend = record.get("spend") or {}
    verifier = record.get("verifier") or {}
    files = record.get("changed_files") or {}
    isolation = record.get("isolation") or {}
    entries = list(files.get("entries") or [])
    samples = list(spend.get("samples") or [])
    lines = [
        f"  Run receipt {record.get('receipt_id')}",
        f"  Plan            {record.get('intake_msg_id')}",
        f"  Branch          {record.get('branch') or 'none'}",
        f"  Worker          {record.get('provider') or 'unknown'}"
        + (f" · {record.get('model')}" if record.get("model") else ""),
        f"  Isolation       {isolation.get('mode') or 'none'}"
        + (f" · {isolation.get('path')}" if isolation.get("path") else "")
        + (f" ({isolation.get('reason')})" if isolation.get("reason") else ""),
        "  Spend           "
        + (
            f"${float(spend.get('usd_est') or 0.0):.2f} est · "
            f"{int(spend.get('tokens') or 0):,} tokens · "
            f"{int(spend.get('iterations') or 0)} iterations "
            f"({len(samples)} reported sample{'s' if len(samples) != 1 else ''})"
            if samples
            else "nothing reported"
        ),
        f"  {format_cap(record.get('cap'))}",
        f"  Verifier        {verifier.get('result') or 'unverified'}"
        + (f" · {verifier.get('command')}" if verifier.get("command") else "")
        + (
            f" · exit {verifier.get('exit_code')}"
            if verifier.get("exit_code") is not None
            else ""
        ),
        f"  Changed files   {len(entries)}"
        + (" (truncated)" if files.get("truncated") else ""),
        f"  Stop reason     {record.get('stop_reason')}"
        + (f" — {record.get('stop_detail')}" if record.get("stop_detail") else ""),
        f"  Integrity       {integrity_of(record)}",
    ]
    for entry in entries[:20]:
        lines.append(f"      {entry.get('status', '?'):<2} {entry.get('path')}")
    if len(entries) > 20:
        lines.append(f"      … {len(entries) - 20} more")
    return "\n".join(lines) + "\n"


def render_plan(state_dir: Path | str, msg_id: str) -> str:
    """The plan's cap, its runs, and where it stands against the cap."""
    state = cap_state(state_dir, msg_id)
    totals = state["totals"]
    lines = [
        f"  Plan {state['intake_msg_id']}",
        f"  {format_cap(state['cap'])}",
        f"  Recorded so far: ${float(totals['usd_est']):.2f} est · "
        f"{int(totals['tokens']):,} tokens · {int(totals['iterations'])} iterations",
    ]
    for warning in state["warnings"]:
        lines.append(f"  Warning: {warning}")
    for breach in state["breaches"]:
        lines.append(f"  STOPPED: {breach}")
    runs = runs_for(state_dir, msg_id)
    if not runs:
        lines.append("  No runs recorded.")
    for record in runs:
        spend = record.get("spend") or {}
        verifier = record.get("verifier") or {}
        lines.append(
            f"    {record.get('receipt_id')}  {record.get('branch') or '-'}  "
            f"{record.get('stop_reason')}  "
            f"${float(spend.get('usd_est') or 0.0):.2f} est  "
            f"verifier {verifier.get('result') or 'unverified'}"
        )
    return "\n".join(lines) + "\n"
