"""When a live conversation may be moved to another account on its own.

Session Kit could already move an exact conversation between two enrolled
accounts, but only because a human sat in the picker and chose it. Nothing
stopped one long conversation from spending an account down to nothing, and
nothing stopped the next choice from spending the one after it. This module is
the rule that closes that: a conversation whose own account has run dry may be
moved once, only into an account that still holds a stated reserve, and only
while facts about both accounts are fresh enough to believe.

Everything here is a decision, never an action. :func:`plan` reads facts and
returns a verdict with a sentence a human can read; the caller performs the
move with the same machinery the manual switch uses, or performs nothing. The
split matters because every refusal in this file has to be provable from a
file on disk, and a function that also kills processes cannot be tested that
way.

Four facts, and only these four, decide a move:

* the kill switch (``account-switching-off``) is absent;
* the usage feed is fresh -- a stale roster is exactly how a machine drains an
  account it believed was full, so unproven usage refuses rather than guesses;
* the conversation's own account is spent, by the weekly number the feed
  publishes for that account;
* some other account is both selectable and still above the reserve.

Usage is published per ACCOUNT, never per conversation. This module therefore
never estimates what one conversation spent, and no rule here depends on such
a number. The hop limit and the reserve are exact for precisely that reason:
both are account-level facts, counted and compared, never apportioned.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence
import uuid as uuidlib

from . import accounts as _accounts
from .common import CollectionError, clean_text, valid_uuid
from .state_io import StateLock, atomic_write_private_json, read_private_json


GUARD_SCHEMA_VERSION = 1

# One hop. A conversation that has already been carried off a spent account
# stops on the second wall and reports; it never walks to a third account.
MAX_AUTOMATIC_HOPS = 1

# Percent of an account's weekly window that automatic switching may never
# consume. A move into an account already past this line is refused even when
# it is the only candidate left.
DEFAULT_RESERVE_PERCENT = 25

# Percent of the weekly window at which the conversation's own account counts
# as run dry. 100 means "the published weekly number says there is nothing
# left"; lowering it moves conversations earlier, which spends the single hop
# earlier and is therefore not the default.
DEFAULT_EXHAUSTED_PERCENT = 100

# The statuses the manual switch already accepts as safe to move, and the
# quiet period it already requires. Mirrored from
# lib/sh/sp_picker.sh:account_switch_stable_snapshot; tests pin the two
# copies together so the automatic path can never be laxer than the manual one.
MOVABLE_AGENT_STATUS = frozenset({"idle", "needs your reply", "reply optional"})
MIN_QUIET_SECONDS = 5

# The ledger is the only thing enforcing "one move". Every bound on it is
# therefore fail-closed: reading more than MAX_LEDGER_THREADS keys raises
# rather than returning a slice, because a silent slice makes every
# conversation past the cut look like it has never moved. Counted rows are
# never removed by age; reaching the cap stops automation instead of granting
# an old conversation another move.
MAX_HOP_LEDGER_BYTES = 4 * 1024 * 1024
MAX_LEDGER_THREADS = 4096
MAX_LEDGER_HOPS = 8


def hop_ledger_path(config: Mapping[str, Any]) -> Path:
    return Path(str(config["state_dir"])) / "account-auto-hops.json"


def _hop_ledger_lock(config: Mapping[str, Any]) -> StateLock:
    # Its own lock, not the account registry's: this ledger is written after a
    # switch commits, and borrowing the registry lock would put a second
    # waiter behind whatever long account operation happens to hold it.
    root = Path(str(config["state_dir"]))
    return StateLock(root, root / "account-auto-hops.lock")


def _percent_from_environment(
    name: str, default: int, environ: Mapping[str, str] | None = None
) -> int:
    source = os.environ if environ is None else environ
    raw = source.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        # An unreadable setting is not permission to use a laxer one. Falling
        # back to the shipped default keeps a typo from silently removing the
        # reserve, which is the one number here that protects money.
        return default
    if not 0 <= value <= 100:
        return default
    return value


def reserve_percent(environ: Mapping[str, str] | None = None) -> int:
    return _percent_from_environment(
        "SESSION_KIT_ACCOUNT_RESERVE_PERCENT", DEFAULT_RESERVE_PERCENT, environ
    )


def exhausted_percent(environ: Mapping[str, str] | None = None) -> int:
    return _percent_from_environment(
        "SESSION_KIT_ACCOUNT_EXHAUSTED_PERCENT", DEFAULT_EXHAUSTED_PERCENT, environ
    )


def automatic_switching_off(config: Mapping[str, Any]) -> bool:
    """True when the shipped kill switch disables every account change.

    The same sentinel the manual switch honours, deliberately: a second file
    would mean an operator who switched account changes off could still be
    surprised by an automatic one.
    """
    return _accounts.kill_switch_path(config).exists()


# A published window number is a fraction: 0.55 means 55% used. A little over
# 1.0 is real (overage), but a value far above it is not this scale at all --
# a feed that switched to whole percents would publish 55, which read as a
# fraction is 5500% used and would make the reserve meaningless in the
# permissive direction. Anything past this is treated as unreadable, so both
# the reserve and the exhaustion test fail closed instead of silently
# misjudging every account.
MAX_CREDIBLE_USAGE_FRACTION = 2.0


def _usage_state(value: Any) -> tuple[str, float | None]:
    """Classify one published usage number.

    Three outcomes, and keeping them apart is load-bearing:

    * ``("absent", None)``  -- the feed published nothing. Codex publishes no
      five-hour number at all, so "absent" has to stay usable or automatic
      switching could never touch a Codex conversation.
    * ``("unreadable", None)`` -- the feed published something that is not a
      window fraction. This must never read as "no objection". Collapsing it
      into "absent" is how a target whose five-hour window was published as
      ``100`` sailed past the wall check and got the conversation.
    * ``("ok", x)`` -- a number to compare.
    """
    if value is None:
        return "absent", None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unreadable", None
    number = float(value)
    if number != number or number < 0 or number > MAX_CREDIBLE_USAGE_FRACTION:
        return "unreadable", None
    return "ok", number


def _usage_fraction(value: Any) -> float | None:
    """The number when there is one to trust, else None.

    Callers that must tell "absent" from "unreadable" use :func:`_usage_state`
    directly. This stays for the places where both answers mean the same
    thing: no proof, so no move.
    """
    return _usage_state(value)[1]


def implausible_feed_reason(rows: Sequence[Any]) -> str:
    """Why the whole published feed cannot be believed, or "" when it can.

    A single number cannot reveal a units change. If the feed ever published
    whole percents instead of fractions, ``55`` is obviously wrong but ``0``,
    ``1`` and ``2`` are not -- they would read as 0%, 100% and 200% used, and
    the reserve would quietly stop meaning anything for those accounts.

    Across a roster the change is visible, because some account will publish a
    value no fraction can take. So one unreadable number condemns the whole
    feed rather than one row: judging the other accounts on the same feed
    means trusting the same units that just proved themselves wrong.
    """
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for key in ("u5h", "u7d"):
            if _usage_state(row.get(key))[0] == "unreadable":
                return (
                    "the usage feed published a %s figure for %s that is not a "
                    "window fraction"
                    % (key, clean_text(row.get("alias"), 12) or "an account")
                )
    return ""


def _thread_key(provider: str, uuid: str) -> str:
    exact = valid_uuid(uuid)
    if provider not in _accounts.PROVIDERS or not exact:
        raise CollectionError("automatic account hop needs an exact provider UUID")
    return f"{provider}:{exact}"


def _empty_ledger() -> dict[str, Any]:
    return {"schema_version": GUARD_SCHEMA_VERSION, "threads": {}}


def load_hop_ledger(config: Mapping[str, Any]) -> dict[str, Any]:
    """Read the automatic-hop ledger.

    Raises when the file exists but cannot be read or parsed. That is on
    purpose: an unreadable ledger means the hop count is unknown, and an
    unknown hop count must never be rounded down to zero, which is what a
    silent empty-ledger fallback would do on every future run.
    """
    raw = read_private_json(
        hop_ledger_path(config),
        max_bytes=MAX_HOP_LEDGER_BYTES,
        allow_missing=True,
    )
    if raw is None:
        return _empty_ledger()
    if not isinstance(raw, Mapping) or raw.get("schema_version") != GUARD_SCHEMA_VERSION:
        raise CollectionError("automatic account hop ledger has an unknown shape")
    threads_raw = raw.get("threads")
    if not isinstance(threads_raw, Mapping):
        raise CollectionError("automatic account hop ledger has an unknown shape")
    # No slice. A ledger larger than the cap is refused outright, because
    # returning the first N keys silently reports "never moved" for every
    # conversation past the cut -- which is the one-hop rule failing open on
    # exactly the busiest estates.
    if len(threads_raw) > MAX_LEDGER_THREADS:
        raise CollectionError(
            "automatic account hop ledger holds more than %d conversations"
            % MAX_LEDGER_THREADS
        )
    threads: dict[str, Any] = {}
    for key, value in threads_raw.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            raise CollectionError("automatic account hop ledger has an unknown shape")
        hops = value.get("hops")
        if not isinstance(hops, Sequence) or isinstance(hops, (str, bytes)):
            raise CollectionError("automatic account hop ledger has an unknown shape")
        kept = []
        for hop in hops:
            if not isinstance(hop, Mapping):
                raise CollectionError(
                    "automatic account hop ledger has an unknown shape"
                )
            kept.append(
                {
                    "at_unix": int(hop.get("at_unix") or 0),
                    "from": clean_text(hop.get("from"), 12),
                    "to": clean_text(hop.get("to"), 12),
                    "reason": clean_text(hop.get("reason"), 200),
                    "state": clean_text(hop.get("state"), 16) or "committed",
                    "token": clean_text(hop.get("token"), 32),
                }
            )
        recorded = value.get("count")
        threads[key] = {
            # `count` is the authority and only ever moves with a hop's own
            # lifecycle. The list is kept for the operator to read and may be
            # trimmed; the count may not, so trimming can never lower it.
            "count": max(
                int(recorded) if isinstance(recorded, int) and not isinstance(recorded, bool) else 0,
                len(kept),
            ),
            "hops": kept[-MAX_LEDGER_HOPS:],
            "notices": _clean_notices(value.get("notices")),
        }
    return {"schema_version": GUARD_SCHEMA_VERSION, "threads": threads}


def _clean_notices(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise CollectionError("automatic account hop ledger has an unknown shape")
    notices = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise CollectionError("automatic account hop ledger has an unknown shape")
        token = clean_text(item.get("token"), 64)
        if not token:
            continue
        notices.append(
            {
                "token": token,
                "sentence": clean_text(item.get("sentence"), 400),
                "at_unix": int(item.get("at_unix") or 0),
                "delivered": item.get("delivered") is True,
            }
        )
    return notices[-MAX_LEDGER_HOPS:]


def _empty_thread() -> dict[str, Any]:
    return {"count": 0, "hops": [], "notices": []}


def _prune_empty_threads(ledger: dict[str, Any]) -> None:
    """Drop only rows that no longer carry a move or a notice.

    Age can never make a counted hop disposable.  Removing an old counted row
    makes the next read report zero and grants that exact conversation a
    second automatic move.  Released reservations, by contrast, leave a
    zero-count empty row and are safe to remove.
    """
    for key, thread in list(ledger["threads"].items()):
        if int(thread["count"]) == 0 and not thread["hops"] and not thread["notices"]:
            del ledger["threads"][key]


def hop_count(config: Mapping[str, Any], provider: str, uuid: str) -> int:
    """How many automatic moves this exact conversation has been given.

    A move is counted the moment it is reserved, before the provider is
    signalled -- not when it completes. A crash between the two therefore
    reads as "already moved", which refuses a second move rather than
    granting one; the opposite ordering let a half-finished move look like no
    move at all and buy another paid account.

    Manual moves are not counted. The operator choosing an account himself is
    not the machine walking a conversation across their estate, and a rule that
    conflated them would take their own escape hatch away after one use.
    """
    thread = load_hop_ledger(config)["threads"].get(_thread_key(provider, uuid))
    return int(thread["count"]) if thread else 0


def begin_hop(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    source_alias: str,
    target_alias: str,
    *,
    reason: str = "",
    now: int | None = None,
) -> str:
    """Reserve this conversation's one move. Returns the token that names it.

    Called BEFORE anything irreversible. From here on the conversation counts
    as moved whatever happens next, so no failure can leave a moved
    conversation with a zero-hop record.

    The limit is enforced HERE, under the lock, not only where the plan reads
    the count. Two passes running at once -- the resident watchdog loop and a
    hand-run `--once`, say -- can both read a count of zero before either
    writes; only one of them can win the reservation, and the loser is refused
    rather than granted a second paid account.
    """
    key = _thread_key(provider, uuid)
    stamp = int(time.time() if now is None else now)
    token = uuidlib.uuid4().hex
    with _hop_ledger_lock(config):
        ledger = load_hop_ledger(config)
        _prune_empty_threads(ledger)
        thread = ledger["threads"].setdefault(key, _empty_thread())
        if int(thread["count"]) >= MAX_AUTOMATIC_HOPS:
            raise CollectionError(
                "this conversation has already had its %d automatic move"
                % MAX_AUTOMATIC_HOPS
            )
        thread["count"] = int(thread["count"]) + 1
        thread["hops"] = (
            thread["hops"]
            + [
                {
                    "at_unix": stamp,
                    "from": clean_text(source_alias, 12),
                    "to": clean_text(target_alias, 12),
                    "reason": clean_text(reason, 200),
                    "state": "pending",
                    "token": token,
                }
            ]
        )[-MAX_LEDGER_HOPS:]
        atomic_write_private_json(hop_ledger_path(config), ledger)
    return token


def _set_hop_state(
    config: Mapping[str, Any], provider: str, uuid: str, token: str, state: str
) -> bool:
    key = _thread_key(provider, uuid)
    stamp = clean_text(token, 32)
    if not stamp:
        return False
    with _hop_ledger_lock(config):
        ledger = load_hop_ledger(config)
        thread = ledger["threads"].get(key)
        if not thread:
            return False
        found = False
        for hop in thread["hops"]:
            if hop.get("token") == stamp:
                hop["state"] = state
                found = True
        if not found:
            return False
        atomic_write_private_json(hop_ledger_path(config), ledger)
        return True


def commit_hop(
    config: Mapping[str, Any], provider: str, uuid: str, token: str
) -> bool:
    """Mark a reserved move as completed. The count does not change."""
    return _set_hop_state(config, provider, uuid, token, "committed")


def release_hop(
    config: Mapping[str, Any], provider: str, uuid: str, token: str
) -> bool:
    """Give a reserved move back, only after a proven return to the source.

    The single caller is the rollback path, and only where the conversation
    has been proven to be running on the account it started on. Anywhere
    the outcome is unproven the reservation stands, because refusing a second
    automatic move costs the operator a manual switch while granting one costs
    them an account.
    """
    key = _thread_key(provider, uuid)
    stamp = clean_text(token, 32)
    if not stamp:
        return False
    with _hop_ledger_lock(config):
        ledger = load_hop_ledger(config)
        thread = ledger["threads"].get(key)
        if not thread:
            return False
        reserved = next(
            (hop for hop in thread["hops"] if hop.get("token") == stamp), None
        )
        if not reserved or reserved.get("state") != "pending":
            # A stale rollback command may outlive the move it belonged to.
            # Once committed, that move is the conversation's one hop forever
            # and no later caller may turn its count back into zero.
            return False
        remaining = [hop for hop in thread["hops"] if hop.get("token") != stamp]
        thread["hops"] = remaining
        thread["count"] = max(0, int(thread["count"]) - 1)
        _prune_empty_threads(ledger)
        atomic_write_private_json(hop_ledger_path(config), ledger)
        return True


def queue_notice(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    token: str,
    sentence: str,
    *,
    now: int | None = None,
) -> bool:
    """Record that the operator is owed one sentence about one move.

    Queuing is not telling them. The earlier version claimed the right to
    announce and then called the notifier, so a delivery that failed was
    filed as delivered and never retried -- the move happened and they were
    never told. The claim now belongs to delivery: this only records the debt,
    and it stays owed until something reports success.

    Returns True when this call created the entry, False when it already
    existed (delivered or not), so a driver running every few minutes cannot
    queue the same move twice.
    """
    key = _thread_key(provider, uuid)
    stamp = clean_text(token, 64)
    if not stamp:
        return False
    when = int(time.time() if now is None else now)
    with _hop_ledger_lock(config):
        ledger = load_hop_ledger(config)
        thread = ledger["threads"].setdefault(key, _empty_thread())
        if any(item["token"] == stamp for item in thread["notices"]):
            return False
        thread["notices"] = (
            thread["notices"]
            + [
                {
                    "token": stamp,
                    "sentence": clean_text(sentence, 400),
                    "at_unix": when,
                    "delivered": False,
                }
            ]
        )[-MAX_LEDGER_HOPS:]
        atomic_write_private_json(hop_ledger_path(config), ledger)
        return True


def notice_delivered(
    config: Mapping[str, Any], provider: str, uuid: str, token: str
) -> bool:
    """Mark one queued notice as actually delivered. Only a success may call."""
    key = _thread_key(provider, uuid)
    stamp = clean_text(token, 64)
    if not stamp:
        return False
    with _hop_ledger_lock(config):
        ledger = load_hop_ledger(config)
        thread = ledger["threads"].get(key)
        if not thread:
            return False
        found = False
        for item in thread["notices"]:
            if item["token"] == stamp and not item["delivered"]:
                item["delivered"] = True
                found = True
        if not found:
            return False
        atomic_write_private_json(hop_ledger_path(config), ledger)
        return True


def pending_notices(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every move the operator has not been told about yet, oldest first.

    This is the hand-off point for whatever route actually reaches them. A
    queue that survives a failed delivery is the whole reason it exists, so
    nothing here expires an undelivered entry.
    """
    ledger = load_hop_ledger(config)
    owed: list[dict[str, Any]] = []
    for key, thread in ledger["threads"].items():
        provider, _, uuid = key.partition(":")
        for item in thread["notices"]:
            if item["delivered"]:
                continue
            owed.append(
                {
                    "provider": provider,
                    "uuid": uuid,
                    "token": item["token"],
                    "sentence": item["sentence"],
                    "at_unix": item["at_unix"],
                }
            )
    owed.sort(key=lambda item: (item["at_unix"], item["token"]))
    return owed


