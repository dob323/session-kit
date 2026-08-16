"""Delivery into a live Claude session, and the twelve ways it can fail.

The bug this replaces is a single sentence. Claude delivery spawned a whole
`claude -p` sender and, whenever anything went wrong, reported "not registered
(possible trust prompt)" -- for a missing binary, for a session that had
exited, for a refused socket, and for an actual trust prompt. Twenty-one
undelivered events all carried it; seven were measured and not one was a trust
prompt. A cause that is always the same is not a cause.

So every test here is one distinct ending, and each asserts that the outcome
NAMES that ending. The stub below is the vendor's socket contract as read off
the 2.1.229 binary: auth frame first or the connection is destroyed, JSON lines
after it, a session_id that must match the receiver's own, and receipts sent
back to the socket the sender named in `from`.

What is NOT stubbed: the paths, the key-file naming and the permissions are the
real ones, because a client that looks in the wrong place fails identically
against a stub that agrees with it.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket as socketmod
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))

from sessionkit_messages import claude_socket  # noqa: E402


SESSION = "22222222-3333-4444-8555-666666666666"
OTHER_SESSION = "11111111-2222-4333-8444-555555555555"


def runtime_env(root: Path, **extra: str) -> dict[str, str]:
    """The environment a real session publishes its socket in.

    A live session's socket is ``$XDG_RUNTIME_DIR/cc-socks/<pid>.sock`` and the
    client refuses one that is anywhere else, so a fixture that left the
    variable unset would not be a fixture of a real session. StubSession puts
    its socket in ``<root>/run/cc-socks``, which makes ``<root>/run`` the
    runtime directory these tests run against.
    """
    environment = {"XDG_RUNTIME_DIR": os.fspath(root / "run")}
    environment.update(extra)
    return environment


class StubSession:
    """A Claude session as far as its messaging socket is concerned."""

    def __init__(
        self,
        root: Path,
        *,
        session_id: str = SESSION,
        pid: int = 424242,
        receipt: str | None = "delivered",
        require_auth: bool = True,
        publish_key: bool = True,
        token: str = "a" * 32,
        close_after_accept: bool = False,
        reject_after_seconds: float | None = None,
        stop_reading_after_auth: bool = False,
    ) -> None:
        self.root = root
        self.session_id = session_id
        self.pid = pid
        self.receipt = receipt
        self.require_auth = require_auth
        self.token = token
        self.publish_key = publish_key
        self.close_after_accept = close_after_accept
        self.stop_reading_after_auth = stop_reading_after_auth
        self.reject_after_seconds = reject_after_seconds
        # Connections deliberately left open with their read side gone.
        self.held: list[socketmod.socket] = []
        self.received: list[dict] = []
        self.controls: list[dict] = []
        self.socket_dir = root / "run" / "cc-socks"
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        self.socket_path = self.socket_dir / f"{pid}.sock"
        self.config_dir = root / ".claude"
        (self.config_dir / "sessions").mkdir(parents=True, exist_ok=True)
        self.server: socketmod.socket | None = None
        self.thread: threading.Thread | None = None
        self.stopping = threading.Event()

    # -- the two files a real session publishes -----------------------------
    def write_record(self, *, socket_path: str | None = "") -> None:
        record = {
            "pid": self.pid,
            "sessionId": self.session_id,
            "kind": "interactive",
            "version": "2.1.229",
            "status": "busy",
            "cwd": str(self.root),
        }
        if socket_path != "":
            if socket_path is not None:
                record["messagingSocketPath"] = socket_path
        else:
            record["messagingSocketPath"] = os.fspath(self.socket_path)
        path = self.config_dir / "sessions" / f"{self.pid}.json"
        path.write_text(json.dumps(record), encoding="utf-8")

    def write_key(self) -> None:
        if not self.publish_key:
            return
        digest = hashlib.sha256(
            os.fspath(Path(os.path.realpath(self.socket_path))).encode("utf-8")
        ).hexdigest()
        path = self.config_dir / "sessions" / f"{self.pid}.{digest}.key"
        path.write_text(json.dumps({"peerToken": self.token}), encoding="utf-8")
        os.chmod(path, 0o600)

    # -- the socket ----------------------------------------------------------
    def start(self) -> None:
        self.stopping = threading.Event()
        self.server = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        self.server.bind(os.fspath(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.server.listen(4)
        # Closing a socket does not wake a thread already blocked in accept(),
        # so the loop wakes on its own and looks at the flag instead.
        self.server.settimeout(0.1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        for connection in getattr(self, "held", []):
            try:
                connection.close()
            except OSError:
                pass
        stopping = getattr(self, "stopping", None)
        if stopping is not None:
            stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                connection, _ = self.server.accept()  # type: ignore[union-attr]
            except (socketmod.timeout, TimeoutError):
                continue
            except OSError:
                return
            threading.Thread(
                target=self._handle, args=(connection,), daemon=True
            ).start()

    def _handle(self, connection: socketmod.socket) -> None:
        authenticated = not self.require_auth
        first = True
        buffer = b""
        keep_open = False
        connection.settimeout(10)
        try:
            while True:
                try:
                    block = connection.recv(65536)
                except OSError:
                    return
                if not block:
                    return
                buffer += block
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    if not line.strip():
                        continue
                    record = json.loads(line.decode("utf-8"))
                    if first:
                        first = False
                        if record.get("type") == "auth":
                            if record.get("token") == self.token:
                                if self.reject_after_seconds is not None:
                                    # A busy session that gets round to
                                    # refusing the token only later.
                                    time.sleep(self.reject_after_seconds)
                                    connection.close()
                                    return
                                if self.stop_reading_after_auth:
                                    # Took the token, then went away before the
                                    # message. The connection is NOT closed, so
                                    # the client's auth probe cannot see it --
                                    # the body write is what finds out, and it
                                    # finds out with EPIPE.
                                    connection.shutdown(socketmod.SHUT_RD)
                                    self.held.append(connection)
                                    keep_open = True
                                    return
                                authenticated = True
                                continue
                        if self.require_auth and not authenticated:
                            # Exactly what the daemon does: no reply, socket
                            # destroyed.
                            connection.close()
                            return
                    if not authenticated:
                        connection.close()
                        return
                    if record.get("type") == "control":
                        self.controls.append(record)
                        continue
                    if record.get("type") != "user":
                        continue
                    if record.get("session_id") not in (None, self.session_id):
                        continue
                    self.received.append(record)
                    self._receipt(record)
                    if self.close_after_accept:
                        # Accepted, queued, and done with the connection -- the
                        # shape that used to be reported as an auth refusal.
                        connection.close()
                        return
        finally:
            if not keep_open:
                try:
                    connection.close()
                except OSError:
                    pass

    def _receipt(self, record: dict) -> None:
        if self.receipt is None:
            return
        address = str(record.get("from") or "")
        if not address.startswith("uds:"):
            return
        target = address[4:]
        reply = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        try:
            reply.settimeout(5)
            reply.connect(target)
            reply.sendall(
                json.dumps(
                    {
                        "type": "control",
                        "action": "peer_message_status",
                        "orig_msg_id": record.get("msg_id"),
                        "status": self.receipt,
                    }
                ).encode("utf-8")
                + b"\n"
            )
        except OSError:
            pass
        finally:
            reply.close()


class SocketDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-socket-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def session(self, **kwargs) -> StubSession:
        stub = StubSession(self.root, **kwargs)
        self.addCleanup(stub.stop)
        return stub

    def send(self, stub: StubSession, text: str = "hello", **kwargs):
        return claude_socket.send(
            kwargs.pop("session_id", stub.session_id),
            text,
            environ=kwargs.pop("environ", runtime_env(self.root)),
            home=self.root,
            config_dir=stub.config_dir,
            timeout=kwargs.pop("timeout", 3),
            **kwargs,
        )

    def test_a_receipt_is_what_makes_it_delivered(self) -> None:
        stub = self.session()
        stub.write_record()
        stub.write_key()
        stub.start()
        outcome = self.send(stub, "the report is ready")
        self.assertTrue(outcome.delivered, outcome.detail)
        self.assertEqual("delivered", outcome.how)
        self.assertTrue(outcome.authenticated)
        self.assertEqual(1, len(stub.received))
        self.assertEqual(
            "the report is ready", stub.received[0]["message"]["content"]
        )
        # The message pins the conversation, so a recycled pid cannot be
        # handed someone else's answer.
        self.assertEqual(stub.session_id, stub.received[0]["session_id"])

    def test_a_message_written_but_never_confirmed_is_not_delivered(self) -> None:
        """The live case, not a corner: an ACCEPTED message sends no receipt.

        Measured against a real 2.1.229 session on 2026-08-13 -- it rendered
        the injected prompt and answered it, and wrote nothing back. So the
        sender must not call this delivered on its own say-so; it hands back
        the message uuid instead, which is the id the message carries in the
        target's own transcript.
        """
        stub = self.session(receipt=None)
        stub.write_record()
        stub.write_key()
        stub.start()
        outcome = self.send(stub, timeout=2)
        self.assertFalse(outcome.delivered)
        self.assertEqual("sent-no-receipt", outcome.how)
        self.assertTrue(outcome.authenticated)
        self.assertEqual(1, len(stub.received))
        self.assertEqual(
            stub.received[0]["uuid"], outcome.as_dict()["message_uuid"]
        )

    def test_a_held_message_is_reported_as_held_not_as_a_failure_to_reach(
        self,
    ) -> None:
        stub = self.session(receipt="held")
        stub.write_record()
        stub.write_key()
        stub.start()
        outcome = self.send(stub)
        self.assertFalse(outcome.delivered)
        self.assertEqual("held", outcome.how)
        self.assertIn("approve", outcome.detail)

    def test_a_declined_message_says_the_operator_declined_it(self) -> None:
        stub = self.session(receipt="denied")
        stub.write_record()
        stub.write_key()
        stub.start()
        outcome = self.send(stub)
        self.assertEqual("denied", outcome.how)
        self.assertIn("declined", outcome.detail)

    def test_a_session_that_exited_is_a_refused_socket_not_a_trust_prompt(
        self,
    ) -> None:
        stub = self.session()
        stub.write_record()
        stub.write_key()
        # The socket file outlives the process that bound it.
        stub.socket_path.parent.mkdir(parents=True, exist_ok=True)
        listener = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        listener.bind(os.fspath(stub.socket_path))
        listener.close()
        outcome = self.send(stub)
        self.assertEqual("socket-refused", outcome.how)
        self.assertEqual(
            claude_socket.FAILURE_DETAIL[claude_socket.SOCKET_REFUSED],
            outcome.detail,
        )
        self.assertFalse(outcome.as_dict().get("sent", False))

    def test_a_session_claude_never_recorded_says_exactly_that(self) -> None:
        stub = self.session()
        outcome = self.send(stub, session_id=OTHER_SESSION)
        self.assertEqual("no-session-record", outcome.how)

    def test_a_bare_session_says_it_has_no_socket(self) -> None:
        """--bare (and pre-2.1.224) sessions publish no socket at all."""
        stub = self.session()
        stub.write_record(socket_path=None)
        outcome = self.send(stub)
        self.assertEqual("no-socket-path", outcome.how)
        self.assertIn("--bare", outcome.detail)

    def test_a_socket_with_no_file_is_not_confused_with_a_refusal(self) -> None:
        stub = self.session()
        stub.write_record()
        stub.write_key()
        outcome = self.send(stub)
        self.assertEqual("socket-missing", outcome.how)

    def test_a_session_that_published_no_key_is_never_sent_to_blind(self) -> None:
        stub = self.session(publish_key=False)
        stub.write_record()
        stub.start()
        outcome = self.send(stub)
        self.assertEqual("no-auth-key", outcome.how)
        self.assertEqual([], stub.received)

    def test_a_refused_token_is_reported_as_a_refused_token(self) -> None:
        stub = self.session(token="b" * 32)
        stub.write_record()
        stub.write_key()
        stub.token = "c" * 32  # the session rotated it after publishing
        stub.start()
        outcome = self.send(stub)
        self.assertEqual("auth-rejected", outcome.how)
        self.assertEqual([], stub.received)

    def test_the_kill_switch_makes_the_channel_report_itself_off(self) -> None:
        stub = self.session()
        stub.write_record()
        stub.write_key()
        stub.start()
        outcome = self.send(stub, environ={"SESSION_KIT_CLAUDE_SOCKET": "0"})
        self.assertEqual("channel-off", outcome.how)
        self.assertEqual([], stub.received)

    def test_an_empty_message_is_refused_before_any_socket_is_touched(self) -> None:
        stub = self.session()
        stub.write_record()
        stub.write_key()
        outcome = self.send(stub, "   ")
        self.assertEqual("bad-request", outcome.how)


class AuthWindowTests(unittest.TestCase):
    """The two windows around the auth line, and why they are not one.

    "The peer closed the connection" is the only refusal signal the vendor
    gives. It is also what a session does after ACCEPTING a message. Reported
    as one thing, a delivered message reads as a refusal, the caller falls back
    to the spawned sender, and the same text arrives twice -- the
    never-double-deliver invariant (B13/C13). These tests are the differential:
    same close, two sides of the body write, two different answers.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-socket-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def session(self, **kwargs) -> StubSession:
        stub = StubSession(self.root, **kwargs)
        self.addCleanup(stub.stop)
        stub.write_record()
        stub.write_key()
        return stub

    def send(self, stub: StubSession, **kwargs):
        return claude_socket.send(
            stub.session_id,
            "the report is ready",
            environ=runtime_env(self.root),
            home=self.root,
            config_dir=stub.config_dir,
            timeout=kwargs.pop("timeout", 3),
            **kwargs,
        )

    def test_a_close_after_the_message_is_never_called_an_auth_refusal(
        self,
    ) -> None:
        stub = self.session(close_after_accept=True, receipt=None)
        stub.start()
        outcome = self.send(stub)
        self.assertEqual(
            ["the report is ready"],
            [item["message"]["content"] for item in stub.received],
            "the stub accepted the message, so the client must not call it refused",
        )
        self.assertNotEqual("auth-rejected", outcome.how)
        self.assertEqual("closed-after-send", outcome.how)
        self.assertTrue(outcome.authenticated)
        # The caller is told, in the outcome itself, that a resend would be a
        # second delivery.
        self.assertIn("twice", outcome.detail)
        self.assertIn("message_uuid", outcome.as_dict())

    def test_a_refused_token_closes_before_anything_is_sent(self) -> None:
        stub = self.session(token="b" * 32)
        stub.token = "c" * 32
        stub.start()
        outcome = self.send(stub)
        self.assertEqual("auth-rejected", outcome.how)
        self.assertFalse(outcome.authenticated)
        self.assertFalse(outcome.as_dict()["sent"])
        self.assertEqual([], stub.received)

    def test_one_sentence_covers_both_shapes_that_report_a_refused_socket(
        self,
    ) -> None:
        """N5: one cause id, two ways to arrive at it.

        A connect that is refused outright -- the session is gone and its
        socket file was left behind -- and a peer that took the token and then
        went away before the body both come back as `socket-refused`, and both
        are classified correctly: nothing was written either way. The sentence
        a person READS was written for the first shape only, in a round whose
        whole thesis is that every cause says its own true thing.
        """
        sentence = claude_socket.FAILURE_DETAIL[claude_socket.SOCKET_REFUSED]

        # Shape 1: the socket file outlives the process that bound it.
        elsewhere = tempfile.TemporaryDirectory(prefix="claude-socket-")
        self.addCleanup(elsewhere.cleanup)
        other_root = Path(elsewhere.name)
        exited = StubSession(other_root)
        self.addCleanup(exited.stop)
        exited.write_record()
        exited.write_key()
        listener = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        listener.bind(os.fspath(exited.socket_path))
        listener.close()
        left_behind = claude_socket.send(
            exited.session_id,
            "the report is ready",
            environ=runtime_env(other_root),
            home=other_root,
            config_dir=exited.config_dir,
            timeout=3,
        )
        self.assertEqual("socket-refused", left_behind.how)
        self.assertFalse(left_behind.delivered)
        self.assertFalse(left_behind.as_dict().get("sent", False))
        self.assertEqual(sentence, left_behind.detail)

        # Shape 2: the token was accepted, and then the peer stopped reading.
        vanished = self.session(stop_reading_after_auth=True)
        vanished.start()
        outcome = self.send(vanished)
        self.assertEqual("socket-refused", outcome.how)
        self.assertTrue(
            outcome.authenticated, "the token was accepted before the peer went"
        )
        self.assertFalse(outcome.as_dict()["sent"])
        self.assertEqual([], vanished.received)
        self.assertEqual(sentence, outcome.detail)

        # So the sentence may not describe only the first shape: this peer
        # left no abandoned socket file, and it was answering when the send
        # began.
        self.assertNotIn("left behind", sentence)
        self.assertNotIn("the session is gone", sentence)
        self.assertIn("nothing was written", sentence)

    def test_a_late_refusal_is_not_dressed_up_as_an_authenticated_send(
        self,
    ) -> None:
        """A session that only gets round to refusing after the probe window.

        It must not come back as sent-no-receipt with authenticated=True: the
        message never reached a queue, and the caller has to be free to try the
        other channel.
        """
        stub = self.session(
            reject_after_seconds=claude_socket.AUTH_PROBE_SECONDS + 0.6
        )
        stub.start()
        outcome = self.send(stub, timeout=3)
        self.assertEqual([], stub.received)
        self.assertFalse(outcome.delivered)
        # It is reported as the ambiguous thing it is, not as a confirmed send:
        # a caller reading `auth_confirmed` sees that nothing was proven, and
        # the detail tells it a resend risks a double delivery.
        self.assertEqual("closed-after-send", outcome.how)
        self.assertFalse(outcome.as_dict()["auth_confirmed"])
        self.assertIn("twice", outcome.detail)


