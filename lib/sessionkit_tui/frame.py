"""Turning what is true into the lines that get painted.

A frame is data: numbered lines, the styled runs inside them, and which line
the highlight is on. The curses shell paints frames and nothing else, so every
question about what a person sees is answered here, in a test, without a
terminal.

Columns are decided here too. Every session row puts its provider at the same
column, whatever its title is worth, because a title long enough to push its
own columns right turns a list into a ragged wall.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence
import unicodedata

from . import voice
from .rows import Row

BOLD = "bold"
DIM = "dim"
ATTENTION = "attention"
MARK = "mark"
NUMBER = "number"
PROVIDER = "provider"
TITLE = "title"
KEY = "key"

TICK = "✓"
ELLIPSIS = "…"

TITLE_LINE = "Session Kit"

# The fixed left edge of every session row: two columns of mark, three of
# number, two of gap.
MARK_WIDTH = 2
NUMBER_WIDTH = 3
GAP = 2
ROW_PREFIX = MARK_WIDTH + NUMBER_WIDTH + GAP

# A title never gets less than this many columns, whatever the details beside
# it want. There used to be a second floor here -- a third of the room,
# whatever the details needed -- and it is gone: it let the title push the
# state and the time off the end of the line. See `label_width`.
MIN_LABEL = 12
RESPONSIVE_MIN_LABEL = 4
PLACEHOLDER_CELL_FLOOR = 3

DETAIL_SEPARATOR = " | "
TITLE_STYLE_PREFIX = "title:"
PROVIDER_STYLE_PREFIX = "provider:"


@dataclass(frozen=True)
class Span:
    start: int
    length: int
    style: str


@dataclass(frozen=True)
class Line:
    text: str
    spans: tuple[Span, ...] = ()
    row_key: str | None = None


@dataclass(frozen=True)
class PanelItem:
    key: str
    label: str
    detail: str = ""
    style: str = ""


@dataclass(frozen=True)
class Panel:
    """The list of things that can happen to what is marked.

    It opens with the choice a person came for already highlighted, and Enter
    takes it. Nothing here asks a second time.
    """

    title: str
    items: tuple[PanelItem, ...]
    index: int = 0
    prompt: str | None = None
    entry: str = ""
    identity_color: str = ""
    subtitle: str = ""
    subtitle_style: str = DIM
    subtitle_provider: str = ""

    def moved(self, delta: int) -> "Panel":
        if not self.items:
            return self
        index = max(0, min(len(self.items) - 1, self.index + delta))
        return Panel(
            self.title, self.items, index, self.prompt, self.entry,
            self.identity_color, self.subtitle, self.subtitle_style,
            self.subtitle_provider,
        )

    @property
    def current(self) -> PanelItem | None:
        if not self.items or not 0 <= self.index < len(self.items):
            return None
        return self.items[self.index]


@dataclass(frozen=True)
class Screen:
    """Everything the frame builder needs, and nothing it does not."""

    rows: tuple[Row, ...] = ()
    cursor_index: int = 0
    input_text: str = ""
    input_is_filter: bool = False
    status: str = ""
    stale: bool = False
    heading: str = ""
    panel: Panel | None = None
    help_open: bool = False
    empty_note: str = ""
    session_total: int = 0
    ready_total: int = 0
    elsewhere_total: int = 0
    view_kind: str = "list"


@dataclass(frozen=True)
class Frame:
    lines: tuple[Line, ...] = ()
    cursor_line: int | None = None
    top: int = 0
    row_lines: dict[int, str] = field(default_factory=dict, repr=False)

    def text(self) -> tuple[str, ...]:
        return tuple(line.text for line in self.lines)

    def joined(self) -> str:
        return "\n".join(line.text for line in self.lines)


def _cells(text: str) -> int:
    """How many terminal columns this text occupies.

    Everything here is measured in columns, not characters, for one reason:
    `curses.chgat` positions a styled run by COLUMN. A row measured in
    characters puts both its columns and its colours in the wrong place the
    moment a name contains a wide character, and a session name is whatever
    the person typed. One CJK character is two columns and one `len()`.
    """
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )


def _pad(text: str, width: int) -> str:
    """`str.ljust`, in columns. See `_cells`."""
    return text + " " * max(0, width - _cells(text))


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if _cells(text) <= width:
        return text
    if width == 1:
        return ELLIPSIS
    kept: list[str] = []
    used = 0
    for character in text:
        size = _cells(character)
        if used + size > width - 1:
            break
        kept.append(character)
        used += size
    return "".join(kept) + ELLIPSIS


def _placeholder_floor_width(value: str) -> int | None:
    if value == voice.MISSING:
        return PLACEHOLDER_CELL_FLOOR
    return None


def _real_value_width(values: Sequence[str], *, default: int = 1) -> int:
    """Let real cells earn width; placeholders merely occupy what they earn."""

    real: list[int] = []
    placeholder_floors: list[int] = []
    for value in values:
        floor = _placeholder_floor_width(value)
        if floor is None:
            real.append(_cells(value))
        else:
            placeholder_floors.append(floor)
    if real:
        return max(real)
    if placeholder_floors:
        return max(placeholder_floors)
    return default


@dataclass(frozen=True)
class DetailWidths:
    provider: int = 1
    account: int = 1
    model: int = 1
    state: int = 1
    subagents: int = 0
    age: int = 1

    @property
    def fixed(self) -> int:
        fields = self.provider + self.account + self.model + self.state + self.age
        if self.subagents:
            fields += self.subagents + len(DETAIL_SEPARATOR)
        return fields + 15


def detail_widths(rows: Sequence[Row], *, compact_age: bool = False) -> DetailWidths:
    sessions = [row for row in rows if row.kind == "session"]
    return DetailWidths(
        max((_cells(row.provider) for row in sessions), default=1),
        _real_value_width([row.account for row in sessions]),
        _real_value_width([row.model for row in sessions]),
        max((_cells(row.state) for row in sessions), default=1),
        max((_cells(row.subagents) for row in sessions), default=0),
        max(
            (_cells(row.compact_age if compact_age else row.age) for row in sessions),
            default=1,
        ),
    )


def _responsive_session_layout(
    rows: Sequence[Row], width: int
) -> tuple[int, DetailWidths, bool, bool]:
    """Return title/detail widths, compact-time use, and status-only fallback."""

    sessions = [row for row in rows if row.kind == "session"]
    full = detail_widths(rows)
    compact = detail_widths(rows, compact_age=True)
    longest = max((_cells(row.label) for row in sessions), default=MIN_LABEL)

    def squeeze(details: DetailWidths) -> DetailWidths:
        account, model = details.account, details.model
        excess = ROW_PREFIX + MIN_LABEL + len(DETAIL_SEPARATOR) + details.fixed - width
        take = min(max(0, excess), max(0, model - 1))
        model -= take
        excess -= take
        take = min(max(0, excess), max(0, account - 1))
        account -= take
        return DetailWidths(
            details.provider,
            account,
            model,
            details.state,
            details.subagents,
            details.age,
        )

    # Use the long age only when it fits without taking cells back from a title
    # or another metadata field.  Preferring it as soon as it technically fit
    # made a one-column resize shorten every title on the page at once.
    full_available = width - ROW_PREFIX - len(DETAIL_SEPARATOR) - full.fixed
    if full_available >= longest:
        return longest, full, False, False

    fitted = squeeze(compact)
    available = width - ROW_PREFIX - len(DETAIL_SEPARATOR) - fitted.fixed
    responsive_floor = RESPONSIVE_MIN_LABEL if width >= 60 else MIN_LABEL
    if available >= responsive_floor:
        return min(longest, available), fitted, True, False

    # Below the full-column floor, keep the row's purpose in one detail cell:
    # state, nonzero subagent count, then time. At 50 columns the time may be
    # the one token omitted; waiting-on-you still comes first.
    return MIN_LABEL, compact, True, True


def label_width(rows: Sequence[Row], width: int) -> int:
    """The one title column every session row shares.

    Wide enough for the longest title that fits, and no wider: one very long
    title used to take the whole line and leave every other row's provider,
    model, and state cut off. The details get the room they ask for and the
    column is the same for every row either way.

    The "a third of the room" floor that used to sit in this ceiling is gone.
    It let the title claim a third of the line whatever the details needed, so
    on a narrow window the row was assembled wider than the terminal and the
    frame then hard-cut the tail — which is the state and the time, the two
    facts the row is for. A title that cannot fit beside them is truncated;
    they are not.
    """

    room = width - ROW_PREFIX
    if room <= MIN_LABEL:
        return max(1, room)
    longest_label = max((_cells(row.label) for row in rows if row.detail), default=0)
    details = detail_widths(rows)
    ceiling = max(MIN_LABEL, room - details.fixed)
    return max(MIN_LABEL, min(ceiling, longest_label))


def _word_fit(text: str, width: int) -> str:
    if _cells(text) <= width:
        return text
    if width <= 1:
        return ELLIPSIS if width == 1 else ""
    cut = _fit(text, width).removesuffix(ELLIPSIS).rstrip()
    whole, separator, _ = cut.rpartition(" ")
    return (whole if separator and whole else cut) + ELLIPSIS


def _row_text(
    row: Row, column: int, details: DetailWidths, width: int,
    *, compact_age: bool = False, status_only: bool = False,
) -> tuple[str, tuple[Span, ...]]:
    tick = f"{TICK} " if row.marked else "  "
    number = f"{row.number:>{NUMBER_WIDTH}}" if row.number is not None else " " * NUMBER_WIDTH
    spans = []
    if row.marked:
        spans.append(Span(0, 1, MARK))
    if row.number is not None:
        spans.append(Span(MARK_WIDTH, NUMBER_WIDTH, NUMBER))

    if not row.detail:
        text = f"{tick}{number}{' ' * GAP}{_fit(row.label, max(1, width - ROW_PREFIX))}"
        spans.append(Span(ROW_PREFIX, max(0, _cells(text) - ROW_PREFIX), DIM))
        return text, tuple(spans)

    # The title is cut to the shared column rather than allowed to push the
    # detail right: one ragged row makes the whole list unreadable.
    fitted_label = _fit(row.label, column)
    label = _pad(fitted_label, column)
    title_style = TITLE_STYLE_PREFIX + (row.session.display_color if row.session else "")
    spans.append(Span(ROW_PREFIX, _cells(fitted_label), title_style))
    detail_start = ROW_PREFIX + column + len(DETAIL_SEPARATOR)
    age = row.compact_age if compact_age else row.age
    if status_only:
        room = max(1, width - ROW_PREFIX - column - len(DETAIL_SEPARATOR))
        parts = [row.state]
        if row.subagents:
            parts.append(row.subagents)
        with_age = DETAIL_SEPARATOR.join((*parts, age)) if age else DETAIL_SEPARATOR.join(parts)
        without_age = DETAIL_SEPARATOR.join(parts)
        detail = with_age if _cells(with_age) <= room else without_age
        text = f"{tick}{number}{' ' * GAP}{label}{DETAIL_SEPARATOR}{_fit(detail, room)}"
        rest_start = detail_start
        spans.append(
            Span(
                rest_start,
                max(0, min(_cells(text), width) - rest_start),
                ATTENTION if row.needs_you else DIM,
            )
        )
        return text, tuple(spans)

    fixed_parts = [
        _pad(row.provider, details.provider),
        _pad(_fit(row.account, details.account), details.account),
        _pad(_fit(row.model, details.model), details.model),
        _pad(row.state, details.state),
    ]
    if details.subagents:
        fixed_parts.append(_pad(row.subagents, details.subagents))
    fixed_parts.append(_pad(age, details.age))
    fixed = DETAIL_SEPARATOR.join(
        fixed_parts
    )
    text = f"{tick}{number}{' ' * GAP}{label}{DETAIL_SEPARATOR}{fixed}"
    provider = row.session.provider.casefold() if row.session else "unknown"
    provider_style = PROVIDER_STYLE_PREFIX + (
        provider if provider in {"claude", "codex", "shell"} else "unknown"
    )
    spans.append(Span(detail_start, _cells(row.provider), provider_style))
    rest_start = detail_start + details.provider
    spans.append(
        Span(
            rest_start,
            max(0, min(_cells(text), width) - rest_start),
            ATTENTION if row.needs_you else DIM,
        )
    )
    return text, tuple(spans)


def _key_spans(text: str, markers: Sequence[tuple[str, str]], *, offset: int = 0) -> list[Span]:
    spans = []
    for phrase, token in markers:
        phrase_start = text.find(phrase)
        start = phrase_start + phrase.rfind(token) if phrase_start >= 0 else -1
        if start >= 0:
            spans.append(Span(offset + start, len(token), KEY))
    return spans


def _footer_line(width: int, footer: str = voice.FOOTER) -> Line:
    """The footer, with every key in it findable by eye."""

    text = _fit(f"  {footer}", width)
    spans = [Span(2, max(0, len(text) - 2), DIM)]
    markers: tuple[tuple[str, str], ...] = (
        ("↵ open", "↵"),
        ("↵ choose", "↵"),
        ("↵ actions", "↵"),
        ("↵ back", "↵"),
        ("↵ save", "↵"),
        ("open #", "#"),
        ("kill k #", "k #"),
        ("history h #", "h #"),
        ("new n", "n"),
        ("more m", "m"),
        ("help ?", "?"),
        ("b back", "b"),
        ("esc leave", "esc"),
        ("esc back", "esc"),
        ("ctrl-d leave", "ctrl-d"),
    )
    spans.extend(_key_spans(text, markers))
    return Line(text, tuple(spans))


def _panel_lines(panel: Panel, width: int) -> tuple[list[Line], int]:
    lines: list[Line] = []
    if panel.subtitle:
        text = _fit(f"  {panel.subtitle}", width)
        spans = [Span(2, len(panel.subtitle), panel.subtitle_style)]
        if panel.subtitle_provider:
            start = text.find("[")
            end = text.find("]", start + 1)
            if start >= 0 and end > start + 1:
                spans.append(
                    Span(
                        start + 1,
                        end - start - 1,
                        PROVIDER_STYLE_PREFIX + panel.subtitle_provider.casefold(),
                    )
                )
        lines.append(Line(text, tuple(spans)))
    cursor_line = len(lines)
    if panel.prompt is not None:
        prompt = panel.prompt
        text = _fit(f"  {prompt} {panel.entry}", width)
        spans = (Span(2, len(prompt), KEY),)
        lines.append(Line(text, spans))
        return lines, cursor_line
    label_width = max((_cells(item.label) for item in panel.items), default=1)
    for index, item in enumerate(panel.items):
        number = f"{index + 1:>2}"
        text = f"  {number}  {item.label}"
        spans: list[Span] = [Span(2, len(number), NUMBER)]
        if item.style:
            spans.append(Span(6, _cells(item.label), item.style))
        if item.detail:
            start = 6 + label_width + len(DETAIL_SEPARATOR)
            text = (
                f"  {number}  {_pad(item.label, label_width)}"
                f"{DETAIL_SEPARATOR}{item.detail}"
            )
            spans.append(Span(start, _cells(item.detail), DIM))
        if index == panel.index:
            cursor_line = len(lines)
        lines.append(Line(_fit(text, width), tuple(spans), row_key=f"panel:{item.key}"))
    return lines, cursor_line


def _help_lines(width: int) -> list[Line]:
    lines: list[Line] = []
    previous = ""
    for section, key, meaning in voice.HELP_ROWS:
        if section != previous:
            if lines:
                lines.append(Line(""))
            lines.append(Line(f"  {section}", (Span(2, len(section), BOLD),)))
            previous = section
        key_text = key.ljust(13)
        text = _fit(f"  {key_text} {meaning}", width)
        lines.append(Line(text, (Span(2, len(key), KEY), Span(16, max(0, len(text) - 16), DIM))))
    lines.append(Line(""))
    lines.extend(Line(_fit(f"  {note}", width), (Span(2, min(len(note), max(0, width - 2)), DIM),)) for note in voice.HELP_NOTES)
    return lines


def closed_widths(rows: Sequence[Row]) -> tuple[int, int]:
    """The provider and title columns a closed-session list shares.

    The live list has had column discipline for a long time; this one, the same
    shape one keypress away, was still writing each row at whatever width its
    own title happened to be.
    """
    closed = [row for row in rows if row.kind == "closed-session"]
    return (
        max((_cells(row.provider or voice.MISSING) for row in closed), default=1),
        max((_cells(row.label) for row in closed), default=1),
    )


def _closed_row_text(
    row: Row, width: int, columns: tuple[int, int] | None = None
) -> tuple[str, tuple[Span, ...]]:
    provider_width, label_column = columns or (
        _cells(row.provider or voice.MISSING),
        _cells(row.label),
    )
    number = f"{row.number:>2}" if row.number is not None else "  "
    provider = row.provider or voice.MISSING
    # The title column is capped so one very long name cannot push every other
    # row's age and state off the right edge.
    room = max(4, width - 8 - provider_width - 4)
    label_column = min(label_column, room)
    label = _pad(_fit(row.label, label_column), label_column)
    prefix = f"  {number}  ["
    text = f"{prefix}{_pad(provider, provider_width)}] {label}"
    if row.age:
        text += f" [{row.age}]"
    if row.state:
        text += f" — {row.state}"
    # Measured from the prefix rather than searched for: a provider word can
    # also occur inside a session's own title, and `index()` finds that first.
    provider_start = _cells(prefix)
    provider_key = row.closed.provider.casefold() if row.closed else "unknown"
    spans = [
        Span(2, len(number), NUMBER),
        Span(provider_start, _cells(provider), PROVIDER_STYLE_PREFIX + provider_key),
    ]
    suffix = provider_start + provider_width + 2 + label_column
    if row.age or row.state:
        spans.append(Span(suffix, max(0, _cells(text) - suffix), DIM))
    return _fit(text, width), tuple(spans)


def build_frame(screen: Screen, *, width: int = 80, height: int = 24, top: int = 0) -> Frame:
    """One frame, complete. Nothing about it depends on what was drawn last."""

    width = max(20, width)
    height = max(6, height)
    if screen.panel is not None:
        title = screen.panel.title
    elif screen.heading or screen.help_open:
        title = screen.heading or TITLE_LINE
    else:
        count = screen.session_total or sum(row.kind == "session" for row in screen.rows)
        ready = screen.ready_total or sum(
            row.kind == "session" and row.session is not None and not row.session.is_attached
            for row in screen.rows
        )
        elsewhere = screen.elsewhere_total or sum(
            row.kind == "session" and row.session is not None and row.session.is_attached
            for row in screen.rows
        )
        title = f"{count} {'session' if count == 1 else 'sessions'} · {ready} ready · {elsewhere} open elsewhere"
    title_style = (
        TITLE_STYLE_PREFIX + screen.panel.identity_color
        if screen.panel is not None and screen.panel.identity_color
        else BOLD
    )
    lines: list[Line] = [Line(_fit(f"  {title}", width), (Span(2, len(title), title_style),))]

    if screen.stale:
        note = "Showing the last confirmed list. Actions wait for a refresh."
        lines.append(Line(_fit(f"  {note}", width), (Span(2, len(note), DIM),)))
    tail: list[Line] = []
    if screen.input_text:
        prompt = "Filter:" if screen.input_is_filter else "Mark:"
        entry = f"  {prompt} {screen.input_text}"
        tail.append(Line(_fit(entry, width), (Span(2, len(prompt), KEY),)))
    if screen.status:
        status = _fit(f"  {screen.status}", width)
        folded = screen.status.casefold()
        style = (
            ATTENTION
            if any(word in folded for word in ("refused", "nothing changed", "there is no", "unavailable", "failed", "could not"))
            else MARK
        )
        tail.append(Line(status, (Span(2, max(0, len(status) - 2), style),)))
    footer = voice.FOOTER
    if screen.help_open:
        footer = voice.HELP_FOOTER
    elif screen.panel is not None:
        # A panel asking for typed text cannot offer `b` as a key: `b` is the
        # first letter of the name being typed.
        footer = (
            voice.NAME_PROMPT_FOOTER
            if screen.panel.prompt is not None
            else voice.BACK_FOOTER
        )
    elif screen.view_kind == "closed":
        footer = voice.CLOSED_FOOTER
    tail.append(_footer_line(width, footer))

    body_height = max(1, height - len(lines) - len(tail))
    cursor_line: int | None = None
    row_lines: dict[int, str] = {}

    if screen.help_open:
        body = _help_lines(width)
        lines.extend(body[:body_height])
    elif screen.panel is not None:
        body, panel_cursor = _panel_lines(screen.panel, width)
        body = body[:body_height]
        offset = len(lines)
        lines.extend(body)
        if panel_cursor < len(body):
            cursor_line = offset + panel_cursor
        for index, line in enumerate(body):
            if line.row_key:
                row_lines[offset + index] = line.row_key
    elif not screen.rows:
        note = screen.empty_note or voice.empty("Sessions")
        lines.append(Line(_fit(f"  {note}", width)))
    else:
        cursor = max(0, min(len(screen.rows) - 1, screen.cursor_index))
        top = max(0, min(top, max(0, len(screen.rows) - body_height)))
        if cursor < top:
            top = cursor
        elif cursor >= top + body_height:
            top = cursor - body_height + 1
        column, details, compact_age, status_only = _responsive_session_layout(
            screen.rows, width
        )
        closed_columns = closed_widths(screen.rows)

        def projected(start: int) -> list[tuple[Row | None, Line]]:
            result: list[tuple[Row | None, Line]] = []
            previous_group = ""
            for row in screen.rows[start:]:
                if row.kind == "closed-session":
                    text, spans = _closed_row_text(row, width, closed_columns)
                    result.append((row, Line(text, spans, row_key=row.key)))
                    if len(result) >= body_height:
                        break
                    continue
                if row.kind == "session" and row.session is not None:
                    group = "Open elsewhere" if row.session.is_attached else "Ready"
                    if group != previous_group and len(result) + 1 < body_height:
                        result.append((None, Line(f"  {group}", (Span(2, len(group), BOLD),))))
                    previous_group = group
                text, spans = _row_text(
                    row,
                    column,
                    details,
                    width,
                    compact_age=compact_age,
                    status_only=status_only,
                )
                result.append((row, Line(_fit(text, width), spans, row_key=row.key)))
                if len(result) >= body_height:
                    break
            return result[:body_height]

        window = projected(top)
        while cursor >= top and not any(
            row is not None and screen.rows[cursor].key == row.key for row, _ in window
        ):
            top += 1
            window = projected(top)
        for row, line in window:
            line_number = len(lines)
            lines.append(line)
            if row is not None:
                row_lines[line_number] = row.key
                if row.key == screen.rows[cursor].key:
                    cursor_line = line_number

    # The footer sits in the same place on every frame. A screen whose last
    # line moves as the list shortens reads as flicker even when nothing was
    # repainted twice.
    while len(lines) + len(tail) < height:
        lines.append(Line(""))
    lines.extend(tail)
    return Frame(tuple(lines), cursor_line, top, row_lines)
