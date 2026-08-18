"""The curses shell.

It paints frames and turns terminal events into the events `state` answers.
Every rule about what a person sees or what a key means lives elsewhere; this
file is the part that cannot be tested without a terminal, so it is kept as
close to nothing as it can be.
"""

from __future__ import annotations

import curses
import os
from pathlib import Path
import queue
import sys
import threading
from typing import Mapping

from sessionkit_inventory.common import CollectionError

from . import frame as framemod, voice
from .frame import Frame
from .model import Inventory
from .runner import Runner, run_batch
from .state import Picker

# How often the list catches up with the estate, and how long a keystroke
# waits before the loop looks around. Both are milliseconds.
REFRESH_SECONDS = 5
INPUT_TIMEOUT_MS = 200

# Leaving on purpose is 0. Every way the picker cannot run is 1, with the
# reason on stderr. 2 belongs to arguments and selectors, which this program
# does not take.
EXIT_LEAVE = 0
EXIT_CANNOT_RUN = 1

CTRL_D = 4
QUIT_KEY = ord("q")
# `b` is back wherever it is a key rather than a letter being typed; the picker
# itself decides which screen that is (`Picker.back_key`).
BACK_KEY = ord("b")

# How long ncurses waits after a bare Esc before deciding it was not the start
# of an arrow key. The default is a full second, which makes the one key that
# clears the input and leaves the screen feel broken.
ESCAPE_DELAY_MS = 25

SESSION_PALETTE = {
    "red": (237, 93, 93),
    "blue": (97, 166, 240),
    "green": (63, 221, 115),
    "yellow": (249, 215, 108),
    "purple": (173, 115, 239),
    "orange": (242, 144, 81),
    "pink": (240, 113, 177),
    "cyan": (64, 216, 209),
    "lime": (170, 230, 70),
    "magenta": (255, 95, 255),
    "silver": (205, 210, 220),
    "sand": (214, 178, 130),
    "sky": (150, 205, 255),
    "sea": (95, 235, 170),
}

PALETTE_INDICES = {name: 16 + index for index, name in enumerate(SESSION_PALETTE)}

FALLBACK_16 = {
    "red": 8,
    "blue": 12,
    "green": 6,
    "yellow": 11,
    "purple": 12,
    "orange": 3,
    "pink": 5,
    "cyan": 6,
    "lime": 3,
    "magenta": 13,
    "silver": 7,
    "sand": 8,
    "sky": 7,
    "sea": 6,
}

PROVIDER_PALETTE = {
    "claude": "yellow",
    "codex": "cyan",
    "shell": "green",
    "unknown": "yellow",
}


class Poller:
    """Collects snapshots off the keyboard's path.

    A snapshot costs over a second. Collected in the loop it would freeze the
    screen every five seconds; collected here the list simply becomes newer
    between keystrokes.
    """

    def __init__(self, runner: Runner, seconds: int = REFRESH_SECONDS) -> None:
        self.runner = runner
        self.seconds = seconds
        self.results: queue.Queue = queue.Queue()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()

    def _loop(self) -> None:
        while not self.stopped.is_set():
            inventory = self.runner.snapshot()
            if inventory is not None:
                self.results.put(inventory)
            self.stopped.wait(self.seconds)

    def drain(self) -> Inventory | None:
        newest = None
        while True:
            try:
                newest = self.results.get_nowait()
            except queue.Empty:
                return newest


def color_wanted(environ) -> bool:
    """Colour unless a person has actually asked for none.

    `NO_COLOR` counts as set whatever its value, which is the convention the
    rest of the kit already follows; an unset variable is not a preference.
    """

    return "NO_COLOR" not in environ and "SESSION_KIT_NO_COLOR" not in environ


def _style(name: str, colors: Mapping[str, int] | None = None) -> int:
    """One place deciding what a named run looks like.

    Without colour the same runs stay distinguishable by weight, so a screen
    on a colourless terminal is still readable rather than merely uniform.
    """

    colors = colors or {}
    pair = curses.color_pair(colors.get(name, 0)) if colors.get(name) else 0
    if name == framemod.BOLD:
        return curses.A_BOLD | pair
    if name == framemod.DIM:
        return curses.A_DIM
    if name == framemod.ATTENTION:
        return curses.A_BOLD | pair
    if name == framemod.MARK:
        return curses.A_BOLD | pair
    if name == framemod.NUMBER:
        return pair or curses.A_BOLD
    if name == framemod.KEY:
        return curses.A_BOLD | pair
    if name.startswith(framemod.PROVIDER_STYLE_PREFIX):
        return curses.A_BOLD | pair
    if name.startswith(framemod.TITLE_STYLE_PREFIX):
        return pair
    return pair or curses.A_NORMAL


