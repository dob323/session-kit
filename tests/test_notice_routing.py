"""Every notice that is not a picker row still reaches the operator.

The picker is being narrowed to "sessions waiting for you". Four things it used
to carry are not that -- a repair that failed, a session reported quiet with
nothing changed, a stalled session, and another window's open question -- and
three of those four had NO other route to a person at all. These tests are the
proof that removing them from the screen costs nothing:

  * presence is decided by evidence and fails SAFE;
  * the away transport gets what the terminal cannot take;
  * the terminal route delivers over a session's own message socket, never a
    pty, and is proved against a real socket rather than a mock;
  * one incident is one notice, and a notice that did not land is never
    recorded as delivered;
  * nothing here writes anything either picker reads.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import REPO
from tests.test_watchdog import WatchdogFixture, session, write_executable

NOTICE = REPO / "bin" / "session_kit_notice"
WATCHDOG = REPO / "bin" / "session_kit_watchdog"
UUID = "00000000-0000-4000-8000-000000000abc"


def load_notice_module():
    loader = importlib.machinery.SourceFileLoader(
        "session_kit_notice_under_test", str(NOTICE)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("could not load session_kit_notice")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


NOTICE_MODULE = load_notice_module()

from sessionkit_inventory import transcript_text  # noqa: E402


def stamp(age_seconds: int = 0) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_seconds)
    )


def snapshot(*, attached: list[dict] | None = None, age_seconds: int = 0, **overrides):
    document = {
        "schema_version": 1,
        "source": "live",
        "stale": False,
        "generated_at": stamp(age_seconds),
        "sessions": attached or [],
        "outside_agents": [],
    }
    document.update(overrides)
    return document


def attached_session(
    *,
    provider: str = "claude",
    uuid: str = UUID,
    needs_you: bool = False,
    title: str = "A session",
    age: int = 5,
) -> dict:
    return {
        "shpool_id": "s20260815-000000-1",
        "display_title": title,
        "title": title,
        "provider": provider,
        "availability": "attached",
        "needs_you": needs_you,
        "recent_output_age_seconds": age,
        "identity": {"uuid": uuid, "confidence": "exact"},
    }


class Sandbox:
    """One notice router, wired to recorders instead of anything real."""

    def __init__(self, **files: object) -> None:
        # Short prefix: a Unix socket path is capped at 108 bytes and the
        # terminal-route fixture binds one under here.
        self.temp = tempfile.TemporaryDirectory(prefix="nr-")
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.shpool = self.bin / "shpool"
        write_executable(
            self.shpool,
            "#!/bin/sh\n"
            "echo 'test fixture refuses shpool' >&2\n"
            "exit 97\n",
        )
        self.fleet = self.base / "fleet"
        (self.fleet / "inbox").mkdir(parents=True)
        self.away_log = self.base / "away.log"
        self.away = self.base / "away"
        write_executable(
            self.away,
            f"""#!/usr/bin/env bash
