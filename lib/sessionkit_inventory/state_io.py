"""Owner-only state file primitives shared by Session Kit modules."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable, Mapping

from .common import PROVIDERS, CollectionError, valid_uuid


def _state_paths(config: Mapping[str, Any]) -> dict[str, Path]:
    root = Path(config["state_dir"])
    return {
        "root": root,
        "inventory": root / "inventory.json",
        "manifest": root / "recovery-manifest.json",
        "pending": root / "recovery-pending.json",
        "aliases": root / "aliases.json",
        "aliases_archive": root / "aliases.json.migrated-v1",
        "aliases_source_backup": root / "aliases.json.pre-migration-v1",
        "terminal_numbers": root / "terminal-numbers.json",
        "terminal_numbers_epoch": root / "terminal-numbers.initialized.json",
        "terminal_numbers_retired": root / "terminal-numbers-retired.json",
        "color_reservations": root / "color-reservations.json",
        "provider_title_retries": root / "provider-title-retries.json",
        "provider_untitled_quarantine": root / "provider-untitled-quarantine",
        "config_lock": root / "config.lock",
        "lock": root / "inventory.lock",
    }


class StateLock:
    def __init__(self, root: Path, lock_path: Path):
        self.root = root
        self.lock_path = lock_path
        self.fd: int | None = None

    def __enter__(self) -> "StateLock":
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            root_stat = self.root.lstat()
        except OSError as exc:
            raise CollectionError(
                f"cannot inspect state directory {self.root}"
            ) from exc
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or stat.S_IMODE(root_stat.st_mode) != 0o700
            or root_stat.st_uid != os.geteuid()
        ):
            raise CollectionError(
                "state directory must be a mode-0700 current-owner real directory: "
                f"{self.root}"
            )
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
            opened = os.fstat(descriptor)
            before = self.lock_path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_uid != os.geteuid()
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise CollectionError(
                    "state lock must be a mode-0600 current-owner regular file: "
                    f"{self.lock_path}"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            after = self.lock_path.lstat()
            if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
                raise CollectionError(
                    f"state lock changed while locking: {self.lock_path}"
                )
        except BaseException:
            if "descriptor" in locals():
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            raise
        self.fd = descriptor
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON with mode 0600, fsync, and same-directory atomic replace."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_state_json(path: Path, *, load_json_file: Callable[[Path], Any]) -> Any:
    try:
        value = load_json_file(path)
    except (OSError, ValueError):
        return None
    return value


def _atomic_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_bounded_owner_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    exact_mode: int | None = None,
    allow_missing: bool = False,
) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise CollectionError(f"{label} does not exist: {path}")
    except OSError as exc:
        raise CollectionError(f"cannot open {label}: {path}") from exc
    try:
        before = os.fstat(descriptor)
        try:
            pathname = path.lstat()
        except OSError as exc:
            raise CollectionError(f"cannot inspect {label}: {path}") from exc
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > max_bytes
            or (exact_mode is not None and mode != exact_mode)
            or (exact_mode is None and mode & 0o022)
            or (before.st_dev, before.st_ino) != (pathname.st_dev, pathname.st_ino)
        ):
            raise CollectionError(f"unsafe {label}: {path}")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, 65536))
            if not block:
                raise CollectionError(f"short read from {label}: {path}")
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise CollectionError(f"{label} grew while reading: {path}")
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable):
            raise CollectionError(f"{label} changed while reading: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_private_launch_file(directory_fd: int, name: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > 8192
        ):
            raise CollectionError("unsafe retained launch record")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, 4096))
            if not block:
                raise CollectionError("short retained launch record")
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise CollectionError("retained launch record grew while reading")
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable):
            raise CollectionError("retained launch record changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _launch_fields(payload: bytes, count: int) -> list[str] | None:
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None
    if not text.endswith("\n") or text.count("\n") != 1 or "\r" in text:
        return None
    fields = text[:-1].split("\t")
    return fields if len(fields) == count else None


def _create_private_backup(
    path: Path,
    payload: bytes,
    *,
    read_bounded_owner_file: Callable[..., bytes | None],
    max_bytes: int,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = read_bounded_owner_file(
            path,
            label="alias migration backup",
            max_bytes=max_bytes,
            exact_mode=0o600,
        )
        if existing != payload:
            raise CollectionError(
                "alias migration backup already exists with other bytes"
            )
        return
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise CollectionError("could not write alias migration backup")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


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
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
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
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
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


def _empty_terminal_registry(boot_id: str, *, schema_version: int) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "boot_id": boot_id,
        "next_number": 1,
        "bindings": {},
    }


def _validate_terminal_registry(
    raw: Any,
    boot_id: str,
    *,
    schema_version: int,
    empty_registry: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "schema_version",
        "boot_id",
        "next_number",
        "bindings",
    }:
        raise CollectionError("terminal number registry has an invalid schema")
    if raw.get("schema_version") != schema_version:
        raise CollectionError("terminal number registry has an unsupported schema")
    stored_boot = raw.get("boot_id")
    next_number = raw.get("next_number")
    bindings = raw.get("bindings")
    if (
        not isinstance(stored_boot, str)
        or not stored_boot
        or isinstance(next_number, bool)
        or not isinstance(next_number, int)
        or next_number <= 0
        or not isinstance(bindings, Mapping)
    ):
        raise CollectionError("terminal number registry has invalid fields")
    checked: dict[str, int] = {}
    ai_by_number: dict[int, str] = {}
    generation_by_number: dict[int, str] = {}
    for key, value in bindings.items():
        if (
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
        ):
            raise CollectionError("terminal number registry has an invalid binding")
        if key.startswith("ai:"):
            _, separator, remainder = key.partition(":")
            provider, separator, uuid = remainder.partition(":")
            if (
                not separator
                or provider not in PROVIDERS
                or valid_uuid(uuid) != uuid
                or (value in ai_by_number and ai_by_number[value] != key)
            ):
                raise CollectionError(
                    "terminal number registry has a conflicting AI binding"
                )
            ai_by_number[value] = key
        elif re.fullmatch(r"generation:[0-9a-f]{64}", key):
            if value in generation_by_number and generation_by_number[value] != key:
                raise CollectionError(
                    "terminal number registry has duplicate generation bindings"
                )
            generation_by_number[value] = key
        else:
            raise CollectionError("terminal number registry has an invalid key")
        checked[key] = value
    if checked and next_number <= max(checked.values()):
        raise CollectionError("terminal number registry would reuse a number")
    if stored_boot != boot_id:
        return empty_registry(boot_id)
    return {
        "schema_version": schema_version,
        "boot_id": stored_boot,
        "next_number": next_number,
        "bindings": checked,
    }


def _read_terminal_registry(
    path: Path,
    boot_id: str,
    epoch_path: Path | None = None,
    *,
    schema_version: int,
    max_bytes: int,
    read_bounded_owner_file: Callable[..., bytes | None],
    empty_registry: Callable[[str], dict[str, Any]],
    validate_registry: Callable[[Any, str], dict[str, Any]],
) -> dict[str, Any]:
    payload = read_bounded_owner_file(
        path,
        label="terminal number registry",
        max_bytes=max_bytes,
        exact_mode=0o600,
        allow_missing=True,
    )
    if payload is None:
        if epoch_path is not None:
            epoch_payload = read_bounded_owner_file(
                epoch_path,
                label="terminal number initialization receipt",
                max_bytes=4096,
                exact_mode=0o600,
                allow_missing=True,
            )
            if epoch_payload is not None:
                try:
                    epoch = json.loads(epoch_payload.decode("utf-8", "strict"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise CollectionError(
                        "terminal number initialization receipt is invalid"
                    ) from exc
                if (
                    not isinstance(epoch, Mapping)
                    or set(epoch) != {"schema_version", "boot_id"}
                    or epoch.get("schema_version") != schema_version
                    or not isinstance(epoch.get("boot_id"), str)
                    or not epoch["boot_id"]
                ):
                    raise CollectionError(
                        "terminal number initialization receipt has an invalid schema"
                    )
                if epoch["boot_id"] == boot_id:
                    raise CollectionError(
                        "same-boot terminal number registry disappeared"
                    )
        return empty_registry(boot_id)
    try:
        raw = json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectionError("terminal number registry is invalid JSON") from exc
    return validate_registry(raw, boot_id)


def _terminal_generation_key(
    inventory: Mapping[str, Any], item: Mapping[str, Any], boot_id: str
) -> str | None:
    shell = item.get("shpool_shell")
    values = (
        item.get("started_at_unix_ms"),
        shell.get("pid") if isinstance(shell, Mapping) else None,
        shell.get("process_start_ticks") if isinstance(shell, Mapping) else None,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in values
    ):
        return None
    raw_id = item.get("shpool_id_raw")
    if not isinstance(raw_id, str) or not raw_id:
        return None
    payload = json.dumps(
        [boot_id, raw_id, values[0], values[1], values[2]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"generation:{hashlib.sha256(payload).hexdigest()}"
