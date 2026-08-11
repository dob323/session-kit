"""Session Kit state adapter for the vendored oversight tools.

The adapter always rebuilds the kit inventory for an observation. It never
attaches to a session, opens a socket, or invents a second session registry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat as statmod
import subprocess
import re
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

from .vendor.registry import SessionRegistry, Worker


_THREAD_KEY = re.compile(
    r"(claude|codex):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_RECEIPT_DELIVERED = re.compile(r"delivered\s+(\d+)")


def _receipt_delivered(stdout: object) -> int:
    """Delivered count from an sp msg receipt; 0 when it cannot be proven."""
    if not isinstance(stdout, str):
        return 0
    match = _RECEIPT_DELIVERED.search(stdout)
    return int(match.group(1)) if match else 0

MAX_COMMAND_OUTPUT = 2 * 1024 * 1024
MAX_LOG_BYTES = 256 * 1024
MAX_EVENT_LINES = 2000
MAX_EVENT_FILES = 200
MAX_ANNOTATION = 500
MAX_MESSAGE = 8000


class SupervisorError(RuntimeError):
    """An observation or gated operation could not be completed safely."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _default_runner(
    argv: Sequence[str], *, timeout: float, env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
        env=dict(env),
    )


def _clean(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:limit]


def _safe_json(payload: str, label: str) -> Mapping[str, Any]:
    if len(payload.encode("utf-8", "replace")) > MAX_COMMAND_OUTPUT:
        raise SupervisorError(f"{label} response exceeded the size limit")
    try:
        value = json.loads(payload)
    except ValueError as exc:
        raise SupervisorError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise SupervisorError(f"{label} returned a non-object")
    return value