# ONE line per invocation, whatever the body contains: a batch body has
# real line breaks in it, and counting log lines would count each of them as
# a separate message -- which is the number under test.
printf '%s\\n' "$*" | tr '\\n' ' ' >> {str(self.away_log)!r}
printf '\\n' >> {str(self.away_log)!r}
""",
        )
        self.snapshot = self.base / "snapshot.json"
        self.snapshot.write_text(json.dumps(snapshot()), encoding="utf-8")
        self.repairs = self.state / "watchdog-repairs.json"
        for name, payload in files.items():
            (self.base / name).write_text(json.dumps(payload), encoding="utf-8")

    def env(self, **overrides: str) -> dict[str, str]:
        # Every path a kit binary can reach is pinned inside this sandbox. On
        # 2026-08-15 three of the operator's live sessions were closed by an
        # unsandboxed run from an agent worktree; a test that can see the real
        # estate is one bad default away from being that run.
        environment = {
            **os.environ,
            "HOME": str(self.base),
            "XDG_STATE_HOME": str(self.base / "xdg-state"),
            "XDG_DATA_HOME": str(self.base / "xdg-data"),
            "SESSION_KIT_STATE_DIR": str(self.state),
            "SESSION_KIT_JOURNAL_DIR": str(self.base / "journals"),
            "SESSION_KIT_ARCHIVE_DIR": str(self.base / "archives"),
            "SESSION_KIT_JOURNAL_RECOVERY_DIR": str(self.base / "recovery"),
            "SESSION_KIT_START_DIR": str(self.base),
            "SESSION_KIT_PROJECTS_FILE": str(self.base / "projects.json"),
            "SESSION_KIT_CONFIG": str(self.base / "config.toml"),
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "SESSION_KIT_NONINTERACTIVE": "1",
            "SESSION_KIT_BACKGROUND": "1",
            # Never the real session manager: a fake that refuses, and a
            # snapshot file the presence test reads instead of asking it.
            "SESSION_KIT_SHPOOL_CMD": str(self.shpool),
            "SESSION_KIT_STATUS_CMD": str(self.base / "no-such-status"),
            "SESSION_KIT_SP_CMD": str(self.base / "no-such-sp"),
            "SESSION_KIT_FLEET_DIR": str(self.fleet),
            "SESSION_KIT_NOTICE_AWAY_CMD": str(self.away),
            "SESSION_KIT_NOTICE_SNAPSHOT": str(self.snapshot),
            "SESSION_KIT_WATCHDOG_REPAIRS": str(self.repairs),
        }
        environment.update(overrides)
        return environment

    def write_snapshot(self, document: object) -> None:
        self.snapshot.write_text(json.dumps(document), encoding="utf-8")

    def typed_in(
        self,
        uuid: str = UUID,
        *,
        seconds_ago: float = 5,
        event: str = "UserPromptSubmit",
    ) -> None:
        """Record that a PERSON submitted a prompt in this session.

        This is the kit's own evidence of input: the Claude UserPromptSubmit
        hook writes it, and `attention_hook.sh` calls that branch "The person
        answered". It is the only thing the presence test accepts, because
        provider OUTPUT keeps happening after everyone has gone home.
        """
        directory = self.state / "attention" / "claude"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{uuid}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": uuid,
                    "hook_event": event,
                    "notification_type": "",
                    "message": "",
                    "needs_you": False,
                    "recorded_at_ms": int((time.time() - seconds_ago) * 1000),
                }
            ),
            encoding="utf-8",
        )

    def run(self, *argv: str, **overrides: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(NOTICE), *argv],
            env=self.env(**overrides),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def route(self, **overrides: str) -> dict:
        result = self.run(
            "route",
            "--class", overrides.pop("notice_class", "quiet-session"),
            "--key", overrides.pop("key", "quiet:s1"),
            "--subject", overrides.pop("subject", "Session quiet: A session"),
            "--body", overrides.pop("body", "Nothing was changed."),
            "--evidence", overrides.pop("evidence", "silence-only"),
            **overrides,
        )
        return json.loads(result.stdout or "{}")

    def away_lines(self) -> list[str]:
        if not self.away_log.exists():
            return []
        return [
            line
            for line in self.away_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def close(self) -> None:
        self.temp.cleanup()


class FakeSession:
    """A real Unix socket standing in for a live Claude session.

    Nothing is mocked: the router's terminal route is exercised against the
    vendor's own registry shape, its auth-key file and a socket that answers,
    because "it writes to a socket" is the whole claim being made -- that a
    notice never goes near a terminal a provider is drawing.
    """

    def __init__(
        self,
        base: Path,
        *,
        uuid: str = UUID,
        linger: float = 1.0,
        records_transcript: bool = True,
    ) -> None:
        self.uuid = uuid
        self.linger = linger
        # A real session writes what it renders into its own transcript, and
        # that file is the transport's documented receipt of record. A session
        # that accepts the bytes and records nothing is the lost-after-write
        # case, and it must NOT count as delivered.
        self.records_transcript = records_transcript
        self.runtime = base / "run"
        (self.runtime / "cc-socks").mkdir(parents=True)
        os.chmod(self.runtime, 0o700)
        os.chmod(self.runtime / "cc-socks", 0o700)
        self.config = base / "claude"
        (self.config / "sessions").mkdir(parents=True)
        (self.config / "projects" / "a-project").mkdir(parents=True)
        self.transcript = self.config / "projects" / "a-project" / f"{uuid}.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.pid = os.getpid()
        self.path = self.runtime / "cc-socks" / f"{self.pid}.sock"
        self.received: list[str] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.server.listen(4)
        self.server.settimeout(30)
        (self.config / "sessions" / f"{self.pid}.json").write_text(
            json.dumps(
                {
                    "pid": self.pid,
                    "sessionId": uuid,
                    "messagingSocketPath": str(self.path),
                    "kind": "interactive",
                    "status": "idle",
                }
            ),
            encoding="utf-8",
        )
        digest = hashlib.sha256(
            os.path.normpath(os.path.abspath(str(self.path))).encode("utf-8")
        ).hexdigest()
        key = self.config / "sessions" / f"{self.pid}.{digest}.key"
        key.write_text(
            json.dumps({"peerToken": "a" * 32, "procStart": "1"}), encoding="utf-8"
        )
        os.chmod(key, 0o600)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(10)
                buffer = b""
                try:
                    # Auth line first, then the body. The connection is held
                    # open across both, because closing it is how a real
                    # session says "token refused".
                    while len(buffer.splitlines()) < 2 and len(buffer) < 200_000:
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        buffer += chunk
                except OSError:
                    pass
                for line in buffer.decode("utf-8", "replace").splitlines():
                    self.received.append(line)
                    if self.records_transcript and '"type": "user"' in line.replace(
                        '"type":"user"', '"type": "user"'
                    ):
                        # What a live session does with a message it renders.
                        with open(self.transcript, "a", encoding="utf-8") as handle:
                            handle.write(line + "\n")
                if self.linger:
                    # A live session does not slam the connection shut the
                    # instant it has the body; holding it open here is what
                    # makes the ORDINARY outcome the one under test.
                    time.sleep(self.linger)

    def messages(self) -> list[dict]:
        found = []
        for line in list(self.received):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") == "user":
                found.append(record)
        return found

    def close(self) -> None:
        try:
            self.server.close()
        except OSError:
            pass


class CountedTerminal:
    """A terminal boundary that records calls and can change live state."""

    SENT_NO_RECEIPT = "sent-no-receipt"
    CLOSED_AFTER_SEND = "closed-after-send"

    def __init__(self, after_capability=None, during_send=None, outcome=None) -> None:
        self.after_capability = after_capability
        self.during_send = during_send
        self.outcome = outcome
        self.capability_calls = 0
        self.send_calls = 0

    def capability(self, uuid: str) -> dict:
        self.capability_calls += 1
        if self.after_capability is not None:
            self.after_capability()
        return {"available": True, "reason": "fixture terminal"}

    def send(self, *args, **kwargs) -> SimpleNamespace:
        self.send_calls += 1
        if self.during_send is not None:
            self.during_send()
        return self.outcome or SimpleNamespace(
            delivered=True,
            how="fixture queue acknowledgement",
            extra={"message_uuid": "fixture-message-uuid"},
        )


class PresenceTests(unittest.TestCase):
    """At the machine is EVIDENCE, and every other answer is away."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.close)

    def verdict(self, document: object) -> dict:
        self.sandbox.write_snapshot(document)
        result = self.sandbox.run("presence")
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_a_person_typing_is_the_evidence_of_presence(self) -> None:
        self.sandbox.typed_in(seconds_ago=5)
        answer = self.verdict(snapshot(attached=[attached_session()]))
        self.assertTrue(answer["present"], answer)
        self.assertIn("a person typed", answer["why"])
        self.assertEqual(1, len(answer["targets"]))
        self.assertEqual(UUID, answer["targets"][0]["uuid"])

    def test_a_busy_session_nobody_is_watching_is_not_presence(self) -> None:
        """Provider output is AUTONOMOUS and proves nothing about a person.

        A session keeps printing for as long as its turn takes, so a window they
        walked away from mid-turn looked more present than one they were reading
        — and the notice was delivered into that unattended conversation and
        latched as said. Output age is no longer consulted at all.
        """
        answer = self.verdict(snapshot(attached=[attached_session(age=1)]))
        self.assertFalse(answer["present"], answer)
        self.assertIn("no sign of a person typing", answer["why"])
        self.assertEqual([], answer["targets"])

    def test_input_that_has_gone_cold_is_a_window_left_open(self) -> None:
        self.sandbox.typed_in(seconds_ago=900)
        answer = self.verdict(snapshot(attached=[attached_session(age=1)]))
        self.assertFalse(answer["present"], answer)
        self.assertIn("nobody has typed", answer["why"])

    def test_input_from_more_than_five_seconds_in_the_future_is_not_evidence(self) -> None:
        """A wall-clock step backwards must not manufacture a person."""
        self.sandbox.typed_in(seconds_ago=-3600)
        answer = self.verdict(snapshot(attached=[attached_session(age=1)]))
        self.assertFalse(answer["present"], answer)
        self.assertIn("no sign of a person typing", answer["why"])

    def test_a_notification_is_not_a_person_typing(self) -> None:
        """The record is replaced per session, so the newest event decides.

        A notification is the session asking for a person, not a person
        answering, and it must not read as one.
        """
        self.sandbox.typed_in(seconds_ago=5, event="Notification")
        answer = self.verdict(snapshot(attached=[attached_session(age=1)]))
        self.assertFalse(answer["present"], answer)
        self.assertIn("no sign of a person typing", answer["why"])

    def test_nothing_attached_is_away(self) -> None:
        detached = attached_session()
        detached["availability"] = "ready"
        answer = self.verdict(snapshot(attached=[detached]))
        self.assertFalse(answer["present"])
        self.assertIn("no terminal is attached", answer["why"])

    def test_a_snapshot_that_is_not_live_is_away(self) -> None:
        answer = self.verdict(
            snapshot(attached=[attached_session()], source="cache", stale=True)
        )
        self.assertFalse(answer["present"])
        self.assertIn("presence unknown", answer["why"])

    def test_an_attached_but_silent_session_is_a_window_left_open(self) -> None:
        """`attached` means "open in another window", not "someone is there".

        The watchdog beside this file exists partly because a session can stay
        wired to an SSH window that no longer exists. Treating that as presence
        suppressed the away route AND prompted an abandoned conversation.
        """
        answer = self.verdict(
            snapshot(attached=[attached_session(age=99999, title="SSH window left open")])
        )
        self.assertFalse(answer["present"], answer)
        self.assertEqual([], answer["targets"])

    def test_two_attached_terminals_are_ambiguous_and_go_away(self) -> None:
        """Nothing in the snapshot says which window they are in.

        Provider output age is not focus: a session working by itself prints
        constantly while they read another. Ranking by it chose the busiest
        conversation and PROMPTED it, because the frame is a user message.
        """
        self.sandbox.typed_in(seconds_ago=5)
        self.sandbox.typed_in("00000000-0000-4000-8000-000000000def", seconds_ago=5)
        answer = self.verdict(
            snapshot(
                attached=[
                    attached_session(age=20, title="One", uuid=UUID),
                    attached_session(
                        age=1, title="Two",
                        uuid="00000000-0000-4000-8000-000000000def",
                    ),
                ]
            )
        )
        self.assertFalse(answer["present"], answer)
        self.assertIn("which one they are in", answer["why"])
        self.assertEqual([], answer["targets"])

    def test_a_snapshot_from_the_future_is_not_evidence(self) -> None:
        """A clock a minute ahead is not something to decide presence on."""
        self.sandbox.typed_in(seconds_ago=5)
        answer = self.verdict(
            snapshot(attached=[attached_session()], age_seconds=-50)
        )
        self.assertFalse(answer["present"], answer)
        self.assertIn("presence unknown", answer["why"])

    def test_asking_who_is_here_never_uses_a_writing_mode(self) -> None:
        """Presence is a question, not a refresh.

        `shpool_status --json` is listed by its own usage under "Modes that
        refresh the cached inventory as they run": it hydrates and PUSHES
        automatic titles and publishes snapshot state. This ran it every 60
        seconds under the watchdog, doubling the machine's refresh and
        title-push rate to ask a read-only question. `--strict-json` is the
        documented no-write answer.
        """
        argv_log = self.sandbox.base / "status-argv.log"
        status = self.sandbox.base / "status"
        write_executable(
            status,
            f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {str(argv_log)!r}
printf '%s\\n' '{json.dumps(snapshot())}'
""",
        )
        result = self.sandbox.run(
            "presence",
            SESSION_KIT_NOTICE_SNAPSHOT="",
            SESSION_KIT_STATUS_CMD=str(status),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        recorded = argv_log.read_text(encoding="utf-8")
        self.assertIn("--strict-json", recorded)
        self.assertNotIn("--json", recorded.replace("--strict-json", ""))
        for writing_mode in ("--waiting-count", "--detail", "--lookup"):
            self.assertNotIn(writing_mode, recorded)

    def test_a_status_command_that_refuses_is_away_not_a_guess(self) -> None:
        """`--strict-json` exits 3 rather than answer from ambiguous evidence,
        and a refusal must read as presence unknown."""
        status = self.sandbox.base / "status"
        write_executable(
            status,
            "#!/usr/bin/env bash\n"
            "echo 'session inventory: strict live snapshot unavailable' >&2\n"
            "exit 3\n",
        )
        result = self.sandbox.run(
            "presence",
            SESSION_KIT_NOTICE_SNAPSHOT="",
            SESSION_KIT_STATUS_CMD=str(status),
        )
        answer = json.loads(result.stdout)
        self.assertFalse(answer["present"], answer)
        self.assertIn("presence unknown", answer["why"])

    def test_an_old_snapshot_cannot_answer_for_now(self) -> None:
        """An hour-old "attached" is a truthful record of an hour ago.

        Reading it as "they are here" delivers the notice to an empty chair, which
        is the one direction this must never be wrong in.
        """
        self.sandbox.typed_in(seconds_ago=5)
        answer = self.verdict(
            snapshot(attached=[attached_session()], age_seconds=3600)
        )
        self.assertFalse(answer["present"])
        self.assertIn("presence unknown", answer["why"])


class AwayRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.close)

    def test_away_gets_the_notice_on_the_existing_notifier_contract(self) -> None:
        answer = self.sandbox.route()
        self.assertTrue(answer["routed"], answer)
        self.assertEqual("away", answer["route"])
        line = self.sandbox.away_lines()[0]
        for flag in ("--type=", "--severity=warning", "--title=", "--body="):
            self.assertIn(flag, line)
        self.assertIn("Session quiet", line)

    def test_presence_that_cannot_be_determined_still_reaches_the_operator(self) -> None:
        """Fail SAFE: unknown presence is away, never silence."""
        self.sandbox.snapshot.unlink()
        answer = self.sandbox.route()
        self.assertTrue(answer["routed"], answer)
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_a_transport_that_failed_is_never_recorded_as_delivered(self) -> None:
        """A drop is not a delivery.

        A dropped alert once got stamped as told and suppressed for six hours.
        The record says plainly that nothing landed, and the next pass retries.
        """
        write_executable(self.sandbox.away, "#!/usr/bin/env bash\nexit 1\n")
        answer = self.sandbox.route()
        self.assertFalse(answer["routed"], answer)
        latch_file = self.sandbox.state / "notice-latch.json"
        latch = json.loads(latch_file.read_text(encoding="utf-8"))
        entry = latch["notices"]["quiet-session:quiet:s1"]
        self.assertFalse(entry["landed"])

        # No successful handoff means there is nothing truthful to suppress.
        write_executable(
            self.sandbox.away,
            f"""#!/usr/bin/env bash
printf '%s\\n' "$*" | tr '\\n' ' ' >> {str(self.sandbox.away_log)!r}
printf '\\n' >> {str(self.sandbox.away_log)!r}
""",
        )
        again = self.sandbox.route()
        self.assertTrue(again["routed"], again)

    def test_no_transport_configured_says_so_instead_of_claiming_success(self) -> None:
        """An unwired away leg is a wiring fault, and it has to READ like one.

        This is the live state of the machine it was written for: nothing is
        configured, so every away notice reaches nobody. That must be loud in
        the log, non-zero on the way out, and impossible to mistake for a quiet
        night.
        """
        answer = self.sandbox.route(SESSION_KIT_NOTICE_AWAY_CMD="")
        self.assertFalse(answer["routed"], answer)
        self.assertIn("SESSION_KIT_NOTICE_AWAY_CMD", answer["why"])
        log = (self.sandbox.state / "notice.log").read_text(encoding="utf-8")
        self.assertIn("UNWIRED", log)

    def test_a_sweep_that_reached_nobody_exits_non_zero(self) -> None:
        self.sandbox.repairs.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "repairs": [
                        {
                            "at_unix_ms": int(time.time() * 1000),
                            "old_shpool_id": "s1", "new_shpool_id": "",
                            "title": "Some work", "provider": "codex",
                            "outcome": "reported", "acknowledged": False,
                            "reason": "no output for far longer than any normal pause",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = self.sandbox.run("sweep", SESSION_KIT_NOTICE_AWAY_CMD="")
        self.assertEqual(1, result.returncode, result.stdout)
        answer = json.loads(result.stdout)
        self.assertEqual("UNSET", answer["away_transport"])
        self.assertEqual(1, answer["messages"])
        self.assertEqual(0, answer["routed"])

    def test_the_kill_switch_routes_nothing(self) -> None:
        answer = self.sandbox.route(SESSION_KIT_NOTICE="off")
        self.assertFalse(answer["routed"])
        self.assertEqual([], self.sandbox.away_lines())


class ShippedAwayTransportTests(unittest.TestCase):
    """The worked example is a real transport, proved against a recorder.

    Never against a live notification path: the card command here writes a
    file. The two things being proved are the two the example exists for --
    the text reaches the card command as ONE argument, and a card that did not
    go out reports a failure instead of a success.
    """

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.close)
        self.card_log = self.sandbox.base / "card.log"
        self.card = self.sandbox.base / "card"
        write_executable(
            self.card,
            f"""#!/usr/bin/env bash
printf 'ARGS=%s\\n' "$#" >> {str(self.card_log)!r}
printf '%s\\n' "$1" >> {str(self.card_log)!r}
""",
        )

    def test_the_example_hands_the_notice_over_as_one_argument(self) -> None:
        answer = self.sandbox.route(
            SESSION_KIT_NOTICE_AWAY_CMD=str(REPO / "extras" / "notify-chat-card"),
            SESSION_KIT_NOTICE_CARD_CMD=str(self.card),
        )
        self.assertTrue(answer["routed"], answer)
        recorded = self.card_log.read_text(encoding="utf-8")
        self.assertIn("ARGS=1", recorded)
        self.assertIn("Session quiet", recorded)

    def test_an_unwired_card_command_is_a_failure_not_a_silent_success(self) -> None:
        answer = self.sandbox.route(
            SESSION_KIT_NOTICE_AWAY_CMD=str(REPO / "extras" / "notify-chat-card"),
            SESSION_KIT_NOTICE_CARD_CMD="",
        )
        self.assertFalse(answer["routed"], answer)


class LatchTests(unittest.TestCase):
    """One incident, one notice -- and genuinely new evidence is a new notice."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.close)

    def test_the_same_statement_is_not_made_twice(self) -> None:
        self.sandbox.route()
        second = self.sandbox.route()
        self.assertEqual("latched", second["route"])
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_stronger_evidence_about_the_same_session_is_a_new_notice(self) -> None:
        self.sandbox.route(evidence="silence-only")
        second = self.sandbox.route(evidence="daemon-marker")
        self.assertTrue(second["routed"], second)
        self.assertEqual(2, len(self.sandbox.away_lines()))

    def test_a_repeated_condition_cannot_buzz_every_pass(self) -> None:
        for _ in range(5):
            self.sandbox.route()
        self.assertEqual(1, len(self.sandbox.away_lines()))


class SweepTests(unittest.TestCase):
    """The classes that live in a record and were never announced anywhere."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.close)

    def write_repairs(self, *entries: dict) -> None:
        self.sandbox.repairs.write_text(
            json.dumps({"schema_version": 1, "repairs": list(entries)}),
            encoding="utf-8",
        )

    def quiet_record(self, session_id: str = "s1", evidence: str = "silence-only") -> dict:
        return {
            "at_unix_ms": int(time.time() * 1000),
            "old_shpool_id": session_id,
            "new_shpool_id": "",
            "title": "Some work",
            "provider": "codex",
            "outcome": "reported",
            "evidence": evidence,
            "reason": "no output for far longer than any normal pause",
            "acknowledged": False,
        }

    def sweep(self, expect_status: int = 0, **overrides: str) -> dict:
        result = self.sandbox.run("sweep", **overrides)
        self.assertEqual(expect_status, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_a_session_reported_quiet_now_reaches_the_operator(self) -> None:
        """The class with no route at all before this: outcome=reported."""
        self.write_repairs(self.quiet_record())
        answer = self.sweep()
        self.assertEqual(1, answer["routed"], answer)
        line = self.sandbox.away_lines()[0]
        self.assertIn("Some work", line)
        self.assertIn("nothing was changed", line.casefold())

    def test_a_stalled_session_reaches_the_operator(self) -> None:
        (self.sandbox.fleet / "stalls.json").write_text(
            json.dumps(
                {
                    "generated_at": time.time(),
                    "stalled": [{"key": "claude:abc", "reason": "silent"}],
                }
            ),
            encoding="utf-8",
        )
        answer = self.sweep()
        self.assertEqual(1, answer["routed"], answer)
        self.assertIn("stalled", self.sandbox.away_lines()[0].casefold())

    def test_a_dead_stall_observer_says_nothing(self) -> None:
        """A flag file nobody is updating is not news; it is a last opinion."""
        (self.sandbox.fleet / "stalls.json").write_text(
            json.dumps(
                {
                    "generated_at": time.time() - 4000,
                    "stalled": [{"key": "claude:abc", "reason": "silent"}],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(0, self.sweep()["seen"])

    def test_a_background_question_reaches_the_operator_while_away(self) -> None:
        """The fourth class, and it has to have a route like the other three.

        A log line and a latch entry are not a delivery route. This shipped
        held-by-default and that left the class with no away route at all,
        which is the exact hole the branch exists to close.
        """
        (self.sandbox.fleet / "inbox" / "q1.json").write_text(
            json.dumps(
                {
                    "id": "q1",
                    "state": "open",
                    "header": "Blueprint",
                    "session": {"title": "Draft work", "uuid": UUID},
                }
            ),
            encoding="utf-8",
        )
        answer = self.sweep()
        self.assertEqual(1, answer["routed"], answer)
        self.assertEqual(0, answer["held"])
        line = self.sandbox.away_lines()[0]
        self.assertIn("Blueprint", line)
        # It says a decision is waiting and where. It never carries the
        # question itself or its options.
        self.assertIn("question inbox", line)

    def test_the_question_push_can_be_switched_back_off_by_name(self) -> None:
        """`hold` restores the operator's earlier ruling exactly, in one word,
        and a held question is recorded and counted rather than dropped."""
        (self.sandbox.fleet / "inbox" / "q1.json").write_text(
            json.dumps({"id": "q1", "state": "open", "header": "Blueprint"}),
            encoding="utf-8",
        )
        answer = self.sweep(SESSION_KIT_NOTICE_QUESTION_AWAY="hold")
        self.assertEqual(1, answer["held"], answer)
        self.assertEqual([], self.sandbox.away_lines())
        latch = json.loads(
            (self.sandbox.state / "notice-latch.json").read_text(encoding="utf-8")
        )
        self.assertEqual("held", latch["notices"]["background-question:question:q1"]["route"])
        self.assertIn("HELD", (self.sandbox.state / "notice.log").read_text())

    def test_one_record_is_one_notice_however_often_the_sweep_runs(self) -> None:
        self.write_repairs(self.quiet_record())
        for _ in range(4):
            self.sweep()
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_a_record_that_retires_takes_its_latch_with_it(self) -> None:
        """Otherwise a session that breaks again is silenced by bookkeeping
        nobody can see."""
        self.write_repairs(self.quiet_record())
        self.sweep()
        self.write_repairs()
        self.assertEqual(1, self.sweep()["forgotten"])
        self.write_repairs(self.quiet_record())
        self.assertEqual(1, self.sweep()["routed"])
        self.assertEqual(2, len(self.sandbox.away_lines()))

    def test_a_failed_repair_reaches_the_operator(self) -> None:
        record = self.quiet_record()
        record["outcome"] = "failed"
        self.write_repairs(record)
        answer = self.sweep()
        self.assertEqual(1, answer["routed"], answer)
        self.assertIn("repair failed", self.sandbox.away_lines()[0].casefold())

    def test_a_successful_repair_is_not_a_notice(self) -> None:
        """Nothing for them to do. It keeps the route it already has."""
        record = self.quiet_record()
        record["outcome"] = "repaired"
        record["new_shpool_id"] = "s2"
        self.write_repairs(record)
        self.assertEqual(0, self.sweep()["seen"])

    def test_five_records_about_one_session_are_one_notice(self) -> None:
        """Live data, 2026-08-15: one wedged session wrote five records in a
        day, and 50 records covered only 21 sessions."""
        self.write_repairs(*[self.quiet_record() for _ in range(5)])
        answer = self.sweep()
        self.assertEqual(1, answer["seen"], answer)
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_everything_new_arrives_as_one_message(self) -> None:
        """The volume finding: one message per pass, whatever it covers.

        Fifty separate messages is not a notification design, it is a reason to
        turn notifications off.
        """
        self.write_repairs(
            *[self.quiet_record(session_id=f"s{index}") for index in range(9)]
        )
        answer = self.sweep()
        self.assertEqual(9, answer["new"], answer)
        self.assertEqual(1, answer["messages"])
        lines = self.sandbox.away_lines()
        self.assertEqual(1, len(lines))
        self.assertIn("9 sessions need a look", lines[0])
        # Names a few, counts the rest, and never pastes nine paragraphs.
        self.assertIn("and 4 more", lines[0])

    def test_an_old_unresolved_record_is_counted_not_quietly_swallowed(self) -> None:
        """Age is not acknowledgement.

        This used to take a record older than the bound into the latch WITHOUT
        sending it, which converted an unresolved warning into "said" on the
        strength of a clock. The bound now decides what gets NAMED; everything
        unresolved is still counted in the one message that goes out.
        """
        old = self.quiet_record(session_id="old")
        old["at_unix_ms"] = int((time.time() - 30 * 3600) * 1000)
        self.write_repairs(old, self.quiet_record(session_id="new"))
        answer = self.sweep()
        self.assertEqual(1, answer["older"], answer)
        self.assertEqual(2, answer["new"])
        self.assertEqual(2, answer["routed"])
        lines = self.sandbox.away_lines()
        self.assertEqual(1, len(lines), "still one message, not one per record")
        self.assertIn("2 sessions need a look", lines[0])
        self.assertIn("unresolved for over", lines[0])

    def test_a_backlog_of_only_old_records_is_still_said_once(self) -> None:
        """Every record on the live estate this was measured against was hours
        old. If age alone silenced them, the whole class would have arrived
        already suppressed."""
        records = []
        for index in range(4):
            record = self.quiet_record(session_id=f"old{index}")
            record["at_unix_ms"] = int((time.time() - 20 * 3600) * 1000)
            records.append(record)
        self.write_repairs(*records)
        answer = self.sweep()
        self.assertEqual(4, answer["older"], answer)
        self.assertEqual(4, answer["routed"])
        self.assertEqual(1, len(self.sandbox.away_lines()))
        # Said once, then quiet: the backlog is one buzz, not a nightly one.
        self.assertEqual(0, self.sweep()["new"])
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_evidence_escalates_but_never_downgrades(self) -> None:
        """Within one incident: stronger evidence speaks, weaker does not.

        The same ranking the watchdog's own record latch uses, so the two
        cannot disagree about one event stream.
        """
        first = self.quiet_record(evidence="silence-only")
        self.write_repairs(first)
        self.assertEqual(1, self.sweep()["routed"])
        stronger = self.quiet_record(evidence="daemon-marker")
        stronger["at_unix_ms"] = first["at_unix_ms"]        # same incident
        self.write_repairs(stronger)
        self.assertEqual(1, self.sweep()["routed"])
        self.assertEqual(2, len(self.sandbox.away_lines()))
        weaker = self.quiet_record(evidence="silence-only")
        weaker["at_unix_ms"] = first["at_unix_ms"]
        self.write_repairs(weaker)
        self.assertEqual(0, self.sweep()["new"])
        self.assertEqual(2, len(self.sandbox.away_lines()))

    def test_a_second_incident_after_a_recovery_is_said(self) -> None:
        """ONE latch rule, and it is the watchdog's.

        The paired watchdog treats output after the previous record as recovery
        and writes a fresh record for the same session and the same evidence.
        This file used to collapse those to one key and call the second one
        already-said, so a real second failure produced a watchdog record and
        NO notice — information loss of exactly the kind the branch exists to
        prevent. A record written after the last thing said about a session is
        the watchdog telling us the last incident was over.
        """
        first = self.quiet_record(session_id="s1")
        first["at_unix_ms"] = int((time.time() - 900) * 1000)
        self.write_repairs(first)
        self.assertEqual(1, self.sweep()["routed"])
        self.assertEqual(1, len(self.sandbox.away_lines()))

        # Same session, same evidence, and the watchdog wrote it AFTER the one
        # we spoke about: it recovered and broke again.
        again = self.quiet_record(session_id="s1")
        self.write_repairs(first, again)
        answer = self.sweep()
        self.assertEqual(1, answer["routed"], answer)
        self.assertEqual(0, answer["latched"])
        self.assertEqual(2, len(self.sandbox.away_lines()))

        # And re-reading the same records says nothing further.
        self.assertEqual(0, self.sweep()["new"])
        self.assertEqual(2, len(self.sandbox.away_lines()))

    def test_a_batch_that_did_not_land_comes_back(self) -> None:
        write_executable(self.sandbox.away, "#!/usr/bin/env bash\nexit 1\n")
        self.write_repairs(self.quiet_record(session_id="a"), self.quiet_record(session_id="b"))
        # A sweep that had something to say and reached nobody EXITS NON-ZERO.
        # It used to exit 0, so an unwired transport looked like a quiet night.
        answer = self.sweep(expect_status=1)
        self.assertEqual(0, answer["routed"], answer)
        latch = json.loads(
            (self.sandbox.state / "notice-latch.json").read_text(encoding="utf-8")
        )
        self.assertEqual(2, len(latch["notices"]))
        for entry in latch["notices"].values():
            self.assertFalse(entry["landed"])
            entry["at_unix"] = time.time() - 1000
        (self.sandbox.state / "notice-latch.json").write_text(
            json.dumps(latch), encoding="utf-8"
        )
        write_executable(
            self.sandbox.away,
            f"""#!/usr/bin/env bash
printf '%s\\n' "$*" | tr '\\n' ' ' >> {str(self.sandbox.away_log)!r}
printf '\\n' >> {str(self.sandbox.away_log)!r}
""",
        )
        self.assertEqual(2, self.sweep()["routed"])

    def test_a_failed_batch_is_retried_on_the_next_pass_not_called_quiet(self) -> None:
        write_executable(self.sandbox.away, "#!/bin/sh\nexit 1\n")
        self.write_repairs(
            self.quiet_record(session_id="a"),
            self.quiet_record(session_id="b"),
        )
        first = self.sweep(expect_status=1)
        self.assertEqual(0, first["routed"], first)
        write_executable(
            self.sandbox.away,
            f"""#!/bin/sh
printf '%s\\n' "$*" | tr '\\n' ' ' >> {str(self.sandbox.away_log)!r}
printf '\\n' >> {str(self.sandbox.away_log)!r}
""",
        )
        second = self.sweep()
        self.assertEqual(2, second["routed"], second)
        self.assertFalse(second["quiet"], second)

    def test_a_batch_never_leaves_a_latch_entry_nothing_can_retire(self) -> None:
        self.write_repairs(self.quiet_record(session_id="a"), self.quiet_record(session_id="b"))
        self.sweep()
        latch = json.loads(
            (self.sandbox.state / "notice-latch.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {"quiet-session:quiet:a", "quiet-session:quiet:b"},
            set(latch["notices"]),
        )

    def test_an_acknowledged_record_is_not_re_announced(self) -> None:
        record = self.quiet_record()
        record["acknowledged"] = True
        self.write_repairs(record)
        self.assertEqual(0, self.sweep()["seen"])


class PickerUntouchedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.close)

    def test_routing_writes_nothing_either_picker_reads(self) -> None:
        """No row, no count, no line: the router owns its own two files."""
        self.sandbox.repairs.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "repairs": [
                        {
                            "at_unix_ms": int(time.time() * 1000),
                            "old_shpool_id": "s1",
                            "new_shpool_id": "",
                            "title": "Some work",
                            "provider": "codex",
                            "outcome": "reported",
                            "reason": "no output for far longer than any normal pause",
                            "acknowledged": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.sandbox.fleet / "stalls.json").write_text(
            json.dumps(
                {"generated_at": time.time(), "stalled": [{"key": "k", "reason": "silent"}]}
            ),
            encoding="utf-8",
        )
        (self.sandbox.fleet / "inbox" / "q1.json").write_text(
            json.dumps({"id": "q1", "state": "open", "header": "Blueprint"}),
            encoding="utf-8",
        )
        watched = {
            path: path.read_bytes()
            for path in (
                self.sandbox.repairs,
                self.sandbox.fleet / "stalls.json",
                self.sandbox.fleet / "inbox" / "q1.json",
            )
        }
        self.sandbox.run("sweep")
        self.sandbox.route()
        for path, before in watched.items():
            self.assertEqual(before, path.read_bytes(), f"{path} was modified")
        self.assertFalse((self.sandbox.state / "inventory.json").exists())
        self.assertEqual(
            {"notice-latch.json", "notice.log", "watchdog-repairs.json"},
            {item.name for item in self.sandbox.state.iterdir()},
        )

    def test_no_picker_module_calls_the_router(self) -> None:
        for relative in (
            "lib/sh/shpool_login_render.sh",
            "lib/sh/shpool_login_view.sh",
            "bin/shpool_login",
            "bin/shpool_login_tui",
        ):
            self.assertNotIn(
                "session_kit_notice",
                (REPO / relative).read_text(encoding="utf-8"),
                f"{relative} reaches into the notice router",
            )
        for module in sorted((REPO / "lib" / "sessionkit_tui").glob("*.py")):
            self.assertNotIn(
                "session_kit_notice", module.read_text(encoding="utf-8")
            )


class InProcessTerminalDecisionTests(unittest.TestCase):
    """Terminal decisions with a counted boundary and no transport socket."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.close)
        self.sandbox.write_snapshot(snapshot(attached=[attached_session()]))
        self.sandbox.typed_in(seconds_ago=5)
        self.confirmations = 0

    def route(self, terminal: CountedTerminal) -> dict:
        def transcript_record(*args, **kwargs) -> bool:
            self.confirmations += 1
            return True

        with mock.patch.dict(os.environ, self.sandbox.env(), clear=True):
            with mock.patch.object(NOTICE_MODULE, "claude_socket", terminal):
                with mock.patch.object(
                    NOTICE_MODULE,
                    "confirm_in_transcript",
                    transcript_record,
                ):
                    return NOTICE_MODULE.route(
                        "quiet-session",
                        "quiet:s1",
                        "Session quiet: A session",
                        "Nothing was changed.",
                        evidence="silence-only",
                    )

    def assert_confirmation_interval_detach_routes_away(self, stage: str) -> None:
        """Exercise every boundary from authorization through reconciliation."""
        ready = attached_session()
        ready["availability"] = "ready"

        def detach() -> None:
            self.sandbox.write_snapshot(snapshot(attached=[ready]))

        terminal = CountedTerminal(
            after_capability=detach if stage == "before-send" else None,
            during_send=detach if stage == "inside-send" else None,
            outcome=SimpleNamespace(
                delivered=False,
                how=CountedTerminal.SENT_NO_RECEIPT,
                extra={"message_uuid": "fixture-message-uuid"},
            ),
        )
        original_presence = NOTICE_MODULE.presence
        presence_calls = 0

        def staged_presence(*args, **kwargs):
            nonlocal presence_calls
            presence_calls += 1
            if stage == "between-send-and-confirmation" and presence_calls == 3:
                detach()
            if stage == "after-confirmation" and presence_calls == 4:
                detach()
            return original_presence(*args, **kwargs)

        def staged_confirmation(*args, **kwargs) -> bool:
            if stage == "during-confirmation":
                detach()
            return True

        with mock.patch.dict(os.environ, self.sandbox.env(), clear=True):
            with mock.patch.object(NOTICE_MODULE, "claude_socket", terminal):
                with mock.patch.object(NOTICE_MODULE, "presence", staged_presence):
                    with mock.patch.object(
                        NOTICE_MODULE,
                        "confirm_in_transcript",
                        staged_confirmation,
                    ):
                        answer = NOTICE_MODULE.route(
                            "quiet-session",
                            "quiet:s1",
                            "Session quiet: A session",
                            "Nothing was changed.",
                            evidence="silence-only",
                        )

        self.assertTrue(answer["routed"], answer)
        self.assertEqual("away", answer["route"], answer)
        self.assertEqual(0 if stage == "before-send" else 1, terminal.send_calls)
        self.assertEqual(1, len(self.sandbox.away_lines()))
        latch = json.loads(
            (self.sandbox.state / "notice-latch.json").read_text(encoding="utf-8")
        )["notices"]["quiet-session:quiet:s1"]
        self.assertEqual("away", latch["route"])
        self.assertTrue(latch["landed"])
        self.assertTrue(latch["reconciled"])
        self.assertEqual("reconciled", latch["state"])

    def assert_presence_change_routes_away(self, rows: list[dict]) -> None:
        terminal = CountedTerminal(
            after_capability=lambda: self.sandbox.write_snapshot(
                snapshot(attached=rows)
            )
        )
        answer = self.route(terminal)
        self.assertTrue(answer["routed"], answer)
        self.assertEqual("away", answer["route"], answer)
        self.assertEqual(0, terminal.send_calls)
        self.assertEqual(1, len(self.sandbox.away_lines()))

    @staticmethod
    def production_transcript_record(message_uuid: str, body: str) -> dict:
        """The user-message shape emitted by claude_socket.send()."""
        return {
            "type": "user",
            "message": {"content": body},
            "session_id": UUID,
            "uuid": message_uuid,
            "msg_id": "fixture-message-id",
            "priority": "next",
            "from": "fixture-receipt-listener",
        }

    def route_with_transcript(
        self, raw_transcript: bytes, message_uuid: str
    ) -> tuple[dict, CountedTerminal, Path]:
        config = self.sandbox.base / "claude"
        transcript = config / "projects" / "fixture" / f"{UUID}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_bytes(raw_transcript)
        terminal = CountedTerminal(
            outcome=SimpleNamespace(
                delivered=True,
                how="delivered",
                extra={"message_uuid": message_uuid},
            )
        )
        environment = self.sandbox.env(
            CLAUDE_CONFIG_DIR=str(config),
            SESSION_KIT_NOTICE_CONFIRM_SECONDS="0",
        )
        with mock.patch.dict(os.environ, environment, clear=True):
            with mock.patch.object(NOTICE_MODULE, "claude_socket", terminal):
                answer = NOTICE_MODULE.route(
                    "quiet-session",
                    "quiet:s1",
                    "Session quiet: A session",
                    "Nothing was changed.",
                    evidence="silence-only",
                )
        return answer, terminal, transcript

    def test_a_future_input_record_routes_away_and_is_never_latched_as_terminal(
        self,
    ) -> None:
        self.sandbox.typed_in(seconds_ago=-3600)
        terminal = CountedTerminal()
        answer = self.route(terminal)
        self.assertTrue(answer["routed"], answer)
        self.assertEqual("away", answer["route"], answer)
        self.assertEqual(0, terminal.send_calls)
        self.assertEqual(1, len(self.sandbox.away_lines()))
        latch = json.loads(
            (self.sandbox.state / "notice-latch.json").read_text(encoding="utf-8")
        )["notices"]["quiet-session:quiet:s1"]
        self.assertTrue(latch["landed"])
        self.assertEqual("away", latch["route"])

    def test_an_unchanged_target_is_sent_after_the_second_presence_check(self) -> None:
        terminal = CountedTerminal()
        answer = self.route(terminal)
        self.assertTrue(answer["routed"], answer)
        self.assertEqual("terminal", answer["route"], answer)
        self.assertEqual(1, terminal.capability_calls)
        self.assertEqual(1, terminal.send_calls)
        self.assertEqual(1, self.confirmations)
        self.assertEqual([], self.sandbox.away_lines())

    def test_interval_detach_before_send_reconciles_away(self) -> None:
        self.assert_confirmation_interval_detach_routes_away("before-send")

    def test_interval_detach_inside_send_reconciles_away(self) -> None:
        self.assert_confirmation_interval_detach_routes_away("inside-send")

    def test_interval_detach_between_send_and_confirmation_reconciles_away(
        self,
    ) -> None:
        self.assert_confirmation_interval_detach_routes_away(
            "between-send-and-confirmation"
        )

    def test_interval_detach_during_confirmation_reconciles_away(self) -> None:
        self.assert_confirmation_interval_detach_routes_away("during-confirmation")

    def test_interval_detach_after_confirmation_reconciles_away(self) -> None:
        self.assert_confirmation_interval_detach_routes_away("after-confirmation")

    def test_a_queue_receipt_without_a_transcript_record_reconciles_away(
        self,
    ) -> None:
        """The socket queue is not the target conversation's receipt of record."""
        terminal = CountedTerminal(
            outcome=SimpleNamespace(
                delivered=True,
                how="delivered",
                extra={"message_uuid": "fixture-message-uuid"},
            )
        )
        confirmations = []

        def missing_record(*args, **kwargs) -> bool:
            confirmations.append((args, kwargs))
            return False

        with mock.patch.dict(os.environ, self.sandbox.env(), clear=True):
            with mock.patch.object(NOTICE_MODULE, "claude_socket", terminal):
                with mock.patch.object(
                    NOTICE_MODULE,
                    "confirm_in_transcript",
                    missing_record,
                ):
                    answer = NOTICE_MODULE.route(
                        "quiet-session",
                        "quiet:s1",
                        "Session quiet: A session",
                        "Nothing was changed.",
                        evidence="silence-only",
                    )

        self.assertEqual(1, len(confirmations))
        self.assertEqual("away", answer["route"], answer)
        self.assertTrue(answer["routed"], answer)
        self.assertEqual(1, terminal.send_calls)
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_a_transcript_line_truncated_after_its_uuid_routes_away(self) -> None:
        message_uuid = "11111111-1111-4111-8111-a11111111111"
        complete = json.dumps(
            self.production_transcript_record(message_uuid, "Nothing was changed.")
        )
        raw = complete[: complete.index('"msg_id"')].encode("utf-8")

        answer, terminal, transcript = self.route_with_transcript(raw, message_uuid)

        self.assertEqual([], transcript_text.render_transcript(transcript))
        self.assertEqual("away", answer["route"], answer)
        self.assertEqual(1, terminal.send_calls)
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_a_uuid_in_malformed_json_before_valid_unrelated_lines_routes_away(
        self,
    ) -> None:
        message_uuid = "22222222-2222-4222-8222-a22222222222"
        malformed = json.dumps(
            self.production_transcript_record(message_uuid, "Nothing was changed.")
        )
        malformed = malformed[: malformed.index('"msg_id"')]
        unrelated = json.dumps(
            self.production_transcript_record(
                "33333333-3333-4333-8333-a33333333333",
                "A separate complete conversation message.",
            )
        )

        answer, terminal, transcript = self.route_with_transcript(
            f"{malformed}\n{unrelated}\n".encode("utf-8"), message_uuid
        )

        self.assertIn(
            "A separate complete conversation message.",
            transcript_text.render_transcript(transcript),
        )
        self.assertEqual("away", answer["route"], answer)
        self.assertEqual(1, terminal.send_calls)
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_a_valid_renderable_record_with_the_uuid_reconciles_terminal(
        self,
    ) -> None:
        message_uuid = "44444444-4444-4444-8444-a44444444444"
        body = "Nothing was changed."
        raw = json.dumps(
            self.production_transcript_record(message_uuid, body)
        ).replace(message_uuid, message_uuid.replace("a", r"\u0061"))

        answer, terminal, transcript = self.route_with_transcript(
            f"{raw}\n".encode("utf-8"), message_uuid
        )

        self.assertIn(body, transcript_text.render_transcript(transcript))
        self.assertEqual("terminal", answer["route"], answer)
        self.assertEqual(1, terminal.send_calls)
        self.assertEqual([], self.sandbox.away_lines())

    def test_a_cut_window_line_is_discarded_without_losing_later_records(
        self,
    ) -> None:
        message_uuid = "55555555-5555-4555-8555-a55555555555"
        unrelated = json.dumps(
            self.production_transcript_record(
                "66666666-6666-4666-8666-a66666666666",
                "A complete unrelated message.",
            )
        )
        config = self.sandbox.base / "claude"
        transcript = config / "projects" / "fixture" / f"{UUID}.jsonl"
        transcript.parent.mkdir(parents=True)
        environment = self.sandbox.env(CLAUDE_CONFIG_DIR=str(config))

        # The 256 KiB tail starts inside this malformed first line. Its UUID
        # bytes are not a record and must not confirm the complete unrelated
        # line that follows it.
        transcript.write_bytes(
            (b"x" * 300_000)
            + message_uuid.encode("ascii")
            + b"\n"
            + unrelated.encode("utf-8")
            + b"\n"
        )
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertFalse(
                NOTICE_MODULE.confirm_in_transcript(
                    UUID, message_uuid, deadline=time.time()
                )
            )

        # Discarding the cut first line must discard only that line. A later
        # complete production record remains eligible, including normal JSON
        # escaping that makes raw byte-substring matching insufficient.
        target = json.dumps(
            self.production_transcript_record(
                message_uuid, "The complete target message."
            )
        ).replace(message_uuid, message_uuid.replace("a", r"\u0061"))
        transcript.write_bytes(
            (b"y" * 300_000) + b"\n" + target.encode("utf-8") + b"\n"
        )
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertTrue(
                NOTICE_MODULE.confirm_in_transcript(
                    UUID, message_uuid, deadline=time.time()
                )
            )

    def test_queue_receipt_establishes_transcript_before_final_presence(
        self,
    ) -> None:
        """The attachment answer used for promotion cannot predate the receipt."""
        terminal = CountedTerminal(
            outcome=SimpleNamespace(
                delivered=True,
                how="delivered",
                extra={"message_uuid": "fixture-message-uuid"},
            )
        )
        ready = attached_session()
        ready["availability"] = "ready"
        evidence_order = []
        original_presence = NOTICE_MODULE.presence

        def ordered_presence(*args, **kwargs):
            if kwargs.get("require_current"):
                evidence_order.append("current-presence")
            return original_presence(*args, **kwargs)

        def confirm_then_detach(*args, **kwargs) -> bool:
            evidence_order.append("transcript-record")
            self.sandbox.write_snapshot(snapshot(attached=[ready]))
            return True

        with mock.patch.dict(os.environ, self.sandbox.env(), clear=True):
            with mock.patch.object(NOTICE_MODULE, "claude_socket", terminal):
                with mock.patch.object(NOTICE_MODULE, "presence", ordered_presence):
                    with mock.patch.object(
                        NOTICE_MODULE,
                        "confirm_in_transcript",
                        confirm_then_detach,
                    ):
                        answer = NOTICE_MODULE.route(
                            "quiet-session",
                            "quiet:s1",
                            "Session quiet: A session",
                            "Nothing was changed.",
                            evidence="silence-only",
                        )

        self.assertEqual(
            ["transcript-record", "current-presence"], evidence_order[-2:]
        )
        self.assertEqual("away", answer["route"], answer)
        self.assertTrue(answer["routed"], answer)
        self.assertEqual(1, terminal.send_calls)
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_stable_queue_receipt_confirms_real_transcript_once(self) -> None:
        """Ordinary use adds one immediate file check, not another notice."""
        message_uuid = "fixture-message-uuid"
        config = self.sandbox.base / "claude"
        transcript = config / "projects" / "fixture" / f"{UUID}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps(
                self.production_transcript_record(
                    message_uuid, "Nothing was changed."
                )
            )
            + "\n",
            encoding="utf-8",
        )
        terminal = CountedTerminal(
            outcome=SimpleNamespace(
                delivered=True,
                how="delivered",
                extra={"message_uuid": message_uuid},
            )
        )
        presence_calls = 0
        confirmation_calls = 0
        original_presence = NOTICE_MODULE.presence
        original_confirmation = NOTICE_MODULE.confirm_in_transcript

        def counted_presence(*args, **kwargs):
            nonlocal presence_calls
            presence_calls += 1
            return original_presence(*args, **kwargs)

        def counted_confirmation(*args, **kwargs) -> bool:
            nonlocal confirmation_calls
            confirmation_calls += 1
            return original_confirmation(*args, **kwargs)

        environment = self.sandbox.env(
            CLAUDE_CONFIG_DIR=str(config),
            SESSION_KIT_NOTICE_CONFIRM_SECONDS="1",
        )
        started = time.monotonic()
        with mock.patch.dict(os.environ, environment, clear=True):
            with mock.patch.object(NOTICE_MODULE, "claude_socket", terminal):
                with mock.patch.object(NOTICE_MODULE, "presence", counted_presence):
                    with mock.patch.object(
                        NOTICE_MODULE,
                        "confirm_in_transcript",
                        counted_confirmation,
                    ):
                        answer = NOTICE_MODULE.route(
                            "quiet-session",
                            "quiet:s1",
                            "Session quiet: A session",
                            "Nothing was changed.",
                            evidence="silence-only",
                        )
        elapsed = time.monotonic() - started

        self.assertEqual("terminal", answer["route"], answer)
        self.assertTrue(answer["routed"], answer)
        self.assertEqual(4, presence_calls)
        self.assertEqual(1, confirmation_calls)
        self.assertEqual(1, terminal.send_calls)
        self.assertEqual([], self.sandbox.away_lines())
        self.assertLess(elapsed, 1.0)

    def test_terminal_latch_is_provisional_until_receipt_reconciliation(self) -> None:
        terminal = CountedTerminal(
            outcome=SimpleNamespace(
                delivered=False,
                how=CountedTerminal.SENT_NO_RECEIPT,
                extra={"message_uuid": "fixture-message-uuid"},
            )
        )
        observed = []

        def confirm_while_provisional(*args, **kwargs) -> bool:
            entry = json.loads(
                (self.sandbox.state / "notice-latch.json").read_text(
                    encoding="utf-8"
                )
            )["notices"]["quiet-session:quiet:s1"]
            observed.append(entry)
            return True

        with mock.patch.dict(os.environ, self.sandbox.env(), clear=True):
            with mock.patch.object(NOTICE_MODULE, "claude_socket", terminal):
                with mock.patch.object(
                    NOTICE_MODULE,
                    "confirm_in_transcript",
                    confirm_while_provisional,
                ):
                    answer = NOTICE_MODULE.route(
                        "quiet-session",
                        "quiet:s1",
                        "Session quiet: A session",
                        "Nothing was changed.",
                        evidence="silence-only",
                    )

        self.assertEqual(1, len(observed))
        self.assertEqual("provisional", observed[0]["state"])
        self.assertFalse(observed[0]["reconciled"])
        self.assertFalse(observed[0]["landed"])
        self.assertEqual("terminal", answer["route"], answer)
        self.assertTrue(answer["routed"], answer)
        self.assertEqual([], self.sandbox.away_lines())
        final = json.loads(
            (self.sandbox.state / "notice-latch.json").read_text(encoding="utf-8")
        )["notices"]["quiet-session:quiet:s1"]
        self.assertEqual("reconciled", final["state"])
        self.assertTrue(final["reconciled"])
        self.assertTrue(final["landed"])

    def test_only_a_reconciled_success_can_suppress_a_replay(self) -> None:
        provisional = {
            "evidence": "silence-only",
            "at_unix": time.time(),
            "route": "terminal",
            "landed": True,
            "reconciled": False,
            "state": "provisional",
        }
        self.assertFalse(
            NOTICE_MODULE.already_said(provisional, "silence-only", 3600)
        )

    def test_presence_is_reproved_after_the_session_capability_check(self) -> None:
        """The only attached row can detach after the first verdict."""
        ready = attached_session()
        ready["availability"] = "ready"
        self.assert_presence_change_routes_away([ready])

    def test_a_detach_during_send_routes_away_despite_a_terminal_receipt(self) -> None:
        """A receipt cannot call an unattended conversation a delivery."""
        ready = attached_session()
        ready["availability"] = "ready"
        terminal = CountedTerminal(
            during_send=lambda: self.sandbox.write_snapshot(
                snapshot(attached=[ready])
            )
        )
        answer = self.route(terminal)
        self.assertTrue(answer["routed"], answer)
        self.assertEqual("away", answer["route"], answer)
        self.assertEqual(1, terminal.capability_calls)
        self.assertEqual(1, terminal.send_calls)
        self.assertEqual(1, len(self.sandbox.away_lines()))
        self.assertIn("may have accepted", answer["why"])
        latch = json.loads(
            (self.sandbox.state / "notice-latch.json").read_text(encoding="utf-8")
        )["notices"]["quiet-session:quiet:s1"]
        self.assertEqual("away", latch["route"])
        self.assertTrue(latch["landed"])

    def test_a_detach_during_send_never_latches_a_failed_away_leg(self) -> None:
        """Possible terminal acceptance is not proof enough to suppress retry."""
        write_executable(self.sandbox.away, "#!/bin/sh\nexit 1\n")
        ready = attached_session()
        ready["availability"] = "ready"
        terminal = CountedTerminal(
            during_send=lambda: self.sandbox.write_snapshot(
                snapshot(attached=[ready])
            )
        )
        answer = self.route(terminal)
        self.assertFalse(answer["routed"], answer)
        self.assertEqual("away", answer["route"], answer)
        self.assertEqual(1, terminal.send_calls)
        latch = json.loads(
            (self.sandbox.state / "notice-latch.json").read_text(encoding="utf-8")
        )["notices"]["quiet-session:quiet:s1"]
        self.assertEqual("away", latch["route"])
        self.assertFalse(latch["landed"])

    def test_post_send_proof_never_uses_the_published_inventory_fallback(self) -> None:
        """A recent cache cannot say what stayed attached during this send."""
        with mock.patch.dict(os.environ, self.sandbox.env(), clear=True):
            initial = NOTICE_MODULE.presence()
        (self.sandbox.state / "inventory.json").write_text(
            self.sandbox.snapshot.read_text(encoding="utf-8"), encoding="utf-8"
        )
        terminal = CountedTerminal()
        environment = self.sandbox.env(SESSION_KIT_NOTICE_SNAPSHOT="")
        with mock.patch.dict(os.environ, environment, clear=True):
            with mock.patch.object(NOTICE_MODULE, "claude_socket", terminal):
                answer = NOTICE_MODULE.route(
                    "quiet-session",
                    "quiet:s1",
                    "Session quiet: A session",
                    "Nothing was changed.",
                    evidence="silence-only",
                    verdict=initial,
                )
        self.assertTrue(answer["routed"], answer)
        self.assertEqual("away", answer["route"], answer)
        self.assertEqual(1, terminal.send_calls)
        self.assertEqual(1, len(self.sandbox.away_lines()))
        self.assertIn("no current answer from the session manager", answer["why"])

    def test_a_second_attachment_after_the_verdict_routes_away(self) -> None:
        self.assert_presence_change_routes_away(
            [
                attached_session(),
                attached_session(
                    uuid="00000000-0000-4000-8000-000000000def",
                    title="Another session",
                ),
            ]
        )

    def test_a_question_opened_after_the_verdict_routes_away(self) -> None:
        self.assert_presence_change_routes_away(
            [attached_session(needs_you=True)]
        )


