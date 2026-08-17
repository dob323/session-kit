"""Owner-private JSON records on disk, shared by the worktree and receipt stores.

Both stores keep small documents the operator alone may read or change, and
both are read back by code that acts on them: a worktree record decides which
directory `sp` is allowed to remove, and a receipt decides whether a worker is
allowed to keep spending. A record that exists but cannot be trusted — a
symlink, another owner's file, a group-readable one, one larger than its
subsystem ever writes — is an error here, never a silent absence. Answering
"no such record" for a damaged file is how a live worktree gets orphaned or a
breached cap gets forgotten.

The write path is the one intake.py established: temp file in the destination
directory, 0600 before any bytes, fsync, atomic rename, fsync of the parent
directory.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any


class PrivateStoreError(ValueError):
    """A private record is unreadable, untrusted, or malformed."""


def private_directory(path: Path, *, label: str) -> Path:
    """Create or adopt one mode-0700 directory owned by this user."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
    ):
        raise PrivateStoreError(
            f"{label} must be a mode-0700 current-owner directory: {path}"
        )
    return path


def check_private_file(path: Path, info: os.stat_result, *, label: str) -> None:
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_uid != os.geteuid()
    ):
        raise PrivateStoreError(f"{label} must be an owner-private file: {path}")


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


def read_private_json(path: Path, *, limit: int, label: str) -> dict[str, Any] | None:
    """One private JSON object, or None when the file does not exist."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    check_private_file(path, info, label=label)
    if info.st_size > limit:
        raise PrivateStoreError(f"{label} exceeds {limit} bytes: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PrivateStoreError(f"cannot read {label}: {path}") from exc
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateStoreError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(document, dict):
        raise PrivateStoreError(f"{label} must be a JSON object: {path}")
    return document


def write_private_json(path: Path, document: dict[str, Any]) -> dict[str, Any]:
    atomic_private_write(path, canonical_bytes(document) + b"\n")
    return document


def canonical_bytes(document: dict[str, Any]) -> bytes:
    """One byte-for-byte reproducible encoding, so a digest means something."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def private_names(
    directory: Path, suffix: str, *, limit: int, strict: bool = False
) -> list[str]:
    """Sorted record names in one private directory, bounded and symlink-free.

    ``strict`` refuses a directory holding more records than the bound instead
    of returning the first ``limit`` of them. A caller that sums the records —
    spend against a cap — has to refuse, because a truncated list reads as a
    smaller total and a cap that under-counts is not a cap.
    """
    try:
        entries = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise PrivateStoreError(f"cannot list {directory}") from exc
    names: list[str] = []
    for entry in entries:
        if not entry.endswith(suffix) or entry.startswith("."):
            continue
        if (directory / entry).is_symlink():
            continue
        names.append(entry[: -len(suffix)] if suffix else entry)
        if len(names) > limit:
            if strict:
                raise PrivateStoreError(
                    f"{directory} holds more than {limit} records; "
                    "refusing a truncated read"
                )
            return names[:limit]
    return names
