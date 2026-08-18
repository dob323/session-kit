"""Guards for the README's generated artwork.

The pictures on the front page are rendered from live code by the tools under
`tools/`, which means they can go wrong in ways prose cannot: a screen can be
captured with another screen stacked on top of it, two pictures can describe
inventories that contradict each other, and a laid-out figure can let its text
escape the box it was drawn in. Each of those has actually happened. Every one
of them is pinned here.

The rendering tests need a browser, and CI does not have one, so they skip
rather than fail there. The reasoning tests need nothing and always run.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

from tests.support import REPO

TOOLS = REPO / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

ASSETS = REPO / "docs" / "assets" / "readme"


def _capture():
    import readme_capture

    return readme_capture


class ReferencedArtworkTests(unittest.TestCase):
    """The README and the asset directory must describe the same set."""

    def setUp(self) -> None:
        self.readme = (REPO / "README.md").read_text(encoding="utf-8")

    def referenced(self) -> set[str]:
        return {
            Path(path).name
            for path in re.findall(r'src="([^"]+)"', self.readme)
            if "assets/readme/" in path
        }

    def test_every_referenced_picture_exists(self) -> None:
        for name in sorted(self.referenced()):
            with self.subTest(name):
                self.assertTrue((ASSETS / name).is_file(), f"{name} is not on disk")

    def test_no_orphan_pictures_ship(self) -> None:
        """An asset nothing points at is dead weight in the release archive.

        The social preview is uploaded through GitHub's settings rather than
        referenced from the page, so it is named here on purpose.
        """

        on_disk = {path.name for path in ASSETS.glob("*.png")}
        self.assertEqual(set(), on_disk - self.referenced() - {"social-preview.png"})

    def test_the_readme_still_shows_the_picker(self) -> None:
        self.assertIn("picker.png", self.referenced())


class ScreenSeparationTests(unittest.TestCase):
    """One captured frame can hold two screens. Neither may pollute the other."""

    LIST = (
        "  7 sessions · 5 ready · 2 open elsewhere\n"
        "  Ready\n"
        "     53  Review Release Notes | CLD | personal | Opus 5 | question  | 11m\n"
        "     57  Fix Login Timeout    | CLD | personal | Opus 5 | needs you | 6m\n"
        "     60  Document Release     | CLD | personal | Opus 5 | working   | 9m\n"
        "\n"
        "  ↵ open 53 · # · kill k # · new n · more m · needs you a · help ? · "
        "history h # · leave q or b\n"
        "  ❯ "
    )
    NEEDS = (
        "\n  needs you: 2\n\n"
        "  Sessions\n"
        "    53  Review Release Notes | Claude | question | needs you 11 min\n"
        "    57  Fix Login Timeout    | Claude | needs you 6 min\n"
        "\n  ↵ back · b back · help ?\n\n"
    )

    def stacked(self) -> str:
        """What the picker really writes: leaving `a` repaints the list here."""

        return self.NEEDS + _capture().REPAINT + " " + self.LIST

    def test_the_list_screen_is_taken_from_after_the_repaint(self) -> None:
        capture = _capture()
        frame = capture.list_frame([self.stacked()])
        self.assertNotIn("needs you:", capture.plain(frame))
        self.assertIn("kill", capture.plain(frame))

    def test_the_needs_screen_is_taken_from_before_the_repaint(self) -> None:
        capture = _capture()
        frame = capture.needs_frame([self.stacked()])
        self.assertNotIn("7 sessions", capture.plain(frame))
        self.assertIn("needs you: 2", capture.plain(frame))

    def test_the_needs_screen_keeps_its_prompt(self) -> None:
        """The repaint overwrites it, and a terminal picture needs one."""

        capture = _capture()
        self.assertTrue(
            capture.plain(capture.needs_frame([self.stacked()])).rstrip().endswith("❯")
        )

    def test_a_capture_with_no_needs_screen_is_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            _capture().needs_frame([self.LIST])

    def test_a_capture_with_no_list_screen_is_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            _capture().list_frame([self.NEEDS])


class ScreenAgreementTests(unittest.TestCase):
    """Two pictures on one page may not contradict each other.

    This is not hypothetical. An ordinary finished Codex turn reports
    `agent_status: idle` with `needs_you: false`; the list prints `needs you`
    for it because that is what idle maps to, and the needs-you screen leaves
    it out because it reads the raw flag. The first capture taken for these
    figures showed four waiting sessions on one picture and three on the other.
    """

    LIST = ScreenSeparationTests.LIST
    NEEDS = ScreenSeparationTests.NEEDS

    def test_agreeing_screens_pass(self) -> None:
        _capture().refuse_disagreeing_screens(self.LIST, self.NEEDS)

    def test_a_missing_session_is_refused(self) -> None:
        short = self.NEEDS.replace("needs you: 2", "needs you: 1").replace(
            "    57  Fix Login Timeout    | Claude | needs you 6 min\n", ""
        )
        with self.assertRaises(SystemExit) as refusal:
            _capture().refuse_disagreeing_screens(self.LIST, short)
        self.assertIn("57", str(refusal.exception))

    def test_a_wrong_headline_count_is_refused(self) -> None:
        """The number and the rows under it are separate evidence."""

        miscounted = self.NEEDS.replace("needs you: 2", "needs you: 9")
        with self.assertRaises(SystemExit):
            _capture().refuse_disagreeing_screens(self.LIST, miscounted)

    def test_idle_counts_as_waiting(self) -> None:
        """It is a needs-you session gone quiet, not a fifth state."""

        self.assertIn("idle", _capture().ATTENTION_WORDS)

    def test_a_needs_you_row_is_not_counted_as_a_list_row(self) -> None:
        """Both screens use the same row shape; only the list screen counts."""

        self.assertEqual([], _capture().attention_rows(self.NEEDS))


class LayoutRefusalTests(unittest.TestCase):
    """A figure whose text escapes its box must not be saved."""

    def setUp(self) -> None:
        import readme_art_lib

        self.art = readme_art_lib
        if self.art.chromium_path() is None:
            self.skipTest("no chromium available for layout checks")

    def test_text_outside_its_box_stops_the_render(self) -> None:
        import tempfile

        body = (
            '<div style="width:60px;height:20px;overflow:hidden">'
            "a sentence far too long to fit inside sixty pixels</div>"
        )
        html = self.art.page(body, "", width=200, height=100)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(self.art.RenderError):
                self.art.render(
                    html, Path(directory) / "x.png", width=200, height=100, scale=1
                )

    def test_a_figure_that_fits_renders(self) -> None:
        import tempfile

        html = self.art.page(
            '<div style="width:180px;height:40px;overflow:hidden">ok</div>',
            "",
            width=200,
            height=100,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "x.png"
            self.art.render(html, output, width=200, height=100, scale=1)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