class TerminalRouteTests(unittest.TestCase):
    """When they are at the machine the notice goes into the session, not the pty."""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.close)
        self.fake = FakeSession(self.sandbox.base)
        self.addCleanup(self.fake.close)
        self.sandbox.write_snapshot(snapshot(attached=[attached_session()]))
        # A person typed in it five seconds ago: the only thing that makes the
        # terminal route apply at all.
        self.sandbox.typed_in(seconds_ago=5)

    def env(self, **overrides: str) -> dict[str, str]:
        return {
            "XDG_RUNTIME_DIR": str(self.fake.runtime),
            "CLAUDE_CONFIG_DIR": str(self.fake.config),
            "SESSION_KIT_NOTICE_TIMEOUT": "2",
            **overrides,
        }

    def test_an_attached_session_receives_it_over_its_own_socket(self) -> None:
        answer = self.sandbox.route(**self.env())
        self.assertTrue(answer["routed"], answer)
        self.assertEqual("terminal", answer["route"])
        # And nothing was pushed to their phone while they were sitting right there.
        self.assertEqual([], self.sandbox.away_lines())
        deadline = time.time() + 10
        while time.time() < deadline and not self.fake.messages():
            time.sleep(0.1)
        messages = self.fake.messages()
        self.assertEqual(1, len(messages), self.fake.received)
        self.assertEqual(UUID, messages[0]["session_id"])
        self.assertIn("Session quiet", json.dumps(messages[0]))

    def test_a_session_that_closes_the_connection_after_the_body_still_counts(
        self,
    ) -> None:
        """A close AFTER the body is never a refusal (the module's own rule).

        Treating it as one would send the notice to their phone a second time
        while they watched it arrive in front of them.
        """
        self.fake.linger = 0
        answer = self.sandbox.route(**self.env())
        self.assertEqual("terminal", answer["route"], answer)
        self.assertEqual([], self.sandbox.away_lines())

    def test_a_send_the_conversation_never_recorded_is_not_a_delivery(self) -> None:
        """The lost-after-write case, and the transport says it cannot see it.

        `send()` returns delivered=False for `sent-no-receipt` and names the
        target's own transcript as the receipt of record. Calling that landed
        — which this did — reported a message they never saw AND latched it, so
        for a swept record it stayed suppressed while the record lived.
        """
        self.fake.records_transcript = False
        answer = self.sandbox.route(**self.env(SESSION_KIT_NOTICE_CONFIRM_SECONDS="1"))
        self.assertEqual("away", answer["route"], answer)
        self.assertTrue(answer["routed"])
        self.assertIn("never appeared in the conversation", answer["why"])
        # It reached them by the other road rather than being counted as sent.
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_two_attached_terminals_send_nothing_into_either(self) -> None:
        self.sandbox.write_snapshot(
            snapshot(
                attached=[
                    attached_session(title="One", uuid=UUID),
                    attached_session(
                        title="Two", uuid="00000000-0000-4000-8000-000000000def"
                    ),
                ]
            )
        )
        self.sandbox.typed_in("00000000-0000-4000-8000-000000000def", seconds_ago=5)
        answer = self.sandbox.route(**self.env())
        self.assertEqual("away", answer["route"], answer)
        self.assertEqual([], self.fake.messages())
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_a_session_with_its_own_question_open_is_not_interrupted(self) -> None:
        """A delivery over an open picker pushed the question off their screen
        once already. A notice is exactly the thing that can wait."""
        self.sandbox.write_snapshot(
            snapshot(attached=[attached_session(needs_you=True)])
        )
        answer = self.sandbox.route(**self.env())
        self.assertTrue(answer["routed"], answer)
        self.assertEqual("away", answer["route"])
        self.assertEqual([], self.fake.messages())
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_a_provider_this_repository_cannot_address_falls_through(self) -> None:
        """Codex sessions are steered from outside this repository. Naming that
        is the point: the notice goes away rather than nowhere."""
        self.sandbox.write_snapshot(
            snapshot(attached=[attached_session(provider="codex")])
        )
        answer = self.sandbox.route(**self.env())
        self.assertTrue(answer["routed"], answer)
        self.assertEqual("away", answer["route"])
        self.assertIn("cannot be addressed here", answer["why"])

    def test_an_unreachable_session_falls_through_rather_than_vanishing(self) -> None:
        self.fake.path.unlink()
        answer = self.sandbox.route(**self.env())
        self.assertTrue(answer["routed"], answer)
        self.assertEqual("away", answer["route"])
        self.assertEqual(1, len(self.sandbox.away_lines()))

    def test_the_terminal_route_can_be_switched_off(self) -> None:
        answer = self.sandbox.route(**self.env(SESSION_KIT_NOTICE_TERMINAL="off"))
        self.assertEqual("away", answer["route"])
        self.assertEqual([], self.fake.messages())


