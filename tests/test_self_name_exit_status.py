"""`sp self-name` must not report success for a name that did not take.

A root agent is told to name itself on its first substantive turn and to say
`name failed` when that does not work. The command used to `return 0` whatever
came back, so a self-name that never landed was indistinguishable from one that
did. Observed live on 2026-08-17: the call met the collector jam, was killed
while waiting, and the session ran for three hours unnamed with nothing in its
exit status to act on.
"""

from __future__ import annotations

import unittest
from unittest import mock

import sys

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))

import session_inventory  # noqa: E402


CALLER = {"provider": "claude", "uuid": "2ace3498-903c-4ba6-b0f1-8e87edb6adac"}
KEY = "claude:2ace3498-903c-4ba6-b0f1-8e87edb6adac"
TITLE = "Session Kit README Remodel"


def result(*, stored=TITLE, state="ready"):
    aliases = {KEY: stored} if stored is not None else {}
    return {
        "caller": dict(CALLER),
        "title": TITLE,
        "aliases": aliases,
        "automatic_name_state": state,
    }


class SelfNameExitStatusTests(unittest.TestCase):
    def run_with(self, payload):
        with (
            mock.patch.object(
                session_inventory, "self_name_automatic_title", return_value=payload
            ),
            mock.patch.object(session_inventory, "_json_print"),
        ):
            return session_inventory._self_name_command({}, TITLE)

    def test_a_name_that_landed_and_shows_is_success(self) -> None:
        self.assertEqual(0, self.run_with(result()))

    def test_a_name_absent_from_the_written_document_fails(self) -> None:
        """The exact silent failure: the call returned, the name did not land."""

        self.assertEqual(1, self.run_with(result(stored=None)))

    def test_a_name_written_for_a_different_title_fails(self) -> None:
        self.assertEqual(1, self.run_with(result(stored="Something Else")))

    def test_a_name_recorded_but_not_showing_is_not_success(self) -> None:
        """`pending` means a retry is queued; the caller is not named yet."""

        self.assertEqual(1, self.run_with(result(state="pending")))

    def test_the_payload_is_printed_even_when_the_status_is_a_refusal(self) -> None:
        printed = []
        with (
            mock.patch.object(
                session_inventory,
                "self_name_automatic_title",
                return_value=result(stored=None),
            ),
            mock.patch.object(session_inventory, "_json_print", printed.append),
        ):
            session_inventory._self_name_command({}, TITLE)
        self.assertEqual(1, len(printed))


if __name__ == "__main__":
    unittest.main()
