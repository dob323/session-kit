"""The anatomy figure must never advertise a key the release does not have.

`tools/render-readme-anatomy` renders its terminal from live code, so a feature
merged privately and not yet released appears in the figure the moment someone
regenerates it. The guard that catches this is only worth having if it actually
fires: an earlier cut of it silently passed because the frame's colour escapes
sit flush against the keys, and because the README's "`b` or `q`" row names two
keys in one line. Both mistakes are pinned here.
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
import unittest

from tests.support import REPO

anatomy = SourceFileLoader(
    "session_kit_render_readme_anatomy", str(REPO / "tools" / "render-readme-anatomy")
).load_module()

# A footer exactly as the picker emits one, colour escapes included.
G, OFF = "\x1b[1m\x1b[32m", "\x1b[0m"
FOOTER = (
    f"  {G}↵{OFF} open 53 · {G}#{OFF} · kill {G}k #{OFF} · new {G}n{OFF} · "
    f"more {G}m{OFF} · needs you {G}a{OFF} · help {G}?{OFF} · "
    f"history {G}h #{OFF} · leave {G}q or b{OFF}"
)


class OfferedKeyTests(unittest.TestCase):
    def test_colour_escapes_do_not_hide_a_key(self) -> None:
        """The escapes abut the keys, so a naive word boundary finds nothing."""

        self.assertEqual(
            {"k", "n", "m", "a", "?", "h", "q", "b"}, anatomy.offered_keys(FOOTER)
        )

    def test_a_frame_without_a_command_bar_is_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            anatomy.offered_keys("7 sessions · 5 ready\nReady\n")


class DocumentedKeyTests(unittest.TestCase):
    def test_the_readme_names_every_key_the_picker_offers(self) -> None:
        """The shipped README and the shipped footer have to agree."""

        self.assertLessEqual(anatomy.offered_keys(FOOTER), anatomy.documented_keys())

    def test_a_row_naming_two_keys_yields_both(self) -> None:
        """ "`b` or `q`" is one row and two keys."""

        keys = anatomy.documented_keys()
        self.assertIn("b", keys)
        self.assertIn("q", keys)

    def test_the_question_mark_is_a_key(self) -> None:
        """It is not a word character, so \\b around it matches nothing."""

        self.assertIn("?", anatomy.documented_keys())


class RefusalTests(unittest.TestCase):
    def test_an_undocumented_key_stops_the_render(self) -> None:
        pinned = FOOTER.replace("· leave", f"· pin {G}p #{OFF} · leave")
        with self.assertRaises(SystemExit) as refusal:
            anatomy.refuse_undocumented_keys(pinned)
        self.assertIn("p", str(refusal.exception))

    def test_a_documented_footer_passes(self) -> None:
        anatomy.refuse_undocumented_keys(FOOTER)


if __name__ == "__main__":
    unittest.main()
