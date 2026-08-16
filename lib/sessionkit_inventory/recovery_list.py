"""One list of the conversations a person can bring back.

There were two, built from different stores, and they disagreed about almost
everything. `sp recover` read the crash manifest and the closed-session
ledger; the picker's Closed-sessions screen read the pending-recovery store.
Measured live on 2026-08-15, the two shared three conversations out of
fifty-one: forty-eight were offered only by the picker, six only by the CLI.
Thirty-four of the picker's seventy-seven entries were conversations that were
LIVE at that moment, offered for restore. The one a person had just closed on
purpose was in neither of the picker's -- the pending store drops those by
design, because a deliberate close is not "recovery work" to it.

So this module is the single projection both surfaces read. It takes every
store that knows about a session that is no longer open, and answers one
question in one order:

* **Never a live conversation.** A conversation open right now is not
  recoverable work; offering it invites a restore that collides with the
  session already running. Live is read from the last published inventory.
* **One number, the one it always had.** The kit already binds a number to a
  CONVERSATION (`ai:<provider>:<uuid>`) and keeps that binding after the
  session closes. Both lists used to number their own rows 1..N instead, so a
  session was 106 everywhere and 1 here -- and `sp restore 3` indexed a list
  that had been rebuilt since it was printed, which is a wrong restore, not
  just a confusing one.
* **A real name or "unnamed".** Titles are only shown when something actually
  named the conversation. A generated label ("Claude in v2 at Aug 14 23:44")
  is not a name, and "the Claude session" is not one either -- it is the
  absence of one, phrased as though it were.
* **History-only says so, in a sentence.** A plain shell has no conversation
  to reopen, and a conversation whose transcript this machine can no longer
  read cannot come back. Both are listed; neither pretends to be restorable.

Retention is decided by RESTORABILITY, not by age. A conversation stays on
this list for exactly as long as it could actually be brought back, which is
what the reader is being offered. An age cutoff would silently drop work that
still restores perfectly -- the same class of failure as the missing session
that prompted this. The newest-first cap bounds the history-only rows, which
exist to be read; it never drops a conversation that can come back, and it
never drops anything for being merely old.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from .common import clean_text, valid_uuid


# How many history-only rows the screen and the read will carry. Nothing that
# can still come back is ever dropped for it, and nothing at all is dropped for
# its age. Well above any real estate: 108 numbers had been issued on the
# machine this was written for, and its ledger held twenty closed
# conversations, of which the stores' own caps bound the rest.
MAX_ROWS = 500

# What the collector calls a title it MADE UP. `alias`, `native` and
# `automatic` are names something chose for this conversation; `context` is a
# directory and a timestamp, and `provider` is the bare word "Claude". A row
# whose title came from either of the last two has no name to show.
GENERATED_TITLE_SOURCES = frozenset({"context", "provider"})

# A record written before provenance was kept says nothing about its own
# title, so the shape has to answer instead -- and only for the labels the kit
# itself makes: "the Claude session" and "the shell session" from the human
# label, "Idle shell" and the bare provider word from the collector's last
# resort, "Claude in v2 at Aug 14 23:44" from the context title, and the
# generated session name ("v2-35", "v2-af"). Anything else recorded is treated
# as a name somebody chose, because it probably is.
GENERATED_TITLE_SHAPES = (
    re.compile(r"the(\s+\w+)?\s+session"),
    re.compile(r"idle shell"),
    re.compile(r"(claude|codex|shell)"),
    re.compile(r"(claude|codex|shell)\s+(in\s+\S+\s+at|started)\s+.+"),
    re.compile(r"[a-z0-9][a-z0-9._-]*-[0-9a-f]{2,4}"),
)

UNNAMED = "unnamed"

SHELL_HISTORY_REASON = (
    "a plain shell, so only its history is kept -- there is no conversation to reopen"
)
UNREADABLE_HISTORY_REASON = (
    "its transcript is no longer on this machine, so only its history is kept"
)
# Two launch records for one conversation that disagree. The kit will not
# choose between them for them, and saying "a plain shell" about a Claude
# conversation with a transcript is worse than saying nothing.
CONFLICT_HISTORY_REASON = (
    "its launch records disagree with each other, so it needs a look by hand"
)
# Actionable is false and nothing above explains it: the record is missing the
# keys a restore is made from.
INCOMPLETE_HISTORY_REASON = (
    "its launch record is incomplete, so only its history is kept"
)


def number_key(provider: Any, uuid: Any) -> str:
    """The registry key a conversation's number is bound to."""
    exact = valid_uuid(uuid) or ""
    if not isinstance(provider, str) or not provider or not exact:
        return ""
    return f"ai:{provider}:{exact}"