def session_block_reason(
    row: Mapping[str, Any] | None,
    provider: str,
    uuid: str,
    *,
    source_alias: str = "",
) -> str:
    """Why this conversation must not be moved right now, or "" when it may be.

    The predicates are the manual switch's, plus two the automatic path needs
    and a human at the picker does not. A switch kills and relaunches the
    provider under a new profile, so a conversation that is mid-turn, running
    sub-agents, or has printed something in the last few seconds would lose
    work.

    The two extra checks are about acting on the right conversation. The
    account that ran dry is decided from the persistent binding, but the
    binding is a record and the live row is the fact: the collector publishes
    the account the process is actually signed in to, and publishes
    ``account_binding_mismatch`` when the two disagree. Moving on the record
    while the process is on some other account restarts a conversation whose
    live account was never the one found dry.
    """
    if not isinstance(row, Mapping):
        return "the conversation could not be found in the live inventory"
    identity = row.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    if row.get("provider") != provider or identity.get("uuid") != valid_uuid(uuid):
        return "the conversation's identity did not match"
    if row.get("mutation_allowed") is not True:
        return "the conversation is not one Session Kit may change"
    if row.get("account_binding_mismatch") is True:
        return (
            "the account it is signed in to does not match the account on record"
        )
    if source_alias:
        live_alias = clean_text(row.get("account_alias"), 12)
        if not live_alias:
            return "the account it is signed in to could not be read"
        if live_alias != source_alias:
            return (
                "it is signed in to %s, not the %s that ran dry"
                % (live_alias, source_alias)
            )
    if row.get("subagents") or int(row.get("active_subagent_count") or 0) != 0:
        return "the conversation is running sub-agents"
    status = str(row.get("agent_status") or "").casefold()
    if status not in MOVABLE_AGENT_STATUS:
        return f"the conversation is {status or 'in an unknown state'}"
    age = row.get("recent_output_age_seconds")
    if isinstance(age, int) and not isinstance(age, bool) and age < MIN_QUIET_SECONDS:
        return "the conversation printed something a moment ago"
    if clean_text(row.get("model"), 128) and row.get("model_handoff_capable") is not True:
        return (
            "its requested model is not bound to this managed shell generation"
        )
    return ""