def paint(window, drawn: Frame, *, colors: Mapping[str, int] | None = None) -> None:
    """One frame, one update. Nothing is drawn twice, so nothing flickers."""

    height, width = window.getmaxyx()
    window.erase()
    for index, line in enumerate(drawn.lines):
        if index >= height:
            break
        text = line.text[:width]
        if text:
            try:
                # insstr, not addstr: writing the last cell of the last line
                # advances the cursor off the screen and curses calls that an
                # error, which would drop the row entirely.
                window.insstr(index, 0, text)
            except curses.error:
                continue
        # The highlight is painted first and the runs on top of it, so a
        # highlighted row keeps its colours instead of going flat.
        base = curses.A_REVERSE if drawn.cursor_line == index else 0
        if base:
            try:
                window.chgat(index, 0, width, base)
            except curses.error:
                pass
        for span in line.spans:
            if span.start >= len(text):
                continue
            length = min(span.length, len(text) - span.start)
            if length <= 0:
                continue
            try:
                window.chgat(index, span.start, length, _style(span.style, colors) | base)
            except curses.error:
                continue
    window.noutrefresh()
    curses.doupdate()


class Application:
    def __init__(self, runner: Runner, picker: Picker) -> None:
        self.runner = runner
        self.picker = picker
        self.poller = Poller(runner)
        self.frame: Frame | None = None
        self.colors: dict[str, int] = {}
        self.background = curses.COLOR_BLACK
        self.custom_colors = False

    def run(self, window) -> int:
        curses.curs_set(0)
        window.keypad(True)
        window.timeout(INPUT_TIMEOUT_MS)
        try:
            curses.set_escdelay(ESCAPE_DELAY_MS)
        except (AttributeError, curses.error):
            pass
        self.colors = self._start_color()
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        except curses.error:
            pass
        self.poller.start()
        try:
            return self._loop(window)
        finally:
            self.poller.stop()

    def _install_palette(self) -> None:
        if not self.custom_colors:
            return
        for name, (red, green, blue) in SESSION_PALETTE.items():
            curses.init_color(
                PALETTE_INDICES[name],
                (red * 1000 + 254) // 255,
                (green * 1000 + 254) // 255,
                (blue * 1000 + 254) // 255,
            )

    def _start_color(self) -> dict[str, int]:
        """Colour, if this terminal has it and nobody asked for none.

        `use_default_colors` runs either way. Without it the default pair is
        white-on-black, and ncurses then paints every cell with an explicit
        colour, the exact opposite of what NO_COLOR asks for.
        """

        try:
            curses.start_color()
        except curses.error:
            return {}
        self.background = curses.COLOR_BLACK
        try:
            curses.use_default_colors()
            self.background = -1
        except curses.error:
            pass
        if not color_wanted(self.runner.environ):
            return {}
        if not curses.has_colors():
            return {}
        try:
            self.custom_colors = bool(
                curses.COLORS >= 256 and curses.can_change_color()
            )
            self._install_palette()
            pairs: dict[str, int] = {}
            for offset, name in enumerate(SESSION_PALETTE, start=10):
                foreground = (
                    PALETTE_INDICES[name]
                    if self.custom_colors
                    else FALLBACK_16[name]
                )
                if curses.COLORS < 16:
                    foreground %= 8
                curses.init_pair(offset, foreground, self.background)
                pairs[f"{framemod.TITLE_STYLE_PREFIX}{name}"] = offset
                pairs[f"palette:{name}"] = offset
            pairs[framemod.BOLD] = pairs["palette:cyan"]
            pairs[framemod.ATTENTION] = pairs["palette:yellow"]
            pairs[framemod.MARK] = pairs["palette:green"]
            pairs[framemod.NUMBER] = pairs["palette:green"]
            pairs[framemod.KEY] = pairs["palette:green"]
            for provider, palette in PROVIDER_PALETTE.items():
                pairs[f"{framemod.PROVIDER_STYLE_PREFIX}{provider}"] = pairs[
                    f"palette:{palette}"
                ]
        except curses.error:
            return {}
        return pairs

    def _draw(self, window) -> None:
        height, width = window.getmaxyx()
        self.frame = self.picker.frame(width=width, height=height)
        paint(window, self.frame, colors=self.colors)

    def _loop(self, window) -> int:
        self._draw(window)
        while True:
            fresh = self.poller.drain()
            if fresh is not None:
                self.picker.set_inventory(fresh)
                self.picker.set_closed(self.runner.closed_sessions())
                self._draw(window)
            try:
                key = window.getch()
            except KeyboardInterrupt:
                self._draw(window)
                continue
            if key == -1:
                continue
            batch = self._handle(window, key)
            if self.picker.leaving:
                return EXIT_LEAVE
            if batch:
                self._act(window, batch)
                if self.picker.leaving:
                    return EXIT_LEAVE
            self._draw(window)

    def _handle(self, window, key: int):
        picker = self.picker
        if key == curses.KEY_RESIZE:
            return None
        if key == curses.KEY_UP:
            picker.move(-1)
            return None
        if key == curses.KEY_DOWN:
            picker.move(1)
            return None
        if key == curses.KEY_PPAGE:
            picker.move(-max(1, picker.body_height - 1))
            return None
        if key == curses.KEY_NPAGE:
            picker.move(max(1, picker.body_height - 1))
            return None
        if key == curses.KEY_HOME:
            picker.move(-10**6)
            return None
        if key == curses.KEY_END:
            picker.move(10**6)
            return None
        if key in (curses.KEY_ENTER, 10, 13):
            return picker.enter()
        if key == 27:
            picker.escape()
            return None
        if key in (curses.KEY_BACKSPACE, 127, 8):
            picker.backspace()
            return None
        if key == CTRL_D:
            picker.leave()
            return None
        if key == QUIT_KEY and picker.quit_key():
            return None
        if key == BACK_KEY and picker.back_key():
            return None
        if key == curses.KEY_MOUSE:
            return self._mouse(picker)
        if 32 <= key < 127:
            picker.type_text(chr(key))
            return None
        return None

    def _mouse(self, picker: Picker):
        try:
            _, column, line, _, state = curses.getmouse()
        except curses.error:
            return None
        if state & getattr(curses, "BUTTON4_PRESSED", 0):
            picker.wheel(-3)
            return None
        if state & getattr(curses, "BUTTON5_PRESSED", 0):
            picker.wheel(3)
            return None
        if not state & curses.BUTTON1_CLICKED and not state & curses.BUTTON1_PRESSED:
            return None
        if self.frame is None:
            return None
        if self._footer_click(line, column):
            return picker.enter()
        return picker.click(line, column, self.frame)

    def _footer_click(self, line: int, column: int) -> bool:
        """The footer is not decoration: its middle hint is the Enter key."""

        if self.frame is None or line != len(self.frame.lines) - 1:
            return False
        text = self.frame.lines[line].text
        start = text.find("↵")
        if start < 0:
            return False
        return start <= column <= start + len("↵ open")

    def _act(self, window, batch) -> None:
        def suspend():
            curses.def_prog_mode()
            curses.endwin()

        def resume():
            window.clear()
            curses.reset_prog_mode()
            self._install_palette()
            window.refresh()

        ok, note = run_batch(
            self.runner,
            self.picker.inventory,
            batch.plans,
            suspend=suspend,
            resume=resume,
        )
        self.picker.action_finished(batch, ok=ok, note=note)
        fresh = self.runner.snapshot()
        if fresh is not None:
            self.picker.set_inventory(fresh)
            self.picker.set_closed(self.runner.closed_sessions())


