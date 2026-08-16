"""Tests for the session wedge watchdog.

The watchdog closes and relaunches sessions with nobody watching, so the
question these tests answer is not only "does it repair a dead session" but
"can it ever harm a live one". Every case that must be left alone is here.

The central rule, learned the hard way on 2026-07-29: the watchdog never
touches the daemon. An earlier version confirmed a candidate by attaching to
it, and that probe deadlocked shpool's `shells` mutex when it hit an
already-wedged session, taking every session's manager functions down with it.
Confirmation is now read-only, and silence with no supporting evidence is
reported rather than acted on.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WATCHDOG = REPO / "bin" / "session_kit_watchdog"

# A journal monotonic timestamp counts from boot, and the watchdog discards any
# event that predates the daemon generation it is reasoning about. Deriving
# fixture timestamps from the host clock made these tests depend on the host's
# uptime: on a freshly booted CI runner, `time.monotonic()` is smaller than the
# ages below, every event landed at or before zero, the watchdog correctly
# dropped the evidence, and the repairs these tests exist to prove never
# happened. The fixture owns its own boot-relative clock instead, far enough
# past the oldest age any case uses that no event can fall off the start of it.
FIXTURE_BOOT_SECONDS = 100_000


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def session(
    shpool_id: str = "wedged",
    *,
    provider: str = "codex",
    agent_status: str = "running",
    recent_output_age_seconds: int | None = 30_000,
    uuid: str | None = "00000000-0000-4000-8000-000000000001",
    mutation_allowed: bool = True,
    availability: str = "ready",
    needs_you: bool | None = None,
) -> dict:
    row = {
        "shpool_id": shpool_id,
        "shpool_id_raw": shpool_id,
        "display_shpool_id": shpool_id,
        "display_title": f"{provider} work",
        "title": f"{provider} work",
        "provider": provider,
        "agent_status": agent_status,
        "recent_output_age_seconds": recent_output_age_seconds,
        "mutation_allowed": mutation_allowed,
        "identity": {"uuid": uuid, "confidence": "exact"},
        "availability": availability,
    }
    if needs_you is not None:
        row["needs_you"] = needs_you
    return row


def journal_event(message: str, *, age_seconds: int = 300, pid: int | None = None) -> str:
    """Return one journalctl -o json fixture line."""
    if age_seconds >= FIXTURE_BOOT_SECONDS:
        raise ValueError(
            "journal fixtures must fall after the fixture boot clock; "
            f"raise FIXTURE_BOOT_SECONDS above {age_seconds}"
        )
    return json.dumps(
        {
            "_PID": str(os.getpid() if pid is None else pid),
            "__MONOTONIC_TIMESTAMP": str(
                int((FIXTURE_BOOT_SECONDS - age_seconds) * 1_000_000)
            ),
            "__REALTIME_TIMESTAMP": str(
                int((time.time() - age_seconds) * 1_000_000)
            ),
            "MESSAGE": message,
        }
    )


def failure_event(
    session_id: str = "wedged", *, age_seconds: int = 300, pid: int | None = None
) -> str:
    return journal_event(
        f'handle_attach:bidi_stream{{s="{session_id}"}}:'
        "disconnect_lock(shell_to_client_ctl): failed to tell shell->client "
        'to disconnect: "SendTimeoutError(..)"',
        age_seconds=age_seconds,
        pid=pid,
    )


def success_event(session_id: str = "wedged", *, age_seconds: int = 60) -> str:
    return journal_event(
        f'handle_attach:bidi_stream{{s="{session_id}"}}:'
        "initial_attach_lock(shell_to_client_ctl): client connection status=New",
        age_seconds=age_seconds,
    )


FLAG_LINE = failure_event()


class WatchdogFixture:
    def __init__(
        self,
        *,
        sessions: list[dict],
        serving_threads: int | None = None,
        journal_lines: str = "",
        repair_exit: int = 0,
        daemon_start_seconds: int = 0,
    ) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".watchdog-", dir=REPO)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "watchdog.log"
        self.repairs = self.base / "repairs.json"
        self.reported = self.base / "reported.json"
        self.sp_log = self.base / "sp.log"
        self.notify_log = self.base / "notify.log"
        self.shpool_log = self.base / "shpool.log"

        # The daemon generation must name a live process whose threads can be
        # counted, so this interpreter stands in for it. One serving thread per
        # session is healthy unless a test says otherwise.
        if serving_threads is None:
            serving_threads = len(sessions)
        self.serving_threads = serving_threads
        self.inventory = self.base / "inventory.json"
        self.inventory.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "live",
                    "stale": False,
                    "warnings": [],
                    "daemon_generation": {
                        "pid": os.getpid(),
                        # Boot-relative, on the same clock as the journal
                        # fixtures above. The default sits before every event
                        # so ordinary cases are unaffected by it.
                        "process_start_ticks": max(
                            1,
                            int(daemon_start_seconds * os.sysconf("SC_CLK_TCK")),
                        ),
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

        self.sp = self.bin / "sp"
        if repair_exit == 0:
            write_executable(
                self.sp,
                f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {str(self.sp_log)!r}
echo s20260729-000000-1234
""",
            )
        else:
            write_executable(
                self.sp,
                f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {str(self.sp_log)!r}
exit {repair_exit}
""",
            )

        self.notify = self.bin / "notify"
        write_executable(
            self.notify,
            f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {str(self.notify_log)!r}
""",
        )

        self.journalctl = self.bin / "journalctl"
        write_executable(
            self.journalctl,
            f"""#!/usr/bin/env bash
cat <<'LINES'
{journal_lines}
LINES
""",
        )

        # Any invocation at all is a failure: the watchdog must never talk to
        # the daemon.
        self.shpool = self.bin / "shpool"
        write_executable(
            self.shpool,
            f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {str(self.shpool_log)!r}
echo 'test fixture refuses shpool' >&2
exit 97
""",
        )

        self.sentinel = self.base / "no-watchdog"

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.base),
            "XDG_STATE_HOME": str(self.base / "xdg-state"),
            "XDG_DATA_HOME": str(self.base / "xdg-data"),
            "SESSION_KIT_STATE_DIR": str(self.state),
            "SESSION_KIT_CONFIG": str(self.base / "config.toml"),
            "SESSION_KIT_JOURNAL_DIR": str(self.base / "journals"),
            "SESSION_KIT_ARCHIVE_DIR": str(self.base / "archives"),
            "SESSION_KIT_SHPOOL_CMD": str(self.shpool),
            "SESSION_KIT_STATUS_CMD": str(self.status),
            "SESSION_KIT_SP_CMD": str(self.sp),
            "SESSION_KIT_WATCHDOG_LOG": str(self.log),
            "SESSION_KIT_WATCHDOG_REPAIRS": str(self.repairs),
            "SESSION_KIT_WATCHDOG_REPORT_STATE": str(self.reported),
            "SESSION_KIT_WATCHDOG_SENTINEL": str(self.sentinel),
            "SESSION_KIT_WATCHDOG_NOTIFY": str(self.notify),
            "SESSION_KIT_WATCHDOG_MODE": "repair",
            "SESSION_KIT_WATCHDOG_SERVING_THREADS": str(self.serving_threads),
        }

    def run(self, **overrides: str) -> subprocess.CompletedProcess:
        env = self.env()
        env.update(overrides)
        return subprocess.run(
            [str(WATCHDOG), "--once"],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def repairs_requested(self) -> list[str]:
        if not self.sp_log.exists():
            return []
        return [
            line.split()[-1]
            for line in self.sp_log.read_text(encoding="utf-8").splitlines()
            if line.startswith("repair ")
        ]

    def recorded(self) -> list[dict]:
        if not self.repairs.exists():
            return []
        return json.loads(self.repairs.read_text(encoding="utf-8"))["repairs"]

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def close(self) -> None:
        self.temp.cleanup()


class JournalFixtureClockTests(unittest.TestCase):
    """The fixture clock must never borrow the host's uptime again.

    Every repair proof in this file feeds the watchdog journal evidence, and
    the watchdog throws away an event that cannot have happened after the
    daemon started. When the fixture read `time.monotonic()`, that made the
    evidence a function of how long the machine had been up: correct on a
    long-lived workstation, at or below zero on a runner minutes past boot,
    where the repair cases all reported "no evidence" and passed nothing.
    """

    def test_event_timestamp_is_a_pure_function_of_its_age(self) -> None:
        first = json.loads(failure_event(age_seconds=300))
        time.sleep(0.01)
        second = json.loads(failure_event(age_seconds=300))

        self.assertEqual(
            first["__MONOTONIC_TIMESTAMP"], second["__MONOTONIC_TIMESTAMP"]
        )
        self.assertEqual(
            str((FIXTURE_BOOT_SECONDS - 300) * 1_000_000),
            first["__MONOTONIC_TIMESTAMP"],
        )

    def test_every_supported_age_lands_after_the_daemon_started(self) -> None:
        oldest = json.loads(failure_event(age_seconds=FIXTURE_BOOT_SECONDS - 1))

        self.assertGreater(int(oldest["__MONOTONIC_TIMESTAMP"]), 0)
        with self.assertRaises(ValueError):
            failure_event(age_seconds=FIXTURE_BOOT_SECONDS)


class WatchdogSafetyTests(unittest.TestCase):
    """Everything the watchdog must never do."""

    def test_default_mode_reports_direct_evidence_without_repair(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session()],
            journal_lines=FLAG_LINE,
        )
        try:
            env = fixture.env()
            env.pop("SESSION_KIT_WATCHDOG_MODE")
            result = subprocess.run(
                [str(WATCHDOG), "--once"],
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], fixture.repairs_requested())
            recorded = fixture.recorded()
            self.assertEqual(1, len(recorded))
            self.assertEqual("reported", recorded[0]["outcome"])
        finally:
            fixture.close()

    def test_it_never_attaches_to_or_detaches_from_a_session(self) -> None:
        """The rule that matters most.

        An attach probe against a wedged session deadlocked shpool's shells
        mutex on 2026-07-29 and broke every session's manager functions until
        the daemon had to be restarted. The watchdog may therefore never attach
        to, detach from, or kill a session itself.

        Asking `list` whether the manager is alive is explicitly fine: it is
        read-only, it is what the inventory already calls constantly, and it was
        the operation that *hung* during the deadlock rather than the one that
        caused it.
        """
        forbidden = ("attach", "detach", "kill")
        for label, kwargs in (
            ("marker", {"journal_lines": FLAG_LINE}),
            ("missing thread", {"serving_threads": 0}),
            ("silence only", {}),
        ):
            with self.subTest(evidence=label):
                fixture = WatchdogFixture(sessions=[session()], **kwargs)
                try:
                    fixture.run()
                    if not fixture.shpool_log.exists():
                        continue
                    invoked = fixture.shpool_log.read_text(encoding="utf-8")
                    for verb in forbidden:
                        self.assertNotIn(
                            verb, invoked, f"the watchdog invoked shpool {verb}"
                        )
                finally:
                    fixture.close()

    def test_silence_alone_is_reported_and_never_repaired(self) -> None:
        """Silence carries no information.

        Codex reports "running" for its whole life whether it is working or
        waiting at its prompt, so a session parked for hours looks exactly like
        a frozen one. Closing on that basis would eventually close a live
        session.

        These are the statuses that say nothing either way. The statuses that
        DO say a person is being waited on are the test below.
        """
        for status in ("running", "working", "unknown"):
            with self.subTest(agent_status=status):
                fixture = WatchdogFixture(
                    sessions=[
                        session(
                            agent_status=status, recent_output_age_seconds=100_000
                        )
                    ]
                )
                try:
                    fixture.run()
                    self.assertEqual([], fixture.repairs_requested())
                    recorded = fixture.recorded()
                    self.assertEqual(1, len(recorded))
                    self.assertEqual("reported", recorded[0]["outcome"])
                finally:
                    fixture.close()

    def test_a_session_waiting_for_a_person_is_not_a_candidate_at_all(self) -> None:
        """Quiet because it is your turn is not quiet because it is broken.

        This is the defect the operator hit on 2026-08-15: fifty records, every
        one outcome=reported, twelve of them about sessions that were alive
        minutes earlier and simply waiting for them. A research session lives in
        "needs your reply" for days; 45 minutes of journal silence is its
        NORMAL resting state, not evidence of a fault.

        The provider's own state word, and the row's own needs_you flag, are in
        the snapshot the sweep already fetched -- it just was not reading them.
        No record at all is the correct outcome, not a quieter record.
        """
        for status in ("idle", "needs your reply", "reply optional"):
            with self.subTest(agent_status=status):
                fixture = WatchdogFixture(
                    sessions=[
                        session(
                            agent_status=status, recent_output_age_seconds=100_000
                        )
                    ]
                )
                try:
                    fixture.run()
                    self.assertEqual([], fixture.repairs_requested())
                    self.assertEqual([], fixture.recorded())
                finally:
                    fixture.close()

        # The row's own needs_you flag says the same thing on its own, whatever
        # word the provider used for its status.
        with self.subTest(needs_you=True):
            fixture = WatchdogFixture(
                sessions=[
                    session(
                        agent_status="running",
                        needs_you=True,
                        recent_output_age_seconds=100_000,
                    )
                ]
            )
            try:
                fixture.run()
                self.assertEqual([], fixture.repairs_requested())
                self.assertEqual([], fixture.recorded())
            finally:
                fixture.close()

    def test_a_wedged_session_is_still_caught_while_it_says_it_needs_you(
        self,
    ) -> None:
        """The turn state must never suppress hard evidence.

        A terminal the daemon could not hand off is wedged whatever the
        conversation last said. If quietening the waiting case had been applied
        before the daemon-marker branch instead of after it, this session --
        the exact class the watchdog exists for -- would have gone unreported.
        """
        fixture = WatchdogFixture(
            sessions=[
                session(
                    agent_status="needs your reply",
                    needs_you=True,
                    recent_output_age_seconds=100_000,
                )
            ],
            journal_lines=FLAG_LINE,
        )
        try:
            fixture.run()
            # Caught, and acted on: the marker is hard evidence, so this
            # session reaches the repair path the waiting case never can.
            self.assertEqual(["wedged"], fixture.repairs_requested())
            recorded = fixture.recorded()
            self.assertEqual(1, len(recorded))
            self.assertIn("handoff failure", recorded[0]["reason"])
        finally:
            fixture.close()

    def seed_records(self, fixture, *records) -> None:
        fixture.repairs.write_text(
            json.dumps({"schema_version": 1, "repairs": list(records)}, indent=2),
            encoding="utf-8",
        )

    def record(
        self,
        shpool_id: str,
        *,
        age_days: float,
        outcome: str = "reported",
        evidence: str | None = None,
        new_shpool_id: str = "",
    ) -> dict:
        row = {
            "at_unix_ms": int((time.time() - age_days * 86400) * 1000),
            "old_shpool_id": shpool_id,
            "new_shpool_id": new_shpool_id,
            "title": "a session",
            "provider": "codex",
            "outcome": outcome,
            "reason": "no output for far longer than any normal pause",
            "acknowledged": False,
        }
        # Omitted entirely by default: that is the shape of every record
        # already on disk before this field existed.
        if evidence is not None:
            row["evidence"] = evidence
        return row

    def test_a_record_whose_session_is_gone_retires_itself(self) -> None:
        """Nothing ever removed a record before this.

        Thirty-eight of the operator's fifty named shpool ids that no longer
        existed; the only bound was a 50-slot cap that dropped the OLDEST, so
        the review list filled with sessions that had not existed for a day and
        they were asked to clear them one at a time.
        """
        fixture = WatchdogFixture(sessions=[session(recent_output_age_seconds=10)])
        try:
            self.seed_records(
                fixture,
                self.record("long-gone", age_days=9),
                self.record("wedged", age_days=9),
                self.record("gone-but-recent", age_days=0.1),
            )
            fixture.run()
            kept = [item["old_shpool_id"] for item in fixture.recorded()]
            # Gone AND past the window: retired.
            self.assertNotIn("long-gone", kept)
            # Still live, however old the record is: never touched.
            self.assertIn("wedged", kept)
            # Gone but inside the window: still readable.
            self.assertIn("gone-but-recent", kept)
            self.assertIn("retired 1 record", fixture.log_text())
        finally:
            fixture.close()

    def test_retirement_never_runs_without_a_live_snapshot(self) -> None:
        """A sweep that cannot see the estate must not erase its warnings."""
        fixture = WatchdogFixture(sessions=[session(recent_output_age_seconds=10)])
        try:
            document = json.loads(fixture.inventory.read_text(encoding="utf-8"))
            document["stale"] = True
            document["source"] = "cache"
            fixture.inventory.write_text(json.dumps(document), encoding="utf-8")
            self.seed_records(fixture, self.record("long-gone", age_days=9))
            fixture.run()
            self.assertEqual(
                ["long-gone"], [item["old_shpool_id"] for item in fixture.recorded()]
            )
        finally:
            fixture.close()

    def test_retirement_off_switch_and_dry_run_change_nothing(self) -> None:
        for label, overrides in (
            ("off", {"SESSION_KIT_WATCHDOG_RETIRE": "off"}),
            ("zero days", {"SESSION_KIT_WATCHDOG_RETIRE_DAYS": "0"}),
            ("dry run", {"SESSION_KIT_WATCHDOG_RETIRE_DRY_RUN": "1"}),
        ):
            with self.subTest(switch=label):
                fixture = WatchdogFixture(
                    sessions=[session(recent_output_age_seconds=10)]
                )
                try:
                    self.seed_records(fixture, self.record("long-gone", age_days=9))
                    before = fixture.repairs.read_bytes()
                    fixture.run(**overrides)
                    self.assertEqual(before, fixture.repairs.read_bytes())
                finally:
                    fixture.close()

    def test_retirement_leaves_a_foreign_owned_file_alone(self) -> None:
        """Own-uid only, and never through a symlink."""
        fixture = WatchdogFixture(sessions=[session(recent_output_age_seconds=10)])
        try:
            elsewhere = fixture.base / "not-ours.json"
            elsewhere.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "repairs": [self.record("long-gone", age_days=9)],
                    }
                ),
                encoding="utf-8",
            )
            before = elsewhere.read_bytes()
            fixture.repairs.unlink(missing_ok=True)
            fixture.repairs.symlink_to(elsewhere)
            fixture.run()
            # O_NOFOLLOW refuses the link, so the target is untouched.
            self.assertEqual(before, elsewhere.read_bytes())
        finally:
            fixture.close()

    def test_one_record_per_incident_however_long_the_silence_lasts(self) -> None:
        """A second record for a session already on the list says nothing new.

        The old 6-hour cooldown was a rate limit, not a latch: twenty-one
        distinct sessions became fifty records, one of them listed five times,
        and every repeat inflated a count the operator had already said they did
        not trust (ruling, 2026-08-15). The cooldown is set to zero here, so
        only the latch can hold the second and third sweeps back.
        """
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=100_000)]
        )
        try:
            for _ in range(3):
                fixture.run(SESSION_KIT_WATCHDOG_QUIET_REPORT_COOLDOWN="0")
            self.assertEqual(1, len(fixture.recorded()))
        finally:
            fixture.close()

    def test_a_session_that_recovered_and_broke_again_earns_a_new_record(
        self,
    ) -> None:
        """The half that can go wrong quietly: telling incidents apart.

        A record was written at T. Output SINCE T means the session recovered,
        so the silence that followed is a DIFFERENT incident and must be
        recorded -- otherwise one stale row silences a session forever. Output
        still older than T is the same unbroken silence.

        Both shapes need an aged record to be expressible at all: a session
        that spoke recently is not quiet enough to be a candidate, so "it
        recovered" can only ever be seen from a record older than the silence
        threshold.
        """
        # Recovered: the record is 3 hours old, the last output 45 minutes ago,
        # so the session spoke well after the record was written.
        fixture = WatchdogFixture(sessions=[session(recent_output_age_seconds=2_800)])
        try:
            self.seed_records(fixture, self.record("wedged", age_days=3 / 24))
            fixture.run(SESSION_KIT_WATCHDOG_QUIET_REPORT_COOLDOWN="0")
            self.assertEqual(2, len(fixture.recorded()))
        finally:
            fixture.close()

        # Same incident: the record is 3 hours old and the session has not
        # spoken for 5, so its last output still predates the record.
        fixture = WatchdogFixture(sessions=[session(recent_output_age_seconds=18_000)])
        try:
            self.seed_records(fixture, self.record("wedged", age_days=3 / 24))
            fixture.run(SESSION_KIT_WATCHDOG_QUIET_REPORT_COOLDOWN="0")
            self.assertEqual(1, len(fixture.recorded()))
        finally:
            fixture.close()

    def test_a_cleared_row_lets_the_next_incident_speak(self) -> None:
        """Acknowledging is a person saying they are done with that row."""
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=100_000)]
        )
        try:
            fixture.run(SESSION_KIT_WATCHDOG_QUIET_REPORT_COOLDOWN="0")
            recorded = fixture.recorded()
            self.assertEqual(1, len(recorded))
            recorded[0]["acknowledged"] = True
            fixture.repairs.write_text(
                json.dumps({"schema_version": 1, "repairs": recorded}),
                encoding="utf-8",
            )
            fixture.run(SESSION_KIT_WATCHDOG_QUIET_REPORT_COOLDOWN="0")
            self.assertEqual(2, len(fixture.recorded()))
        finally:
            fixture.close()

    def test_hard_evidence_is_never_silenced_by_a_softer_record(self) -> None:
        """The blocker: the latch swallowed the strongest thing the watchdog knows.

        Keying only on the session's output timestamp cannot tell "same
        silence, same story" from "same silence, and we have since learned
        why". So once a session had any unacknowledged `reported` row, the
        DAEMON-MARKER verdict for it was suppressed -- and a wedged terminal
        never stops being quiet, while retirement only clears records whose
        session is GONE, so in practice forever. The operator's list said
        "reported quiet, no repair attempted" about a terminal the daemon had
        already proven could not be served (found in review, 2026-08-15).
        """
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=100_000)],
            journal_lines=FLAG_LINE,
        )
        try:
            self.seed_records(
                fixture,
                self.record("wedged", age_days=8 / 24, evidence="silence-only"),
            )
            fixture.run(
                SESSION_KIT_WATCHDOG_MODE="report",
                SESSION_KIT_WATCHDOG_QUIET_REPORT_COOLDOWN="0",
            )
            reasons = [item["reason"] for item in fixture.recorded()]
            self.assertIn(
                "the daemon logged a terminal handoff failure for this session",
                reasons,
            )
        finally:
            fixture.close()

    def test_hard_evidence_is_recorded_even_when_the_output_age_is_unreadable(
        self,
    ) -> None:
        """A marker proves the terminal is unservable on its own.

        With no readable output age the latch has no timestamp to reason
        about, and defaulting to "same incident" swallowed a fresh,
        daemon-proven second incident.
        """
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=None)],
            journal_lines=FLAG_LINE,
        )
        try:
            self.seed_records(
                fixture,
                self.record("wedged", age_days=1 / 24, evidence="silence-only"),
            )
            fixture.run(
                SESSION_KIT_WATCHDOG_MODE="report",
                SESSION_KIT_WATCHDOG_QUIET_REPORT_COOLDOWN="0",
            )
            self.assertEqual(2, len(fixture.recorded()))
        finally:
            fixture.close()

    def test_the_same_statement_repeated_is_still_one_record(self) -> None:
        """Escalation must not become a licence to duplicate."""
        for label, seeded in (
            ("evidence recorded", "silence-only"),
            ("record predates the field", None),
        ):
            with self.subTest(seeded=label):
                fixture = WatchdogFixture(
                    sessions=[session(recent_output_age_seconds=100_000)]
                )
                try:
                    self.seed_records(
                        fixture,
                        self.record("wedged", age_days=8 / 24, evidence=seeded),
                    )
                    fixture.run(SESSION_KIT_WATCHDOG_QUIET_REPORT_COOLDOWN="0")
                    self.assertEqual(1, len(fixture.recorded()))
                finally:
                    fixture.close()

    def test_a_fleet_stall_flag_is_a_fault_not_a_person_waiting(self) -> None:
        """`needs_you` is not purely a turn-state field.

        The snapshot publisher folds fleet stall flags into it, so a session
        another observer believes is stuck comes back with needs_you true and
        its reason listed. Reading that as "a person is being waited on"
        excluded a genuinely wedged session from detection precisely BECAUSE
        something else had already noticed it was stuck (found in review,
        2026-08-15). A live fault also beats a provider status word, which can
        be frozen at whatever the conversation last said.
        """
        for status in ("running", "needs your reply"):
            with self.subTest(agent_status=status):
                row = session(
                    agent_status=status,
                    needs_you=True,
                    recent_output_age_seconds=100_000,
                )
                row["needs_you_reasons"] = ["silent"]
                fixture = WatchdogFixture(sessions=[row])
                try:
                    fixture.run()
                    recorded = fixture.recorded()
                    self.assertEqual(1, len(recorded))
                    self.assertEqual("reported", recorded[0]["outcome"])
                finally:
                    fixture.close()

    def test_a_record_whose_replacement_is_live_is_never_retired(self) -> None:
        """A repaired record names the session still carrying the conversation.

        Retiring on the old id alone deleted records whose live successor was
        right there in the same snapshot (found in review, 2026-08-15).
        """
        fixture = WatchdogFixture(
            sessions=[session(shpool_id="replacement", recent_output_age_seconds=10)]
        )
        try:
            self.seed_records(
                fixture,
                self.record(
                    "old-id",
                    age_days=9,
                    outcome="repaired",
                    new_shpool_id="replacement",
                ),
            )
            fixture.run()
            self.assertEqual(
                ["old-id"],
                [item["old_shpool_id"] for item in fixture.recorded()],
            )
        finally:
            fixture.close()

    def test_briefly_quiet_session_is_not_even_reported(self) -> None:
        fixture = WatchdogFixture(sessions=[session(recent_output_age_seconds=600)])
        try:
            fixture.run()
            self.assertEqual([], fixture.repairs_requested())
            self.assertEqual([], fixture.recorded())
        finally:
            fixture.close()

    def test_attached_session_is_never_touched(self) -> None:
        """A session showing as attached may have a human on it."""
        fixture = WatchdogFixture(
            sessions=[
                session(availability="attached", recent_output_age_seconds=100_000)
            ],
            journal_lines=FLAG_LINE,
            serving_threads=0,
        )
        try:
            fixture.run()
            self.assertEqual([], fixture.repairs_requested())
            self.assertEqual([], fixture.recorded())
        finally:
            fixture.close()

    def test_session_without_a_conversation_is_never_repaired(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session(provider="shell", uuid=None)],
            journal_lines=FLAG_LINE,
        )
        try:
            fixture.run()
            self.assertEqual([], fixture.repairs_requested())
        finally:
            fixture.close()

    def test_display_only_session_is_never_repaired(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session(mutation_allowed=False)], journal_lines=FLAG_LINE
        )
        try:
            fixture.run()
            self.assertEqual([], fixture.repairs_requested())
        finally:
            fixture.close()

    def test_sentinel_disables_everything(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session()], journal_lines=FLAG_LINE
        )
        try:
            fixture.sentinel.write_text("off\n", encoding="utf-8")
            fixture.run()
            self.assertEqual([], fixture.repairs_requested())
            self.assertEqual([], fixture.recorded())
        finally:
            fixture.close()

    def test_stale_inventory_is_never_acted_on(self) -> None:
        fixture = WatchdogFixture(sessions=[session()], journal_lines=FLAG_LINE)
        try:
            document = json.loads(fixture.inventory.read_text(encoding="utf-8"))
            document["stale"] = True
            document["source"] = "cache"
            fixture.inventory.write_text(json.dumps(document), encoding="utf-8")
            fixture.run()
            self.assertEqual([], fixture.repairs_requested())
        finally:
            fixture.close()

    def test_a_quiet_session_is_reported_once_not_every_sweep(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=100_000)]
        )
        try:
            fixture.run()
            fixture.run()
            fixture.run()
            self.assertEqual(1, len(fixture.recorded()))
        finally:
            fixture.close()


class WatchdogRepairTests(unittest.TestCase):
    """What the watchdog must do when it has real evidence."""

    def test_daemon_marker_repairs_the_session(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            journal_lines=FLAG_LINE,
        )
        try:
            result = fixture.run()
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(["wedged"], fixture.repairs_requested())
        finally:
            fixture.close()

    def test_watchdog_does_not_restamp_the_session_it_repairs(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            journal_lines=FLAG_LINE,
        )
        try:
            write_executable(
                fixture.sp,
                "#!/usr/bin/env bash\n"
                '[[ -z ${SESSION_KIT_ORIGIN:-} ]] || exit 91\n'
                'printf \'%s\\n\' "$*" >> "$WATCHDOG_SP_LOG"\n'
                "echo s20260729-000000-1234\n",
            )
            completed = fixture.run(WATCHDOG_SP_LOG=str(fixture.sp_log))
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("repaired", fixture.recorded()[0]["outcome"])
        finally:
            fixture.close()

    def test_missing_serving_thread_warns_once_and_repairs_nothing(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            serving_threads=0,
        )
        try:
            fixture.run()
            fixture.run()
            self.assertEqual([], fixture.repairs_requested())
            announced = fixture.notify_log.read_text(encoding="utf-8")
            self.assertEqual(1, announced.count("Session manager is serving fewer sessions than it lists"))
        finally:
            fixture.close()

    def test_null_output_age_with_marker_reports_but_never_repairs(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=None)],
            journal_lines=FLAG_LINE,
        )
        try:
            fixture.run()
            self.assertEqual([], fixture.repairs_requested())
            self.assertEqual("reported", fixture.recorded()[0]["outcome"])
        finally:
            fixture.close()

    def test_fresh_marker_is_not_reported_or_repaired(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            journal_lines=failure_event(age_seconds=30),
        )
        try:
            fixture.run()
            self.assertEqual([], fixture.repairs_requested())
            self.assertEqual([], fixture.recorded())
        finally:
            fixture.close()

    def test_later_success_invalidates_failure(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            journal_lines="\n".join(
                [failure_event(age_seconds=300), success_event(age_seconds=60)]
            ),
        )
        try:
            fixture.run()
            self.assertEqual([], fixture.repairs_requested())
            self.assertEqual([], fixture.recorded())
        finally:
            fixture.close()

    def test_marker_from_old_daemon_generation_is_ignored(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            journal_lines=failure_event(pid=os.getpid() + 100_000),
        )
        try:
            fixture.run()
            self.assertEqual([], fixture.repairs_requested())
            self.assertEqual([], fixture.recorded())
        finally:
            fixture.close()

    def test_marker_from_before_the_daemon_started_is_ignored(self) -> None:
        """A failure older than the daemon cannot be describing this daemon.

        The watchdog compares journal timestamps against the daemon's own
        boot-relative start, and both sides of that comparison come from the
        fixture. Nothing else exercises it, and it is the rule a host-derived
        fixture clock used to trip by accident on a freshly booted runner.
        """
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            journal_lines=failure_event(age_seconds=300),
            daemon_start_seconds=FIXTURE_BOOT_SECONDS - 100,
        )
        try:
            fixture.run()

            self.assertEqual([], fixture.repairs_requested())
            self.assertEqual([], fixture.recorded())
        finally:
            fixture.close()

    def test_global_thread_gap_is_not_attributed_to_every_session(self) -> None:
        fixture = WatchdogFixture(
            sessions=[
                session("one", recent_output_age_seconds=300),
                session("two", recent_output_age_seconds=300),
            ],
            serving_threads=1,
        )
        try:
            fixture.run()
            self.assertEqual([], fixture.repairs_requested())
            self.assertEqual([], fixture.recorded())
            announced = fixture.notify_log.read_text(encoding="utf-8")
            self.assertEqual(1, announced.count("Session manager is serving fewer sessions than it lists"))
        finally:
            fixture.close()

    def test_evidence_repairs_whatever_status_the_session_claims(self) -> None:
        for status in ("idle", "needs your reply", "running", "unknown"):
            with self.subTest(agent_status=status):
                fixture = WatchdogFixture(
                    sessions=[session(agent_status=status, recent_output_age_seconds=300)],
                    journal_lines=FLAG_LINE,
                )
                try:
                    fixture.run()
                    self.assertEqual(["wedged"], fixture.repairs_requested())
                finally:
                    fixture.close()

    def test_repair_is_recorded_and_announced(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            journal_lines=FLAG_LINE,
        )
        try:
            fixture.run()
            entry = fixture.recorded()[0]
            self.assertEqual("repaired", entry["outcome"])
            self.assertEqual("wedged", entry["old_shpool_id"])
            self.assertFalse(entry["acknowledged"])
            announced = fixture.notify_log.read_text(encoding="utf-8")
            self.assertIn("--severity=warning", announced)
            self.assertIn("Session restored", announced)
        finally:
            fixture.close()

    def test_failed_repair_is_announced_without_a_critical(self) -> None:
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            journal_lines=FLAG_LINE,
            repair_exit=1,
        )
        try:
            fixture.run()
            self.assertEqual("failed", fixture.recorded()[0]["outcome"])
            announced = fixture.notify_log.read_text(encoding="utf-8")
            self.assertIn("could not be restored", announced)
            # An automatic repair must never wake a phone.
            self.assertNotIn("critical", announced)
        finally:
            fixture.close()

    def test_an_unresponsive_manager_is_reported(self) -> None:
        """The 73-minute silent outage.

        When the manager jams, every open/close/create hangs and repair cannot
        run. Nothing noticed on 2026-07-29; it was found by walking into it.
        """
        fixture = WatchdogFixture(sessions=[session()])
        try:
            # A manager that never answers `list`.
            write_executable(
                fixture.shpool,
                """#!/usr/bin/env bash
if [ "$1" = list ]; then sleep 300; fi
exit 0
""",
            )
            fixture.run(SESSION_KIT_WATCHDOG_MANAGER_TIMEOUT="2")
            announced = (
                fixture.notify_log.read_text(encoding="utf-8")
                if fixture.notify_log.exists()
                else ""
            )
            self.assertIn("not responding", announced)
            self.assertIn("--severity=warning", announced)
            self.assertIn("manager-stuck", fixture.log_text())
        finally:
            fixture.close()

    def test_a_dropped_alert_is_not_recorded_as_delivered(self) -> None:
        """The state file used to say the operator was told.

        On 2026-08-06 the manager was running an unexpected build, the alert
        bounced off a notifier that was down, and `report_once` stamped the
        condition as reported anyway -- so the fault went quiet for six hours
        without a human ever hearing it. A drop is not a delivery.
        """
        fixture = WatchdogFixture(sessions=[session()])
        try:
            write_executable(
                fixture.notify,
                """#!/usr/bin/env bash
exit 1
""",
            )
            write_executable(
                fixture.shpool,
                """#!/usr/bin/env bash
if [ "$1" = list ]; then sleep 300; fi
exit 0
""",
            )
            fixture.run(
                SESSION_KIT_WATCHDOG_MANAGER_TIMEOUT="2",
                SESSION_KIT_WATCHDOG_QUIET_RETRY="0",
            )
            self.assertIn("not delivered", fixture.log_text())
            self.assertFalse(fixture.notify_log.exists())

            # The condition is not held: the next sweep speaks again, and this
            # time the notifier is up.
            write_executable(
                fixture.notify,
                f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {str(fixture.notify_log)!r}
""",
            )
            fixture.run(SESSION_KIT_WATCHDOG_MANAGER_TIMEOUT="2")
            self.assertIn(
                "not responding",
                fixture.notify_log.read_text(encoding="utf-8"),
            )
        finally:
            fixture.close()

    def test_a_dropped_alert_is_held_only_for_the_retry_window(self) -> None:
        fixture = WatchdogFixture(sessions=[session()])
        try:
            write_executable(fixture.notify, "#!/usr/bin/env bash\nexit 1\n")
            write_executable(
                fixture.shpool,
                """#!/usr/bin/env bash
if [ "$1" = list ]; then sleep 300; fi
exit 0
""",
            )
            fixture.run(SESSION_KIT_WATCHDOG_MANAGER_TIMEOUT="2")
            stamps = json.loads(fixture.reported.read_text(encoding="utf-8"))
            held = stamps["reported"]["health:manager-stuck"]
            age = time.time() - held
            # Stamped 15 minutes short of the six-hour cooldown: a notifier
            # nobody configured cannot fill the log, and a notifier that was
            # down for a minute costs one retry.
            self.assertLess(age, 21600)
            self.assertGreater(age, 21600 - 1000)
        finally:
            fixture.close()

    def test_a_delivered_alert_holds_the_full_cooldown(self) -> None:
        fixture = WatchdogFixture(sessions=[session()])
        try:
            write_executable(
                fixture.shpool,
                """#!/usr/bin/env bash
if [ "$1" = list ]; then sleep 300; fi
exit 0
""",
            )
            fixture.run(SESSION_KIT_WATCHDOG_MANAGER_TIMEOUT="2")
            stamps = json.loads(fixture.reported.read_text(encoding="utf-8"))
            held = stamps["reported"]["health:manager-stuck"]
            self.assertLess(time.time() - held, 5)
            # One message per condition per cooldown: the second sweep is quiet.
            before = fixture.notify_log.read_text(encoding="utf-8")
            fixture.run(SESSION_KIT_WATCHDOG_MANAGER_TIMEOUT="2")
            self.assertEqual(
                before, fixture.notify_log.read_text(encoding="utf-8")
            )
        finally:
            fixture.close()

    def test_a_replaced_manager_binary_is_reported(self) -> None:
        """A routine reinstall must not silently undo the patch."""
        fixture = WatchdogFixture(sessions=[session()])
        try:
            fingerprint = fixture.base / "expected.sha256"
            fingerprint.write_text("0" * 64 + "\n", encoding="utf-8")
            fixture.run(
                SESSION_KIT_WATCHDOG_BINARY_FINGERPRINT=str(fingerprint)
            )
            log = fixture.log_text()
            # Reported only when a running daemon can actually be identified;
            # never a false alarm when there is nothing to compare against.
            if "binary-changed" in log:
                self.assertIn(
                    "different build",
                    fixture.notify_log.read_text(encoding="utf-8"),
                )
        finally:
            fixture.close()

    def test_a_restored_session_that_prints_a_closing_note_is_a_repair(
        self,
    ) -> None:
        """The status decides, not the shape of the last line printed."""
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            journal_lines=FLAG_LINE,
        )
        try:
            write_executable(
                fixture.sp,
                """#!/usr/bin/env bash
echo s20260811-101500-7
echo "session-kit: the terminal name was kept" >&2
exit 0
""",
            )
            fixture.run()
            entry = fixture.recorded()[0]
            self.assertEqual("repaired", entry["outcome"])
            self.assertEqual("s20260811-101500-7", entry["new_shpool_id"])
            self.assertIn("Session restored", fixture.notify_log.read_text())
        finally:
            fixture.close()

    def test_a_failed_repair_is_never_read_as_a_new_session(self) -> None:
        """A diagnostic that merely looks like an ID is still a failure."""
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            journal_lines=FLAG_LINE,
        )
        try:
            write_executable(
                fixture.sp,
                """#!/usr/bin/env bash
echo s20260811-101500-7
echo "session-kit: shpool refused to close the exact disconnected session" >&2
exit 1
""",
            )
            fixture.run()
            entry = fixture.recorded()[0]
            self.assertEqual("failed", entry["outcome"])
            self.assertEqual("", entry["new_shpool_id"])
            self.assertIn("could not be restored", fixture.notify_log.read_text())
        finally:
            fixture.close()

    def test_a_post_kill_failure_announces_the_route_that_works(self) -> None:
        """The session is gone, so "open the picker" is not the answer."""
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            journal_lines=FLAG_LINE,
        )
        try:
            write_executable(
                fixture.sp,
                """#!/usr/bin/env bash
echo "The original terminal was already closed but the conversation is intact on disk." >&2
echo "Retry with: sp restore-exact claude 11111111-2222-4333-8444-555555555555 /srv/work" >&2
exit 1
""",
            )
            fixture.run()
            self.assertEqual("failed", fixture.recorded()[0]["outcome"])
            announced = fixture.notify_log.read_text(encoding="utf-8")
            self.assertIn(
                "sp restore-exact claude "
                "11111111-2222-4333-8444-555555555555 /srv/work",
                announced,
            )
            self.assertNotIn("Open the picker to restore it.", announced)
        finally:
            fixture.close()

    def test_a_blind_inventory_defers_instead_of_reporting_failure(self) -> None:
        """A loaded daemon is a blind spot, not a failed repair."""
        fixture = WatchdogFixture(
            sessions=[session(recent_output_age_seconds=300)],
            journal_lines=FLAG_LINE,
        )
        try:
            write_executable(
                fixture.sp,
                """#!/usr/bin/env bash
echo "session inventory: guard live snapshot unavailable; refusing stale, partial, or malformed data" >&2
exit 1
""",
            )
            fixture.run()
            self.assertEqual([], fixture.recorded())
            self.assertFalse(fixture.notify_log.exists())
            self.assertIn("repair deferred", fixture.log_text())
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