class RenameTests(unittest.TestCase):
    """Renaming through the vendor's channel instead of its transcript file.

    The kit's name push appends an `agent-name` record to a file both vendors
    call internal (docs/pinned-internal-formats.md). The control frame says the
    same thing on a supported interface, and it reaches the session already on
    screen instead of the next one that starts.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-socket-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stub = StubSession(self.root)
        self.addCleanup(self.stub.stop)

    def rename(self, name: str = "  Overhaul   Bench  "):
        return claude_socket.rename(
            self.stub.session_id,
            name,
            environ=runtime_env(self.root),
            home=self.root,
            config_dir=self.stub.config_dir,
        )

    def test_a_rename_reaches_the_session_over_the_control_channel(self) -> None:
        self.stub.write_record()
        self.stub.write_key()
        self.stub.start()
        outcome = self.rename()
        # The session applies the name and answers nothing, so this can never
        # be "done" -- only "written to a session that took our token".
        self.assertFalse(outcome.delivered)
        self.assertEqual("rename-sent", outcome.how)
        self.assertEqual("Overhaul Bench", outcome.as_dict()["name"])
        deadline = time.monotonic() + 5
        while not self.stub.controls and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(1, len(self.stub.controls), "no control frame arrived")
        self.assertEqual("rename", self.stub.controls[0]["action"])
        self.assertEqual("Overhaul Bench", self.stub.controls[0]["name"])
        self.assertEqual(
            self.stub.session_id, self.stub.controls[0]["session_id"]
        )

    def test_a_session_that_refuses_the_token_is_never_reported_renamed(
        self,
    ) -> None:
        """The finding this test exists for: rename used to always succeed."""
        self.stub.write_record()
        self.stub.write_key()
        self.stub.token = "c" * 32  # rotated after publishing
        self.stub.start()
        outcome = self.rename()
        self.assertFalse(outcome.delivered)
        self.assertEqual("auth-rejected", outcome.how)
        self.assertFalse(outcome.as_dict()["sent"])
        time.sleep(0.3)
        self.assertEqual([], self.stub.controls)

    def test_an_unreachable_session_is_named_the_same_way_a_send_names_it(
        self,
    ) -> None:
        self.stub.write_record()
        outcome = claude_socket.rename(
            self.stub.session_id,
            "Anything",
            environ=runtime_env(self.root),
            home=self.root,
            config_dir=self.stub.config_dir,
        )
        self.assertFalse(outcome.delivered)
        self.assertEqual("socket-missing", outcome.how)


class StaleRecordTests(unittest.TestCase):
    """A crash leaves its session record behind; the live one has to win.

    Claude writes one record per PROCESS. Resume the same conversation after a
    crash and two records name the same conversation -- the dead one first, by
    pid order. Answering from it reports `socket-refused` for a session that is
    running and reachable, which is the same class of wrong answer as the
    catch-all sentence this module replaces.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-socket-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_the_record_whose_socket_answers_is_the_one_used(self) -> None:
        dead = StubSession(self.root, pid=100)
        self.addCleanup(dead.stop)
        dead.write_record()
        dead.write_key()
        # A socket file with nobody behind it: exactly what a crash leaves.
        listener = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        listener.bind(os.fspath(dead.socket_path))
        listener.close()

        live = StubSession(self.root, pid=999)
        self.addCleanup(live.stop)
        live.write_record()
        live.write_key()
        live.start()

        outcome = claude_socket.send(
            SESSION,
            "resumed",
            environ=runtime_env(self.root),
            home=self.root,
            config_dir=live.config_dir,
            timeout=3,
        )
        self.assertEqual(999, outcome.pid, outcome.detail)
        self.assertEqual(1, len(live.received))
        self.assertEqual([], dead.received)

    def test_a_record_without_a_socket_never_hides_a_live_one(self) -> None:
        bare = StubSession(self.root, pid=101)
        self.addCleanup(bare.stop)
        bare.write_record(socket_path=None)

        live = StubSession(self.root, pid=555)
        self.addCleanup(live.stop)
        live.write_record()
        live.write_key()
        live.start()

        report = claude_socket.capability(
            SESSION, environ=runtime_env(self.root), home=self.root, config_dir=live.config_dir
        )
        self.assertTrue(report["available"], report)
        self.assertEqual(555, report["pid"])


