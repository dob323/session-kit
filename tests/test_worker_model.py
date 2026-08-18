"""The launch gate's own rules, pinned.

``validate_requested_model`` sits on the LIVE launch path, the login shell
(bashrc/shpool.bashrc) and ``sp new`` both call the ``validate-worker-model``
verb before starting a provider. It moved into ``sessionkit_inventory`` in the
one-door rebuild (2026-08-12); these tests pin its rules so a future edit
cannot silently loosen what launches.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

from sessionkit_inventory.worker_model import (  # noqa: E402
    IntakeError,
    validate_requested_model,
)


class WorkerModelGateTests(unittest.TestCase):
    def test_claude_models_must_carry_the_claude_prefix(self) -> None:
        self.assertEqual(
            "claude-fable-5", validate_requested_model("claude", "claude-fable-5")
        )
        self.assertEqual(
            "claude-opus-5", validate_requested_model("claude", " claude-opus-5 ")
        )
        with self.assertRaises(IntakeError):
            validate_requested_model("claude", "gpt-5")

    def test_codex_models_must_carry_a_codex_family_prefix(self) -> None:
        for accepted in ("gpt-5.6", "o3-pro", "o4-mini", "codex-mini"):
            self.assertEqual(accepted, validate_requested_model("codex", accepted))
        with self.assertRaises(IntakeError):
            validate_requested_model("codex", "claude-opus-5")

    def test_only_the_two_providers_pass_the_gate(self) -> None:
        for provider in ("gemini", "", None, "CLAUDE", 7):
            with self.subTest(provider=provider):
                with self.assertRaises(IntakeError):
                    validate_requested_model(provider, "claude-opus-5")

    def test_identifier_bounds_and_character_set_hold(self) -> None:
        # Inherited contract, pinned as-is: an overlong identifier is TRUNCATED
        # to 128 characters before validation, not refused (the ported code
        # has always done this; changing a launch-path gate needs its own
        # decision, not a drive-by).
        self.assertEqual(
            128, len(validate_requested_model("claude", "claude-" + "x" * 130))
        )
        with self.assertRaises(IntakeError):
            validate_requested_model("claude", "claude one")
        with self.assertRaises(IntakeError):
            validate_requested_model("claude", "")
        with self.assertRaises(IntakeError):
            validate_requested_model("claude", None)
        with self.assertRaises(IntakeError):
            validate_requested_model("claude", ["claude-opus-5"])

    def test_error_type_is_a_value_error_for_the_facade_handler(self) -> None:
        # session_inventory.main() catches ValueError; the gate's refusals must
        # stay inside that net so the verb exits with a message, not a traceback.
        self.assertTrue(issubclass(IntakeError, ValueError))


if __name__ == "__main__":
    unittest.main()
