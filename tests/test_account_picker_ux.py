"""The two account pickers: one vocabulary, one Enter key, refusals that quote
the row.

`sp account choices` is the only thing that decides whether an account can be
selected, and it says so per row in a `state` string. These tests hold both
shell renderers -- the new-session wizard step and the account switch -- to
printing that string verbatim, numbering every row including the ones they will
refuse, naming the refused row's own state when a person picks it, and staying
on the step afterwards instead of throwing the wizard away.

The renderers are bash with embedded python, so the harness below sources the
module, replaces the four picker services it calls (temp files, the modal read,
the proof action, the refresh) and drives the real functions with a fabricated
choices document -- the same shape `sp account choices` prints.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from tests.support import REPO, run


ACTIONS = REPO / "lib" / "sh" / "shpool_login_actions.sh"
SP = REPO / "bin" / "sp"

HARNESS = r"""
set -u
source "$ACTIONS_MODULE"

sp_stub() {
  # The only call these functions make: sp account choices <provider>.
  cat "$CHOICES_JSON"
}
SP_CMD=sp_stub
TEMP_FILES=()
NEW_TEMP=
CONFIRM_FORGIVE=0

new_temp() {
  NEW_TEMP=$(mktemp "$SANDBOX/$1.XXXXXX") || return 1
  TEMP_FILES+=("$NEW_TEMP")
}

