"""Every word the kit shows a person, and the collector words they translate.

`docs/voice.md` is the contract; this module is that contract as code. A screen
that needs a word imports it from here, so `sp list`, `sp detail`, the picker
and the machine snapshot can never drift into three spellings of one idea
again. Nothing in this file reads a file, a clock, or the environment: every
name is a constant and every function is pure, which is why the whole module
can be asserted directly in tests instead of through a rendered screen.

Two vocabularies live here and they are not the same thing:

* **Screen words** — what a person reads: `question`, `needs you`, `working`, `idle`,
  `Ready`, `Open elsewhere`. Changing one changes what the kit says.
* **Collector words** — what the collectors write into an inventory row:
  `needs your reply`, `running`, `attached`. They are evidence, not copy. They
  are named here only so the renderers compare against one spelling, and they
  are never renamed at the source.

The translation between the two happens at the render boundary, in
`state_word()` and `session_state()`, and nowhere else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .common import STALL_DEFAULT_SECONDS, clean_text


# --------------------------------------------------------------- placeholders

# A value the kit has not pulled yet. One rule for every column (operator
# ruling, 2026-08-16): until a section has a valid read, it says `pending` —
# never `unknown`, never a dash, never an empty cell.
MISSING = "pending"

# Punctuation the screens share: what a truncated string ends with, and what
# separates two facts on one line.
ELLIPSIS = "…"
SEPARATOR = " | "

# A session with no number of its own: the column still holds its place, and
# the row says plainly that there is nothing to type.
UNNUMBERED = "-"

# How long a session may print nothing before the kit stops describing it as
# working. The value is defined beside the shared environment resolver in
# ``common`` and re-exported here for the state vocabulary's callers.


# ---------------------------------------------------------- collector words

AVAILABILITY_READY = "ready"
AVAILABILITY_ATTACHED = "attached"
# Display order for the two availability groups, and the only two values a
# session row may carry.
AVAILABILITY_ORDER = (AVAILABILITY_READY, AVAILABILITY_ATTACHED)

PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"
PROVIDER_SHELL = "shell"
PROVIDER_UNKNOWN = "unknown"
PROVIDER_ORDER = (PROVIDER_CLAUDE, PROVIDER_CODEX, PROVIDER_SHELL, PROVIDER_UNKNOWN)

STATUS_NEEDS_YOUR_REPLY = "needs your reply"
STATUS_REPLY_OPTIONAL = "reply optional"
STATUS_RUNNING = "running"
STATUS_STATE_UNAVAILABLE = "state unavailable"
STATUS_PROVIDER_EXITED = "provider exited"
# Both collectors write this one, and it is the same fact from either: a Codex
# rollout whose last lifecycle event is `task_complete`, and a `claude agents
# --json` row parked at its prompt, both say `idle`. Neither is a state a
# person can act on, and both mean the session is waiting for them.
STATUS_IDLE = "idle"
# What a live Claude poll actually calls a session mid-turn is `busy`, which
# `attention.poll_attention` already rewrites; `working` is the documented
# spelling and is translated here so a vendor that starts sending it cannot
# leak a raw collector word onto the screen.
STATUS_WORKING = "working"
# The collector's own "I could not resolve this session" word, written at the
# unresolved-provider branch and as the base row's default.
STATUS_UNKNOWN = "unknown"

TITLE_STATE_READY = "ready"
TITLE_STATE_PENDING = "pending"

CONFIDENCE_EXACT = "exact"

# Fleet stall degrees. `docs/voice.md`: unsurfaced, unanswered and silent are
# degrees of **needs you** and render as that one state. `orphan` is the fourth
# reason the fleet flagger can write and is deliberately absent: an orphaned
# session is a dead session, not a person's turn.
STALL_REASONS_NEEDS_YOU = ("unsurfaced", "unanswered", "silent")


# ------------------------------------------------------------ session states

# `idle` used to be a third word and was retired (operator ruling,
# 2026-08-15). It never meant anything a person could act on, and it made the
# two providers describe
# one situation in two ways: a Claude session parked at its prompt raises the
# vendor's own `idle_prompt` notification and read as `needs you`, while a
# Codex session parked at exactly the same prompt writes `task_complete` to its
# rollout, has no notification hook at all, and read as `idle`. Same screen,
# same situation, two words. Both first become `needs you` at this boundary.
#
# The new `idle` (operator ruling, 2026-08-16) is a different fact. It is never
# derived from either vendor's notification vocabulary. It means a session that
# would read `needs you` whose own transcript path, size and nanosecond mtime
# have remained unchanged for the operator's full ageing window.
WAITING_ON_YOU = "needs you"
QUESTION = "question"
WORKING = "working"
IDLE = "idle"
SETUP_INCOMPLETE = "pending"
STATUS_UNAVAILABLE = "pending"

# The historical spelling. Kept as one name for one string so nothing that
# still imports it silently starts printing a different word than the picker.
NEEDS_YOU = WAITING_ON_YOU

# Both can't-read cases say `pending` (operator rule, 2026-08-16: every
# section without a valid pulled value says pending). They are not a third state of the
# working/waiting pair: they are the kit saying it cannot read this session.
# A row it cannot read must not be described as either.
SESSION_STATES = (
    QUESTION,
    WAITING_ON_YOU,
    WORKING,
    IDLE,
    SETUP_INCOMPLETE,
    STATUS_UNAVAILABLE,
)

# The full translation, used for the machine snapshot's `state` field and by
# every screen that shows one state word per session.
#
# The provider's own exit is a person's turn, not a state of its own: the
# provider is gone and only they can restore or close the shell. It used to
# print `provider exited` straight onto the screen.
STATE_WORDS = {
    STATUS_NEEDS_YOUR_REPLY: WAITING_ON_YOU,
    STATUS_REPLY_OPTIONAL: WORKING,
    STATUS_RUNNING: WORKING,
    STATUS_IDLE: WAITING_ON_YOU,
    STATUS_WORKING: WORKING,
    STATUS_PROVIDER_EXITED: WAITING_ON_YOU,
    STATUS_STATE_UNAVAILABLE: STATUS_UNAVAILABLE,
    STATUS_UNKNOWN: STATUS_UNAVAILABLE,
}

# `sp list` prints the collector's own status beside a session, and has only
# ever renamed the one word that contradicted the term sheet. It is a subset of
# STATE_WORDS, named rather than inlined, so the day the two converge is a
# deliberate edit here and not an accident somewhere else.
LIST_STATE_WORDS = {
    STATUS_NEEDS_YOUR_REPLY: NEEDS_YOU,
}

# Every word a collector may write into `agent_status`. `state_word` must have
# an entry for each: the test that walks this tuple is what keeps the mapping
# total as providers add vocabulary.
COLLECTOR_STATUSES = (
    STATUS_NEEDS_YOUR_REPLY,
    STATUS_REPLY_OPTIONAL,
    STATUS_RUNNING,
    STATUS_WORKING,
    STATUS_IDLE,
    STATUS_PROVIDER_EXITED,
    STATUS_STATE_UNAVAILABLE,
    STATUS_UNKNOWN,
)


def state_word(text: str, *, terms: Mapping[str, str] | None = None) -> str:
    """One screen word for one collector word. TOTAL — never the word itself.

    This used to end `table.get(text, text)`, so any status the table did not
    name printed verbatim: `unknown`, `provider exited`, the vendor's `busy`,
    and anything a future provider release invents. That is a collector word on
    a person's screen, which this module's own docstring forbids, and it meant
    the vocabulary was open-ended rather than the four action words named here.

    A word this file does not recognise is not a state — it is the kit failing
    to read one, and it says so.
    """
    table = STATE_WORDS if terms is None else terms
    return table.get(text.casefold(), STATUS_UNAVAILABLE)


def session_state(
    row: Mapping[str, Any],
    *,
    stall_seconds: int,
) -> str:
    """The one state word for a session row.

    The precedence is the picker's, because the picker is the screen a person
    reads first: an unfinished launch is named before anything else, a person's
    turn beats every other description, an optional reply is still the model
    working, and a session that has printed nothing for the whole stall window
    has stopped working whatever the provider calls itself — which leaves the
    person, so it says so.
    """
    if row.get("setup_incomplete"):
        return SETUP_INCOMPLETE
    # An open picker or permission approval is one keystroke from unblocking
    # the whole session, so it outranks every other readable state, including
    # an old transcript which has also crossed the idle window.
    if row.get("blocking_question") is True:
        return QUESTION
    status = clean_text(row.get("agent_status"), 64) or STATUS_STATE_UNAVAILABLE
    if row.get("needs_you"):
        return IDLE if row.get("transcript_idle") is True else WAITING_ON_YOU
    reply_optional = (
        bool(row.get("reply_optional"))
        or status.casefold() == STATUS_REPLY_OPTIONAL
    )
    if reply_optional:
        return WORKING
    state = state_word(status)
    quiet = row.get("recent_output_age_seconds")
    if (
        state == WORKING
        and isinstance(quiet, int)
        and not isinstance(quiet, bool)
        and quiet >= stall_seconds
    ):
        state = WAITING_ON_YOU
    if state == WAITING_ON_YOU and row.get("transcript_idle") is True:
        return IDLE
    return state


# ------------------------------------------------------------ provider names

# Provider names are product names and are always capitalized. A plain shell is
# a common noun; the group heading capitalizes it the way any heading would.
PROVIDER_NAMES = {
    PROVIDER_CLAUDE: "Claude",
    PROVIDER_CODEX: "Codex",
    PROVIDER_SHELL: "Shell",
    PROVIDER_UNKNOWN: "Unknown",
}


def provider_name(value: object) -> str:
    """The display name for a provider, or the placeholder when there is none."""
    text = clean_text(value, 40)
    if not text:
        return MISSING
    return PROVIDER_NAMES.get(text.casefold(), text.title())


# --------------------------------------------------------------- group words

GROUP_READY = "Ready"
GROUP_OPEN_ELSEWHERE = "Open elsewhere"
GROUP_OUTSIDE_THE_KIT = "Outside the kit"

WHERE_READY = "ready"
WHERE_OPEN_ELSEWHERE = "open elsewhere"


def group_heading(availability: str) -> str:
    """The heading a group of sessions sits under."""
    return GROUP_READY if availability == AVAILABILITY_READY else GROUP_OPEN_ELSEWHERE


def where_word(availability: object) -> str:
    """Where a session can be opened, in the words every screen uses."""
    text = clean_text(availability, 40).casefold()
    if text == AVAILABILITY_READY:
        return WHERE_READY
    if text == AVAILABILITY_ATTACHED:
        return WHERE_OPEN_ELSEWHERE
    return text or MISSING


# -------------------------------------------------------------- title states

TITLE_STATE_SHOWING = "showing in the provider"
TITLE_STATE_WAITING = "waits for the session to restart"


def title_state(value: object) -> str:
    """Whether the provider is already showing this session's title.

    Never the word `ready`: that is reserved for "openable in this window", and
    a reader who has learned it would read this row as a place to open.
    """
    state = clean_text(value, 40).casefold()
    if state == TITLE_STATE_READY:
        return TITLE_STATE_SHOWING
    if state == TITLE_STATE_PENDING:
        return TITLE_STATE_WAITING
    return state


# ---------------------------------------------------------------- age phrases

JUST_NOW = "just now"
LOCAL_TIME = "local time"


def plural(count: int, singular: str, suffix: str = "s") -> str:
    """`1 day`, `2 days` — the kit never prints `1 days` or `day(s)`."""
    return singular if count == 1 else f"{singular}{suffix}"


def relative_time(timestamp_ms: int, now_ms: int) -> str:
    """`just now`, `12 min ago`, `1 hr ago`, `3 days ago`, then a date."""
    seconds = max(0, (now_ms - timestamp_ms) // 1000)
    if seconds < 60:
        return JUST_NOW
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} hr ago"
    if seconds < 30 * 86400:
        days = seconds // 86400
        return f"{days} {plural(days, 'day')} ago"
    local = datetime.fromtimestamp(timestamp_ms / 1000).astimezone()
    current = datetime.fromtimestamp(now_ms / 1000).astimezone()
    date = f"{local.strftime('%b')} {local.day}"
    return date if local.year == current.year else f"{date}, {local.year}"


def precise_local_time(timestamp_ms: int) -> str:
    """`Aug 12, 2026 at 4:05 PM CDT` — the exact moment, in the reader's zone."""
    local = datetime.fromtimestamp(timestamp_ms / 1000).astimezone()
    hour = local.hour % 12 or 12
    zone = local.tzname() or LOCAL_TIME
    return (
        f"{local.strftime('%b')} {local.day}, {local.year} at "
        f"{hour}:{local.minute:02d} {local.strftime('%p')} {zone}"
    )


