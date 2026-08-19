"""Capture real picker screens for the README artwork.

Nothing here draws a picker. Every frame is what Session Kit's own picker wrote
to a pty, driven through the login test fixture, so a picture can never promise
a screen the code does not produce. The fixture's inventory is neutral demo
data: no real session titles, account aliases, host names, or projects.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import html as html_mod
from pathlib import Path
import re
import sys
import time

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# The frames come from the real picker through the login fixture, so the
# repository has to be importable before this resolves.
from tests.test_login import LoginFixture, inventory, row, run_pty  # noqa: E402

CLEAR = "\x1b[2J\x1b[H"
# The prompt the needs-you screen reads on, and therefore the first thing the
# picker writes when it repaints the list behind it. The repaint lands in the
# same captured frame as the screen it replaces, and it does not start on a
# line of its own, so a line-anchored pattern never sees it.
REPAINT = "needs ❯"

SGR = re.compile(r"\x1b\[([0-9;]*)m")
# Cursor moves, bracketed-paste toggles and the rest carry no colour and would
# otherwise land in the text as stray characters.
OTHER_ESCAPES = re.compile(
    r"\x1b\[[0-9;?]*[a-ln-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
)

BASE_COLOURS = {
    30: "#484f58",
    31: "#f85149",
    32: "#3fb950",
    33: "#d29922",
    34: "#58a6ff",
    35: "#bc8cff",
    36: "#39c5cf",
    37: "#c9d1d9",
}
DEFAULT_INK = "#c9d1d9"


@dataclass(frozen=True)
class Style:
    colour: str = DEFAULT_INK
    bold: bool = False


def demo_row(
    session_id: str,
    number: int,
    provider: str,
    title: str,
    *,
    availability: str = "ready",
    status: str = "idle",
    needs_you: bool = False,
    blocking_question: bool = False,
    transcript_idle: bool = False,
    color: str,
    subagents: int = 0,
) -> dict:
    item = row(
        session_id,
        number=number,
        provider=provider,
        availability=availability,
        needs_you=needs_you,
    )
    item.update(
        {
            "account_alias": "personal" if provider == "claude" else "work",
            "model": "claude-opus-5" if provider == "claude" else "gpt-5-codex",
            "display_model": "Opus 5" if provider == "claude" else "GPT-5-Codex",
            "model_source": "transcript",
            "recent_output_at_unix_ms": int(
                (time.time() - 60 * (3 + number % 9)) * 1000
            ),
            "recent_output_age_seconds": 60 * (3 + number % 9),
            "title": title,
            "native_title": title,
            "display_title": title,
            "agent_status": status,
            "blocking_question": blocking_question,
            "transcript_idle": transcript_idle,
            "display_color": color,
            "subagents": [
                {"provider": "codex", "status": "idle"} for _ in range(subagents)
            ],
        }
    )
    return item


def demo_rows() -> list[dict]:
    """Seven neutral sessions that exercise every state word the README names."""

    return [
        demo_row(
            "demo53",
            53,
            "claude",
            "Review Release Notes",
            status="needs your reply",
            needs_you=True,
            blocking_question=True,
            color="purple",
        ),
        demo_row(
            "demo57", 57, "claude", "Fix Login Timeout", needs_you=True, color="cyan"
        ),
        demo_row(
            "demo59",
            59,
            "claude",
            "Plan Database Upgrade",
            needs_you=True,
            transcript_idle=True,
            color="green",
        ),
        demo_row(
            "demo60",
            60,
            "claude",
            "Document Release Process",
            status="working",
            color="orange",
        ),
        demo_row(
            "demo61",
            61,
            "codex",
            "Improve Session Picker",
            status="running",
            color="pink",
            subagents=2,
        ),
        demo_row(
            "demo62",
            62,
            "codex",
            "Draft API Guide",
            availability="attached",
            status="needs your reply",
            needs_you=True,
            color="blue",
        ),
        demo_row(
            "demo64",
            64,
            "codex",
            "Update Installation Docs",
            availability="attached",
            status="working",
            color="yellow",
        ),
    ]


def capture(
    keystrokes: bytes = b"q\n", *, lines: int = 30, columns: int = 110
) -> list[str]:
    """Every screen the real picker painted, in order, ANSI colour intact."""

    document = inventory(*demo_rows())
    # Five neutral display-only roots keep the More counter honest.
    document["outside_agents"] = [{"provider": "codex"} for _ in range(5)]
    fixture = LoginFixture(document)
    try:
        code, output = run_pty(
            fixture,
            keystrokes,
            lines=lines,
            columns=columns,
            env_updates={"SESSION_KIT_NO_COLOR": None, "NO_COLOR": None},
        )
    finally:
        fixture.close()
    if CLEAR not in output:
        raise RuntimeError(
            f"the real picker painted no screen (exit {code}); refusing to invent one"
        )
    return [part for part in output.split(CLEAR) if part.strip()]


def list_frame(frames: list[str]) -> str:
    """The last complete session-list screen, prompt line kept.

    The prompt is part of the picture on purpose: a terminal screenshot with no
    command line on it does not read as a terminal.

    Leaving the needs-you screen repaints the list into the frame that screen
    already occupies, so one captured frame can hold both. Take the half after
    the repaint, and require the command bar the list has and the needs-you
    screen does not, so neither screen can ever be mistaken for the other.
    """

    complete = []
    for part in frames:
        candidate = part.rsplit(REPAINT, 1)[-1]
        readable = plain(candidate)
        if "sessions" in readable and "kill" in readable and "quit" in readable:
            complete.append(candidate)
    if not complete:
        raise RuntimeError("the real picker produced no complete list screen")
    return complete[-1].rstrip("\n")


def plain(text: str) -> str:
    """The text a person reads, with every escape sequence removed."""

    return OTHER_ESCAPES.sub("", SGR.sub("", text))


def _apply(style: Style, codes: list[int]) -> Style:
    index = 0
    while index < len(codes):
        code = codes[index]
        if code == 0:
            style = Style()
        elif code == 1:
            style = replace(style, bold=True)
        elif code == 22:
            style = replace(style, bold=False)
        elif code == 39:
            style = replace(style, colour=DEFAULT_INK)
        elif code in BASE_COLOURS:
            style = replace(style, colour=BASE_COLOURS[code])
        elif code in (38, 48) and index + 1 < len(codes):
            if codes[index + 1] == 2 and index + 4 < len(codes):
                red, green, blue = codes[index + 2 : index + 5]
                if code == 38:
                    style = replace(style, colour=f"#{red:02x}{green:02x}{blue:02x}")
                index += 4
            elif codes[index + 1] == 5 and index + 2 < len(codes):
                index += 2
        index += 1
    return style


def runs(line: str) -> list[tuple[str, Style]]:
    """One line split into coloured runs, escapes consumed."""

    style = Style()
    out: list[tuple[str, Style]] = []
    position = 0
    for match in SGR.finditer(line):
        chunk = line[position : match.start()]
        if chunk:
            out.append((OTHER_ESCAPES.sub("", chunk), style))
        codes = [int(value) if value else 0 for value in match.group(1).split(";")]
        style = _apply(style, codes)
        position = match.end()
    tail = line[position:]
    if tail:
        out.append((OTHER_ESCAPES.sub("", tail), style))
    return [(text, style) for text, style in out if text]


def frame_html(frame: str) -> str:
    """A captured screen as coloured HTML, ready to drop into a terminal shell."""

    rendered = []
    for line in frame.splitlines():
        pieces = []
        for text, style in runs(line):
            weight = ";font-weight:700" if style.bold else ""
            pieces.append(
                f'<span style="color:{style.colour}{weight}">'
                f"{html_mod.escape(text)}</span>"
            )
        rendered.append("".join(pieces) or "&nbsp;")
    return "\n".join(rendered)


# --------------------------------------------------------------- needs you

# The state words that mean a person is being waited on. `idle` is one of them:
# it is a needs-you session whose transcript has stopped moving, not a fifth
# state. Keep this identical to the README's state table.
ATTENTION_WORDS = ("question", "needs you", "idle")


def needs_frame(frames: list[str]) -> str:
    """The needs-you screen alone, with the list repaint behind it removed."""

    found = [
        part for part in frames if "needs you:" in plain(part) and "back" in plain(part)
    ]
    if not found:
        raise RuntimeError("the real picker produced no needs-you screen")
    head = found[-1].split(REPAINT, 1)[0]
    if "needs you:" not in plain(head):
        raise RuntimeError("the needs-you screen was lost when the repaint was cut")
    # The prompt is the first thing the repaint overwrote, so putting it back is
    # restoring the screen rather than decorating it: a terminal picture with no
    # command line on it does not read as a terminal.
    return (head + REPAINT + " ").strip("\n")


def attention_total(needs_screen: str) -> int:
    """The number the needs-you screen puts at the top of itself."""

    match = re.search(r"needs you:\s*(\d+)", plain(needs_screen))
    if match is None:
        raise RuntimeError("the needs-you screen printed no total")
    return int(match.group(1))


def attention_rows(list_screen: str) -> list[str]:
    """Session rows on the list screen whose state word is an attention word."""

    rows = []
    for line in plain(list_screen).splitlines():
        if not re.match(r"\s+\d+\s{2}", line):
            continue
        fields = [field.strip() for field in line.split("|")]
        # Both screens number their rows the same way and both can carry a
        # bare state word, so shape is what separates them: a list row spends
        # a column each on provider, account, model, state and age, while a
        # needs-you row names only the provider and the wait. Without this a
        # frame holding both screens counts some sessions twice.
        if len(fields) < 5:
            continue
        if any(field in ATTENTION_WORDS for field in fields):
            rows.append(line.split()[0])
    return rows


def refuse_disagreeing_screens(list_screen: str, needs_screen: str) -> None:
    """The two screens must count the same sessions as waiting on a person.

    They are built from different evidence, the list prints the one state word
    `session_state()` derives, and the needs-you screen counts the rows whose
    raw `needs_you`/`blocking_question` flags are set, so they can disagree,
    and a release where they do must not be photographed for the front page.
    The first capture taken for this figure disagreed: an ordinary finished
    Codex turn reports `agent_status: idle` with `needs_you: false`, the list
    printed `needs you` for it because `STATE_WORDS` maps idle to that word,
    and the needs-you screen left it out. Two pictures on one page, one saying
    four sessions are waiting and the other three.
    """

    listed = attention_rows(list_screen)
    total = attention_total(needs_screen)
    numbered = re.findall(r"^\s+(\d+)\s", plain(needs_screen), re.MULTILINE)
    if total != len(listed) or sorted(numbered) != sorted(listed):
        raise SystemExit(
            "the two picker screens disagree about who is waiting:\n"
            f"  list screen says {len(listed)}: {', '.join(listed) or 'none'}\n"
            f"  needs-you screen says {total}: {', '.join(numbered) or 'none'}\n"
            "Shipping both pictures would put the contradiction on the front "
            "page. Fix the disagreement before regenerating the figures."
        )
