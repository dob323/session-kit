"""A clean provider exit keeps the managed terminal alive.

Contract: every provider exit — clean or crashed — stops at the recovery menu
with the terminal still open. The terminal is the only thing that still knows
the exact conversation identity `r` needs, so losing it is not undoable while
reaching the picker is one more keypress (`c`). Touching
``~/.sk_autoclose_on_clean_exit`` opts back into closing on a zero exit code;
``tests/test_lifecycle_shell.py`` covers that opt-in path.
"""

from __future__ import annotations

from tests.test_lifecycle_shell import ProviderExitShellHarness, write_executable


class CleanExitStaysAliveTests(ProviderExitShellHarness):
    def test_clean_exit_stops_at_the_recovery_menu_by_default(self) -> None:
        # No ~/.sk_autoclose_on_clean_exit marker: /exit must not close the
        # terminal. `c` is the explicit way back to the picker.
        completed = self.launch("c\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited with status 0", completed.stdout)
        self.assertIn("This terminal is still open", completed.stdout)
        self.assertIn("Provider exited: [r] reopen conversation", completed.stdout)
        # `c` closed the shell rather than returning to the sourcing caller.
        self.assertNotIn("SOURCE_RETURNED", completed.stdout)
        self.assertTrue(self.lifecycle_document()["user_input_after_exit"])

    def test_clean_exit_menu_offers_reopen_and_survives_a_refused_one(
        self,
    ) -> None:
        # The whole point of staying alive after /exit is that `r` is on the
        # table at all. This fixture cannot prove an exact generation, so the
        # reopen is refused rather than guessed; the terminal must survive
        # that refusal and keep offering the menu until `c`.
        completed = self.launch("r\nc\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited with status 0", completed.stdout)
        self.assertGreaterEqual(
            completed.stdout.count("Provider exited: [r] reopen conversation"),
            2,
            completed.stdout,
        )
        self.assertIn("Exact recovery is not ready", completed.stderr)

    def test_clean_exit_menu_can_drop_to_a_shell(self) -> None:
        completed = self.launch("s\nexit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited with status 0", completed.stdout)
        self.assertIn("Shell opened", completed.stdout)
        self.assertIn("SOURCE_RETURNED", completed.stdout)

    def test_autoclose_marker_never_swallows_a_crash(self) -> None:
        # A non-zero exit is an incident; the opt-in marker must not close it.
        self.autoclose_on_clean_exit()
        write_executable(
            self.bin / "codex",
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$PROVIDER_LOG"\nexit 3\n',
        )
        completed = self.launch("c\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited with status 3", completed.stdout)
        self.assertIn("Provider exited: [r] reopen conversation", completed.stdout)

    def test_bashrc_documents_the_stay_alive_default(self) -> None:
        # The retired marker must not survive anywhere in the shipped bashrc.
        from tests.test_lifecycle_shell import BASHRC

        text = BASHRC.read_text(encoding="utf-8")
        self.assertNotIn("sk_keep_exit_menu", text)
        self.assertIn("sk_autoclose_on_clean_exit", text)
