from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time
import unittest

from tests.support import REPO


SP = REPO / "bin" / "sp"
MSG_MODULE = REPO / "lib" / "sh" / "sp_msg.sh"
CLAUDE_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CODEX_UUID = "11111111-2222-4333-8444-555555555555"


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def resolve_document(*, targets: list[dict] | None = None) -> dict:
    if targets is None:
        targets = [
            {
                "thread_key": f"claude:{CLAUDE_UUID}",
                "provider": "claude",
                "shpool_id": "main2",
                "uuid": CLAUDE_UUID,
                "terminal_number": 2,
                "title": "Fleet Rebuild",
                "agent_status": "idle",
            },
            {
                "thread_key": f"codex:{CODEX_UUID}",
                "provider": "codex",
                "shpool_id": "s20260807-120000-3",
                "uuid": CODEX_UUID,
                "terminal_number": 3,
                "title": "Ledger Work",
                "agent_status": "working",
            },
        ]
    claude = sum(1 for row in targets if row.get("provider") == "claude")
    codex = sum(1 for row in targets if row.get("provider") == "codex")
    return {
        "targets": targets,
        "counts": {"claude": claude, "codex": codex, "unreachable_hint": 0},
        "skipped": [
            {
                "shpool_id": "main9",
                "provider": "unknown",
                "reason": "managed shell row, not an agent",
            }
        ],
    }


def send_document() -> dict:
    return {
        "msg_id": "4f3a2b1c",
        "targets": [
            {
                "thread_key": f"claude:{CLAUDE_UUID}",
                "provider": "claude",
                "shpool_id": "main2",
                "uuid": CLAUDE_UUID,
                "terminal_number": 2,
                "title": "Fleet Rebuild",
                "method": "wake",
                "status": "delivered-woke",
                "detail": "transcript shows the envelope",
                "updated_unix_ms": 1_754_000_000_000,
            },
            {
                "thread_key": f"codex:{CODEX_UUID}",
                "provider": "codex",
                "shpool_id": "s20260807-120000-3",
                "uuid": CODEX_UUID,
                "terminal_number": 3,
                "title": "Ledger Work",
                "method": None,
                "status": "unreachable",
                "detail": "no app-server socket (plain-TUI Codex)",
                "updated_unix_ms": 1_754_000_000_000,
            },
        ],
    }


def report_document() -> dict:
    document = send_document()
    document.update(
        {
            "created_unix_ms": 1_754_000_000_000,
            "operator_text": "status in one line please",
            "fyi": False,
        }
    )
    document["targets"][0].update(
        {
            "status": "replied",
            "unread": True,
            "thread": [
                {
                    "ts_unix_ms": 1_754_000_000_000,
                    "dir": "out",
                    "msg_id": "4f3a2b1c",
                    "text": "status in one line please",
                    "via": "headless-send",
                },
                {
                    "ts_unix_ms": 1_754_000_060_000,
                    "dir": "in",
                    "msg_id": "4f3a2b1c",
                    "text": "ledger tests green",
                    "via": "sp msg reply",
                },
            ],
        }
    )
    return document


def nested_report_document() -> dict:
    """The shape the messaging core actually returns for `msg report`.

    The send record is nested under `send`, and each thread arrives in a map
    keyed by thread_key carrying its own unread flag.
    """
    record = send_document()
    record.update(
        {
            "created_unix_ms": 1_754_000_000_000,
            "operator_text": "status in one line please",
            "fyi": False,
        }
    )
    record["targets"][0]["status"] = "replied"
    key = f"claude:{CLAUDE_UUID}"
    return {
        "msg_id": "4f3a2b1c",
        "send": record,
        "threads": {
            key: {
                "unread": True,
                "lines": [
                    {
                        "ts_unix_ms": 1_754_000_000_000,
                        "dir": "out",
                        "msg_id": "4f3a2b1c",
                        "text": "status in one line please",
                        "via": "headless-send",
                    },
                    {
                        "ts_unix_ms": 1_754_000_060_000,
                        "dir": "in",
                        "msg_id": "4f3a2b1c",
                        "text": "ledger tests green",
                        "via": "sp msg reply",
                    },
                ],
            }
        },
        "unread_cleared": [key],
    }


def list_document() -> dict:
    return {
        "sends": [
            {
                "msg_id": "4f3a2b1c",
                "created_unix_ms": 1_754_000_000_000,
                "n_targets": 2,
                "n_replied": 1,
                "n_unreachable": 1,
                "preview": "status in one line please",
            }
        ]
    }


