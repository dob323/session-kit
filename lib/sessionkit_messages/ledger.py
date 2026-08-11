"""Owner-private store for operator sends, per-thread transcripts, and unread marks.

Layout under ``$SK_STATE_DIR/messages`` (mode 0700 throughout, files 0600):

    sends/<msg_id>.json      one operator send and every target's outcome
    keys/<idempotency_key>   the message id one repeatable purpose already has
    threads/<thread_key>.jsonl  append-only out/in lines for one conversation
    unread/<thread_key>      zero-byte marker set by an inbound reply
    messages.lock            flock held across every mutation

Every write is atomic (mkstemp + fsync + rename) so a crash mid-send leaves
the previous record intact rather than a truncated one.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import stat as statmod
import tempfile
import time
from typing import Any, Iterator, Mapping, Sequence

from .envelope import (
    IDEMPOTENCY_KEY_RULE,
    MessageError,
    preview,
    valid_idempotency_key,
    valid_msg_id,
    valid_thread_key,
)

MAX_SEND_BYTES = 1024 * 1024
MAX_THREAD_BYTES = 4 * 1024 * 1024
MAX_THREAD_TAIL_BYTES = 256 * 1024
MAX_TEXT_IN_THREAD = 8000
RETENTION_DAYS = 90
RETENTION_MS = RETENTION_DAYS * 24 * 60 * 60 * 1000
MAX_SENDS_KEPT = 500
MAX_LIST_SENDS = 50
THREAD_TAIL_LINES = 20


def now_unix_ms(clock: Any = time.time) -> int:
    return int(clock() * 1000)


def messages_root(state_dir: Path | str) -> Path:
    """The messages store under an exact, caller-supplied state directory.

    The caller passes ``config["state_dir"]`` — the same
    ``SESSION_KIT_STATE_DIR``/``XDG_STATE_HOME`` value every other kit store
    honours — so a sandboxed environment is the sandbox, with no home
    fallback anywhere in this module.
    """
    return Path(state_dir) / "messages"


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (
        not statmod.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or statmod.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise MessageError(
            f"messages directory must be an owner-only real directory: {path}"
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
    """Read a regular owner file, refusing symlinks and oversized payloads."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return b""
    try:
        metadata = os.fstat(descriptor)
        if not statmod.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            return b""
        if metadata.st_size > limit:
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                handle.seek(metadata.st_size - limit)
                return handle.read(limit)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read(limit)
    except OSError:
        return b""
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class Ledger:
    """Every read and write of the operator message store."""

    def __init__(self, state_dir: Path | str):
        self.root = messages_root(state_dir)
        self.sends = self.root / "sends"
        self.keys = self.root / "keys"
        self.threads = self.root / "threads"
        self.unread = self.root / "unread"
        self.lock_path = self.root / "messages.lock"

    # ---- structure -----------------------------------------------------

    def ensure(self) -> None:
        _private_dir(self.root)
        for directory in (self.sends, self.keys, self.threads, self.unread):
            _private_dir(directory)

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the store's exclusive lock for one mutation."""
        self.ensure()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    # ---- sends ---------------------------------------------------------

    def send_path(self, msg_id: str) -> Path:
        exact = valid_msg_id(msg_id)
        if not exact:
            raise MessageError("message id must be 8 hex characters")
        return self.sends / f"{exact}.json"

    def has_send(self, msg_id: str) -> bool:
        try:
            return self.send_path(msg_id).exists()
        except MessageError:
            return False

    def write_send(self, record: Mapping[str, Any]) -> None:
        msg_id = valid_msg_id(record.get("msg_id"))
        if not msg_id:
            raise MessageError("a send record needs an exact message id")
        self.ensure()
        payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(payload) > MAX_SEND_BYTES:
            raise MessageError("send record exceeds the store's size limit")
        _atomic_write(self.send_path(msg_id), payload)

    def update_send(
        self, msg_id: str, mutate: Any
    ) -> dict[str, Any]:
        """Read-modify-write one send record under the store lock.

        Delivery can take minutes while a woken agent is already replying, so
        the lock is held for this one short critical section rather than for
        the whole send — otherwise a reply would block behind the sender and
        an in-flight status write would clobber it.
        """
        with self.locked():
            record = self.read_send(msg_id)
            if record is None:
                raise MessageError(f"no stored send {msg_id}")
            updated = mutate(record)
            if not isinstance(updated, dict):
                updated = record
            self.write_send(updated)
            return updated

    def read_send(self, msg_id: str) -> dict[str, Any] | None:
        raw = _read_bounded(self.send_path(msg_id), MAX_SEND_BYTES)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def send_ids(self) -> list[str]:
        """Every stored send id, newest first by recorded creation time."""
        try:
            names = [
                entry.name
                for entry in os.scandir(self.sends)
                if entry.is_file(follow_symlinks=False) and entry.name.endswith(".json")
            ]
        except OSError:
            return []
        stamped: list[tuple[int, str]] = []
        for name in names:
            msg_id = valid_msg_id(name[: -len(".json")])
            if not msg_id:
                continue
            stamped.append((self._created_ms(msg_id), msg_id))
        stamped.sort(key=lambda item: (-item[0], item[1]))
        return [msg_id for _, msg_id in stamped]

    def _created_ms(self, msg_id: str) -> int:
        record = self.read_send(msg_id)
        created = record.get("created_unix_ms") if record else None
        if isinstance(created, int) and not isinstance(created, bool) and created > 0:
            return created
        try:
            return int(self.send_path(msg_id).stat().st_mtime * 1000)
        except OSError:
            return 0

    def newest_send_id(self) -> str | None:
        ids = self.send_ids()
        return ids[0] if ids else None

    # ---- idempotency keys ----------------------------------------------

    def key_path(self, key: str) -> Path:
        exact = valid_idempotency_key(key)
        if not exact:
            raise MessageError(IDEMPOTENCY_KEY_RULE)
        return self.keys / exact

    def claim_key(self, key: str, msg_id: str) -> None:
        """Bind one repeatable purpose to the one message id that serves it."""
        exact = valid_msg_id(msg_id)
        if not exact:
            raise MessageError("an idempotency claim needs an exact message id")
        self.ensure()
        _atomic_write(self.key_path(key), (exact + "\n").encode("ascii"))

    def msg_id_for_key(self, key: str) -> str | None:
        """The message id this purpose already has, or None.

        A claim whose send record is gone is not a claim: the record IS the
        message, and reusing an id nothing can be read back from would leave a
        repeat with no target rows to suppress against.
        """
        try:
            raw = _read_bounded(self.key_path(key), 64)
        except MessageError:
            return None
        msg_id = valid_msg_id(raw.decode("ascii", "ignore").strip())
        if not msg_id or not self.has_send(msg_id):
            return None
        return msg_id

    def claimed_msg_id_for_key(self, key: str) -> str | None:
        """Return a key's exact claim even when its send record is missing.

        A missing record after a durable claim is an unknown outcome, not
        proof that no provider call happened. Callers use this distinction to
        refuse an unsafe resend; retention removes record and claim together.
        """
        try:
            raw = _read_bounded(self.key_path(key), 64)
        except MessageError:
            return None
        return valid_msg_id(raw.decode("ascii", "ignore").strip()) or None

    # ---- threads -------------------------------------------------------

    def thread_path(self, key: str) -> Path:
        exact = valid_thread_key(key)
        if not exact:
            raise MessageError("thread key must be <claude|codex>:<uuid>")
        return self.threads / f"{exact}.jsonl"

    def append_thread(
        self,
        key: str,
        *,
        ts_unix_ms: int,
        direction: str,
        msg_id: str,
        text: str,
        via: str,
    ) -> None:
        if direction not in {"out", "in"}:
            raise MessageError("thread direction must be out or in")
        path = self.thread_path(key)
        self.ensure()
        entry = json.dumps(
            {
                "ts_unix_ms": int(ts_unix_ms),
                "dir": direction,
                "msg_id": valid_msg_id(msg_id) or str(msg_id)[:64],
                "text": str(text)[:MAX_TEXT_IN_THREAD],
                "via": str(via)[:64],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(entry + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_thread(self, key: str, limit: int = THREAD_TAIL_LINES) -> list[dict[str, Any]]:
        """The last ``limit`` parsed lines of one thread, oldest first."""
        try:
            path = self.thread_path(key)
        except MessageError:
            return []
        raw = _read_bounded(path, MAX_THREAD_TAIL_BYTES)
        if not raw:
            return []
        lines = raw.decode("utf-8", "replace").splitlines()
        entries: list[dict[str, Any]] = []
        for line in lines[-max(1, limit) * 2 :]:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                entries.append(value)
        return entries[-max(1, limit) :]

    # ---- unread --------------------------------------------------------

    def unread_path(self, key: str) -> Path:
        exact = valid_thread_key(key)
        if not exact:
            raise MessageError("thread key must be <claude|codex>:<uuid>")
        return self.unread / exact

    def mark_unread(self, key: str) -> None:
        self.ensure()
        path = self.unread_path(key)
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        os.close(os.open(path, flags, 0o600))

    def clear_unread(self, key: str) -> None:
        with contextlib.suppress(OSError, MessageError):
            self.unread_path(key).unlink()

    def unread_keys(self) -> list[str]:
        try:
            names = [
                entry.name
                for entry in os.scandir(self.unread)
                if entry.is_file(follow_symlinks=False)
            ]
        except OSError:
            return []
        return sorted(name for name in names if valid_thread_key(name))

    def unread_count(self) -> int:
        return len(self.unread_keys())

    # ---- retention -----------------------------------------------------

    def prune(self, now_ms: int) -> dict[str, list[str]]:
        """Drop sends past 90 days or past the newest 500, and dead threads.

        A thread and its unread marker outlive their send: a reply that lands
        after the send record is pruned still has somewhere to go. They are
        removed only once the conversation itself has been silent for the
        retention window.
        """
        dropped_sends: list[str] = []
        for index, msg_id in enumerate(self.send_ids()):
            created = self._created_ms(msg_id)
            too_old = created > 0 and now_ms - created > RETENTION_MS
            if index >= MAX_SENDS_KEPT or too_old:
                with contextlib.suppress(OSError, MessageError):
                    self.send_path(msg_id).unlink()
                    dropped_sends.append(msg_id)
        dropped_threads: list[str] = []
        try:
            entries = list(os.scandir(self.threads))
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(
                ".jsonl"
            ):
                continue
            key = valid_thread_key(entry.name[: -len(".jsonl")])
            if not key:
                continue
            try:
                last_activity = int(entry.stat().st_mtime * 1000)
            except OSError:
                continue
            if now_ms - last_activity <= RETENTION_MS:
                continue
            with contextlib.suppress(OSError, MessageError):
                self.thread_path(key).unlink()
                dropped_threads.append(key)
            self.clear_unread(key)
        return {
            "sends": dropped_sends,
            "threads": dropped_threads,
            "keys": self._prune_keys(),
        }

    def _prune_keys(self) -> list[str]:
        """Drop claims whose send has been pruned out from under them.

        A dangling claim would make the next repeat of that purpose resume an
        id with no record, so it is cleared and the purpose starts a fresh
        message instead.
        """
        try:
            entries = list(os.scandir(self.keys))
        except OSError:
            return []
        dropped: list[str] = []
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            key = valid_idempotency_key(entry.name)
            if not key or self.msg_id_for_key(key):
                continue
            with contextlib.suppress(OSError, MessageError):
                self.key_path(key).unlink()
                dropped.append(key)
        return dropped

    # ---- views ---------------------------------------------------------

    def list_sends(self, limit: int = MAX_LIST_SENDS) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for msg_id in self.send_ids()[: max(0, limit)]:
            record = self.read_send(msg_id)
            if record is None:
                continue
            targets: Sequence[Any] = record.get("targets") or ()
            statuses = [
                str(target.get("status"))
                for target in targets
                if isinstance(target, Mapping)
            ]
            rows.append(
                {
                    "msg_id": msg_id,
                    "created_unix_ms": record.get("created_unix_ms"),
                    "n_targets": len(statuses),
                    "n_replied": sum(1 for value in statuses if value == "replied"),
                    "n_unreachable": sum(
                        1 for value in statuses if value == "unreachable"
                    ),
                    "preview": preview(record.get("operator_text")),
                }
            )
        return rows