ANSWERS=()
mapfile -t ANSWERS < "$ANSWERS_FILE"
ANSWER_INDEX=0
picker_modal_read() {
  local __variable=$1 __prompt=$2
  if (( ANSWER_INDEX >= ${#ANSWERS[@]} )); then
    printf '%s[input closed]\n' "$__prompt"
    return 1
  fi
  printf '%s%s\n' "$__prompt" "${ANSWERS[ANSWER_INDEX]}"
  printf -v "$__variable" '%s' "${ANSWERS[ANSWER_INDEX]}"
  ANSWER_INDEX=$((ANSWER_INDEX + 1))
}

run_proof_action() {
  printf 'proof-action %s\n' "$*"
}
refresh_after_action() { :; }
"""

GUIDED_TAIL = """
guided_account "$PROVIDER"
printf 'rc=%s\\n' "$?"
printf 'account=%s\\n' "${GUIDED_ACCOUNT:-}"
"""

SWITCH_TAIL = """
change_account_number 6 "$PROVIDER"
printf 'rc=%s\\n' "$?"
"""


def account(
    alias: str,
    email: str,
    *,
    eligible: bool = True,
    state: str | None = None,
    plan: str = "max 20x",
    recommended: bool = False,
    u5h: float | None = None,
    u7d: float | None = None,
) -> dict:
    return {
        "alias": alias,
        "email": email,
        "plan": plan,
        "eligible": eligible,
        "state": state if state is not None else ("ready" if eligible else "blocked"),
        "health": "ok" if eligible else "expired",
        "serving": False,
        "u5h": u5h,
        "u7d": u7d,
        "recommended": recommended,
    }


def document(
    *rows: dict,
    recommendation: str | None = None,
    reason: str = "",
    roster_state: str = "fresh",
    advice_fresh: bool = True,
    blocked: dict | None = None,
) -> dict:
    return {
        "schema_version": 3,
        "provider": "claude",
        "roster_fresh": roster_state == "fresh",
        "roster_state": roster_state,
        "advice_fresh": advice_fresh,
        "recommendation": recommendation,
        "recommendation_reason": reason,
        "recommendation_blocked": blocked,
        "choices": list(rows),
    }


class PickerHarness(unittest.TestCase):
    """Runs one real picker function against a fabricated choices document."""

    def drive(
        self,
        choices: dict,
        answers: list[str],
        *,
        surface: str = "guided",
        provider: str = "claude",
    ) -> str:
        fixture_root = Path(
            os.environ.get("SESSION_KIT_TEST_EXEC_ROOT", os.fspath(REPO))
        )
        with tempfile.TemporaryDirectory(
            prefix=".account-picker-", dir=fixture_root
        ) as name:
            sandbox = Path(name)
            choices_path = sandbox / "choices.json"
            choices_path.write_text(json.dumps(choices), encoding="utf-8")
            answers_path = sandbox / "answers"
            answers_path.write_text(
                "".join(f"{line}\n" for line in answers), encoding="utf-8"
            )
            script = HARNESS + (
                GUIDED_TAIL if surface == "guided" else SWITCH_TAIL
            )
            result = run(
                ["bash", "-c", script],
                env={
                    "ACTIONS_MODULE": os.fspath(ACTIONS),
                    "CHOICES_JSON": os.fspath(choices_path),
                    "ANSWERS_FILE": os.fspath(answers_path),
                    "SANDBOX": os.fspath(sandbox),
                    "PROVIDER": provider,
                },
                check=False,
            )
            self.assertEqual(
                "", result.stderr, f"the picker wrote to stderr:\n{result.stderr}"
            )
            return result.stdout


class AccountRowVocabularyTests(PickerHarness):
    def test_every_row_prints_the_state_the_core_decided(self) -> None:
        output = self.drive(
            document(
                account("wren", "wren@example.com"),
                account(
                    "old",
                    "old@example.com",
                    eligible=False,
                    state="blocked: health expired",
                ),
            ),
            ["b"],
        )
        self.assertIn("   1  wren: wren@example.com | max 20x | ready", output)
        self.assertIn(
            "   2  old: old@example.com   | max 20x | blocked: health expired | —", output
        )

    def test_the_switch_selector_prints_state_and_the_recommended_marker(self) -> None:
        output = self.drive(
            document(
                account("wren", "wren@example.com"),
                account("work", "work@example.com", recommended=True),
                account(
                    "old",
                    "old@example.com",
                    eligible=False,
                    state="blocked: not in the account roster",
                ),
                recommendation="work",
            ),
            ["b"],
            surface="switch",
        )
        self.assertIn("Change to account", output)
        # Padded to columns: the alias+email field is variable width and every
        # field behind it used to ragged-edge (operator ruling, 2026-08-15).
        self.assertIn(
            "work: work@example.com | max 20x | ready"
            "                              | — | recommended",
            output,
        )
        self.assertIn("blocked: not in the account roster", output)

    def test_usage_is_printed_per_row_when_the_feed_carries_it(self) -> None:
        output = self.drive(
            document(
                account("wren", "wren@example.com", u5h=0.34, u7d=0.36),
                account("work", "work@example.com"),
            ),
            ["b"],
        )
        self.assertIn("wren@example.com | max 20x | ready | 5h 34% · 7d 36%", output)
        # A row the feed says nothing about prints no usage rather than zeroes.
        self.assertNotIn("work@example.com | max 20x | ready | 5h", output)

    def test_the_recommendation_reason_is_printed_under_the_list(self) -> None:
        output = self.drive(
            document(
                account("wren", "wren@example.com"),
                account("work", "work@example.com", recommended=True),
                recommendation="work",
                reason="weekly headroom is highest here",
            ),
            ["b"],
        )
        self.assertIn("Why work: weekly headroom is highest here", output)

    def test_a_stale_feed_and_a_blocked_recommendation_are_both_named(self) -> None:
        output = self.drive(
            document(
                account("wren", "wren@example.com", state="ready (feed stale)"),
                account(
                    "work",
                    "work@example.com",
                    eligible=False,
                    state="blocked: health expired",
                ),
                roster_state="stale",
                advice_fresh=False,
                blocked={"alias": "work", "state": "blocked: health expired"},
            ),
            ["b"],
        )
        self.assertIn("Account health is unknown. Every enabled account is offered.", output)
        self.assertIn("Rotation advice is stale or absent", output)
        self.assertIn(
            "work is advised but not selectable: blocked: health expired", output
        )

    def test_a_list_with_nothing_selectable_says_so(self) -> None:
        output = self.drive(
            document(
                account(
                    "wren",
                    "wren@example.com",
                    eligible=False,
                    state="blocked: profile disabled",
                )
            ),
            ["b"],
        )
        self.assertIn("No account here is selectable", output)


class AccountRefusalTests(PickerHarness):
    def test_a_blocked_row_is_offered_and_refused_with_its_own_state(self) -> None:
        output = self.drive(
            document(
                account("wren", "wren@example.com"),
                account(
                    "old",
                    "old@example.com",
                    eligible=False,
                    state="blocked: health expired",
                ),
            ),
            ["2", "1"],
        )
        self.assertIn("Account 2 (old) is not selectable: blocked: health expired.", output)
        self.assertIn("rc=0", output)
        self.assertIn("account=wren", output)

    def test_a_refused_pick_asks_again_inside_the_account_step(self) -> None:
        output = self.drive(
            document(
                account("wren", "wren@example.com"),
                account("old", "old@example.com", eligible=False),
            ),
            ["2", "9", "1"],
        )
        # Three prompts for three answers: the step never unwound to the
        # dashboard between them.
        self.assertEqual(3, output.count("account ❯"))
        self.assertIn("account=wren", output)

    def test_an_out_of_range_pick_is_not_the_eligibility_refusal(self) -> None:
        output = self.drive(
            document(account("wren", "wren@example.com")),
            ["4", "b"],
        )
        self.assertIn("There is no account 4 on this screen. Numbers shown here work.", output)
        self.assertNotIn("not selectable", output)

    def test_a_typed_word_is_told_what_the_step_accepts(self) -> None:
        output = self.drive(
            document(account("wren", "wren@example.com")),
            ["wren", "b"],
        )
        self.assertIn("That is not an account number. Numbers shown here work.", output)

    def test_b_backs_out_one_level_without_an_account(self) -> None:
        output = self.drive(
            document(
                account("wren", "wren@example.com"),
                account("work", "work@example.com"),
            ),
            ["b"],
        )
        self.assertIn("rc=1", output)
        self.assertIn("account=\n", output)

    def test_the_switch_selector_refuses_with_the_row_and_asks_again(self) -> None:
        output = self.drive(
            document(
                account("wren", "wren@example.com"),
                account(
                    "old",
                    "old@example.com",
                    eligible=False,
                    state="blocked: health error",
                ),
            ),
            ["2", "1", "y"],
            surface="switch",
        )
        self.assertIn("Account 2 (old) is not selectable: blocked: health error.", output)
        self.assertEqual(2, output.count("target account ❯"))
        self.assertIn("proof-action picker-account-switch 6 wren", output)

    def test_an_unusable_alias_from_the_core_is_named(self) -> None:
        output = self.drive(
            document(account("Not An Alias", "odd@example.com")),
            ["1"],
        )
        self.assertIn("The account list is unreadable. Nothing changed.", output)
        self.assertIn("rc=1", output)


class AccountEnterKeyTests(PickerHarness):
    def test_enter_takes_the_recommended_account(self) -> None:
        """One grammar on every screen (operator ruling, 2026-08-15).

        Enter used to be Back here while the very next step of the same wizard
        made Enter "use the default project", so one key meant two things
        depending on which screen you were on. It now takes the recommended
        choice wherever a screen has one. Switching a RUNNING session's account
        is deliberately still not a default -- see the switch test below.
        """
        output = self.drive(
            document(
                account("wren", "wren@example.com"),
                account("work", "work@example.com", recommended=True),
                recommendation="work",
                reason="weekly headroom is highest here",
            ),
            [""],
        )
        self.assertIn("rc=0", output)
        self.assertIn("account=work", output)
        self.assertIn("↵ use work", output)
        self.assertIn("b back", output)

    def test_the_recommended_account_is_taken_by_its_number(self) -> None:
        output = self.drive(
            document(
                account("wren", "wren@example.com"),
                account("work", "work@example.com", recommended=True),
                recommendation="work",
            ),
            ["2"],
        )
        self.assertIn("2 is the recommended work", output)
        self.assertIn("account=work", output)

    def test_the_sole_selectable_account_is_the_enter_default(self) -> None:
        output = self.drive(
            document(
                account("wren", "wren@example.com"),
                account(
                    "old",
                    "old@example.com",
                    eligible=False,
                    state="blocked: health expired",
                ),
            ),
            [""],
        )
        self.assertIn("↵ use wren", output)
        self.assertIn("account=wren", output)

    def test_two_selectable_accounts_have_no_enter_default(self) -> None:
        output = self.drive(
            document(
                account("wren", "wren@example.com"),
                account("work", "work@example.com"),
            ),
            [""],
        )
        self.assertIn("↵ back", output)
        self.assertIn("rc=1", output)
        self.assertIn("account=\n", output)

    def test_the_switch_selector_never_defaults_to_a_change(self) -> None:
        output = self.drive(
            document(account("wren", "wren@example.com")),
            [""],
            surface="switch",
        )
        self.assertIn("↵ back", output)
        self.assertNotIn("proof-action", output)


class AccountEmptyListTests(PickerHarness):
    def test_the_enrolment_message_replaces_the_list_and_the_footer(self) -> None:
        output = self.drive(document(), [])
        self.assertIn("No claude account is enrolled", output)
        self.assertNotIn("Account\n", output)
        self.assertNotIn("Back", output)
        self.assertNotIn("account ❯", output)

    def test_the_switch_selector_says_there_is_nothing_to_change_to(self) -> None:
        output = self.drive(document(), [], surface="switch", provider="codex")
        self.assertIn("No codex account is enrolled", output)
        self.assertNotIn("target account ❯", output)


class AccountHelpTests(unittest.TestCase):
    """`sp help accounts` has to explain the rule the picker enforces."""

    def help_text(self) -> str:
        with tempfile.TemporaryDirectory(
            prefix=".account-help-",
            dir=Path(os.environ.get("SESSION_KIT_TEST_EXEC_ROOT", os.fspath(REPO))),
        ) as name:
            home = Path(name) / "home"
            home.mkdir()
            return run(
                [SP, "help", "accounts"], env={"HOME": os.fspath(home)}
            ).stdout

    def test_help_states_the_eligibility_rule(self) -> None:
        text = self.help_text()
        self.assertIn("health", text)
        self.assertIn("serving", text)

    def test_help_names_the_feed_dependency_and_its_failure_mode(self) -> None:
        text = self.help_text()
        self.assertIn("configure-feeds", text)
        self.assertIn("stale", text)

    def test_help_quotes_the_refusal_a_person_will_see(self) -> None:
        text = self.help_text()
        self.assertIn("not selectable", text)
