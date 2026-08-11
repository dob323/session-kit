from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest

from tests.support import REPO


sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_events import (  # noqa: E402
    EventError,
    EventStore,
    append_event,
    mark_seen,
    read_events,
    thread_key,
)


UUID = "019fdf1e-8b4c-7573-a089-be495bfece6a"
KEY = f"claude:{UUID}"


class EventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".events-", dir=REPO)
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_store_is_private_and_lines_have_the_frozen_shape(self) -> None:
        record = append_event(
            self.state,
            KEY,
            "permission_prompt",
            question="Allow this command?",
            source="hook",
            ts_unix_ms=1234,
        )
        self.assertEqual(
            {
                "ts_unix_ms": 1234,
                "event": "permission_prompt",
                "question": "Allow this command?",
                "source": "hook",
            },
            record,
        )
        store = EventStore(self.state)
        self.assertEqual(0o700, stat.S_IMODE(store.root.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(store.seen.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(store.event_path(KEY).stat().st_mode))
        line = json.loads(store.event_path(KEY).read_text(encoding="utf-8"))
        self.assertEqual(record, line)

    def test_invalid_identity_event_source_and_question_are_refused(self) -> None:
        with self.assertRaises(EventError):
            thread_key("shell", UUID)
        with self.assertRaises(EventError):
            append_event(self.state, "codex:../../escape", "turn_done", source="synth")
        with self.assertRaises(EventError):
            append_event(self.state, KEY, "made_up", source="hook")
        with self.assertRaises(EventError):
            append_event(self.state, KEY, "turn_done", source="other")
        with self.assertRaises(EventError):
            append_event(
                self.state, KEY, "needs_input", question=object(), source="hook"
            )

    def test_corrupt_lines_are_skipped_without_hiding_valid_events(self) -> None:
        append_event(self.state, KEY, "session_start", source="hook", ts_unix_ms=1)
        path = EventStore(self.state).event_path(KEY)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{broken\n")
        append_event(self.state, KEY, "turn_done", source="hook", ts_unix_ms=2)
        self.assertEqual(
            ["session_start", "turn_done"],
            [record["event"] for record in read_events(self.state, KEY)],
        )

    def test_flock_serializes_concurrent_atomic_appends(self) -> None:
        threads = []
        errors: list[BaseException] = []

        def writer(worker: int) -> None:
            try:
                for index in range(30):
                    append_event(
                        self.state,
                        KEY,
                        "turn_done",
                        source="synth",
                        ts_unix_ms=worker * 1000 + index,
                    )
            except BaseException as exc:
                errors.append(exc)

        for worker in range(6):
            thread = threading.Thread(target=writer, args=(worker,))
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        records = read_events(self.state, KEY)
        self.assertEqual(180, len(records))
        self.assertEqual(180, len({record["ts_unix_ms"] for record in records}))

    def test_seen_marks_store_an_exact_timestamp_privately(self) -> None:
        self.assertEqual(9876, mark_seen(self.state, KEY, 9876))
        store = EventStore(self.state)
        self.assertEqual(9876, store.seen_unix_ms(KEY))
        self.assertEqual(0o600, stat.S_IMODE(store.seen_path(KEY).stat().st_mode))

    def test_symlinked_event_root_is_refused(self) -> None:
        target = self.base / "elsewhere"
        target.mkdir()
        (self.state / "events").symlink_to(target, target_is_directory=True)
        with self.assertRaises(EventError):
            append_event(self.state, KEY, "turn_done", source="hook")


if __name__ == "__main__":
    unittest.main()
