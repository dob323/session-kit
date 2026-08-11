from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import stat
import sys
import tempfile
import subprocess
import time
import unittest

from tests.support import REPO, run


HOOK = REPO / "extras" / "hooks" / "sk_session_events.py"
UUID = "019fdf1e-8b4c-7573-a089-be495bfece6a"
KEY = f"claude:{UUID}"


class EventHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".event-hook-", dir=REPO)
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.env = {
            "HOME": os.fspath(self.base / "home"),
            "SESSION_KIT_STATE_DIR": os.fspath(self.state),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _send(self, payload: object) -> int:
        return run(
            [sys.executable, HOOK],
            env=self.env,
            input_text=json.dumps(payload),
            check=False,
        ).returncode

    def _events(self) -> list[dict]:
        path = self.state / "events" / f"{KEY}.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_all_four_claude_hook_kinds_map_to_event_lines(self) -> None:
        payloads = (
            {"hook_event_name": "SessionStart", "session_id": UUID},
            {
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
                "message": "Approve shell command?",
                "session_id": UUID,
            },
            {"hook_event_name": "Stop", "session_id": UUID},
            {"hook_event_name": "SessionEnd", "session_id": UUID},
        )
        for payload in payloads:
            self.assertEqual(0, self._send(payload))
        events = self._events()
        self.assertEqual(
            ["session_start", "permission_prompt", "turn_done", "session_end"],
            [event["event"] for event in events],
        )
        self.assertEqual("Approve shell command?", events[1]["question"])
        self.assertTrue(all(event["source"] == "hook" for event in events))
        self.assertEqual(
            0o600,
            stat.S_IMODE((self.state / "events" / f"{KEY}.jsonl").stat().st_mode),
        )

    def test_non_permission_notification_is_needs_input(self) -> None:
        self.assertEqual(
            0,
            self._send(
                {
                    "hook_event_name": "Notification",
                    "notification_type": "idle_prompt",
                    "message": "Claude is waiting",
                    "sessionId": UUID,
                }
            ),
        )
        self.assertEqual("needs_input", self._events()[0]["event"])

    def test_every_bad_input_fails_open_without_creating_an_event(self) -> None:
        for raw in ("not json", "[]", '{"hook_event_name":"Stop"}'):
            result = run(
                [sys.executable, HOOK],
                env=self.env,
                input_text=raw,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
        event_root = self.state / "events"
        self.assertFalse(event_root.exists())

    def test_held_store_lock_fails_open_promptly(self) -> None:
        event_root = self.state / "events"
        (event_root / "seen").mkdir(parents=True, mode=0o700)
        lock_path = event_root / "events.lock"
        with lock_path.open("w") as lock:
            lock_path.chmod(0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            started = time.monotonic()
            result = run(
                [sys.executable, HOOK],
                env=self.env,
                input_text=json.dumps(
                    {"hook_event_name": "Stop", "session_id": UUID}
                ),
                check=False,
            )
            elapsed = time.monotonic() - started
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertLess(elapsed, 1.5)
        self.assertFalse((event_root / f"{KEY}.jsonl").exists())

    def test_open_stdin_pipe_is_bounded_and_fails_open(self) -> None:
        process = subprocess.Popen(
            [sys.executable, HOOK],
            env={**os.environ, **self.env},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdin is not None
        process.stdin.write(
            json.dumps({"hook_event_name": "Stop", "session_id": UUID})
        )
        process.stdin.flush()
        started = time.monotonic()
        try:
            returncode = process.wait(timeout=3.5)
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            assert process.stdout is not None and process.stderr is not None
            process.stdout.close()
            process.stderr.close()
        self.assertEqual(0, returncode)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertFalse((self.state / "events" / f"{KEY}.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
