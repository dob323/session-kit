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
import threading
from typing import Any, Callable, Iterable, Mapping

from .common import PROVIDERS, CollectionError, valid_uuid


_PUBLISHING_LOCK_NAME = "inventory-v2.lock"
_LEGACY_PUBLISHING_LOCK_NAME = "inventory.lock"
_LEGACY_LOCK_FENCE = b"session-kit publishing lock generation 2\n"
_LEGACY_ROLLBACK_MARKER_NAME = "inventory-rollback-v2"
_LEGACY_ROLLBACK_MARKER = b"session-kit legacy rollback selected\n"
_COLLECTION_DOCUMENT_KEYS = (
    "inventory",
    "terminal_numbers",
    "terminal_numbers_retired",
    "terminal_numbers_epoch",
    "manifest",
    "pending",
)
_HELD_PUBLISHING_LOCKS = threading.local()


def _publishing_lock_key(root: Path) -> str:
    return os.path.realpath(root)


def _held_publishing_locks() -> set[str]:
    held = getattr(_HELD_PUBLISHING_LOCKS, "roots", None)
    if held is None:
        held = set()
        _HELD_PUBLISHING_LOCKS.roots = held
    return held


def _require_pending_publication_lock(path: Path) -> None:
    if _publishing_lock_key(path.parent) not in _held_publishing_locks():
        raise CollectionError(
            f"{path.name} publication requires this thread to hold "
            f"{_PUBLISHING_LOCK_NAME}"
        )


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
        "collection_sequence": root / "collection-sequence.json",
        "collection_sequence_floor": root / "collection-sequence-floor.json",
        "collection_markers": root / "collection-markers",
        "config_lock": root / "config.lock",
        "legacy_lock": root / _LEGACY_PUBLISHING_LOCK_NAME,
        "legacy_rollback_marker": root / _LEGACY_ROLLBACK_MARKER_NAME,
        "lock": root / _PUBLISHING_LOCK_NAME,
    }


