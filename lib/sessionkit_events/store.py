"""Owner-private append-only session event storage."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat as statmod
import tempfile
import time
from typing import Any, Iterator, Mapping


EVENTS = frozenset(
    {
        "needs_input",
        "turn_done",
        "session_start",
        "session_end",
        "permission_prompt",
    }
)
SOURCES = frozenset({"hook", "synth"})
THREAD_KEY_RE = re.compile(
    r"^(?:claude|codex):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
MAX_QUESTION_CHARS = 8000
MAX_EVENT_TAIL_BYTES = 256 * 1024


class EventError(ValueError):
    """An event or event-store path is not safe to use."""


def now_unix_ms(clock: Any = time.time) -> int:
    return int(clock() * 1000)


def valid_thread_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    return candidate if THREAD_KEY_RE.fullmatch(candidate) else ""


def thread_key(provider: object, uuid: object) -> str:
    candidate = f"{provider}:{uuid}".lower()
    exact = valid_thread_key(candidate)
    if not exact:
        raise EventError("thread key must be <claude|codex>:<uuid>")
    return exact


def events_root(state_dir: Path | str) -> Path:
    return Path(state_dir) / "events"


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (
        not statmod.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or statmod.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise EventError(
            f"events directory must be an owner-only real directory: {path}"
        )
    return path


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _read_bounded(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return b""
    try:
        metadata = os.fstat(descriptor)
        if not statmod.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            return b""
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            if metadata.st_size > limit:
                handle.seek(metadata.st_size - limit)
                raw = handle.read(limit)
                newline = raw.find(b"\n")
                return raw[newline + 1 :] if newline >= 0 else b""
            return handle.read(limit)
    except OSError:
        return b""
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _clean_question(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EventError("event question must be a string or null")
    cleaned = "".join(
        character
        for character in value
        if character in {"\n", "\t"} or ord(character) >= 32
    ).strip()
    return cleaned[:MAX_QUESTION_CHARS] or None


def event_record(
    event: object,
    *,
    question: object = None,
    source: object,
    ts_unix_ms: object | None = None,
) -> dict[str, Any]:
    if event not in EVENTS:
        raise EventError("unknown session event")
    if source not in SOURCES:
        raise EventError("event source must be hook or synth")
    timestamp = now_unix_ms() if ts_unix_ms is None else ts_unix_ms
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp < 0
    ):
        raise EventError("event timestamp must be a non-negative integer")
    return {
        "ts_unix_ms": timestamp,
        "event": event,
        "question": _clean_question(question),
        "source": source,
    }


class EventStore:
    """All reads and writes below one exact Session Kit state directory."""

    def __init__(self, state_dir: Path | str):
        self.root = events_root(state_dir)
        self.seen = self.root / "seen"
        self.lock_path = self.root / "events.lock"

    def ensure(self) -> None:
        _private_dir(self.root)
        _private_dir(self.seen)

    @contextlib.contextmanager
    def locked(self, *, timeout_seconds: float | None = None) -> Iterator[None]:
        self.ensure()
        if timeout_seconds is not None and timeout_seconds < 0:
            raise EventError("lock timeout must be non-negative")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if (
                not statmod.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
            ):
                raise EventError("event lock must be an owner-owned regular file")
            os.fchmod(descriptor, 0o600)
            if timeout_seconds is None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + timeout_seconds
                while True:
                    try:
                        fcntl.flock(
                            descriptor,
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("event store lock is busy")
                        time.sleep(min(0.01, remaining))
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def event_path(self, key: str) -> Path:
        exact = valid_thread_key(key)
        if not exact:
            raise EventError("thread key must be <claude|codex>:<uuid>")
        return self.root / f"{exact}.jsonl"

    def seen_path(self, key: str) -> Path:
        exact = valid_thread_key(key)
        if not exact:
            raise EventError("thread key must be <claude|codex>:<uuid>")
        return self.seen / exact

    def _append_locked(self, key: str, record: Mapping[str, Any]) -> None:
        payload = (
            json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(self.event_path(key), flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if (
                not statmod.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
            ):
                raise EventError("event log must be an owner-owned regular file")
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def append(
        self,
        key: str,
        event: str,
        *,
        question: str | None = None,
        source: str,
        ts_unix_ms: int | None = None,
        lock_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        exact = valid_thread_key(key)
        if not exact:
            raise EventError("thread key must be <claude|codex>:<uuid>")
        record = event_record(
            event,
            question=question,
            source=source,
            ts_unix_ms=ts_unix_ms,
        )
        with self.locked(timeout_seconds=lock_timeout_seconds):
            self._append_locked(exact, record)
        return record

    def read(self, key: str) -> list[dict[str, Any]]:
        try:
            raw = _read_bounded(self.event_path(key), MAX_EVENT_TAIL_BYTES)
        except EventError:
            return []
        records: list[dict[str, Any]] = []
        for line in raw.splitlines():
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            try:
                exact = event_record(
                    value.get("event"),
                    question=value.get("question"),
                    source=value.get("source"),
                    ts_unix_ms=value.get("ts_unix_ms"),
                )
            except EventError:
                continue
            records.append(exact)
        return records

    def mark_seen(self, key: str, ts_unix_ms: int | None = None) -> int:
        exact = valid_thread_key(key)
        if not exact:
            raise EventError("thread key must be <claude|codex>:<uuid>")
        timestamp = now_unix_ms() if ts_unix_ms is None else ts_unix_ms
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
        ):
            raise EventError("seen timestamp must be a non-negative integer")
        with self.locked():
            _atomic_write(self.seen_path(exact), f"{timestamp}\n".encode("ascii"))
        return timestamp

    def seen_unix_ms(self, key: str) -> int | None:
        try:
            path = self.seen_path(key)
            raw = _read_bounded(path, 64)
        except EventError:
            return None
        try:
            timestamp = int(raw.strip())
        except (TypeError, ValueError):
            try:
                timestamp = int(path.stat().st_mtime * 1000)
            except OSError:
                return None
        return timestamp if timestamp >= 0 else None


def append_event(
    state_dir: Path | str,
    key: str,
    event: str,
    *,
    question: str | None = None,
    source: str,
    ts_unix_ms: int | None = None,
    lock_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    return EventStore(state_dir).append(
        key,
        event,
        question=question,
        source=source,
        ts_unix_ms=ts_unix_ms,
        lock_timeout_seconds=lock_timeout_seconds,
    )


def read_events(state_dir: Path | str, key: str) -> list[dict[str, Any]]:
    return EventStore(state_dir).read(key)


def mark_seen(
    state_dir: Path | str, key: str, ts_unix_ms: int | None = None
) -> int:
    return EventStore(state_dir).mark_seen(key, ts_unix_ms)
