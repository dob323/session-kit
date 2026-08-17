#!/usr/bin/env python3
"""Render raw terminal journals into settled plain text.

The kit's journals hold the RAW bytes a Claude Code TUI wrote to its pty
(`script -qfa`-style capture). A TUI repaints lines in place: it moves the
cursor up, jumps to a column, overwrites a few characters, and moves on.
Played back as plain text with the escapes merely stripped, two drafts of the
same screen line braid into mush ("WidgetiSampledTwohwasiflaggedrbyua").

The fix is to replay the recording through a real simulated screen, obeying
every cursor and erase instruction, and to emit a line only once it has
SETTLED -- that is, once it has scrolled off the top of the screen and can no
longer be repainted. Rows still on the live screen are never settled; they
persist in the checkpoint and are re-rendered on the next pass.

Everything here is stdlib only. The renderer is incremental: feed it any
chunking of the same byte stream and the settled output is byte identical.

CLI
---
    python3 journal_render.py render --journal DIR_OR_FILE \\
        --out sidecar.txt --state state.json [--max-bytes N]

    python3 journal_render.py flush  ...same flags...

`render` appends newly settled lines to the sidecar and commits progress to
the state file. `flush` does the same and then appends the current live screen
as a trailing block behind a marker line, WITHOUT settling it -- read-time use,
so history includes the newest screen. The live block is not committed, so the
next run truncates it away before appending.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

# Journals do not record window resizes, so the replay picks a fixed geometry.
# A generous width minimises wrap artifacts on the wide TUI frames Claude Code
# draws; anything narrower than the recording window would fold real lines.
DEFAULT_WIDTH = 220
DEFAULT_HEIGHT = 60

CHECKPOINT_VERSION = 1
STATE_VERSION = 1
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
READ_CHUNK = 1 << 20

LIVE_MARKER_PREFIX = "── live screen at "
LIVE_MARKER_SUFFIX = " ──"

# An escape sequence held across a feed() boundary. 64 KiB is far past any real
# OSC string; beyond it the bytes are treated as garbage and swallowed rather
# than leaked into the text.
MAX_PENDING = 1 << 16

# Ordered alternation: CSI and the string-terminated families must win over the
# single-character branch, whose class therefore excludes their introducers
# ('[' ']' 'P' 'X' '^' '_').
_ESCAPE_RE = re.compile(
    rb"\x1b(?:"
    rb"\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"
    rb"|\][^\x07\x1b]*(?:\x07|\x1b\\)"
    rb"|[PX\x5e_][^\x1b\x07]*(?:\x07|\x1b\\)"
    rb"|[\x20-\x2f]+[\x30-\x7e]"
    rb"|[\x30-\x4f\x51-\x57\x59\x5a\x5c\x60-\x7e]"
    rb")"
)

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_STRING_INTRODUCERS = frozenset(b"]PX^_")
_ALT_SCREEN_MODES = frozenset((47, 1047, 1049))


def _clamp(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _incomplete_escape(data: bytes, start: int) -> bool:
    """True when data[start:] could still grow into a valid escape sequence."""
    rest = data[start:]
    if len(rest) == 1:
        return True
    kind = rest[1]
    if kind == 0x5B:  # CSI: parameters and intermediates, final not yet seen
        return all(0x20 <= byte <= 0x3F for byte in rest[2:])
    if kind in _STRING_INTRODUCERS:  # OSC/DCS/SOS/PM/APC: waiting for ST
        return True
    if 0x20 <= kind <= 0x2F:  # nF escape: waiting for its final byte
        return all(0x20 <= byte <= 0x2F for byte in rest[2:])
    return False


def _utf8_tail(chunk: bytes) -> int:
    """Length of the trailing bytes that form an incomplete UTF-8 sequence."""
    size = len(chunk)
    limit = 4 if size >= 4 else size
    for back in range(1, limit + 1):
        byte = chunk[size - back]
        if byte < 0x80:
            return 0
        if byte >= 0xC0:
            if byte < 0xE0:
                need = 2
            elif byte < 0xF0:
                need = 3
            else:
                need = 4
            return back if back < need else 0
    return 0


class JournalRenderer:
    """An incremental VT replay that emits only settled screen lines."""

    def __init__(self, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT):
        if width < 2 or height < 2:
            raise ValueError("screen must be at least 2x2")
        self.width = width
        self.height = height
        self._blank = " " * width
        self._grid = [self._blank] * height
        self._row = 0
        self._col = 0
        self._top = 0
        self._bottom = height - 1
        self._saved: tuple[int, int] | None = None
        self._alt = False
        self._alt_backup: tuple[list[str], int, int] | None = None
        self._pending = b""
        self._settled: list[str] = []
        self.settled_lines = 0
        self.bytes_fed = 0

    # ---------------------------------------------------------------- state

    @classmethod
    def resume(cls, checkpoint: dict | None) -> "JournalRenderer":
        """Rebuild a renderer from checkpoint() output (None starts fresh)."""
        if not checkpoint:
            return cls()
        version = checkpoint.get("version")
        if version != CHECKPOINT_VERSION:
            raise ValueError(f"unsupported checkpoint version: {version!r}")
        width = int(checkpoint.get("width", DEFAULT_WIDTH))
        height = int(checkpoint.get("height", DEFAULT_HEIGHT))
        self = cls(width=width, height=height)
        self._grid = self._restore_rows(checkpoint.get("rows"), height, width)
        cursor = checkpoint.get("cursor") or {}
        self._row = _clamp(int(cursor.get("row", 0)), 0, height - 1)
        self._col = _clamp(int(cursor.get("col", 0)), 0, width)
        saved = checkpoint.get("saved_cursor")
        if saved:
            self._saved = (
                _clamp(int(saved[0]), 0, height - 1),
                _clamp(int(saved[1]), 0, width),
            )
        region = checkpoint.get("scroll_region") or [0, height - 1]
        self._top = _clamp(int(region[0]), 0, height - 1)
        self._bottom = _clamp(int(region[1]), self._top, height - 1)
        self._alt = bool(checkpoint.get("alt_screen"))
        backup = checkpoint.get("alt_backup")
        if backup:
            self._alt_backup = (
                self._restore_rows(backup.get("rows"), height, width),
                _clamp(int(backup.get("row", 0)), 0, height - 1),
                _clamp(int(backup.get("col", 0)), 0, width),
            )
        pending = checkpoint.get("pending") or ""
        self._pending = bytes.fromhex(pending) if pending else b""
        self.settled_lines = int(checkpoint.get("settled_lines", 0))
        self.bytes_fed = int(checkpoint.get("bytes_fed", 0))
        return self

    @staticmethod
    def _restore_rows(rows, height: int, width: int) -> list[str]:
        blank = " " * width
        grid = [blank] * height
        for index, text in enumerate(rows or []):
            if index >= height:
                break
            line = str(text)[:width]
            grid[index] = line + " " * (width - len(line))
        return grid

    def checkpoint(self) -> dict:
        """A JSON-safe snapshot; feed()ing on top of resume() is seamless."""
        state = {
            "version": CHECKPOINT_VERSION,
            "width": self.width,
            "height": self.height,
            # Trailing blanks are invisible to every reader (settled lines and
            # live rows are both rstripped), so they are dropped here.
            "rows": [row.rstrip() for row in self._grid],
            "cursor": {"row": self._row, "col": self._col},
            "saved_cursor": list(self._saved) if self._saved else None,
            "scroll_region": [self._top, self._bottom],
            "alt_screen": self._alt,
            "alt_backup": None,
            "pending": self._pending.hex(),
            "settled_lines": self.settled_lines,
            "bytes_fed": self.bytes_fed,
        }
        if self._alt_backup is not None:
            rows, row, col = self._alt_backup
            state["alt_backup"] = {
                "rows": [line.rstrip() for line in rows],
                "row": row,
                "col": col,
            }
        return state

    # --------------------------------------------------------------- output

    def take_settled(self) -> list[str]:
        """Drain the lines that have scrolled off the screen since last call."""
        drained = self._settled
        self._settled = []
        return drained

    def live_rows(self) -> list[str]:
        """Non-empty rows still on the live screen (never settled)."""
        rows = [row.rstrip() for row in self._grid]
        while rows and not rows[-1]:
            rows.pop()
        return rows

    # ---------------------------------------------------------------- input

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self.bytes_fed += len(data)
        if self._pending:
            data = self._pending + data
            self._pending = b""
        index = 0
        size = len(data)
        find = data.find
        match_escape = _ESCAPE_RE.match
        while index < size:
            hit = find(0x1B, index)
            if hit == -1:
                run = data[index:]
                tail = _utf8_tail(run)
                if tail:
                    self._pending = run[len(run) - tail :]
                    run = run[: len(run) - tail]
                if run:
                    self._put(run.decode("utf-8", "replace"))
                return
            if hit > index:
                self._put(data[index:hit].decode("utf-8", "replace"))
                index = hit
            found = match_escape(data, index)
            if found is None:
                if _incomplete_escape(data, index):
                    rest = data[index:]
                    if len(rest) <= MAX_PENDING:
                        self._pending = rest
                    return
                index += 1  # stray ESC: swallow the byte, never leak it
                continue
            self._dispatch(data[index : found.end()])
            index = found.end()

    # ------------------------------------------------------------ plain text

    def _put(self, text: str) -> None:
        position = 0
        for found in _CONTROL_RE.finditer(text):
            start = found.start()
            if start > position:
                self._write(text[position:start])
            self._control(found.group())
            position = found.end()
        if position < len(text):
            self._write(text[position:])

    def _write(self, chunk: str) -> None:
        width = self.width
        grid = self._grid
        while chunk:
            if self._col >= width:
                self._col = 0
                self._index()
            col = self._col
            part = chunk[: width - col]
            chunk = chunk[len(part) :]
            row = self._row
            line = grid[row]
            grid[row] = line[:col] + part + line[col + len(part) :]
            self._col = col + len(part)

    def _control(self, char: str) -> None:
        if char == "\n":
            self._index()
        elif char == "\r":
            self._col = 0
        elif char == "\b":
            if self._col > 0:
                self._col -= 1
        elif char == "\t":
            self._col = min(self.width - 1, (self._col // 8 + 1) * 8)
        elif char in ("\x0b", "\x0c"):
            self._index()
        # BEL, NUL, SO/SI and friends have no effect on plain-text output.

    # --------------------------------------------------------------- screen

    def _index(self) -> None:
        if self._row == self._bottom:
            self._scroll_up(1)
        elif self._row < self.height - 1:
            self._row += 1

    def _reverse_index(self) -> None:
        if self._row == self._top:
            self._scroll_down(1)
        elif self._row > 0:
            self._row -= 1

    def _scroll_up(self, count: int) -> None:
        grid = self._grid
        top = self._top
        bottom = self._bottom
        settles = top == 0 and not self._alt
        for _ in range(min(count, bottom - top + 1)):
            line = grid[top]
            if settles:
                self._settled.append(line.rstrip())
                self.settled_lines += 1
            del grid[top]
            grid.insert(bottom, self._blank)

    def _scroll_down(self, count: int) -> None:
        grid = self._grid
        top = self._top
        bottom = self._bottom
        for _ in range(min(count, bottom - top + 1)):
            del grid[bottom]
            grid.insert(top, self._blank)

    def _erase_row(self, row: int, start: int, end: int) -> None:
        line = self._grid[row]
        start = _clamp(start, 0, self.width)
        end = _clamp(end, start, self.width)
        if end > start:
            self._grid[row] = line[:start] + " " * (end - start) + line[end:]

    # ------------------------------------------------------------- dispatch

    def _dispatch(self, sequence: bytes) -> None:
        kind = sequence[1]
        if kind == 0x5B:  # CSI
            final = sequence[-1]
            if final == 0x6D:  # SGR: colour only, irrelevant to plain text
                return
            self._csi(sequence, final)
            return
        if kind in _STRING_INTRODUCERS:  # OSC/DCS/SOS/PM/APC: swallowed whole
            return
        if kind == 0x37:  # ESC 7
            self._saved = (self._row, self._col)
        elif kind == 0x38:  # ESC 8
            self._restore_cursor()
        elif kind == 0x44:  # ESC D  index
            self._index()
        elif kind == 0x4D:  # ESC M  reverse index
            self._reverse_index()
        elif kind == 0x45:  # ESC E  next line
            self._col = 0
            self._index()
        elif kind == 0x63:  # ESC c  hard reset
            self._reset()
        # Charset designations (ESC ( B), keypad modes and the rest carry no
        # text, so they are swallowed.

    def _restore_cursor(self) -> None:
        if self._saved is not None:
            self._row, self._col = self._saved
            self._row = _clamp(self._row, 0, self.height - 1)
            self._col = _clamp(self._col, 0, self.width)

    def _private_mode(self, body: bytes, enable: bool) -> None:
        for value in self._params(body):
            if value in _ALT_SCREEN_MODES:
                if enable:
                    self._enter_alt()
                else:
                    self._exit_alt()
        # Every other private mode (cursor visibility, bracketed paste,
        # synchronised output) leaves the text untouched.

    def _enter_alt(self) -> None:
        """Alt-screen content is a transient overlay: it never settles."""
        if self._alt:
            return
        self._alt_backup = (list(self._grid), self._row, self._col)
        self._alt = True
        self._grid = [self._blank] * self.height
        self._row = 0
        self._col = 0

    def _exit_alt(self) -> None:
        if not self._alt:
            return
        self._alt = False
        if self._alt_backup is not None:
            grid, row, col = self._alt_backup
            self._grid = list(grid)
            self._row = _clamp(row, 0, self.height - 1)
            self._col = _clamp(col, 0, self.width)
            self._alt_backup = None

    def _reset(self) -> None:
        self._grid = [self._blank] * self.height
        self._row = 0
        self._col = 0
        self._top = 0
        self._bottom = self.height - 1
        self._saved = None

    def _csi(self, sequence: bytes, final: int) -> None:
        body = sequence[2:-1]
        while body and 0x20 <= body[-1] <= 0x2F:  # drop intermediates
            body = body[:-1]
        if body[:1] in (b"?", b"<", b"=", b">"):
            if body[:1] == b"?" and final in (0x68, 0x6C):
                self._private_mode(body[1:], final == 0x68)
            return
        params = self._params(body)
        first = params[0] if params else 0

        if final == 0x43:  # C  cursor forward
            self._col = min(self.width - 1, self._col + max(1, first))
        elif final == 0x44:  # D  cursor back
            self._col = max(0, self._col - max(1, first))
        elif final == 0x41:  # A  cursor up
            limit = self._top if self._row >= self._top else 0
            self._row = max(limit, self._row - max(1, first))
        elif final == 0x42:  # B  cursor down
            limit = self._bottom if self._row <= self._bottom else self.height - 1
            self._row = min(limit, self._row + max(1, first))
        elif final in (0x47, 0x60):  # G / `  cursor to column
            self._col = _clamp(max(1, first) - 1, 0, self.width - 1)
        elif final == 0x64:  # d  cursor to row
            self._row = _clamp(max(1, first) - 1, 0, self.height - 1)
        elif final in (0x48, 0x66):  # H / f  cursor position
            second = params[1] if len(params) > 1 else 0
            self._row = _clamp(max(1, first) - 1, 0, self.height - 1)
            self._col = _clamp(max(1, second) - 1, 0, self.width - 1)
        elif final == 0x4B:  # K  erase in line
            if first == 0:
                self._erase_row(self._row, self._col, self.width)
            elif first == 1:
                self._erase_row(self._row, 0, self._col + 1)
            else:
                self._erase_row(self._row, 0, self.width)
        elif final == 0x4A:  # J  erase in display
            self._erase_display(first)
        elif final == 0x45:  # E  cursor next line
            self._col = 0
            for _ in range(max(1, first)):
                self._index()
        elif final == 0x46:  # F  cursor previous line
            self._col = 0
            for _ in range(max(1, first)):
                self._reverse_index()
        elif final == 0x4C:  # L  insert lines
            self._insert_lines(max(1, first))
        elif final == 0x4D:  # M  delete lines
            self._delete_lines(max(1, first))
        elif final == 0x40:  # @  insert characters
            self._insert_chars(max(1, first))
        elif final == 0x50:  # P  delete characters
            self._delete_chars(max(1, first))
        elif final == 0x58:  # X  erase characters
            self._erase_row(self._row, self._col, self._col + max(1, first))
        elif final == 0x53:  # S  scroll up
            self._scroll_up(max(1, first))
        elif final == 0x54:  # T  scroll down
            self._scroll_down(max(1, first))
        elif final == 0x72:  # r  set scrolling region
            second = params[1] if len(params) > 1 else 0
            top = _clamp(max(1, first) - 1, 0, self.height - 1)
            bottom = _clamp((second or self.height) - 1, top, self.height - 1)
            self._top = top
            self._bottom = bottom
            self._row = top
            self._col = 0
        elif final == 0x73:  # s  save cursor
            self._saved = (self._row, self._col)
        elif final == 0x75:  # u  restore cursor
            self._restore_cursor()
        # Everything else (device reports, SGR handled above, tab stops) has no
        # effect on the plain text this renderer produces.

    @staticmethod
    def _params(body: bytes) -> list[int]:
        if not body:
            return []
        values = []
        for piece in body.split(b";"):
            piece = piece.split(b":")[0]
            try:
                values.append(int(piece) if piece else 0)
            except ValueError:
                values.append(0)
        return values

    def _erase_display(self, mode: int) -> None:
        if mode == 0:
            self._erase_row(self._row, self._col, self.width)
            for row in range(self._row + 1, self.height):
                self._grid[row] = self._blank
        elif mode == 1:
            for row in range(0, self._row):
                self._grid[row] = self._blank
            self._erase_row(self._row, 0, self._col + 1)
        else:
            # A full clear settles nothing: the screen is discarded, not
            # scrolled into history.
            self._grid = [self._blank] * self.height

    def _insert_lines(self, count: int) -> None:
        if not (self._top <= self._row <= self._bottom):
            return
        grid = self._grid
        for _ in range(min(count, self._bottom - self._row + 1)):
            del grid[self._bottom]
            grid.insert(self._row, self._blank)

    def _delete_lines(self, count: int) -> None:
        if not (self._top <= self._row <= self._bottom):
            return
        grid = self._grid
        for _ in range(min(count, self._bottom - self._row + 1)):
            del grid[self._row]
            grid.insert(self._bottom, self._blank)

    def _insert_chars(self, count: int) -> None:
        line = self._grid[self._row]
        col = self._col
        count = min(count, self.width - col)
        self._grid[self._row] = (
            line[:col] + " " * count + line[col : self.width - count]
        )

    def _delete_chars(self, count: int) -> None:
        line = self._grid[self._row]
        col = self._col
        count = min(count, self.width - col)
        self._grid[self._row] = line[:col] + line[col + count :] + " " * count


# ----------------------------------------------------------------- journals


def journal_segments(journal: Path) -> list[Path]:
    """Ordered raw segments: a directory of segment-*.raw, or a single file."""
    journal = Path(journal)
    if journal.is_dir():
        return sorted(journal.glob("segment-*.raw"))
    if journal.is_file():
        return [journal]
    return []


def _load_state(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        return None
    return state


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _head_digest(segments: list[Path]) -> str:
    """Fingerprint the start of the journal so a same-size rewrite is caught."""
    if not segments:
        return ""
    try:
        with open(segments[0], "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return ""
    return hashlib.sha256(head).hexdigest()


def _plan(segments: list[Path], state: dict | None) -> tuple[dict, bool]:
    """Match stored progress against what is on disk now.

    Returns the per-segment consumed map and whether the journal has to be
    re-rendered from the start (a segment shrank, the head bytes changed, or
    the sequence diverged -- rotation, truncation, or a different journal
    reusing the state file).
    """
    consumed = {}
    restart = False
    stored_head = (state or {}).get("head")
    if stored_head and stored_head != _head_digest(segments):
        restart = True
    stored = (state or {}).get("segments") or []
    stored_map = {}
    for entry in stored:
        if isinstance(entry, dict) and "name" in entry:
            stored_map[str(entry["name"])] = int(entry.get("consumed", 0))
    names = [segment.name for segment in segments]
    for index, entry in enumerate(stored):
        if not isinstance(entry, dict):
            restart = True
            break
        if index >= len(names) or names[index] != str(entry.get("name")):
            restart = True
            break
    for segment in segments:
        done = stored_map.get(segment.name, 0)
        try:
            size = segment.stat().st_size
        except OSError:
            size = 0
        if done > size:
            restart = True
        consumed[segment.name] = 0 if restart else min(done, size)
    if restart:
        consumed = {segment.name: 0 for segment in segments}
    return consumed, restart


def render_journal(
    journal: Path,
    out_path: Path,
    state_path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    flush_live: bool = False,
    now: str | None = None,
) -> dict:
    """Render new journal bytes into the sidecar and commit progress."""
    journal = Path(journal)
    out_path = Path(out_path)
    state_path = Path(state_path)
    segments = journal_segments(journal)
    state = _load_state(state_path)
    if state and state.get("journal") != str(journal):
        state = None
    consumed, restart = _plan(segments, state)

    if restart or state is None:
        renderer = JournalRenderer(width=width, height=height)
        committed = 0
    else:
        checkpoint = state.get("renderer")
        try:
            renderer = JournalRenderer.resume(checkpoint)
        except (ValueError, TypeError, KeyError):
            renderer = JournalRenderer(width=width, height=height)
            consumed = {segment.name: 0 for segment in segments}
            restart = True
            committed = 0
        else:
            committed = int(state.get("sidecar_bytes", 0))
    if restart:
        committed = 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Truncating to the committed length makes the append idempotent: it drops
    # a half-written batch from a crashed run and any live block a previous
    # flush appended past the commit point.
    with open(out_path, "a+b") as handle:
        if handle.tell() > committed:
            handle.truncate(committed)

    started = time.monotonic()
    read_bytes = 0
    budget = max(0, int(max_bytes))
    lines_written = 0

    with open(out_path, "ab") as sink:
        for segment in segments:
            if read_bytes >= budget:
                break
            offset = consumed.get(segment.name, 0)
            try:
                size = segment.stat().st_size
            except OSError:
                continue
            if offset >= size:
                continue
            with open(segment, "rb") as source:
                source.seek(offset)
                while read_bytes < budget:
                    want = min(READ_CHUNK, budget - read_bytes)
                    block = source.read(want)
                    if not block:
                        break
                    read_bytes += len(block)
                    offset += len(block)
                    renderer.feed(block)
                    settled = renderer.take_settled()
                    if settled:
                        lines_written += len(settled)
                        sink.write(("\n".join(settled) + "\n").encode("utf-8"))
            consumed[segment.name] = offset
        settled = renderer.take_settled()
        if settled:
            lines_written += len(settled)
            sink.write(("\n".join(settled) + "\n").encode("utf-8"))
        sink.flush()
        os.fsync(sink.fileno())
        # fstat rather than tell(): append-mode position is only guaranteed to
        # sit at end-of-file after a write, and a no-op run writes nothing.
        committed = os.fstat(sink.fileno()).st_size

    elapsed = time.monotonic() - started
    total_offset = sum(consumed.get(segment.name, 0) for segment in segments)
    new_state = {
        "version": STATE_VERSION,
        "journal": str(journal),
        "segments": [
            {"name": segment.name, "consumed": consumed.get(segment.name, 0)}
            for segment in segments
        ],
        "head": _head_digest(segments),
        "byte_offset": total_offset,
        "sidecar_bytes": committed,
        "renderer": renderer.checkpoint(),
        "updated_at": now or _datetime.datetime.now().astimezone().isoformat(),
    }
    _save_state(state_path, new_state)

    live_lines = 0
    if flush_live:
        rows = renderer.live_rows()
        stamp = now or _datetime.datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        live_block = [LIVE_MARKER_PREFIX + stamp + LIVE_MARKER_SUFFIX] + rows
        live_lines = len(rows)
        with open(out_path, "ab") as sink:
            sink.write(("\n".join(live_block) + "\n").encode("utf-8"))
            sink.flush()
            os.fsync(sink.fileno())

    return {
        "journal": str(journal),
        "segments": len(segments),
        "restarted": restart,
        "bytes_read": read_bytes,
        "byte_offset": total_offset,
        "settled_lines": lines_written,
        "live_lines": live_lines,
        "sidecar_bytes": committed,
        "seconds": round(elapsed, 4),
        "bytes_per_second": int(read_bytes / elapsed) if elapsed > 0 else 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="journal_render.py",
        description="Replay a raw terminal journal into settled plain text.",
    )
    parser.add_argument("mode", choices=("render", "flush"))
    parser.add_argument("--journal", required=True, help="segment dir or .raw file")
    parser.add_argument("--out", required=True, help="sidecar text file to append")
    parser.add_argument("--state", required=True, help="progress checkpoint JSON")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    args = parser.parse_args(argv)

    journal = Path(args.journal)
    if not journal_segments(journal):
        print(f"no journal segments under {journal}", file=sys.stderr)
        return 2
    result = render_journal(
        journal,
        Path(args.out),
        Path(args.state),
        max_bytes=args.max_bytes,
        width=args.width,
        height=args.height,
        flush_live=args.mode == "flush",
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