class WatchdogWiringTests(unittest.TestCase):
    """The watchdog's own alerts take the same route, and its sweep feeds it."""

    def fixture(self, **kwargs) -> WatchdogFixture:
        made = WatchdogFixture(**kwargs)
        self.addCleanup(made.close)
        made.notice_log = made.base / "notice-calls.log"
        made.notice = made.bin / "notice"
        write_executable(
            made.notice,
            f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {str(made.notice_log)!r}
echo '{{"routed": true, "route": "away"}}'
""",
        )
        return made

    def notice_calls(self, made: WatchdogFixture) -> list[str]:
        if not made.notice_log.exists():
            return []
        return [
            line
            for line in made.notice_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_the_alert_path_is_left_exactly_as_it_was(self) -> None:
        """`notify()` is not a route to them and this branch does not touch it.

        Its notifier is an alert board with a 7-day retention, and granting it
        push would mean claiming an emergency policy id — an alerting decision
        that belongs to the operator, not to this branch. The router is a
        SEPARATE road; `notify()` stays byte-identical to what it was.
        """
        source = (REPO / "bin" / "session_kit_watchdog").read_text(encoding="utf-8")
        body = source[source.index("\nnotify() {") : source.index("\nflagged_events()")]
        self.assertNotIn("session_kit_notice", body)
        self.assertNotIn("NOTICE_CMD", body)
        self.assertNotIn("route_notice", source)

    def test_a_manager_alert_is_not_diverted_through_the_router(self) -> None:
        made = self.fixture(sessions=[session()])
        write_executable(
            made.shpool,
            """#!/usr/bin/env bash
if [ "$1" = list ]; then sleep 300; fi
exit 0
""",
        )
        made.run(
            SESSION_KIT_NOTICE_CMD=str(made.notice),
            SESSION_KIT_WATCHDOG_MANAGER_TIMEOUT="2",
        )
        self.assertIn("manager-stuck", made.log_text())
        # The router saw one call, and it was the record sweep -- not the alert.
        calls = self.notice_calls(made)
        self.assertEqual(["sweep"], calls)

    def test_every_run_sweeps_the_records_the_picker_no_longer_shows(self) -> None:
        made = self.fixture(sessions=[session()])
        made.run(SESSION_KIT_NOTICE_CMD=str(made.notice))
        self.assertIn("sweep", " ".join(self.notice_calls(made)))

    def test_an_idle_pass_writes_no_notice_line(self) -> None:
        """Against the REAL router, because a stub is how this got shipped.

        The suppression test read `"routed": 0` followed by `"held": 0`, and
        the router sorts its keys, so the quiet case could never match: every
        pass of a 60-second watchdog wrote a line saying nothing had happened —
        1,440 a day. The branch's own test used a stub whose JSON omitted
        `held`, so it never exercised the branch it was guarding.
        """
        # Recent output, so the watchdog's own sweep records nothing and the
        # notice sweep genuinely has nothing to say.
        made = self.fixture(sessions=[session(recent_output_age_seconds=30)])
        made.run(
            SESSION_KIT_NOTICE_CMD=str(REPO / "bin" / "session_kit_notice"),
            SESSION_KIT_FLEET_DIR=str(made.base / "fleet"),
            SESSION_KIT_NOTICE_AWAY_CMD="",
        )
        self.assertNotIn("notice sweep", made.log_text())

    def test_a_pass_with_something_to_say_does_write_one(self) -> None:
        made = self.fixture(sessions=[session()])
        made.repairs.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "repairs": [
                        {
                            "at_unix_ms": int(time.time() * 1000),
                            "old_shpool_id": "s1", "new_shpool_id": "",
                            "title": "Some work", "provider": "codex",
                            "outcome": "reported", "acknowledged": False,
                            "reason": "quiet",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        made.run(
            SESSION_KIT_NOTICE_CMD=str(REPO / "bin" / "session_kit_notice"),
            SESSION_KIT_FLEET_DIR=str(made.base / "fleet"),
            SESSION_KIT_NOTICE_AWAY_CMD="",
        )
        # And with no away transport it says the loud thing, not the quiet one.
        self.assertIn("notice sweep DID NOT REACH THEM", made.log_text())

    def test_the_watchdog_sentinel_silences_the_router_too(self) -> None:
        made = self.fixture(sessions=[session()])
        made.sentinel.write_text("", encoding="utf-8")
        made.run(SESSION_KIT_NOTICE_CMD=str(made.notice))
        self.assertEqual([], self.notice_calls(made))


if __name__ == "__main__":
    unittest.main()
