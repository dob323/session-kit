"""Opt-in desktop notifications for the attention queue.

The watchdog already owns the one notifier this machine is configured with, so
the queue alerts ride on it rather than inventing a second channel. Everything
here is about restraint: off unless asked for, silent about a session that has
only just asked, one alert per wait, never critical, and never a write to the
event store — a notification that synthesized the event it was reporting would
be a lie that also changed the picker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parent.parent
WATCHDOG = REPO / "bin" / "session_kit_watchdog"


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def session(
    *,
    number: int = 1,
    provider: str = "codex",
    uuid: str = "00000000-0000-4000-8000-000000000001",
    title: str = "parser refactor",
    needs_you: bool = True,
) -> dict:
    return {
        "shpool_id": f"work-{number}",
        "shpool_id_raw": f"work-{number}",
        "display_shpool_id": f"work-{number}",
        "terminal_number": number,
        "title": title,
        "display_title": title,
        "provider": provider,
        "agent_status": "needs your reply" if needs_you else "working",
        "needs_you": needs_you,
        "availability": "ready",
        "mutation_allowed": True,
        "recent_output_age_seconds": 30,
        "identity": {"uuid": uuid, "confidence": "exact"},
    }


class AttentionFixture:
    def __init__(self, *, sessions: list[dict], stale: bool = False) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".attention-", dir=REPO)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "watchdog.log"
        self.notify_log = self.base / "notify.log"
        self.announced = self.base / "announced.json"
        self.sessions = sessions

        self.inventory = self.base / "inventory.json"
        self.inventory.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "cache" if stale else "live",
                    "stale": stale,
                    "warnings": [],
                    "daemon_generation": {
                        "pid": os.getpid(),
                        "process_start_ticks": 1,
                    },
                    "sessions": sessions,
                    "outside_agents": [],
                }
            ),
            encoding="utf-8",
        )

        self.status = self.bin / "status"
        write_executable(
            self.status,
            f"""#!/usr/bin/env python3
