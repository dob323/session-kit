"""Every session somebody closed on purpose, kept so it can come back.

A close used to leave a tombstone that expired after seven days, and the
recovery feed only ever listed CRASHES — so a conversation closed deliberately
was unreachable from every surface the moment it ended, and unrecoverable for
good a week later. That is the opposite of the promise: closing a session ends
the terminal, never the conversation.

This is the ledger that makes the promise true. Every deliberate close appends
one small record — provider, conversation, name, directory, when, and who
asked for the session — under the durable data directory rather than the state
directory, because state is what a boot may throw away and this may not be.
There is no retention window: an entry is two hundred bytes and stays as
durable evidence after restore. The exact-live projection hides it while the
conversation is open and can reveal it again after an unobserved loss.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Mapping

from .common import CollectionError, clean_text, valid_uuid
from .state_io import StateLock

LEDGER_NAME = "closed-sessions.jsonl"
LEDGER_LOCK_NAME = "closed-sessions.lock"
MAX_LEDGER_BYTES = 4 * 1024 * 1024
# This name remains for compatibility with older fixture builders. Rewrites no
# longer use the old 4 MiB bound: they validate and copy one original row at a
# time, so file size no longer changes their memory safety.

# More than three million ordinary close rows fit under 512 MiB. At ten closes
# every day that is over eight centuries of history, well beyond a plausible
# lifetime for one machine's transcript store. The ceiling protects an
# unattended refresh from a pathological file, not normal history. A person
# can deliberately stream a larger valid file with the recovery override named
# in every refusal.
MAX_LIST_LEDGER_BYTES = 512 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
MAX_ROW_BYTES = 64 * 1024
MAX_ENTRIES: int | None = None
PROVIDERS = ("claude", "codex", "shell")
# What a close can say about who owned the conversation. The third value is
# the one that matters: a session nobody stamped has no origin, and this row
# is read back on every future restore, so a guess written here becomes
# permanent provenance for a conversation that never had any.
ORIGINS = ("human", "machine")
UNKNOWN_ORIGIN = "unknown"

_LARGE_LEDGER_RECOVERY = (
    "Run `sp recover --allow-large-ledger` to stream the complete ledger, "
    "then `sp restore --allow-large-ledger NUMBER` to restore a shown row."
)


@dataclass(frozen=True)
class _LedgerScan:
    size: int
    lines: int
    device: int | None
    inode: int | None


def data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Durable, not state: a closed conversation outlives a state reset."""
    env = environ if environ is not None else os.environ
    explicit = env.get("SESSION_KIT_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()
    base = env.get("XDG_DATA_HOME") or os.path.join(
        env.get("HOME") or str(Path.home()), ".local", "share"
    )
    return Path(base) / "session-kit"


def ledger_path(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    explicit = env.get("SESSION_KIT_CLOSED_LEDGER")
    return Path(explicit).expanduser() if explicit else data_dir(env) / LEDGER_NAME


def ledger_lock_path(environ: Mapping[str, str] | None = None) -> Path:
    return ledger_path(environ).with_name(LEDGER_LOCK_NAME)


def _prepare_directory(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    directory = ledger_path(environ).parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not env.get("SESSION_KIT_CLOSED_LEDGER"):
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    return directory


def _entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    provider = value.get("provider")
    if provider not in PROVIDERS:
        return None
    uuid = valid_uuid(value.get("uuid")) or ""
    if provider != "shell" and not uuid:
        return None
    closed_at = value.get("closed_at_unix_ms")
    if not isinstance(closed_at, int) or isinstance(closed_at, bool) or closed_at <= 0:
        return None
    origin = value.get("origin")
    return {
        "provider": provider,
        "uuid": uuid,
        "title": clean_text(value.get("title"), 120),
        # Where that title came from, so a later reader can tell a NAME from
        # a label the kit generated. Without it a recovery list has to either
        # show "Claude in v2 at Aug 14 23:44" as though somebody chose it, or
        # guess by shape. Absent on records written before this was kept.
        "title_source": clean_text(value.get("title_source"), 40),
        "cwd": clean_text(value.get("cwd"), 4096),
        "closed_at_unix_ms": closed_at,
        # Three values, not two. A session nobody stamped has no origin to
        # record, and writing "human" for it would be inventing at the moment
        # of writing exactly the durable provenance this ledger exists to
        # carry -- an absence would come back on every future restore as a
        # positive claim. "unknown" says what is true; every reader treats it
        # as the person's, which is what an unproven session is.
        "origin": origin if origin in ORIGINS else UNKNOWN_ORIGIN,
        "shpool_id": clean_text(value.get("shpool_id"), 64),
        "account_alias": clean_text(value.get("account_alias"), 12),
    }


def record_close(
    *,
    provider: str,
    uuid: str = "",
    title: str = "",
    title_source: str = "",
    cwd: str = "",
    origin: str = UNKNOWN_ORIGIN,
    shpool_id: str = "",
    account_alias: str = "",
    environ: Mapping[str, str] | None = None,
    now_unix_ms: int | None = None,
) -> dict[str, Any]:
    """Append one close only when the resulting ledger is completely listable."""
    entry = _entry(
        {
            "provider": provider,
            "uuid": uuid,
            "title": title,
            "title_source": title_source,
            "cwd": cwd,
            "closed_at_unix_ms": int(time.time() * 1000)
            if now_unix_ms is None
            else now_unix_ms,
            "origin": origin,
            "shpool_id": shpool_id,
            "account_alias": account_alias,
        }
    )
    if entry is None:
        raise CollectionError("a closed-session record needs a provider and conversation")
    directory = _prepare_directory(environ)
    path = ledger_path(environ)
    line = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    # One stable sidecar lock owns every ledger read, append and replacement.
    # O_APPEND keeps the append itself indivisible, while the lock also keeps
    # legal short-write loops from interleaving and keeps a restore rewrite
    # from replacing a close that landed after its read.
    with StateLock(directory, ledger_lock_path(environ)):
        before = _scan_records_unlocked(path, maximum=MAX_LIST_LEDGER_BYTES)
        _parse_record(line, before.lines + 1)
        after_size = before.size + len(line)
        if after_size > MAX_LIST_LEDGER_BYTES:
            raise CollectionError(
                f"closed-sessions ledger would grow from {before.size} to "
                f"{after_size} bytes; the complete-list safety limit (listing "
                f"ceiling) is {MAX_LIST_LEDGER_BYTES} bytes. No row or "
                f"tombstone was written. {_LARGE_LEDGER_RECOVERY}"
            )
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise CollectionError(
                    f"closed-sessions ledger must be a mode-0600 current-owner "
                    f"regular file: {path}"
                )
            if opened.st_size != before.size or (
                before.device is not None
                and (opened.st_dev, opened.st_ino) != (before.device, before.inode)
            ):
                raise CollectionError(
                    f"closed-sessions ledger changed before append: {path}"
                )
            try:
                _write_all(descriptor, line, "recording a closed session")
                os.fsync(descriptor)
                appended: bytes | None = None

                def prove_appended_row(
                    _entry: dict[str, Any] | None, raw: bytes, number: int
                ) -> None:
                    nonlocal appended
                    if number == before.lines + 1:
                        appended = raw

                # Prove the post-state through a duplicate of the descriptor
                # that received the append. Reopening the pathname here can
                # validate an atomically substituted file while the new row is
                # stranded on an unlinked inode.
                after = _scan_descriptor_unlocked(
                    descriptor,
                    path=path,
                    maximum=MAX_LIST_LEDGER_BYTES,
                    visit=prove_appended_row,
                )
                if (
                    after.size != after_size
                    or after.lines != before.lines + 1
                    or appended != line
                ):
                    raise CollectionError(
                        f"closed-sessions ledger changed after append: {path}"
                    )
                _fsync_directory(directory)
                _prove_descriptor_authoritative(
                    descriptor, path, action="appended"
                )
            except BaseException:
                # A short write, injected corruption, or post-write read error
                # must not leave a new row behind when no tombstone will be
                # allowed. The original validated prefix is still exact.
                os.ftruncate(descriptor, before.size)
                os.fsync(descriptor)
                raise
        finally:
            os.close(descriptor)
    return entry


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes, action: str) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(f"short write while {action}")
        view = view[written:]


def _parse_record(raw_line: bytes, number: int) -> dict[str, Any] | None:
    if len(raw_line) > MAX_ROW_BYTES:
        raise CollectionError(
            f"closed-sessions ledger row {number} exceeds the "
            f"{MAX_ROW_BYTES}-byte row safety limit; no partial list was returned"
        )
    if not raw_line.strip():
        return None
    try:
        value = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectionError(
            f"closed-sessions ledger has a malformed row near retained line {number}"
        ) from exc
    entry = _entry(value)
    if entry is None:
        raise CollectionError(
            f"closed-sessions ledger has an invalid row near retained line {number}"
        )
    return entry


def _scan_descriptor_unlocked(
    descriptor: int,
    *,
    path: Path,
    maximum: int | None,
    visit: Callable[[dict[str, Any] | None, bytes, int], None] | None = None,
) -> _LedgerScan:
    """Validate the inode behind an existing descriptor through dup + seek."""

    scan_descriptor = os.dup(descriptor)
    try:
        os.lseek(scan_descriptor, 0, os.SEEK_SET)
        opened = os.fstat(scan_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
        ):
            raise CollectionError(
                f"closed-sessions ledger must be a current-owner regular file: {path}"
            )
        size = opened.st_size
        if maximum is not None and size > maximum:
            raise CollectionError(
                f"closed-sessions ledger is {size} bytes; the complete-list "
                f"safety limit is {maximum} bytes, so no partial list was "
                f"returned. {_LARGE_LEDGER_RECOVERY}"
            )
        remaining = size
        buffered = bytearray()
        number = 0
        while remaining:
            chunk = os.read(scan_descriptor, min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                raise CollectionError(
                    f"closed-sessions ledger changed while it was being read: {path}"
                )
            remaining -= len(chunk)
            buffered.extend(chunk)
            while True:
                boundary = buffered.find(b"\n")
                if boundary < 0:
                    if len(buffered) > MAX_ROW_BYTES:
                        raise CollectionError(
                            f"closed-sessions ledger row {number + 1} exceeds the "
                            f"{MAX_ROW_BYTES}-byte row safety limit; no partial "
                            "list was returned"
                        )
                    break
                raw_line = bytes(buffered[: boundary + 1])
                del buffered[: boundary + 1]
                number += 1
                entry = _parse_record(raw_line, number)
                if visit is not None:
                    visit(entry, raw_line, number)
        if buffered:
            raise CollectionError(
                "closed-sessions ledger does not end at a complete row boundary"
            )
        finished = os.fstat(scan_descriptor)
        if (
            finished.st_size != opened.st_size
            or (finished.st_dev, finished.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise CollectionError(
                f"closed-sessions ledger changed while it was being read: {path}"
            )
    finally:
        os.close(scan_descriptor)
    return _LedgerScan(size, number, opened.st_dev, opened.st_ino)


def _prove_descriptor_authoritative(
    descriptor: int, path: Path, *, action: str
) -> None:
    """Require path to name the exact inode still held by descriptor."""

    written = os.fstat(descriptor)
    try:
        pathname = path.lstat()
    except OSError as exc:
        raise CollectionError(
            f"closed-sessions ledger path no longer names the file that was "
            f"{action}: {path}"
        ) from exc
    if (
        not stat.S_ISREG(pathname.st_mode)
        or (written.st_dev, written.st_ino) != (pathname.st_dev, pathname.st_ino)
    ):
        raise CollectionError(
            f"closed-sessions ledger path no longer names the file that was "
            f"{action}: {path}"
        )


def _scan_records_unlocked(
    path: Path,
    *,
    maximum: int | None,
    visit: Callable[[dict[str, Any] | None, bytes, int], None] | None = None,
) -> _LedgerScan:
    """Validate one authoritative complete ledger using one bounded row."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return _LedgerScan(0, 0, None, None)
    except OSError as exc:
        raise CollectionError(f"cannot open closed-sessions ledger: {path}") from exc
    try:
        try:
            _prove_descriptor_authoritative(descriptor, path, action="opened")
        except CollectionError as exc:
            raise CollectionError(
                f"closed-sessions ledger must be a current-owner regular file: {path}"
            ) from exc
        scanned = _scan_descriptor_unlocked(
            descriptor, path=path, maximum=maximum, visit=visit
        )
        try:
            _prove_descriptor_authoritative(descriptor, path, action="read")
        except CollectionError as exc:
            raise CollectionError(
                f"closed-sessions ledger changed while it was being read: {path}"
            ) from exc
        return scanned
    finally:
        os.close(descriptor)


@contextmanager
def closed_snapshot(
    *,
    environ: Mapping[str, str] | None = None,
    limit: int | None = MAX_ENTRIES,
    still_readable: Callable[[str, str], bool] | None = None,
    allow_large: bool = False,
) -> Iterator[Iterable[dict[str, Any]]]:
    """A complete, disk-backed snapshot, ordered newest first.

    Validation and de-duplication finish before the first result is exposed,
    so a malformed final row can never turn a valid prefix into partial truth.
    SQLite keeps keys and sort state on private temporary disk rather than in
    RAM; Python retains only one bounded ledger row and one result row.
    """

    directory = _prepare_directory(environ)
    path = ledger_path(environ)
    temporary_descriptor, temporary_name = tempfile.mkstemp(
        prefix="session-kit-closed-snapshot-", suffix=".sqlite3"
    )
    os.close(temporary_descriptor)
    os.chmod(temporary_name, 0o600)
    connection = sqlite3.connect(temporary_name)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA cache_size=-2048")
        connection.execute(
            """CREATE TABLE closed (
                provider TEXT NOT NULL,
                identity TEXT NOT NULL,
                uuid TEXT NOT NULL,
                title TEXT NOT NULL,
                cwd TEXT NOT NULL,
                closed_at INTEGER NOT NULL,
                origin TEXT NOT NULL,
                shpool_id TEXT NOT NULL,
                account_alias TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                PRIMARY KEY (provider, identity)
            ) WITHOUT ROWID"""
        )

        def retain(entry: dict[str, Any] | None, _raw: bytes, number: int) -> None:
            if entry is None:
                return
            identity = entry["uuid"] or entry["shpool_id"]
            connection.execute(
                """INSERT INTO closed VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, identity) DO UPDATE SET
                    uuid=excluded.uuid,
                    title=excluded.title,
                    cwd=excluded.cwd,
                    closed_at=excluded.closed_at,
                    origin=excluded.origin,
                    shpool_id=excluded.shpool_id,
                    account_alias=excluded.account_alias,
                    sequence=excluded.sequence
                WHERE excluded.closed_at > closed.closed_at""",
                (
                    entry["provider"],
                    identity,
                    entry["uuid"],
                    entry["title"],
                    entry["cwd"],
                    entry["closed_at_unix_ms"],
                    entry["origin"],
                    entry["shpool_id"],
                    entry["account_alias"],
                    number,
                ),
            )

        with StateLock(directory, ledger_lock_path(environ)):
            _scan_records_unlocked(
                path,
                maximum=None if allow_large else MAX_LIST_LEDGER_BYTES,
                visit=retain,
            )
        connection.commit()

        def rows() -> Iterator[dict[str, Any]]:
            yielded = 0
            cursor = connection.execute(
                """SELECT provider, uuid, title, cwd, closed_at, origin,
                          shpool_id, account_alias
                   FROM closed ORDER BY closed_at DESC, sequence ASC"""
            )
            for (
                provider,
                uuid,
                title,
                cwd,
                closed_at,
                origin,
                shpool_id,
                account_alias,
            ) in cursor:
                if limit is not None and yielded >= max(0, limit):
                    break
                if provider == "shell":
                    restorable = False
                else:
                    readable = (
                        True
                        if still_readable is None
                        else bool(still_readable(provider, uuid))
                    )
                    restorable = readable
                yielded += 1
                yield {
                    "provider": provider,
                    "uuid": uuid,
                    "title": title,
                    "cwd": cwd,
                    "closed_at_unix_ms": closed_at,
                    "origin": origin,
                    "shpool_id": shpool_id,
                    "account_alias": account_alias,
                    "restorable": restorable,
                }

        class SnapshotRows:
            """Reiterate the same validated SQLite snapshot without reopening it."""

            def __iter__(self) -> Iterator[dict[str, Any]]:
                return rows()

            def conversation_keys(self) -> Iterator[tuple[str, str]]:
                """Exact identities from this snapshot, without transcript probes."""

                for provider, uuid in connection.execute(
                    "SELECT provider, uuid FROM closed WHERE uuid != ''"
                ):
                    yield provider, uuid

        yield SnapshotRows()
    finally:
        connection.close()
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def ledger_is_readable(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the ledger could be read AND understood, apart from what it says.

    A ledger that is not there has nothing to say, and that IS a fact: no
    conversation has been closed on this machine yet. A ledger that will not
    open, or that opens and does not parse, also says nothing -- and that is
    not the same fact. Every reader here degrades both to "no rows", which is
    right for a list and wrong for a decision: the ledger is the only thing
    that says a conversation was the person's, so its silence is answered from
    somewhere else, and a caller that cannot tell an empty ledger from a
    damaged one lets one truncated write decide whose session this is.

    Opening it is not enough to answer this. Every line is parsed, and one
    line this module would skip is enough to make the whole reading unproven:
    a skipped line is a conversation whose origin was lost, and it may be the
    one being asked about.
    """
    path = ledger_path(environ)
    try:
        if path.is_symlink():
            return False
        if not path.is_file():
            return True
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > MAX_LEDGER_BYTES:
                # Same window `_read_lines` reads, so the same rows are judged:
                # the partial first line of a tail read is not a damaged row.
                handle.seek(size - MAX_LEDGER_BYTES)
                handle.readline()
            payload = handle.read(MAX_LEDGER_BYTES)
    except OSError:
        return False
    for line in payload.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = _entry(json.loads(line))
        except ValueError:
            return False
        if entry is None:
            return False
    return True


def load_closed(
    *,
    environ: Mapping[str, str] | None = None,
    limit: int | None = MAX_ENTRIES,
    still_readable: Callable[[str, str], bool] | None = None,
    allow_large: bool = False,
) -> list[dict[str, Any]]:
    """Newest first, one row per conversation, each saying if it can return.

    A conversation whose transcript this machine can no longer read cannot be
    restored, so it is listed as history only rather than dropped: the close
    happened, the person remembers the session, and a screen that accounts for
    every close but that one is the missing-session bug again. Offering it as
    a full restore would be the opposite lie. Shell sessions have no
    conversation at all: they are history only, and are never filtered by a
    transcript they never had.
    """
    with closed_snapshot(
        environ=environ,
        limit=limit,
        still_readable=still_readable,
        allow_large=allow_large,
    ) as rows:
        return list(rows)


def forget(
    provider: str,
    uuid: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Drop a conversation from the closed list once it is open again."""
    exact = valid_uuid(uuid) or ""
    if provider not in PROVIDERS or not exact:
        return 0
    path = ledger_path(environ)
    directory = _prepare_directory(environ)
    with StateLock(directory, ledger_lock_path(environ)):
        removed = 0

        def count_target(
            entry: dict[str, Any] | None, _raw: bytes, _number: int
        ) -> None:
            nonlocal removed
            if entry is not None and (
                entry["provider"] == provider and entry["uuid"] == exact
            ):
                removed += 1

        # Complete validation happens before a temporary file exists. There
        # is no file-size exception now that neither pass materializes bytes.
        source = _scan_records_unlocked(path, maximum=None, visit=count_target)
        if not removed:
            return 0
        descriptor, temporary = tempfile.mkstemp(
            prefix=".closed-sessions.", dir=directory
        )
        try:
            os.fchmod(descriptor, 0o600)
            copied_removed = 0
            expected_size = 0
            expected_digest = hashlib.sha256()

            def copy_unrelated(
                entry: dict[str, Any] | None, raw: bytes, _number: int
            ) -> None:
                nonlocal copied_removed, expected_size
                if entry is not None and (
                    entry["provider"] == provider and entry["uuid"] == exact
                ):
                    copied_removed += 1
                    return
                expected_size += len(raw)
                expected_digest.update(raw)
                if entry is None or not (
                    entry["provider"] == provider and entry["uuid"] == exact
                ):
                    _write_all(descriptor, raw, "rewriting closed sessions")

            # Validate again while copying each unrelated row's original
            # bytes. Any error leaves the original pathname untouched.
            copied_source = _scan_records_unlocked(
                path, maximum=None, visit=copy_unrelated
            )
            if (
                (copied_source.device, copied_source.inode)
                != (source.device, source.inode)
                or copied_removed != removed
            ):
                raise CollectionError(
                    f"closed-sessions ledger changed before rewrite: {path}"
                )
            os.fsync(descriptor)
            actual_digest = hashlib.sha256()

            def hash_rewrite(
                _entry: dict[str, Any] | None, raw: bytes, _number: int
            ) -> None:
                actual_digest.update(raw)

            rewritten = _scan_descriptor_unlocked(
                descriptor, path=path, maximum=None, visit=hash_rewrite
            )
            if (
                rewritten.size != expected_size
                or actual_digest.digest() != expected_digest.digest()
            ):
                raise CollectionError(
                    f"closed-sessions rewrite changed before publication: {path}"
                )
            os.replace(temporary, path)
            _fsync_directory(directory)
            _prove_descriptor_authoritative(
                descriptor, path, action="published"
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return removed


def entry_from_inventory(
    inventory: Mapping[str, Any] | None,
    *,
    provider: str,
    uuid: str = "",
    shpool_id: str = "",
) -> dict[str, str]:
    """Name, directory, and origin for a close, from the last known list.

    The picker and `sp close` know the session they are closing; the shell that
    closes itself knows only its own ID. Both get the same record by reading
    what the kit last saw, so no caller has to carry fields it does not have.
    """
    found: dict[str, str] = {
        "title": "",
        "title_source": "",
        "cwd": "",
        # Unproven stays UNKNOWN and therefore visible (ses87 ruling): the
        # ledger never asserts a creator it cannot prove.
        "origin": UNKNOWN_ORIGIN,
        "account_alias": "",
    }
    rows = inventory.get("sessions") if isinstance(inventory, Mapping) else None
    if not isinstance(rows, Iterable):
        return found
    exact = valid_uuid(uuid) or ""
    for row in rows:
        if not isinstance(row, Mapping) or row.get("provider") != provider:
            continue
        identity = row.get("identity")
        row_uuid = (
            valid_uuid(identity.get("uuid")) if isinstance(identity, Mapping) else None
        ) or ""
        if exact:
            if row_uuid != exact:
                continue
        elif not shpool_id or row.get("shpool_id_raw") != shpool_id:
            continue
        found["title"] = clean_text(row.get("title"), 120)
        found["title_source"] = clean_text(row.get("title_source"), 40)
        found["cwd"] = clean_text(row.get("cwd"), 4096)
        # `origin_recorded`, not `origin`: this row outlives the session by
        # design and is read back on every future restore, so only the stamp
        # belongs in it. `origin` on an unstamped session is what collection
        # inferred about who held a socket at one instant, and a close landing
        # during a provider restart would freeze that instant into a permanent
        # machine record for a conversation nobody ever stamped. No stamp means
        # no claim, and no claim reads as the person's.
        origin = row.get("origin_recorded")
        found["origin"] = origin if origin in ORIGINS else UNKNOWN_ORIGIN
        found["account_alias"] = clean_text(row.get("account_alias"), 12)
        break
    return found
