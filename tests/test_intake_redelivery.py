"""Redelivery of intake notices, and the pin that survives an activation.

Two failures from one morning's supervisor evidence, kept honest here.

The first: the relay from a source thread to the supervisor was one-shot. A
notice that could not be delivered — the resident was mid-turn, its harness
queue was full, it was between sessions — was marked and never tried again,
and nothing anywhere said how many were owed. Real operator instructions sat
undelivered for days behind a status that read like a verdict.

The second: the supervisor's MCP definition pinned a versioned release
directory, so every activation left the fleet's policing session pointed at
code nobody was running. These tests hold the pin to the install's own
`current` pointer, which activation moves atomically.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

from tests.support import REPO, run
from tests.test_supervisor_bin_lifecycle import SP_STUB, STATUS_STUB

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor import intake as intake_mod  # noqa: E402
from sessionkit_supervisor.intake import Spool, produce  # noqa: E402

SUPERVISOR = REPO / "bin" / "supervisor"
SOURCE_UUID = "9b1f4d6a-1b2c-4d3e-8f90-a1b2c3d4e5f6"
SUPERVISOR_UUID = "5e0d1c2b-3a4f-4b5c-8d6e-7f8091a2b3c4"
SOURCE_KEY = f"claude:{SOURCE_UUID}"
SUPERVISOR_KEY = f"claude:{SUPERVISOR_UUID}"
EVENT = "e" * 64
DIGEST = "d" * 64


class RedeliveryCase(unittest.TestCase):
    """One open intake, one supervisor to tell, and a fake delivery channel."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="intake-redelivery-")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state"
        self.spool = Spool(self.state)
        self.spool.ensure()
        self.now = 1_700_000_000_000
        self.sends: list[dict] = []
        self.status = "delivered-woke"

    def clock(self) -> float:
        return self.now / 1000

    def deliver(self, *, thread_key: str, text: str, key: str) -> dict:
        self.sends.append({"thread_key": thread_key, "text": text, "key": key})
        return {
            "msg_id": "aaaa0001",
            "targets": [
                {"thread_key": thread_key, "status": self.status, "detail": "stub"}
            ],
        }

    def name_a_supervisor(self) -> None:
        root = self.state / "supervisor"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity = root / "identity"
        identity.write_text(f"{SUPERVISOR_KEY}\n", encoding="utf-8")
        identity.chmod(0o600)

    def open_a_project(self, *, amendments: int = 0) -> None:
        produce(
            self.spool,
            thread_key=SOURCE_KEY,
            prompt="rebuild the sitemap generator and ship it",
            turn_id="turn-0",
            source_event_id=EVENT,
            source_digest=DIGEST,
            clock=self.clock,
        )
        for index in range(amendments):
            self.now += 1_000
            produce(
                self.spool,
                thread_key=SOURCE_KEY,
                prompt=f"and also regenerate the redirect map, pass {index}",
                turn_id=f"turn-{index + 1}",
                source_event_id=EVENT,
                source_digest=f"{index:064d}",
                clock=self.clock,
            )

    def flush(self) -> dict:
        return intake_mod.flush(
            self.spool,
            deliver=self.deliver,
            state_dir=self.state,
            clock=self.clock,
        )

    def pending(self) -> dict:
        return intake_mod.pending_relays(self.spool, clock=self.clock)


