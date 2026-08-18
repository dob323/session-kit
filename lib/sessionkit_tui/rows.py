"""What the screen shows, in the order it shows it.

One list. Sessions that need you, then the ones openable here, then the ones
open in another window; one counted row for machine sessions; then the
ordinary rows — New session, Projects, Closed sessions, Help. Marking,
filtering, and the attention rules all live here, where they can be tested
without a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable, Sequence

from . import voice
from sessionkit_inventory import labels
from .model import ClosedSession, Session
from sessionkit_inventory.model import canonical_session_order_key

SESSION = "session"
MACHINE_COUNT = "machine-count"
NEW_SESSION = "new-session"
PROJECTS = "projects"
CLOSED = "closed"
HELP = "help"
CLOSED_SESSION = "closed-session"

# How long the list holds still after the highlight moves. Re-sorting under a
# moving cursor moves the row out from under the person reading it.
SORT_HOLD_MS = 3000

_RANGE = re.compile(r"^(\d+)-(\d+)$")


def is_filter(text: str) -> bool:
    """Digits, commas, and dashes mark. One letter and the whole input is a
    filter — there is no half-and-half state to explain."""

    return any(character.isalpha() for character in text)


def parse_marks(text: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """`1,4,7-9` becomes the numbers it names.

    Returns the numbers in the order they were named and the fragments that
    named nothing, so the screen can say which one it could not use.
    """

    numbers: list[int] = []
    unusable: list[str] = []
    for fragment in text.replace(" ", "").split(","):
        if not fragment:
            continue
        match = _RANGE.match(fragment)
        if match:
            first, last = int(match.group(1)), int(match.group(2))
            step = 1 if last >= first else -1
            for number in range(first, last + step, step):
                if number not in numbers:
                    numbers.append(number)
            continue
        if fragment.isdigit():
            number = int(fragment)
            if number not in numbers:
                numbers.append(number)
            continue
        unusable.append(fragment)
    return tuple(numbers), tuple(unusable)


def matches(needle: str, haystack: str) -> bool:
    """Loose, in-order matching: `lsa` finds `Ledger Sync Audit`."""

    needle = needle.casefold().replace(" ", "")
    if not needle:
        return True
    position = 0
    for character in needle:
        position = haystack.find(character, position)
        if position < 0:
            return False
        position += 1
    return True


def is_seen(session: Session, seen: Iterable[str]) -> bool:
    return session.key in set(seen)


# The words that mean a person is being waited on. `idle` is one of them: it
# is a needs-you session whose transcript has stopped moving, not a state of
# its own.
ATTENTION_STATES = frozenset({labels.QUESTION, labels.WAITING_ON_YOU, labels.IDLE})


def needs_you(session: Session, seen: Iterable[str]) -> bool:
    """A session a person is looking at is seen, never counted as waiting.

    Asked of the state word this row displays, not of the raw `needs_you` and
    `blocking_question` flags underneath it. Those answer a narrower question:
    a provider turn that ended normally reports `agent_status: idle` with
    `needs_you: false`, so the row read `needs you` on screen while this
    predicate said no, and the count beside it disagreed with the rows it was
    counting.
    """

    if is_seen(session, seen):
        return False
    return session.state_word() in ATTENTION_STATES


def _natural_key(session: Session) -> tuple:
    return canonical_session_order_key(session.raw)


def order_sessions(
    sessions: Sequence[Session],
    *,
    seen: Iterable[str] = (),
    pinned: Sequence[str] = (),
) -> tuple[Session, ...]:
    """The list order, or the order it was already in.

    While `pinned` holds, a session keeps the place it had when the highlight
    last moved; anything new lands after it in natural order.
    """

    # ``seen`` changes the attention wording in this window, never the order.
    # Otherwise glancing at one row would make the TUI disagree with the shell
    # picker even though both read the same snapshot.
    natural = sorted(sessions, key=_natural_key)
    if not pinned:
        return tuple(natural)
    places = {key: index for index, key in enumerate(pinned)}
    limit = len(places)
    return tuple(
        sorted(
            natural,
            key=lambda item: (
                places.get(item.key, limit),
                natural.index(item),
            ),
        )
    )


@dataclass(frozen=True)
class Row:
    """One line a person can move to. Every row on the screen is one of
    these, so the highlight, the mouse, and Enter share one meaning."""

    key: str
    kind: str
    label: str
    detail: str = ""
    number: int | None = None
    needs_you: bool = False
    marked: bool = False
    provider: str = ""
    account: str = ""
    model: str = ""
    state: str = ""
    subagents: str = ""
    age: str = ""
    compact_age: str = ""
    extras: tuple[str, ...] = ()
    session: Session | None = field(default=None, repr=False)
    closed: ClosedSession | None = field(default=None, repr=False)

    @property
    def markable(self) -> bool:
        return self.number is not None


def build_rows(
    sessions: Sequence[Session],
    *,
    query: str = "",
    marks: Iterable[int] = (),
    seen: Iterable[str] = (),
    pinned: Sequence[str] = (),
    machine_expanded: bool = False,
    stall: int | None = None,
    age_text=None,
    now_ms: int | None = None,
) -> tuple[Row, ...]:
    """The whole list, top to bottom."""

    seen_set = frozenset(seen)
    marked = set(marks)
    needle = query.casefold().strip()

    def keep(session: Session) -> bool:
        return matches(needle, session.haystack()) if needle else True

    people = [s for s in sessions if not s.is_machine and keep(s)]
    machines = [s for s in sessions if s.is_machine and keep(s)]

    def row_for(session: Session) -> Row:
        seen_here = session.key in seen_set
        state = session.state_word(seen=seen_here, stall=stall)
        # One time on every row, saying the same thing in the same words: when
        # this session last did something. A row that needed the person used to
        # fold its wait into the state cell and show no time at all, so no two
        # rows on the screen were measuring the same quantity.
        #
        # With a clock, the row is read through the SAME function the text
        # picker uses, from the recorded moment; the two screens disagreed
        # across a minute boundary while this one formatted a frozen age.
        if now_ms is not None:
            age = session.last_active_text(now_ms)
            compact_age = labels.row_last_active_compact(session.raw, now_ms)
        elif age_text is not None:
            age = age_text(session.quiet_seconds)
            compact_age = labels.brief_age(session.quiet_seconds) if session.quiet_seconds is not None else "unknown"
        else:
            age = ""
            compact_age = ""
        subagents = (
            voice.plural(session.subagent_count, "subagent")
            if session.subagent_count
            else ""
        )
        detail_parts = [
            session.provider_label,
            session.account_text,
            session.model_text,
            state,
            subagents,
            age,
        ]
        return Row(
            key=session.key,
            kind=SESSION,
            label=session.title,
            detail=" | ".join(part for part in detail_parts if part),
            number=session.number,
            needs_you=needs_you(session, seen_set),
            marked=session.number in marked,
            provider=session.provider_label,
            account=session.account_text,
            model=session.model_text,
            state=state,
            subagents=subagents,
            age=age,
            compact_age=compact_age,
            session=session,
        )

    result = [row_for(session) for session in order_sessions(people, seen=seen_set, pinned=pinned)]

    if machines:
        needing = sum(1 for session in machines if needs_you(session, seen_set))
        result.append(
            Row(
                key=MACHINE_COUNT,
                kind=MACHINE_COUNT,
                label=voice.machine_row(len(machines), needing),
            )
        )
        if machine_expanded:
            result.extend(
                row_for(session)
                for session in order_sessions(machines, seen=seen_set, pinned=pinned)
            )

    for key, label in (
        (NEW_SESSION, voice.NEW_SESSION),
        (PROJECTS, voice.PROJECTS),
        (CLOSED, voice.CLOSED_SESSIONS),
        (HELP, voice.HELP),
    ):
        if needle and not matches(needle, label.casefold()):
            continue
        result.append(Row(key=key, kind=key, label=label))
    return tuple(result)


def build_closed_rows(
    closed: Sequence[ClosedSession],
    *,
    query: str = "",
    age_text=None,
) -> tuple[Row, ...]:
    needle = query.casefold().strip()
    result = []
    for index, item in enumerate(closed, start=1):
        if needle and not matches(needle, item.haystack()):
            continue
        state = "" if item.restorable else "history only"
        age = "login time unknown"
        result.append(
            Row(
                key=item.key,
                kind=CLOSED_SESSION,
                label=item.title,
                detail=item.provider_label,
                number=index,
                provider=item.provider_label,
                account=item.account_alias or voice.MISSING,
                state=state,
                age=age,
                closed=item,
            )
        )
    return tuple(result)


def attention_count(rows: Sequence[Row]) -> int:
    """Every count on the screen is drillable: this one counts exactly the
    rows a person can move to and open."""

    return sum(1 for row in rows if row.needs_you)
