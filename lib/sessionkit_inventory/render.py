"""Terminal width, semantic colour, and the dashboard, detail, and lookup views.

Not one word a person reads is written here. Every label, state word, group
heading, time phrase and refusal comes from `labels.py`, so this file decides
layout and colour only, and `sp list`, `sp detail` and the picker cannot drift
into three spellings of one idea. A literal shown to a person belongs in
`labels.py`; `tests/test_voice_labels.py` fails the day one appears below.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import unicodedata
from typing import Any, Callable, Iterable, Mapping

from . import labels
from .common import (
    _positive_int,
    clean_text,
    stall_threshold_seconds as _shared_stall_threshold_seconds,
)
from .model import classify_top_level_sessions, session_is_unavailable


DEFAULT_STALL_SECONDS = labels.STALL_DEFAULT_SECONDS

# One definition, in `labels`, beside the function that applies it.
MAX_PLAUSIBLE_AGE_SECONDS = labels.MAX_PLAUSIBLE_AGE_SECONDS

# One palette for every text dashboard.  The old picker and ``sp list`` call
# this module, so a provider or session colour cannot drift between them.
SESSION_PALETTE = {
    "red": "\033[38;2;237;93;93m",
    "blue": "\033[38;2;97;166;240m",
    "green": "\033[38;2;63;221;115m",
    "yellow": "\033[38;2;249;215;108m",
    "purple": "\033[38;2;173;115;239m",
    "orange": "\033[38;2;242;144;81m",
    "pink": "\033[38;2;240;113;177m",
    "cyan": "\033[38;2;64;216;209m",
    "lime": "\033[38;2;170;230;70m",
    "magenta": "\033[38;2;255;95;255m",
    "silver": "\033[38;2;205;210;220m",
    "sand": "\033[38;2;214;178;130m",
    "sky": "\033[38;2;150;205;255m",
    "sea": "\033[38;2;95;235;170m",
}

PROVIDER_BADGES = {
    labels.PROVIDER_CLAUDE: "CLD",
    labels.PROVIDER_CODEX: "CDX",
    labels.PROVIDER_SHELL: "SHL",
    labels.PROVIDER_UNKNOWN: "UNK",
}

PROVIDER_BADGE_COLORS = {
    labels.PROVIDER_CLAUDE: SESSION_PALETTE["yellow"],
    labels.PROVIDER_CODEX: SESSION_PALETTE["cyan"],
    labels.PROVIDER_SHELL: SESSION_PALETTE["green"],
    # Unknown used to reuse Claude yellow, visually claiming evidence the row
    # did not contain.  Silver is deliberately provider-neutral.
    labels.PROVIDER_UNKNOWN: SESSION_PALETTE["silver"],
}

ACCOUNT_CELL_MAX = 12
MODEL_CELL_MAX = 20

# The fewest cells a title may be squeezed into before the metadata columns
# start giving room back. Filling the model column cost every row real width,
# and on a narrow terminal that came straight out of the title: a row rendered
# as `…` names nothing and is worth less than the model beside it.
MIN_TITLE_CELLS = 12

# The dashboard's last resort is a recognisable title fragment beside the
# complete state/count/time group.  This lower floor is used only after the
# metadata columns have already yielded; it keeps the same row structure at
# 60 columns instead of changing the whole page at the next width.
RESPONSIVE_MIN_TITLE_CELLS = 4

# The floors the metadata columns keep once the state-and-time pair starts
# taking room back from them. Eight cells still shows `Opus 5`, `no model`, or
# a recognisable `GPT-5.6…`; three still shows a shortened alias. Below these a
# column stops being worth the space it costs.
MIN_MODEL_CELLS = 8
MIN_ACCOUNT_CELLS = 3

# A placeholder is visible copy, but it is not evidence that its column needs
# seven cells. Real values decide the width. When every value is a placeholder,
# three cells retain an identifiable ``pe…`` without letting absence become the
# widest fact on the page.
PLACEHOLDER_CELL_FLOOR = 3


def _placeholder_floor_width(value: str) -> int | None:
    """Width an all-placeholder account/model column may claim."""

    if value == labels.MISSING:
        return PLACEHOLDER_CELL_FLOOR
    return None


def _real_value_width(values: Iterable[str], *, default: int = 1) -> int:
    """Size one aligned column from real values, not placeholder copy."""

    real: list[int] = []
    placeholder_floors: list[int] = []
    for value in values:
        floor = _placeholder_floor_width(value)
        if floor is None:
            real.append(_display_width(value))
        else:
            placeholder_floors.append(floor)
    if real:
        return max(real)
    if placeholder_floors:
        return max(placeholder_floors)
    return default


def _yield_to_essential(
    account_width: int,
    model_width: int,
    shortfall: int,
    *,
    model_floor: int = MIN_MODEL_CELLS,
    account_floor: int = MIN_ACCOUNT_CELLS,
) -> tuple[int, int, int]:
    """Take room from the metadata columns to keep the state and the time.

    The order is what a person needs off a row when the window is small: whose
    turn it is and when it last moved, then the name, then which model, then
    which account. Returns the two widths and whatever shortfall is still
    unmet, at which point the row genuinely cannot carry the pair and it comes
    off every row on the page together.

    The floors are readable ones by default, and callers drop them to a single
    cell for the last squeeze: on a window too narrow to hold even the state
    word, a shortened model is worth less than the word that says whose turn
    it is.
    """
    if shortfall <= 0:
        return account_width, model_width, 0
    taken = min(shortfall, max(0, model_width - model_floor))
    model_width -= taken
    shortfall -= taken
    taken = min(shortfall, max(0, account_width - account_floor))
    return account_width - taken, model_width, shortfall - taken


def _yield_to_title(
    account_width: int, model_width: int, title_room: int
) -> tuple[int, int, int]:
    """Give metadata room back to the title when a row is cramped.

    Order matters and is a judgement about what a person reads first: the model
    gives up its room before the account does, and both stop at the floor where
    a shortened value still reads. They used to give down to a single cell,
    which rendered the model column as a bare `…`, a column that costs a cell
    and says nothing is worse than a narrow one.
    """
    needed = MIN_TITLE_CELLS - title_room
    if needed <= 0:
        return account_width, model_width, title_room
    from_model = min(needed, max(0, model_width - MIN_MODEL_CELLS))
    model_width -= from_model
    title_room += from_model
    needed -= from_model
    from_account = min(needed, max(0, account_width - MIN_ACCOUNT_CELLS))
    return account_width - from_account, model_width, title_room + from_account


def _budget_field_widths(
    raw_account_width: int, raw_model_width: int, field_budget: int
) -> tuple[int, int]:
    """Fit account and model cells into one terminal-width budget.

    Both text list surfaces use this allocation. Account gets at most one
    third of a constrained budget; model gets the remainder, and both keep at
    least one display cell so a missing or shortened value remains visible.
    """
    field_budget = max(2, field_budget)
    if raw_account_width + raw_model_width <= field_budget:
        return raw_account_width, raw_model_width
    account_width = min(raw_account_width, max(1, field_budget // 3))
    model_width = min(raw_model_width, max(1, field_budget - account_width))
    return account_width, model_width


def _provider_name(value: object) -> str:
    return labels.provider_name(value)


def _term(value: str) -> str:
    """The one word `sp list` prints for a collector's own status word.

    The full table: `sp list` and the picker say the same word for the
    same state ("working", never the collector's "running").
    """
    return labels.state_word(value)


def _where(row: Mapping[str, Any]) -> str:
    """Where the session can be opened, in the two words `sp list` uses."""
    return labels.where_word(row.get("availability"))


def _title_state(row: Mapping[str, Any]) -> str:
    """Whether the provider is already showing this session's title."""
    return labels.title_state(row.get("provider_title_state"))


def _worktree_branch(row: Mapping[str, Any]) -> str:
    """The branch a session is isolated on, as the collector recorded it."""
    worktree = row.get("worktree")
    if not isinstance(worktree, Mapping):
        return ""
    return clean_text(worktree.get("branch"), 128)


def _timestamp(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _relative_time(timestamp_ms: int, now_ms: int) -> str:
    return labels.relative_time(timestamp_ms, now_ms)


def _precise_local_time(timestamp_ms: int) -> str:
    return labels.precise_local_time(timestamp_ms)


def _time_detail(timestamp_ms: object, now_ms: int) -> str:
    exact = _timestamp(timestamp_ms)
    if exact is None:
        return ""
    return labels.exact_and_relative(exact, now_ms)


# Both row facts come from `labels`, which is the only copy of either. They are
# named here so this module reads the same as before; they are not a second
# implementation, which is what the two surfaces had and disagreed over.
_last_active_seconds = labels.last_active_seconds
_last_active = labels.row_last_active
_model_cell = labels.model_cell


def _format_age(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return labels.duration(seconds)


def _short_path(path: str, *, home_factory: Callable[[], Any]) -> str:
    home = str(home_factory())
    return (
        f"~{path[len(home) :]}"
        if path == home or path.startswith(home + os.sep)
        else path
    )


def _display_width(value: str) -> int:
    """Conservative terminal-cell width without adding a runtime dependency."""
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in value
    )


def _pad(text: str, width: int) -> str:
    """Pad to a number of terminal cells, not a number of characters.

    `str.ljust` counts characters. Every cell in these rows is truncated by
    display width and was then padded by `ljust`, so a six-character CJK name
    measured twelve cells wide, was accepted as fitting, and then had six more
    spaces appended, the row over-ran its column by exactly the width the
    wide characters added. One helper, used for every column.
    """
    return text + " " * max(0, width - _display_width(text))


def _truncate_cells(text: str, limit: int) -> str:
    """Terminal-cell-aware truncation that leaves the text alone otherwise.

    Deliberately does NOT clean: `clean_text` flattens runs of spaces, which
    is right for a value read out of a row and fatal for a line that has
    already been padded into columns, it collapses the padding and the
    columns with it.
    """
    if _display_width(text) <= limit:
        return text
    if limit <= 1:
        return labels.ELLIPSIS
    kept: list[str] = []
    used = 0
    for character in text:
        cells = _display_width(character)
        if used + cells > limit - 1:
            break
        kept.append(character)
        used += cells
    return f"{''.join(kept)}{labels.ELLIPSIS}"


def _display_title(value: Any, limit: int = 48) -> str:
    """Control-safe, terminal-cell-aware truncation for display only."""
    return _truncate_cells(clean_text(value, 10000), limit)


def _color_enabled() -> bool:
    return (
        sys.stdout.isatty()
        and not os.environ.get("NO_COLOR")
        and not os.environ.get("SESSION_KIT_NO_COLOR")
    )


def stall_threshold_seconds() -> int:
    """Silence after which a session stops being described as running.

    Deliberately far above any normal pause. Codex sessions report "running"
    for their whole life, working or waiting, so a short threshold would label
    every parked session and the warning would stop meaning anything. Override
    with SESSION_KIT_STALL_SECONDS.
    """
    return _shared_stall_threshold_seconds()


def render_inventory(
    inventory: Mapping[str, Any],
    rows_only: bool = False,
    *,
    color_enabled: Callable[[], bool],
    now_ms: int | None = None,
) -> str:
    """Render action-first/provider groups with sequential visible row numbers."""
    color = color_enabled()
    rendered_at = int(time.time() * 1000) if now_ms is None else now_ms
    bold = "\033[1m" if color else ""
    dim = "\033[2m" if color else ""
    cyan = "\033[36m" if color else ""
    green = "\033[32m" if color else ""
    yellow = "\033[33m" if color else ""
    reset = "\033[0m" if color else ""
    # Truecolor values equal to what Codex RENDERS for each sk-<color> theme
    # accent (measured from captured frames, Codex contrast-adjusts the raw
    # theme hex), so a session's name is pixel-identical here and in its own
    # Codex status bar. Remeasure if the theme anchors ever change.
    session_palette = SESSION_PALETTE if color else {}

    def tint(item: Mapping[str, Any], text: str) -> str:
        code = session_palette.get(item.get("display_color") or "")
        return f"{code}{text}{reset}" if code else text

    terminal_columns = shutil.get_terminal_size(fallback=(101, 24)).columns
    columns = _positive_int(
        os.environ.get("COLUMNS"),
        max(2, min(240, terminal_columns)),
        2,
        240,
    )
    width = max(1, columns - 1)
    lines: list[str] = []
    if inventory.get("stale"):
        lines.append(
            f"{yellow}  {labels.stale_warning(inventory.get('source'))}{reset}"
        )
    collected_sessions = [
        item for item in inventory.get("sessions", ()) if isinstance(item, Mapping)
    ]
    sessions, machine_sessions, _ = classify_top_level_sessions(collected_sessions)
    sessions = [item for item in sessions if not session_is_unavailable(item)]
    machine_sessions = [
        item for item in machine_sessions if not session_is_unavailable(item)
    ]
    has_terminal_numbers = any(
        isinstance(item, Mapping)
        and isinstance(item.get("terminal_number"), int)
        and not isinstance(item.get("terminal_number"), bool)
        and item.get("terminal_number", 0) > 0
        for item in sessions
    )
    ready_count = sum(
        1 for item in sessions if item.get("availability") == labels.AVAILABILITY_READY
    )
    elsewhere_count = sum(
        1
        for item in sessions
        if item.get("availability") == labels.AVAILABILITY_ATTACHED
    )
    if sessions:
        lines.append(
            f"  {labels.session_count(len(sessions), ready_count, elsewhere_count)}"
        )
    if sessions:
        lines.append("")
    number_width = max(
        2,
        max(
            (
                len(
                    str(
                        item.get("terminal_number")
                        or item.get("row")
                        or labels.UNNUMBERED
                    )
                )
                for item in sessions
            ),
            default=2,
        ),
    )
    field_budget = max(2, width - (8 + number_width + 3 + 12 + 8 + 4))
    raw_account_width = min(
        ACCOUNT_CELL_MAX,
        _real_value_width(
            (
                clean_text(item.get("account_alias"), 80) or labels.MISSING
                for item in sessions
            )
        ),
    )
    raw_model_width = min(
        MODEL_CELL_MAX,
        _real_value_width((_model_cell(item) for item in sessions)),
    )
    stall = stall_threshold_seconds()
    # The state column is padded to one width across the whole list so the time
    # phrase after it starts in the same place on every row.
    state_width = max(
        (
            _display_width(labels.session_state(item, stall_seconds=stall))
            for item in sessions
        ),
        default=0,
    )
    account_width, model_width = _budget_field_widths(
        raw_account_width, raw_model_width, field_budget
    )
    subagent_detail_reserve = max(
        4,
        max(
            (
                _display_width(
                    labels.subagent_detail(
                        int(
                            item.get(
                                "active_subagent_count",
                                len(item.get("subagents", ())),
                            )
                        )
                    )
                )
                + 1
                for item in sessions
                if int(
                    item.get("active_subagent_count", len(item.get("subagents", ())))
                )
                > 0
            ),
            default=4,
        ),
    )
    # The pair every row exists to carry -- whose turn it is, and when it last
    # did something -- is reserved before the title is sized, so a long session
    # name costs its own tail and never the column. `sp list` had the same
    # defect the picker did.
    essential_reserve = max(
        (
            _display_width(labels.session_state(item, stall_seconds=stall))
            + len(labels.SEPARATOR)
            + (
                len(labels.SEPARATOR)
                + _display_width(
                    labels.subagent_detail(
                        int(
                            item.get(
                                "active_subagent_count", len(item.get("subagents", ()))
                            )
                        )
                    )
                )
                if int(
                    item.get("active_subagent_count", len(item.get("subagents", ())))
                )
                > 0
                else 0
            )
            + len(labels.SEPARATOR)
            + _display_width(
                labels.row_last_active_compact(item, rendered_at)
                if width < 100
                else labels.row_last_active(item, rendered_at)
            )
            for item in sessions
        ),
        default=0,
    )
    title_room = width - (
        8
        + number_width
        + 3
        + account_width
        + model_width
        + 12
        + max(subagent_detail_reserve, essential_reserve)
    )
    account_width, model_width, title_room = _yield_to_title(
        account_width, model_width, title_room
    )
    # And when even the floor cannot carry the state-and-time pair, the
    # metadata columns give room back before the pair is dropped.
    account_width, model_width, _ = _yield_to_essential(
        account_width, model_width, MIN_TITLE_CELLS - title_room
    )
    title_room = width - (
        8
        + number_width
        + 3
        + account_width
        + model_width
        + 12
        + max(subagent_detail_reserve, essential_reserve)
    )
    # The title keeps its floor even when the reserved pair leaves less: below
    # about twelve cells a name stops naming anything, and `sp list` truncates
    # its details rather than rendering every row as a bare ellipsis.
    title_width = max(
        1,
        min(
            max(
                (
                    _display_width(clean_text(item.get("title"), 10000))
                    for item in sessions
                ),
                default=1,
            ),
            max(title_room, MIN_TITLE_CELLS),
        ),
    )
    for availability in labels.AVAILABILITY_ORDER:
        selected = [
            item for item in sessions if item.get("availability") == availability
        ]
        if not selected:
            continue
        lines.append(f"  {bold}{labels.group_heading(availability)}{reset}")
        for item in selected:
            provider = clean_text(
                item.get("display_provider") or item.get("provider"), 20
            ).casefold()
            if provider not in PROVIDER_BADGES:
                provider = labels.PROVIDER_UNKNOWN
            status = labels.session_state(item, stall_seconds=stall)
            agent_count = int(
                item.get("active_subagent_count", len(item.get("subagents", ())))
            )
            agent_detail = labels.subagent_detail(agent_count) if agent_count else ""
            age_detail = (
                labels.row_last_active_compact(item, rendered_at)
                if width < 100
                else _last_active(item, rendered_at)
            )
            status_parts = [
                _pad(status, state_width),
                *([agent_detail] if agent_detail else []),
                age_detail,
            ]
            branch = _worktree_branch(item)
            if branch:
                status_parts.append(labels.worktree_detail(branch))
            selector = item.get("terminal_number")
            if (
                isinstance(selector, bool)
                or not isinstance(selector, int)
                or selector <= 0
            ):
                selector = (
                    labels.UNNUMBERED if has_terminal_numbers else item.get("row")
                )
            selector_text = str(selector)
            account = _display_title(
                clean_text(item.get("account_alias"), 80) or labels.MISSING,
                account_width,
            )
            model = _display_title(_model_cell(item), model_width)
            badge = PROVIDER_BADGES[provider]
            title = _display_title(item.get("title"), title_width)
            # Padding is measured on the plain text and appended after the
            # coloured text: `_display_width` counts an escape sequence's bytes.
            title_padding = " " * max(0, title_width - _display_width(title))
            detail_color = (
                yellow
                if item.get("needs_you")
                or status
                in {
                    labels.QUESTION,
                    labels.WAITING_ON_YOU,
                    labels.SETUP_INCOMPLETE,
                }
                else dim
            )
            if width < 60:
                narrow_parts = [status, *([agent_detail] if agent_detail else [])]
                narrow_detail = labels.SEPARATOR.join(narrow_parts)
                narrow_prefix = f"      {selector_text:>{number_width}}  "
                narrow_title_room = max(
                    1,
                    width
                    - _display_width(narrow_prefix)
                    - len(labels.SEPARATOR)
                    - _display_width(narrow_detail),
                )
                narrow_title = _display_title(item.get("title"), narrow_title_room)
                lines.append(
                    f"{narrow_prefix}{tint(item, narrow_title)} | "
                    f"{detail_color}{narrow_detail}{reset}"
                )
                continue
            fixed = (
                8 + number_width + 3 + account_width + model_width + 12 + title_width
            )
            # Truncated, never re-cleaned: the state cell is padded so the time
            # phrase after it starts at one column on every row, and cleaning
            # would eat that padding.
            details = _truncate_cells(
                labels.SEPARATOR.join(status_parts), max(1, width - fixed)
            )
            # A person's turn is highlighted whatever put it there. This used to
            # name `provider exited` explicitly; that word no longer reaches the
            # screen (it translates to `needs you`), and testing the screen
            # word instead keeps an exited provider highlighted rather than
            # silently dimming it.
            lines.append(
                f"      {green}{selector_text:>{number_width}}{reset}  "
                f"{tint(item, title)}{title_padding} | {cyan}{badge}{reset} | "
                f"{_pad(account, account_width)} | {_pad(model, model_width)} | "
                f"{detail_color}{details}{reset}"
            )
    collapsed_count = len(machine_sessions)
    if collapsed_count:
        count = collapsed_count
        lines.append(f"  {count} {labels.plural(count, 'machine session')}")
    outside = list(inventory.get("outside_agents", ()))
    if outside:
        lines.append(f"  {bold}{labels.GROUP_OUTSIDE_THE_KIT}{reset}")
        for item in outside:
            agent_count = int(
                item.get("active_subagent_count", len(item.get("subagents", ())))
            )
            prefix = f"       {labels.UNNUMBERED}  "
            title = _display_title(
                item.get("title"), max(1, width - _display_width(prefix))
            )
            lines.append(f"{prefix}{tint(item, title)}")
            detail_parts = [
                _provider_name(item.get("provider")),
                _term(clean_text(item.get("agent_status"), 64) or labels.MISSING),
            ]
            if agent_count:
                detail_parts.append(labels.subagent_detail(agent_count))
            detail_prefix = "          "
            details = _display_title(
                labels.SEPARATOR.join(detail_parts),
                max(1, width - _display_width(detail_prefix)),
            )
            lines.append(f"{detail_prefix}{dim}{details}{reset}")
    if not sessions and not machine_sessions and not outside:
        lines.append(f"  {labels.SESSIONS_EMPTY}")
    if not rows_only:
        lines.append("")
        lines.extend(f"  {dim}{hint}{reset}" for hint in labels.LIST_HINTS)
    return "\n".join(lines)


def render_picker_page(
    inventory: Mapping[str, Any],
    *,
    page: int,
    page_size: int,
    style_enabled: bool,
    compact: bool,
    jump_number: int = 0,
    columns: int | None = None,
    now_ms: int | None = None,
) -> str:
    """Render one page of the key-driven picker from its projected snapshot.

    ``sp list`` and the picker intentionally have different framing, but the
    terminal layout, value rules, state words, ages, and colours now live in
    this one module.  The Bash picker supplies only navigation state.
    """

    bold = "\033[1m"
    green = "\033[32m"
    cyan = "\033[36m"
    yellow = "\033[33m"
    reset = "\033[0m"

    def styled(text: str, *codes: str) -> str:
        if not style_enabled or not text:
            return text
        return f"{''.join(codes)}{text}{reset}"

    def key(row: Mapping[str, Any]) -> str:
        provider = clean_text(
            row.get("display_provider") or row.get("provider"), 40
        ).casefold()
        return provider if provider in PROVIDER_BADGES else labels.PROVIDER_UNKNOWN

    def badge(row: Mapping[str, Any]) -> str:
        return PROVIDER_BADGES[key(row)]

    def field(row: Mapping[str, Any], name: str, limit: int) -> str:
        value = clean_text(row.get(name), limit)
        return value or labels.MISSING

    def shortened(value: object, room: int) -> str:
        return _display_title(value, max(1, room))

    def aligned_shortened(value: str, room: int) -> str:
        """Truncate without cleaning, so column padding survives."""
        return _truncate_cells(value, room)

    rows = [row for row in inventory.get("sessions", ()) if isinstance(row, Mapping)]
    picker = inventory.get("_picker")
    picker = picker if isinstance(picker, Mapping) else {}
    machine_count = picker.get("machine_count", 0)
    machine_needs = picker.get("machine_needs_you", 0)
    machine_expanded = picker.get("machine_expanded") is True
    machine_expandable = picker.get("machine_expandable_count", machine_count)
    source_total = picker.get("source_total")
    if isinstance(source_total, bool) or not isinstance(source_total, int):
        source_total = len(rows)
    query = clean_text(picker.get("query"), 4096)
    ready = sum(row.get("availability") == labels.AVAILABILITY_READY for row in rows)
    attached = sum(
        row.get("availability") == labels.AVAILABILITY_ATTACHED for row in rows
    )
    page_size = max(1, page_size)
    pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    first = (page - 1) * page_size
    selected = rows[first : first + page_size]
    terminal_columns = columns or shutil.get_terminal_size(fallback=(100, 24)).columns
    width = max(1, min(239, terminal_columns - 1))
    rendered_at = int(time.time() * 1000) if now_ms is None else now_ms
    stall = stall_threshold_seconds()
    lines: list[str] = []

    if query:
        match_word = labels.plural(len(rows), "match", "es")
        session_word = labels.plural(source_total, labels.SESSION)
        summary = (
            f"{len(rows)} {match_word} of {source_total} {session_word} · "
            f"{ready} {labels.WHERE_READY} · "
            f"{attached} {labels.WHERE_OPEN_ELSEWHERE}"
        )
        lines.append(f"  {styled(shortened(summary, width - 2), bold)}")
        shown_query = (
            "one exact conversation"
            if __import__("re").fullmatch(
                r"(?:claude:|codex:)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                query.strip(),
            )
            else query
        )
        search = shortened(shown_query, width - len("  Search: "))
        lines.append(f"  {styled('Search:', bold)} {search}")
    elif rows:
        session_word = labels.plural(len(rows), labels.SESSION)
        summary = (
            f"{len(rows)} {session_word} · {ready} {labels.WHERE_READY} · "
            f"{attached} {labels.WHERE_OPEN_ELSEWHERE}"
        )
        lines.append(f"  {styled(shortened(summary, width - 2), bold)}")
    elif not isinstance(machine_count, int) or machine_count <= 0:
        lines.append(f"  {labels.SESSIONS_EMPTY}")

    if inventory.get("stale"):
        lines.append(
            "  "
            + styled(
                shortened(
                    "Showing a cached list. Actions are off until it refreshes.",
                    width - 2,
                ),
                bold,
                yellow,
            )
        )
    if query and not selected:
        lines.append(f"  {labels.NO_MATCHES}")

    prepared: list[dict[str, Any]] = []
    for row in selected:
        number = row.get("terminal_number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            number = labels.UNNUMBERED
        state = labels.session_state(row, stall_seconds=stall)
        projected_now = _timestamp(row.get("_picker_time_as_of_unix_ms"))
        as_of = rendered_at if projected_now is None else projected_now
        # One time, one phrasing, on every row. Rows used to choose between
        # three shapes, `needs you 3 hr`, `12 min ago`, `opened 3 hr
        # ago`, depending on what each row happened to know, so no two rows
        # measured the same thing and nothing lined up.
        age_detail = _last_active(row, as_of)
        compact_age = labels.row_last_active_compact(row, as_of)
        name_state = clean_text(row.get("automatic_name_state"), 20).casefold()
        agents = int(row.get("active_subagent_count", len(row.get("subagents") or ())))
        agent_detail = ""
        if agents:
            agent_detail = labels.subagent_detail(agents)
        details = [state, *([agent_detail] if agent_detail else []), age_detail]
        if name_state in {labels.TITLE_STATE_PENDING, "failed"}:
            details.append(f"name {name_state}")
        branch = _worktree_branch(row)
        if branch:
            details.append(labels.worktree_detail(branch))
        # Narrowing drops the extras before it drops the time: state and "when
        # did this last do something" are the two facts the row exists for.
        compact_details = [
            state,
            *([agent_detail] if agent_detail else []),
            compact_age,
        ]
        prepared.append(
            {
                "row": row,
                "number": number,
                "title": clean_text(row.get("display_title") or row.get("title"), 120),
                "provider": badge(row),
                "account": shortened(field(row, "account_alias", 80), ACCOUNT_CELL_MAX),
                "model": shortened(_model_cell(row), MODEL_CELL_MAX),
                "state": state,
                "subagents": agent_detail,
                "compact_age": compact_age,
                "details": details,
                "compact": compact_details,
                # The pair the row exists to carry: whose turn it is, and when
                # it last did something. Narrowing takes the extras first and
                # this last -- and the title yields before this does.
                "essential": compact_details,
                "status": [state, *([agent_detail] if agent_detail else [])],
                "warning": state
                in {
                    labels.QUESTION,
                    labels.WAITING_ON_YOU,
                    labels.SETUP_INCOMPLETE,
                }
                or name_state == "failed",
            }
        )

    if prepared:
        number_width = max(3, max(len(str(item["number"])) for item in prepared))
        provider_width = max(_display_width(item["provider"]) for item in prepared)
        raw_account_width = _real_value_width((item["account"] for item in prepared))
        raw_model_width = _real_value_width((item["model"] for item in prepared))
        prefix_width = 4 + number_width + 2
        # The same account/model allocator as ``sp list``. Preserve room for
        # an eight-cell title and four cells of state before dividing what is
        # left between the two metadata columns.
        field_budget = max(
            2,
            width - prefix_width - provider_width - 12 - 8 - 4,
        )
        account_width, model_width = _budget_field_widths(
            raw_account_width, raw_model_width, field_budget
        )
        account_width, model_width, available_width = _yield_to_title(
            account_width,
            model_width,
            width - prefix_width - provider_width - account_width - model_width - 12,
        )
        available_width = max(1, available_width)
        ideal_title = max(_display_width(item["title"]) for item in prepared)
        # Measured on the state word alone, so the time phrase after it begins
        # at one column down the whole page.
        state_width = max(_display_width(item["state"]) for item in prepared)

        def details_text(parts: list[str]) -> str:
            if not parts:
                return ""
            primary = parts[0]
            if len(parts) > 1:
                primary = _pad(primary, state_width)
            return labels.SEPARATOR.join((primary, *parts[1:]))

        def widest(kind: str) -> int:
            return max(
                (_display_width(details_text(item[kind])) for item in prepared),
                default=0,
            )

        # The state word AND the one time, which is the pair the row exists to
        # carry. The title yields to protect it.
        #
        # This was the review's blocking finding, and it deleted the time from
        # every row at the width the operator actually works at. `label_width`
        # was sized against the SHORTEST detail form, the state word alone,
        # so the title grew until only that fit; the page-wide chooser then
        # found the real details would not fit the room the title had just
        # taken and fell all the way back to state-only, which carries no time.
        # It was a one-character cliff: at 120 columns a 40-character session
        # name kept the column and a 41-character one deleted it from the whole
        # page. Sizing the title against the pair instead makes a long name cost
        # its own tail, which is what a name should cost.
        essential = widest("essential")
        title_ceiling = available_width - 3 - essential
        label_width = max(MIN_TITLE_CELLS, min(ideal_title, title_ceiling))

        def room_for(label: int) -> int:
            return max(
                1,
                width
                - prefix_width
                - label
                - provider_width
                - account_width
                - model_width
                - 12,
            )

        # With the title already at its floor, the metadata columns give up
        # room next. A shortened model still reads; a missing time does not.
        account_width, model_width, _ = _yield_to_essential(
            account_width, model_width, essential - room_for(label_width)
        )
        # Last squeeze: a window so narrow that even the state word would be
        # cut takes the metadata columns down to a single cell first. `wor…`
        # tells a person nothing; `working` at least says whose turn it is.
        if room_for(label_width) < essential:
            account_width, model_width, _ = _yield_to_essential(
                account_width,
                model_width,
                essential - room_for(label_width),
                model_floor=1,
                account_floor=1,
            )
        # If the metadata is already at its one-cell floor, take the final few
        # cells from the title before changing to a different row shape.  Four
        # cells retain a recognisable title fragment in the 60-column estate.
        remaining = essential - room_for(label_width)
        if remaining > 0:
            responsive_floor = (
                RESPONSIVE_MIN_TITLE_CELLS if width >= 59 else MIN_TITLE_CELLS
            )
            label_width = max(responsive_floor, label_width - remaining)
        detail_room = room_for(label_width)
        status_only = widest("essential") > detail_room

        if status_only:
            # At the status-width floor, account/model/provider columns yield
            # as a unit. This preserves the full state and every nonzero
            # provider count. From 60 columns up, an identifiable compact time
            # follows them; at the narrowest tier waiting-on-you wins first.
            status_with_age = widest("essential")
            status_kind = "essential"
            status_room = width - prefix_width - 3 - 4
            if status_with_age > status_room:
                status_kind = "status"
                status_room = max(1, width - prefix_width - 3 - 4)
            chosen_width = widest(status_kind)
            label_width = max(
                1,
                min(
                    ideal_title,
                    MIN_TITLE_CELLS,
                    width - prefix_width - 3 - chosen_width,
                ),
            )
            detail_room = max(1, width - prefix_width - label_width - 3)

        # Which form of the details every row shows, decided ONCE for the page.
        # It used to be decided per row against the same room, so at one width
        # a row saying `last active just now` kept its time while the row above
        # it saying `last active 3h 9m ago`, one cell longer, lost it. That
        # is the ragged column the operator was reading: not a column at all,
        # just whatever each row happened to fit.
        # `essential` is the LAST form, not `status`. A window too narrow for
        # the whole pair gets it truncated rather than deleted, which is what
        # `sp list` has always done, and the two surfaces gave different answers
        # at the same width while this ladder ended in a state word alone.
        forms = (
            ["compact", "essential"]
            if compact
            else [
                "details",
                "compact",
                "essential",
            ]
        )
        detail_form = forms[-1]
        for candidate in forms:
            if widest(candidate) <= detail_room:
                detail_form = candidate
                break

        previous_group: str | None = None
        for item in prepared:
            row = item["row"]
            group = clean_text(
                row.get("_picker_group_label"), 80
            ) or labels.group_heading(clean_text(row.get("availability"), 20))
            if not compact and group != previous_group:
                lines.append(f"  {styled(group, bold)}")
                previous_group = group
            title = shortened(item["title"], label_width)
            # Measured before colouring: `_display_width` counts escape bytes.
            padding = " " * max(0, label_width - _display_width(title))
            color = SESSION_PALETTE.get(clean_text(row.get("display_color"), 20))
            title = styled(title, color) if color else title
            title += padding
            if status_only:
                detail_line = aligned_shortened(
                    details_text(item[status_kind]), detail_room
                )
                if item["warning"]:
                    detail_line = styled(detail_line, bold, yellow)
                number_text = f"{item['number']:>{number_width}}"
                marker = "  ▸ " if item["number"] == jump_number else "    "
                lines.append(
                    f"{marker}{styled(number_text, bold, green)}  {title} | {detail_line}"
                )
                continue
            detail_line = aligned_shortened(
                details_text(item[detail_form]), detail_room
            )
            if item["warning"]:
                detail_line = styled(detail_line, bold, yellow)
            number_text = f"{item['number']:>{number_width}}"
            marker = "  ▸ " if item["number"] == jump_number else "    "
            provider_key = key(row)
            provider = styled(
                item["provider"], bold, PROVIDER_BADGE_COLORS[provider_key]
            )
            account = _pad(
                aligned_shortened(item["account"], account_width), account_width
            )
            model = _pad(aligned_shortened(item["model"], model_width), model_width)
            lines.append(
                f"{marker}{styled(number_text, bold, green)}  {title} | "
                f"{provider} | {account} | {model} | {detail_line}"
            )

    if (
        page == 1
        and isinstance(machine_count, int)
        and not isinstance(machine_count, bool)
        and machine_count > 0
    ):
        machine_line = (
            f"{machine_count} {labels.plural(machine_count, 'machine session')}"
        )
        if isinstance(machine_needs, int) and machine_needs > 0:
            machine_line += f" · {machine_needs} {labels.NEEDS_YOU}"
        if (
            isinstance(machine_expandable, int)
            and not isinstance(machine_expandable, bool)
            and machine_expandable > 0
        ):
            machine_line += " · x " + ("hide" if machine_expanded else "show")
        lines.append(f"  {styled(shortened(machine_line, width - 2), bold)}")

    if pages > 1:
        directions = []
        if page > 1:
            directions.append("prev")
        if page < pages:
            directions.append("next")
        direction_text = " | ".join(styled(value, bold, cyan) for value in directions)
        lines.append(f"  {styled(f'Page {page}/{pages}', bold)} | {direction_text}")
    return "\n".join(lines)


def render_detail(
    inventory: Mapping[str, Any],
    selector: str,
    *,
    home_factory: Callable[[], Any],
    activity: Mapping[str, Any] | None = None,
    now_ms: int | None = None,
) -> str:
    """One session in full, for a person.

    `lookup` returns the row itself and is a machine mode: it carries the
    shpool id, the conversation UUID, PIDs and start ticks. This is what `sp
    detail` prints instead: everything a person can act on, and no identifier
    they could paste anywhere.
    """
    row = lookup(inventory, selector)
    if row is None:
        return labels.NO_MATCHING_SESSION
    identity = row.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    subagents = row.get("subagents")
    subagents = subagents if isinstance(subagents, list) else []
    aged_children = row.get("aged_children")
    aged_children = aged_children if isinstance(aged_children, list) else []
    activity = activity if isinstance(activity, Mapping) else {}
    projected_now = _timestamp(now_ms)
    as_of = projected_now if projected_now is not None else int(time.time() * 1000)
    status = labels.session_state(row, stall_seconds=stall_threshold_seconds())
    is_waiting = status in {
        labels.QUESTION,
        labels.WAITING_ON_YOU,
        labels.IDLE,
    }
    number = row.get("terminal_number")
    child_fields: list[tuple[str, str]] = []
    for child in aged_children:
        if not isinstance(child, Mapping):
            continue
        age = child.get("age_seconds")
        if isinstance(age, bool) or not isinstance(age, int) or age < 60 * 60:
            continue
        title = clean_text(child.get("title"), 120)
        kind = clean_text(child.get("kind"), 20).casefold()
        if not title or kind not in {
            labels.PROVIDER_SHELL,
            labels.CHILD_KIND_WORKER,
        }:
            continue
        child_fields.append(
            (
                labels.DETAIL_CHILD_SHELL
                if kind == labels.PROVIDER_SHELL
                else labels.DETAIL_CHILD_WORKER,
                f"{title}{labels.SEPARATOR}{labels.duration(age)}",
            )
        )
    fields: list[tuple[str, str]] = [
        (
            labels.DETAIL_SESSION,
            str(number)
            if isinstance(number, int) and not isinstance(number, bool) and number > 0
            else labels.NOT_NUMBERED,
        ),
        (
            labels.DETAIL_TITLE,
            clean_text(row.get("display_title") or row.get("title"), 120),
        ),
        (
            labels.DETAIL_PROVIDER,
            _provider_name(row.get("display_provider") or row.get("provider")),
        ),
        (labels.DETAIL_ACCOUNT_ALIAS, clean_text(row.get("account_alias"), 20)),
        (labels.DETAIL_ACCOUNT_EMAIL, clean_text(row.get("account_email"), 254)),
        (labels.DETAIL_ACCOUNT_PLAN, clean_text(row.get("account_plan"), 80)),
        (labels.DETAIL_MODEL, _model_cell(row)),
        (labels.DETAIL_STATE, status),
        (labels.DETAIL_WHERE, _where(row)),
        (
            labels.DETAIL_LAST_RESPONSE,
            _time_detail(activity.get("last_response_at_unix_ms"), as_of),
        ),
        (
            labels.DETAIL_WAITING_SINCE,
            _time_detail(
                activity.get("waiting_since_unix_ms") if is_waiting else None,
                as_of,
            ),
        ),
        (labels.DETAIL_OPENED, _time_detail(row.get("started_at_unix_ms"), as_of)),
        # The list rows carry one time each; the process age lives here, where
        # nothing else is competing for the line.
        (labels.DETAIL_PROCESS_AGE, _format_age(row.get("process_age_seconds"))),
        (
            labels.DETAIL_PROJECT,
            _short_path(clean_text(row.get("cwd"), 4096), home_factory=home_factory),
        ),
        (labels.DETAIL_WORKTREE, _worktree_branch(row)),
        (
            labels.DETAIL_COLOR,
            clean_text(row.get("display_color") or row.get("color"), 40),
        ),
        (
            labels.DETAIL_CONVERSATION,
            labels.CONVERSATION_EXACT
            if identity.get("confidence") == labels.CONFIDENCE_EXACT
            else labels.CONVERSATION_INEXACT,
        ),
        (
            labels.DETAIL_SUBAGENTS,
            str(len(subagents)) if subagents else labels.NONE,
        ),
        *child_fields,
        (labels.DETAIL_TITLE_STATE, _title_state(row)),
    ]
    lines = [f"  {labels.DETAIL_HEADING}"]
    width = max(len(label) for label, _ in fields)
    for label, value in fields:
        if value:
            lines.append(f"  {label.ljust(width)}  {value}")
    return "\n".join(lines) + "\n"


def lookup(inventory: Mapping[str, Any], selector: str) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    if selector.isdigit():
        number = int(selector)
        sessions = list(inventory.get("sessions", ()))
        if any(
            isinstance(item.get("terminal_number"), int)
            and not isinstance(item.get("terminal_number"), bool)
            and item.get("terminal_number", 0) > 0
            for item in sessions
            if isinstance(item, Mapping)
        ):
            matches = [
                item for item in sessions if item.get("terminal_number") == number
            ]
        else:
            matches = [item for item in sessions if item.get("row") == number]
    else:
        matches = [
            item
            for item in inventory.get("sessions", ())
            if (item.get("shpool_id_raw") or item.get("shpool_id")) == selector
        ]
    return matches[0] if len(matches) == 1 else None