class OutcomeCoverageTests(unittest.TestCase):
    """The endings that had no test, each proven to name itself."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-socket-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stub = StubSession(self.root)
        self.addCleanup(self.stub.stop)

    def send(self, text: str = "hello", **kwargs):
        return claude_socket.send(
            self.stub.session_id,
            text,
            environ=runtime_env(self.root),
            home=self.root,
            config_dir=self.stub.config_dir,
            timeout=kwargs.pop("timeout", 3),
            **kwargs,
        )

    def test_a_path_that_is_not_a_socket_says_so(self) -> None:
        self.stub.write_record()
        self.stub.write_key()
        self.stub.socket_path.write_text("not a socket", encoding="utf-8")
        self.assertEqual("not-a-socket", self.send().how)

    def test_an_oversize_message_is_refused_before_the_socket(self) -> None:
        self.stub.write_record()
        self.stub.write_key()
        self.stub.start()
        outcome = self.send("x" * (claude_socket.MAX_TEXT_CHARS + 1))
        self.assertEqual("bad-request", outcome.how)
        self.assertEqual([], self.stub.received)

    def test_an_expired_receipt_is_reported_as_expired(self) -> None:
        stub = StubSession(self.root, pid=606, receipt="expired")
        self.addCleanup(stub.stop)
        stub.write_record()
        stub.write_key()
        stub.start()
        outcome = claude_socket.send(
            stub.session_id,
            "hello",
            environ=runtime_env(self.root),
            home=self.root,
            config_dir=stub.config_dir,
            timeout=3,
        )
        self.assertEqual("expired", outcome.how)
        self.assertIn("shut down", outcome.detail)

    def test_an_infinite_timeout_is_refused_rather_than_waited_out(self) -> None:
        import argparse

        with self.assertRaises(argparse.ArgumentTypeError):
            claude_socket._timeout_argument("inf")
        with self.assertRaises(argparse.ArgumentTypeError):
            claude_socket._timeout_argument("nan")
        # A library caller is bounded too, not just the command line.
        self.assertEqual(
            claude_socket.DEFAULT_TIMEOUT_SECONDS,
            claude_socket._bounded_seconds(float("inf")),
        )

    def test_every_outcome_name_carries_its_own_sentence(self) -> None:
        """One list, one count -- the report used to give two."""
        self.assertEqual(len(set(claude_socket.OUTCOMES)), len(claude_socket.OUTCOMES))
        failures = set(claude_socket.OUTCOMES) - {claude_socket.DELIVERED}
        self.assertEqual(
            failures,
            set(claude_socket.FAILURE_DETAIL),
            "an outcome exists with no sentence, or a sentence with no outcome",
        )
        for name, sentence in claude_socket.FAILURE_DETAIL.items():
            self.assertGreater(len(sentence), 20, name)


class RecordingListener:
    """A socket that is NOT the session's, and a ledger of what reached it.

    The point of every test below is a number: how many bytes this listener
    was told. Anything above zero means the auth token and the message body
    left this process for a destination nobody proved was ours.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.connections = 0
        self.payload = bytearray()
        self.socket = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        self.socket.bind(os.fspath(self.path))
        os.chmod(self.path, 0o600)
        self.socket.listen(4)
        self.socket.settimeout(0.1)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                connection, _ = self.socket.accept()
            except (socketmod.timeout, TimeoutError):
                continue
            except OSError:
                return
            self.connections += 1
            connection.settimeout(0.2)
            try:
                # Held open deliberately. A stand-in that hangs up after the
                # first line makes the client report auth-rejected and stop,
                # which UNDER-counts the disclosure: with the connection kept,
                # the body follows the token, which is what the security
                # report measured (372 bytes, token and message both).
                while not self.stopping.is_set():
                    try:
                        block = connection.recv(65536)
                    except (socketmod.timeout, TimeoutError):
                        continue
                    if not block:
                        break
                    self.payload.extend(block)
            except OSError:
                pass
            finally:
                connection.close()

    def stop(self) -> None:
        self.stopping.set()
        self.thread.join(timeout=5)
        try:
            self.socket.close()
        except OSError:
            pass