class UndeliveredNoticesAreOwedTests(RedeliveryCase):
    def test_a_failed_relay_is_owed_counted_and_swept_again(self) -> None:
        """The morning's actual failure: unreachable, then nothing, forever."""
        self.name_a_supervisor()
        self.open_a_project(amendments=2)
        primary = str(self.spool.open_entries()[0]["msg_id"])
        self.status = "unreachable"
        first = self.flush()
        self.assertEqual(0, first["delivered"])
        self.assertEqual(3, first["attempted"])
        self.assertEqual(3, first["pending"])

        # Undelivered is a count somebody can read, not a state file to open.
        owed = self.pending()
        self.assertEqual(3, owed["count"])
        self.assertEqual(0, owed["due_now"])
        self.assertEqual(3, owed["deferred"])
        self.assertEqual(
            ["arrival", "amendment", "amendment"],
            [item["kind"] for item in owed["items"]],
        )
        self.assertEqual(1, owed["items"][0]["delivery_attempts"])
        self.assertEqual("unreachable", owed["items"][0]["relay_status"])

        # The condition passes; the sweep after it delivers what was owed.
        self.now += intake_mod.RELAY_BACKOFF_MS[0]
        self.status = "delivered-woke"
        second = self.flush()
        self.assertEqual(3, second["delivered"])
        self.assertEqual(0, second["pending"])
        self.assertEqual(0, self.pending()["count"])
        self.assertEqual(f"intake-arrival:{primary}", self.sends[0]["key"])
        # Same words, same key, both times: a resumed send, never a copy.
        self.assertEqual(
            [send["key"] for send in self.sends[:3]],
            [send["key"] for send in self.sends[3:]],
        )

    def test_the_backoff_lengthens_and_never_writes_a_notice_off(self) -> None:
        self.name_a_supervisor()
        self.open_a_project()
        self.status = "unreachable"
        for rung, wait in enumerate(intake_mod.RELAY_BACKOFF_MS):
            outcome = self.flush()
            self.assertEqual(1, outcome["attempted"], f"rung {rung}")
            self.assertEqual(1, outcome["pending"], f"rung {rung}")
            self.assertEqual(0, self.flush()["attempted"], f"rung {rung} repeat")
            self.now += wait
        # Past the last rung the wait repeats; the notice is still owed.
        self.assertEqual(1, self.flush()["attempted"])
        self.assertEqual(1, self.pending()["count"])

    def test_a_fresh_notice_is_delivered_at_once_and_never_deferred(self) -> None:
        self.name_a_supervisor()
        self.open_a_project(amendments=1)
        outcome = self.flush()
        self.assertEqual(2, outcome["delivered"])
        self.assertEqual(2, outcome["attempted"])
        self.assertEqual(0, outcome["deferred"])

    def test_open_intakes_carry_the_undelivered_count(self) -> None:
        """A resident's first read of every turn says what never reached it."""
        self.name_a_supervisor()
        self.open_a_project(amendments=1)
        self.status = "unreachable"
        self.flush()
        payload = intake_mod.open_intakes(self.spool, clock=self.clock)
        self.assertEqual(2, payload["undelivered"]["count"])
        self.assertEqual(
            SOURCE_KEY, payload["undelivered"]["items"][0]["source_thread_key"]
        )
        self.assertGreater(payload["undelivered"]["oldest_waiting_ms"], 0)

    def test_nothing_owed_reports_nothing_owed(self) -> None:
        self.name_a_supervisor()
        self.open_a_project()
        self.flush()
        self.assertEqual(0, self.pending()["count"])
        self.assertEqual("nothing is owed", self.flush()["reason"])

    def test_a_notice_survives_an_entry_written_before_attempts_were_counted(
        self,
    ) -> None:
        """Old entries are readable, and their notices are due, not corrupt."""
        self.name_a_supervisor()
        self.open_a_project(amendments=1)
        primary = str(self.spool.open_entries()[0]["msg_id"])
        path = self.spool.entry_path(primary)
        raw = json.loads(path.read_text())
        for row in raw["amendments"]:
            row.pop("delivery_attempts", None)
            row.pop("last_attempt_unix_ms", None)
            row.pop("next_attempt_unix_ms", None)
        raw["arrival_notice"].pop("delivery_attempts", None)
        path.write_text(json.dumps(raw))
        owed = self.pending()
        self.assertEqual(2, owed["count"])
        self.assertEqual(2, owed["due_now"])
        self.assertEqual(2, self.flush()["delivered"])

    def test_the_pending_verb_is_read_only(self) -> None:
        self.name_a_supervisor()
        self.open_a_project(amendments=1)
        code, payload = intake_mod.run(
            "pending",
            spool=self.spool,
            deliver=self.deliver,
            reply=lambda **_: {},
            clock=self.clock,
            state_dir=self.state,
        )
        self.assertEqual(0, code)
        self.assertEqual(2, payload["count"])
        self.assertEqual([], self.sends)


