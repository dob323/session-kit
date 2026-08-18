"""Who asked for a session: a person, or a machine.

The picker's default view is the person's own screen. Everything an
automation, a drill, or a headless restore creates belongs behind one counted
row; it is work nobody chose to look at, and a list that mixes the two makes
a person's own sessions harder to find every time a machine starts one.

Origin is stamped at CREATION, by the verb that creates the session, because
that is the only moment anybody knows who asked. Nothing infers it later from
a directory, a title, or a guess: an unstamped session is a person's, so a
tool that forgets to say it is a machine shows up in the human list and gets
noticed, rather than hiding by accident.

TWO SIGNALS, AND WHICH ONE WINS. An instance-bound stamp is provenance -- who
created this session instance -- and it is the answer whenever it matches, in both directions. A
second signal exists for sessions created before any of this was recorded:
collection can PROVE that a program and no window holds a Codex App Server's
socket (see `collector`). That verdict is consulted ONLY where there is no
stamp at all. It never overturns one, because a stamp that says a person
created a session is a fact about its creation, and a window that happens to
be absent right now is not. The order is: a machine stamp folds, a human
stamp stays, and only silence is decided by the driver.

SILENCE MUST BE PROVEN, TOO. "This session was never stamped" is a claim about
the store, and it is only true when the store was READ. A store that will not
open, will not parse, or does not have the shape this version writes is not
evidence of anything, so it never licenses the driver to fold a row and it is
never rewritten -- rewriting it is how a reading failure turns into permanent
data loss.

Legacy stamps predate instance binding. A reused session-manager name must not
inherit one: those stamps remain readable for rollout, but classify no current
row. This deliberately leaves an old machine-created session visible as
clutter instead of risking that a person's later session disappears.

The record outlives nothing: it is keyed by session ID and forgotten the
moment its session is gone. "Gone" also has to be proven: an older collection
can have read the manager just before a newly attached session was visible,
and a listing that names no sessions at all is not proof that every stamped
session died. Both keep their stamps.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .common import CollectionError
from .state_io import (
    StateLock,
    atomic_write_private_json,
    ensure_private_directory,
    read_private_json,
)

ORIGIN_SCHEMA_VERSION = 2
MAX_ORIGIN_BYTES = 256 * 1024
HUMAN = "human"
MACHINE = "machine"
ORIGINS = (HUMAN, MACHINE)
_ORIGIN_GENERATION_RE = re.compile(r"[0-9a-f]{64}")
_LEGACY_ORIGIN_GENERATION = "legacy"
_ORIGIN_INSTANCE_ENV = {
    "shell_pid": "SESSION_KIT_ORIGIN_SHELL_PID",
    "shell_start_ticks": "SESSION_KIT_ORIGIN_SHELL_START_TICKS",
    "started_at_unix_ms": "SESSION_KIT_ORIGIN_STARTED_AT_UNIX_MS",
}
# Record generations are freshness nonces for cooperating writers, not
# authentication tags. They prevent an older captured record from reaching a
# replacement; a same-account process that can rewrite private state can also
# delete that state directly and is outside this boundary.
# Creation and collection use different locks. A collector can read the
# manager just before attach, then prune after creation captures and stamps the
# exact new shell. An id-not-listed test alone would delete that fresh stamp,
# and nothing ever stamps a live session again. Two minutes is far past this
# attach/capture race and costs one dictionary entry.
ORIGIN_PRUNE_GRACE_MS = 120 * 1000
# The suffix a bounce marker carries once the session shell has taken the
# instruction and is relaunching. The picker writes the bare session ID and
# never a receipt; collection deletes only a generated receipt captured before
# its process reading and never the bare ID. Both forms read as "bouncing".
TAKEN_SUFFIX = ".taken"
# New provider shells put the request generation after the receipt suffix.
# The whole name is immutable: a later bounce gets a different name, so a
# collector carrying an older sighting has no pathname by which it could
# remove the later receipt.  The fixed ``.taken`` name remains readable for
# shells that were already live when this protocol shipped, but collection
# never settles that reusable legacy name.
TAKEN_GENERATION_SEPARATOR = f"{TAKEN_SUFFIX}."
# A request reserves its generated receipt name for the lifetime of the
# session. Keeping the reservation after settlement makes generated names
# non-reusable while any collector for that session can still be in flight.
BOUNCE_GENERATION_RESERVATION_PREFIX = ".generation."
# A listing with NO sessions in it is the one shape that can empty the whole
# store in a single pass, and it is also what a collection run against a
# stand-in `shpool` produces -- the store is shared, the session manager is
# not. It forgets nothing at all. An age cut here reads as "old enough that no
# live session could still be using it", and that is not a fact about the
# session, only about the stamp: the operator's own sessions run for days, and
# one empty listing plus a day of uptime deleted their provenance and handed
# them to the driver. Nothing grows without bound as a result -- the first
# listing that names any session prunes whatever really died, and while the
# listing is empty nothing new is being stamped either.


def origins_path(state_dir: Path) -> Path:
    return state_dir / "session-origins.json"


def checked_origin(value: Any) -> str:
    if value not in ORIGINS:
        raise CollectionError(f"session origin must be one of {', '.join(ORIGINS)}")
    return str(value)


def _empty_document() -> dict[str, Any]:
    return {"schema_version": ORIGIN_SCHEMA_VERSION, "sessions": {}}


def _origin_lock(state_dir: Path) -> StateLock:
    ensure_private_directory(state_dir)
    return StateLock(state_dir, state_dir / "session-origins.lock")


def _validated_pair(document: Any) -> tuple[dict[str, Any], bool]:
    """The store, and whether the document it came from was fully understood.

    The second value is the difference between "nobody has been stamped" and
    "this file did not read as a stamp store". Only the first is evidence.
    """
    if (
        not isinstance(document, Mapping)
        or document.get("schema_version") not in {1, ORIGIN_SCHEMA_VERSION}
        or not isinstance(document.get("sessions"), Mapping)
    ):
        return _empty_document(), False
    sessions: dict[str, Any] = {}
    recognized = True
    schema_version = document["schema_version"]
    for key, entry in document["sessions"].items():
        if (
            not isinstance(key, str)
            or not isinstance(entry, Mapping)
            or entry.get("origin") not in ORIGINS
        ):
            # One unreadable row is one session whose provenance was lost, so
            # the whole reading stops being proof that anybody is unstamped.
            recognized = False
            continue
        stamp = entry.get("at_unix_ms")
        generation = entry.get("record_generation")
        if schema_version == ORIGIN_SCHEMA_VERSION and (
            not isinstance(generation, str)
            or (
                generation != _LEGACY_ORIGIN_GENERATION
                and not _ORIGIN_GENERATION_RE.fullmatch(generation)
            )
        ):
            recognized = False
            continue
        normalized = {
            "origin": entry["origin"],
            "at_unix_ms": stamp
            if isinstance(stamp, int) and not isinstance(stamp, bool)
            else 0,
        }
        normalized["record_generation"] = (
            generation
            if schema_version == ORIGIN_SCHEMA_VERSION
            else _LEGACY_ORIGIN_GENERATION
        )
        instance = entry.get("instance")
        if isinstance(instance, Mapping):
            pid = instance.get("shell_pid")
            ticks = instance.get("shell_start_ticks")
            started = instance.get("started_at_unix_ms")
            if (
                isinstance(pid, int)
                and not isinstance(pid, bool)
                and pid > 0
                and isinstance(ticks, int)
                and not isinstance(ticks, bool)
                and ticks > 0
                and isinstance(started, int)
                and not isinstance(started, bool)
                and started >= 0
            ):
                normalized["instance"] = {
                    "shell_pid": pid,
                    "shell_start_ticks": ticks,
                    "started_at_unix_ms": started,
                }
            else:
                recognized = False
        sessions[key] = normalized
    return {"schema_version": ORIGIN_SCHEMA_VERSION, "sessions": sessions}, recognized


def _validated(document: Any) -> dict[str, Any]:
    return _validated_pair(document)[0]


def read_origins(state_dir: Path) -> tuple[dict[str, Any], bool]:
    """Load the store and say whether the reading can be trusted.

    A store that has never been written reads as an empty TRUSTED store: no
    session has been stamped yet, which is a fact. Anything else that goes
    wrong -- permissions, a truncated file, a size cap, JSON that will not
    parse, a shape from another version -- reads as an empty UNTRUSTED store,
    and callers must neither infer from it nor write over it.
    """
    try:
        raw = read_private_json(
            origins_path(state_dir),
            max_bytes=MAX_ORIGIN_BYTES,
            allow_missing=True,
        )
    except (CollectionError, OSError):
        return _empty_document(), False
    if raw is None:
        return _empty_document(), True
    return _validated_pair(raw)


def load_origins(state_dir: Path) -> dict[str, Any]:
    return _validated(
        read_private_json(
            origins_path(state_dir),
            max_bytes=MAX_ORIGIN_BYTES,
            allow_missing=True,
        )
    )


def record_origin(
    state_dir: Path,
    *,
    shpool_id: str,
    origin: str,
    now_unix_ms: int | None = None,
) -> dict[str, Any]:
    """Stamp one proven session instance at the moment it is created."""
    if not isinstance(shpool_id, str) or not shpool_id:
        raise CollectionError("session origin requires a session ID")
    checked = checked_origin(origin)
    instance: dict[str, int] = {}
    for field, variable in _ORIGIN_INSTANCE_ENV.items():
        raw = os.environ.get(variable)
        try:
            value = int(raw) if raw is not None else 0
        except ValueError:
            value = 0
        if value <= 0:
            raise CollectionError(
                "session origin requires the exact shell PID, shell start ticks, "
                "and session start time"
            )
        instance[field] = value
    with _origin_lock(state_dir):
        document, trusted = read_origins(state_dir)
        if not trusted:
            # Writing here would replace every stamp this reading could not see
            # with one entry, and the sessions behind those stamps are live. A
            # refusal costs one unstamped session, which is listed as a person's;
            # the write would cost every other session its provenance.
            raise CollectionError(
                "session origin store did not read as a stamp store; refusing to "
                "overwrite it"
            )
        document["schema_version"] = ORIGIN_SCHEMA_VERSION
        document["sessions"][shpool_id] = {
            "origin": checked,
            "at_unix_ms": int(time.time() * 1000)
            if now_unix_ms is None
            else now_unix_ms,
            "record_generation": secrets.token_hex(32),
            "instance": instance,
        }
        atomic_write_private_json(origins_path(state_dir), document)
        return document


def origin_for(shpool_id: Any, origins: Mapping[str, Any] | None) -> str:
    """An unstamped session belongs to the person. Never the other way."""
    if not isinstance(shpool_id, str) or not isinstance(origins, Mapping):
        return HUMAN
    entry = (origins.get("sessions") or {}).get(shpool_id)
    if not isinstance(entry, Mapping):
        return HUMAN
    origin = entry.get("origin")
    return origin if isinstance(origin, str) and origin in ORIGINS else HUMAN


def recorded_origin(shpool_id: Any, origins: Mapping[str, Any] | None) -> str | None:
    """The stamp itself, or None when this session was never stamped.

    `origin_for` answers what a session IS, and reads an unstamped session as
    a person's. This answers the different question of whether anybody ever
    said, which is what separates "a person created this" from "this was
    created before the kit recorded creators at all".
    """
    if not isinstance(shpool_id, str) or not isinstance(origins, Mapping):
        return None
    entry = (origins.get("sessions") or {}).get(shpool_id)
    if not isinstance(entry, Mapping):
        return None
    return entry.get("origin") if entry.get("origin") in ORIGINS else None


def _recorded_origin_for_row(
    row: Mapping[str, Any], origins: Mapping[str, Any] | None
) -> str | None:
    shpool_id = row.get("shpool_id_raw")
    if not isinstance(shpool_id, str) or not isinstance(origins, Mapping):
        return None
    entry = (origins.get("sessions") or {}).get(shpool_id)
    if not isinstance(entry, Mapping):
        return None
    # A name is reusable. Schema-1 stamps and early schema-2 stamps have no
    # evidence tying them to this instance, so applying either would turn the
    # old creator into the new one. They remain readable so their generation
    # can still be retired by the ordinary pruner.
    instance = entry.get("instance")
    if not isinstance(instance, Mapping):
        return None
    shell = row.get("shpool_shell")
    if not isinstance(shell, Mapping) or (
        shell.get("pid") != instance.get("shell_pid")
        or shell.get("process_start_ticks") != instance.get("shell_start_ticks")
        or row.get("started_at_unix_ms") != instance.get("started_at_unix_ms")
    ):
        return None
    return recorded_origin(shpool_id, origins)


def _provider_is_bouncing(state_dir: Path, shpool_id: Any) -> bool:
    """Whether the kit itself asked this session's provider to restart.

    A bounce relaunches the same conversation to repaint its title or theme,
    so the window is briefly gone from a session a PERSON is sitting in. That
    absence is the kit's own doing and says nothing about who drives the
    session, so the driver verdict is not read while it lasts.
    """
    if not isinstance(shpool_id, str) or not shpool_id or "/" in shpool_id:
        return False
    directory = state_dir / "provider-bounce"
    # The request name, the reusable receipt written by already-live legacy
    # shells, and every generation-specific receipt written by new shells all
    # mean the same thing here: the kit removed this session's window itself.
    markers = [directory / shpool_id, directory / f"{shpool_id}{TAKEN_SUFFIX}"]
    try:
        markers.extend(
            marker
            for marker in directory.iterdir()
            if _bounce_session_id(marker.name, generated_only=True) == shpool_id
        )
    except FileNotFoundError:
        # No bounce directory is the ordinary pre-bounce state, not a failed
        # reading of state that might contain a protective receipt.
        pass
    except OSError:
        return True
    for marker in markers:
        try:
            if marker.is_file() and not marker.is_symlink():
                return True
        except OSError:
            # State that cannot be read must not be what clears a session out
            # of the list: treat it as bouncing, which suppresses the inference
            # and leaves the row visible.
            return True
    return False


def apply_session_origins(
    inventory: dict[str, Any],
    *,
    state_dir: Path,
    clear_settled_bounces: bool = True,
    settle_bounce_receipts: Iterable[str] = (),
) -> None:
    """Stamp every row with who asked for it, before anything filters.

    Two fields come out of this. `origin` is what the row IS, and every
    consumer that shows or filters rows reads it. `origin_recorded` is the
    stamp and nothing but the stamp -- absent when this session was never
    stamped -- and it is what anything DURABLE must read: a repair that
    re-declares an origin, a close that writes the conversation's origin into
    the ledger. Without that separation a verdict about who holds a socket
    right now becomes a permanent fact about who created the session, and no
    later refresh can take it back.
    """
    # Before anything is read: a bounce whose replacement window is visible in
    # THIS collection is over, and its marker must stop suppressing the
    # verdict. This is the only place that can say so -- the shell that
    # relaunches a provider blocks on it and cannot report its own success --
    # so the marker outlives the relaunch instead of being dropped inside it,
    # and a positive sighting is what ends it.
    # ...except on a reading that promised to change nothing. `--no-write` is
    # used for deployment validation and by every `sp` guard snapshot, and it
    # states that it will not so much as create a state directory; deleting a
    # file there is worse than that, and it does it without the lock the write
    # path holds. Skipping it costs nothing: the marker is cleared by the next
    # collection that is allowed to write, and until then the row is visible.
    if clear_settled_bounces:
        clear_settled_bounce_markers(
            inventory,
            state_dir=state_dir,
            settle_bounce_receipts=settle_bounce_receipts,
        )
    try:
        origins, trusted = read_origins(state_dir)
    except (CollectionError, OSError):  # pragma: no cover - read_origins catches
        origins, trusted = _empty_document(), False
    for row in inventory.get("sessions", ()):
        if not isinstance(row, dict):
            continue
        shpool_id = row.get("shpool_id_raw")
        stamped = _recorded_origin_for_row(row, origins) if trusted else None
        row.pop("origin_recorded", None)
        if stamped is not None:
            # Provenance, in both directions. A session a person created stays
            # a person's even on a refresh where no window happens to hold it.
            row["origin"] = stamped
            row["origin_recorded"] = stamped
        elif (
            trusted
            and row.get("machine_driven") is True
            and not _provider_is_bouncing(state_dir, shpool_id)
        ):
            # Never stamped -- created before creators were recorded. The one
            # proven reading available decides it. `trusted` is part of the
            # condition because "never stamped" is a claim about the store: if
            # the store did not read, nobody knows whether this session was
            # stamped, and an unknown provenance is a person's.
            row["origin"] = MACHINE
        else:
            row["origin"] = HUMAN


def capture_origin_generations(state_dir: Path) -> frozenset[tuple[str, str]]:
    """Exact origin-entry generations present before process collection."""
    if not origins_path(state_dir).exists():
        return frozenset()
    try:
        with _origin_lock(state_dir):
            document, trusted = read_origins(state_dir)
            if not trusted:
                return frozenset()
            return frozenset(
                (key, generation)
                for key, entry in document["sessions"].items()
                if isinstance(entry, Mapping)
                and isinstance(generation := entry.get("record_generation"), str)
                and bool(_ORIGIN_GENERATION_RE.fullmatch(generation))
            )
    except (CollectionError, OSError):
        return frozenset()


def prune_origins(
    state_dir: Path,
    active_session_ids: Iterable[str],
    *,
    now_unix_ms: int | None = None,
    retire_generations: Iterable[tuple[str, str]] = (),
    retire_bounce_generations: Iterable[tuple[str, str]] = (),
) -> int:
    """Forget the stamp once its session is PROVEN gone.

    Absence from one listing is not that proof in two shapes, and a stamp
    deleted here is never rewritten -- nothing re-stamps a live session -- so
    each wrong deletion costs that session its provenance for the rest of its
    life and hands it to the driver.

    A collection can have read just before a new session attached, so a stamp
    younger than the grace can still be absent from that older reading. And a
    listing that names no sessions at all forgets nothing: it can empty
    the store in a single pass, it is what a collection run against a stand-in
    session manager produces, and no amount of elapsed time turns it into
    proof that a live session died.
    """
    active = set(active_session_ids)
    now = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    # Same question, same key, same evidence: state about a session that no
    # longer exists. Kept here so every caller of the one prune pass sweeps
    # the bounce markers too, and a bounce interrupted by a kill cannot leave
    # classification suppressed for an id nothing will ever consume again.
    prune_bounce_markers(
        state_dir,
        active,
        now_unix_ms=now,
        retire_generations=retire_bounce_generations,
    )
    allowed = frozenset(retire_generations)
    if not active or not allowed or not origins_path(state_dir).exists():
        return 0
    with _origin_lock(state_dir):
        try:
            document, trusted = read_origins(state_dir)
        except (CollectionError, OSError):  # pragma: no cover - reader catches
            return 0
        if not trusted:
            # A store that did not read is not a store full of dead sessions.
            return 0
        floor = now - ORIGIN_PRUNE_GRACE_MS
        keep = {
            key: entry
            for key, entry in document["sessions"].items()
            if key in active
            or _stamped_after(entry, floor)
            or (key, entry.get("record_generation")) not in allowed
        }
        removed = len(document["sessions"]) - len(keep)
        if removed:
            document["sessions"] = keep
            atomic_write_private_json(origins_path(state_dir), document)
        return removed


def capture_bounce_receipts(state_dir: Path) -> frozenset[str]:
    """Exact generated receipt names that predate the next process reading.

    Receipt names are generations.  Capturing them before collection binds a
    later positive window sighting to the only receipts that sighting is old
    enough to settle.  A receipt created while collection is running has a new
    name and is absent from this set.

    The reusable legacy ``<id>.taken`` name is deliberately excluded.  Shells
    that were already running when generated receipts shipped still create
    it, and no deployed code can make that pathname immune to ABA replacement.
    Leaving it in place errs visible until that shell's ordinary final cleanup
    or the session-gone sweep removes it.
    """
    directory = state_dir / "provider-bounce"
    captured: set[str] = set()
    try:
        entries = list(directory.iterdir())
    except OSError:
        return frozenset()
    for marker in entries:
        if _bounce_session_id(marker.name, generated_only=True) is None:
            continue
        try:
            if marker.is_file() and not marker.is_symlink():
                captured.add(marker.name)
        except OSError:
            continue
    return frozenset(captured)


def _bounce_cleanup_generation(
    marker: Path,
    *,
    original_name: str | None = None,
) -> tuple[str, str] | None:
    name = marker.name if original_name is None else original_name
    session_id = _bounce_session_id(name, generated_only=True)
    if session_id is not None or name.startswith(BOUNCE_GENERATION_RESERVATION_PREFIX):
        return (name, name)
    if _bounce_session_id(name) != name:
        # Reusable legacy receipts and temporary files have no safe generation.
        return None
    try:
        lines = marker.read_text(encoding="utf-8", errors="strict").splitlines()
    except OSError:
        return None
    if len(lines) != 3 or not lines[2].isalnum():
        return None
    return (name, lines[2])


def capture_bounce_cleanup_generations(
    state_dir: Path,
) -> frozenset[tuple[str, str]]:
    """Exact marker generations eligible for a later session-gone sweep."""
    directory = state_dir / "provider-bounce"
    try:
        entries = list(directory.iterdir())
    except OSError:
        return frozenset()
    captured: set[tuple[str, str]] = set()
    for marker in entries:
        try:
            if marker.is_symlink() or not marker.is_file():
                continue
            generation = _bounce_cleanup_generation(marker)
            if generation is not None:
                captured.add(generation)
        except OSError:
            continue
    return frozenset(captured)


def _retire_bounce_generation(
    marker: Path,
    allowed: frozenset[tuple[str, str]],
) -> bool:
    """Move, verify, and either retire or restore one exact marker inode."""
    original_name = marker.name
    temporary = marker.parent / f".retiring.{secrets.token_hex(16)}"
    try:
        marker.replace(temporary)
        actual = _bounce_cleanup_generation(
            temporary,
            original_name=original_name,
        )
        if actual in allowed:
            temporary.unlink()
            return True
        try:
            marker.hardlink_to(temporary)
        except FileExistsError:
            # A still-newer marker already owns the live name.
            pass
        temporary.unlink()
    except OSError:
        try:
            if temporary.exists() and not marker.exists():
                marker.hardlink_to(temporary)
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return False


def clear_settled_bounce_markers(
    inventory: Mapping[str, Any],
    *,
    state_dir: Path,
    settle_bounce_receipts: Iterable[str] = (),
) -> int:
    """Drop the marker for every session whose window is visible again.

    A bounce ends when the replacement window exists, and nothing inside the
    relaunch can observe that: the session shell is blocked on the provider it
    just started. So the shell hands the question over by renaming the request
    to a generated receipt, which leaves the session reading as bouncing for
    as long as the answer is unknown. The answer is given here, by the one
    reader that can see the window.

    Only a positive sighting clears it, and only for a bounce that has already
    HAPPENED -- which is why this deletes the RENAMED marker and never the
    original. A marker still under its first name is an instruction nobody has
    carried out yet, and the window in sight beside it is the old one: the
    picker writes the instruction, re-proves the session on a fresh collection,
    and only then asks the window to exit, so the whole interval between the
    request and the exit is a live window sitting next to a live instruction.
    Deleting it there does not end a bounce, it CANCELS one -- the shell reaches
    the marker it was told to read, finds nothing, takes the ordinary provider
    exit instead of relaunching, and the session the person was sitting in
    closes.

    A generated receipt name rather than one reusable second path binds this
    unlink to the exact receipt captured before the process reading. Telling
    generations apart by content, timestamp, or inode would still put a check
    and an unlink around a reusable path, allowing a replacement between them.

    A bounce whose replacement never starts keeps its renamed marker, keeps its
    row visible, and is forgotten only when the session itself is gone.
    """
    allowed = frozenset(settle_bounce_receipts)
    if not allowed:
        return 0
    cleared = 0
    for row in inventory.get("sessions", ()):
        if not isinstance(row, Mapping) or row.get("app_server_window") is not True:
            continue
        shpool_id = row.get("shpool_id_raw")
        if not isinstance(shpool_id, str) or not shpool_id or "/" in shpool_id:
            continue
        prefix = f"{shpool_id}{TAKEN_GENERATION_SEPARATOR}"
        for name in allowed:
            # An exact generated name, captured before the sighting.  The
            # provider never reuses it, so unlinking this path cannot reach a
            # receipt installed by a later bounce.
            if (
                not name.startswith(prefix)
                or _bounce_session_id(name, generated_only=True) != shpool_id
            ):
                continue
            marker = state_dir / "provider-bounce" / name
            try:
                if marker.is_symlink() or not marker.is_file():
                    continue
                marker.unlink()
            except OSError:
                continue
            cleared += 1
    return cleared


def _bounce_session_id(name: str, *, generated_only: bool = False) -> str | None:
    """Session id carried by a request or either receipt-name generation."""
    if not isinstance(name, str) or not name or "/" in name:
        return None
    if name.startswith(BOUNCE_GENERATION_RESERVATION_PREFIX):
        if generated_only:
            return None
        reserved = name[len(BOUNCE_GENERATION_RESERVATION_PREFIX) :]
        if TAKEN_GENERATION_SEPARATOR not in reserved:
            return None
        session_id, generation = reserved.split(TAKEN_GENERATION_SEPARATOR, 1)
        if session_id and generation and generation.isalnum():
            return session_id
        return None
    if TAKEN_GENERATION_SEPARATOR in name:
        session_id, generation = name.split(TAKEN_GENERATION_SEPARATOR, 1)
        if session_id and generation and generation.isalnum():
            return session_id
        return None
    if generated_only:
        return None
    if name.endswith(TAKEN_SUFFIX):
        session_id = name[: -len(TAKEN_SUFFIX)]
        return session_id or None
    # Other dot-prefixed mktemp files are not requests and must age out as
    # their own names, never be attributed to a live session accidentally.
    return None if name.startswith(".") else name


def prune_bounce_markers(
    state_dir: Path,
    active_session_ids: Iterable[str],
    *,
    now_unix_ms: int | None = None,
    retire_generations: Iterable[tuple[str, str]] = (),
) -> int:
    """Forget a bounce marker whose session no longer exists.

    The session shell removes its own marker as it relaunches, so a marker
    outliving its session means the shell never got there -- it was killed, or
    the box went down mid-bounce. That marker can never be consumed again and
    would sit in the state directory forever.

    Only markers for sessions the listing did not name are touched, only when
    the listing named some session (an empty listing proves nothing), and only
    after the grace, so a bounce in flight is never disarmed under a live
    session -- disarming one is what puts a person's window in the machine
    list while it restarts.
    """
    active = set(active_session_ids)
    allowed = frozenset(retire_generations)
    if not active or not allowed:
        return 0
    directory = state_dir / "provider-bounce"
    now = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    floor = (now - ORIGIN_PRUNE_GRACE_MS) / 1000
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for marker in entries:
        # A marker the shell has taken carries a second name, and it belongs to
        # the same session -- swept as that session's, or a bounce in flight
        # under a live session gets disarmed the moment it passes the grace.
        name = _bounce_session_id(marker.name)
        if name is None:
            name = marker.name
        if name in active or marker.is_symlink():
            continue
        try:
            if not marker.is_file() or marker.stat().st_mtime > floor:
                continue
            if not _retire_bounce_generation(marker, allowed):
                continue
        except OSError:
            continue
        removed += 1
    return removed


def _stamped_after(entry: Any, floor_unix_ms: int) -> bool:
    if not isinstance(entry, Mapping):
        return False
    stamp = entry.get("at_unix_ms")
    if isinstance(stamp, bool) or not isinstance(stamp, int):
        return False
    return stamp > floor_unix_ms
