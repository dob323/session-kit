"""Crash-recoverable compact segmented commitments for source authority."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping


ID_RE = re.compile(r"\A[0-9a-f]{64}\Z")
DEFAULT_SEGMENT_BYTES = 256 * 1024
MAX_SEGMENT_BYTES = 2 * 1024 * 1024


class LedgerError(ValueError):
    pass


def canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def atomic_private_write(path: Path, payload: bytes) -> None:
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


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise LedgerError(f"source ledger directory is unsafe: {path}")


def private_read(path: Path, maximum: int) -> bytes:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_size > maximum
    ):
        raise LedgerError(f"source ledger file is unsafe: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise LedgerError("source ledger file changed while opening")
        raw = os.read(descriptor, maximum + 1)
        if len(raw) > maximum:
            raise LedgerError("source ledger file exceeds its bound")
        return raw
    finally:
        os.close(descriptor)


class SegmentedLedger:
    """Numbered compact segments, immutable seals, index, checkpoint, and WAL."""

    def __init__(self, root: Path, *, segment_bytes: int = DEFAULT_SEGMENT_BYTES) -> None:
        self.root = root
        self.segments = root / "segments"
        self.index = root / "index"
        self.transactions = root / "transactions"
        self.checkpoint = root / "checkpoint.json"
        if not isinstance(segment_bytes, int) or not 512 <= segment_bytes <= MAX_SEGMENT_BYTES:
            raise LedgerError("source ledger segment bound must be 512 bytes to 2 MiB")
        self.segment_bytes = segment_bytes

    def ensure(self) -> None:
        for path in (self.root, self.segments, self.index, self.transactions):
            private_directory(path)

    @staticmethod
    def _segment_name(number: int) -> str:
        return f"{number:08d}.jsonl"

    def _segment_path(self, number: int) -> Path:
        return self.segments / self._segment_name(number)

    def _seal_path(self, number: int) -> Path:
        return self.segments / f"{number:08d}.seal.json"

    def _segment_numbers(self) -> list[int]:
        self.ensure()
        numbers = sorted(
            int(match.group(1))
            for entry in os.scandir(self.segments)
            if entry.is_file(follow_symlinks=False)
            and (match := re.fullmatch(r"([0-9]{8})\.jsonl", entry.name))
        )
        if numbers and numbers != list(range(1, numbers[-1] + 1)):
            raise LedgerError("source ledger has a missing segment")
        return numbers

    def scan(self) -> dict[str, Any]:
        """Verify every compact commitment and every immutable segment seal."""
        rows: dict[str, dict[str, Any]] = {}
        previous_event_hash = ""
        previous_seal_hash = ""
        sequence = 0
        numbers = self._segment_numbers()
        for position, number in enumerate(numbers):
            path = self._segment_path(number)
            raw = private_read(path, self.segment_bytes)
            lines = raw.splitlines()
            first_sequence = sequence + 1
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise LedgerError("source ledger segment is malformed") from exc
                if not isinstance(row, Mapping):
                    raise LedgerError("source ledger commitment is malformed")
                checked = dict(row)
                ledger_hash = checked.pop("ledger_hash", None)
                event_id = checked.get("event_id")
                event_hash = checked.get("event_hash")
                sequence += 1
                if (
                    checked.get("schema_version") != 2
                    or checked.get("sequence") != sequence
                    or checked.get("previous_event_hash", "") != previous_event_hash
                    or not isinstance(event_id, str)
                    or not ID_RE.fullmatch(event_id)
                    or event_id in rows
                    or not isinstance(event_hash, str)
                    or not ID_RE.fullmatch(event_hash)
                    or not isinstance(ledger_hash, str)
                    or hashlib.sha256(canonical(checked)).hexdigest() != ledger_hash
                ):
                    raise LedgerError("source ledger commitment chain mismatch")
                rows[event_id] = {
                    "segment": number,
                    "sequence": sequence,
                    "event_hash": event_hash,
                    "ledger_hash": ledger_hash,
                }
                previous_event_hash = event_hash
            sealed = self._seal_path(number)
            must_be_sealed = position < len(numbers) - 1
            if must_be_sealed or sealed.exists() or sealed.is_symlink():
                try:
                    seal = json.loads(private_read(sealed, 4096))
                except (OSError, ValueError) as exc:
                    raise LedgerError("source ledger segment seal is missing or malformed") from exc
                unsigned = dict(seal) if isinstance(seal, Mapping) else {}
                seal_hash = unsigned.pop("seal_hash", None)
                if (
                    unsigned.get("schema_version") != 2
                    or unsigned.get("segment") != number
                    or unsigned.get("first_sequence") != first_sequence
                    or unsigned.get("last_sequence") != sequence
                    or unsigned.get("head_event_hash", "") != previous_event_hash
                    or unsigned.get("previous_seal_hash", "") != previous_seal_hash
                    or unsigned.get("segment_sha256") != hashlib.sha256(raw).hexdigest()
                    or not isinstance(seal_hash, str)
                    or hashlib.sha256(canonical(unsigned)).hexdigest() != seal_hash
                ):
                    raise LedgerError("source ledger segment seal mismatch")
                previous_seal_hash = seal_hash
        return {
            "rows": rows,
            "head_event_hash": previous_event_hash,
            "last_sequence": sequence,
            "active_segment": numbers[-1] if numbers else 1,
            "previous_seal_hash": previous_seal_hash,
        }

    def _seal(self, number: int, state: Mapping[str, Any]) -> str:
        path = self._segment_path(number)
        raw = private_read(path, self.segment_bytes)
        rows = [json.loads(line) for line in raw.splitlines()]
        if not rows:
            raise LedgerError("an empty source ledger segment cannot be sealed")
        unsigned = {
            "schema_version": 2,
            "segment": number,
            "first_sequence": int(rows[0]["sequence"]),
            "last_sequence": int(rows[-1]["sequence"]),
            "head_event_hash": str(rows[-1]["event_hash"]),
            "previous_seal_hash": str(state["previous_seal_hash"]),
            "segment_sha256": hashlib.sha256(raw).hexdigest(),
        }
        unsigned["seal_hash"] = hashlib.sha256(canonical(unsigned)).hexdigest()
        atomic_private_write(self._seal_path(number), canonical(unsigned) + b"\n")
        return str(unsigned["seal_hash"])

    def _write_checkpoint(self, state: Mapping[str, Any]) -> None:
        payload = {
            "schema_version": 2,
            "active_segment": int(state["active_segment"]),
            "last_sequence": int(state["last_sequence"]),
            "head_event_hash": str(state["head_event_hash"]),
            "previous_seal_hash": str(state["previous_seal_hash"]),
        }
        payload["checkpoint_hash"] = hashlib.sha256(canonical(payload)).hexdigest()
        atomic_private_write(self.checkpoint, canonical(payload) + b"\n")

    def _ensure_index(self, event_id: str, locator: Mapping[str, Any]) -> None:
        """Rebuild only absent/malformed derived indexes; conflicts fail closed."""
        path = self.index / f"{event_id}.json"
        try:
            raw = private_read(path, 4096)
        except FileNotFoundError:
            atomic_private_write(path, canonical(locator) + b"\n")
            return
        except OSError as exc:
            raise LedgerError("source event index cannot be inspected") from exc
        try:
            indexed = json.loads(raw)
        except ValueError:
            atomic_private_write(path, canonical(locator) + b"\n")
            return
        if not isinstance(indexed, Mapping):
            atomic_private_write(path, canonical(locator) + b"\n")
            return
        if dict(indexed) != dict(locator):
            raise LedgerError("source event index disagrees with its segment")

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        self.ensure()
        state = self.scan()
        event_id = str(event.get("event_id") or "")
        if event_id in state["rows"]:
            locator = dict(state["rows"][event_id])
            if locator["event_hash"] != event.get("event_hash"):
                raise LedgerError("source ledger event id collision")
            self._ensure_index(event_id, locator)
            return locator
        if event.get("previous_event_hash", "") != state["head_event_hash"]:
            raise LedgerError("source event does not extend the recovered ledger head")
        sequence = int(state["last_sequence"]) + 1
        number = int(state["active_segment"])
        unsigned = {
            "schema_version": 2,
            "sequence": sequence,
            "event_id": event_id,
            "event_hash": event.get("event_hash"),
            "previous_event_hash": event.get("previous_event_hash", ""),
            "provider": event.get("provider"),
            "session_id": event.get("session_id"),
            "submission_key": event.get("submission_key"),
            "prompt_sha256": event.get("prompt_sha256"),
            "recorded_unix_ms": event.get("recorded_unix_ms"),
        }
        unsigned["ledger_hash"] = hashlib.sha256(canonical(unsigned)).hexdigest()
        encoded = canonical(unsigned) + b"\n"
        path = self._segment_path(number)
        current_size = path.lstat().st_size if path.exists() else 0
        previous_seal = str(state["previous_seal_hash"])
        already_sealed = self._seal_path(number).exists()
        if current_size and (
            already_sealed or current_size + len(encoded) > self.segment_bytes
        ):
            if not already_sealed:
                previous_seal = self._seal(number, state)
            number += 1
            path = self._segment_path(number)
        current = private_read(path, self.segment_bytes) if path.exists() else b""
        if len(current) + len(encoded) > self.segment_bytes:
            raise LedgerError("one source ledger commitment exceeds its segment bound")
        # Atomic replacement makes a killed writer leave either the old complete
        # segment or the new complete segment. The WAL then finishes any index,
        # event-object, pointer, or checkpoint work on the next capture.
        atomic_private_write(path, current + encoded)
        locator = {
            "segment": number,
            "sequence": sequence,
            "event_hash": event["event_hash"],
            "ledger_hash": unsigned["ledger_hash"],
        }
        self._ensure_index(event_id, locator)
        self._write_checkpoint(
            {
                "active_segment": number,
                "last_sequence": sequence,
                "head_event_hash": event["event_hash"],
                "previous_seal_hash": previous_seal,
            }
        )
        return locator

    def verify_event(self, event_id: str, event_hash: str) -> dict[str, Any]:
        state = self.scan()
        locator = state["rows"].get(event_id)
        if locator is None or locator["event_hash"] != event_hash:
            raise LedgerError("source event is absent from the compact ledger")
        path = self.index / f"{event_id}.json"
        try:
            indexed = json.loads(private_read(path, 4096))
        except (OSError, ValueError) as exc:
            raise LedgerError("source event index is missing or malformed") from exc
        if indexed != locator:
            raise LedgerError("source event index disagrees with its segment")
        return dict(locator)

    def begin(self, event: Mapping[str, Any], tuple_id: str) -> None:
        payload = {"schema_version": 2, "tuple_id": tuple_id, "event": dict(event)}
        atomic_private_write(
            self.transactions / f"{event['event_id']}.json", canonical(payload) + b"\n"
        )

    def pending(self) -> list[dict[str, Any]]:
        self.ensure()
        rows: list[dict[str, Any]] = []
        for entry in sorted(os.scandir(self.transactions), key=lambda item: item.name):
            if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".json"):
                continue
            try:
                value = json.loads(private_read(Path(entry.path), 2 * 1024 * 1024))
            except (OSError, ValueError) as exc:
                raise LedgerError("source event WAL is malformed") from exc
            if (
                not isinstance(value, Mapping)
                or value.get("schema_version") != 2
                or not isinstance(value.get("event"), Mapping)
                or not isinstance(value.get("tuple_id"), str)
            ):
                raise LedgerError("source event WAL is malformed")
            rows.append(dict(value))
        return rows

    def finish(self, event_id: str) -> None:
        path = self.transactions / f"{event_id}.json"
        path.unlink()
        directory = os.open(self.transactions, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def referenced_segments(self, event_ids: set[str]) -> set[int]:
        """Retention fence: every referenced event keeps its complete segment."""
        state = self.scan()
        missing = event_ids - set(state["rows"])
        if missing:
            raise LedgerError("a retained authority event is absent from the ledger")
        return {int(state["rows"][event_id]["segment"]) for event_id in event_ids}