def _stable_number(numbers: Mapping[str, Any], provider: Any, uuid: Any) -> int | None:
    """The number this conversation already has, or None if it never had one."""
    key = number_key(provider, uuid)
    if not key:
        return None
    value = numbers.get(key) if isinstance(numbers, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _unambiguous_numbers(numbers: Mapping[str, Any]) -> dict[str, Any]:
    """The bindings, minus any number that two conversations both claim.

    This read does not go through the registry's validating reader (a listing
    verb takes no lock), so a half-written or stale file can hold one number
    twice. A number that names two conversations names neither: both rows drop
    to no number rather than one of them silently standing for the other.
    """
    if not isinstance(numbers, Mapping):
        return {}
    counts: dict[Any, int] = {}
    for key, value in numbers.items():
        if isinstance(key, str) and key.startswith("ai:"):
            counts[value] = counts.get(value, 0) + 1
    return {
        key: value
        for key, value in numbers.items()
        if counts.get(value, 0) <= 1
    }


def _looks_generated(title: str) -> bool:
    """True when the kit, not a person, made this label up."""
    folded = " ".join(title.casefold().split())
    return any(shape.fullmatch(folded) for shape in GENERATED_TITLE_SHAPES)


def _live_keys(live_sessions: Iterable[Any]) -> set[tuple[str, str]]:
    """Every conversation visible in the last published inventory."""
    keys: set[tuple[str, str]] = set()
    for row in live_sessions or ():
        if not isinstance(row, Mapping):
            continue
        identity = row.get("identity")
        raw = identity.get("uuid") if isinstance(identity, Mapping) else None
        exact = valid_uuid(raw) or ""
        provider = row.get("provider")
        if exact and isinstance(provider, str):
            keys.add((provider, exact.casefold()))
    return keys


def _real_name(
    *,
    provider: Any,
    uuid: Any,
    recorded_title: Any,
    title_source: Any,
    aliases: Mapping[str, Any],
    automatic_titles: Mapping[str, Any],
) -> str:
    """The name something actually gave this conversation, or "".

    The stores that hold a chosen name outlive the session, so they are asked
    first and settle it. Only when neither has one does the recorded title
    speak -- and then only if whatever wrote it said where it came from and
    that source was a name. A record written before provenance was kept is
    judged by shape against the labels the kit generates, and by nothing else:
    never invent a name, never discard one somebody chose.
    """
    # A shell has no conversation, so nothing ever named it. What its record
    # holds is the name of whatever process happened to be running -- "python3",
    # or "Idle shell" when none was -- and neither is a name a person chose.
    if provider == "shell":
        return ""
    exact = valid_uuid(uuid) or ""
    if isinstance(provider, str) and provider and exact:
        key = f"{provider}:{exact}"
        for store in (aliases, automatic_titles):
            if not isinstance(store, Mapping):
                continue
            found = clean_text(store.get(key), 120)
            if found:
                return found
    recorded = clean_text(recorded_title, 120)
    if not recorded:
        return ""
    source = clean_text(title_source, 40).casefold()
    if source:
        return "" if source in GENERATED_TITLE_SOURCES else recorded
    return "" if _looks_generated(recorded) else recorded


def _candidate(
    *,
    source: str,
    provider: Any,
    uuid: Any,
    cwd: Any,
    when: Any,
    recorded_title: Any,
    title_source: Any,
    restorable: bool,
    history_reason: str,
    generation_key: Any = "",
    old_shpool_id: Any = "",
    conflict_fields: Any = (),
    aliases: Mapping[str, Any],
    automatic_titles: Mapping[str, Any],
    numbers: Mapping[str, Any],
) -> dict[str, Any]:
    exact = valid_uuid(uuid) or ""
    name = _real_name(
        provider=provider,
        uuid=exact,
        recorded_title=recorded_title,
        title_source=title_source,
        aliases=aliases,
        automatic_titles=automatic_titles,
    )
    when_ms = (
        when
        if isinstance(when, int) and not isinstance(when, bool) and when > 0
        else 0
    )
    return {
        "source": source,
        "number": _stable_number(numbers, provider, exact),
        # A number is bound per boot, so a conversation closed before the last
        # one has none to be consistent with. It stays listed and stays
        # reachable: `sp restore` takes this short id as well as a number.
        "short_id": exact[:8],
        "provider": provider if isinstance(provider, str) else "",
        "uuid": exact,
        "cwd": clean_text(cwd, 4096),
        "name": name,
        "named": bool(name),
        "display_name": name or UNNAMED,
        "restorable": bool(restorable and exact),
        "history_only_reason": "" if (restorable and exact) else history_reason,
        "when_unix_ms": when_ms,
        "source_generation_key": clean_text(generation_key, 200),
        "old_shpool_id": clean_text(old_shpool_id, 200),
        # The picker refuses to act on a record whose evidence disagrees with
        # itself. That guard is older than this list and outlives it.
        "conflict_fields": [
            clean_text(field, 80)
            for field in (conflict_fields or ())
            if clean_text(field, 80)
        ],
    }


def recovery_rows(
    *,
    manifest_sessions: Iterable[Any] = (),
    closed_rows: Iterable[Any] = (),
    pending_entries: Iterable[Any] = (),
    live_sessions: Iterable[Any] = (),
    aliases: Mapping[str, Any] | None = None,
    automatic_titles: Mapping[str, Any] | None = None,
    numbers: Mapping[str, Any] | None = None,
    still_readable: Callable[[Any, str], bool] | None = None,
    limit: int = MAX_ROWS,
) -> list[dict[str, Any]]:
    """The one ordered list both recovery surfaces show.

    Newest first. One row per conversation, whichever store knew about it;
    when more than one does, the newest event wins and the pending record
    still contributes the keys its acknowledgment needs.

    ``still_readable`` answers the one question that decides whether a
    conversation can come back at all, and every store is asked it the same
    way: the ledger, the crash manifest and the pending queue used to disagree
    about a transcript that is gone -- one dropped the row, the others offered
    a restore that walks into a missing file.
    """
    aliases = aliases if isinstance(aliases, Mapping) else {}
    automatic_titles = (
        automatic_titles if isinstance(automatic_titles, Mapping) else {}
    )
    numbers = _unambiguous_numbers(numbers if isinstance(numbers, Mapping) else {})
    live = _live_keys(live_sessions)
    readable_cache: dict[tuple[Any, str], bool] = {}

    def readable(provider: Any, exact: str) -> bool:
        # Nothing to read is not the same as unreadable: a shell never had a
        # conversation, and a row with no uuid has nothing to look for.
        if still_readable is None or provider == "shell" or not exact:
            return True
        key = (provider, exact)
        if key not in readable_cache:
            readable_cache[key] = bool(still_readable(provider, exact))
        return readable_cache[key]

    candidates: list[dict[str, Any]] = []

    # A crash took these: the session is gone and nobody chose that.
    for row in manifest_sessions or ():
        if not isinstance(row, Mapping):
            continue
        provider = row.get("provider")
        exact = valid_uuid(row.get("uuid")) or ""
        intact = readable(provider, exact)
        candidates.append(
            _candidate(
                source="lost",
                provider=provider,
                uuid=exact,
                cwd=row.get("cwd"),
                when=row.get("crashed_at_unix_ms"),
                recorded_title=row.get("title"),
                title_source=row.get("title_source"),
                restorable=bool(exact) and intact,
                history_reason=(
                    SHELL_HISTORY_REASON
                    if provider == "shell" or not exact
                    else UNREADABLE_HISTORY_REASON
                ),
                aliases=aliases,
                automatic_titles=automatic_titles,
                numbers=numbers,
            )
        )

    # Closed on purpose. Ending a terminal never ends the conversation, so
    # these belong here exactly like the lost ones -- hiding them was the
    # broken half of the promise, and the pending store hides them all.
    for row in closed_rows or ():
        if not isinstance(row, Mapping):
            continue
        provider = row.get("provider")
        exact = valid_uuid(row.get("uuid")) or ""
        restorable = (
            bool(row.get("restorable"))
            and provider != "shell"
            and readable(provider, exact)
        )
        candidates.append(
            _candidate(
                source="closed",
                provider=provider,
                uuid=exact,
                cwd=row.get("cwd"),
                when=row.get("closed_at_unix_ms"),
                recorded_title=row.get("title"),
                title_source=row.get("title_source"),
                restorable=restorable,
                history_reason=(
                    SHELL_HISTORY_REASON
                    if provider == "shell"
                    else UNREADABLE_HISTORY_REASON
                ),
                # The ledger's own identity for a session that has no
                # conversation. Without it a closed shell whose directory was
                # never recorded is dropped by the merge below -- listed by
                # neither surface, which is the bug this list exists to end.
                old_shpool_id=row.get("shpool_id"),
                aliases=aliases,
                automatic_titles=automatic_titles,
                numbers=numbers,
            )
        )

    # A daemon generation ended under these. They carry the only keys the
    # acknowledgment can be made with, so they are merged in rather than
    # chosen between.
    for row in pending_entries or ():
        if not isinstance(row, Mapping):
            continue
        provider = row.get("provider")
        exact = valid_uuid(row.get("uuid")) or ""
        conflicts = row.get("conflict_fields") or ()
        intact = readable(provider, exact)
        candidates.append(
            _candidate(
                source="lost",
                provider=provider,
                uuid=exact,
                cwd=row.get("cwd"),
                when=row.get("started_at_unix_ms"),
                recorded_title=row.get("title"),
                title_source=row.get("title_source"),
                restorable=bool(row.get("actionable")) and intact,
                # Why it cannot come back, in the words that are true of THIS
                # record. Every non-actionable pending row used to be called a
                # plain shell, including a Claude conversation whose launch
                # records disagree -- which is both false and the one case
                # that needs them to look.
                history_reason=(
                    SHELL_HISTORY_REASON
                    if provider == "shell" or not exact
                    else UNREADABLE_HISTORY_REASON
                    if not intact
                    else CONFLICT_HISTORY_REASON
                    if conflicts
                    else INCOMPLETE_HISTORY_REASON
                ),
                generation_key=row.get("source_generation_key"),
                old_shpool_id=row.get("old_shpool_id"),
                conflict_fields=row.get("conflict_fields") or (),
                aliases=aliases,
                automatic_titles=automatic_titles,
                numbers=numbers,
            )
        )

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for row in candidates:
        if row["uuid"] and (row["provider"], row["uuid"].casefold()) in live:
            # Open right now. Not recoverable work, and never offered as such.
            continue
        if not row["uuid"]:
            # A shell has no conversation to merge on; it is its own row, and
            # only its own record can identify it.
            if not row["old_shpool_id"] and not row["cwd"]:
                continue
            ordered.append(row)
            continue
        key = (row["provider"], row["uuid"].casefold())
        held = merged.get(key)
        if held is None:
            merged[key] = row
            ordered.append(row)
            continue
        # The newest event describes the session best, but the acknowledgment
        # keys exist on exactly one record and must survive the merge.
        if row["when_unix_ms"] > held["when_unix_ms"]:
            for field in ("source", "when_unix_ms", "cwd", "restorable",
                          "history_only_reason"):
                held[field] = row[field]
            if row["named"]:
                held["name"] = row["name"]
                held["named"] = True
                held["display_name"] = row["display_name"]
        elif row["named"] and not held["named"]:
            held["name"] = row["name"]
            held["named"] = True
            held["display_name"] = row["display_name"]
        if row["source_generation_key"] and not held["source_generation_key"]:
            held["source_generation_key"] = row["source_generation_key"]
            held["old_shpool_id"] = row["old_shpool_id"]
        if row["conflict_fields"] and not held["conflict_fields"]:
            held["conflict_fields"] = row["conflict_fields"]

    # A short id names one conversation for a machine reader; no screen prints
    # it. Eight hex characters separate any real estate, and when two rows do
    # share a prefix both grow until they actually differ -- growing once to
    # twelve left two conversations sharing "aaaaaaaaaaaa" and calling it
    # settled.
    width = 8
    while width < 32:
        by_short: dict[str, list[dict[str, Any]]] = {}
        for row in ordered:
            if row["short_id"]:
                by_short.setdefault(row["short_id"], []).append(row)
        sharing = [rows for rows in by_short.values() if len(rows) > 1]
        if not sharing:
            break
        width += 4
        for rows in sharing:
            for row in rows:
                row["short_id"] = row["uuid"].replace("-", "")[:width]

    ordered.sort(
        key=lambda item: (
            -item["when_unix_ms"],
            item["number"] if isinstance(item["number"], int) else 1 << 30,
            item["uuid"],
        )
    )
    bound = limit if isinstance(limit, int) and not isinstance(limit, bool) else MAX_ROWS
    bound = max(0, bound)
    if len(ordered) > bound:
        # The cap bounds the reading, and reading is all a history-only row is
        # for. A conversation that can still come back is the work itself, so
        # the cap never takes one: an aggregate limit applied over the merged
        # list made the module's own promise false at its edge, and the row it
        # dropped was reachable from neither surface.
        restorable = [row for row in ordered if row["restorable"]]
        history = [row for row in ordered if not row["restorable"]]
        kept = {id(row) for row in restorable}
        kept.update(id(row) for row in history[: max(0, bound - len(restorable))])
        ordered = [row for row in ordered if id(row) in kept]

    _assign_selectors(ordered)
    return ordered


def _assign_selectors(rows: Sequence[dict[str, Any]]) -> None:
    """What a person types to act on a row -- the same string on both screens.

    A number if the conversation still has one. Otherwise the name beside it,
    which is what the screen shows and the only thing on that row a person
    could type. A name is not always unique, though: everything nobody named
    reads "unnamed", so two of those would answer to one word -- one surface
    restoring the first and the other refusing. When a name is taken, the row
    takes the time of its own event instead, which is fixed for as long as the
    row exists and cannot be a row position.

    Taken means taken by ANY row on this list, not by another row with the
    same name. A person may call a conversation "2", and session 2 may exist:
    those were settled in separate passes, so both rows answered to "2" and
    both surfaces sent it to the numbered one -- a word that names two rows,
    which is the whole thing this list exists to stop. A word belongs to one
    row on the whole list or it belongs to none of them.

    A row that cannot come back has no selector: its sentence is already under
    it on the list, and there is nothing to act on. Its number still selects
    it, because the screen shows one.
    """
    taken: set[str] = set()
    waiting: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row["number"], int):
            # The number is the identity the rest of the kit already uses for
            # this conversation, so it is never given up to a name.
            row["selector"] = str(row["number"])
            taken.add(row["selector"].casefold())
            continue
        row["selector"] = ""
        if row["restorable"]:
            waiting.append(row)

    def offer(candidates: Sequence[tuple[str, dict[str, Any]]]) -> None:
        """Hand out the words no other row is asking for or already holds."""
        claims: dict[str, int] = {}
        for token, _ in candidates:
            claims[token.casefold()] = claims.get(token.casefold(), 0) + 1
        for token, row in candidates:
            folded = token.casefold()
            if not token or folded in taken or claims[folded] != 1:
                continue
            row["selector"] = token
            taken.add(folded)

    offer([(row["display_name"], row) for row in waiting])
    for pattern in ("%H:%M", "%H:%M:%S"):
        waiting = [row for row in waiting if not row["selector"]]
        if not waiting:
            break
        offer([(_event_stamp(row, pattern), row) for row in waiting])
    # Whatever is still empty had nothing left to tell it apart -- same name,
    # same second. Those rows keep no selector, so both surfaces refuse rather
    # than one of them guessing.


def _event_stamp(row: Mapping[str, Any], pattern: str) -> str:
    when = row.get("when_unix_ms")
    if not isinstance(when, int) or isinstance(when, bool) or when <= 0:
        return ""
    try:
        moment = datetime.datetime.fromtimestamp(when / 1000)
    except (OSError, OverflowError, ValueError):
        return ""
    return f"@{moment.strftime(pattern)}"