class KitAdapter:
    """Fresh inventory, event, transcript, annotation, and message access."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        runner: Runner = _default_runner,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        self.runner = runner
        self.clock = clock
        self.sleeper = sleeper
        package_root = Path(__file__).resolve().parents[1]
        self.inventory_core = Path(
            self.environ.get(
                "SESSION_KIT_INVENTORY_CORE", package_root / "session_inventory.py"
            )
        )
        self.sp_argv = shlex.split(self.environ.get("SESSION_KIT_SP_CMD", "")) or [
            os.fspath(package_root.parent / "bin" / "sp")
        ]

    @property
    def home(self) -> Path:
        return Path(self.environ.get("HOME") or Path.home())

    @property
    def state_dir(self) -> Path:
        explicit = self.environ.get("SESSION_KIT_STATE_DIR")
        if explicit:
            return Path(explicit)
        xdg = self.environ.get("XDG_STATE_HOME")
        return Path(xdg) / "session-kit" if xdg else self.home / ".local/state/session-kit"

    def _run_json(
        self, argv: Sequence[str], label: str, *, timeout: float = 12.0
    ) -> Mapping[str, Any]:
        env = dict(self.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            completed = self.runner(argv, timeout=timeout, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SupervisorError(f"{label} failed: {exc}") from exc
        if completed.returncode != 0:
            detail = _clean(completed.stderr, 300) or f"exit {completed.returncode}"
            raise SupervisorError(f"{label} failed: {detail}")
        return _safe_json(completed.stdout, label)

    def inventory(self) -> Mapping[str, Any]:
        """Build through the exact Python facade used by shpool_status."""
        return self._run_json(
            [
                sys.executable,
                os.fspath(self.inventory_core),
                "snapshot",
                "--guard-live",
                "--no-write",
            ],
            "session inventory",
        )

    def registry(self) -> SessionRegistry:
        registry = SessionRegistry.from_inventory(self.inventory(), clock=self.clock)
        registry.apply_annotations(self._read_annotations())
        return registry

    def attention_queue(self) -> Mapping[str, Any]:
        return self._run_json(
            [sys.executable, os.fspath(self.inventory_core), "msg", "queue"],
            "attention queue",
        )

    def mark_briefed(self, timestamps: Sequence[int]) -> Mapping[str, Any]:
        """Flip brief_shown on surfaced ledger rows — the audit of surfacing."""
        try:
            from . import ratchet_module

            module = ratchet_module()
        except ImportError as exc:
            raise SupervisorError("ratchet unavailable") from exc
        ratchet_type = getattr(module, "Ratchet", None)
        if not callable(ratchet_type):
            raise SupervisorError("ratchet unavailable")
        ratchet = ratchet_type(self.state_dir, clock=self.clock)
        return dict(ratchet.mark_briefed(timestamps))

    def verify_source_event(self, event_id: str) -> Mapping[str, Any]:
        """Read one hook-created authority event through its fail-closed store."""
        from .source_authority import verify_source_event

        return verify_source_event(self.state_dir, event_id, clock=self.clock)

    def create_authority_request(
        self,
        *,
        target: str,
        text: str,
        category: str,
        authority_scope: str,
        source_thread_key: str,
        ttl_seconds: int = 600,
    ) -> Mapping[str, Any]:
        from .source_authority import AuthorityRequestStore

        return AuthorityRequestStore(self.state_dir).create(
            target=target,
            text=text,
            category=category,
            scope=authority_scope,
            source_thread_key=source_thread_key,
            ttl_seconds=ttl_seconds,
            clock=self.clock,
        )

    def _annotations_path(self) -> Path:
        return self.state_dir / "supervisor" / "annotations.json"

    def _read_annotations(self) -> dict[str, str]:
        path = self._annotations_path()
        try:
            metadata = path.lstat()
            if (
                path.is_symlink()
                or not statmod.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or statmod.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_size > MAX_COMMAND_OUTPUT
            ):
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(value, Mapping):
            return {}
        return {
            key: badge
            for key, raw in value.items()
            if isinstance(key, str)
            and isinstance(raw, str)
            and (badge := _clean(raw, MAX_ANNOTATION))
        }

    def annotate(self, worker: Worker, badge: str) -> None:
        clean_badge = _clean(badge, MAX_ANNOTATION)
        if not clean_badge:
            raise SupervisorError("badge is required")
        path = self._annotations_path()
        parent = path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = parent.lstat()
        if (
            parent.is_symlink()
            or not statmod.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            raise SupervisorError("supervisor state directory is not owner controlled")
        if statmod.S_IMODE(metadata.st_mode) & 0o077:
            os.chmod(parent, 0o700)
        if path.exists() or path.is_symlink():
            existing = path.lstat()
            if (
                path.is_symlink()
                or not statmod.S_ISREG(existing.st_mode)
                or existing.st_uid != os.geteuid()
                or statmod.S_IMODE(existing.st_mode) & 0o077
            ):
                raise SupervisorError("annotations file is not an owner-private regular file")
        annotations = self._read_annotations()
        annotations[worker.session_id] = clean_badge
        descriptor, temporary = tempfile.mkstemp(prefix=".annotations.", dir=parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(annotations, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def events(
        self,
        *,
        worker: Worker | None = None,
        since_unix_ms: int | None = None,
        limit: int = 1000,
    ) -> Mapping[str, Any]:
        limit = max(1, min(int(limit), MAX_EVENT_LINES))
        root = self.state_dir / "events"
        if worker is not None:
            paths = [root / f"{worker.session_id}.jsonl"]
            truncated = False
        else:
            candidates: list[tuple[int, Path]] = []
            if root.is_dir():
                for path in root.glob("*.jsonl"):
                    try:
                        metadata = path.lstat()
                    except OSError:
                        continue
                    if path.is_symlink() or not statmod.S_ISREG(metadata.st_mode):
                        continue
                    candidates.append((metadata.st_mtime_ns, path))
            candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
            truncated = len(candidates) > MAX_EVENT_FILES
            paths = [path for _, path in candidates[:MAX_EVENT_FILES]]
        rows: list[dict[str, Any]] = []
        for path in paths:
            if path.is_symlink() or ":" not in path.name:
                continue
            try:
                raw = _bounded_tail(path, MAX_LOG_BYTES).decode("utf-8", "replace")
            except OSError:
                continue
            for line in raw.splitlines():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(value, dict):
                    continue
                timestamp = value.get("ts_unix_ms")
                if not isinstance(timestamp, int):
                    continue
                if since_unix_ms is not None and timestamp < since_unix_ms:
                    continue
                value = dict(value)
                value["thread_key"] = path.name[:-6]
                rows.append(value)
        rows.sort(key=lambda row: (row["ts_unix_ms"], row["thread_key"]))
        return {"events": rows[-limit:], "truncated": truncated}

    def transcript_path(self, worker: Worker) -> Path | None:
        if worker.provider == "claude":
            try:
                matches = sorted(
                    (self.home / ".claude/projects").glob(
                        f"*/{worker.uuid}.jsonl"
                    ),
                    key=lambda path: path.stat().st_mtime_ns,
                    reverse=True,
                )
            except OSError:
                return None
            return next(
                (path for path in matches if path.is_file() and not path.is_symlink()),
                None,
            )
        return self._codex_transcript(worker.uuid)

    def _codex_transcript(self, uuid: str) -> Path | None:
        codex_root = Path(self.environ.get("CODEX_HOME") or self.home / ".codex")
        databases = [codex_root / "state_5.sqlite", codex_root / "state.sqlite"]
        for database in databases:
            if not database.is_file() or database.is_symlink():
                continue
            try:
                connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
                try:
                    columns = {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(threads)")
                    }
                    if "rollout_path" not in columns:
                        continue
                    row = connection.execute(
                        "SELECT rollout_path FROM threads WHERE id = ?", (uuid,)
                    ).fetchone()
                finally:
                    connection.close()
            except sqlite3.Error:
                continue
            if row and isinstance(row[0], str):
                candidate = Path(row[0])
                if not candidate.is_absolute():
                    candidate = database.parent / candidate
                if candidate.is_file() and not candidate.is_symlink():
                    return candidate
        sessions = codex_root / "sessions"
        try:
            day_dirs = sorted(
                (
                    day
                    for year in sessions.iterdir()
                    if year.is_dir() and not year.is_symlink()
                    for month in year.iterdir()
                    if month.is_dir() and not month.is_symlink()
                    for day in month.iterdir()
                    if day.is_dir() and not day.is_symlink()
                ),
                reverse=True,
            )[:3]
            matches = [
                path
                for day in day_dirs
                for path in day.glob(f"*{uuid}*.jsonl")
                if path.is_file() and not path.is_symlink()
            ][:20]
            matches.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        except OSError:
            return None
        return matches[0] if matches else None

    def read_logs(
        self, worker: Worker, *, pages: int = 1, offset: int = 0
    ) -> Mapping[str, Any]:
        pages = max(1, min(int(pages), 20))
        offset = max(0, min(int(offset), 100))
        path = self.transcript_path(worker)
        if path is None:
            raise SupervisorError("no provider transcript found")
        raw = _bounded_tail(path, MAX_LOG_BYTES).decode("utf-8", "replace")
        records: list[Any] = []
        for line in raw.splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                records.append({"raw": line[:2000]})
        end = max(0, len(records) - offset * 5)
        start = max(0, end - pages * 5)
        selected = records[start:end]
        return {
            "session_id": worker.session_id,
            "provider": worker.provider,
            "records": selected,
            "page_info": {
                "records_in_bounded_tail": len(records),
                "records_returned": len(selected),
                "page_size": 5,
                "pages": pages,
                "offset": offset,
                "tail_byte_limit": MAX_LOG_BYTES,
            },
        }

    def send_message(
        self,
        *,
        target: str,
        text: str,
        lane: int,
        operator_confirmed: bool = False,
        category: str | None = None,
        authority_event_id: str | None = None,
        authority_scope: str | None = None,
        authority_request_id: str | None = None,
    ) -> Mapping[str, Any]:
        if isinstance(lane, bool) or lane not in (1, 2, 3):
            raise SupervisorError("lane must be 1, 2, or 3")
        target = _clean(target, 200)
        message = text.strip() if isinstance(text, str) else ""
        if not target or not message:
            raise SupervisorError("target and text are required")
        if len(message) > MAX_MESSAGE:
            raise SupervisorError(f"text exceeds {MAX_MESSAGE} characters")
        if operator_confirmed is True:
            event_id = authority_event_id if isinstance(authority_event_id, str) else ""
            scope = _clean(authority_scope, 1000)
            verification = self.verify_source_event(event_id)
            if not verification.get("verified"):
                return {
                    "success": False,
                    "refused": True,
                    "reason": "source-authority",
                    "authority_event_id": event_id,
                    "authority_reason": verification.get("reason", "unverified source event"),
                    "target": target,
                    "lane": lane,
                    "category": category,
                }
            if not scope:
                return {
                    "success": False,
                    "refused": True,
                    "reason": "authority-scope",
                    "authority_event_id": event_id,
                    "target": target,
                    "lane": lane,
                    "category": category,
                }
            from .source_authority import AuthorityRequestStore, SourceAuthorityError

            try:
                AuthorityRequestStore(self.state_dir).verify_send(
                    authority_request_id,
                    verification=verification,
                    target=target,
                    text=message,
                    category=category or "",
                    scope=scope,
                    clock=self.clock,
                )
            except SourceAuthorityError as exc:
                return {
                    "success": False,
                    "refused": True,
                    "reason": "authority-request",
                    "authority_event_id": event_id,
                    "authority_request_id": authority_request_id,
                    "authority_reason": str(exc),
                    "target": target,
                    "lane": lane,
                    "category": category,
                }
        else:
            event_id = None
            scope = None
            authority_request_id = None
        # Every read tool hands the model thread_keys, but sp msg resolves
        # terminal numbers and shpool ids — the F-series drills caught the
        # supervisor's reads and its send speaking different languages.
        # Translate BEFORE any authorization so a bad target never costs a
        # budget claim or a ledger row.
        wire_target = target
        if _THREAD_KEY.fullmatch(target.strip().casefold()):
            worker = self.registry().get(target.strip().casefold())
            if worker is None:
                raise SupervisorError(f"target session not found: {target}")
            if worker.terminal_number is not None:
                wire_target = str(worker.terminal_number)
            elif worker.shpool_id:
                wire_target = worker.shpool_id
            else:
                raise SupervisorError(
                    f"target has no terminal number or shpool id: {target}"
                )
        if (
            not isinstance(category, str)
            or not category
            or len(category) > 64
            or not category.replace("_", "a").isalnum()
            or category[0].isdigit()
        ):
            return {
                "success": False,
                "refused": True,
                "reason": "category",
                "target": target,
                "lane": lane,
                "category": category,
            }

        recorded_lane = lane
        try:
            from . import ratchet_module

            module = ratchet_module()
        except ImportError:
            return {
                "success": False,
                "refused": True,
                "reason": "ratchet-unavailable",
                "target": target,
                "lane": lane,
                "category": category,
            }
        ratchet_type = getattr(module, "Ratchet", None)
        if not callable(ratchet_type):
            return {
                "success": False,
                "refused": True,
                "reason": "ratchet-unavailable",
                "target": target,
                "lane": lane,
                "category": category,
            }
        ratchet = ratchet_type(self.state_dir, clock=self.clock)
        lanes = ratchet.lanes()
        entry = lanes.get(category)
        if not isinstance(entry, Mapping):
            return {
                "success": False,
                "refused": True,
                "reason": "category",
                "target": target,
                "lane": lane,
                "category": category,
            }
        known_lane = entry.get("lane")
        if known_lane not in (1, 2, 3) or isinstance(known_lane, bool):
            return {
                "success": False,
                "refused": True,
                "reason": "category",
                "target": target,
                "lane": lane,
                "category": category,
            }
        recorded_lane = known_lane

        effective_lane = max(lane, recorded_lane)
        folded_target = target.casefold()
        broadcast = folded_target in {
            "all",
            "idle",
            "broadcast",
            "*",
        } or folded_target.startswith("keys:")
        if (effective_lane == 3 or broadcast) and operator_confirmed is not True:
            return {
                "success": False,
                "refused": True,
                "reason": "lane" if effective_lane == 3 else "broadcast",
                "target": target,
                "lane": lane,
                "effective_lane": effective_lane,
                "category": category,
            }
        decision = ratchet.authorize(
            category,
            operator_confirmed=operator_confirmed is True,
        )
        if not decision.get("allowed"):
            reasons = decision.get("reasons")
            ratchet_reasons = reasons if isinstance(reasons, list) else []
            refusal_reason = decision.get("reason")
            if refusal_reason not in {"lane", "budget"}:
                refusal_reason = "ratchet-unavailable"
            return {
                "success": False,
                "refused": True,
                "reason": refusal_reason,
                "ratchet_reasons": ratchet_reasons,
                "target": target,
                "lane": lane,
                "effective_lane": effective_lane,
                "category": category,
            }
        env = dict(self.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            completed = self.runner(
                [*self.sp_argv, "msg", "--yes", "--", wire_target, message],
                timeout=300.0,
                env=env,
            )
        except Exception as exc:
            # The runner raising means we do not know whether the message
            # landed — a transport fault, not a judgment failure. "unknown"
            # consumes the claim and breaks the streak without demoting the
            # category; "bad" (and its lane-3 demotion) is reserved for a
            # completed send that returned failure.
            ratchet.record(
                category=category,
                action="send_message",
                target=target,
                outcome="unknown",
                brief_shown=False,
                lane=effective_lane,
                operator_confirmed=operator_confirmed is True,
                authority_event_id=event_id,
                authority_scope=scope,
                authority_request_id=authority_request_id,
            )
            if isinstance(exc, (OSError, subprocess.SubprocessError)):
                raise SupervisorError(f"sp msg failed: {exc}") from exc
            raise
        # Outcome is DELIVERY truth, in both directions. A non-zero exit is a
        # send that did not land; so is exit 0 whose receipt shows zero
        # delivered (an ambiguous target, a sender that never reported) — the
        # re-drill caught that case scoring "ok" and feeding a false
        # promotion streak. "bad" is reserved for explicitly judged bad
        # outcomes; the machine never assigns it.
        delivered = _receipt_delivered(completed.stdout)
        outcome = "ok" if completed.returncode == 0 and delivered > 0 else "unknown"
        ratchet.record(
            category=category,
            action="send_message",
            target=target,
            outcome=outcome,
            brief_shown=False,
            lane=effective_lane,
            operator_confirmed=operator_confirmed is True,
            authority_event_id=event_id,
            authority_scope=scope,
            authority_request_id=authority_request_id,
        )
        return {
            "success": outcome == "ok",
            "refused": False,
            "target": target,
            "lane": lane,
            "effective_lane": effective_lane,
            "category": category,
            "operator_confirmed": operator_confirmed is True,
            "authority_event_id": event_id,
            "authority_scope": scope,
            "authority_request_id": authority_request_id,
            "exit_code": completed.returncode,
            "delivered": delivered,
            "output": _clean(completed.stdout, 4000),
            "error": _clean(completed.stderr, 1000),
        }


def _bounded_tail(path: Path, maximum: int) -> bytes:
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        offset = max(0, size - maximum)
        handle.seek(offset)
        raw = handle.read(maximum)
    if offset:
        boundary = raw.find(b"\n")
        raw = raw[boundary + 1 :] if boundary >= 0 else b""
    return raw
