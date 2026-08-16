"""Tests for the journal VT renderer.

The renderer exists because a raw TUI capture played back as plain text
braids repainted drafts of the same screen line into mush. The braid case at
the bottom of this file reproduces an artifact of exactly that shape
("WidgetiSampledTwohwasiflaggedrbyua") from the mechanism that produced it,
and asserts the renderer reads back clean.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import REPO


MODULE_PATH = REPO / "lib" / "sessionkit_inventory" / "journal_render.py"
_SPEC = importlib.util.spec_from_file_location("journal_render", MODULE_PATH)
journal_render = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(journal_render)

JournalRenderer = journal_render.JournalRenderer

# The flattener the renderer replaces: strip the escapes, keep every byte the
# terminal was told to draw. Used as the negative control in the braid tests.
NAIVE_STRIP = re.compile(
    rb"\x1b\[[0-9;?]*[ -/]*[@-~]"
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    rb"|\x1b."
)


def naive_text(stream: bytes) -> str:
    return NAIVE_STRIP.sub(b"", stream).decode("utf-8", "replace")


def render(stream: bytes, width: int = 40, height: int = 8) -> list[str]:
    """Everything a reader sees: settled history plus the live screen."""
    renderer = JournalRenderer(width=width, height=height)
    renderer.feed(stream)
    return renderer.take_settled() + renderer.live_rows()


def render_chunked(stream: bytes, size: int, width: int = 40, height: int = 8):
    renderer = JournalRenderer(width=width, height=height)
    settled = []
    for start in range(0, len(stream), size):
        renderer.feed(stream[start : start + size])
        settled.extend(renderer.take_settled())
    return settled, renderer.live_rows()


class ScreenBehaviourTests(unittest.TestCase):
    def test_repainted_line_keeps_only_the_final_draft(self):
        stream = (
            b"keep me\r\n"
            b"draft one\r\x1b[K"
            b"draft two is longer\r\x1b[K"
            b"final draft\r\n"
        )
        lines = render(stream)
        self.assertIn("final draft", lines)
        self.assertNotIn("draft one", lines)
        self.assertNotIn("draft two is longer", lines)
        # The naive flattener is what braids: every draft survives in it.
        self.assertIn("draft one", naive_text(stream))

    def test_cursor_up_repaint_rewrites_an_earlier_line(self):
        stream = (
            b"alpha\r\n"
            b"bravo\r\n"
            b"charlie\r\n"
            b"\x1b[2A\r\x1b[Kbravo repainted\r\n"
        )
        lines = render(stream)
        self.assertEqual(["alpha", "bravo repainted", "charlie"], lines)

    def test_partial_overwrite_leaves_the_untouched_tail(self):
        stream = b"abcdefghij\r\x1b[3CXY\r\n"
        self.assertEqual(["abcXYfghij"], render(stream))

    def test_erase_display_two_clears_and_settles_nothing(self):
        stream = b"gone one\r\ngone two\r\n\x1b[2J\x1b[Hkept\r\n"
        lines = render(stream)
        self.assertEqual(["kept"], lines)

    def test_erase_display_zero_clears_below_the_cursor(self):
        stream = b"one\r\ntwo\r\nthree\r\n\x1b[2;1H\x1b[0Jrewritten\r\n"
        self.assertEqual(["one", "rewritten"], render(stream))

    def test_alt_screen_excursion_never_reaches_the_output(self):
        stream = (
            b"before the picker\r\n"
            b"\x1b[?1049h"
            b"PICKER ROW ONE\r\nPICKER ROW TWO\r\n" + b"filler\r\n" * 20 +
            b"\x1b[?1049l"
            b"after the picker\r\n"
        )
        lines = render(stream)
        self.assertIn("before the picker", lines)
        self.assertIn("after the picker", lines)
        for line in lines:
            self.assertNotIn("PICKER", line)
            self.assertNotIn("filler", line)

    def test_alt_screen_restores_the_main_screen_underneath(self):
        stream = b"main row\r\n\x1b[?1049hoverlay\x1b[?1049l\x1b[10;1Hbelow\r\n"
        lines = render(stream)
        self.assertIn("main row", lines)
        self.assertNotIn("overlay", "".join(lines))

    def test_scrolled_lines_settle_in_order(self):
        stream = b"".join(f"line {index}\r\n".encode() for index in range(20))
        renderer = JournalRenderer(width=40, height=8)
        renderer.feed(stream)
        settled = renderer.take_settled()
        self.assertEqual(["line %d" % index for index in range(13)], settled)
        self.assertEqual(
            ["line %d" % index for index in range(13, 20)], renderer.live_rows()
        )

    def test_live_rows_are_not_settled(self):
        renderer = JournalRenderer(width=40, height=8)
        renderer.feed(b"still being drawn")
        self.assertEqual([], renderer.take_settled())
        self.assertEqual(["still being drawn"], renderer.live_rows())

    def test_wrap_at_width(self):
        stream = b"x" * 45 + b"\r\n"
        self.assertEqual(["x" * 40, "x" * 5], render(stream, width=40))

    def test_scroll_region_lines_do_not_settle(self):
        stream = b"\x1b[3;6r\x1b[3;1H" + b"".join(
            f"region {index}\r\n".encode() for index in range(12)
        )
        renderer = JournalRenderer(width=40, height=8)
        renderer.feed(stream)
        self.assertEqual([], renderer.take_settled())

    def test_insert_and_delete_characters(self):
        self.assertEqual(["ab  cdef"], render(b"abcdef\r\x1b[3G\x1b[2@"))
        self.assertEqual(["abef"], render(b"abcdef\r\x1b[3G\x1b[2P"))

    def test_insert_and_delete_lines(self):
        stream = b"one\r\ntwo\r\nthree\r\n\x1b[2;1H\x1b[L"
        renderer = JournalRenderer(width=40, height=8)
        renderer.feed(stream)
        self.assertEqual(["one", "", "two", "three"], renderer.live_rows())
        renderer = JournalRenderer(width=40, height=8)
        renderer.feed(b"one\r\ntwo\r\nthree\r\n\x1b[2;1H\x1b[M")
        self.assertEqual(["one", "three"], renderer.live_rows())

    def test_tabs_and_backspace(self):
        self.assertEqual(["a       b"], render(b"a\tb"))
        self.assertEqual(["ac"], render(b"ab\bc"))

    def test_save_and_restore_cursor(self):
        self.assertEqual(["Xbcd"], render(b"\x1b[sabcd\r\x1b[uX"))
        self.assertEqual(["Xbcd"], render(b"\x1b7abcd\r\x1b8X"))


class EscapeSwallowingTests(unittest.TestCase):
    def test_osc_title_is_swallowed(self):
        stream = b"\x1b]0;a window title\x07visible\r\n"
        self.assertEqual(["visible"], render(stream))

    def test_osc_with_string_terminator_is_swallowed(self):
        stream = b"\x1b]777;notify;body\x1b\\visible\r\n"
        self.assertEqual(["visible"], render(stream))

    def test_unknown_csi_and_private_modes_are_swallowed(self):
        stream = (
            b"\x1b[?2026h\x1b[?25l\x1b[>4;2m\x1b[38;2;153;153;153m"
            b"visible\x1b[39m\x1b[?25h\x1b[?2026l\r\n"
        )
        self.assertEqual(["visible"], render(stream))

    def test_charset_and_single_character_escapes_are_swallowed(self):
        self.assertEqual(["visible"], render(b"\x1b(Bvis\x1b=ible\x1b>\r\n"))

    def test_dcs_string_is_swallowed(self):
        self.assertEqual(["visible"], render(b"\x1bP+q544e\x1b\\visible\r\n"))

    def test_no_escape_byte_ever_reaches_the_output(self):
        stream = b"\x1b[1;31mred\x1b[0m \x1b]0;t\x07 \x1bZ tail\r\n"
        for line in render(stream):
            self.assertNotIn("\x1b", line)

    def test_truncated_escape_at_the_end_is_held_not_leaked(self):
        renderer = JournalRenderer(width=40, height=8)
        renderer.feed(b"visible\x1b[38;2;1")
        self.assertEqual(["visible"], renderer.live_rows())
        renderer.feed(b";2m more\r\n")
        self.assertEqual(["visible more"], renderer.take_settled() + renderer.live_rows())

    def test_stray_escape_byte_is_dropped(self):
        self.assertEqual(["ab"], render(b"a\x1b\x00b"))


class IncrementalTests(unittest.TestCase):
    STREAM = (
        b"\x1b]0;title\x07"
        b"header line\r\n"
        + b"".join(f"row {index} \xe2\x94\x82 value\r\n".encode() for index in range(30))
        + b"\x1b[5A\r\x1b[Krepainted row\r\n"
        b"\x1b[?1049hoverlay\x1b[?1049l"
        b"tail \xc3\xa9\xe2\x80\x94 done\r\n"
    )

    def test_seven_byte_chunking_matches_one_shot(self):
        one_shot = JournalRenderer(width=60, height=10)
        one_shot.feed(self.STREAM)
        expected_settled = one_shot.take_settled()
        expected_live = one_shot.live_rows()
        for size in (1, 2, 3, 7, 13, 64, 997):
            settled, live = render_chunked(self.STREAM, size, width=60, height=10)
            self.assertEqual(expected_settled, settled, f"chunk size {size}")
            self.assertEqual(expected_live, live, f"chunk size {size}")

    def test_utf8_split_across_chunks(self):
        stream = "café — naïve ☃\r\n".encode("utf-8")
        for size in range(1, 9):
            settled, live = render_chunked(stream, size)
            self.assertEqual(["café — naïve ☃"], settled + live, f"chunk size {size}")

    def test_checkpoint_resume_matches_uninterrupted_feed(self):
        one_shot = JournalRenderer(width=60, height=10)
        one_shot.feed(self.STREAM)
        expected = one_shot.take_settled() + one_shot.live_rows()

        renderer = JournalRenderer(width=60, height=10)
        settled = []
        for start in range(0, len(self.STREAM), 11):
            renderer.feed(self.STREAM[start : start + 11])
            settled.extend(renderer.take_settled())
            state = json.loads(json.dumps(renderer.checkpoint()))
            renderer = JournalRenderer.resume(state)
        self.assertEqual(expected, settled + renderer.live_rows())

    def test_checkpoint_is_json_safe_and_carries_partial_state(self):
        renderer = JournalRenderer(width=60, height=10)
        renderer.feed(b"partial \xe2\x94")  # incomplete UTF-8
        renderer.feed(b"\x82 \x1b[38;2")  # incomplete escape
        state = renderer.checkpoint()
        json.dumps(state)
        self.assertEqual(journal_render.CHECKPOINT_VERSION, state["version"])
        self.assertTrue(state["pending"])
        resumed = JournalRenderer.resume(state)
        resumed.feed(b";1;2mtail\r\n")
        self.assertEqual(["partial │ tail"], resumed.take_settled() + resumed.live_rows())

    def test_resume_of_none_starts_fresh(self):
        renderer = JournalRenderer.resume(None)
        self.assertEqual(journal_render.DEFAULT_WIDTH, renderer.width)

    def test_resume_rejects_a_foreign_version(self):
        with self.assertRaises(ValueError):
            JournalRenderer.resume({"version": 999})


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.journal = self.root / "journal"
        self.journal.mkdir()
        self.out = self.root / "sidecar.txt"
        self.state = self.root / "state.json"

    def run_cli(self, mode: str, *extra: str):
        completed = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                mode,
                "--journal",
                str(self.journal),
                "--out",
                str(self.out),
                "--state",
                str(self.state),
                *extra,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_render_appends_only_new_bytes_across_runs(self):
        segment = self.journal / "segment-000001.raw"
        segment.write_bytes(b"".join(f"line {i}\r\n".encode() for i in range(80)))
        first = self.run_cli("render")
        self.assertGreater(first["settled_lines"], 0)
        body = self.out.read_text(encoding="utf-8")
        self.assertIn("line 0\n", body)

        with open(segment, "ab") as handle:
            handle.write(b"".join(f"more {i}\r\n".encode() for i in range(80)))
        second = self.run_cli("render")
        self.assertGreater(second["settled_lines"], 0)
        grown = self.out.read_text(encoding="utf-8")
        self.assertTrue(grown.startswith(body))
        self.assertIn("more 0\n", grown)
        # No duplicated history: each source line settles exactly once.
        self.assertEqual(1, grown.count("line 7\n"))

    def test_second_segment_continues_the_same_screen(self):
        (self.journal / "segment-000001.raw").write_bytes(b"kept\r\ndraft one\r")
        self.run_cli("render")
        (self.journal / "segment-000002.raw").write_bytes(
            b"\x1b[Kdraft two\r\n" + b"pad\r\n" * 80
        )
        self.run_cli("render")
        body = self.out.read_text(encoding="utf-8")
        self.assertIn("draft two\n", body)
        self.assertNotIn("draft one", body)

    def test_single_file_journal(self):
        target = self.root / "one.raw"
        target.write_bytes(b"solo\r\n" + b"pad\r\n" * 80)
        completed = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "render",
                "--journal",
                str(target),
                "--out",
                str(self.out),
                "--state",
                str(self.state),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("solo\n", self.out.read_text(encoding="utf-8"))

    def test_max_bytes_limits_a_single_run(self):
        segment = self.journal / "segment-000001.raw"
        segment.write_bytes(b"".join(f"line {i}\r\n".encode() for i in range(400)))
        first = self.run_cli("render", "--max-bytes", "512")
        self.assertEqual(512, first["bytes_read"])
        self.assertEqual(512, first["byte_offset"])
        second = self.run_cli("render")
        self.assertEqual(segment.stat().st_size - 512, second["bytes_read"])

    def test_flush_appends_a_live_block_that_is_not_committed(self):
        segment = self.journal / "segment-000001.raw"
        segment.write_bytes(b"settled\r\n" * 80 + b"live and still drawing")
        result = self.run_cli("flush")
        body = self.out.read_text(encoding="utf-8")
        self.assertIn(journal_render.LIVE_MARKER_PREFIX, body)
        self.assertIn("live and still drawing", body)
        self.assertGreaterEqual(result["live_lines"], 1)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertLess(state["sidecar_bytes"], len(body.encode("utf-8")))

        # The next run drops the stale live block instead of duplicating it.
        again = self.run_cli("render")
        self.assertEqual(0, again["bytes_read"])
        refreshed = self.out.read_text(encoding="utf-8")
        self.assertNotIn(journal_render.LIVE_MARKER_PREFIX, refreshed)
        self.assertNotIn("live and still drawing", refreshed)
        # ...and a run with nothing new to read keeps the settled history.
        self.assertIn("settled\n", refreshed)
        self.assertEqual(
            len(refreshed.encode("utf-8")),
            json.loads(self.state.read_text(encoding="utf-8"))["sidecar_bytes"],
        )

    def test_truncated_journal_restarts_cleanly(self):
        segment = self.journal / "segment-000001.raw"
        segment.write_bytes(b"".join(f"old {i}\r\n".encode() for i in range(80)))
        self.run_cli("render")
        segment.write_bytes(b"".join(f"new {i}\r\n".encode() for i in range(80)))
        result = self.run_cli("render")
        self.assertTrue(result["restarted"])
        body = self.out.read_text(encoding="utf-8")
        self.assertIn("new 0\n", body)
        self.assertNotIn("old 0\n", body)

    def test_missing_journal_reports_an_error(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "render",
                "--journal",
                str(self.root / "nope"),
                "--out",
                str(self.out),
                "--state",
                str(self.state),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(2, completed.returncode)


# An example line shaped exactly like the one that was seen braided, and the
# row-interleaved repaint that produced the braid. Claude Code draws a block
# of rows by jumping between them mid-line, so a flattener that ignores cursor
# motion splices the neighbouring row's characters into this one at exactly
# the columns the sentence skipped.
CLEAN_SENTENCE = (
    'Widget Sample Two was flagged by a "repeat entry" routine on 2020-01-02 '
    "(job group B_20200102)."
)
OPERATOR_BRAID = (
    'WidgetiSampledTwohwasiflaggedrbyua "repeatyentry"lroutine.on 2020-01-02 '
    "(job groupB_20200102)."
)
NEIGHBOUR_CHARS = "idhiruyl."

# One entry per gap between the sentence's words, aligned against the captured
# paste with difflib. Three real behaviours show up there:
#   "<char>" the TUI dropped a row, painted one cell, and came back a column on
#   " "      an ordinary space byte
#   ""       the column was skipped with a cursor-forward and never painted
GAP_PLAN = ("i", "d", "h", "i", "r", "u", " ", "y", "l", ".", " ", " ", " ", "")


def braided_stream() -> bytes:
    """Emit CLEAN_SENTENCE the way the capture that braided it did."""
    out = bytearray(b"context row\r\n")
    words = CLEAN_SENTENCE.split(" ")
    assert len(words) == len(GAP_PLAN) + 1
    for index, word in enumerate(words):
        out += word.encode("utf-8")
        if index >= len(GAP_PLAN):
            break
        action = GAP_PLAN[index]
        if action == " ":
            out += b" "
        elif action == "":
            out += b"\x1b[C"
        else:
            out += b"\x1b[B" + action.encode("utf-8") + b"\x1b[A"
    out += b"\r\n\r\n"
    return bytes(out)


class BraidGoldenTests(unittest.TestCase):
    def test_the_generator_reproduces_the_operator_artifact(self):
        self.assertIn(OPERATOR_BRAID, naive_text(braided_stream()))

    def test_renderer_reads_the_sentence_back_clean(self):
        lines = render(braided_stream(), width=120, height=8)
        joined = "\n".join(lines)
        self.assertIn("Widget Sample Two was flagged", joined)
        self.assertIn(CLEAN_SENTENCE, joined)
        self.assertNotIn("WidgetiSampledTwo", joined)
        self.assertEqual(0, joined.count("WidgetiSampledTwo"))

    def test_the_neighbouring_row_keeps_its_own_characters(self):
        lines = render(braided_stream(), width=120, height=8)
        allowed = set(NEIGHBOUR_CHARS) | {" "}
        neighbour = [
            line
            for line in lines
            if line.strip() and set(line.strip()) <= allowed
        ]
        self.assertTrue(neighbour, lines)
        self.assertEqual(
            NEIGHBOUR_CHARS, "".join(neighbour[0].split()), neighbour[0]
        )


class RealJournalTests(unittest.TestCase):
    """Invariants over a real capture, skipped when the box has none."""

    BUDGET = 8 * 1024 * 1024

    # A capture only exercises the renderer if it repaints, and cursor-up is
    # the idiom that does it. Plain shell logs are skipped, not asserted on.
    REPAINT = re.compile(rb"\x1b\[[0-9]*A")

    @classmethod
    def journal(cls) -> Path | None:
        override = os.environ.get("SESSION_KIT_GOLDEN_JOURNAL")
        if override:
            candidate = Path(override)
            return candidate if candidate.exists() else None
        root = Path(
            os.environ.get("SESSION_KIT_JOURNAL_DIR")
            or Path.home() / ".local" / "state" / "shpool-journal"
        )
        if not root.is_dir():
            return None
        found = [path for path in root.glob("*.raw") if path.stat().st_size > 1 << 20]
        found += [
            path
            for path in root.glob("*/segment-*.raw")
            if path.stat().st_size > 1 << 20
        ]
        for path in sorted(found, key=lambda item: item.stat().st_size):
            try:
                with open(path, "rb") as handle:
                    head = handle.read(1 << 20)
            except OSError:
                continue
            if cls.REPAINT.search(head):
                return path
        return None

    def setUp(self):
        path = self.journal()
        if path is None:
            self.skipTest("no real TUI journal capture on this box")
        self.path = path
        with open(path, "rb") as handle:
            self.data = handle.read(self.BUDGET)

    def test_render_is_fast_enough_for_a_background_tick(self):
        renderer = JournalRenderer()
        started = time.monotonic()
        renderer.feed(self.data)
        settled = renderer.take_settled()
        elapsed = time.monotonic() - started
        rate = len(self.data) / elapsed / 1e6
        self.assertGreater(settled, [], "a real capture should settle lines")
        self.assertGreater(rate, 2.0, f"only {rate:.2f} MB/s")

    def test_no_escape_bytes_survive_a_real_capture(self):
        renderer = JournalRenderer()
        renderer.feed(self.data)
        for line in renderer.take_settled() + renderer.live_rows():
            self.assertNotIn("\x1b", line)
            self.assertLessEqual(len(line), renderer.width)

    def test_real_capture_is_chunk_invariant(self):
        sample = self.data[: 1 << 20]
        one_shot = JournalRenderer()
        one_shot.feed(sample)
        expected = one_shot.take_settled()
        chunked, _ = render_chunked(
            sample, 7, width=journal_render.DEFAULT_WIDTH,
            height=journal_render.DEFAULT_HEIGHT,
        )
        self.assertEqual(expected, chunked)

    def test_renderer_recovers_text_the_naive_flattener_destroys(self):
        renderer = JournalRenderer()
        renderer.feed(self.data)
        lines = renderer.take_settled()
        flattened = naive_text(self.data)
        recovered = [
            line.strip()
            for line in lines
            if len(line.strip()) >= 55 and line.strip() not in flattened
        ]
        self.assertTrue(
            recovered,
            "a real TUI capture should contain lines only cursor emulation recovers",
        )


if __name__ == "__main__":
    unittest.main()
