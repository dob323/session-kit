"""A clean provider exit closes the session; a crash reopens it once.

Contract (operator rule, 2026-08-11, replacing the stay-alive rule of the same
day): `/exit` — the provider ending with status 0 — is the operator saying
"done here". The managed shell ends with it, which ends the shpool session,
frees its terminal number into the ordinary quarantine, and lands the person
back at the picker. Nothing is left behind to reopen, and `/kit` is the verb
for the other intention: leave the conversation running and walk away.

A CRASH heals itself (D14). The conversation is reopened once, with one line
saying so; a second crash within a minute of that reopen stops there and hands
the window back to the picker with the session still open. No question is
asked on either path — no screen in the kit asks one.

Every deliberate close — the clean exit and `bye` — records intent, so the
crash queue can tell "somebody closed this" from "this was lost".
tests/test_close_intent.py owns that queue's side of the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from tests.test_lifecycle_shell import ProviderExitShellHarness, UUID


class CleanExitClosesTests(ProviderExitShellHarness):
    def test_a_clean_exit_closes_without_a_menu_and_says_so(self) -> None:
        completed = self.launch("")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited. Closing this session.", completed.stdout)
        self.assertNotIn("Provider exited:", completed.stdout)
        self.assertNotIn("crashed", completed.stdout)
        # The shell really ended rather than returning to the sourcing caller:
        # that is what ends the shpool session and frees the number.
        self.assertNotIn("SOURCE_RETURNED", completed.stdout)

    def test_a_clean_exit_records_the_intent_to_close(self) -> None:
        completed = self.launch("")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([f"codex:{UUID}"], sorted(self.close_intents()))

    def test_the_shipped_bashrc_no_longer_reads_the_retired_opt_in(self) -> None:
        # One policy. A machine that still has the old marker file must not
        # get a second behaviour out of it.
        from tests.test_lifecycle_shell import BASHRC

        text = BASHRC.read_text(encoding="utf-8")
        self.assertNotIn("sk_autoclose_on_clean_exit", text)
        self.assertNotIn("sk_keep_exit_menu", text)

    def test_bye_closes_without_a_question_and_without_an_id(self) -> None:
        """The line an operator meets most often broke two rules at once.

        It printed the raw shpool id -- which no screen may do -- and asked
        `[y/N]`, which no screen may do either. It closes and reports.
        """
        from tests.test_lifecycle_shell import BASHRC

        text = BASHRC.read_text(encoding="utf-8")
        body = text[text.index("  bye() {") : text.index("  bye() {") + 400]
        self.assertIn("Closed this session.", body)
        self.assertNotIn("SHPOOL_SESSION_NAME", body)
        self.assertNotIn("[y/N]", body)
        self.assertNotIn("read -r", body)
        self.assertNotIn("Close cancelled", text)


class CrashSelfHealsTests(ProviderExitShellHarness):
    def test_a_crash_reopens_the_conversation_once_and_says_so(self) -> None:
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited with status 3", completed.stdout)
        self.assertEqual(1, completed.stdout.count("Codex crashed. Reopened."))
        # A reopen the guard refuses hands the window back in one step, with
        # the session still open and still reopenable. No picker is reachable
        # in this harness, so the hand-back falls to a shell.
        self.assertIn("nothing reopened", completed.stderr)
        self.assertIn("Shell opened", completed.stdout)
        self.assertIn("SOURCE_RETURNED", completed.stdout)
        # Nothing was closed, so nothing is tombstoned.
        self.assertEqual([], sorted(self.close_intents()))

    def test_a_closed_crash_comes_back_as_the_conversation_not_as_history(
        self,
    ) -> None:
        """The hard condition on closing a crashed session.

        Closing is only better than the husk if the conversation survives it.
        Their session 99 was a Claude session whose provider had exited, and the
        close recorded it as a `shell` row with no uuid -- "history only",
        scrollback and nothing else, unrestorable as a conversation. Ending a
        crashed session into THAT shape would be worse than leaving it open,
        because the session that still knew the conversation would be gone.

        So this asserts what a person gets back: the exact conversation, by
        uuid, under its real name, marked restorable by the same reader the
        picker and `sp recover` use.
        """
        # R3: the row it lands in carries the session's REAL name. The shell
        # closing itself knows only its own ID, so the name comes from the
        # last list the collector wrote -- assert against a session that has
        # one, or an empty title passes every "not a placeholder" check.
        self.remembered_name("Exit Ruling Work")
        self.provider_transcript()
        self.reopen_answers(76)
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("the conversation is in Closed sessions", completed.stdout)

        ledger = (
            self.home / ".local/share/session-kit/closed-sessions.jsonl"
        )
        self.assertTrue(ledger.is_file(), "no closed-sessions row was written")
        rows = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(1, len(rows), rows)
        row = rows[0]
        # The conversation, not a shell husk of it.
        self.assertEqual("codex", row["provider"])
        self.assertEqual(UUID, row["uuid"])
        # Its real name, not "the shell session", "v2-35", "Idle shell", or
        # the machine-made "Codex in <dir> at <time>".
        self.assertEqual("Exit Ruling Work", row["title"])
        self.assertEqual(str(self.project), row["cwd"])

        # And the reader every restore surface goes through agrees it can be
        # brought back, rather than listing it as history.
        listed = self.closed_sessions_list()
        self.assertEqual(1, len(listed), listed)
        self.assertTrue(listed[0]["restorable"], listed[0])
        self.assertEqual(UUID, listed[0]["uuid"])
        self.assertEqual("Exit Ruling Work", listed[0]["title"])

    def test_a_momentary_record_failure_is_retried_not_made_permanent(
        self,
    ) -> None:
        """One failed write used to strand the row forever.

        The lifecycle record is what makes a row say "provider exited", and
        automatic cleanup only ever considers a row that says it. A single
        failed attempt therefore left a session that looked like an ordinary
        idle shell for good: nothing closed it, and nothing said why.
        """
        log = self.provider_exit_record_fails(times=1)
        completed = self.launch("")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(2, len(log.read_text(encoding="utf-8").split()))
        # The retry succeeded, so the ordinary clean-exit close still happened.
        self.assertIn("Codex exited. Closing this session.", completed.stdout)
        self.assertEqual([f"codex:{UUID}"], sorted(self.close_intents()))

    def test_a_record_that_cannot_be_written_says_so_and_hands_back(
        self,
    ) -> None:
        """When both attempts fail, the person is told AND returned.

        This test was named "...and hands back" and never asserted the hand
        back. It could not have passed if it had: the call sat ~300 lines
        above the function's definition in a file that runs top to bottom, so
        the one path meant to stop the shell dropping into a prompt nobody
        asked for printed `__sk_leave_after_provider_exit: command not found`
        and dropped into a prompt nobody asked for (found in review,
        2026-08-15). A test that asserts a message but not the action is how
        that survives a suite.
        """
        self.provider_exit_record_fails()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("its record could not be written", completed.stderr)
        self.assertIn("Close it from the picker", completed.stdout)
        # THE HAND-BACK ITSELF. No picker is reachable in this fixture, so it
        # falls to the managed shell -- which is still a hand-back, and still
        # says so, unlike a bare prompt.
        self.assertIn("Shell opened", completed.stdout)
        self.assertNotIn("command not found", completed.stderr)
        # Nothing was tombstoned: there was no proof to tombstone.
        self.assertEqual([], sorted(self.close_intents()))

    def test_asking_whether_a_close_would_keep_the_conversation_closes_nothing(
        self,
    ) -> None:
        """The question must not be the answer.

        `lifecycle closed` used to do two jobs at once: when the session had
        no exact conversation it wrote a history-only `shell` row on the
        closed list AND reported that the conversation was not kept. The
        crash path calls it to ASK, so asking alone put a row on the Closed
        list for a session that is still open and still running -- a close
        the person never made, listed under a session they can still type
        into. A Codex session started with `sp new` is exactly that shape:
        the thread ID is allocated inside the TUI, so the shell never has it.

        Asking now changes nothing at all.
        """
        self.without_a_conversation()
        self.reopen_answers(76)
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        # It stayed open: the session is still the only thing that knows
        # which conversation this was.
        self.assertIn("SOURCE_RETURNED", completed.stdout)
        ledger = self.home / ".local/share/session-kit/closed-sessions.jsonl"
        self.assertFalse(
            ledger.exists(),
            "asking put a closed row on the list for a session that is still open:"
            f" {ledger.read_text(encoding='utf-8') if ledger.exists() else ''}",
        )
        self.assertEqual([], sorted(self.close_intents()))

    def test_a_failed_history_only_append_is_reported_not_discarded(self) -> None:
        """A shell-history close uses the same checked success choke point."""

        self.without_a_conversation()
        self.ledger_cannot_be_written()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("shell-history row could not be added", completed.stderr)
        self.assertNotIn("stays in the recovery list", completed.stderr)
        self.assertEqual([], self.ledger_rows())
        self.assertEqual([], sorted(self.close_intents()))

    def test_a_session_kept_open_says_why_in_words(self) -> None:
        """Keeping it is a decision, so it is explained, not silent.

        "Back to the picker" left the person with a row that would sit in
        the list until the 72-hour reaper and no idea why this one did not
        close like the others.
        """
        self.without_a_conversation()
        self.reopen_answers(76)
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(
            "could not promise the conversation back",
            completed.stdout + completed.stderr,
        )

    def test_a_session_marked_keep_is_not_closed_by_a_crash(self) -> None:
        """`keep_session` outranks the automatic close, and says so.

        `keep_session` means "automatic cleanup is off for this session", and
        the reaper has always obeyed it (reaper.py `_safe_candidate` requires
        `provider_exit_keep is False`). Turning the crash hand-back into a
        close created a SECOND automatic closer, and it did not obey anything
        -- a session the person had deliberately pinned would have ended
        because its provider died. It stays, and the reason it stays is the
        real one rather than the one that fits the other case.

        `bye` and a clean provider exit still close a kept session: those are
        the person asking, and keep was never about them.
        """
        log = self.reopen_answers(76)
        self.keep_this_session()
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("SOURCE_RETURNED", completed.stdout)
        self.assertIn("because you asked to keep it", completed.stdout)
        self.assertNotIn("could not promise the conversation back", completed.stdout)
        self.assertNotIn("Closing this session", completed.stdout)
        # Nothing closed, so nothing tombstoned and nothing on the list.
        self.assertEqual([], sorted(self.close_intents()))
        self.assertFalse(
            (self.home / ".local/share/session-kit/closed-sessions.jsonl").exists()
        )
        # It really did reach the close decision rather than stopping earlier.
        self.assertEqual(["reopen"], log.read_text(encoding="utf-8").split())

    def test_a_clean_exit_says_when_the_conversation_cannot_come_back(
        self,
    ) -> None:
        """A close the PERSON asked for still happens, and still says so.

        The automatic close refuses when the conversation cannot be read
        back. A clean `/exit` is the person deciding, so it goes ahead -- but
        the ledger row it writes will be filtered straight out of Closed
        sessions as unrestorable, and letting them walk away believing
        otherwise is the same broken promise in a politer voice.
        """
        completed = self.launch("")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited. Closing this session.", completed.stdout)
        self.assertIn("cannot be read back from this machine", completed.stderr)

    def test_a_clean_exit_with_a_readable_conversation_says_nothing_extra(
        self,
    ) -> None:
        """...and stays quiet when the promise is good."""
        self.provider_transcript()
        completed = self.launch("")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited. Closing this session.", completed.stdout)
        self.assertNotIn("cannot be read back", completed.stderr)
        self.assertNotIn("Note:", completed.stderr)

    def test_a_record_from_a_previous_shell_of_this_id_closes_nothing(
        self,
    ) -> None:
        """A session id is not an identity; a shell generation is.

        shpool ids get reused. `main2` closes, a new `main2` opens, and the old
        lifecycle document survives until a collector pass prunes it. The close
        loaded that document by session id alone -- `_prove_lifecycle_caller`
        checks the CALLER against /proc and says nothing about the record --
        so a fresh shell could tombstone the previous occupant's conversation
        and put its own name on the row. `update_state` had refused exactly
        this since it was written; the close had not (found in review,
        2026-08-15).
        """
        self.remembered_name("Exit Ruling Work")
        self.provider_transcript()
        self.reopen_answers(76)
        self.lifecycle_record_from_another_generation()
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        # Nothing of the previous generation was touched.
        self.assertEqual([], sorted(self.close_intents()))
        self.assertEqual([], self.closed_sessions_list())
        self.assertIn("SOURCE_RETURNED", completed.stdout)

    def test_a_reopen_refusal_never_closes_even_when_everything_else_is_ready(
        self,
    ) -> None:
        """A refusal is not evidence that a conversation is finished.

        `lifecycle reopen` refuses for six different reasons, and one of them
        is that the trusted inventory says A PROVIDER IS ALREADY RUNNING in
        this terminal. The shell used to fold every status that was not 76
        into the close path without reading the reason -- so the code proved a
        provider was alive and then ended the session anyway (found in review,
        2026-08-15).

        Everything else here is ready: exact conversation, readable
        transcript, real name, writable ledger. The close still must not
        happen, because the one missing thing is any evidence the conversation
        is over.
        """
        self.remembered_name("Exit Ruling Work")
        self.provider_transcript()
        self.reopen_answers(1)
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        # The session is still here.
        self.assertIn("SOURCE_RETURNED", completed.stdout)
        self.assertNotIn("Closing this session", completed.stdout)
        # Nothing was recorded in either register.
        self.assertEqual([], sorted(self.close_intents()))
        self.assertEqual([], self.closed_sessions_list())
        # And the person is told which of the two facts is missing.
        self.assertIn("a refusal is not proof", completed.stdout)

    def test_a_ledger_that_cannot_be_written_keeps_the_session(self) -> None:
        """The bitter one: the fix for the husk carried the husk's own bug.

        `_record_closed_session` catches an append failure and answers
        `recorded: false`; the close verb threw that answer away and printed
        `recorded: true` regardless. The shell read that as permission, told
        the person "the conversation is in Closed sessions", and ended the
        session -- while the tombstone it had already written told crash
        recovery not to offer the conversation either. Reachable from nowhere
        at all, which is the exact outcome this branch exists to prevent
        (found in review, 2026-08-15).

        The ledger row is written FIRST now and its result is read. No row
        means no tombstone and no close.
        """
        self.remembered_name("Exit Ruling Work")
        self.provider_transcript()
        self.ledger_cannot_be_written()
        self.reopen_answers(76)
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        # It stayed, and it did not claim the conversation had been filed.
        self.assertIn("SOURCE_RETURNED", completed.stdout)
        self.assertNotIn("the conversation is in Closed sessions", completed.stdout)
        # NOTHING was tombstoned: a tombstone with no row is the trap.
        self.assertEqual([], sorted(self.close_intents()))
        self.assertIn("Closed sessions list could not be written", completed.stdout)

    def test_a_conversation_with_no_transcript_on_this_machine_is_not_closed(
        self,
    ) -> None:
        """A valid UUID is not a conversation you can get back.

        The probe proved only that the id was well-formed. The closed-sessions
        list drops any row whose transcript this machine cannot read, so
        closing on the id alone ended the live session AND produced a row the
        person would never see (found in review, 2026-08-15).
        """
        self.remembered_name("Exit Ruling Work")
        self.reopen_answers(76)
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("SOURCE_RETURNED", completed.stdout)
        self.assertEqual([], sorted(self.close_intents()))
        self.assertEqual([], self.closed_sessions_list())
        self.assertIn("cannot be read back from this machine", completed.stdout)

    def test_a_transcript_that_exists_but_cannot_be_read_is_not_closed(
        self,
    ) -> None:
        """Locating a file is not reading it.

        A transcript with mode 000 -- a botched chmod, an archive restored
        without its bits, a file owned by another account -- was located
        happily, reported `restorable: true`, and would have been closed on.
        The floor is an actual read now, in the close AND in the list.
        """
        self.remembered_name("Exit Ruling Work")
        self.provider_transcript(mode=0o000)
        self.reopen_answers(76)
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("SOURCE_RETURNED", completed.stdout)
        self.assertEqual([], sorted(self.close_intents()))
        self.assertIn("cannot be read back from this machine", completed.stdout)

    def test_a_session_that_never_told_the_shell_its_uuid_can_still_close(
        self,
    ) -> None:
        """The husk class that could never be closed at all.

        A provider-exit record with no conversation had no way out. The
        shell would not close it, because closing would have lost the
        conversation; and the reaper's auto-close refuses any candidate
        without a uuid (reaper.py `_safe_candidate`), so the 72-hour
        backstop would never reach it either. Every Codex session started
        with `sp new` whose provider then died is this shape, and each one
        stayed in the list until somebody cleared it by hand.

        The conversation was not actually unknown. The collector proved it
        while the provider was live and kept it against exactly this
        handoff, bound to this boot, this shell PID and this shell start.
        The close reads that record now, so the session ends and the
        conversation is restorable by name.
        """
        self.without_a_conversation()
        self.remembered_name("Exit Ruling Work")
        self.provider_transcript()
        self.reopen_answers(76)
        self.collector_kept_the_exact_conversation()
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        # It closed: the shell ended instead of returning to its caller.
        self.assertIn("the conversation is in Closed sessions", completed.stdout)
        self.assertNotIn("SOURCE_RETURNED", completed.stdout)
        # And it closed as the conversation, under its real name.
        self.assertEqual([f"codex:{UUID}"], sorted(self.close_intents()))
        listed = self.closed_sessions_list()
        self.assertEqual(1, len(listed), listed)
        self.assertEqual("codex", listed[0]["provider"])
        self.assertEqual(UUID, listed[0]["uuid"])
        self.assertEqual("Exit Ruling Work", listed[0]["title"])
        self.assertTrue(listed[0]["restorable"], listed[0])

    def test_no_screen_offers_a_letter_menu_after_a_crash(self) -> None:
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        for banned in ("[r]", "[p]", "[s]", "[k]", "[c]", "Choice:", "Unknown choice"):
            self.assertNotIn(banned, completed.stdout, banned)

    def test_a_second_crash_inside_a_minute_stops_at_the_picker(self) -> None:
        self.provider_transcript()
        log = self.reopen_answers(76)
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(1, completed.stdout.count("Codex crashed. Reopened."))
        # CONTRACT INVERTED, deliberately (operator ruling, 2026-08-15): "the
        # session closes at once and lands in Closed sessions, recoverable,
        # under its REAL name." This used to hand the window back with the
        # session still alive and assert that NOTHING was tombstoned; the row
        # then sat in their list saying "provider exited" until the 72-hour
        # reaper. It closes now -- but only when the close keeps the
        # conversation, which the next test proves.
        self.assertIn(
            "Codex crashed twice. Closing this session; the conversation is in"
            " Closed sessions.",
            completed.stdout,
        )
        # Exactly one reopen: the loop stops instead of running forever.
        self.assertEqual(["reopen"], log.read_text(encoding="utf-8").split())
        self.assertEqual([f"codex:{UUID}"], sorted(self.close_intents()))

    def test_a_reopened_conversation_that_exits_cleanly_closes_the_session(
        self,
    ) -> None:
        log = self.reopen_answers(0)
        self.crashing_provider()
        completed = self.launch("")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex crashed. Reopened.", completed.stdout)
        self.assertIn("Codex exited. Closing this session.", completed.stdout)
        self.assertNotIn("SOURCE_RETURNED", completed.stdout)
        self.assertEqual(["reopen"], log.read_text(encoding="utf-8").split())
        # A conversation the operator finished is a deliberate close.
        self.assertEqual([f"codex:{UUID}"], sorted(self.close_intents()))


if __name__ == "__main__":
    import unittest

    unittest.main()
