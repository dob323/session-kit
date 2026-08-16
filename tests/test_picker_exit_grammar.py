"""One exit grammar, proved on the picker's own surfaces.

Every screen in the kit now answers the same three ways of leaving:

    q / quit / exit   leave this surface for the one above it
    Ctrl-C            cancel the prompt you are in, back to its menu
    Ctrl-D            one level, out loud -- never a silent end three menus deep

Enter is not among them any more: it takes the most likely option on the
screen it is pressed on -- back out of a modal, and on the home list the top
row it just drew (operator ruling, 2026-08-16).

Before this, `q` meant four different things on four screens, `exit` and
`quit` were unknown words to a menu whose job is leaving, an interrupt could
not cancel a single nested prompt, and a Ctrl-D typed into a confirmation
ended the whole picker without a word.
"""

from __future__ import annotations

import json
import signal
import subprocess
import unittest

from tests.support import REPO
from tests.test_login import LoginFixture, inventory, row, run_pty

LOGIN = REPO / "bin" / "shpool_login"


def exit_reasons(fixture: LoginFixture) -> list[str]:
    """Why the picker ended, straight from the action log."""
    log = fixture.state / "action-events.jsonl"
    if not log.exists():
        return []
    reasons = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("action") == "picker_exit":
            reasons.append(entry.get("outcome"))
    return reasons