class HookSweepTests(RedeliveryCase):
    """A prompt is a wake, and a wake is when a failed relay belongs again."""

    PAYLOAD = {
        "provider": "claude",
        "session_id": SOURCE_UUID,
        "prompt": "rebuild the sitemap generator and ship it",
        "turn_id": "turn-0",
    }

    def hook(self, spawned: list[list[str]], environ: dict[str, str]) -> dict:
        return intake_mod.from_hook(
            dict(self.PAYLOAD),
            state_dir=self.state,
            environ=environ,
            spool=self.spool,
            spawn=lambda argv: spawned.append(list(argv)),
            clock=self.clock,
        )

    def test_a_prompt_that_changes_nothing_still_asks_for_a_sweep(self) -> None:
        environ = {
            "SESSION_KIT_INVENTORY_CORE": os.fspath(
                REPO / "lib" / "session_inventory.py"
            )
        }
        self.name_a_supervisor()
        opened: list[list[str]] = []
        self.assertTrue(self.hook(opened, environ)["produced"])
        self.status = "unreachable"
        self.flush()
        self.now += intake_mod.RELAY_BACKOFF_MS[0]

        # The same words on the same turn: nothing new is recorded, and until
        # now nothing was retried either.
        spawned: list[list[str]] = []
        outcome = self.hook(spawned, environ)
        self.assertEqual("duplicate", outcome["action"])
        self.assertTrue(outcome["delivery"]["requested"])
        self.assertEqual([["msg", "intake", "flush"]], [row[-3:] for row in spawned])

    def test_a_prompt_owing_nothing_asks_for_no_sweep(self) -> None:
        environ = {
            "SESSION_KIT_INVENTORY_CORE": os.fspath(
                REPO / "lib" / "session_inventory.py"
            )
        }
        self.name_a_supervisor()
        opened: list[list[str]] = []
        self.assertTrue(self.hook(opened, environ)["produced"])
        self.flush()

        spawned: list[list[str]] = []
        outcome = self.hook(spawned, environ)
        self.assertEqual("duplicate", outcome["action"])
        self.assertNotIn("delivery", outcome)
        self.assertEqual([], spawned)

    def test_a_notice_still_inside_its_backoff_asks_for_no_sweep(self) -> None:
        """Deferred is not owed-now: a wake must not spend a send per prompt."""
        environ = {
            "SESSION_KIT_INVENTORY_CORE": os.fspath(
                REPO / "lib" / "session_inventory.py"
            )
        }
        self.name_a_supervisor()
        opened: list[list[str]] = []
        self.assertTrue(self.hook(opened, environ)["produced"])
        self.status = "unreachable"
        self.flush()

        spawned: list[list[str]] = []
        outcome = self.hook(spawned, environ)
        self.assertEqual("duplicate", outcome["action"])
        self.assertNotIn("delivery", outcome)
        self.assertEqual([], spawned)


class McpReleasePinTests(unittest.TestCase):
    """The pin the supervisor's tools are started with, across an activation."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="supervisor-mcp-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.install = self.root / "lib" / "session-kit"
        (self.install / "releases").mkdir(parents=True)

    def make_release(self, release_id: str) -> Path:
        release = self.install / "releases" / release_id
        (release / "lib").mkdir(parents=True)
        (release / "bin").mkdir(parents=True)
        return release

    def activate(self, release_id: str) -> None:
        pointer = self.install / "current"
        if pointer.is_symlink():
            pointer.unlink()
        pointer.symlink_to(self.install / "releases" / release_id)

    def write_config(self, release: Path) -> dict:
        completed = subprocess.run(
            [os.fspath(SUPERVISOR), "mcp-config"],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": os.fspath(self.root),
                "SESSION_KIT_STATE_DIR": os.fspath(self.state),
                "SESSION_KIT_RELEASE_DIR": os.fspath(release),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        path = Path(completed.stdout.strip())
        self.assertEqual(self.state / "supervisor" / "mcp.json", path)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        return json.loads(path.read_text())

    def pythonpath(self, config: dict) -> str:
        return config["mcpServers"]["session-kit-supervisor"]["env"]["PYTHONPATH"]

    def test_the_pin_follows_the_current_pointer_through_an_activation(self) -> None:
        first = self.make_release("a" * 40)
        self.activate("a" * 40)
        pinned = self.pythonpath(self.write_config(first))
        self.assertEqual(os.fspath(self.install / "current" / "lib"), pinned)

        # The next release activates. The pin still names running code —
        # which is the whole defect: it used to name the release before it.
        second = self.make_release("b" * 40)
        self.activate("b" * 40)
        self.assertEqual(os.fspath(second / "lib"), os.path.realpath(pinned))

    def test_a_release_that_is_not_the_current_one_pins_itself(self) -> None:
        """A pointer naming somebody else is not this release's pointer."""
        self.make_release("a" * 40)
        stale = self.make_release("b" * 40)
        self.activate("a" * 40)
        self.assertEqual(
            os.fspath(stale / "lib"), self.pythonpath(self.write_config(stale))
        )

    def test_a_checkout_that_is_not_an_install_pins_its_own_lib(self) -> None:
        checkout = self.root / "work" / "session-kit"
        (checkout / "lib").mkdir(parents=True)
        self.assertEqual(
            os.fspath(checkout / "lib"), self.pythonpath(self.write_config(checkout))
        )

    def test_mcp_config_creates_no_session_and_touches_no_identity(self) -> None:
        release = self.make_release("a" * 40)
        self.activate("a" * 40)
        self.write_config(release)
        supervisor_dir = self.state / "supervisor"
        self.assertEqual(
            ["mcp.json"], sorted(item.name for item in supervisor_dir.iterdir())
        )