class StateLock:
    def __init__(self, root: Path, lock_path: Path):
        self.root = root
        self.lock_path = lock_path
        self.fd: int | None = None

    def _validate_root(self) -> None:
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

    def _open_and_lock(self, path: Path) -> int:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
            opened = os.fstat(descriptor)
            before = path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_uid != os.geteuid()
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise CollectionError(
                    "state lock must be a mode-0600 current-owner regular file: "
                    f"{path}"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            after = path.lstat()
            if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
                raise CollectionError(
                    f"state lock changed while locking: {path}"
                )
        except BaseException:
            if "descriptor" in locals():
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            raise
        return descriptor

    def _legacy_lock_is_fenced(self, path: Path) -> bool:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CollectionError(f"cannot inspect legacy state lock: {path}") from exc
        try:
            metadata = os.fstat(descriptor)
            pathname = path.lstat()
            content = os.read(descriptor, len(_LEGACY_LOCK_FENCE) + 1)
        except OSError as exc:
            raise CollectionError(f"cannot inspect legacy state lock: {path}") from exc
        finally:
            os.close(descriptor)
        if (
            stat.S_ISREG(metadata.st_mode)
            and stat.S_IMODE(metadata.st_mode) == 0o400
            and metadata.st_uid == os.geteuid()
            and metadata.st_nlink == 1
            and (metadata.st_dev, metadata.st_ino)
            == (pathname.st_dev, pathname.st_ino)
            and content == _LEGACY_LOCK_FENCE
        ):
            return True
        if stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == 0o600:
            return False
        raise CollectionError(f"legacy state lock is unsafe: {path}")

    def _publish_legacy_lock_fence(self, path: Path) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.root)
        try:
            os.fchmod(descriptor, 0o400)
            os.write(descriptor, _LEGACY_LOCK_FENCE)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)

    def _legacy_rollback_is_active(self, path: Path) -> bool:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CollectionError(
                f"cannot inspect legacy rollback publication hold: {path}"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            pathname = path.lstat()
            content = os.read(descriptor, len(_LEGACY_ROLLBACK_MARKER) + 1)
        except OSError as exc:
            raise CollectionError(
                f"cannot inspect legacy rollback publication hold: {path}"
            ) from exc
        finally:
            os.close(descriptor)
        if (
            stat.S_ISREG(metadata.st_mode)
            and stat.S_IMODE(metadata.st_mode) == 0o400
            and metadata.st_uid == os.geteuid()
            and metadata.st_nlink == 1
            and (metadata.st_dev, metadata.st_ino)
            == (pathname.st_dev, pathname.st_ino)
            and content == _LEGACY_ROLLBACK_MARKER
        ):
            return True
        raise CollectionError(f"legacy rollback publication hold is unsafe: {path}")

    def _launched_release_is_current(self) -> bool:
        release_raw = os.environ.get("SESSION_KIT_RELEASE_DIR")
        root_raw = os.environ.get("SESSION_KIT_ROOT")
        if not root_raw:
            home_raw = os.environ.get("HOME")
            if home_raw:
                root_raw = os.fspath(Path(home_raw) / ".local/lib/session-kit")
        if not release_raw or not root_raw:
            return False
        release = Path(release_raw)
        root = Path(root_raw)
        current = root / "current"
        releases = root / "releases"
        if not release.is_absolute() or not root.is_absolute():
            return False
        try:
            resolved_release = release.resolve(strict=True)
            return (
                resolved_release.parent == releases.resolve(strict=True)
                and resolved_release == current.resolve(strict=True)
            )
        except OSError:
            return False

    def _respect_legacy_rollback(self) -> None:
        marker = self.root / _LEGACY_ROLLBACK_MARKER_NAME
        if not self._legacy_rollback_is_active(marker):
            return
        if not self._launched_release_is_current():
            raise CollectionError(
                "legacy rollback selected; this older generation-two publisher "
                "is read-only"
            )
        # A newly launched, currently selected generation-two release is the
        # forward-update boundary. It may retire the rollback hold while it
        # owns the versioned lock, then recreate the legacy refusal fence.
        self._legacy_rollback_is_active(marker)
        try:
            marker.unlink()
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise CollectionError(
                "cannot retire legacy rollback publication hold"
            ) from exc

    def _open_versioned_publishing_lock(self) -> int:
        legacy = self.root / _LEGACY_PUBLISHING_LOCK_NAME
        while True:
            if self._legacy_lock_is_fenced(legacy):
                descriptor = self._open_and_lock(self.lock_path)
                try:
                    self._respect_legacy_rollback()
                except BaseException:
                    with contextlib.suppress(OSError):
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                    raise
                return descriptor
            try:
                legacy_descriptor = self._open_and_lock(legacy)
            except CollectionError:
                # Another new process may have replaced the legacy inode while
                # this process waited on it. The exact fence is the only safe
                # reason to retry through the new lock generation.
                if self._legacy_lock_is_fenced(legacy):
                    continue
                raise
            versioned_descriptor: int | None = None
            try:
                versioned_descriptor = self._open_and_lock(self.lock_path)
                self._respect_legacy_rollback()
                self._publish_legacy_lock_fence(legacy)
                return versioned_descriptor
            except BaseException:
                if versioned_descriptor is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(versioned_descriptor, fcntl.LOCK_UN)
                    os.close(versioned_descriptor)
                raise
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(legacy_descriptor, fcntl.LOCK_UN)
                os.close(legacy_descriptor)

    def __enter__(self) -> "StateLock":
        self._validate_root()
        descriptor = (
            self._open_versioned_publishing_lock()
            if self.lock_path == self.root / _PUBLISHING_LOCK_NAME
            else self._open_and_lock(self.lock_path)
        )
        self.fd = descriptor
        if self.lock_path == self.root / _PUBLISHING_LOCK_NAME:
            _held_publishing_locks().add(_publishing_lock_key(self.root))
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
                self.fd = None
            finally:
                if self.lock_path == self.root / _PUBLISHING_LOCK_NAME:
                    _held_publishing_locks().discard(
                        _publishing_lock_key(self.root)
                    )


@contextlib.contextmanager
def publication_lock(paths: Mapping[str, Path]):
    """Acquire the publishing lock unless this thread already owns it."""
    key = _publishing_lock_key(paths["root"])
    if key in _held_publishing_locks():
        yield
        return
    with StateLock(paths["root"], paths["lock"]):
        yield


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON with mode 0600, fsync, and same-directory atomic replace."""
    _atomic_write_bytes(path, _atomic_json_bytes(payload))


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while publishing state")
        view = view[written:]


def _read_back_descriptor(descriptor: int, expected_size: int) -> bytes:
    reader = os.dup(descriptor)
    try:
        os.lseek(reader, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = expected_size + 1
        while remaining:
            block = os.read(reader, min(remaining, 65536))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)
    finally:
        os.close(reader)


def _prove_published_descriptor(descriptor: int, path: Path) -> None:
    written = os.fstat(descriptor)
    try:
        pathname = path.lstat()
    except OSError as exc:
        raise CollectionError(
            f"state file path no longer names the file that was published: {path}"
        ) from exc
    if (
        not stat.S_ISREG(pathname.st_mode)
        or (written.st_dev, written.st_ino) != (pathname.st_dev, pathname.st_ino)
    ):
        raise CollectionError(
            f"state file path no longer names the file that was published: {path}"
        )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        if _read_back_descriptor(descriptor, len(payload)) != payload:
            raise CollectionError(
                f"state file changed while it was being written: {path}"
            )
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _prove_published_descriptor(descriptor, path)
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _collection_marker_path(paths: Mapping[str, Path], path: Path) -> Path:
    if path.parent != paths["root"] or not path.name or "/" in path.name:
        raise CollectionError(f"collection document is outside the state root: {path}")
    return paths["collection_markers"] / f"{path.name}.json"


def _read_collection_sequence_document(path: Path) -> int | None:
    try:
        value = read_private_json(path, max_bytes=8192, allow_missing=True)
    except (CollectionError, OSError, ValueError):
        return None
    sequence = value.get("last_collection_start") if isinstance(value, Mapping) else None
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        return None
    return sequence


def _lost_collection_order_exists(paths: Mapping[str, Path]) -> bool:
    """Whether published state proves this is not a first allocation.

    This check must not create or repair anything.  With both allocation
    records gone, even a corrupt document or marker is evidence that a prior
    collector may still hold a larger sequence.
    """
    for key in _COLLECTION_DOCUMENT_KEYS:
        path = paths[key]
        if os.path.lexists(path):
            return True

    marker_dir = paths["collection_markers"]
    try:
        marker_info = marker_dir.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CollectionError("cannot inspect collection markers") from exc
    if not stat.S_ISDIR(marker_info.st_mode) or stat.S_ISLNK(marker_info.st_mode):
        return True
    try:
        next(marker_dir.iterdir())
    except StopIteration:
        return False
    except OSError as exc:
        raise CollectionError("cannot inspect collection markers") from exc
    return True


def read_collection_marker(paths: Mapping[str, Path], path: Path) -> int | None:
    """Return a document marker only when it still names the exact bytes."""
    marker_path = _collection_marker_path(paths, path)
    try:
        marker = read_private_json(marker_path, max_bytes=8192, allow_missing=True)
        content = _read_bounded_owner_file(
            path,
            label="published collection document",
            max_bytes=16 * 1024 * 1024,
            exact_mode=0o600,
            allow_missing=True,
        )
    except (CollectionError, OSError, ValueError):
        return None
    sequence = marker.get("collection_start") if isinstance(marker, Mapping) else None
    digest = marker.get("content_sha256") if isinstance(marker, Mapping) else None
    if (
        content is None
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= 0
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or hashlib.sha256(content).hexdigest() != digest
    ):
        return None
    return sequence


def _read_collection_witness(paths: Mapping[str, Path], path: Path) -> int | None:
    """Return the durable order witness even while its document is replacing.

    A writer publishes the witness before it replaces the document.  During
    that short interval the digest intentionally names the incoming bytes, not
    the incumbent bytes.  Freshness checks must still honor the sequence or an
    older collector could use the digest mismatch as permission to publish.
    """
    try:
        marker = read_private_json(
            _collection_marker_path(paths, path),
            max_bytes=8192,
            allow_missing=True,
        )
    except (CollectionError, OSError, ValueError):
        return None
    sequence = marker.get("collection_start") if isinstance(marker, Mapping) else None
    digest = marker.get("content_sha256") if isinstance(marker, Mapping) else None
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= 0
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        return None
    return sequence


def allocate_collection_start(paths: Mapping[str, Path]) -> tuple[int, str | None]:
    """Allocate one durable collection-start order while inventory.lock is held."""
    counter_path = paths["collection_sequence"]
    prior = _read_collection_sequence_document(counter_path)
    floor_path = paths.get(
        "collection_sequence_floor",
        paths["root"] / "collection-sequence-floor.json",
    )
    durable_floor = _read_collection_sequence_document(floor_path)
    counter_exists = os.path.lexists(counter_path)
    floor_exists = os.path.lexists(floor_path)
    if prior is None and durable_floor is None and (
        counter_exists
        or floor_exists
        or _lost_collection_order_exists(paths)
    ):
        raise CollectionError(
            "both durable collection sequence records are unreadable; "
            "collection publication is read-only"
        )
    known = max(prior or 0, durable_floor or 0)
    marker_dir = paths["collection_markers"]
    ensure_private_directory(marker_dir)
    try:
        marker_paths = list(marker_dir.iterdir())
    except OSError as exc:
        raise CollectionError("cannot inspect collection markers") from exc
    for marker_path in marker_paths:
        if marker_path.name.startswith(".") or not marker_path.name.endswith(".json"):
            continue
        document_name = marker_path.name.removesuffix(".json")
        candidate = read_collection_marker(paths, paths["root"] / document_name)
        if candidate is not None:
            known = max(known, candidate)
    sequence = known + 1
    diagnostic = None
    if counter_exists and prior is None:
        diagnostic = (
            "collection sequence was unreadable; rebuilt it from the durable "
            "allocation floor and published markers"
        )
    # The floor is the allocation record.  Publish and fsync it before the
    # ordinary counter and before returning the sequence to the collector, so
    # a later counter reset cannot undercut an allocation still in flight.
    atomic_write_json(
        floor_path,
        {"schema_version": 1, "last_collection_start": sequence},
    )
    atomic_write_json(
        counter_path,
        {"schema_version": 1, "last_collection_start": sequence},
    )
    return sequence, diagnostic


def preflight_collection_documents(
    paths: Mapping[str, Path],
    document_keys: Iterable[str],
    sequence: int | None,
) -> tuple[list[str], list[str]]:
    """Compare one incoming reading with every incumbent before any mutation."""
    refused: list[str] = []
    diagnostics: list[str] = []
    incoming_sequence = (
        sequence
        if not isinstance(sequence, bool)
        and isinstance(sequence, int)
        and sequence > 0
        else None
    )
    for key in document_keys:
        path = paths[key]
        marker = read_collection_marker(paths, path)
        witness = _read_collection_witness(paths, path)
        exists = path.exists() or path.is_symlink()
        if witness is not None and (
            incoming_sequence is None or witness > incoming_sequence
        ):
            refused.append(path.name)
        elif exists and marker is None and witness is None:
            diagnostics.append(
                f"{path.name} has no readable collection marker; replacing pre-upgrade or corrupt state"
            )
    return refused, diagnostics


def write_collection_json(
    paths: Mapping[str, Path],
    path: Path,
    payload: Any,
    *,
    collection_start: int | None,
) -> None:
    """Publish a freshness witness, then atomically replace its document."""
    incumbent = _read_collection_witness(paths, path)
    if (
        isinstance(collection_start, bool)
        or not isinstance(collection_start, int)
        or collection_start <= 0
    ):
        if incumbent is not None:
            raise CollectionError(
                f"refused unknown collection replacing marked {path.name}"
            )
        raise CollectionError(f"collection start marker is unavailable for {path.name}")
    if incumbent is not None and incumbent > collection_start:
        raise CollectionError(
            f"refused collection {collection_start} replacing newer {path.name} marker {incumbent}"
        )
    content = _atomic_json_bytes(payload)
    ensure_private_directory(paths["collection_markers"])
    atomic_write_json(
        _collection_marker_path(paths, path),
        {
            "schema_version": 1,
            "collection_start": collection_start,
            "content_sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    atomic_write_json(path, payload)


def stamp_collection_document(
    paths: Mapping[str, Path],
    path: Path,
    *,
    collection_start: int,
) -> None:
    """Stamp exact existing bytes, including migration-preserved formatting."""
    content = _read_bounded_owner_file(
        path,
        label="published collection document",
        max_bytes=16 * 1024 * 1024,
        exact_mode=0o600,
    )
    assert content is not None
    ensure_private_directory(paths["collection_markers"])
    atomic_write_json(
        _collection_marker_path(paths, path),
        {
            "schema_version": 1,
            "collection_start": collection_start,
            "content_sha256": hashlib.sha256(content).hexdigest(),
        },
    )


def _read_state_json(path: Path, *, load_json_file: Callable[[Path], Any]) -> Any:
    try:
        return load_json_file(path)
    except FileNotFoundError:
        return None


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
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    _atomic_write_bytes(path, payload)


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