def build(script_dir: Path, environ=None) -> tuple[Runner, Picker]:
    environ = dict(os.environ if environ is None else environ)
    runner = Runner.from_environment(script_dir, environ)
    inventory = runner.snapshot()
    picker = Picker(
        inventory=inventory if inventory is not None else Inventory(source="cache", stale=True),
        closed=runner.closed_sessions(),
        projects=runner.projects(),
        account_source=runner.accounts,
        model_source=runner.models,
        self_session_id=environ.get("SHPOOL_SESSION_NAME") or None,
    )
    return runner, picker


def main() -> int:
    script_dir = Path(__file__).resolve().parents[2] / "bin"
    environ = os.environ
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(voice.error("the picker needs a terminal"), file=sys.stderr)
        return EXIT_CANNOT_RUN
    if environ.get("SESSION_KIT_NONINTERACTIVE") == "1":
        print(voice.error("the picker needs a terminal"), file=sys.stderr)
        return EXIT_CANNOT_RUN
    try:
        runner, picker = build(script_dir, environ)
        if not Path(runner.sp_cmd).exists() or not Path(runner.status_cmd).exists():
            print(
                voice.error("the picker's helpers are missing from this release"),
                file=sys.stderr,
            )
            return EXIT_CANNOT_RUN
        application = Application(runner, picker)
        return curses.wrapper(application.run)
    except (curses.error, CollectionError) as failure:
        print(voice.error(f"the screen could not be drawn: {failure}"), file=sys.stderr)
        return EXIT_CANNOT_RUN


__all__ = ["Application", "Poller", "build", "main", "paint"]