class ForeignSocketTests(unittest.TestCase):
    """S1: the destination is proved ours BEFORE the token leaves this process.

    The socket path is read out of a FILE. Until this gate existed the client
    dialled whatever that file said, sent the auth token as its first line and
    the whole message after it -- proven against a stand-in foreign listener,
    361 bytes including the token. Nothing checked the owner, the mode, the
    directory or the runtime root.

    So each test here plants one thing a real session would never publish and
    asserts the same two facts: the outcome is `foreign-socket`, and the
    listener standing in for the foreign end was told ZERO bytes. The last
    test is the control -- the legitimate own-session path still delivers,
    because a gate that refuses everything is not a fix.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-socket-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "run"
        (self.runtime / "cc-socks").mkdir(parents=True, exist_ok=True)

    def listener(self, path: Path) -> RecordingListener:
        listening = RecordingListener(path)
        self.addCleanup(listening.stop)
        return listening

    def planted(self, socket_path: Path) -> StubSession:
        """A complete, believable session record naming this socket.

        Both files, exactly as a live session publishes them -- the record AND
        the key whose name is the digest of that path. A fixture missing the
        key would refuse for the wrong reason (`no-auth-key`) and prove
        nothing about the gate.
        """
        stub = StubSession(self.root)
        self.addCleanup(stub.stop)
        stub.socket_path = Path(socket_path)
        stub.write_record()
        stub.write_key()
        return stub

    def send(self, stub: StubSession, **kwargs):
        return claude_socket.send(
            stub.session_id,
            "the report is ready",
            environ=kwargs.pop("environ", runtime_env(self.root)),
            home=self.root,
            config_dir=stub.config_dir,
            timeout=3,
        )

    def assert_refused(self, outcome, listening: RecordingListener) -> None:
        self.assertEqual(claude_socket.FOREIGN_SOCKET, outcome.how, outcome.detail)
        self.assertFalse(outcome.delivered)
        self.assertFalse(outcome.authenticated, "a token was offered to it")
        self.assertFalse(outcome.as_dict().get("sent", False))
        self.assertEqual(0, listening.connections, "the client dialled it")
        self.assertEqual(
            0, len(listening.payload), f"bytes sent: {bytes(listening.payload)!r}"
        )
        # Nothing of ours may be left in a directory we refused to trust,
        # either -- the receipt listener binds beside the target's socket.
        residue = sorted(
            path.name
            for path in listening.path.parent.iterdir()
            if path.name.startswith("session-kit-receipt.")
        )
        self.assertEqual([], residue, "a receipt socket was bound beside it")

    def test_a_socket_outside_the_runtime_directory_is_never_dialled(self) -> None:
        """The proven attack: a record pointing at a shared directory."""
        shared = Path(tempfile.mkdtemp(prefix="claude-socket-foreign-"))
        self.addCleanup(lambda: shutil.rmtree(shared, True))
        listening = self.listener(shared / "foreign.sock")
        stub = self.planted(listening.path)
        # The receipt listener binds in the TARGET's directory, so a refusal
        # that still built one would have put a socket of ours in a directory
        # we just refused to trust. It is never even constructed.
        built: list[Path] = []
        real = claude_socket._ReceiptListener
        with mock.patch.object(
            claude_socket,
            "_ReceiptListener",
            lambda directory: built.append(directory) or real(directory),
        ):
            outcome = self.send(stub)
        self.assertEqual([], built, "a receipt socket was built for a foreign directory")
        self.assert_refused(outcome, listening)

    def test_a_socket_another_user_owns_is_never_dialled(self) -> None:
        """Ownership, with this process standing in for the other user.

        A test cannot create a file owned by somebody else without being root,
        so the euid moves instead: the socket below belongs to a uid that is
        not the one asking, which is the only thing the check reads.
        """
        listening = self.listener(self.runtime / "cc-socks" / "424242.sock")
        stub = self.planted(listening.path)
        with mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            outcome = self.send(stub)
        self.assert_refused(outcome, listening)

    def test_a_group_writable_socket_is_never_dialled(self) -> None:
        listening = self.listener(self.runtime / "cc-socks" / "424242.sock")
        os.chmod(listening.path, 0o660)
        stub = self.planted(listening.path)
        self.assert_refused(self.send(stub), listening)

    def test_a_world_writable_socket_is_never_dialled(self) -> None:
        listening = self.listener(self.runtime / "cc-socks" / "424242.sock")
        os.chmod(listening.path, 0o666)
        stub = self.planted(listening.path)
        self.assert_refused(self.send(stub), listening)

    def test_a_socket_in_a_world_writable_directory_is_never_dialled(self) -> None:
        """Owned by us, 0600, in the runtime dir -- and still not safe.

        Anyone who can write the directory can unlink the socket and bind
        their own in its place between the check and the connect, so the
        directory is part of the gate, not context for it.
        """
        shared = self.runtime / "shared"
        shared.mkdir()
        os.chmod(shared, 0o777)
        listening = self.listener(shared / "424242.sock")
        stub = self.planted(listening.path)
        self.assert_refused(self.send(stub), listening)

    def test_a_symlinked_socket_path_is_never_dialled(self) -> None:
        listening = self.listener(self.runtime / "cc-socks" / "424242.sock")
        link = self.runtime / "cc-socks" / "link.sock"
        link.symlink_to(listening.path)
        stub = self.planted(link)
        self.assert_refused(self.send(stub), listening)

    def test_a_symlinked_parent_directory_is_never_dialled(self) -> None:
        """The link is one component up, and the socket itself is honest."""
        outside = Path(tempfile.mkdtemp(prefix="claude-socket-outside-"))
        self.addCleanup(lambda: shutil.rmtree(outside, True))
        listening = self.listener(outside / "424242.sock")
        link = self.runtime / "linked"
        link.symlink_to(outside)
        stub = self.planted(link / "424242.sock")
        self.assert_refused(self.send(stub), listening)

    def test_the_capability_probe_refuses_the_same_target_the_same_way(self) -> None:
        """One gate, both paths: the probe cannot bless what the send refuses."""
        shared = Path(tempfile.mkdtemp(prefix="claude-socket-foreign-"))
        self.addCleanup(lambda: shutil.rmtree(shared, True))
        listening = self.listener(shared / "foreign.sock")
        stub = self.planted(listening.path)
        report = claude_socket.capability(
            stub.session_id,
            environ=runtime_env(self.root),
            home=self.root,
            config_dir=stub.config_dir,
        )
        self.assertFalse(report["available"], report)
        self.assertEqual(claude_socket.FOREIGN_SOCKET, report["reason"])
        self.assertFalse(report["authenticated"])
        self.assertEqual(0, listening.connections)
        self.assertEqual(0, len(listening.payload))

    def test_the_rename_channel_refuses_it_too(self) -> None:
        """The third route to the wire, held to the same check."""
        shared = Path(tempfile.mkdtemp(prefix="claude-socket-foreign-"))
        self.addCleanup(lambda: shutil.rmtree(shared, True))
        listening = self.listener(shared / "foreign.sock")
        stub = self.planted(listening.path)
        outcome = claude_socket.rename(
            stub.session_id,
            "Anything",
            environ=runtime_env(self.root),
            home=self.root,
            config_dir=stub.config_dir,
        )
        self.assertEqual(claude_socket.FOREIGN_SOCKET, outcome.how)
        self.assertEqual(0, listening.connections)
        self.assertEqual(0, len(listening.payload))

    def test_a_planted_record_never_wins_the_stale_record_probe(self) -> None:
        """Two records, one conversation: the foreign one gets no connect.

        find_target dials candidates to find out which is live. That probe is
        a connect like any other, so it passes the same gate -- a record
        planted with a higher pid must not be able to make the client touch a
        socket it does not own.
        """
        shared = Path(tempfile.mkdtemp(prefix="claude-socket-foreign-"))
        self.addCleanup(lambda: shutil.rmtree(shared, True))
        listening = self.listener(shared / "foreign.sock")
        planted = self.planted(listening.path)
        planted.pid = 999999
        planted.write_record()
        planted.write_key()

        live = StubSession(self.root, pid=424242)
        self.addCleanup(live.stop)
        live.write_record()
        live.write_key()
        live.start()

        outcome = claude_socket.send(
            SESSION,
            "the report is ready",
            environ=runtime_env(self.root),
            home=self.root,
            config_dir=live.config_dir,
            timeout=3,
        )
        self.assertEqual(0, listening.connections, "the planted record got a connect")
        self.assertEqual(0, len(listening.payload))
        self.assertEqual(424242, outcome.pid, outcome.detail)
        self.assertEqual(1, len(live.received))

    def test_the_legitimate_own_session_path_still_delivers(self) -> None:
        """The control. A gate that refuses everything is not a fix.

        Owned by this euid, 0600, in `$XDG_RUNTIME_DIR/cc-socks`, reached
        through no symlink -- which is what a real 2.1.229 session publishes.
        """
        stub = StubSession(self.root)
        self.addCleanup(stub.stop)
        stub.write_record()
        stub.write_key()
        stub.start()
        outcome = self.send(stub)
        self.assertTrue(outcome.delivered, outcome.detail)
        self.assertEqual(
            ["the report is ready"],
            [item["message"]["content"] for item in stub.received],
        )
        self.assertEqual("", claude_socket._socket_problem(
            stub.socket_path, environ=runtime_env(self.root)
        ))

    def test_the_receipt_socket_is_private_before_the_chmod_lands(self) -> None:
        """S5: bind() takes the umask, and the chmod is a syscall later.

        The chmod is disabled here on purpose, so what is asserted is the mode
        the BIND produced. Under a 0000 umask the old code published a
        world-writable socket for the length of that window, in whatever
        directory the target named.
        """
        directory = self.runtime / "cc-socks"
        previous = os.umask(0o000)
        self.addCleanup(os.umask, previous)
        with mock.patch.object(os, "chmod"):
            with claude_socket._ReceiptListener(directory) as listener:
                self.assertIsNotNone(listener.path, "the listener never bound")
                mode = stat.S_IMODE(os.stat(listener.path).st_mode)
        self.assertEqual(
            0, mode & (stat.S_IRWXG | stat.S_IRWXO), f"bound as {mode:04o}"
        )


class CapabilityTests(unittest.TestCase):
    """Choosing a channel before there is a message to lose."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-socket-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def capability(self, stub: StubSession, **kwargs):
        return claude_socket.capability(
            kwargs.pop("session_id", stub.session_id),
            environ=kwargs.pop("environ", runtime_env(self.root)),
            home=self.root,
            config_dir=stub.config_dir,
        )

    def test_a_reachable_session_is_reported_available_and_authenticated(
        self,
    ) -> None:
        stub = StubSession(self.root)
        self.addCleanup(stub.stop)
        stub.write_record()
        stub.write_key()
        stub.start()
        report = self.capability(stub)
        self.assertTrue(report["available"])
        self.assertTrue(report["authenticated"])
        self.assertEqual("", report["reason"])

    def test_capability_touches_no_socket(self) -> None:
        stub = StubSession(self.root)
        self.addCleanup(stub.stop)
        stub.write_record()
        stub.write_key()
        stub.start()
        self.capability(stub)
        self.assertEqual([], stub.received)

    def test_an_unreachable_session_reports_the_same_cause_a_send_would(
        self,
    ) -> None:
        stub = StubSession(self.root)
        self.addCleanup(stub.stop)
        stub.write_record()
        report = self.capability(stub)
        self.assertFalse(report["available"])
        self.assertEqual("socket-missing", report["reason"])

    def test_the_machine_answer_ignores_a_record_naming_a_foreign_socket(
        self,
    ) -> None:
        """"Has this box the channel at all" is asked with no session named.

        A record pointing at a socket the gate would refuse is not a channel
        this box has: answering yes sends a caller down a path every later
        step refuses, which is the "available but unusable" shape the module
        already avoids for a missing auth key.
        """
        shared = Path(tempfile.mkdtemp(prefix="claude-socket-foreign-"))
        self.addCleanup(lambda: shutil.rmtree(shared, True))
        foreign = shared / "foreign.sock"
        holder = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        holder.bind(os.fspath(foreign))
        holder.close()
        planted = StubSession(self.root, pid=999999)
        self.addCleanup(planted.stop)
        planted.socket_path = foreign
        planted.write_record()

        report = claude_socket.capability(
            "",
            environ=runtime_env(self.root),
            home=self.root,
            config_dir=planted.config_dir,
        )
        self.assertFalse(report["available"], report)
        self.assertEqual("no-socket-path", report["reason"])

        # The same box, once a record names a socket the gate accepts.
        live = StubSession(self.root, pid=424242)
        self.addCleanup(live.stop)
        live.write_record()
        live.start()
        report = claude_socket.capability(
            "",
            environ=runtime_env(self.root),
            home=self.root,
            config_dir=live.config_dir,
        )
        self.assertTrue(report["available"], report)

    def test_the_kill_switch_is_visible_before_a_send_is_attempted(self) -> None:
        stub = StubSession(self.root)
        self.addCleanup(stub.stop)
        stub.write_record()
        stub.write_key()
        report = self.capability(stub, environ={"SESSION_KIT_CLAUDE_SOCKET": "0"})
        self.assertFalse(report["enabled"])
        self.assertEqual("channel-off", report["reason"])


class KeyNamingTests(unittest.TestCase):
    def test_the_key_file_name_is_the_digest_of_the_resolved_socket_path(
        self,
    ) -> None:
        """Pinned against the 2.1.229 key-file naming rule.

        The name is ``<pid>.<sha256 of the resolved socket path>.key``, shaped
        exactly like a live record; the digest below is that same
        construction, recomputed here so a change to the rule fails rather
        than silently looking in an empty directory.
        """
        target = claude_socket.Target(
            session_id=SESSION,
            pid=987654,
            socket_path=Path("/run/user/1000/cc-socks/987654.sock"),
            record_path=Path("/dev/null"),
        )
        expected = hashlib.sha256(
            b"/run/user/1000/cc-socks/987654.sock"
        ).hexdigest()
        name = claude_socket.auth_key_path(target, config_dir=Path("/tmp")).name
        self.assertEqual(f"987654.{expected}.key", name)
        self.assertEqual(
            "d76bebf7ede4eb05d82e19f2b3e1d58ee5275f71a15d81eb2cff8f311c62b338",
            expected,
        )


if __name__ == "__main__":
    unittest.main()
