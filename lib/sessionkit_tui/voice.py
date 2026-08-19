"""Every string this screen can show a person.

One file so the words can be read in one sitting and compared against
docs/voice.md, which governs them. Nothing here builds a sentence out of
fragments at the call site: a caller asks for the line it wants and gets the
whole line.
"""

from __future__ import annotations

# One grammar on every screen (operator ruling, 2026-08-15): every footer opens by saying
# what Enter does HERE, in this screen's own words, and every screen that can
# take `b` says `b back`. On the session list and the closed list, letters are
# the filter, so those screens name `esc` as the way back instead of stealing
# a letter.
#
# `needs you a` stayed out on purpose (found in review, 2026-08-15): on this
# door every printable key, `a` included, types into the filter box, and the
# derived needs_you field is not in that filter's haystack -- so following the
# hint HID the waiting session it claimed to list. The footer names only keys
# that do what they say. The waiting rows still sort to the top and still say
# "needs you" in their own row.
# Priority order to match the key-driven picker's footer (operator ruling,
# 2026-08-16): a narrow window truncates from the end, so position is
# survival, history is the first luxury, more is a door. esc stays last on
# this surface because it is named nowhere else.
FOOTER = (
    "↵ open · open # · kill k # · new n · more m · "
    "help ? · history h # · esc quit"
)

# The parts of the footer that name a key. Coloured so the eye finds them
# without the footer growing brackets or a glossary.
FOOTER_KEYS = ("↵", "#", "k #", "h #", "n", "m", "?", "esc")

BACK_FOOTER = "↵ choose · b back · esc back · ctrl-d quit"
HELP_FOOTER = "↵ back · b back · esc back · ctrl-d quit"
CLOSED_FOOTER = "↵ actions · esc back · ctrl-d quit"
# The rename prompt is the one panel where `b` is a name, not a key.
NAME_PROMPT_FOOTER = "↵ save · esc back · ctrl-d quit"

# The one cancel wording. Nothing else means "nothing happened".
NOTHING_CHANGED = "Nothing changed."
SUBAGENT_STAYS_WITH_PARENT = (
    "Subagent sessions stay with their parent. Nothing changed."
)

# The one error prefix. Errors go to stderr; screen copy never carries it.
ERROR_PREFIX = "session-kit"

# Row labels that are not sessions.
NEW_SESSION = "New session"
PROJECTS = "Projects"
CLOSED_SESSIONS = "Closed sessions"
HELP = "Help"

# The action panel, in the order a person reads it.
ACTION_OPEN = "Open"
ACTION_HISTORY = "History"
ACTION_CLOSE = "Close"
ACTION_RESTORE = "Restore"
ACTION_ACCOUNT = "Change account"
ACTION_MODEL = "Change model"
ACTION_RENAME = "Rename"
ACTION_COLOR = "Color"

# What an absent field renders as. Never "unknown", never a blank column.
MISSING = "pending"

PICKER_HELP = "Picker help"

# The old picker's key table, narrowed only where this cursor-driven screen
# deliberately gives the same task a different key. The stable two-column
# shape matters more than wrapping prose around it.
HELP_ROWS = (
    ("Sessions", "↑ / ↓", "Move the highlight"),
    ("Sessions", "Enter", "Open the highlighted session"),
    ("Sessions", "number", "Mark a session; commas and ranges mark several"),
    ("Sessions", "h", "Read history from the highlighted session's actions"),
    ("Sessions", "n", "Choose New session from the list"),
    ("Sessions", "k", "Choose Close from the marked sessions' actions"),
    ("Sessions", "name", "Choose Rename from a session's actions"),
    ("Needs you", "a", "Filter the list as you type"),
    ("The list", "text", "Type to filter names, providers, and projects"),
    ("The list", "Esc", "Clear typed text, go back, then quit"),
    ("The list", "mouse", "Wheel to scroll; click to highlight or open"),
    ("The list", "?", "Choose Help from the list"),
    ("Going back", "Enter", "Takes the choice under the cursor on any screen"),
    ("Going back", "b", "Back, on every screen that is not filtering by name"),
    ("Quitting", "Esc", "Quit when there is nothing to clear or go back from"),
    ("Quitting", "q", "Quit from the main list"),
    ("Quitting", "Ctrl-D", "Quit the picker from anywhere"),
)

HELP_NOTES = (
    "Nothing on this screen asks you to confirm. An action runs, then says what it did.",
    "Inside a session: Ctrl-Q leaves it running, Ctrl-D or bye closes it.",
    "History is an action on every session row.",
)


def plural(count: int, singular: str, many: str | None = None) -> str:
    """`1 session`, `3 sessions`, the count and its noun, never apart."""

    word = singular if count == 1 else (many or singular + "s")
    return f"{count} {word}"


# There was a needs_you_line() here that printed "needs you: N · N sessions"
# above the footer. An attention summary above the footer is out, by operator
# ruling (2026-08-15) -- the same ruling that removed the older picker's
# version of it, which had grown a repair-failure part and was announcing fifty
# failed repairs that had never been attempted. Both doors are kept identical
# on purpose: the two of them drifting apart is its own recurring bug in this
# tree. The rows still say "needs you" and still sort to the top; on the older
# picker the `a` screen still lists them, which is NOT true on this door and
# was wrongly claimed here.


def machine_row(count: int, needing: int = 0) -> str:
    """The counted row machine sessions live behind."""

    label = plural(count, "machine session")
    if needing > 0:
        return f"{label} · {needing} need you" if needing != 1 else f"{label} · 1 needs you"
    return label


def no_such_session(number: int | str) -> str:
    """The one refusal grammar: the fact, then the way forward."""

    return (
        f"There is no session {number} on this screen. Numbers shown here work."
    )


def empty(thing: str) -> str:
    """The one empty state."""

    return f"{thing}: none."


def closed_one(name: str) -> str:
    return f"Closed {name}."


def closed_many(count: int) -> str:
    return f"Closed {plural(count, 'session')}."


def opened(name: str) -> str:
    return f"Opened {name}."


def restored(name: str) -> str:
    return f"Restored {name}."


def renamed(name: str) -> str:
    return f"Renamed {name}."


def account_changed(name: str, alias: str) -> str:
    return f"{name} now uses account {alias}."


def model_changed(name: str, model: str) -> str:
    return f"{name} now runs {model}."


def color_changed(name: str, color: str) -> str:
    return f"{name} is now {color}."


def action_failed(name: str) -> str:
    """A refused action changed nothing, and says so in the cancel wording."""

    return f"{name} was refused. {NOTHING_CHANGED}"


def error(fact: str) -> str:
    """The stderr line for a picker that cannot run. One prefix, one period."""

    return f"{ERROR_PREFIX}: {fact.rstrip('.')}."
