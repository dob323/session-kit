"""Both delivery paths, against fakes only.

The Codex path talks to a kit-local fake app server over a disposable Unix
socket; the Claude path talks to a stub `claude` script. No test in this file
opens a real provider socket, runs a real provider, or reads a real home.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_messages import claude_send, codex_send  # noqa: E402
from sessionkit_messages.codex_send import (  # noqa: E402
    AppServerClient,
    AppServerError,
    active_turn_id,
    live_socket,
    loaded_thread_ids,
    socket_path,
    thread_status,
)

UUID = "019fdf1e-8b4c-7573-a089-be495bfece6a"
OTHER_UUID = "dcbdf940-4eda-4967-8e41-23a5760c32b5"
ENVELOPE = "[session-kit operator message abcd1234]\n---\nStatus?"


def read_exact(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def read_client_frame(connection: socket.socket) -> dict:
    first, second = read_exact(connection, 2)
    if first & 0x0F == 8:
        raise EOFError
    if first & 0x0F != 1 or not second & 0x80:
        raise ValueError("expected a masked text frame")
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", read_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(connection, 8))[0]
    mask = read_exact(connection, 4)
    payload = read_exact(connection, length)
    return json.loads(bytes(v ^ mask[i % 4] for i, v in enumerate(payload)))


def send_server_frame(connection: socket.socket, payload: dict) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    if len(encoded) < 126:
        header = bytes((0x81, len(encoded)))
    else:
        header = bytes((0x81, 126)) + struct.pack("!H", len(encoded))
    connection.sendall(header + encoded)


class FakeAppServer(threading.Thread):
    """A scriptable stand-in for `codex app-server` on a disposable socket."""

    def __init__(
        self,
        path: Path,
        *,
        loaded: tuple[str, ...] = (UUID,),
        statuses: tuple[str, ...] = ("idle",),
        turn_id: str = "019fdf22-6d71-7a70-bda6-d22939a7579a",
        turn_status: str = "inProgress",
        steer_errors: int = 0,
        start_error: str | None = None,
        bad_handshake: bool = False,
    ):
        super().__init__(daemon=True)
        self.path = path
        self.loaded = list(loaded)
        self.statuses = list(statuses)
        self.turn_id = turn_id
        self.turn_status = turn_status
        self.steer_errors = steer_errors
        self.start_error = start_error
        self.bad_handshake = bad_handshake
        self.ready = threading.Event()
        self.turn_starts: list[dict] = []
        self.steers: list[dict] = []
        self.notifications: list[str] = []
        self.error: Exception | None = None
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    def _next_status(self) -> str:
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0] if self.statuses else "idle"

    def run(self) -> None:
        try:
            self._server.bind(os.fspath(self.path))
            self._server.listen(1)
            self.ready.set()
            connection, _ = self._server.accept()
            with connection:
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    chunk = connection.recv(4096)
                    if not chunk:
                        return
                    request.extend(chunk)
                key = next(
                    line.split(":", 1)[1].strip()
                    for line in request.decode("ascii").split("\r\n")
                    if line.lower().startswith("sec-websocket-key:")
                )
                accept = base64.b64encode(
                    hashlib.sha1(
                        (
                            ("wrong" if self.bad_handshake else key)
                            + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                        ).encode()
                    ).digest()
                ).decode()
                connection.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                while True:
                    try:
                        message = read_client_frame(connection)
                    except (EOFError, OSError, ValueError):
                        return
                    method = str(message.get("method"))
                    request_id = message.get("id")
                    if request_id is None:
                        self.notifications.append(method)
                        continue
                    error: dict | None = None
                    result: dict = {}
                    if method == "initialize":
                        result = {"userAgent": "fake"}
                    elif method == "thread/loaded/list":
                        result = {"data": list(self.loaded)}
                    elif method == "thread/read":
                        thread: dict = {
                            "id": UUID,
                            "status": {"type": self._next_status()},
                        }
                        if message["params"].get("includeTurns"):
                            thread["turns"] = (
                                [{"id": self.turn_id, "status": self.turn_status}]
                                if self.turn_id
                                else []
                            )
                        result = {"thread": thread}
                    elif method == "turn/start":
                        self.turn_starts.append(message["params"])
                        if self.start_error:
                            error = {"code": -32600, "message": self.start_error}
                        else:
                            result = {
                                "turn": {"id": self.turn_id, "status": "inProgress"}
                            }
                    elif method == "turn/steer":
                        self.steers.append(message["params"])
                        if self.steer_errors > 0:
                            self.steer_errors -= 1
                            error = {
                                "code": -32600,
                                "message": "no active turn to steer",
                            }
                    else:
                        error = {"code": -32601, "message": f"unknown {method}"}
                    body: dict = {"id": request_id}
                    if error is None:
                        body["result"] = result
                    else:
                        body["error"] = error
                    send_server_frame(connection, body)
        except Exception as exc:  # noqa: BLE001 - surfaced to the test
            self.error = exc
        finally:
            self._server.close()


class CodexSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".session-kit-msgsock-", dir="/tmp"
        )
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        (self.state / "app-server" / "s-test").mkdir(parents=True)
        self.socket_path = self.state / "app-server" / "s-test" / "app.sock"
        self.servers: list[FakeAppServer] = []

    def tearDown(self) -> None:
        for server in self.servers:
            server.join(timeout=2)
        self.temporary.cleanup()

    def start(self, **kwargs: object) -> FakeAppServer:
        server = FakeAppServer(self.socket_path, **kwargs)  # type: ignore[arg-type]
        self.servers.append(server)
        server.start()
        self.assertTrue(server.ready.wait(3))
        return server

    def deliver(self, uuid: str = UUID, **kwargs: object) -> dict:
        return codex_send.deliver(
            uuid=uuid,
            envelope=ENVELOPE,
            path=self.socket_path,
            sleep=lambda _seconds: None,
            **kwargs,  # type: ignore[arg-type]
        )

    # ---- path safety ---------------------------------------------------

    def test_only_this_target_socket_is_ever_addressed(self) -> None:
        self.assertEqual(
            self.state / "app-server" / "s-test" / "app.sock",
            socket_path(self.state, "s-test"),
        )
        for unsafe in ("../other", "/abs", ".hidden", "", None, 7, "x" * 200):
            self.assertIsNone(socket_path(self.state, unsafe))

    def test_a_missing_socket_reports_a_plain_tui_codex(self) -> None:
        outcome = codex_send.deliver(
            uuid=UUID, envelope=ENVELOPE, path=self.state / "nope" / "app.sock"
        )
        self.assertEqual("unreachable", outcome["status"])
        self.assertEqual(codex_send.NO_SOCKET_DETAIL, outcome["detail"])
        self.assertIsNone(outcome["method"])

    def test_a_regular_file_is_not_a_socket(self) -> None:
        plain = self.base / "not-a-socket"
        plain.write_text("", encoding="utf-8")
        self.assertFalse(live_socket(plain))
        self.assertFalse(live_socket(None))

    def test_a_dead_socket_is_unreachable_not_failed(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(os.fspath(self.socket_path))
        server.close()
        outcome = codex_send.deliver(uuid=UUID, envelope=ENVELOPE, path=self.socket_path)
        self.assertEqual("unreachable", outcome["status"])
        self.assertIn(codex_send.NO_SOCKET_DETAIL, str(outcome["detail"]))

    def test_a_forged_handshake_is_refused(self) -> None:
        self.start(bad_handshake=True)
        with self.assertRaises(AppServerError):
            AppServerClient(self.socket_path)

    def test_oversized_outbound_frames_are_refused(self) -> None:
        self.start()
        client = AppServerClient(self.socket_path)
        try:
            with self.assertRaises(AppServerError):
                client.send_frame(b"x" * (codex_send.MAX_FRAME + 1))
        finally:
            client.close()

    # ---- delivery ------------------------------------------------------

    def test_an_idle_thread_is_woken_by_turn_start(self) -> None:
        server = self.start(statuses=("idle",))
        outcome = self.deliver()
        self.assertEqual("delivered-woke", outcome["status"])
        self.assertEqual("wake", outcome["method"])
        self.assertEqual(1, len(server.turn_starts))
        self.assertEqual(ENVELOPE, server.turn_starts[0]["input"][0]["text"])
        self.assertEqual(UUID, server.turn_starts[0]["threadId"])
        self.assertIn("initialized", server.notifications)

    def test_an_active_thread_is_steered_with_the_expected_turn_id(self) -> None:
        server = self.start(statuses=("active",))
        outcome = self.deliver()
        self.assertEqual("delivered-midturn", outcome["status"])
        self.assertEqual("steer", outcome["method"])
        self.assertEqual(1, len(server.steers))
        self.assertEqual(
            "019fdf22-6d71-7a70-bda6-d22939a7579a",
            server.steers[0]["expectedTurnId"],
        )
        self.assertEqual(ENVELOPE, server.steers[0]["input"][0]["text"])
        self.assertEqual([], server.turn_starts)

    def test_the_no_active_turn_race_is_retried_once(self) -> None:
        server = self.start(statuses=("active",), steer_errors=1)
        outcome = self.deliver()
        self.assertEqual("delivered-midturn", outcome["status"])
        self.assertEqual(2, len(server.steers))

    def test_a_steer_that_keeps_failing_falls_back_to_an_idle_start(self) -> None:
        server = self.start(
            statuses=("active", "active", "active", "active", "idle"),
            steer_errors=5,
        )
        outcome = self.deliver()
        self.assertEqual("delivered-woke", outcome["status"])
        self.assertEqual(2, len(server.steers))
        self.assertEqual(1, len(server.turn_starts))

    def test_a_thread_that_never_goes_idle_fails_with_the_exact_error(self) -> None:
        server = self.start(statuses=("active",), steer_errors=9)
        outcome = self.deliver(idle_fallback_seconds=0.0)
        self.assertEqual("failed", outcome["status"])
        self.assertEqual("steer", outcome["method"])
        self.assertIn("no active turn to steer", str(outcome["detail"]))
        self.assertEqual([], server.turn_starts)

    def test_a_thread_absent_from_its_socket_is_unreachable(self) -> None:
        self.start(loaded=(OTHER_UUID,))
        outcome = self.deliver()
        self.assertEqual("unreachable", outcome["status"])
        self.assertEqual(codex_send.NOT_LOADED_DETAIL, outcome["detail"])

    def test_a_refused_turn_start_is_failed_with_the_server_error(self) -> None:
        self.start(statuses=("idle",), start_error="model unavailable")
        outcome = self.deliver()
        self.assertEqual("failed", outcome["status"])
        self.assertIn("model unavailable", str(outcome["detail"]))

    def test_never_waits_for_a_turn_completed_event(self) -> None:
        """Phase 0 proved turn/completed never arrives; delivery must not wait."""
        source = (REPO / "lib/sessionkit_messages/codex_send.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('"turn/completed"', source)

    # ---- payload readers ------------------------------------------------

    def test_loaded_thread_ids_reads_both_shapes(self) -> None:
        self.assertEqual({UUID}, loaded_thread_ids({"data": [UUID.upper()]}))
        self.assertEqual({UUID}, loaded_thread_ids({"threads": [{"id": UUID}]}))
        self.assertEqual(set(), loaded_thread_ids({}))

    def test_status_and_turn_readers_tolerate_junk(self) -> None:
        self.assertEqual("", thread_status({"thread": "nope"}))
        self.assertEqual("idle", thread_status({"thread": {"status": {"type": "idle"}}}))
        self.assertEqual("", active_turn_id({"thread": {"turns": "nope"}}))
        self.assertEqual(
            "t2",
            active_turn_id(
                {
                    "thread": {
                        "turns": [
                            {"id": "t0", "status": "completed"},
                            {"id": "t2", "status": "inProgress"},
                        ]
                    }
                }
            ),
        )


class ClaudeRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".session-kit-msgclaude-", dir=REPO
        )
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_registry(self, payload: object) -> Path:
        path = self.base / "agents.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def stub_claude(self, stdout: str, exit_code: int = 0) -> Path:
        path = self.base / "claude-stub"
        path.write_text(
            "#!/usr/bin/env bash\n"
            f"cat <<'STUB_EOF'\n{stdout}\nSTUB_EOF\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def test_registry_reads_the_fixture_hook(self) -> None:
        path = self.write_registry(
            [{"sessionId": UUID, "name": "v2-ec", "cwd": "/srv/example"}]
        )
        entries = claude_send.registry_entries(
            {"SESSION_KIT_TESTING": "1", "SESSION_KIT_CLAUDE_JSON_FILE": str(path)}
        )
        index = claude_send.registry_index(entries)
        self.assertEqual(
            {"name": "v2-ec", "cwd": "/srv/example"}, index[UUID]
        )

    def test_registry_runs_the_configured_command_with_a_stub_binary(self) -> None:
        stub = self.stub_claude(json.dumps([{"sessionId": UUID, "name": "v2-ec"}]))
        entries = claude_send.registry_entries({"SESSION_KIT_CLAUDE_CMD": str(stub)})
        self.assertEqual([UUID], sorted(claude_send.registry_index(entries)))

    def test_a_failing_or_unparsable_registry_is_empty_not_fatal(self) -> None:
        broken = self.stub_claude("not json", exit_code=0)
        self.assertEqual(
            [], claude_send.registry_entries({"SESSION_KIT_CLAUDE_CMD": str(broken)})
        )
        failing = self.stub_claude("[]", exit_code=3)
        self.assertEqual(
            [], claude_send.registry_entries({"SESSION_KIT_CLAUDE_CMD": str(failing)})
        )
        missing = self.base / "no-such-binary"
        self.assertEqual(
            [], claude_send.registry_entries({"SESSION_KIT_CLAUDE_CMD": str(missing)})
        )

    def test_a_registry_row_without_a_session_id_is_ignored(self) -> None:
        path = self.write_registry([{"name": "nameless"}, "junk", {"sessionId": UUID}])
        index = claude_send.registry_index(
            claude_send.registry_entries(
                {"SESSION_KIT_TESTING": "1", "SESSION_KIT_CLAUDE_JSON_FILE": str(path)}
            )
        )
        self.assertEqual([UUID], sorted(index))

    # ---- transcript receipt ---------------------------------------------

    def write_transcript(self, cwd: str, uuid: str, lines: list[object]) -> Path:
        directory = self.home / ".claude" / "projects" / claude_send.project_dir_name(cwd)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid}.jsonl"
        path.write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
        return path

    def test_project_dir_name_matches_claude_layout(self) -> None:
        self.assertEqual(
            "-srv-example", claude_send.project_dir_name("/srv/example")
        )
        self.assertEqual("", claude_send.project_dir_name(""))

    def test_transcript_is_found_by_derived_dir_then_by_search(self) -> None:
        self.write_transcript("/srv/example", UUID, [{"sessionId": UUID}])
        self.assertIsNotNone(
            claude_send.transcript_path(self.home, "/srv/example", UUID)
        )
        self.assertIsNotNone(claude_send.transcript_path(self.home, "/moved/away", UUID))
        self.assertIsNone(claude_send.transcript_path(self.home, "/x", OTHER_UUID))

    def test_the_receipt_is_the_id_in_the_targets_own_transcript(self) -> None:
        path = self.write_transcript(
            "/srv/example",
            UUID,
            [
                {"sessionId": UUID, "type": "user", "text": "unrelated"},
                {
                    "sessionId": UUID,
                    "type": "user",
                    "text": "[session-kit operator message abcd1234]",
                },
            ],
        )
        self.assertTrue(claude_send.transcript_has_message(path, "abcd1234", UUID))
        self.assertFalse(claude_send.transcript_has_message(path, "beef5678", UUID))
        self.assertFalse(claude_send.transcript_has_message(None, "abcd1234", UUID))

    def test_a_receipt_in_another_sessions_record_does_not_count(self) -> None:
        path = self.write_transcript(
            "/srv/example",
            UUID,
            [{"sessionId": OTHER_UUID, "text": "abcd1234"}],
        )
        self.assertFalse(claude_send.transcript_has_message(path, "abcd1234", UUID))

    def test_verification_polls_until_the_receipt_lands(self) -> None:
        target = {
            "thread_key": f"claude:{UUID}",
            "uuid": UUID,
            "cwd": "/srv/example",
            "display_name": "v2-ec",
        }
        self.write_transcript("/srv/example", UUID, [{"sessionId": UUID}])
        ticks = {"n": 0}

        def sleep(_seconds: float) -> None:
            ticks["n"] += 1
            if ticks["n"] == 2:
                self.write_transcript(
                    "/srv/example",
                    UUID,
                    [{"sessionId": UUID, "text": "abcd1234"}],
                )

        clock = {"t": 0.0}

        def monotonic() -> float:
            clock["t"] += 0.5
            return clock["t"]

        found = claude_send.verify_delivery(
            [target],
            msg_id="abcd1234",
            home=self.home,
            deadline_seconds=30.0,
            sleep=sleep,
            monotonic=monotonic,
        )
        self.assertTrue(found[f"claude:{UUID}"])

    def test_verification_gives_up_when_the_window_closes(self) -> None:
        target = {
            "thread_key": f"claude:{UUID}",
            "uuid": UUID,
            "cwd": "/srv/example",
            "display_name": "v2-ec",
        }
        self.write_transcript("/srv/example", UUID, [{"sessionId": UUID}])
        found = claude_send.verify_delivery(
            [target],
            msg_id="abcd1234",
            home=self.home,
            deadline_seconds=0.0,
            sleep=lambda _seconds: None,
            monotonic=lambda: 1.0,
        )
        self.assertFalse(found[f"claude:{UUID}"])

    # ---- the headless sender --------------------------------------------

    def test_instructions_name_every_target_and_forbid_the_rest(self) -> None:
        text = claude_send.compose_instructions(
            [{"display_name": "v2-ec"}, {"display_name": "core-sh"}],
            ENVELOPE,
            "abcd1234",
        )
        self.assertIn('"v2-ec"', text)
        self.assertIn('"core-sh"', text)
        self.assertIn("Never message any session that is not in the expected list", text)
        self.assertIn("ambiguous", text)
        self.assertIn(ENVELOPE, text)
        self.assertIn("VERBATIM", text)

    def test_sender_argv_is_the_locked_down_headless_run(self) -> None:
        argv = claude_send.sender_argv("do it", "claude-sonnet-5")
        self.assertEqual(
            ["claude", "-p", "do it", "--allowedTools", "ListAgents,SendMessage",
             "--model", "claude-sonnet-5"],
            argv,
        )

    def test_sender_model_is_overridable(self) -> None:
        self.assertEqual("claude-sonnet-5", claude_send.sender_model({}))
        self.assertEqual(
            "claude-opus-5",
            claude_send.sender_model({"SESSION_KIT_MSG_SENDER_MODEL": "claude-opus-5"}),
        )

    def test_sender_output_parsing_takes_the_last_json_array(self) -> None:
        rows = claude_send.parse_sender_output(
            'chatter [1,2] more\n[{"name": "v2-ec", "send_ok": true}]\n'
        )
        self.assertEqual([{"name": "v2-ec", "send_ok": True}], rows)
        self.assertEqual([], claude_send.parse_sender_output("no json here"))
        self.assertEqual([], claude_send.parse_sender_output("[unterminated"))

    def test_run_sender_uses_a_stub_binary_and_reports_its_rows(self) -> None:
        stub = self.stub_claude('[{"name": "v2-ec", "send_ok": true}]')
        seen: dict[str, object] = {}

        def runner(argv, timeout, cwd=None):  # type: ignore[no-untyped-def]
            seen["argv"] = list(argv)
            seen["cwd"] = cwd
            return claude_send.default_runner([str(stub), *argv[1:]], timeout, cwd)

        result = claude_send.run_sender(
            "do it", model="claude-sonnet-5", cwd=self.home, runner=runner
        )
        self.assertTrue(result["ok"])
        self.assertEqual([{"name": "v2-ec", "send_ok": True}], result["rows"])
        self.assertEqual("claude", list(seen["argv"])[0])  # type: ignore[arg-type]
        self.assertEqual(self.home, seen["cwd"])

    def test_a_sender_that_fails_or_times_out_is_reported_not_raised(self) -> None:
        def failing(argv, timeout, cwd=None):  # type: ignore[no-untyped-def]
            return subprocess.CompletedProcess(list(argv), 2, "", "quota exhausted")

        result = claude_send.run_sender(
            "do it", model="m", cwd=self.home, runner=failing
        )
        self.assertFalse(result["ok"])
        self.assertIn("quota exhausted", str(result["detail"]))

        def timing_out(argv, timeout, cwd=None):  # type: ignore[no-untyped-def]
            raise subprocess.TimeoutExpired(list(argv), timeout)

        timed = claude_send.run_sender(
            "do it", model="m", cwd=self.home, runner=timing_out
        )
        self.assertFalse(timed["ok"])
        self.assertEqual("sender timed out", timed["detail"])


if __name__ == "__main__":
    unittest.main()