class ExitCodeTests(unittest.TestCase):
    """0 is leaving on purpose; 1 is the picker failing to run.

    Every way out used to be 2 -- the code `sp help exit-codes` reserves for a
    wrong argument or a selector that matched nothing -- so the shell that
    launched the picker could not tell "the person chose a terminal" from "the
    session manager never answered", and treated both as normal.
    """

    def test_leaving_on_purpose_is_a_success(self) -> None:
        for typed, reason in ((b"q\n", "quit"), (b"\x04", "input_closed")):
            with self.subTest(typed=typed):
                fixture = LoginFixture(inventory(row("ready", number=9)))
                try:
                    code, _output = run_pty(fixture, typed)
                    self.assertEqual(0, code)
                    self.assertEqual([reason], exit_reasons(fixture))
                finally:
                    fixture.close()

    def test_a_picker_without_a_terminal_exits_1_and_says_why(self) -> None:
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            completed = subprocess.run(
                [LOGIN],
                env=fixture.env(),
                cwd=fixture.base,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(1, completed.returncode, completed.stderr)
            self.assertIn(
                "session-kit: the picker needs a terminal.", completed.stderr
            )
            self.assertEqual(["noninteractive"], exit_reasons(fixture))
        finally:
            fixture.close()

    def test_a_release_missing_its_helpers_exits_1_and_says_why(self) -> None:
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(
                fixture,
                b"",
                env_updates={
                    "SESSION_KIT_SP_CMD": str(fixture.base / "not-installed"),
                },
            )
            self.assertEqual(1, code)
            self.assertIn(
                "session-kit: the picker's helpers are missing from this release.",
                output,
            )
        finally:
            fixture.close()


class ExitWordsTests(unittest.TestCase):
    def test_the_dashboard_leaves_on_the_typed_words_exit_and_quit(self) -> None:
        """The words a person types when the key does not come to mind."""
        for word in (b"exit\n", b"quit\n", b"EXIT\n"):
            with self.subTest(word=word):
                fixture = LoginFixture(inventory(row("ready", number=9)))
                try:
                    code, output = run_pty(fixture, word)
                    self.assertEqual(0, code)
                    self.assertNotIn("There is no such key", output)
                    self.assertEqual(["quit"], exit_reasons(fixture))
                    self.assertEqual([], fixture.sp_entries())
                finally:
                    fixture.close()

    def test_the_help_screen_documents_the_words_it_accepts(self) -> None:
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(fixture, b"?\n\nq\n", columns=110)
            self.assertEqual(0, code)
            help_text = output[output.index("Picker help") :]
            self.assertIn("q / quit / exit", help_text)
        finally:
            fixture.close()

    def test_the_help_screen_never_says_enter_leaves(self) -> None:
        """A help row is a claim, and this one outlived the behaviour.

        The table carried `Leaving  Enter  Leave the picker for a regular
        shell` after Enter stopped leaving anything.
        """
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(fixture, b"?\n\nq\n", columns=110)
            self.assertEqual(0, code)
            help_text = output[output.index("Picker help") :]
            enter_rows = [
                line
                for line in help_text.splitlines()
                if line.strip().startswith("Enter")
            ]
            self.assertEqual(1, len(enter_rows), help_text)
            self.assertNotIn("Leave the picker", enter_rows[0])
            self.assertIn("Takes the most likely choice", enter_rows[0])
            # The door out is named for the keys that actually open it.
            self.assertIn("Back from the top is out", help_text)
        finally:
            fixture.close()


class BackOneLevelTests(unittest.TestCase):
    def test_q_on_needs_you_returns_to_the_list_instead_of_leaving(self) -> None:
        """`q` there used to end the picker; `q3` still resolves a prompt."""
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(fixture, b"a\nq\nq\n")
            self.assertEqual(0, code)
            # The list came back after the Needs You screen, and only the
            # deliberate q that followed ended the picker.
            needs = output.index("needs you: 0")
            self.assertIn("# · kill k #", output[needs:])
            self.assertEqual(["quit"], exit_reasons(fixture))
        finally:
            fixture.close()

    def test_q_is_back_on_more_and_on_help(self) -> None:
        for label, payload, marker in (
            ("more", b"m\nq\nq\n", "More"),
            ("help", b"?\nq\nq\n", "Picker help"),
        ):
            with self.subTest(label=label):
                fixture = LoginFixture(inventory(row("ready", number=9)))
                try:
                    code, output = run_pty(fixture, payload)
                    self.assertEqual(0, code)
                    self.assertIn(marker, output)
                    # Back to the list, then out through the q after it.
                    self.assertIn("# · kill k #", output[output.index(marker) :])
                    self.assertEqual(["quit"], exit_reasons(fixture))
                finally:
                    fixture.close()

    def test_q_is_back_on_the_projects_screen(self) -> None:
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(fixture, b"m\np\nq\n\nq\n")
            self.assertEqual(0, code)
            self.assertIn("projects ❯", output)
            self.assertNotIn("Unknown choice", output)
            self.assertEqual(["quit"], exit_reasons(fixture))
        finally:
            fixture.close()


class WaitingPromptTests(unittest.TestCase):
    def test_a_modal_prompt_left_alone_stays_where_it_is(self) -> None:
        """The poll that lets Ctrl-C be noticed must not become an exit.

        The modal read waits in one-second steps. A step that ends with
        nothing typed is a step, not closed input -- and `$?` after a bare
        failed `if` is the `if`'s own zero, which is exactly how a timeout
        turns into an end-of-input that walks out of the prompt on its own.
        """
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(
                fixture,
                b"m\n",
                # Nothing is typed until the More screen has been sitting on
                # its prompt for several polls.
                deferred=("more ❯", b"q\nq\n"),
                deferred_delay_seconds=3,
            )
            self.assertEqual(0, code)
            self.assertNotIn("End of input", output)
            self.assertEqual(["quit"], exit_reasons(fixture))
            # One More screen, not one per poll: it never redrew itself.
            self.assertEqual(1, output.count("Help: every picker key"))
        finally:
            fixture.close()


class UnknownKeyTests(unittest.TestCase):
    def test_an_unknown_key_on_the_list_names_the_keys_that_work(self) -> None:
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(fixture, b"zz\nq\n")
            self.assertEqual(0, code)
            # Enter leads the list: it is a key this screen takes now, and a
            # list that omits it teaches the screen it used to be.
            self.assertIn(
                "There is no such key on this screen. These work: Enter, a session number,",
                output,
            )
            self.assertIn("and q.", output)
            # Every key it names is a key the dispatcher takes.
            self.assertNotIn("i number", output)
        finally:
            fixture.close()

    def test_an_unknown_key_on_needs_you_names_its_own_keys(self) -> None:
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(fixture, b"a\nzz\n\nq\n", columns=120)
            self.assertEqual(0, code)
            self.assertIn(
                "There is no such key on this screen. These work: d number, ?, b, and Enter.",
                output,
            )
        finally:
            fixture.close()

    def test_an_unknown_key_on_the_new_session_screen_names_its_own_keys(self) -> None:
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(fixture, b"n\nzz\nq\n")
            self.assertEqual(0, code)
            self.assertIn(
                "There is no such key on this screen. These work: 1, 2, 3, Enter, and b.",
                output,
            )
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()


class EscapeSequenceTests(unittest.TestCase):
    """An arrow key is one keystroke, not three characters of an answer."""

    def test_an_arrow_key_at_a_modal_prompt_is_not_an_answer(self) -> None:
        # The modal prompts read whole lines, so Up-arrow arrived as ^[[A
        # inside a name, a path, or a confirmation word.
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(fixture, b"m\n\x1b[A\nq\nq\n")
            self.assertEqual(0, code)
            self.assertIn("Arrow keys do nothing here.", output)
            # It asked again instead of acting: the More screen was still
            # there for the q that followed.
            self.assertNotIn("There is no such key", output)
            self.assertEqual(["quit"], exit_reasons(fixture))
        finally:
            fixture.close()

    def test_an_arrow_key_never_reaches_the_list_prompt(self) -> None:
        # Typed at the home menu it is swallowed whole: no bytes in the
        # buffer, nothing dispatched, no echo of the sequence.
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(
                fixture,
                b"",
                # Typed after the menu paints, so the character-by-character
                # read owns the keyboard when it lands.
                deferred=("❯", b"\x1b[Aq\n"),
                deferred_delay_seconds=1.5,
            )
            self.assertEqual(0, code)
            # The buffer held "q" and nothing else: the picker left on the
            # key, not on an unknown choice built out of escape bytes.
            self.assertEqual(["quit"], exit_reasons(fixture))
            self.assertNotIn("There is no such key", output)
            self.assertNotIn("[A", output.split("# · kill k #")[-1])
        finally:
            fixture.close()


class InterruptTests(unittest.TestCase):
    def test_ctrl_c_cancels_a_modal_prompt_back_to_its_menu(self) -> None:
        """The 24 `|| return` guards on modal reads were dead code.

        `picker_modal_read` looped on an interrupt and asked again, so a
        Ctrl-C inside More, Help, a project form, or a confirmation did
        nothing at all. It now cancels the prompt and hands the level back.
        """
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(
                fixture,
                b"m\n",
                send_signal=signal.SIGINT,
                signal_marker="more ❯",
                post_signal_bytes=b"q\n",
                post_signal_marker="Nothing changed.",
            )
            self.assertEqual(0, code)
            self.assertIn("Nothing changed.", output)
            # Cancelled the prompt, not the picker: the list came back and the
            # q after the signal is what ended it.
            cancelled = output.index("Nothing changed.")
            self.assertIn("# · kill k #", output[cancelled:])
            self.assertEqual(["quit"], exit_reasons(fixture))
        finally:
            fixture.close()

    def test_ctrl_c_three_levels_down_returns_one_level(self) -> None:
        """The list, More, Projects, then the add form: an interrupt at the
        form hands back the Projects screen, not the list and not a shell."""
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(
                fixture,
                b"m\np\na\n",
                send_signal=signal.SIGINT,
                signal_marker="Directory (Enter for",
                post_signal_bytes=b"\n\nq\n",
                post_signal_marker="Nothing changed.",
            )
            self.assertEqual(0, code)
            cancelled = output.index("Nothing changed.")
            # The screen that asked is the screen that came back.
            self.assertIn("projects ❯", output[cancelled:])
            self.assertEqual(["quit"], exit_reasons(fixture))
        finally:
            fixture.close()

    def test_the_home_menu_still_redraws_on_an_interrupt(self) -> None:
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(
                fixture,
                b"",
                send_signal=signal.SIGINT,
                post_signal_bytes=b"q\n",
            )
            self.assertEqual(0, code)
            self.assertNotIn("Nothing changed.", output)
            self.assertEqual(["quit"], exit_reasons(fixture))
        finally:
            fixture.close()

    def test_an_interrupt_discards_the_line_it_interrupted(self) -> None:
        """Typed first, then interrupted -- the assertion this test needed.

        Asserting only that the picker survived an interrupt with an empty
        buffer passed while Ctrl-C was a complete no-op here: the flag was
        read inside a branch a signalled read can never reach, so the
        abandoned line was still in the buffer and the next Enter submitted
        it. A destructive line makes the difference impossible to miss.
        """
        fixture = LoginFixture(
            inventory(row("ready", number=9), row("other", number=17))
        )
        try:
            code, output = run_pty(
                fixture,
                b"",
                # Typed after the opening canonical second, so the picker owns
                # the characters and the interrupt has a buffer to throw away.
                deferred=("❯ ", b"k 17"),
                deferred_delay_seconds=1.3,
                send_signal=signal.SIGINT,
                post_signal_bytes=b"q\n",
            )
            self.assertEqual(0, code)
            # The line died with the interrupt: q left the picker instead
            # of closing session 17.
            self.assertEqual(["quit"], exit_reasons(fixture))
            self.assertEqual(
                [], [entry for entry in fixture.sp_entries() if entry.get("args")]
            )
            # And the prompt came back, as the help text promises.
            self.assertIn("❯", output[output.rindex("k 17") :])
        finally:
            fixture.close()


class EndOfInputTests(unittest.TestCase):
    def test_ctrl_d_in_a_modal_goes_back_one_level_instead_of_killing_it(
        self,
    ) -> None:
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(fixture, b"m\n\x04q\n")
            self.assertEqual(0, code)
            self.assertIn("Back.", output)
            back = output.index("  Back.")
            self.assertIn("# · kill k #", output[back:])
            # The picker ended on the q after it, not on the Ctrl-D.
            self.assertEqual(["quit"], exit_reasons(fixture))
        finally:
            fixture.close()

    def test_ctrl_d_on_the_home_menu_leaves_and_says_so(self) -> None:
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(fixture, b"\x04")
            self.assertEqual(0, code)
            self.assertIn("Leaving the picker.", output)
            self.assertEqual(["input_closed"], exit_reasons(fixture))
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
