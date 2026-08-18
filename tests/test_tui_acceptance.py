"""The picker as a terminal actually renders it.

The rest of the suite tests the frame model, which is what a person would see
if a terminal drew exactly what it was told. This file tests what a terminal
does draw: the bytes go through a screen model that tracks a cell grid and the
attributes on every cell, the same way the terminal in front of a person does.

That difference matters. A title too long for its column pushed its own detail
right; every splitlines-based assertion passed, because the *line* was still
correct, only the *columns* were wrong. Reconstructing the grid is the check
that sees it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import struct
import tempfile
import termios
import time
import unittest

from tests.tui_support import REPO, document, row, write_executable

PICKER = REPO / "bin" / "shpool_login_tui"
CTRL_D = b"\x04"
ESC = b"\x1b"
ENTER = b"\r"
DOWN = b"\x1bOB"

LINES, COLUMNS = 30, 120

# The terminals to prove against. Ghostty is what the operator runs; it is
# skipped rather than faked where its terminfo is not installed.
TERMINALS = ("xterm-256color", "xterm-ghostty")

# A colour, as opposed to "whatever this terminal calls default". 39 and 49
# are the default-foreground and default-background codes: they are in the
# colour family but they set no colour, and a screen that emits only those is
# a screen with no colour on it.
COLOR_SGR = re.compile(
    rb"\x1b\[(?P<params>[0-9;]*)m"
)
COLOR_PARAMS = re.compile(r"(?:^|;)(3[0-7]|9[0-7]|4[0-7]|10[0-7]|38|48)(?:;|$)")

# The exact shared palette from shpool_login_render.sh. Curses installs these
# at stable custom indices so the cell grid proves both identity and RGB.
SESSION_PALETTE = {
    "red": (16, (237, 93, 93)),
    "blue": (17, (97, 166, 240)),
    "green": (18, (63, 221, 115)),
    "yellow": (19, (249, 215, 108)),
    "purple": (20, (173, 115, 239)),
    "orange": (21, (242, 144, 81)),
    "pink": (22, (240, 113, 177)),
    "cyan": (23, (64, 216, 209)),
    "lime": (24, (170, 230, 70)),
    "magenta": (25, (255, 95, 255)),
    "silver": (26, (205, 210, 220)),
    "sand": (27, (214, 178, 130)),
    "sky": (28, (150, 205, 255)),
    "sea": (29, (95, 235, 170)),
}


def has_color_bytes(payload: bytes) -> bool:
    for match in COLOR_SGR.finditer(payload):
        params = match.group("params").decode("ascii", "replace")
        if COLOR_PARAMS.search(params):
            return True
    return False


# ---------------------------------------------------------------------------
# A screen, as the terminal keeps one.


@dataclass
class Cell:
    char: str = " "
    bold: bool = False
    dim: bool = False
    reverse: bool = False
    foreground: int | None = None
    background: int | None = None

    def blank(self) -> "Cell":
        return Cell()


@dataclass
class Emulator:
    """Enough of a terminal to hold what ncurses sends it.

    Cursor addressing, erasing, insertion, deletion, scrolling regions, and
    the attributes in force when each cell was written. Not a terminal, a
    faithful enough model of one to assert against.
    """

    rows: int = LINES
    columns: int = COLUMNS
    row: int = 0
    column: int = 0
    top: int = 0
    bottom: int = LINES - 1
    bold: bool = False
    dim: bool = False
    reverse: bool = False
    foreground: int | None = None
    background: int | None = None
    saved: tuple[int, int] = (0, 0)
    pending: bytes = b""
    grid: list[list[Cell]] = field(default_factory=list)
    palette: dict[int, tuple[int, int, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.bottom = self.rows - 1
        self.grid = [[Cell() for _ in range(self.columns)] for _ in range(self.rows)]

    # -- reading it back ----------------------------------------------------

    def line(self, index: int) -> str:
        return "".join(cell.char for cell in self.grid[index]).rstrip()

    def lines(self) -> list[str]:
        return [self.line(index) for index in range(self.rows)]

    def text(self) -> str:
        return "\n".join(self.lines())

    def find(self, needle: str) -> int:
        for index in range(self.rows):
            if needle in self.line(index):
                return index
        return -1

    def colored_columns(self, index: int, foreground: int) -> list[int]:
        return [
            column
            for column, cell in enumerate(self.grid[index])
            if cell.foreground == foreground and cell.char != " "
        ]

    def colored_cells(self, index: int) -> list[Cell]:
        return [
            cell
            for cell in self.grid[index]
            if cell.foreground is not None and cell.char != " "
        ]

    def any_color(self) -> bool:
        return any(
            cell.foreground is not None or cell.background is not None
            for line in self.grid
            for cell in line
        )

    # -- writing into it ----------------------------------------------------

    def _cell(self) -> Cell:
        return Cell(
            " ",
            self.bold,
            self.dim,
            self.reverse,
            self.foreground,
            self.background,
        )

    def _put(self, character: str) -> None:
        if self.column >= self.columns:
            self.column = 0
            self._index()
        cell = self._cell()
        cell.char = character
        self.grid[self.row][self.column] = cell
        self.column += 1

    def _index(self) -> None:
        if self.row == self.bottom:
            self._scroll_up(1)
        elif self.row < self.rows - 1:
            self.row += 1

    def _scroll_up(self, count: int) -> None:
        for _ in range(count):
            del self.grid[self.top]
            self.grid.insert(self.bottom, [Cell() for _ in range(self.columns)])

    def _scroll_down(self, count: int) -> None:
        for _ in range(count):
            del self.grid[self.bottom]
            self.grid.insert(self.top, [Cell() for _ in range(self.columns)])

    def _erase(self, index: int, start: int, stop: int) -> None:
        for column in range(max(0, start), min(self.columns, stop)):
            self.grid[index][column] = Cell()

    def feed(self, payload: bytes) -> None:
        data = self.pending + payload
        self.pending = b""
        position = 0
        length = len(data)
        while position < length:
            byte = data[position]
            if byte == 0x1B:
                consumed = self._escape(data, position)
                if consumed is None:
                    self.pending = data[position:]
                    return
                position += consumed
                continue
            if byte in (0x07,):
                position += 1
                continue
            if byte == 0x0D:
                self.column = 0
                position += 1
                continue
            if byte == 0x0A:
                self._index()
                position += 1
                continue
            if byte == 0x08:
                self.column = max(0, self.column - 1)
                position += 1
                continue
            if byte == 0x09:
                self.column = min(self.columns - 1, (self.column // 8 + 1) * 8)
                position += 1
                continue
            if byte < 0x20:
                position += 1
                continue
            # One UTF-8 character, held back when the read cut it in half.
            size = 1
            if byte >= 0xF0:
                size = 4
            elif byte >= 0xE0:
                size = 3
            elif byte >= 0xC0:
                size = 2
            if position + size > length:
                self.pending = data[position:]
                return
            self._put(data[position : position + size].decode("utf-8", "replace"))
            position += size

    def _escape(self, data: bytes, position: int) -> int | None:
        if position + 1 >= len(data):
            return None
        marker = data[position + 1]
        if marker == 0x5B:  # CSI
            index = position + 2
            while index < len(data) and not 0x40 <= data[index] <= 0x7E:
                index += 1
            if index >= len(data):
                return None
            self._csi(data[position + 2 : index].decode("ascii", "replace"), chr(data[index]))
            return index - position + 1
        if marker == 0x5D:  # OSC, ended by BEL or ST
            index = position + 2
            while index < len(data):
                if data[index] == 0x07:
                    self._osc(data[position + 2 : index])
                    return index - position + 1
                if data[index] == 0x1B and index + 1 < len(data) and data[index + 1] == 0x5C:
                    self._osc(data[position + 2 : index])
                    return index - position + 2
                index += 1
            return None
        if marker in (0x28, 0x29, 0x2A, 0x2B, 0x25, 0x23):  # charset designators
            if position + 2 >= len(data):
                return None
            return 3
        if marker == 0x4D:  # RI
            if self.row == self.top:
                self._scroll_down(1)
            else:
                self.row = max(0, self.row - 1)
            return 2
        if marker == 0x44:  # IND
            self._index()
            return 2
        if marker == 0x45:  # NEL
            self.column = 0
            self._index()
            return 2
        if marker == 0x37:  # DECSC
            self.saved = (self.row, self.column)
            return 2
        if marker == 0x38:  # DECRC
            self.row, self.column = self.saved
            return 2
        return 2

    def _osc(self, payload: bytes) -> None:
        """Remember palette changes emitted by terminfo's initc capability."""

        if payload == b"104":
            self.palette.clear()
            return
        match = re.fullmatch(
            rb"4;(?P<index>\d+);rgb:(?P<red>[0-9A-Fa-f]+)/"
            rb"(?P<green>[0-9A-Fa-f]+)/(?P<blue>[0-9A-Fa-f]+)",
            payload,
        )
        if match is None:
            return

        def channel(name: str) -> int:
            raw = match.group(name)
            value = int(raw, 16)
            maximum = (1 << (4 * len(raw))) - 1
            return (value * 255 + maximum // 2) // maximum

        self.palette[int(match.group("index"))] = (
            channel("red"),
            channel("green"),
            channel("blue"),
        )

    def _csi(self, raw: str, final: str) -> None:
        private = raw.startswith("?") or raw.startswith(">")
        body = raw.lstrip("?>")
        parts = [part for part in body.split(";")]
        numbers = [int(part) if part.isdigit() else 0 for part in parts if part != ""]

        def first(default: int = 1) -> int:
            return numbers[0] if numbers and numbers[0] else default

        if private and final in "hl":
            return
        if final == "m":
            self._sgr(parts)
            return
        if final in "ht":
            return
        if final == "r":
            self.top = (numbers[0] - 1) if numbers else 0
            self.bottom = (numbers[1] - 1) if len(numbers) > 1 else self.rows - 1
            self.top = max(0, min(self.rows - 1, self.top))
            self.bottom = max(self.top, min(self.rows - 1, self.bottom))
            self.row, self.column = 0, 0
            return
        if final == "A":
            self.row = max(0, self.row - first())
        elif final == "B":
            self.row = min(self.rows - 1, self.row + first())
        elif final == "C":
            self.column = min(self.columns - 1, self.column + first())
        elif final == "D":
            self.column = max(0, self.column - first())
        elif final == "E":
            self.row = min(self.rows - 1, self.row + first())
            self.column = 0
        elif final == "F":
            self.row = max(0, self.row - first())
            self.column = 0
        elif final in "G`":
            self.column = max(0, min(self.columns - 1, first() - 1))
        elif final == "d":
            self.row = max(0, min(self.rows - 1, first() - 1))
        elif final in "Hf":
            self.row = max(0, min(self.rows - 1, (numbers[0] - 1) if numbers else 0))
            self.column = max(
                0, min(self.columns - 1, (numbers[1] - 1) if len(numbers) > 1 else 0)
            )
        elif final == "J":
            mode = numbers[0] if numbers else 0
            if mode == 0:
                self._erase(self.row, self.column, self.columns)
                for index in range(self.row + 1, self.rows):
                    self._erase(index, 0, self.columns)
            elif mode == 1:
                self._erase(self.row, 0, self.column + 1)
                for index in range(0, self.row):
                    self._erase(index, 0, self.columns)
            else:
                for index in range(self.rows):
                    self._erase(index, 0, self.columns)
        elif final == "K":
            mode = numbers[0] if numbers else 0
            if mode == 0:
                self._erase(self.row, self.column, self.columns)
            elif mode == 1:
                self._erase(self.row, 0, self.column + 1)
            else:
                self._erase(self.row, 0, self.columns)
        elif final == "L":
            for _ in range(first()):
                del self.grid[self.bottom]
                self.grid.insert(self.row, [Cell() for _ in range(self.columns)])
        elif final == "M":
            for _ in range(first()):
                del self.grid[self.row]
                self.grid.insert(self.bottom, [Cell() for _ in range(self.columns)])
        elif final == "P":
            for _ in range(first()):
                del self.grid[self.row][self.column]
                self.grid[self.row].append(Cell())
        elif final == "@":
            for _ in range(first()):
                self.grid[self.row].insert(self.column, Cell())
                del self.grid[self.row][-1]
        elif final == "X":
            self._erase(self.row, self.column, self.column + first())
        elif final == "S":
            self._scroll_up(first())
        elif final == "T":
            self._scroll_down(first())

    def _sgr(self, parts: list[str]) -> None:
        if not parts or parts == [""]:
            parts = ["0"]
        index = 0
        while index < len(parts):
            part = parts[index]
            code = int(part) if part.isdigit() else 0
            if code == 0:
                self.bold = self.dim = self.reverse = False
                self.foreground = self.background = None
            elif code == 1:
                self.bold = True
            elif code == 2:
                self.dim = True
            elif code == 7:
                self.reverse = True
            elif code == 22:
                self.bold = self.dim = False
            elif code == 27:
                self.reverse = False
            elif 30 <= code <= 37:
                self.foreground = code - 30
            elif code == 39:
                self.foreground = None
            elif 40 <= code <= 47:
                self.background = code - 40
            elif code == 49:
                self.background = None
            elif 90 <= code <= 97:
                self.foreground = code - 90 + 8
            elif 100 <= code <= 107:
                self.background = code - 100 + 8
            elif code in (38, 48):
                mode = int(parts[index + 1]) if index + 1 < len(parts) else 5
                if mode == 5:
                    value = int(parts[index + 2]) if index + 2 < len(parts) else 0
                    index += 2
                else:
                    value = 256
                    index += 4
                if code == 38:
                    self.foreground = value
                else:
                    self.background = value
            index += 1


# ---------------------------------------------------------------------------
# Driving the real program.


class Session:
    """The picker running on a real pty, with a screen model beside it."""

    def __init__(
        self,
        *records,
        term: str = "xterm-256color",
        environ=None,
        projects=(),
        closed=(),
        transcripts=(),
        fail_commands=(),
    ):
        self.base = Path(tempfile.mkdtemp(prefix="tui-accept."))
        self.snapshot = self.base / "inventory.json"
        self.snapshot.write_text(json.dumps(document(*records)), encoding="utf-8")
        self.sp_log = self.base / "sp.log"
        status = write_executable(
            self.base / "fake-status",
            f'#!/usr/bin/env bash\ncat "{self.snapshot}"\n',
        )
        failures = "|".join(re.escape(command) for command in fail_commands) or "__never__"
        sp = write_executable(
            self.base / "fake-sp",
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.sp_log}"\n'
            "if [[ $1 == account && $2 == choices ]]; then\n"
            "  printf '%s\\n' '{\"choices\":[{\"alias\":\"primary\",\"eligible\":true},{\"alias\":\"spare\",\"eligible\":true}]}'\n"
            "  exit 0\n"
            "fi\n"
            f"[[ $1 =~ ^({failures})$ ]] && exit 2\n"
            "exit 0\n",
        )
        projects_file = self.base / "projects.tsv"
        projects_file.write_text(
            "".join(f"{alias}\tclaude\t{path}\n" for alias, path in projects),
            encoding="utf-8",
        )
        closed_file = self.base / "closed.jsonl"
        closed_file.write_text(
            "".join(json.dumps(record) + "\n" for record in closed),
            encoding="utf-8",
        )
        # A conversation can only come back if its record is still on this
        # machine, and this screen offers a Restore, so the cases that expect
        # one put the record there.
        for provider, uuid in transcripts:
            if provider == "codex":
                folder = self.base / ".codex" / "sessions" / "2026" / "08"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"rollout-2026-08-15T00-00-00-{uuid}.jsonl").write_text(
                    '{"type":"user"}\n', encoding="utf-8"
                )
                continue
            folder = self.base / ".claude" / "projects" / "-srv-project"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{uuid}.jsonl").write_text(
                '{"type":"user"}\n', encoding="utf-8"
            )
        # No COLUMNS and no LINES: the size has to come from the pty itself,
        # which is where it comes from for a person.
        self.environ = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base),
            "TERM": term,
            "PYTHONDONTWRITEBYTECODE": "1",
            "SESSION_KIT_STATUS_CMD": str(status),
            "SESSION_KIT_SP_CMD": str(sp),
            "SESSION_KIT_STATE_DIR": str(self.base / "state"),
            "SESSION_KIT_CLOSED_LEDGER": str(self.base / "closed.jsonl"),
            "SESSION_KIT_PROJECTS_FILE": str(self.base / "projects.tsv"),
            "SESSION_KIT_TUI_MODELS": "claude-opus-5,claude-sonnet-5",
        }
        self.environ.update(environ or {})
        self.screen = Emulator(LINES, COLUMNS)
        self.raw = bytearray()
        self.status: int | None = None
        self.pid, self.descriptor = pty.fork()
        if self.pid == 0:
            try:
                os.chdir(self.base)
                os.execve(
                    "/usr/bin/python3", ["python3", os.fspath(PICKER)], self.environ
                )
            finally:
                os._exit(127)
        fcntl.ioctl(
            self.descriptor,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", LINES, COLUMNS, 0, 0),
        )

    # -- lifetime -----------------------------------------------------------

    def close(self) -> None:
        if self.status is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
                _, self.status = os.waitpid(self.pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
        try:
            os.close(self.descriptor)
        except OSError:
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    def _pump(self, seconds: float) -> bool:
        """Read whatever the program has said. False once it has ended."""

        ready, _, _ = select.select([self.descriptor], [], [], seconds)
        if not ready:
            return True
        try:
            chunk = os.read(self.descriptor, 65536)
        except OSError as failure:
            if failure.errno != errno.EIO:
                raise
            return False
        if not chunk:
            return False
        self.raw.extend(chunk)
        self.screen.feed(chunk)
        return True

    def expect(self, predicate, *, timeout: float = 15.0, what: str = "") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.screen):
                return
            if not self._pump(0.05):
                break
        if predicate(self.screen):
            return
        raise AssertionError(
            f"the screen never showed {what or predicate}\n--- screen ---\n"
            + self.screen.text()
        )

    def type(self, keystrokes: bytes) -> None:
        os.write(self.descriptor, keystrokes)

    def settle(self, seconds: float = 0.6) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self._pump(0.05):
                return

    def wait(self, timeout: float = 15.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            alive = self._pump(0.05)
            waited, raw = os.waitpid(self.pid, os.WNOHANG)
            if waited == self.pid:
                self.status = raw
                return os.waitstatus_to_exitcode(raw)
            if not alive:
                _, raw = os.waitpid(self.pid, 0)
                self.status = raw
                return os.waitstatus_to_exitcode(raw)
        raise AssertionError(
            "the picker never left\n--- screen ---\n" + self.screen.text()
        )

    def sp_calls(self) -> list[str]:
        if not self.sp_log.exists():
            return []
        return [
            line for line in self.sp_log.read_text(encoding="utf-8").splitlines() if line
        ]


def terminfo_directory(term: str) -> str | None:
    """Where this terminal's description lives, if it lives anywhere.

    The picker runs with HOME pointed at a sandbox, so a description under
    the real ~/.terminfo would vanish from under it. Finding the directory
    here lets the child be told about it explicitly.
    """

    initial = term[0]
    candidates = [
        Path(os.environ["HOME"]) / ".terminfo" if os.environ.get("HOME") else None,
        Path("/etc/terminfo"),
        Path("/usr/share/terminfo"),
        Path("/lib/terminfo"),
    ]
    for root in candidates:
        if root is None:
            continue
        for leaf in (root / initial / term, root / f"{ord(initial):02x}" / term):
            if leaf.exists():
                return str(root)
    return None


def estate():
    """One long title, one short, one open elsewhere, one machine session."""

    return (
        row(
            "Orphaned Record Audit",
            number=3,
            needs_you=True,
            account_alias="primary",
            model="opus-4.6",
            subagents=2,
            age_seconds=5400,
            quiet_seconds=30,
            display_color="red",
        ),
        row(
            "Matrix",
            number=17,
            provider="codex",
            account_alias="spare",
            model="gpt-5-codex",
            agent_status="running",
            quiet_seconds=40,
            age_seconds=18_000,
            display_color="blue",
        ),
        row(
            "A session title long enough to run past its own column and drag the "
            "next one along with it",
            number=54,
            availability="attached",
            account_alias="third",
            agent_status="running",
            quiet_seconds=20,
            age_seconds=14_400,
            display_color="sea",
        ),
        row("drill", number=71, origin="machine", display_color="green"),
    )


class TerminalCaseMixin:
    term = "xterm-256color"

    @classmethod
    def setUpClass(cls) -> None:
        cls.terminfo = terminfo_directory(cls.term)
        if cls.terminfo is None:
            raise unittest.SkipTest(f"no terminfo for {cls.term}")

    def open(self, *records, environ=None, **keywords) -> Session:
        passed = {"TERMINFO_DIRS": f"{self.terminfo}:"}
        passed.update(environ or {})
        session = Session(*(records or estate()), term=self.term, environ=passed, **keywords)
        self.addCleanup(session.close)
        session.expect(
            lambda screen: screen.find(" ready ·") >= 0,
            what="the opening screen",
        )
        session.expect(
            lambda screen: screen.find("open #") >= 0, what="the footer"
        )
        session.settle()
        return session


class ColorTests(TerminalCaseMixin, unittest.TestCase):
    def test_the_screen_is_drawn_in_colour(self) -> None:
        session = self.open()
        self.assertTrue(
            has_color_bytes(bytes(session.raw)),
            "no colour sequence reached the terminal",
        )
        self.assertTrue(session.screen.any_color(), "no cell on the screen carries a colour")

    def test_there_is_no_attention_summary_line(self) -> None:
        """Removed by operator ruling, 2026-08-15 -- on a real terminal."""
        session = self.open()
        self.assertEqual(-1, session.screen.find("needs you:"))

    def test_the_footer_keys_are_coloured(self) -> None:
        session = self.open()
        index = session.screen.find("open #")
        self.assertGreaterEqual(index, 0)
        self.assertTrue(
            session.screen.colored_cells(index),
            f"the footer names no key in colour: {session.screen.line(index)!r}",
        )

    def test_the_session_numbers_and_provider_identities_are_coloured(self) -> None:
        session = self.open()
        index = session.screen.find("Orphaned Record Audit")
        self.assertGreaterEqual(index, 0)
        line = session.screen.line(index)
        number = re.search(r"\b3\b", line)
        self.assertIsNotNone(number, line)
        self.assertTrue(
            all(
                cell.foreground is not None
                for cell in session.screen.grid[index][number.start() : number.end()]
            ),
            "the session number has no colour",
        )
        for title, provider, color in (
            ("Orphaned Record Audit", "CLD", "yellow"),
            ("Matrix", "CDX", "cyan"),
        ):
            line_index = session.screen.find(title)
            line = session.screen.line(line_index)
            start = line.index(provider)
            expected, _ = SESSION_PALETTE[color]
            cells = session.screen.grid[line_index][start : start + len(provider)]
            self.assertTrue(all(cell.foreground == expected for cell in cells), line)

    def test_titles_carry_three_exact_identity_colours(self) -> None:
        session = self.open()
        fixtures = (
            ("Orphaned Record Audit", "red"),
            ("Matrix", "blue"),
            ("A session title long", "sea"),
        )
        seen = set()
        for title, color in fixtures:
            line_index = session.screen.find(title)
            self.assertGreaterEqual(line_index, 0, session.screen.text())
            line = session.screen.line(line_index)
            start = line.index(title)
            expected_index, expected_rgb = SESSION_PALETTE[color]
            cells = session.screen.grid[line_index][start : start + len(title)]
            self.assertTrue(cells)
            self.assertTrue(
                all(cell.foreground == expected_index for cell in cells),
                f"{title!r} is not colour {expected_index}: {line!r}",
            )
            self.assertEqual(expected_rgb, session.screen.palette.get(expected_index))
            seen.add(cells[0].foreground)
        self.assertEqual(3, len(seen))

    def test_selected_title_keeps_its_colour_over_reverse_video(self) -> None:
        session = self.open()
        title = "Orphaned Record Audit"
        line_index = session.screen.find(title)
        line = session.screen.line(line_index)
        start = line.index(title)
        expected, _ = SESSION_PALETTE["red"]
        cells = session.screen.grid[line_index][start : start + len(title)]
        self.assertTrue(all(cell.foreground == expected and cell.reverse for cell in cells))
        padding = session.screen.grid[line_index][start + len(title)]
        self.assertEqual(" ", padding.char, line)
        self.assertTrue(padding.reverse, line)
        self.assertNotEqual(expected, padding.foreground, line)

    def test_no_colour_reaches_a_terminal_that_asked_for_none(self) -> None:
        session = self.open(environ={"NO_COLOR": "1"})
        self.assertFalse(
            has_color_bytes(bytes(session.raw)),
            "a colour sequence reached a NO_COLOR terminal",
        )
        self.assertFalse(session.screen.any_color())
        self.assertEqual({}, session.screen.palette)
        # Still readable: the same runs keep their weight.
        self.assertGreaterEqual(session.screen.find("Orphaned Record Audit"), 0)

    def test_the_kit_s_own_switch_turns_colour_off_too(self) -> None:
        session = self.open(environ={"SESSION_KIT_NO_COLOR": "1"})
        self.assertFalse(has_color_bytes(bytes(session.raw)))


class AlignmentTests(TerminalCaseMixin, unittest.TestCase):
    def _provider_columns(self, screen) -> list[int]:
        return [
            match.start(1)
            for line in screen.lines()
            if (match := re.search(r"\| (CLD|CDX|SHL) \|", line))
        ]

    def test_every_session_row_puts_its_provider_in_the_same_column(self) -> None:
        session = self.open()
        columns = self._provider_columns(session.screen)
        self.assertGreaterEqual(len(columns), 3, session.screen.text())
        self.assertEqual(
            1,
            len(set(columns)),
            f"provider columns disagree: {columns}\n{session.screen.text()}",
        )

    def test_no_rendered_line_runs_past_the_terminal(self) -> None:
        session = self.open()
        for index, line in enumerate(session.screen.lines()):
            self.assertLessEqual(len(line), COLUMNS, f"line {index}: {line!r}")

    def test_a_title_too_long_for_its_column_is_cut_not_carried(self) -> None:
        session = self.open()
        index = session.screen.find("A session title long")
        self.assertGreaterEqual(index, 0)
        self.assertIn("…", session.screen.line(index))

    def test_one_long_title_does_not_cut_everyone_else_s_detail(self) -> None:
        session = self.open()
        index = session.screen.find("Orphaned Record Audit")
        self.assertGreaterEqual(index, 0)
        self.assertIn("opus-4.6", session.screen.line(index), session.screen.text())
        self.assertIn(
            "needs you | 2 subagents | now",
            session.screen.line(index),
        )
        matrix = session.screen.line(session.screen.find("Matrix"))
        self.assertIn("gpt-5-codex", matrix)
        # Both rows carry the one compact time column starting at the same
        # place; the nonzero count occupies its own aligned field before it.
        self.assertTrue(matrix.rstrip().endswith("| now"), matrix)
        self.assertEqual(
            matrix.rindex("| now"),
            session.screen.line(index).rindex("| now"),
        )

    def test_the_columns_hold_when_the_list_is_filtered(self) -> None:
        session = self.open()
        session.type(b"a")
        session.expect(lambda screen: screen.find("Filter: a") >= 0, what="the filter")
        session.settle()
        columns = self._provider_columns(session.screen)
        self.assertEqual(1, len(set(columns)), f"{columns}\n{session.screen.text()}")

    def test_account_state_and_age_fields_start_in_the_same_columns(self) -> None:
        session = self.open()
        starts = []
        for title in (
            "Orphaned Record Audit",
            "Matrix",
            "A session title long",
        ):
            line = session.screen.line(session.screen.find(title))
            # A blank last field leaves a visible trailing pipe after line()
            # removes spaces. The pipe cells themselves still define every
            # field boundary exactly as they do on a real terminal.
            separators = [index for index, character in enumerate(line) if character == "|"]
            self.assertGreaterEqual(len(separators), 5, line)
            starts.append(
                (
                    separators[1] + 2,
                    separators[3] + 2,
                    separators[4] + 2,
                )
            )
        self.assertEqual(1, len(set(starts)), f"field columns disagree: {starts}")


class InputTests(TerminalCaseMixin, unittest.TestCase):
    def test_a_letter_filters_and_escape_clears_it(self) -> None:
        session = self.open()
        session.type(b"m")
        session.expect(lambda screen: screen.find("Filter: m") >= 0, what="the filter")
        session.type(ESC)
        session.expect(
            lambda screen: screen.find("Filter:") < 0, what="the filter cleared"
        )
        self.assertGreaterEqual(session.screen.find("Orphaned Record Audit"), 0)

    def test_escape_on_an_empty_screen_leaves(self) -> None:
        session = self.open()
        session.type(b"m")
        session.expect(lambda screen: screen.find("Filter: m") >= 0, what="the filter")
        session.type(ESC)
        session.expect(lambda screen: screen.find("Filter:") < 0, what="the filter cleared")
        session.type(ESC)
        self.assertEqual(0, session.wait())

    def test_q_with_nothing_typed_leaves(self) -> None:
        session = self.open()
        session.type(b"q")
        self.assertEqual(0, session.wait())

    def test_q_while_filtering_is_an_ordinary_letter(self) -> None:
        session = self.open()
        session.type(b"a")
        session.expect(lambda screen: screen.find("Filter: a") >= 0, what="the filter")
        session.type(b"q")
        session.expect(lambda screen: screen.find("Filter: aq") >= 0, what="the letter q")

    def test_ctrl_d_leaves_from_anywhere(self) -> None:
        session = self.open()
        session.type(b"al")
        session.expect(lambda screen: screen.find("Filter: al") >= 0, what="the filter")
        session.type(CTRL_D)
        self.assertEqual(0, session.wait())

    def test_a_digit_marks_a_row_instead_of_filtering(self) -> None:
        session = self.open()
        session.type(b"3")
        session.expect(lambda screen: screen.find("Mark: 3") >= 0, what="the mark line")
        index = session.screen.find("Orphaned Record Audit")
        self.assertTrue(
            session.screen.line(index).startswith("✓"),
            f"row not ticked: {session.screen.line(index)!r}",
        )
        self.assertLess(session.screen.find("Filter:"), 0)

    def test_the_footer_names_the_way_out(self) -> None:
        session = self.open()
        index = session.screen.find("open #")
        self.assertIn("esc leave", session.screen.line(index))


class SecondaryPageTests(TerminalCaseMixin, unittest.TestCase):
    def one(self, **keywords) -> Session:
        return self.open(
            row(
                "Orphaned Record Audit",
                number=3,
                needs_you=True,
                account_alias="primary",
                model="opus-4.6",
                quiet_seconds=30,
                display_color="red",
            ),
            **keywords,
        )

    def move_to(self, session: Session, offset: int, heading: str) -> None:
        session.type(DOWN * offset + ENTER)
        session.expect(lambda screen: screen.line(0).strip() == heading, what=heading)
        session.settle()

    def assert_green(self, session: Session, line: int, text: str) -> None:
        rendered = session.screen.line(line)
        start = rendered.index(text)
        expected, _ = SESSION_PALETTE["green"]
        self.assertTrue(
            all(cell.foreground == expected for cell in session.screen.grid[line][start : start + len(text)]),
            rendered,
        )

    def test_help_is_the_old_picker_key_table_with_a_coloured_way_back(self) -> None:
        session = self.one()
        self.move_to(session, 4, "Picker help")
        for heading in ("Sessions", "Needs you", "The list", "Leaving"):
            self.assertGreaterEqual(session.screen.find(heading), 0, session.screen.text())
        rows = [session.screen.line(session.screen.find(key)) for key in ("Enter         Open", "number        Mark")]
        self.assertEqual(1, len({line.index("Open") if "Open" in line else line.index("Mark") for line in rows}))
        footer = session.screen.find("↵ back")
        self.assertGreaterEqual(footer, 0)
        self.assert_green(session, footer, "↵")
        self.assertIn("ctrl-d leave", session.screen.line(footer))

    def test_project_chooser_mirrors_the_numbered_old_project_step(self) -> None:
        session = self.one(projects=(("main", "/srv/project"),))
        self.move_to(session, 2, "Project")
        line = session.screen.find("main")
        self.assertIn("main", session.screen.line(line))
        self.assertIn("| /srv/project", session.screen.line(line))
        path = session.screen.line(line).index("/srv/project")
        self.assertTrue(all(cell.dim for cell in session.screen.grid[line][path : path + len("/srv/project")]))
        footer = session.screen.find("esc back")
        self.assert_green(session, footer, "esc")

    def test_a_conversation_with_no_transcript_is_not_offered_a_restore(self) -> None:
        """The screen offers a Restore, so it may not offer one that fails.

        This screen reads the ledger straight off disk and answered the
        question from the shape of the record -- a provider and a uuid -- so a
        conversation whose transcript is gone was listed with no mark and a
        Restore beside it. The other two surfaces had been taught to ask.
        """
        closed = ({
            "provider": "codex",
            "uuid": "00000000-0000-4000-8000-000000000004",
            "title": "Transcript Gone",
            "cwd": "/srv/project",
            "closed_at_unix_ms": 1,
            "account_alias": "spare",
        },)
        session = self.one(closed=closed)
        self.move_to(session, 3, "Closed sessions")
        line = session.screen.find("Transcript Gone")
        self.assertIn("history only", session.screen.line(line))
        session.type(ENTER)
        session.expect(lambda screen: screen.find("History") >= 0, what="closed actions")
        self.assertEqual(-1, session.screen.find("Restore"))
        self.assertGreaterEqual(
            session.screen.find("transcript is no longer on this machine"), 0
        )

    def test_closed_sessions_use_the_old_bracketed_provider_row(self) -> None:
        closed = ({
            "provider": "codex",
            "uuid": "00000000-0000-4000-8000-000000000003",
            "title": "Closed Matrix Audit",
            "cwd": "/srv/project",
            "closed_at_unix_ms": 1,
            "account_alias": "spare",
        },)
        session = self.one(
            closed=closed,
            transcripts=(("codex", "00000000-0000-4000-8000-000000000003"),),
        )
        self.move_to(session, 3, "Closed sessions")
        line = session.screen.find("Closed Matrix Audit")
        self.assertIn("[CDX] Closed Matrix Audit [login time unknown]", session.screen.line(line))
        start = session.screen.line(line).index("CDX")
        expected, _ = SESSION_PALETTE["cyan"]
        self.assertTrue(all(cell.foreground == expected for cell in session.screen.grid[line][start : start + 3]))
        footer = session.screen.find("↵ actions")
        self.assert_green(session, footer, "↵")
        self.assertIn("esc back", session.screen.line(footer))
        session.type(ENTER)
        # Wait for the whole page, not its first word: a loaded runner can
        # capture a frame where Restore has flushed and History has not.
        session.expect(
            lambda screen: screen.find("Restore") >= 0
            and screen.find("History") >= 0
            and screen.find("[CDX] login time unknown") >= 0,
            what="closed actions",
        )
        self.assertGreaterEqual(session.screen.find("History"), 0)
        subtitle = session.screen.find("[CDX] login time unknown")
        provider = session.screen.line(subtitle).index("CDX")
        self.assertTrue(
            all(
                cell.foreground == expected
                for cell in session.screen.grid[subtitle][provider : provider + 3]
            )
        )
        self.assertIn("esc back", session.screen.line(session.screen.find("↵ choose")))

    def test_action_and_close_flow_keep_identity_attention_and_back_roles(self) -> None:
        session = self.open(
            row(
                "Orphaned Record Audit",
                number=3,
                availability="attached",
                account_alias="primary",
                model="opus-4.6",
                display_color="red",
            )
        )
        session.type(b"3" + ENTER)
        session.expect(lambda screen: screen.find("Change account") >= 0, what="the action page")
        session.expect(lambda screen: screen.find("↵ choose") >= 0, what="the action footer")
        self.assertGreaterEqual(session.screen.find("Open elsewhere."), 0)
        self.assertGreaterEqual(session.screen.find("Move it here"), 0)
        title_line = session.screen.find("Orphaned Record Audit")
        title = session.screen.line(title_line)
        start = title.index("Orphaned Record Audit")
        expected_red, _ = SESSION_PALETTE["red"]
        self.assertTrue(all(cell.foreground == expected_red for cell in session.screen.grid[title_line][start : start + 21]))
        close_line = session.screen.find("everything inside will end")
        self.assertIn("| the shell and everything inside will end", session.screen.line(close_line))
        close_start = session.screen.line(close_line).index("Close")
        expected_yellow, _ = SESSION_PALETTE["yellow"]
        self.assertTrue(all(cell.foreground == expected_yellow for cell in session.screen.grid[close_line][close_start : close_start + 5]))
        footer = session.screen.find("↵ choose")
        self.assert_green(session, footer, "↵")
        self.assertIn("esc back", session.screen.line(footer))

    def test_rename_prompt_mirrors_the_old_prompt_and_names_back(self) -> None:
        session = self.one()
        session.type(b"3" + ENTER + DOWN * 3 + ENTER)
        session.expect(lambda screen: screen.find("New name (↵ back) ❯") >= 0, what="rename")
        line = session.screen.find("New name")
        self.assert_green(session, line, "New name (↵ back) ❯")
        self.assertIn("esc back", session.screen.line(session.screen.find("↵ choose")))

    def test_account_model_and_color_choosers_share_the_picker_face(self) -> None:
        for offset, heading, choice in (
            (1, "Change to account", "primary"),
            (2, "Change model", "claude-opus-5"),
            (4, "Color", "red"),
        ):
            with self.subTest(heading=heading):
                session = self.one()
                session.type(b"3" + ENTER + DOWN * offset + ENTER)
                # A PTY read can split one atomic repaint after its title.
                # Wait for the chosen panel's body as well, rather than
                # inspecting the previous action list under the new title.
                session.expect(
                    lambda screen, h=heading, c=choice: (
                        screen.line(0).strip() == h and screen.find(c) >= 0
                    ),
                    what=heading,
                )
                line = session.screen.find(choice)
                self.assertGreaterEqual(line, 0, session.screen.text())
                self.assertIn("↵ choose", session.screen.line(session.screen.find("↵ choose")))
                number = re.search(r"\b1\b", session.screen.line(line))
                self.assertIsNotNone(number, session.screen.line(line))
                self.assert_green(session, line, number.group())

    def test_new_session_provider_page_uses_old_names_and_provider_colours(self) -> None:
        session = self.one()
        self.move_to(session, 1, "New session")
        for label, color in (("Claude Code", "yellow"), ("Codex", "cyan"), ("Shell", "green")):
            line = session.screen.find(label)
            start = session.screen.line(line).index(label)
            expected, _ = SESSION_PALETTE[color]
            self.assertTrue(all(cell.foreground == expected for cell in session.screen.grid[line][start : start + len(label)]))
        self.assertIn("esc back", session.screen.line(session.screen.find("↵ choose")))

    def test_success_and_refusal_messages_have_distinct_colour_roles(self) -> None:
        success = self.one()
        success.type(b"3" + ENTER + ENTER)
        success.expect(lambda screen: screen.find("Closed Orphaned Record Audit.") >= 0, what="close result")
        line = success.screen.find("Closed Orphaned Record Audit.")
        self.assert_green(success, line, "Closed Orphaned Record Audit.")

        refused = self.one(fail_commands=("picker-close",))
        refused.type(b"3" + ENTER + ENTER)
        refused.expect(lambda screen: screen.find("Close was refused. Nothing changed.") >= 0, what="refusal")
        line = refused.screen.find("Close was refused. Nothing changed.")
        start = refused.screen.line(line).index("Close")
        expected, _ = SESSION_PALETTE["yellow"]
        self.assertTrue(all(cell.foreground == expected for cell in refused.screen.grid[line][start : start + len("Close was refused. Nothing changed.")]))

    def test_secondary_pages_emit_no_colour_when_asked_for_none(self) -> None:
        session = self.one(environ={"NO_COLOR": "1"})
        self.move_to(session, 4, "Picker help")
        self.assertFalse(has_color_bytes(bytes(session.raw)))
        self.assertFalse(session.screen.any_color())


class PasteTests(TerminalCaseMixin, unittest.TestCase):
    def test_a_pasted_close_command_closes_nothing(self) -> None:
        session = self.open()
        session.type(b"k 17\n")
        session.settle(1.0)
        self.assertEqual([], session.sp_calls(), "a paste reached a session command")
        self.assertGreaterEqual(session.screen.find("Filter: k 17"), 0, session.screen.text())
        session.type(CTRL_D)
        self.assertEqual(0, session.wait())

    def test_a_pasted_number_run_marks_and_runs_nothing_on_its_own(self) -> None:
        session = self.open()
        session.type(b"3,17")
        session.expect(lambda screen: screen.find("Mark: 3,17") >= 0, what="the marks")
        self.assertEqual([], session.sp_calls())
        session.type(CTRL_D)
        self.assertEqual(0, session.wait())


class GhosttyColorTests(ColorTests):
    term = "xterm-ghostty"


class GhosttyAlignmentTests(AlignmentTests):
    term = "xterm-ghostty"


class GhosttyInputTests(InputTests):
    term = "xterm-ghostty"


class GhosttyPasteTests(PasteTests):
    term = "xterm-ghostty"


class GhosttySecondaryPageTests(SecondaryPageTests):
    term = "xterm-ghostty"


if __name__ == "__main__":
    unittest.main()