class MsgFixture:
    """Sandbox for the CLI layer only.

    The messaging core is a stub that answers the frozen `msg` verb contract
    and records its own argv. No session, socket, provider, or real ledger is
    touched, and every path the CLI reads is inside this disposable tree.
    """

    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".msg-cli-", dir=REPO)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.log = self.base / "core-calls.jsonl"
        self.resolve = self.base / "resolve.json"
        self.send = self.base / "send.json"
        self.report = self.base / "report.json"
        self.listing = self.base / "list.json"
        self.set_resolve(resolve_document())
        self.set_send(send_document())
        self.set_report(report_document())
        self.set_list(list_document())
        self.fake_shpool = self.base / "fake-shpool"
        write_executable(self.fake_shpool, "#!/usr/bin/env bash\nexit 0\n")
        self.fake_core = self.base / "fake-core"
        write_executable(
            self.fake_core,
            """#!/usr/bin/env python3
import json, os, pathlib, sys

args = sys.argv[1:]
with pathlib.Path(os.environ["STUB_MSG_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")


def answer(variable, path_variable):
    failure = os.environ.get(variable)
    if failure:
        sys.stderr.write(failure + "\\n")
        raise SystemExit(1)
    text = pathlib.Path(os.environ[path_variable]).read_text(encoding="utf-8")
    sys.stdout.write(text)
    raise SystemExit(0)


if args[:2] == ["msg", "resolve"]:
    answer("STUB_MSG_RESOLVE_FAIL", "STUB_MSG_RESOLVE")
if args[:2] == ["msg", "send"]:
    answer("STUB_MSG_SEND_FAIL", "STUB_MSG_SEND")
if args[:2] == ["msg", "report"]:
    answer("STUB_MSG_REPORT_FAIL", "STUB_MSG_REPORT")
if args[:2] == ["msg", "list"]:
    answer("STUB_MSG_LIST_FAIL", "STUB_MSG_LIST")
if args[:2] == ["msg", "intake"]:
    print(json.dumps({"count": 0, "open": []}, sort_keys=True))
    raise SystemExit(0)
if args[:2] == ["msg", "reply"]:
    reason = os.environ.get("STUB_MSG_REPLY_FAIL")
    if reason:
        sys.stderr.write(reason + "\\n")
        raise SystemExit(1)
    print(json.dumps({"msg_id": args[2], "status": "replied"}, sort_keys=True))
    raise SystemExit(0)
sys.stderr.write("unexpected core call: " + json.dumps(args) + "\\n")
raise SystemExit(2)
""",
        )

    def close(self) -> None:
        self.temp.cleanup()

    def set_resolve(self, document: dict) -> None:
        self.resolve.write_text(json.dumps(document) + "\n", encoding="utf-8")

    def set_send(self, document: dict) -> None:
        self.send.write_text(json.dumps(document) + "\n", encoding="utf-8")

    def set_report(self, document: dict) -> None:
        self.report.write_text(json.dumps(document) + "\n", encoding="utf-8")

    def set_list(self, document: dict) -> None:
        self.listing.write_text(json.dumps(document) + "\n", encoding="utf-8")

    def env(self, **overrides: str) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SESSION_KIT_SHPOOL_CMD": str(self.fake_shpool),
                "SESSION_KIT_INVENTORY_CORE": str(self.fake_core),
                "SESSION_KIT_NONINTERACTIVE": "1",
                "SESSION_KIT_NO_COLOR": "1",
                "STUB_MSG_LOG": str(self.log),
                "STUB_MSG_RESOLVE": str(self.resolve),
                "STUB_MSG_SEND": str(self.send),
                "STUB_MSG_REPORT": str(self.report),
                "STUB_MSG_LIST": str(self.listing),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        environment.update(overrides)
        return environment

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def sp(self, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SP), *args],
            cwd=REPO,
            env=self.env(**overrides),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )


class MsgCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = MsgFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def sent_calls(self) -> list[list[str]]:
        return [call for call in self.fixture.calls() if call[:2] == ["msg", "send"]]

    def test_usage_documents_every_msg_verb(self) -> None:
        """bin/sp's usage and the module's own usage must not drift apart."""
        module_usage = subprocess.run(
            ["bash", "-c", f'source "{MSG_MODULE}"; msg_usage'],
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, module_usage.returncode, module_usage.stderr)
        lines = [line for line in module_usage.stdout.splitlines() if line.strip()]
        self.assertEqual(4, len(lines))
        help_text = self.fixture.sp("help")
        self.assertEqual(0, help_text.returncode, help_text.stderr)
        for line in lines:
            self.assertIn(line, help_text.stdout)

    def test_single_target_sends_without_asking_for_a_confirmation(self) -> None:
        completed = self.fixture.sp("msg", "2", "status please")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("Confirm?", completed.stdout)
        self.assertIn("Targets: 2", completed.stdout)
        self.assertIn("delivered-woke", completed.stdout)
        self.assertIn("sp msg report 4f3a2b1c", completed.stdout)
        self.assertEqual(
            [["msg", "resolve", "--target", "2"], ["msg", "send", "--target", "2", "--text", "status please"]],
            self.fixture.calls(),
        )

    def test_no_receipt_or_preview_ever_prints_a_raw_session_id(self) -> None:
        """Every msg table names sessions by number, provider and title."""
        completed = self.fixture.sp("msg", "2", "status please")
        self.assertEqual(0, completed.returncode, completed.stderr)
        for leaked in ("main2", "main9", "s20260807-120000-3", CLAUDE_UUID, CODEX_UUID):
            self.assertNotIn(leaked, completed.stdout)
            self.assertNotIn(leaked, completed.stderr)
        # What replaced them is still enough to tell the sessions apart.
        self.assertIn("#2", completed.stdout)
        self.assertIn("Fleet Rebuild", completed.stdout)

    def test_mass_send_without_a_terminal_refuses_instead_of_broadcasting(self) -> None:
        completed = self.fixture.sp("msg", "all", "status please")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("--yes", completed.stderr)
        self.assertEqual([], self.sent_calls())

    def test_mass_send_with_yes_reports_counts_and_delivers(self) -> None:
        completed = self.fixture.sp("msg", "all", "status please", "--yes")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("claude 1", completed.stdout)
        self.assertIn("codex 1", completed.stdout)
        self.assertIn("Sent 4f3a2b1c to 2 target(s)", completed.stdout)
        self.assertIn("no app-server socket", completed.stdout)
        self.assertEqual(
            [["msg", "send", "--target", "all", "--text", "status please"]],
            self.sent_calls(),
        )

    def test_skipped_rows_are_named_rather_than_silently_dropped(self) -> None:
        completed = self.fixture.sp("msg", "2", "status please")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Skipped 1 row(s)", completed.stdout)
        self.assertIn("managed shell row", completed.stdout)

    def test_target_status_column_appears_only_when_the_core_reports_one(self) -> None:
        with_status = self.fixture.sp("msg", "2", "status please")
        self.assertEqual(0, with_status.returncode, with_status.stderr)
        self.assertIn("status", with_status.stdout.splitlines()[1])
        self.assertIn("idle", with_status.stdout)

        quiet = resolve_document()
        for row in quiet["targets"]:
            row.pop("agent_status")
        self.fixture.set_resolve(quiet)
        without_status = self.fixture.sp("msg", "2", "status please")
        self.assertEqual(0, without_status.returncode, without_status.stderr)
        header = without_status.stdout.splitlines()[1]
        self.assertNotIn("status", header)
        self.assertIn("title", header)
        self.assertIn("Fleet Rebuild", without_status.stdout)

    def test_fyi_flag_reaches_the_core(self) -> None:
        completed = self.fixture.sp("msg", "idle", "no reply needed", "--fyi", "--yes")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            [
                [
                    "msg",
                    "send",
                    "--target",
                    "idle",
                    "--text",
                    "no reply needed",
                    "--fyi",
                ]
            ],
            self.sent_calls(),
        )

    def test_an_idempotency_key_reaches_the_core_only_when_one_is_set(self) -> None:
        """A person writing a message wants a new one; a retrying script does
        not, and says so for the one command."""
        plain = self.fixture.sp("msg", "2", "status please")
        self.assertEqual(0, plain.returncode, plain.stderr)
        self.assertNotIn("--key", self.sent_calls()[0])

        keyed = self.fixture.sp(
            "msg",
            "2",
            "status please",
            SESSION_KIT_MSG_KEY="supervisor-brief:main2",
        )
        self.assertEqual(0, keyed.returncode, keyed.stderr)
        self.assertEqual(
            [
                "msg",
                "send",
                "--target",
                "2",
                "--text",
                "status please",
                "--key",
                "supervisor-brief:main2",
            ],
            self.sent_calls()[1],
        )

    def test_a_landed_but_unconfirmed_target_counts_as_neither(self) -> None:
        """Counting it as delivered claims proof; counting it as undelivered
        invites a duplicate."""
        document = send_document()
        document["targets"][1].update(
            {
                "status": "landed-unconfirmed",
                "detail": "the sender delivered it to this target",
            }
        )
        self.fixture.set_send(document)
        completed = self.fixture.sp("msg", "2", "status please")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("delivered 1", completed.stdout)
        self.assertIn("not delivered 0", completed.stdout)
        self.assertIn("1 landed, unconfirmed", completed.stdout)
        self.assertIn("landed-unconfirmed", completed.stdout)

    def test_text_that_looks_like_a_flag_survives_the_end_of_options(self) -> None:
        completed = self.fixture.sp("msg", "--", "2", "--fyi is the topic")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            [["msg", "send", "--target", "2", "--text", "--fyi is the topic"]],
            self.sent_calls(),
        )

    def test_empty_mass_resolution_sends_nothing_and_is_not_an_error(self) -> None:
        self.fixture.set_resolve(resolve_document(targets=[]))
        completed = self.fixture.sp("msg", "all", "status please", "--yes")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("nothing was sent", completed.stdout)
        self.assertEqual([], self.sent_calls())

    def test_a_named_target_that_resolves_to_nothing_fails(self) -> None:
        self.fixture.set_resolve(resolve_document(targets=[]))
        completed = self.fixture.sp("msg", "7", "status please")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("no live Claude or Codex session matched", completed.stderr)
        self.assertEqual([], self.sent_calls())

    def test_kill_switch_stops_the_command_before_it_reads_anything(self) -> None:
        completed = self.fixture.sp(
            "msg", "all", "status please", "--yes", SESSION_KIT_MSG="0"
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("SESSION_KIT_MSG=0", completed.stderr)
        self.assertEqual([], self.fixture.calls())

    def test_a_failed_resolve_never_reaches_the_send(self) -> None:
        completed = self.fixture.sp(
            "msg", "2", "status please", STUB_MSG_RESOLVE_FAIL="inventory unavailable"
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("could not resolve message targets", completed.stderr)
        self.assertEqual([], self.sent_calls())

    def test_a_failed_send_warns_against_a_blind_repeat(self) -> None:
        completed = self.fixture.sp(
            "msg", "2", "status please", STUB_MSG_SEND_FAIL="socket refused"
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("sp msg list", completed.stderr)
        self.assertEqual(1, len(self.sent_calls()))

    def test_target_titles_can_never_write_escape_sequences_to_the_terminal(
        self,
    ) -> None:
        hostile = resolve_document()
        hostile["targets"][0]["title"] = "red \x1b[31mALERT\x1b[0m\nsecond line"
        self.fixture.set_resolve(hostile)
        completed = self.fixture.sp("msg", "2", "status please")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("\x1b", completed.stdout)
        self.assertIn("red [31mALERT", completed.stdout.replace("\x1b", ""))
        self.assertIn("red", completed.stdout)

    def test_report_defaults_to_the_newest_send_and_renders_its_thread(self) -> None:
        completed = self.fixture.sp("msg", "report")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Message 4f3a2b1c", completed.stdout)
        self.assertIn("status in one line please", completed.stdout)
        self.assertIn("ledger tests green", completed.stdout)
        self.assertIn("new reply", completed.stdout)
        self.assertIn("sp msg reply 4f3a2b1c", completed.stdout)
        # Reading a report no longer clears anything by itself, so a view that
        # printed an unread reply asks the core to clear that thread. Nothing
        # else may follow the report.
        self.assertEqual(["msg", "report"], self.fixture.calls()[0])
        self.assertEqual(
            [["msg", "report"], ["msg", "mark-read"]],
            [call[:2] for call in self.fixture.calls()],
        )

    def test_report_passes_an_explicit_id_through(self) -> None:
        completed = self.fixture.sp("msg", "report", "4f3a2b1c")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            ["msg", "report", "--id", "4f3a2b1c"], self.fixture.calls()[0]
        )

    def test_report_renders_the_cores_nested_send_and_thread_map(self) -> None:
        self.fixture.set_report(nested_report_document())
        completed = self.fixture.sp("msg", "report")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Message 4f3a2b1c", completed.stdout)
        self.assertIn("2 target(s)", completed.stdout)
        self.assertIn("1 replied", completed.stdout)
        self.assertIn("status in one line please", completed.stdout)
        self.assertIn("ledger tests green", completed.stdout)
        self.assertIn("new reply", completed.stdout)

    def test_report_says_so_when_nothing_has_been_sent(self) -> None:
        self.fixture.set_report({})
        completed = self.fixture.sp("msg", "report")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("No message has been sent yet.", completed.stdout)

    def test_report_thread_text_is_scrubbed_of_control_bytes(self) -> None:
        hostile = report_document()
        hostile["targets"][0]["thread"][1]["text"] = "done \x1b[2J\x1b[H wiped"
        self.fixture.set_report(hostile)
        completed = self.fixture.sp("msg", "report")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("\x1b", completed.stdout)

    def test_intake_reaches_the_core_instead_of_addressing_a_session(self) -> None:
        """`intake` is a verb, not a target; the word must never reach the sender."""
        completed = self.fixture.sp("msg", "intake", "open")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn('"count": 0', completed.stdout)
        self.assertEqual([["msg", "intake", "open"]], self.fixture.calls())
        self.assertEqual([], self.sent_calls())

    def test_intake_flush_reaches_the_core_as_a_real_verb(self) -> None:
        completed = self.fixture.sp("msg", "intake", "flush")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([["msg", "intake", "flush"]], self.fixture.calls())
        self.assertEqual([], self.sent_calls())

    def test_list_renders_one_line_per_send(self) -> None:
        completed = self.fixture.sp("msg", "list")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("4f3a2b1c", completed.stdout)
        self.assertIn("status in one line please", completed.stdout)
        self.assertEqual([["msg", "list"]], self.fixture.calls())

    def test_list_says_so_when_the_ledger_is_empty(self) -> None:
        self.fixture.set_list({"sends": []})
        completed = self.fixture.sp("msg", "list")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("No messages have been sent yet.", completed.stdout)

    def test_reply_is_a_pass_through_to_the_core(self) -> None:
        completed = self.fixture.sp("msg", "reply", "4f3a2b1c", "ledger tests green")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            [["msg", "reply", "4f3a2b1c", "--text", "ledger tests green"]],
            self.fixture.calls(),
        )
        self.assertIn('"status": "replied"', completed.stdout)

    def test_reply_keeps_the_cores_refusal_and_its_reason(self) -> None:
        completed = self.fixture.sp(
            "msg",
            "reply",
            "deadbeef",
            "answer",
            STUB_MSG_REPLY_FAIL="caller is not a target of deadbeef",
        )
        self.assertEqual(1, completed.returncode)
        self.assertIn("caller is not a target of deadbeef", completed.stderr)

    def test_argument_mistakes_are_usage_errors_that_send_nothing(self) -> None:
        for args in (
            ("msg",),
            ("msg", "2"),
            ("msg", "2", "text", "extra"),
            ("msg", "2", "text", "--unknown"),
            ("msg", "report", "a", "b"),
            ("msg", "list", "extra"),
            ("msg", "reply", "4f3a2b1c"),
        ):
            with self.subTest(args=args):
                completed = self.fixture.sp(*args)
                self.assertEqual(2, completed.returncode, completed.stdout)
                self.assertIn("sp msg", completed.stderr)
        self.assertEqual([], self.fixture.calls())


class MsgConfirmTerminalTests(unittest.TestCase):
    """The mass confirm on a real terminal, including the drained Enter.

    A single-character read leaves the human's Enter in the terminal buffer.
    The picker's next read consumed exactly that leftover once and silently
    ended the picker after every kill, so the regression is pinned here for
    the send confirm too.
    """

    def setUp(self) -> None:
        self.fixture = MsgFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def drive(self, keys: bytes) -> str:
        import pty

        script = (
            f'"{SP}" msg all "status please"\n'
            'printf "STATUS=%s\\n" "$?"\n'
            "IFS= read -r next\n"
            'printf "NEXT=[%s]\\n" "$next"\n'
        )
        environment = self.fixture.env(SESSION_KIT_NONINTERACTIVE="0")
        pid, descriptor = pty.fork()
        if pid == 0:  # pragma: no cover - the child execs immediately
            # A child that cannot exec must never return: it is a forked copy
            # of the test runner, and returning resumes the suite from here.
            try:
                os.chdir(REPO)
                os.execvpe("bash", ["bash", "-c", script], environment)
            finally:
                os._exit(127)
        os.write(descriptor, keys)
        output = bytearray()
        probed = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready, _, _ = select.select([descriptor], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            if not probed and b"STATUS=" in output:
                # sp has exited and the caller is at its own read. A newline
                # left over from the confirm would be consumed as an empty
                # answer here — before this probe is ever typed.
                time.sleep(0.5)
                os.write(descriptor, b"PROBE\n")
                probed = True
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                time.sleep(0.2)
                while True:
                    ready, _, _ = select.select([descriptor], [], [], 0)
                    if not ready:
                        break
                    try:
                        chunk = os.read(descriptor, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                break
        os.close(descriptor)
        return output.decode("utf-8", "replace")

    def test_y_confirms_the_broadcast_and_drains_the_enter(self) -> None:
        text = self.drive(b"y\nPROBE\n")
        self.assertIn("Confirm?", text)
        self.assertIn("Sent 4f3a2b1c", text)
        self.assertIn("STATUS=0", text)
        self.assertIn("NEXT=[PROBE]", text)
        self.assertEqual(
            [["msg", "send", "--target", "all", "--text", "status please"]],
            [call for call in self.fixture.calls() if call[:2] == ["msg", "send"]],
        )

    def test_anything_but_y_cancels_and_delivers_nothing(self) -> None:
        text = self.drive(b"n\nPROBE\n")
        self.assertIn("Send cancelled", text)
        self.assertNotIn("Sent 4f3a2b1c", text)
        self.assertIn("NEXT=[PROBE]", text)
        self.assertEqual(
            [], [call for call in self.fixture.calls() if call[:2] == ["msg", "send"]]
        )


class MsgSelectionGrammarTests(unittest.TestCase):
    """`sp msg` takes the same numbers, ranges and `all` the picker takes."""

    def setUp(self) -> None:
        import sys

        library = os.fspath(REPO / "lib")
        if library not in sys.path:
            sys.path.insert(0, library)
        from sessionkit_messages import verbs
        from sessionkit_messages.envelope import MessageError

        self.verbs = verbs
        self.error = MessageError

    def _inventory(self, *numbers: int) -> dict:
        return {
            "sessions": [
                {
                    "provider": "claude" if number % 2 else "codex",
                    "identity": {"uuid": f"00000000-0000-4000-8000-{number:012d}"},
                    "shpool_id": f"s-{number}",
                    "shpool_id_raw": f"s-{number}",
                    "terminal_number": number,
                    "title": f"Session {number}",
                    "agent_status": "idle",
                }
                for number in numbers
            ]
        }

    def _numbers(self, selector: str, *present: int) -> list[int]:
        targets, _ = self.verbs.select_rows(self._inventory(*present), selector)
        return [row["terminal_number"] for row in targets]

    def test_commas_ranges_and_all_choose_the_same_sessions_everywhere(self) -> None:
        present = (2, 3, 4, 5, 9)
        for selector, expected in (
            ("3", [3]),
            ("2,5", [2, 5]),
            (" 2 , 5 ", [2, 5]),
            ("3-5", [3, 4, 5]),
            ("9,3-4,2", [2, 3, 4, 9]),
            ("2 5", [2, 5]),
            ("3-3", [3]),
            ("2,2,2", [2]),
            ("all", [2, 3, 4, 5, 9]),
            ("a", [2, 3, 4, 5, 9]),
            ("ALL", [2, 3, 4, 5, 9]),
        ):
            with self.subTest(selector=selector):
                self.assertEqual(expected, self._numbers(selector, *present))

    def test_an_invalid_selection_is_refused_without_sending_anything(self) -> None:
        present = (2, 3, 4, 5, 9)
        for selector in (
            "5-2",
            "3-",
            "-3",
            "1,,2",
            "2,",
            "0",
            "2-0",
            "1-99999",
            "٣",
            "²",
            "2-x",
            "nope",
            "",
        ):
            with self.subTest(selector=selector):
                with self.assertRaises(self.error):
                    self._numbers(selector, *present)

    def test_a_number_nothing_is_using_is_named_rather_than_ignored(self) -> None:
        with self.assertRaisesRegex(self.error, "no live session is numbered 6, 7"):
            self._numbers("5-7", 2, 3, 4, 5, 9)
        # `all` is not a claim about which numbers exist, so it never says this.
        self.assertEqual([2, 9], self._numbers("all", 2, 9))


if __name__ == "__main__":
    unittest.main()
