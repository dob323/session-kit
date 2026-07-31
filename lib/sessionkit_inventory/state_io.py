"""Owner-only state file primitives shared by Session Kit modules."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from .common import CollectionError


def ensure_private_directory(path: Path) -> None:
    """Create or validate one current-owner mode-0700 real directory."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CollectionError(f"cannot inspect state directory {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise CollectionError(
            f"state directory must be a mode-0700 current-owner real directory: {path}"
        )


def read_private_json(
    path: Path,
    *,
    max_bytes: int,
    allow_missing: bool = False,
) -> Any:
    """Read bounded JSON from a current-owner mode-0600 regular file."""
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise CollectionError(f"state file does not exist: {path}") from None
    except OSError as exc:
        raise CollectionError(f"cannot open state file {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            raise CollectionError(
                f"state file must be a mode-0600 current-owner regular file: {path}"
            )
        payload = os.read(descriptor, max_bytes + 1)
        if len(payload) > max_bytes:
            raise CollectionError(f"state file exceeds {max_bytes} bytes: {path}")
        if os.read(descriptor, 1):
            raise CollectionError(f"state file exceeds {max_bytes} bytes: {path}")
    finally:
        os.close(descriptor)
    try:
        return json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectionError(f"state file is invalid JSON: {path}") from exc


def atomic_write_private_json(path: Path, value: Any) -> None:
    """Durably replace one owner-only JSON object without following symlinks."""
    ensure_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        payload = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while publishing state")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def create_private_json(path: Path, value: Any) -> bool:
    """Create one owner-only JSON file exactly once.

    Return false when another process already created the path.
    """
    ensure_private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while creating state")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return True
