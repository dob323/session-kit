"""The screen's answers to keys, clicks, and the clock.

Everything a person can do arrives here as an event and leaves as either a new
state or a command to run. No curses, no subprocess, no clock of its own: the
caller supplies the time, so a test can hold the list still for three seconds
without waiting three seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import time
from typing import Callable, Iterable, Mapping, Sequence

from . import actions, rows as rowmod, voice
from .actions import Plan
from .frame import Frame, Panel, PanelItem, Screen, build_frame
from .model import ClosedSession, Inventory, Session, format_age, stall_seconds

LIST = "list"
CLOSED = "closed"
HELP = "help"

PROVIDERS = (("claude", "Claude"), ("codex", "Codex"), ("shell", "shell"))

HERE = " here"


@dataclass(frozen=True)
class Batch:
    """One Enter, one sentence. A batch that half-runs still says what it did."""

    plans: tuple[Plan, ...]
    success: str = ""
    failure: str = ""
    # The session this Enter puts in front of the person. It counts as seen
    # once the open actually worked — a refused open showed them nothing.
    seen_key: str = ""

    def __bool__(self) -> bool:
        return bool(self.plans)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Picker:
    """The whole screen, as state plus the events that change it."""

    inventory: Inventory = field(default_factory=Inventory)
    closed: tuple[ClosedSession, ...] = ()
    projects: tuple[tuple[str, str], ...] = ()
    accounts: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    # Where the two lists come from when they are not passed in whole: the
    # runner asks the release for them the moment a person opens the panel, so
    # a screen that never opens it never pays for the answer.
    account_source: Callable[[str], tuple[str, ...]] | None = None
    model_source: Callable[[str], tuple[str, ...]] | None = None
    self_session_id: str | None = None
    clock: Callable[[], int] = _now_ms
    stall: int | None = None

    view: str = LIST
    input_text: str = ""
    cursor_key: str | None = None
    cursor_fallback: int = 0
    machine_expanded: bool = False
    panel: Panel | None = None
    status: str = ""
    top: int = 0
    pinned: tuple[str, ...] = ()
    cursor_moved_at_ms: int = 0
    seen_at: dict[str, int] = field(default_factory=dict)
    panel_targets: tuple[str, ...] = ()
    panel_kind: str = ""
    pending_provider: str = ""
    pending_project: str = ""
    body_height: int = 10
    # Set the moment a person asks to leave. The shell reads it and returns
    # the deliberate-leave status; nothing else in here acts on it.
    leaving: bool = False

    def __post_init__(self) -> None:
        if self.stall is None:
            self.stall = stall_seconds()

    # ---- what is true right now ------------------------------------------

    def seen_keys(self) -> frozenset[str]:
        """Sessions a person has already looked at.

        The session attached in this window is seen by definition. One a
        person opened from here stays seen until it says something new — that
        is what makes the attention count trustworthy instead of merely large.
        """

        now = self.clock()
        keys = set()
        for session in self.inventory.sessions:
            if self.self_session_id and session.shpool_id == self.self_session_id:
                keys.add(session.key)
                continue
            at = self.seen_at.get(session.key)
            if at is None:
                continue
            if session.quiet_seconds is None:
                keys.add(session.key)
                continue
            if at >= now - session.quiet_seconds * 1000:
                keys.add(session.key)
        return frozenset(keys)

    @property
    def is_filter(self) -> bool:
        return rowmod.is_filter(self.input_text)

    @property
    def marks(self) -> tuple[int, ...]:
        if not self.input_text or self.is_filter:
            return ()
        numbers, _ = rowmod.parse_marks(self.input_text)
        on_screen = {row.number for row in self._session_rows() if row.number is not None}
        return tuple(number for number in numbers if number in on_screen)

    def _active_pinned(self) -> tuple[str, ...]:
        if not self.pinned:
            return ()
        if self.clock() - self.cursor_moved_at_ms >= rowmod.SORT_HOLD_MS:
            return ()
        return self.pinned

    def _session_rows(self) -> tuple[rowmod.Row, ...]:
        numbers: tuple[int, ...] = ()
        if not self.is_filter:
            numbers, _ = rowmod.parse_marks(self.input_text)
        return rowmod.build_rows(
            self.inventory.sessions,
            query=self.input_text if self.is_filter else "",
            marks=numbers,
            seen=self.seen_keys(),
            pinned=self._active_pinned(),
            machine_expanded=self.machine_expanded,
            stall=self.stall,
            age_text=format_age,
            now_ms=self.clock(),
        )

    def visible_rows(self) -> tuple[rowmod.Row, ...]:
        if self.view == CLOSED:
            return rowmod.build_closed_rows(
                self.closed, query=self.input_text if self.is_filter else ""
            )
        if self.view == HELP:
            return ()
        return self._session_rows()

    def cursor_index(self) -> int:
        visible = self.visible_rows()
        if not visible:
            return 0
        if self.cursor_key is not None:
            for index, row in enumerate(visible):
                if row.key == self.cursor_key:
                    return index
        return max(0, min(len(visible) - 1, self.cursor_fallback))

    def current_row(self) -> rowmod.Row | None:
        visible = self.visible_rows()
        if not visible:
            return None
        return visible[self.cursor_index()]

    def screen(self) -> Screen:
        visible = self.visible_rows()
        heading = ""
        empty_note = voice.empty("Sessions")
        if self.view == CLOSED:
            heading = voice.CLOSED_SESSIONS
            empty_note = voice.empty(voice.CLOSED_SESSIONS)
        elif self.view == HELP:
            heading = voice.PICKER_HELP
        query = self.input_text if self.is_filter else ""
        matching = tuple(
            session
            for session in self.inventory.sessions
            if not query or rowmod.matches(query.casefold(), session.haystack())
        )
        return Screen(
            rows=visible,
            cursor_index=self.cursor_index(),
            input_text="" if self.panel is not None else self.input_text,
            input_is_filter=self.is_filter,
            status=self.status,
            stale=not self.inventory.live,
            heading=heading,
            panel=self.panel,
            help_open=self.view == HELP,
            empty_note=empty_note,
            session_total=len(matching) if self.view == LIST else 0,
            ready_total=sum(not session.is_attached for session in matching) if self.view == LIST else 0,
            elsewhere_total=sum(session.is_attached for session in matching) if self.view == LIST else 0,
            view_kind=self.view,
        )

    def frame(self, *, width: int = 80, height: int = 24) -> Frame:
        drawn = build_frame(self.screen(), width=width, height=height, top=self.top)
        self.top = drawn.top
        if drawn.row_lines:
            self.body_height = max(1, len(drawn.row_lines))
        return drawn

    # ---- events ----------------------------------------------------------

    def set_inventory(self, inventory: Inventory) -> None:
        """A refresh replaces the facts and never the place a person is in."""

        self.inventory = inventory
        if self.cursor_key is not None and self.view == LIST:
            self.cursor_fallback = self.cursor_index()

    def set_closed(self, closed: Sequence[ClosedSession]) -> None:
        self.closed = tuple(closed)

    def move(self, delta: int) -> None:
        if self.panel is not None:
            self.panel = self.panel.moved(delta)
            return
        visible = self.visible_rows()
        if not visible:
            return
        index = max(0, min(len(visible) - 1, self.cursor_index() + delta))
        self.cursor_key = visible[index].key
        self.cursor_fallback = index
        self.pinned = tuple(row.key for row in visible if row.kind == rowmod.SESSION)
        self.cursor_moved_at_ms = self.clock()

    def wheel(self, delta: int) -> None:
        visible = self.visible_rows()
        if not visible:
            return
        self.top = max(0, min(max(0, len(visible) - self.body_height), self.top + delta))
        index = self.cursor_index()
        if index < self.top:
            self.move(self.top - index)
        elif index >= self.top + self.body_height:
            self.move(self.top + self.body_height - 1 - index)

    def type_text(self, text: str) -> None:
        if self.panel is not None and self.panel.prompt is not None:
            self.panel = replace(self.panel, entry=self.panel.entry + text)
            return
        self.input_text += text
        self.status = self._input_note()
        self.top = 0

    def backspace(self) -> None:
        if self.panel is not None and self.panel.prompt is not None:
            self.panel = replace(self.panel, entry=self.panel.entry[:-1])
            return
        self.input_text = self.input_text[:-1]
        self.status = self._input_note()

    def escape(self) -> None:
        """Esc steps back out of wherever a person is.

        A panel closes, then typed input clears, then a second screen returns
        to the list. With nothing left to step out of, Esc leaves the picker
        — the way out has to be reachable from the footer that names it.
        """

        if self.panel is not None:
            self.panel = None
            self.panel_targets = ()
            self.panel_kind = ""
            self.pending_provider = ""
            self.pending_project = ""
            self.status = voice.NOTHING_CHANGED
            return
        if self.input_text:
            self.input_text = ""
            self.status = ""
            return
        if self.view != LIST:
            self.view = LIST
            self.status = ""
            return
        self.leave()

    def back_key(self) -> bool:
        """`b` goes back — on every screen where `b` is a key and not a letter.

        One grammar across the product (operator ruling, 2026-08-15). The two screens that
        refuse it are the session list and the closed list, where letters are
        the filter: taking `b` there would cost a person the ability to search
        for anything beginning with it. Unlike Esc this never leaves the
        picker — back from the top is still the top.
        """

        if self.panel is not None and self.panel.prompt is None:
            self.escape()
            return True
        if self.panel is None and self.view == HELP:
            self.view = LIST
            self.status = ""
            return True
        return False

    def leave(self) -> None:
        """Leaving on purpose. Ctrl-D asks for this from anywhere."""

        self.leaving = True

    def quit_key(self) -> bool:
        """`q` leaves, but only when there is nothing typed for it to join.

        With a filter under way it is an ordinary letter, because a person
        narrowing a list by name must be able to type the letter q.
        """

        if self.panel is None and not self.input_text and self.view == LIST:
            self.leave()
            return True
        return False

    def _input_note(self) -> str:
        if not self.input_text or self.is_filter:
            return ""
        numbers, unusable = rowmod.parse_marks(self.input_text)
        on_screen = {row.number for row in self._session_rows() if row.number is not None}
        for number in numbers:
            if number not in on_screen:
                return voice.no_such_session(number)
        for fragment in unusable:
            return voice.no_such_session(fragment)
        return ""

    # ---- Enter -----------------------------------------------------------

    def enter(self) -> Batch | None:
        if self.panel is not None:
            return self._panel_enter()
        if self.view == HELP:
            self.view = LIST
            return None
        # A typed number that names no row is refused, and Enter stops there.
        # Falling through to the highlight would open a session nobody named.
        if self.view == LIST and self.input_text and not self.is_filter:
            note = self._input_note()
            if note:
                self.status = note
                return None
        marked = self.marks
        if self.view == LIST and marked:
            self._open_action_panel(
                [self._session_for_number(number) for number in marked], default="close"
            )
            return None
        row = self.current_row()
        if row is None:
            # Enter never leaves. The old picker left on an empty line because
            # it had a text prompt; this screen always has a row under the
            # highlight, so an Enter that sometimes dropped a person into a
            # shell would be the same key meaning two things. Esc, q, and
            # Ctrl-D leave, and the footer names the first of them.
            return None
        return self._activate(row)

    def _session_for_number(self, number: int) -> Session | None:
        return self.inventory.by_number(number)

    def _activate(self, row: rowmod.Row) -> Batch | None:
        if row.kind == rowmod.SESSION and row.session is not None:
            if (
                row.session.raw.get("is_subagent") is True
                or isinstance(row.session.raw.get("parent_session"), Mapping)
            ):
                self.status = voice.SUBAGENT_STAYS_WITH_PARENT
                return None
            if not self.inventory.live:
                self.status = voice.NOTHING_CHANGED
                return None
            return self._run(actions.open_session(row.session), row.session.key)
        if row.kind == rowmod.MACHINE_COUNT:
            self.machine_expanded = not self.machine_expanded
            self.status = ""
            return None
        if row.kind == rowmod.NEW_SESSION:
            self._open_provider_panel()
            return None
        if row.kind == rowmod.PROJECTS:
            self._open_project_panel()
            return None
        if row.kind == rowmod.CLOSED:
            self.view = CLOSED
            self.cursor_key = None
            self.cursor_fallback = 0
            self.top = 0
            self.status = ""
            return None
        if row.kind == rowmod.HELP:
            self.view = HELP
            self.status = ""
            return None
        if row.kind == rowmod.CLOSED_SESSION and row.closed is not None:
            self._open_closed_panel(row.closed)
            return None
        return None

    def _run(self, plan: Plan, seen_key: str | None = None) -> Batch:
        return Batch((plan,), plan.success, plan.failure, seen_key or "")

    # ---- panels ----------------------------------------------------------

    def _targets(self) -> list[Session]:
        found = []
        for key in self.panel_targets:
            session = self.inventory.by_key(key)
            if session is not None:
                found.append(session)
        return found

    def _open_action_panel(self, sessions: Iterable[Session | None], *, default: str) -> None:
        chosen = [session for session in sessions if session is not None]
        if not chosen:
            self.status = voice.NOTHING_CHANGED
            return
        self.panel_targets = tuple(session.key for session in chosen)
        self.panel_kind = "actions"
        if len(chosen) == 1:
            session = chosen[0]
            open_label = "Move it here" if session.is_attached else voice.ACTION_OPEN
            open_detail = "the other window disconnects" if session.is_attached else ""
            items = (
                PanelItem("open", open_label, open_detail),
                PanelItem("history", voice.ACTION_HISTORY),
                PanelItem("close", voice.ACTION_CLOSE, "the shell and everything inside will end", "attention"),
                PanelItem("account", voice.ACTION_ACCOUNT, "keeps this exact conversation"),
                PanelItem("model", voice.ACTION_MODEL),
                PanelItem("rename", voice.ACTION_RENAME),
                PanelItem("color", voice.ACTION_COLOR),
            )
            title = chosen[0].title
        else:
            items = (PanelItem("close", voice.ACTION_CLOSE, "every shell and everything inside will end", "attention"),)
            title = voice.plural(len(chosen), "session") + " marked"
        index = next(
            (position for position, item in enumerate(items) if item.key == default), 0
        )
        identity = chosen[0].display_color if len(chosen) == 1 else ""
        subtitle = "Open elsewhere." if len(chosen) == 1 and chosen[0].is_attached else ""
        self.panel = Panel(
            title, items, index, identity_color=identity, subtitle=subtitle,
            subtitle_style="attention",
        )
        self.status = ""

    def _open_closed_panel(self, item: ClosedSession) -> None:
        self.panel_targets = (item.key,)
        self.panel_kind = "closed"
        entries = []
        if item.restorable:
            entries.append(PanelItem("restore", voice.ACTION_RESTORE))
        entries.append(
            PanelItem(
                "history",
                voice.ACTION_HISTORY,
                # Two ways to be history only, and they are not the same
                # sentence: a shell never had a conversation, and a
                # conversation whose transcript is gone had one this machine
                # can no longer read. Calling the second a shell is how the
                # list itself used to lie about that row.
                ""
                if item.restorable
                else "a shell session keeps its history and nothing else"
                if item.provider.casefold() == "shell"
                else "its transcript is no longer on this machine, so only its"
                " history is kept",
            )
        )
        self.panel = Panel(
            item.title, tuple(entries), 0,
            subtitle=f"[{item.provider_label}] login time unknown",
            subtitle_provider=item.provider,
        )
        self.status = ""

    def _open_provider_panel(self) -> None:
        self.panel_kind = "provider"
        self.panel = Panel(
            voice.NEW_SESSION,
            tuple(
                PanelItem(
                    key,
                    "Claude Code" if key == "claude" else ("Shell" if key == "shell" else label),
                    style=f"provider:{key}",
                )
                for key, label in PROVIDERS
            ),
            0,
        )
        self.status = ""

    def _open_project_panel(self) -> None:
        self.panel_kind = "project"
        items = [PanelItem(HERE, "This directory")]
        items.extend(PanelItem(alias, alias, path) for alias, path in self.projects)
        self.panel = Panel("Project", tuple(items), 0)
        self.status = ""

    def _panel_enter(self) -> Batch | None:
        panel = self.panel
        if panel is None:
            return None
        if panel.prompt is not None:
            title = panel.entry.strip()
            targets = self._targets()
            self.panel = None
            self.panel_kind = ""
            if not title or not targets:
                self.status = voice.NOTHING_CHANGED
                return None
            self.input_text = ""
            return self._run(actions.rename(targets[0], title))
        item = panel.current
        if item is None:
            self.status = voice.NOTHING_CHANGED
            return None
        kind, key = self.panel_kind, item.key

        if kind == "provider":
            self.pending_provider = key
            if self.pending_project:
                return self._new_session_batch()
            self._open_project_panel()
            return None
        if kind == "project":
            self.pending_project = "" if key == HERE else key
            if self.pending_provider:
                return self._new_session_batch()
            self._open_provider_panel()
            return None
        if kind == "closed":
            item_key = self.panel_targets[0] if self.panel_targets else ""
            record = next((entry for entry in self.closed if entry.key == item_key), None)
            self.panel = None
            self.panel_kind = ""
            if record is None:
                self.status = voice.NOTHING_CHANGED
                return None
            if key == "restore":
                return Batch(
                    (actions.restore(record),),
                    voice.restored(record.title),
                    voice.action_failed(voice.ACTION_RESTORE),
                )
            self.status = voice.NOTHING_CHANGED
            return None

        targets = self._targets()
        if not targets:
            self.panel = None
            self.status = voice.NOTHING_CHANGED
            return None

        if kind == "actions":
            return self._action_enter(key, targets)
        if kind == "account":
            self.panel = None
            self.panel_kind = ""
            self.input_text = ""
            return self._run(actions.change_account(targets[0], key))
        if kind == "model":
            self.panel = None
            self.panel_kind = ""
            self.input_text = ""
            return self._run(actions.change_model(targets[0], key))
        if kind == "color":
            self.panel = None
            self.panel_kind = ""
            self.input_text = ""
            return self._run(actions.color(targets[0], key))
        self.panel = None
        self.status = voice.NOTHING_CHANGED
        return None

    def _new_session_batch(self) -> Batch:
        provider, project = self.pending_provider, self.pending_project
        self.panel = None
        self.panel_kind = ""
        self.pending_provider = ""
        self.pending_project = ""
        plan = actions.new_session(provider, project or None)
        return Batch((plan,), plan.success, plan.failure)

    def _action_enter(self, key: str, targets: list[Session]) -> Batch | None:
        if key == "close":
            plans = tuple(actions.close(session) for session in targets)
            self.panel = None
            self.panel_kind = ""
            self.input_text = ""
            success = (
                voice.closed_one(targets[0].title)
                if len(targets) == 1
                else voice.closed_many(len(targets))
            )
            return Batch(plans, success, voice.action_failed(voice.ACTION_CLOSE))
        session = targets[0]
        if key == "open":
            self.panel = None
            self.panel_kind = ""
            self.input_text = ""
            return self._run(actions.open_session(session), session.key)
        if key == "history":
            self.panel = None
            self.panel_kind = ""
            self.input_text = ""
            return self._run(actions.history(session))
        if key == "rename":
            self.panel = Panel(
                session.title, (), 0, prompt="New name (↵ back) ❯", entry="",
                identity_color=session.display_color,
            )
            return None
        if key == "account":
            aliases = self.accounts or (
                self.account_source(session.provider) if self.account_source else ()
            )
            if not aliases:
                self.panel = None
                self.panel_kind = ""
                self.status = voice.empty("Accounts")
                return None
            self.panel_kind = "account"
            self.panel = Panel(
                "Change to account", tuple(PanelItem(a, a) for a in aliases), 0,
                identity_color=session.display_color,
            )
            return None
        if key == "model":
            names = self.models or (
                self.model_source(session.provider) if self.model_source else ()
            )
            if not names:
                self.panel = None
                self.panel_kind = ""
                self.status = voice.empty("Models")
                return None
            self.panel_kind = "model"
            self.panel = Panel(
                voice.ACTION_MODEL, tuple(PanelItem(n, n) for n in names), 0,
                identity_color=session.display_color,
            )
            return None
        if key == "color":
            self.panel_kind = "color"
            names = actions.colors_for(session.provider)
            self.panel = Panel(
                voice.ACTION_COLOR,
                tuple(PanelItem(n, n, style=f"title:{n}") for n in names),
                0, identity_color=session.display_color,
            )
            return None
        self.panel = None
        self.status = voice.NOTHING_CHANGED
        return None

    # ---- results ---------------------------------------------------------

    def action_finished(self, batch: Batch, *, ok: bool, note: str = "") -> None:
        """One line, after the fact. Never a question, never silence."""

        self.panel = None
        self.panel_kind = ""
        self.panel_targets = ()
        self.input_text = ""
        if ok and batch.seen_key:
            self.seen_at[batch.seen_key] = self.clock()
        if note:
            self.status = note
        elif ok:
            self.status = batch.success
        else:
            self.status = batch.failure

    # ---- mouse -----------------------------------------------------------

    def click(self, line: int, column: int, frame: Frame) -> Batch | None:
        key = frame.row_lines.get(line)
        if key is None:
            return None
        if key.startswith("panel:") and self.panel is not None:
            wanted = key[len("panel:") :]
            for index, item in enumerate(self.panel.items):
                if item.key != wanted:
                    continue
                if index == self.panel.index:
                    return self._panel_enter()
                self.panel = replace(self.panel, index=index)
                return None
            return None
        visible = self.visible_rows()
        for index, row in enumerate(visible):
            if row.key != key:
                continue
            if column <= 1 and row.markable:
                self.toggle_mark(row.number)
                return None
            if self.cursor_key == key:
                return self._activate(row)
            self.cursor_key = key
            self.cursor_fallback = index
            self.pinned = tuple(item.key for item in visible if item.kind == rowmod.SESSION)
            self.cursor_moved_at_ms = self.clock()
            return None
        return None

    def toggle_mark(self, number: int | None) -> None:
        if number is None:
            return
        if self.is_filter:
            self.input_text = ""
        numbers, _ = rowmod.parse_marks(self.input_text)
        chosen = [value for value in numbers if value != number]
        if len(chosen) == len(numbers):
            chosen.append(number)
        self.input_text = ",".join(str(value) for value in chosen)
        self.status = self._input_note()