def target_still_eligible(
    config: Mapping[str, Any],
    provider: str,
    alias: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Re-read, without changing anything, whether one account may be used.

    The last look before the point of no return, and deliberately read-only:
    it consults the registry and the usage feed and writes nothing, so asking
    the question can never be what makes the answer true. It exists because
    preparing a target profile is a *mutating* call, and the operator can
    switch an account off at any moment -- including between the moment policy
    chose it and the moment a conversation is handed to it.
    """
    if automatic_switching_off(config):
        return {"alias": alias, "eligible": False, "reason": "account changes are switched off"}
    choices = _accounts.account_choices(config, provider)
    if choices.get("roster_fresh") is not True or choices.get("advice_fresh") is not True:
        return {"alias": alias, "eligible": False, "reason": "the usage feed is stale"}
    rows = [
        item for item in choices.get("choices", []) if isinstance(item, Mapping)
    ]
    implausible = implausible_feed_reason(rows)
    if implausible:
        return {"alias": alias, "eligible": False, "reason": implausible}
    row = next(
        (
            item
            for item in rows
            if item.get("alias") == alias
        ),
        None,
    )
    if row is None:
        return {"alias": alias, "eligible": False, "reason": "it is not enrolled"}
    if row.get("eligible") is not True:
        return {
            "alias": alias,
            "eligible": False,
            "reason": clean_text(row.get("state"), 80) or "it is not selectable",
        }
    reserve = reserve_percent(environ)
    weekly = _usage_fraction(row.get("u7d"))
    if weekly is None or weekly > (100 - reserve) / 100.0:
        return {
            "alias": alias,
            "eligible": False,
            "reason": "it is no longer above the %d%% reserve" % reserve,
        }
    five_state, five_hour = _usage_state(row.get("u5h"))
    if five_state == "unreadable":
        return {
            "alias": alias,
            "eligible": False,
            "reason": "its five-hour usage is unreadable",
        }
    if five_hour is not None and five_hour >= 1.0:
        return {
            "alias": alias,
            "eligible": False,
            "reason": "its five-hour window is spent",
        }
    return {"alias": alias, "eligible": True, "reason": ""}


def spent_aliases(
    config: Mapping[str, Any],
    provider: str,
    *,
    environ: Mapping[str, str] | None = None,
    choices: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Which enrolled accounts the feed currently reports as run dry.

    The cheap question a periodic driver asks first. Nothing is spent while
    the feeds are stale, so a driver reading this never begins the expensive
    per-session pass on numbers it could not believe.
    """
    if provider not in _accounts.PROVIDERS:
        raise CollectionError("account provider is invalid")
    if automatic_switching_off(config):
        return {"provider": provider, "fresh": False, "aliases": [], "off": True}
    if choices is None:
        choices = _accounts.account_choices(config, provider)
    fresh = (
        choices.get("roster_fresh") is True and choices.get("advice_fresh") is True
    )
    if not fresh:
        return {"provider": provider, "fresh": False, "aliases": [], "off": False}
    rows = choices.get("choices", [])
    if implausible_feed_reason(rows):
        return {"provider": provider, "fresh": False, "aliases": [], "off": False}
    line = exhausted_percent(environ)
    aliases = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        weekly = _usage_fraction(row.get("u7d"))
        alias = str(row.get("alias") or "")
        if alias and weekly is not None and weekly * 100 >= line:
            aliases.append(alias)
    return {
        "provider": provider,
        "fresh": True,
        "aliases": sorted(aliases),
        "off": False,
    }


def _verdict(
    action: str,
    code: str,
    sentence: str,
    *,
    provider: str,
    uuid: str,
    source_alias: str = "",
    target_alias: str = "",
    reserve: int,
    hops: int,
    candidates: list[dict[str, Any]] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": GUARD_SCHEMA_VERSION,
        "action": action,
        "reason_code": code,
        "reason": sentence,
        "provider": provider,
        "uuid": valid_uuid(uuid) or "",
        "source_alias": source_alias,
        "target_alias": target_alias,
        "reserve_percent": reserve,
        "hops_used": hops,
        "hop_limit": MAX_AUTOMATIC_HOPS,
        "candidates": candidates or [],
    }
    if extra:
        value.update(dict(extra))
    return value


def plan(
    config: Mapping[str, Any],
    provider: str,
    uuid: str,
    session_row: Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
    choices: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide whether this conversation should be moved, and where.

    Returns a verdict; performs nothing. ``action`` is ``"switch"`` with a
    ``target_alias``, or ``"hold"`` with a ``reason`` written for a human.
    """
    exact = valid_uuid(uuid)
    if provider not in _accounts.PROVIDERS or not exact:
        raise CollectionError("automatic account plan needs an exact provider UUID")
    reserve = reserve_percent(environ)
    spent_line = exhausted_percent(environ)
    try:
        hops = hop_count(config, provider, exact)
    except CollectionError as exc:
        # Unknown hop count, so the one-hop rule cannot be enforced. Holding
        # is the only answer that cannot move a conversation twice.
        return _verdict(
            "hold",
            "ledger_unreadable",
            "The record of past automatic moves could not be read (%s), so the "
            "one-move limit cannot be enforced; nothing was moved." % exc,
            provider=provider,
            uuid=exact,
            reserve=reserve,
            hops=-1,
            extra={"needs_attention": True},
        )

    if automatic_switching_off(config):
        return _verdict(
            "hold",
            "kill_switch",
            "Automatic account changes are switched off; nothing was moved.",
            provider=provider,
            uuid=exact,
            reserve=reserve,
            hops=hops,
        )

    if choices is None:
        choices = _accounts.account_choices(config, provider)
    rows = [row for row in choices.get("choices", []) if isinstance(row, Mapping)]

    # Requirement in one line: no fresh fact, no move. The roster carries the
    # usage numbers the reserve is measured against and the advice carries the
    # choice of best account; either one stale and there is nothing to judge.
    if choices.get("roster_fresh") is not True or choices.get("advice_fresh") is not True:
        which = []
        if choices.get("roster_fresh") is not True:
            which.append("account usage")
        if choices.get("advice_fresh") is not True:
            which.append("rotation advice")
        return _verdict(
            "hold",
            "feed_stale",
            "The %s feed is stale or unreadable, so no account change can be "
            "judged; nothing was moved." % " and ".join(which),
            provider=provider,
            uuid=exact,
            reserve=reserve,
            hops=hops,
        )

    implausible = implausible_feed_reason(rows)
    if implausible:
        return _verdict(
            "hold",
            "feed_implausible",
            "%s, so none of its numbers can be judged; nothing was moved."
            % implausible,
            provider=provider,
            uuid=exact,
            reserve=reserve,
            hops=hops,
            extra={"needs_attention": True},
        )

    binding = _accounts.binding_for(config, provider, exact)
    source_alias = str(binding["alias"]) if binding else ""
    if not source_alias:
        return _verdict(
            "hold",
            "source_unknown",
            "The account this conversation is signed in to could not be "
            "proven; nothing was moved.",
            provider=provider,
            uuid=exact,
            reserve=reserve,
            hops=hops,
        )

    source_row = next((row for row in rows if row.get("alias") == source_alias), None)
    source_weekly = _usage_fraction(source_row.get("u7d")) if source_row else None
    if source_weekly is None:
        return _verdict(
            "hold",
            "usage_unknown",
            "The feed publishes no weekly usage for %s, so it cannot be shown "
            "to have run dry; nothing was moved." % source_alias,
            provider=provider,
            uuid=exact,
            source_alias=source_alias,
            reserve=reserve,
            hops=hops,
        )

    if source_weekly * 100 < spent_line:
        return _verdict(
            "hold",
            "source_has_quota",
            "%s still has %d%% of its weekly window; nothing was moved."
            % (source_alias, round((1 - source_weekly) * 100)),
            provider=provider,
            uuid=exact,
            source_alias=source_alias,
            reserve=reserve,
            hops=hops,
            extra={"source_weekly_used_percent": round(source_weekly * 100)},
        )

    # From here the account really is spent, so every remaining refusal is one
    # the operator needs to read.
    if hops >= MAX_AUTOMATIC_HOPS:
        return _verdict(
            "hold",
            "hop_limit",
            "This conversation was already moved once automatically and %s is "
            "now spent too. It stops here and waits for you rather than "
            "walking to a third account." % source_alias,
            provider=provider,
            uuid=exact,
            source_alias=source_alias,
            reserve=reserve,
            hops=hops,
            extra={"needs_attention": True},
        )

    blocked = session_block_reason(
        session_row, provider, exact, source_alias=source_alias
    )
    if blocked:
        return _verdict(
            "hold",
            "session_busy",
            "%s is spent but the conversation cannot be moved safely right "
            "now: %s." % (source_alias, blocked),
            provider=provider,
            uuid=exact,
            source_alias=source_alias,
            reserve=reserve,
            hops=hops,
        )

    ceiling = (100 - reserve) / 100.0
    candidates: list[dict[str, Any]] = []
    for row in rows:
        alias = str(row.get("alias") or "")
        if not alias or alias == source_alias or row.get("eligible") is not True:
            continue
        weekly = _usage_fraction(row.get("u7d"))
        if weekly is None or weekly > ceiling:
            continue
        five_state, five_hour = _usage_state(row.get("u5h"))
        if five_state == "unreadable":
            # A five-hour number the feed published but this code cannot read
            # is not permission to move here. Treating it as absent let a
            # target whose short window was already spent take the one hop.
            continue
        if five_hour is not None and five_hour >= 1.0:
            # Published as spent for the current five-hour window: moving here
            # lands the conversation in a wall and burns the only hop.
            continue
        candidates.append(
            {
                "alias": alias,
                "weekly_used_percent": round(weekly * 100),
                "weekly_left_percent": round((1 - weekly) * 100),
                "recommended": row.get("recommended") is True,
            }
        )
    candidates.sort(key=lambda item: (not item["recommended"], item["weekly_used_percent"], item["alias"]))

    if not candidates:
        return _verdict(
            "hold",
            "no_eligible_account",
            "%s is spent and no other account is both selectable and above "
            "the %d%% reserve, so automatic switching stood down and the work "
            "waits for you." % (source_alias, reserve),
            provider=provider,
            uuid=exact,
            source_alias=source_alias,
            reserve=reserve,
            hops=hops,
            extra={"needs_attention": True, "stood_down": True},
        )

    chosen = candidates[0]
    return _verdict(
        "switch",
        "move",
        "%s is spent; moving this conversation to %s, which still has %d%% of "
        "its weekly window."
        % (source_alias, chosen["alias"], chosen["weekly_left_percent"]),
        provider=provider,
        uuid=exact,
        source_alias=source_alias,
        target_alias=chosen["alias"],
        reserve=reserve,
        hops=hops,
        candidates=candidates,
        extra={
            "source_weekly_used_percent": round(source_weekly * 100),
            "advice_reason": clean_text(choices.get("recommendation_reason"), 240),
        },
    )
