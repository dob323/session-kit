"""The kit never changes a model by itself, and says so before a session runs.

The launch gate only ever checked the *shape* of a model identifier. A
well-formed name that this machine answers with something smaller passed it,
the flag went on the command line, and the session ran for its whole life on a
model nobody chose, the exact way a Fable request has been quietly served by
a much smaller model on this estate before.

So a second question is asked before anything starts: is this model really
served here? Two local answers exist (what was served the last time it was
asked for, and the machine's own model list), and with neither the answer is
"unknown", said as unknown. Nothing is ever substituted: a refusal names what
would serve the request and leaves the choice where it belongs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import tempfile
import unittest

from lib.sessionkit_inventory import worker_model
from tests.support import REPO, run
from tests.test_commands import CommandFixture

CORE = REPO / "lib" / "session_inventory.py"
SP = REPO / "bin" / "sp"


class AvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-models.")
        self.state = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def availability(self, model: str, **environ: str) -> dict:
        return worker_model.availability(
            "claude", model, state_dir=self.state, environ=environ
        )

    def test_a_model_on_the_machines_list_starts_a_session(self) -> None:
        verdict = self.availability(
            "claude-opus-5", SESSION_KIT_TUI_MODELS="claude-opus-5,claude-sonnet-4-5"
        )
        self.assertEqual(worker_model.OFFERED, verdict["verdict"])
        self.assertNotIn(verdict["verdict"], worker_model.REFUSALS)

    def test_a_model_the_machine_does_not_offer_is_refused_with_the_list(self) -> None:
        verdict = self.availability(
            "claude-fable-5", SESSION_KIT_TUI_MODELS="claude-opus-5,claude-sonnet-4-5"
        )
        self.assertEqual(worker_model.NOT_OFFERED, verdict["verdict"])
        self.assertIn(verdict["verdict"], worker_model.REFUSALS)
        self.assertEqual(["claude-opus-5", "claude-sonnet-4-5"], verdict["offered"])
        message = worker_model.render_availability(verdict, flag="--model-anyway")
        self.assertIn("You asked for claude-fable-5.", message)
        self.assertIn("claude-opus-5, claude-sonnet-4-5", message)
        self.assertIn("--model-anyway", message)

    def test_a_recorded_downgrade_refuses_and_names_what_really_serves_it(
        self,
    ) -> None:
        worker_model.record_served(
            self.state, "claude", "claude-fable-5", "claude-haiku-4-5"
        )
        verdict = self.availability("claude-fable-5")
        self.assertEqual(worker_model.DOWNGRADED, verdict["verdict"])
        self.assertEqual("claude-haiku-4-5", verdict["serves"])
        message = worker_model.render_availability(verdict, flag="--model-anyway")
        self.assertIn("actually ran on claude-haiku-4-5", message)
        self.assertIn("--model claude-haiku-4-5", message)

    def test_an_observation_outranks_the_list_a_machine_declares(self) -> None:
        """The list is what the operator believes; the observation is what happened."""
        worker_model.record_served(
            self.state, "claude", "claude-fable-5", "claude-haiku-4-5"
        )
        verdict = self.availability(
            "claude-fable-5", SESSION_KIT_TUI_MODELS="claude-fable-5"
        )
        self.assertEqual(worker_model.DOWNGRADED, verdict["verdict"])

    def test_a_model_that_served_before_is_confirmed_by_that(self) -> None:
        worker_model.record_served(
            self.state, "claude", "claude-opus-5", "claude-opus-5"
        )
        verdict = self.availability("claude-opus-5")
        self.assertEqual(worker_model.SERVED, verdict["verdict"])

    def test_with_nothing_to_go_on_the_answer_is_unknown_not_approved(self) -> None:
        verdict = self.availability("claude-opus-5")
        self.assertEqual(worker_model.UNKNOWN, verdict["verdict"])
        self.assertNotIn(verdict["verdict"], worker_model.REFUSALS)
        self.assertIn("nothing here can confirm it", verdict["reason"])

    def test_the_newest_observation_is_the_one_that_counts(self) -> None:
        worker_model.record_served(
            self.state, "claude", "claude-fable-5", "claude-haiku-4-5", now_unix_ms=1
        )
        worker_model.record_served(
            self.state, "claude", "claude-fable-5", "claude-fable-5", now_unix_ms=2
        )
        self.assertEqual(
            worker_model.SERVED, self.availability("claude-fable-5")["verdict"]
        )

    def test_nothing_here_ever_returns_a_model_that_was_not_asked_for(self) -> None:
        """The whole point: a verdict, never a substitution."""
        worker_model.record_served(
            self.state, "claude", "claude-fable-5", "claude-haiku-4-5"
        )
        for model in ("claude-fable-5", "claude-opus-5"):
            verdict = self.availability(model)
            self.assertEqual(model, verdict["model"])

    # ---- the command surface ---------------------------------------------

    def core(self, *argv: str, **environ: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, os.fspath(CORE), *argv],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "SESSION_KIT_STATE_DIR": os.fspath(self.state),
                **environ,
            },
        )

    def test_the_verb_refuses_with_status_three_and_says_why(self) -> None:
        completed = self.core(
            "model-availability",
            "claude",
            "claude-fable-5",
            SESSION_KIT_TUI_MODELS="claude-opus-5",
        )
        self.assertEqual(3, completed.returncode, completed.stderr)
        self.assertIn("You asked for claude-fable-5.", completed.stderr)
        self.assertIn("claude-opus-5", completed.stderr)

    def test_the_verb_agrees_when_the_machine_offers_the_model(self) -> None:
        completed = self.core(
            "model-availability",
            "claude",
            "claude-opus-5",
            SESSION_KIT_TUI_MODELS="claude-opus-5",
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_an_observation_can_be_recorded_from_outside(self) -> None:
        recorded = self.core(
            "model-served", "claude", "claude-fable-5", "claude-haiku-4-5"
        )
        self.assertEqual(0, recorded.returncode, recorded.stderr)
        self.assertTrue(json.loads(recorded.stdout)["downgraded"])
        self.assertEqual(
            3, self.core("model-availability", "claude", "claude-fable-5").returncode
        )


class LaunchRefusesASilentDowngradeTests(unittest.TestCase):
    """`sp new` and `sp change-model` stop before the session exists."""

    def setUp(self) -> None:
        self.fixture = CommandFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def environment(self, **extra: str) -> dict[str, str]:
        return {
            **self.fixture.env(),
            "STUB_DYNAMIC_PROVIDER": "claude",
            "STUB_DYNAMIC_CWD": str(self.fixture.project),
            "SESSION_KIT_TUI_MODELS": "claude-opus-5",
            **extra,
        }

    def test_a_model_this_machine_does_not_serve_starts_nothing(self) -> None:
        refused = run(
            [SP, "new", "claude", "fixture", "--model", "claude-fable-5"],
            env=self.environment(),
            check=False,
        )
        self.assertEqual(2, refused.returncode, refused.stdout)
        self.assertIn("You asked for claude-fable-5.", refused.stderr)
        self.assertIn("claude-opus-5", refused.stderr)
        self.assertIn("no session was created", refused.stderr)
        self.assertFalse(
            self.fixture.shpool_log.exists(),
            "a refused model must not have started anything",
        )

    def test_the_person_can_say_they_meant_it(self) -> None:
        started = run(
            [
                SP,
                "new",
                "claude",
                "fixture",
                "--model",
                "claude-fable-5",
                "--model-anyway",
            ],
            env=self.environment(),
            check=False,
        )
        self.assertEqual(0, started.returncode, started.stderr)
        self.assertTrue(self.fixture.shpool_log.exists())

    def test_an_unconfirmed_model_tells_the_person_at_their_terminal(self) -> None:
        """"Unknown" has to reach the person, or it is not an answer.

        With no model list and no observation, nothing on this machine can
        confirm a model. That is not a refusal: the session starts exactly as
        asked, but a launch that says nothing is indistinguishable from a
        launch that was checked. A person asking at a terminal is told; a
        scripted launch is not, because "nobody has confirmed this" repeated
        into every log line is how a real warning stops being read.
        """
        environment = self.environment()
        environment.pop("SESSION_KIT_TUI_MODELS")
        primary, secondary = pty.openpty()
        try:
            started = subprocess.run(
                [SP, "new", "claude", "fixture", "--model", "claude-opus-5"],
                env={**os.environ, **environment},
                stdout=subprocess.PIPE,
                stderr=secondary,
                text=True,
                check=False,
                timeout=120,
            )
            os.close(secondary)
            secondary = -1
            seen = b""
            while True:
                try:
                    block = os.read(primary, 4096)
                except OSError:
                    break
                if not block:
                    break
                seen += block
        finally:
            os.close(primary)
            if secondary != -1:
                os.close(secondary)
        self.assertEqual(0, started.returncode, seen)
        self.assertIn("nothing here can confirm it", seen.decode("utf-8", "replace"))
        self.assertTrue(self.fixture.shpool_log.exists())

    def test_a_scripted_launch_is_not_told_twice_a_day(self) -> None:
        environment = self.environment()
        environment.pop("SESSION_KIT_TUI_MODELS")
        started = run(
            [SP, "new", "claude", "fixture", "--model", "claude-opus-5"],
            env=environment,
            check=False,
        )
        self.assertEqual(0, started.returncode, started.stderr)
        self.assertNotIn("nothing here can confirm it", started.stderr)
        self.assertTrue(self.fixture.shpool_log.exists())

    def test_a_model_on_the_list_is_untouched_and_starts(self) -> None:
        started = run(
            [SP, "new", "claude", "fixture", "--model", "claude-opus-5"],
            env=self.environment(),
            check=False,
        )
        self.assertEqual(0, started.returncode, started.stderr)
        launches = sorted(Path(self.fixture.start).glob("*.launch"))
        self.assertTrue(launches, "the launch record is what carries the model")
        self.assertIn("claude-opus-5", launches[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
