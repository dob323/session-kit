"""What a screen printed beside a row, so the word they type still means it.

A selector is only worth typing if it still names the row it was printed
beside. Two of them cannot be checked by looking at them: `unnamed`, and the
clock face a shared name falls back to. Both are properties of the LIST, and
the list is rebuilt every time it is asked for -- so between printing a word
and acting on it, the word can quietly change conversations:

    sp recover   ->  @19:00  [Claude] Alpha  [closed 1 day ago]
                     @20:00  [Claude] Alpha  [closed 1 day ago]

    (the 19:00 conversation's transcript goes missing, so it can no longer
     come back and loses its selector; a third Alpha closes the next day at
     the same time of day and takes `@19:00` for itself)

    sp restore @19:00  ->  Restored Alpha.

They asked for one conversation, a different one opened, and the sentence they
was shown was true of the wrong one -- both rows are called Alpha, so nothing
on the screen could tell them. Rebinding is the point of the fallback: while a
name is shared, each row goes by its own event. What must not happen is a word
being rebound BETWEEN the screen and the action.

So a surface that prints the list records what it printed beside each row, and
an action checks the word it was given against that record. Same conversation,
or refuse -- and refuse in a sentence, never a restore that reports success.

The record is deliberately dumber than the projection: it holds the token, the
conversation it stood for, and when it was printed. It is not a lock, not a
queue and not a second list; it cannot make a wrong restore right, it can only
stop a word from changing its mind. What it does not know, it says so about --
a token nobody has printed on this machine is not evidence of anything, and
the caller is told exactly that rather than a guess.

Where it stops, exactly, since a check that overstates itself is worse than
none. Two screens printed at the same instant merge one into the other, and
the token count is bounded, so a binding can be lost; a lost binding reads as
never printed, which the caller is allowed to act on. And a second screen
printed between the one they read and the word they type is a screen this machine
did show them, so its meaning is the one that stands. Neither is the reported
failure -- that one is a list rebuilt by the restore itself, with no screen in
between -- and both are the same direction the surfaces already fail in: they
lose a check, never gain a wrong one.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Iterable, Mapping

from .common import valid_uuid
from .state_io import atomic_write_json


SCHEMA_VERSION = 1

# How many printed words are remembered. Every screen prints at most the whole
# recovery list, which is itself capped, and a redraw of the same list rewrites
# the same tokens rather than adding any -- so this holds several distinct
# screens, not several minutes. The oldest go first.
MAX_REMEMBERED = 2000

# The three answers a check can have. UNKNOWN is not a quiet yes: it says this
# machine has no record of that word ever being printed, and the caller decides
# what to do about that.
AGREES = "agrees"
DISAGREES = "disagrees"
UNKNOWN = "unknown"


def printed_selectors_path(state_dir: Any) -> Path:
    return Path(state_dir) / "recovery-selectors-printed.json"


def folded(token: Any) -> str:
    """A typed word and a printed one, reduced to the same thing.

    Both surfaces already match case-insensitively on runs of whitespace
    collapsed to one space; the record has to fold a word exactly the way the
    matcher does, or it would answer about a different string than the one the
    action resolved.
    """
    return " ".join(str(token or "").split()).casefold()


def load_printed(state_dir: Any) -> dict[str, dict[str, Any]]:
    """Every word this machine has printed, and what it stood for.

    A record that cannot be read is not a claim that a word means something
    else; it is no record at all. Every malformed shape is dropped rather than
    trusted, and a caller that gets nothing back is told UNKNOWN, never AGREES.
    """
    try:
        with printed_selectors_path(state_dir).open(encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(document, Mapping):
        return {}
    recorded = document.get("selectors")
    if not isinstance(recorded, Mapping):
        return {}
    known: dict[str, dict[str, Any]] = {}
    for token, binding in recorded.items():
        if not isinstance(token, str) or not isinstance(binding, Mapping):
            continue
        provider = binding.get("provider")
        exact = valid_uuid(binding.get("uuid")) or ""
        when = binding.get("printed_at_unix_ms")
        if not isinstance(provider, str) or not provider or not exact:
            continue
        known[folded(token)] = {
            "provider": provider,
            "uuid": exact,
            "printed_at_unix_ms": (
                when
                if isinstance(when, int) and not isinstance(when, bool) and when > 0
                else 0
            ),
        }
    return known


def remember_printed(
    state_dir: Any,
    rows: Iterable[Any],
    *,
    now_unix_ms: int | None = None,
) -> dict[str, Any]:
    """Record the word each row was printed under, on the screen just drawn.

    Later prints win: a word that legitimately moved to another row -- which is
    what the fallback is FOR -- is the word they have just been shown, and the
    screen they have just been shown is the one they are about to type at. Words that
    were not on this screen keep the row they were last printed beside, so a
    conversation that leaves the list does not quietly release its word to the
    next one.
    """
    stamp = (
        now_unix_ms
        if isinstance(now_unix_ms, int) and not isinstance(now_unix_ms, bool)
        else int(time.time() * 1000)
    )
    known = load_printed(state_dir)
    printed = 0
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        token = folded(row.get("selector"))
        provider = row.get("provider")
        exact = valid_uuid(row.get("uuid")) or ""
        # A row with no selector offers no word to type, and a row with no
        # conversation has nothing a word could stand for.
        if not token or not isinstance(provider, str) or not provider or not exact:
            continue
        known[token] = {
            "provider": provider,
            "uuid": exact,
            "printed_at_unix_ms": stamp,
        }
        printed += 1
    if len(known) > MAX_REMEMBERED:
        newest = sorted(
            known.items(),
            key=lambda item: (item[1]["printed_at_unix_ms"], item[0]),
            reverse=True,
        )[:MAX_REMEMBERED]
        known = dict(newest)
    directory = Path(state_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_json(
        printed_selectors_path(directory),
        {"schema_version": SCHEMA_VERSION, "selectors": known},
    )
    return {"remembered": printed, "known": len(known)}


def check_printed(
    state_dir: Any, token: Any, provider: Any, uuid: Any
) -> dict[str, Any]:
    """Does this word still name the conversation it was printed beside?

    AGREES and DISAGREES are about a word this machine has actually printed.
    UNKNOWN is the honest third answer: nothing here has ever shown them that
    word, so there is no earlier meaning for it to have drifted from.
    """
    wanted = folded(token)
    exact = valid_uuid(uuid) or ""
    binding = load_printed(state_dir).get(wanted)
    if binding is None:
        return {"token": wanted, "verdict": UNKNOWN}
    same = (
        isinstance(provider, str)
        and binding["provider"] == provider
        and bool(exact)
        and binding["uuid"].casefold() == exact.casefold()
    )
    return {
        "token": wanted,
        "verdict": AGREES if same else DISAGREES,
        "printed_at_unix_ms": binding["printed_at_unix_ms"],
    }