class EnsureSweepsTests(unittest.TestCase):
    """`supervisor ensure` is a wake, and a wake retries what is owed."""

    CORE_STUB = r"""#!/usr/bin/env python3
import os
import sys

with open(os.environ["SWEEP_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\n")
"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ensure-sweep-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.cwd = self.root / "home"
        self.cwd.mkdir()
        self.fake_home = self.root / "fake-home"
        self.fake_home.mkdir()
        (self.root / "sessions.json").write_text('{"sessions": []}\n')
        (self.root / "next").write_text("1")
        self.sp = self.root / "sp"
        self.status = self.root / "shpool_status"
        self.core = self.root / "session_inventory_stub.py"
        self.sweep_log = self.root / "sweep.log"
        self.sp.write_text(textwrap.dedent(SP_STUB), encoding="utf-8")
        self.status.write_text(textwrap.dedent(STATUS_STUB), encoding="utf-8")
        self.core.write_text(textwrap.dedent(self.CORE_STUB), encoding="utf-8")
        self.sp.chmod(0o755)
        self.status.chmod(0o755)
        self.env = {
            "SESSION_KIT_SP": os.fspath(self.sp),
            "SESSION_KIT_SHPOOL_STATUS": os.fspath(self.status),
            "SESSION_KIT_STATE_DIR": os.fspath(self.state),
            "SESSION_KIT_SUPERVISOR_CWD": os.fspath(self.cwd),
            "SESSION_KIT_SUPERVISOR_BRIEF_WAIT_SECONDS": "0.4",
            "SESSION_KIT_SUPERVISOR_BRIEF_ATTEMPTS": "3",
            "SESSION_KIT_INVENTORY_CORE": os.fspath(self.core),
            "SESSION_KIT_TESTING": "1",
            "SWEEP_LOG": os.fspath(self.sweep_log),
            "HOME": os.fspath(self.fake_home),
            "STUB_ROOT": os.fspath(self.root),
            "STUB_HANDOFF_REPLY": "1",
            "STUB_REGISTER": "1",
        }

    def test_ensure_asks_the_delivery_verb_to_sweep(self) -> None:
        created = run([SUPERVISOR, "ensure"], env=self.env)
        self.assertEqual("Fleet Supervisor is ready.", created.stdout.strip())
        self.assertEqual(["msg intake flush"], self.sweep_log.read_text().splitlines())

    def test_ensure_rewrites_a_definition_that_drifted(self) -> None:
        """The repair that matters: an install that already went stale."""
        run([SUPERVISOR, "ensure"], env=self.env)
        config = self.state / "supervisor" / "mcp.json"
        drifted = json.loads(config.read_text())
        server = drifted["mcpServers"]["session-kit-supervisor"]
        server["env"]["PYTHONPATH"] = "/nowhere/releases/deadbeef/lib"
        config.write_text(json.dumps(drifted))

        run([SUPERVISOR, "ensure"], env=self.env)
        repaired = json.loads(config.read_text())
        self.assertEqual(
            os.fspath(REPO / "lib"),
            repaired["mcpServers"]["session-kit-supervisor"]["env"]["PYTHONPATH"],
        )


if __name__ == "__main__":
    unittest.main()