def exact_and_relative(timestamp_ms: int, now_ms: int) -> str:
    """The detail view's one time form: the moment, then how long ago."""
    return f"{precise_local_time(timestamp_ms)} ({relative_time(timestamp_ms, now_ms)})"


def duration(seconds: int) -> str:
    """A span, in the compact form the lists use: `5m`, `2h 3m`, `1d 4h`."""
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def brief_age(seconds: int) -> str:
    """`duration()` with a word for the first minute: `now`, then `5m`."""
    if seconds < 60:
        return "now"
    return duration(seconds)


def waiting_phrase(seconds: int) -> str:
    """How long a session has been a person's turn: `needs you 12 min`."""
    if seconds < 60:
        return f"{NEEDS_YOU} under 1 min"
    if seconds < 3600:
        return f"{NEEDS_YOU} {seconds // 60} min"
    if seconds < 86400:
        return f"{NEEDS_YOU} {seconds // 3600} hr"
    days = seconds // 86400
    return f"{NEEDS_YOU} {days} {plural(days, 'day')}"


def waiting_since(timestamp_ms: int, now_ms: int) -> str:
    """`needs you 12 min` while that reads, then `needs you since Jul 4`."""
    seconds = max(0, (now_ms - timestamp_ms) // 1000)
    if seconds < 30 * 86400:
        return waiting_phrase(seconds)
    return f"{NEEDS_YOU} since {relative_time(timestamp_ms, now_ms)}"


# ------------------------------------------------------------- list wording

SESSION = "session"
SESSIONS_EMPTY = "Sessions: none."
NO_MATCHES = "No matches."


def empty_state(thing: str) -> str:
    """`Accounts: none.` — the one empty-state form. A count line at zero
    disappears rather than printing `0`."""
    return f"{thing}: {NONE}."


def session_count(total: int, ready: int, elsewhere: int) -> str:
    """`3 sessions: 2 ready, 1 open elsewhere` — the line above the list."""
    return (
        f"{total} {plural(total, SESSION)}: "
        f"{ready} {WHERE_READY}, {elsewhere} {WHERE_OPEN_ELSEWHERE}"
    )


def stale_warning(source: object) -> str:
    """The list is not live, and says which copy it is showing."""
    return f"Warning: showing {source} inventory."


def quiet_status(age: str) -> str:
    """A provider that claims to be running but has printed nothing."""
    return f"quiet {age}"


# ------------------------------------------------------ the one time phrase

# Every session row carries exactly one time, and it answers one question:
# when did this session last do something. It used to carry two — `recent
# output 3h 9m ago` beside `process age 3h 9m` — which is one fact more than
# any row had room for, so every row truncated at a different point and the
# column stopped being a column (operator ruling, 2026-08-15). The second fact, how long
# the process has been alive, is still on `sp detail`, where there is room for
# it and nothing else is competing for the line.
LAST_ACTIVE = "last active"
LAST_ACTIVE_UNKNOWN = f"{LAST_ACTIVE} pending"
LAST_ACTIVE_JUST_NOW = f"{LAST_ACTIVE} just now"


def last_active(seconds: int | None) -> str:
    """`last active 3h 9m ago` — the same words on every row, always.

    One phrase and one shape, so the column lines up down the whole list at
    any width instead of alternating between `3 hr ago` and `opened 3 hr ago`.
    """
    if seconds is None or isinstance(seconds, bool) or not isinstance(seconds, int):
        return LAST_ACTIVE_UNKNOWN
    if seconds < 0:
        return LAST_ACTIVE_UNKNOWN
    if seconds < 60:
        return LAST_ACTIVE_JUST_NOW
    return f"{LAST_ACTIVE} {duration(seconds)} ago"


# ------------------------------------------------------------- model wording

# What the model column says when there is no model to name. A dash told the
# person the field exists and is empty, which for this column was the one thing
# they already knew; each of these says WHY it is empty instead.
#
# The collector records the reason (`model_state`) and the screens turn it into
# a word here — a placeholder is copy, and copy does not belong in a row.
MODEL_STATE_NOT_APPLICABLE = "not-applicable"
MODEL_STATE_NO_REPLY_YET = "no-reply-yet"
MODEL_STATE_UNREADABLE = "unreadable"

MODEL_NOT_APPLICABLE = "no model"
MODEL_NO_REPLY_YET = "pending"
MODEL_UNREADABLE = "pending"

MODEL_STATE_WORDS = {
    MODEL_STATE_NOT_APPLICABLE: MODEL_NOT_APPLICABLE,
    MODEL_STATE_NO_REPLY_YET: MODEL_NO_REPLY_YET,
    MODEL_STATE_UNREADABLE: MODEL_UNREADABLE,
}


def model_state_word(state: object, provider: object) -> str:
    """Why this row has no model, in words, or a guess from the provider.

    A row assembled before the collector recorded a reason still has to say
    something true: a shell has no model to show, and a provider session whose
    own record could not be read is exactly that.
    """
    word = MODEL_STATE_WORDS.get(clean_text(state, 40))
    if word:
        return word
    family = clean_text(provider, 40).casefold()
    if family in {PROVIDER_CLAUDE, PROVIDER_CODEX}:
        return MODEL_UNREADABLE
    return MODEL_NOT_APPLICABLE


def model_cell(row: Mapping[str, Any]) -> str:
    """What the model column says for one row, on EVERY surface.

    Both stacks used to answer this separately and they disagreed: the picker
    honoured the collector's recorded reason and said `no reply yet` for a
    brand-new conversation, while the cursor picker never read that field,
    guessed from the provider alone, and called the same healthy session
    `unreadable` — the kit reporting a broken transcript with nothing wrong.
    One function, so there is one answer.
    """
    display = clean_text(row.get("display_model"), 80)
    if display:
        return display
    model = clean_text(row.get("model"), 160)
    if model:
        return model
    return model_state_word(
        row.get("model_state"), row.get("display_provider") or row.get("provider")
    )


# The furthest into the past a recorded moment may sit and still be believed.
# Ten years: longer than any session, and the shape a bad stamp takes.
MAX_PLAUSIBLE_AGE_SECONDS = 10 * 365 * 86400


def last_active_seconds(row: Mapping[str, Any], now_ms: int) -> int | None:
    """How long since this session last did anything, on EVERY surface.

    The recorded moment is preferred, because a list can be drawn many times
    from one snapshot and the collector's stored age froze when it was written.
    Two ways a stamp is refused rather than believed:

    * it is in the FUTURE. That used to be clamped to zero, so a box whose
      clock had skewed forward — or a stamp source ahead of it — reported every
      row as `just now`, which turns a screen full of sessions waiting on a
      person into a screen full of busy ones. Silence read as activity is the
      failure this column exists to prevent.
    * it is implausibly old, which is what a few-milliseconds-past-the-epoch
      stamp subtracts to.

    In both cases the collector's recorded age answers instead.
    """
    stamp = row.get("recent_output_at_unix_ms")
    if isinstance(stamp, int) and not isinstance(stamp, bool) and stamp > 0:
        age = (now_ms - stamp) // 1000
        if 0 <= age <= MAX_PLAUSIBLE_AGE_SECONDS:
            return age
    seconds = row.get("recent_output_age_seconds")
    if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds >= 0:
        return seconds
    return None


def row_last_active(row: Mapping[str, Any], now_ms: int) -> str:
    """The one time phrase a session row shows, on EVERY surface."""
    return last_active(last_active_seconds(row, now_ms))


def row_last_active_compact(row: Mapping[str, Any], now_ms: int) -> str:
    """An identifiable time token for a row that cannot carry the full label."""

    seconds = last_active_seconds(row, now_ms)
    if seconds is None:
        return "pending"
    return brief_age(seconds)


def subagent_detail(count: int) -> str:
    return f"{count} {plural(count, 'subagent')}"


def worktree_detail(branch: str) -> str:
    return f"worktree {branch}"


# The footer of `sp list`: verb first, one action per clause.
# '#' is the number placeholder (operator ruling 2026-08-16): a bare n reads
# as a letter key; '#' reads as "a number goes here".
LIST_HINTS = (
    "sp go # open · sp new start",
    'sp close # close · sp find "text" search history',
)


# ------------------------------------------------------------ detail wording

DETAIL_HEADING = "Session detail"

DETAIL_SESSION = "Session"
DETAIL_TITLE = "Title"
DETAIL_PROVIDER = "Provider"
DETAIL_ACCOUNT_ALIAS = "Account alias"
DETAIL_ACCOUNT_EMAIL = "Account email"
DETAIL_ACCOUNT_PLAN = "Account plan"
DETAIL_MODEL = "Model"
DETAIL_STATE = "State"
DETAIL_WHERE = "Where"
DETAIL_LAST_RESPONSE = "Last response"
DETAIL_WAITING_SINCE = "Waiting since"
DETAIL_OPENED = "Opened"
# The list rows carry one time each; this is where the other one still lives.
DETAIL_PROCESS_AGE = "Running for"
DETAIL_PROJECT = "Project"
DETAIL_WORKTREE = "Worktree"
DETAIL_COLOR = "Color"
DETAIL_CONVERSATION = "Conversation"
DETAIL_SUBAGENTS = "Subagents"
DETAIL_CHILD_SHELL = "Child shell"
DETAIL_CHILD_WORKER = "Child worker"
CHILD_KIND_WORKER = "worker"
DETAIL_TITLE_STATE = "Title state"

NOT_NUMBERED = "not numbered"
CONVERSATION_EXACT = "exact"
CONVERSATION_INEXACT = "not yet exact"
NONE = "none"


# --------------------------------------------------- refusals and outcomes

# One prefix for a genuine failure, on stderr. Screen copy is the UI and prints
# unprefixed on stdout.
ERROR_PREFIX = "session-kit:"

NO_MATCHING_SESSION = f"{ERROR_PREFIX} no session matches that selector"

# The only cancel wording in the kit.
CANCEL = "Nothing changed."

NUMBERS_HINT = "Numbers shown here work."


def error(fact: str) -> str:
    """`session-kit: <fact>.` — the one error form, for stderr."""
    return f"{ERROR_PREFIX} {fact}"


def no_such_session(number: object) -> str:
    """State the fact, then the way forward, in one line."""
    return f"There is no session {number} on this screen. {NUMBERS_HINT}"
