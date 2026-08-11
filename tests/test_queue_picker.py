from __future__ import annotations

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import time
import unittest

from tests.support import REPO, run
from tests.test_login import LoginFixture, inventory, row, run_pty, write_executable


class QueuePickerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = LoginFixture(inventory(row("queue", number=4)))

    def tearDown(self) -> None:
        self.fixture.close()

    def _queue_core(self, payload: dict) -> Path:
        path = self.fixture.base / "fake-inventory-core"
        write_executable(
            path,
            "#!/usr/bin/env python3\n"
            "import json,os,pathlib,sys\n"
            f"payload={payload!r}\n"
            "log=os.environ.get('QUEUE_CORE_LOG')\n"
            "if log:\n"
            "    with pathlib.Path(log).open('a') as handle:\n"
            "        handle.write(json.dumps(sys.argv[1:])+'\\n')\n"
            "if sys.argv[1:] == ['msg', 'queue']:\n"
            "    print(json.dumps(payload, sort_keys=True))\n"
            "    raise SystemExit(0)\n"
            "if sys.argv[1:3] == ['msg', 'queue'] and len(sys.argv) == 5 and sys.argv[3] == '--mark-seen':\n"
            "    print(json.dumps({'thread_key':sys.argv[4],'seen_unix_ms':1}))\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(2)\n",
        )
        return path

    def _write_inventory(self, document: dict) -> None:
        encoded = json.dumps(document)
        self.fixture.inventory.write_text(encoded, encoding="utf-8")
        self.fixture.refreshed_inventory.write_text(encoded, encoding="utf-8")

    def test_needs_you_queue_adds_exactly_one_header_line(self) -> None:
        current_ms = int(time.time() * 1000)
        waiting = row(
            "queue",
            number=4,
            needs_you=True,
            recent_output_at_unix_ms=current_ms - 125_999,
        )
        waiting["title"] = "Check deployment"
        waiting["agent_status"] = "needs your reply"
        key = f"codex:{waiting['identity']['uuid']}"
        document = inventory(waiting)
        document["generated_at"] = datetime.fromtimestamp(
            current_ms / 1000, timezone.utc
        ).isoformat().replace("+00:00", "Z")
        self._write_inventory(document)
        core = self._queue_core(
            {
                "as_of_unix_ms": 1,
                "items": [
                    {
                        "thread_key": key,
                        "terminal_number": 4,
                        "title": "Check deployment",
                        "provider": "codex",
                        "bucket": "needs_you",
                        "question": None,
                        "waiting_ms": 125_999,
                        "stale": False,
                    }
                ],
            }
        )
        code, output = run_pty(
            self.fixture,
            b"\n",
            env_updates={"SESSION_KIT_INVENTORY_CORE": str(core)},
        )
        self.assertEqual(2, code)
        self.assertIn("Needs you: 1 · 1 session", output)
        self.assertEqual(1, output.count("Needs you:"))
        self.assertIn("Check deployment", output)

    def test_a_titleless_queue_item_never_shows_its_thread_key(self) -> None:
        """The banner names a session; a thread key is a UUID with a prefix."""
        import sys

        library = os.fspath(REPO / "lib")
        if library not in sys.path:
            sys.path.insert(0, library)
        from sessionkit_events.queue import build_attention_queue

        current_ms = int(time.time() * 1000)
        waiting = row("queue", number=4, needs_you=True)
        waiting["title"] = ""
        waiting["agent_status"] = "needs your reply"
        uuid = waiting["identity"]["uuid"]
        document = inventory(waiting)
        document["generated_at"] = (
            datetime.fromtimestamp(current_ms / 1000, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        queue = build_attention_queue(
            document, self.fixture.base, now_ms=current_ms
        )
        titles = [item["title"] for item in queue["items"]]
        self.assertTrue(titles)
        for title in titles:
            self.assertNotIn(uuid, title)
            self.assertNotIn("codex:", title)
        self.assertEqual(["session 4"], titles)

        # And with no terminal number either, still no key.
        numberless = row("queue", number=7, needs_you=True)
        numberless["title"] = ""
        numberless["terminal_number"] = None
        numberless["agent_status"] = "needs your reply"
        document = inventory(numberless)
        document["generated_at"] = (
            datetime.fromtimestamp(current_ms / 1000, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        queue = build_attention_queue(
            document, self.fixture.base, now_ms=current_ms
        )
        for item in queue["items"]:
            self.assertNotIn(numberless["identity"]["uuid"], item["title"])
            self.assertEqual("a codex session", item["title"])

    def test_non_attention_queue_draws_no_header(self) -> None:
        core = self._queue_core({"as_of_unix_ms": 1, "items": []})
        log = self.fixture.base / "queue-core.log"
        code, output = run_pty(
            self.fixture,
            b"\n",
            env_updates={
                "SESSION_KIT_INVENTORY_CORE": str(core),
                "QUEUE_CORE_LOG": str(log),
            },
        )
        self.assertEqual(2, code)
        self.assertNotIn("⚑", output)
        self.assertFalse(log.exists(), "redraw must import the queue, not spawn the core")

    def test_v_key_tolerates_supervisor_absence(self) -> None:
        missing = self.fixture.base / "not-installed-supervisor"
        script = (
            'set -u; SCRIPT_DIR="$1"; source "$2"; '
            'SESSION_KIT_SUPERVISOR_CMD="$3"; open_supervisor'
        )
        result = run(
            [
                "bash",
                "-c",
                script,
                "test",
                self.fixture.base,
                REPO / "lib/sh/shpool_login_actions.sh",
                missing,
            ],
            check=False,
        )
        self.assertEqual(0, result.returncode)
        self.assertIn("fleet supervisor is not installed yet", result.stdout)

    def test_v_key_calls_supervisor_open(self) -> None:
        core = self._queue_core({"as_of_unix_ms": 1, "items": []})
        log = self.fixture.base / "supervisor.log"
        supervisor = self.fixture.base / "fake-supervisor"
        write_executable(
            supervisor,
            "#!/usr/bin/env python3\n"
            "import json,os,pathlib,sys\n"
            "pathlib.Path(os.environ['SUPERVISOR_LOG']).write_text(json.dumps(sys.argv[1:]))\n",
        )
        code, _output = run_pty(
            self.fixture,
            b"v\n\n",
            env_updates={
                "SESSION_KIT_INVENTORY_CORE": str(core),
                "SESSION_KIT_SUPERVISOR_CMD": str(supervisor),
                "SUPERVISOR_LOG": str(log),
            },
        )
        self.assertEqual(2, code)
        self.assertEqual(["open"], json.loads(log.read_text(encoding="utf-8")))

    def test_picker_open_marks_the_exact_thread_seen(self) -> None:
        core = self._queue_core({"as_of_unix_ms": 1, "items": []})
        log = self.fixture.base / "queue-core.log"
        expected = self.fixture.inventory
        document = json.loads(expected.read_text(encoding="utf-8"))
        identity = document["sessions"][0]["identity"]
        key = f"codex:{identity['uuid']}"
        code, _output = run_pty(
            self.fixture,
            b"4\n\n",
            env_updates={
                "SESSION_KIT_INVENTORY_CORE": str(core),
                "QUEUE_CORE_LOG": str(log),
            },
        )
        self.assertEqual(2, code)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertIn(["msg", "queue", "--mark-seen", key], calls)

    def test_pinned_supervisor_leads_all_sessions_without_renumbering(self) -> None:
        first = row("first", number=2, provider="codex")
        pinned = row(
            "supervisor", number=77, provider="claude", availability="attached"
        )
        attached = row(
            "attached", number=78, provider="codex", availability="attached"
        )
        fixture = LoginFixture(inventory(first, pinned, attached))
        try:
            marker_dir = fixture.state / "supervisor"
            marker_dir.mkdir(mode=0o700)
            marker = marker_dir / "identity"
            marker.write_text(f"claude:{pinned['identity']['uuid']}\n", encoding="ascii")
            marker.chmod(0o600)
            code, output = run_pty(fixture, b"\n")
            self.assertEqual(2, code)
            self.assertLess(output.index("Claude supervisor"), output.index("Codex first"))
            self.assertLess(output.index("Codex first"), output.index("Codex attached"))
            self.assertEqual(1, output.count("Ready to open"))
            self.assertEqual(1, output.count("Open elsewhere"))
            self.assertIn("77  Claude supervisor", output)
        finally:
            fixture.close()

    def test_absent_or_invalid_pin_keeps_normal_order(self) -> None:
        cases = (
            (None, None),
            ("not-an-identity", 0o600),
            ("claude:00000000-0000-4000-8000-000000000000", 0o644),
        )
        for marker_value, marker_mode in cases:
            with self.subTest(marker_value=marker_value, marker_mode=marker_mode):
                first = row("first", number=2, provider="codex")
                later = row(
                    "later", number=77, provider="claude", availability="attached"
                )
                fixture = LoginFixture(inventory(first, later))
                try:
                    if marker_value is not None:
                        marker_dir = fixture.state / "supervisor"
                        marker_dir.mkdir(mode=0o700)
                        marker = marker_dir / "identity"
                        marker.write_text(marker_value + "\n", encoding="ascii")
                        marker.chmod(marker_mode or 0o600)
                    code, output = run_pty(fixture, b"\n")
                    self.assertEqual(2, code)
                    self.assertLess(output.index("Codex first"), output.index("Claude later"))
                finally:
                    fixture.close()

    def test_help_and_footer_advertise_supervisor_key(self) -> None:
        code, output = run_pty(self.fixture, b"?\n\n\n")
        self.assertEqual(2, code)
        self.assertIn("Help ?", output)
        self.assertIn("v             Open the fleet supervisor", output)

    def test_invalid_supervisor_override_is_ignored(self) -> None:
        target = self.fixture.base / "target-supervisor"
        log = self.fixture.base / "invalid-supervisor.log"
        write_executable(
            target,
            "#!/usr/bin/env bash\nprintf called > \"$INVALID_SUPERVISOR_LOG\"\n",
        )
        override = self.fixture.base / "supervisor-link"
        override.symlink_to(target)
        script = (
            'set -u; SCRIPT_DIR="$1"; source "$2"; '
            'SESSION_KIT_SUPERVISOR_CMD="$3"; open_supervisor'
        )
        result = run(
            ["bash", "-c", script, "test", self.fixture.base, REPO / "lib/sh/shpool_login_actions.sh", override],
            env={"INVALID_SUPERVISOR_LOG": os.fspath(log)},
            check=False,
        )
        self.assertEqual(0, result.returncode)
        self.assertIn("not installed yet", result.stdout)
        self.assertFalse(log.exists())

    def test_hanging_supervisor_times_out_and_returns_to_picker(self) -> None:
        core = self._queue_core({"as_of_unix_ms": 1, "items": []})
        supervisor = self.fixture.base / "hanging-supervisor"
        write_executable(supervisor, "#!/usr/bin/env bash\nsleep 30\n")
        started = time.monotonic()
        code, output = run_pty(
            self.fixture,
            b"v\n\n",
            env_updates={
                "SESSION_KIT_INVENTORY_CORE": str(core),
                "SESSION_KIT_SUPERVISOR_CMD": str(supervisor),
                # The real ensure bound is 20s; the hang-detection contract is
                # what matters here, not the production ceiling.
                "SESSION_KIT_SUPERVISOR_ENSURE_TIMEOUT": "1",
            },
        )
        self.assertEqual(2, code)
        self.assertLess(time.monotonic() - started, 6.0)
        self.assertIn("could not be started", output)


if __name__ == "__main__":
    unittest.main()