import pathlib, sys
sys.stdout.write(pathlib.Path({str(self.inventory)!r}).read_text())
""",
        )
        self.notify = self.bin / "notify"
        write_executable(
            self.notify,
            f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {str(self.notify_log)!r}
""",
        )
        self.shpool = self.bin / "shpool"
        write_executable(self.shpool, "#!/usr/bin/env bash\nexit 0\n")
        self.journalctl = self.bin / "journalctl"
        write_executable(self.journalctl, "#!/usr/bin/env bash\nexit 0\n")
        self.sp = self.bin / "sp"
        write_executable(self.sp, "#!/usr/bin/env bash\nexit 0\n")
        self.sentinel = self.base / "no-watchdog"

    def ask(self, item: dict, *, question: str, waited_seconds: int) -> None:
        """Record the question a session is blocked on, as its hook would."""
        events = self.state / "events"
        events.mkdir(mode=0o700, exist_ok=True)
        key = f"{item['provider']}:{item['identity']['uuid']}"
        path = events / f"{key}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "event": "needs_input",
                        "question": question,
                        "source": "hook",
                        "ts_unix_ms": int((time.time() - waited_seconds) * 1000),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        path.chmod(0o600)

    def run(self, **overrides: str) -> subprocess.CompletedProcess:
        environment = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.base),
            "SESSION_KIT_STATE_DIR": str(self.state),
            "SESSION_KIT_JOURNAL_DIR": str(self.base / "journals"),
            "SESSION_KIT_SHPOOL_CMD": str(self.shpool),
            "SESSION_KIT_STATUS_CMD": str(self.status),
            "SESSION_KIT_SP_CMD": str(self.sp),
            "SESSION_KIT_WATCHDOG_LOG": str(self.log),
            "SESSION_KIT_WATCHDOG_REPAIRS": str(self.base / "repairs.json"),
            "SESSION_KIT_WATCHDOG_REPORT_STATE": str(self.base / "reported.json"),
            "SESSION_KIT_WATCHDOG_SENTINEL": str(self.sentinel),
            "SESSION_KIT_WATCHDOG_NOTIFY": str(self.notify),
            "SESSION_KIT_WATCHDOG_MODE": "report",
            "SESSION_KIT_ATTENTION_NOTIFY": "1",
            "SESSION_KIT_ATTENTION_NOTIFY_AFTER_SECONDS": "600",
            "SESSION_KIT_ATTENTION_NOTIFY_STATE": str(self.announced),
        }
        environment.update(overrides)
        return subprocess.run(
            [str(WATCHDOG), "--once"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def alerts(self) -> list[str]:
        if not self.notify_log.exists():
            return []
        return [
            line
            for line in self.notify_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def attention_alerts(self) -> list[str]:
        return [line for line in self.alerts() if "session-kit.attention" in line]

    def close(self) -> None:
        self.temp.cleanup()


class AttentionNotifyTests(unittest.TestCase):
    def test_nothing_is_delivered_unless_it_is_switched_on(self) -> None:
        item = session()
        fixture = AttentionFixture(sessions=[item])
        try:
            fixture.ask(item, question="Which branch?", waited_seconds=7200)
            # The default: no environment variable set at all.
            result = fixture.run(SESSION_KIT_ATTENTION_NOTIFY="0")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], fixture.attention_alerts())
            self.assertFalse(fixture.announced.exists())
        finally:
            fixture.close()

    def test_a_long_wait_is_announced_once(self) -> None:
        item = session()
        fixture = AttentionFixture(sessions=[item])
        try:
            fixture.ask(item, question="Which branch?", waited_seconds=7200)
            self.assertEqual(0, fixture.run().returncode)
            alerts = fixture.attention_alerts()
            self.assertEqual(1, len(alerts))
            self.assertIn("--severity=warning", alerts[0])
            self.assertIn("1 session needs you", alerts[0])
            self.assertIn("parser refactor", alerts[0])
            self.assertIn("waiting 2 hr", alerts[0])

            # The same unbroken wait, one poll later: already said.
            self.assertEqual(0, fixture.run().returncode)
            self.assertEqual(1, len(fixture.attention_alerts()))
        finally:
            fixture.close()

    def test_a_session_that_has_only_just_asked_is_left_alone(self) -> None:
        item = session()
        fixture = AttentionFixture(sessions=[item])
        try:
            fixture.ask(item, question="Which branch?", waited_seconds=60)
            self.assertEqual(0, fixture.run().returncode)
            self.assertEqual([], fixture.attention_alerts())
        finally:
            fixture.close()

    def test_a_new_question_from_the_same_session_is_announced_again(self) -> None:
        item = session()
        fixture = AttentionFixture(sessions=[item])
        try:
            fixture.ask(item, question="Which branch?", waited_seconds=7200)
            fixture.run()
            self.assertEqual(1, len(fixture.attention_alerts()))
            # A different wait, not the same one continuing.
            fixture.ask(item, question="Merge it?", waited_seconds=3600)
            fixture.run()
            self.assertEqual(2, len(fixture.attention_alerts()))
        finally:
            fixture.close()

    def test_several_waiting_sessions_are_one_alert(self) -> None:
        first = session(number=1, title="parser refactor")
        second = session(
            number=2,
            provider="claude",
            uuid="00000000-0000-4000-8000-000000000002",
            title="docs sweep",
        )
        fixture = AttentionFixture(sessions=[first, second])
        try:
            fixture.ask(first, question="Which branch?", waited_seconds=7200)
            fixture.ask(second, question="Ship it?", waited_seconds=5400)
            self.assertEqual(0, fixture.run().returncode)
            alerts = fixture.attention_alerts()
            self.assertEqual(1, len(alerts))
            self.assertIn("2 sessions need you", alerts[0])
            self.assertIn("parser refactor", alerts[0])
            self.assertIn("docs sweep", alerts[0])
        finally:
            fixture.close()

    def test_a_cached_inventory_never_speaks(self) -> None:
        item = session()
        fixture = AttentionFixture(sessions=[item], stale=True)
        try:
            fixture.ask(item, question="Which branch?", waited_seconds=7200)
            self.assertEqual(0, fixture.run().returncode)
            self.assertEqual([], fixture.attention_alerts())
        finally:
            fixture.close()

    def test_the_watchdog_sentinel_silences_it_too(self) -> None:
        item = session()
        fixture = AttentionFixture(sessions=[item])
        try:
            fixture.ask(item, question="Which branch?", waited_seconds=7200)
            fixture.sentinel.write_text("", encoding="utf-8")
            self.assertEqual(0, fixture.run().returncode)
            self.assertEqual([], fixture.attention_alerts())
        finally:
            fixture.close()

    def test_being_switched_on_without_a_notifier_is_logged_not_fatal(self) -> None:
        item = session()
        fixture = AttentionFixture(sessions=[item])
        try:
            fixture.ask(item, question="Which branch?", waited_seconds=7200)
            result = fixture.run(SESSION_KIT_WATCHDOG_NOTIFY="")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(
                "no usable SESSION_KIT_WATCHDOG_NOTIFY",
                fixture.log.read_text(encoding="utf-8"),
            )
        finally:
            fixture.close()

    def test_reading_the_queue_never_writes_to_the_event_store(self) -> None:
        item = session()
        fixture = AttentionFixture(sessions=[item])
        try:
            fixture.ask(item, question="Which branch?", waited_seconds=7200)
            events = fixture.state / "events"
            before = {
                path.name: path.read_bytes() for path in sorted(events.iterdir())
            }
            fixture.run()
            after = {
                path.name: path.read_bytes() for path in sorted(events.iterdir())
            }
            self.assertEqual(before, after)
        finally:
            fixture.close()


class NotifierExampleTests(unittest.TestCase):
    def test_the_example_notifier_ignores_flags_it_does_not_know(self) -> None:
        notifier = REPO / "extras" / "notify-desktop"
        self.assertTrue(os.access(notifier, os.X_OK))
        # No notify-send in a container: the point is that an unknown flag is
        # parsed rather than refused, so the contract can grow.
        result = subprocess.run(
            ["bash", "-n", str(notifier)], capture_output=True, text=True
        )
        self.assertEqual(0, result.returncode, result.stderr)
        source = notifier.read_text(encoding="utf-8")
        self.assertIn("--title=*", source)
        self.assertIn("--body=*", source)
        self.assertIn("*) ;;", source)


if __name__ == "__main__":
    unittest.main()
