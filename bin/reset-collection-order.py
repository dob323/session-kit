#!/usr/bin/env python3
"""Offline, preserving reset for irrecoverably lost collection ordering."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import stat


DOCUMENT_NAMES = (
    "inventory.json",
    "terminal-numbers.json",
    "terminal-numbers-retired.json",
    "terminal-numbers.initialized.json",
    "recovery-manifest.json",
    "recovery-pending.json",
)
SEQUENCE_NAMES = ("collection-sequence.json", "collection-sequence-floor.json")
MARKER_NAME = "collection-markers"
RESET_NAMES = (*DOCUMENT_NAMES, *SEQUENCE_NAMES, MARKER_NAME)
PENDING_NAME = ".collection-order-reset.pending"
MANIFEST_NAME = "reset-manifest.json"


def refuse(message: str) -> None:
    raise SystemExit(f"session-kit: refusing collection-order reset: {message}")


def entry_exists(path: Path) -> bool:
    return os.path.lexists(path)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_private_root(root: Path) -> None:
    try:
        info = root.lstat()
    except OSError as exc:
        refuse(f"cannot inspect state directory {root}: {exc}")
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
    ):
        refuse(f"state directory is not an owner-only real directory: {root}")


def readable_sequence(path: Path) -> bool:
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_size > 8192
        ):
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    sequence = value.get("last_collection_start") if isinstance(value, dict) else None
    return isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > 0


def require_safe_entry(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        refuse(f"cannot inspect state entry {path}: {exc}")
    if info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode):
        refuse(f"unsafe state entry: {path}")
    if stat.S_ISREG(info.st_mode):
        if stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            refuse(f"unsafe state file: {path}")
        return
    if stat.S_ISDIR(info.st_mode):
        if stat.S_IMODE(info.st_mode) != 0o700:
            refuse(f"unsafe state directory: {path}")
        try:
            children = list(path.iterdir())
        except OSError as exc:
            refuse(f"cannot inspect state directory {path}: {exc}")
        for child in children:
            require_safe_entry(child)
        return
    refuse(f"unsupported state entry: {path}")


def write_manifest(path: Path, names: list[str]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"schema_version": 1, "entries": names}, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_manifest(path: Path) -> list[str]:
    require_safe_entry(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        refuse(f"cannot read pending reset manifest: {exc}")
    names = value.get("entries") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(names, list)
        or not names
        or any(name not in RESET_NAMES for name in names)
        or len(set(names)) != len(names)
    ):
        refuse("pending reset manifest is invalid")
    return names


def state_root() -> Path:
    explicit = os.environ.get("SESSION_KIT_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    state_home = Path(
        os.environ.get("XDG_STATE_HOME", os.fspath(Path.home() / ".local/state"))
    ).expanduser()
    return state_home / "session-kit"


def main() -> int:
    root = state_root()
    require_private_root(root)
    lock = root / "inventory-v2.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(lock, flags, 0o600)
    try:
        lock_info = os.fstat(descriptor)
        lock_path_info = lock.lstat()
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or stat.S_IMODE(lock_info.st_mode) != 0o600
            or lock_info.st_uid != os.geteuid()
            or lock_info.st_nlink != 1
            or (lock_info.st_dev, lock_info.st_ino)
            != (lock_path_info.st_dev, lock_path_info.st_ino)
        ):
            refuse(f"unsafe publication lock: {lock}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked_path_info = lock.lstat()
        if (lock_info.st_dev, lock_info.st_ino) != (
            locked_path_info.st_dev,
            locked_path_info.st_ino,
        ):
            refuse(f"publication lock changed while locking: {lock}")
        pending = root / PENDING_NAME
        if entry_exists(pending):
            require_safe_entry(pending)
            names = read_manifest(pending / MANIFEST_NAME)
        else:
            for name in SEQUENCE_NAMES:
                if readable_sequence(root / name):
                    refuse("a durable sequence record is readable; reset is unnecessary")
            names = [name for name in RESET_NAMES if entry_exists(root / name)]
            if not names:
                refuse("no lost collection-order state exists")
            for name in names:
                require_safe_entry(root / name)
            pending.mkdir(mode=0o700)
            write_manifest(pending / MANIFEST_NAME, names)
            sync_directory(pending)
            sync_directory(root)

        for name in names:
            source = root / name
            target = pending / name
            if entry_exists(target):
                require_safe_entry(target)
                if entry_exists(source):
                    refuse(f"reset entry exists in both live state and archive: {name}")
                continue
            if not entry_exists(source):
                refuse(f"reset entry disappeared before it was archived: {name}")
            require_safe_entry(source)
            os.replace(source, target)
            sync_directory(root)
            sync_directory(pending)

        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = root / f"collection-order-reset-{stamp}"
        if entry_exists(archive):
            refuse(f"archive path already exists: {archive}")
        os.replace(pending, archive)
        sync_directory(root)
        print(archive)
        return 0
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
