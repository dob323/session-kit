"""The session snapshot, read once and turned into something the screen can
reason about.

The kit already produces this document (`shpool_status --json`); nothing here
collects state or asks the session manager anything. Two fields the row schema
is growing, `model` and `origin`, may be absent in a snapshot written by an
older release, so both have an answer when they are missing: the model renders
as an em dash, and a session with no recorded origin is one a person started.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Callable, Mapping, Sequence

from . import voice
from sessionkit_inventory import labels
from sessionkit_inventory.common import stall_threshold_seconds
from sessionkit_inventory.model import (
    classify_top_level_sessions,
    session_is_unavailable,
)

# A session the model has been quiet in for this long has stopped working,
# whatever the provider calls its own state. Mirrors the renderer's threshold
# so two surfaces can never disagree about the same session.
DEFAULT_STALL_SECONDS = labels.STALL_DEFAULT_SECONDS

# One vocabulary on the screen, whatever the collector called it -- and it is
# the SAME table the text picker and `sp list` read, not a second copy of it.
# This file used to keep its own, which is exactly how the two surfaces last
# drifted: the old notification-derived `idle` was retired in labels.py. The
# new transcript-aged state lives there too, so it cannot drift here again.
STATE_WORDS = labels.STATE_WORDS

PROVIDER_LABELS = {
    "claude": "CLD",
    "codex": "CDX",
    "shell": "SHL",
}

PROVIDER_NAMES = {
    "claude": "Claude",
    "codex": "Codex",
    "shell": "shell",
}

HUMAN = "human"
MACHINE = "machine"


def stall_seconds(environ: Mapping[str, str] | None = None) -> int:
    return stall_threshold_seconds(environ)


def _text(value: Any, limit: int = 200) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def format_age(seconds: int | None) -> str:
    """When a session last did something, in the words every surface uses.

    One phrase, one shape, on every row. It used to be `opened 3 hr ago` here
    and `3 hr ago` on a row that happened to know its last reply, so the
    column measured two different things and lined up as neither.
    """

    return labels.last_active(seconds)


@dataclass(frozen=True)
class Session:
    """One managed session, as the screen needs it.

    `key` is the identity the highlight is glued to. It is never printed: no
    screen prints a shpool ID or a conversation UUID.
    """

    key: str
    number: int | None
    title: str
    provider: str
    account_alias: str | None
    model: str | None
    origin: str
    availability: str
    agent_status: str
    needs_you: bool
    subagent_count: int
    age_seconds: int | None
    quiet_seconds: int | None
    cwd: str
    uuid: str | None
    shpool_id: str | None
    mutation_allowed: bool
    display_color: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider.casefold(), "UNK")

    @property
    def account_text(self) -> str:
        return self.account_alias or voice.MISSING

    @property
    def model_text(self) -> str:
        """Read through the SAME function the text picker uses.

        This used to guess from the provider alone and never looked at the
        reason the collector recorded, so a brand-new conversation that simply
        had not answered yet was reported as `unreadable` -- the kit claiming a
        broken transcript with nothing wrong, on one screen and not the other.
        """
        return labels.model_cell(self.raw)

    def last_active_text(self, now_ms: int) -> str:
        """The one time this row shows, from the same function as the picker.

        Formatting the snapshot's frozen age here while the picker recomputed
        from the recorded moment made the two screens disagree across a minute
        boundary on one snapshot.
        """
        return labels.row_last_active(self.raw, now_ms)

    @property
    def is_machine(self) -> bool:
        return self.origin == MACHINE

    @property
    def is_attached(self) -> bool:
        return self.availability.casefold() == "attached"

    @property
    def restorable(self) -> bool:
        return bool(self.uuid) and self.provider.casefold() in {"claude", "codex"}

    def state_word(self, *, seen: bool = False, stall: int | None = None) -> str:
        """The shared state word, with question and aged-idle precedence.

        The WHOLE row goes to `session_state`, not three fields copied out of
        it. Passing a synthetic dict meant `setup_incomplete` -- which that
        function tests first -- was a key it never received, so a session the
        text picker called `pending` was described here as whatever
        its status happened to map to, and a row with no status at all
        defaulted differently on each screen. A session seen in this window is
        the one thing this screen knows that the other does not.
        """

        threshold = stall if stall is not None else stall_threshold_seconds()
        row = dict(self.raw)
        if seen:
            row["needs_you"] = False
        return labels.session_state(row, stall_seconds=threshold)

    def haystack(self) -> str:
        """What the filter searches. Identifiers are deliberately absent: a
        person cannot see them, so they cannot type them."""

        project = os.path.basename(self.cwd.rstrip("/")) if self.cwd else ""
        parts = (
            self.title,
            self.provider_label,
            PROVIDER_NAMES.get(self.provider.casefold(), self.provider),
            self.account_alias or "",
            project,
            self.cwd,
            self.agent_status,
        )
        return " ".join(part for part in parts if part).casefold()


@dataclass(frozen=True)
class Inventory:
    """The snapshot as a whole, plus the two facts that gate actions."""

    sessions: tuple[Session, ...] = ()
    source: str = "live"
    stale: bool = False
    daemon: Mapping[str, Any] = field(default_factory=dict, repr=False)
    unavailable_total: int = 0

    @property
    def live(self) -> bool:
        return self.source == "live" and not self.stale

    def by_key(self, key: str) -> Session | None:
        for session in self.sessions:
            if session.key == key:
                return session
        return None

    def by_number(self, number: int) -> Session | None:
        for session in self.sessions:
            if session.number == number:
                return session
        return None


def _origin(row: Mapping[str, Any]) -> str:
    """A session with no recorded origin is one a person started.

    The stamp is written at creation. Until every launcher writes it, the safe
    reading of silence is the one that keeps a person's own session visible.
    """

    raw = _text(row.get("origin"), 20).casefold()
    return MACHINE if raw == MACHINE else HUMAN


def _model(row: Mapping[str, Any]) -> str | None:
    """The model's product name if the collector resolved one, else its id.

    `display_model` first: `model` is the raw identifier a machine reads, and
    a person reading `Opus 5` should not have to read `claude-opus-5`.
    """
    for name in ("display_model", "model", "model_id"):
        value = _text(row.get(name), 40)
        if value:
            return value
    return None


def parse_session(row: Mapping[str, Any]) -> Session | None:
    if not isinstance(row, Mapping):
        return None
    identity = row.get("identity") if isinstance(row.get("identity"), Mapping) else {}
    shpool_id = _text(row.get("shpool_id_raw") or row.get("shpool_id"), 96) or None
    uuid = _text(identity.get("uuid"), 64) or None
    number = _whole(row.get("terminal_number"))
    if number is not None and number <= 0:
        number = None
    key = shpool_id or uuid or f"row-{row.get('row')}"
    subagents = row.get("subagents")
    subagents = subagents if isinstance(subagents, Sequence) else ()
    count = _whole(row.get("active_subagent_count"))
    if count is None:
        count = len(subagents)
    return Session(
        key=key,
        number=number,
        title=_text(row.get("display_title") or row.get("title"), 120) or "Untitled",
        provider=_text(row.get("display_provider") or row.get("provider"), 20).casefold()
        or "shell",
        account_alias=_text(row.get("account_alias"), 20) or None,
        model=_model(row),
        origin=_origin(row),
        availability=_text(row.get("availability"), 20).casefold() or "ready",
        # No default of its own: `session_state` decides what an absent status
        # means, and it must decide it once for both screens.
        agent_status=_text(row.get("agent_status"), 60),
        needs_you=bool(row.get("needs_you")),
        subagent_count=max(0, int(count)),
        age_seconds=_whole(row.get("process_age_seconds")),
        quiet_seconds=_whole(row.get("recent_output_age_seconds")),
        cwd=_text(row.get("cwd"), 4096),
        uuid=uuid,
        shpool_id=shpool_id,
        mutation_allowed=row.get("mutation_allowed") is True,
        display_color=_text(row.get("display_color") or row.get("color"), 20).casefold(),
        raw=dict(row),
    )


def parse_inventory(document: Any) -> Inventory:
    """Read the snapshot document. A shape this cannot read is an empty
    estate, never a crash: the screen still draws, and says the list is
    empty."""

    if not isinstance(document, Mapping):
        return Inventory(sessions=(), source="cache", stale=True)
    human_rows, machine_rows, _ = classify_top_level_sessions(
        document.get("sessions", ()) or ()
    )
    unavailable_total = sum(
        session_is_unavailable(row) for row in (*human_rows, *machine_rows)
    )
    sessions = []
    for row in (*human_rows, *machine_rows):
        if session_is_unavailable(row):
            continue
        parsed = parse_session(row)
        if parsed is not None:
            sessions.append(parsed)
    daemon = document.get("daemon_generation")
    return Inventory(
        sessions=tuple(sessions),
        source=_text(document.get("source"), 20) or "cache",
        stale=bool(document.get("stale")),
        daemon=dict(daemon) if isinstance(daemon, Mapping) else {},
        unavailable_total=unavailable_total,
    )


@dataclass(frozen=True)
class ClosedSession:
    """A conversation that was closed on purpose and can come back.

    Written by the close path into a small ledger; absent until that ledger
    exists, which the screen states rather than hides.
    """

    key: str
    title: str
    provider: str
    uuid: str
    cwd: str
    account_alias: str | None
    closed_at_unix_ms: int | None
    # What the ledger itself says, when it says anything. A conversation whose
    # transcript this machine can no longer read has a provider and a uuid and
    # still cannot come back, so the shape of the record does not answer this.
    recorded_restorable: bool | None = None

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider.casefold(), "shell")

    @property
    def restorable(self) -> bool:
        if self.recorded_restorable is not None:
            return self.recorded_restorable
        return bool(self.uuid) and self.provider.casefold() in {"claude", "codex"}

    def haystack(self) -> str:
        project = os.path.basename(self.cwd.rstrip("/")) if self.cwd else ""
        parts = (self.title, self.provider_label, self.account_alias or "", project)
        return " ".join(part for part in parts if part).casefold()


def parse_closed(
    records: Any,
    *,
    still_readable: Callable[[str, str], bool] | None = None,
) -> tuple[ClosedSession, ...]:
    """Newest first, one row per conversation, the newest close winning.

    ``still_readable`` is the one question that decides whether a conversation
    can actually come back, and every surface has to ask it the same way. This
    screen never asked: it read the ledger straight off disk, so a
    conversation whose transcript is gone was listed with no mark and offered
    a Restore that cannot work.
    """

    if not isinstance(records, Sequence):
        return ()
    seen: dict[str, ClosedSession] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        uuid = _text(record.get("uuid"), 64)
        provider = _text(record.get("provider"), 20).casefold()
        if not provider:
            continue
        key = uuid or _text(record.get("shpool_id"), 96)
        if not key:
            continue
        closed_at = _whole(record.get("closed_at_unix_ms"))
        existing = seen.get(key)
        if existing is not None and (existing.closed_at_unix_ms or 0) >= (closed_at or 0):
            continue
        recorded = record.get("restorable")
        if isinstance(recorded, bool):
            pass
        elif provider == "shell" or not uuid:
            recorded = False
        elif still_readable is not None:
            recorded = bool(still_readable(provider, uuid))
        seen[key] = ClosedSession(
            key=key,
            title=_text(record.get("title"), 120) or "Untitled",
            provider=provider,
            uuid=uuid,
            cwd=_text(record.get("cwd"), 4096),
            account_alias=_text(record.get("account_alias"), 20) or None,
            closed_at_unix_ms=closed_at,
            recorded_restorable=bool(recorded) if isinstance(recorded, bool) else None,
        )
    return tuple(
        sorted(seen.values(), key=lambda item: -(item.closed_at_unix_ms or 0))
    )
