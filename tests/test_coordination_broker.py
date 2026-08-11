from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


# The broker and its message core live in a separate repository, not in this
# one. Point SESSION_KIT_COORDINATION_ROOT at a checkout that has them to run
# these tests; with it unset they skip. No default path is written here on
# purpose: a checkout location belongs to whoever made the checkout, so this
# file names no machine and the suite is green on a clean install.
COORDINATION_ROOT = os.environ.get("SESSION_KIT_COORDINATION_ROOT", "").strip()
BROKER = Path(COORDINATION_ROOT, ".claude/coordination/provider_broker.py")
CORE = Path(COORDINATION_ROOT, ".claude/coordination/coord_messages.py")
COORDINATION_AVAILABLE = bool(COORDINATION_ROOT) and BROKER.is_file() and CORE.is_file()
COORDINATION_SKIP_REASON = (
    "no coordination checkout: set SESSION_KIT_COORDINATION_ROOT to a directory "
    "holding .claude/coordination/provider_broker.py and coord_messages.py"
)
SHPOOL_BASHRC = Path(__file__).resolve().parents[1] / "bashrc/shpool.bashrc"


def read_exact(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def read_client_frame(connection: socket.socket) -> dict[str, object]:
    first, second = read_exact(connection, 2)
    if first & 0x0F != 1 or not second & 0x80:
        raise ValueError("expected masked text frame")
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", read_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(connection, 8))[0]
    mask = read_exact(connection, 4)
    payload = read_exact(connection, length)
    decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return json.loads(decoded)


def send_server_frame(connection: socket.socket, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    if len(encoded) < 126:
        header = bytes((0x81, len(encoded)))
    else:
        header = bytes((0x81, 126)) + struct.pack("!H", len(encoded))
    connection.sendall(header + encoded)


class FakeAppServer(threading.Thread):
    def __init__(self, path: Path, thread_id: str):
        super().__init__(daemon=True)
        self.path = path
        self.thread_id = thread_id
        self.turn_input = ""
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.error: Exception | None = None

    def run(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(os.fspath(self.path))
            server.listen(1)
            self.ready.set()
            connection, _ = server.accept()
            with connection:
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    request.extend(connection.recv(4096))
                headers = request.decode("ascii").split("\r\n")
                key = next(
                    line.split(":", 1)[1].strip()
                    for line in headers
                    if line.lower().startswith("sec-websocket-key:")
                )
                accept = base64.b64encode(
                    hashlib.sha1(
                        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                    ).digest()
                ).decode()
                connection.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                while not self.finished.is_set():
                    message = read_client_frame(connection)
                    method = message.get("method")
                    request_id = message.get("id")
                    if request_id is None:
                        continue
                    if method == "initialize":
                        result: dict[str, object] = {}
                    elif method == "thread/loaded/list":
                        result = {"data": [self.thread_id]}
                    elif method == "thread/read":
                        result = {
                            "thread": {
                                "id": self.thread_id,
                                "status": {"type": "idle"},
                            }
                        }
                    elif method == "turn/start":
                        params = message["params"]
                        self.turn_input = params["input"][0]["text"]
                        result = {"turn": {"id": "turn-test", "status": "inProgress"}}
                        self.finished.set()
                    else:
                        raise ValueError(f"unexpected method: {method}")
                    send_server_frame(connection, {"id": request_id, "result": result})
        except Exception as exc:
            self.error = exc
            self.finished.set()
        finally:
            server.close()


@unittest.skipUnless(COORDINATION_AVAILABLE, COORDINATION_SKIP_REASON)
class CoordinationBrokerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.coordination = self.root / ".claude/coordination"
        self.coordination.mkdir(parents=True)
        (self.root / ".git").mkdir()
        shutil.copy2(CORE, self.coordination / "coord_messages.py")
        self.core = self.coordination / "coord_messages.py"
        self.thread_id = "019fc000-0000-7000-8000-000000000001"
        self.actor = f"codex:root:{self.thread_id}"
        self.sender = "claude:test:root:sender"
        self.call(
            "register", self.actor, "--provider", "codex", "--native-id",
            self.thread_id, "--lifecycle", "live", "--role", "root",
        )
        self.call(
            "register", self.sender, "--provider", "claude", "--native-id",
            "sender", "--lifecycle", "live", "--role", "root",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def call(self, *arguments: str) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(self.core), *arguments],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout)

    def test_exact_idle_thread_accepts_one_action_and_records_native_enqueue(self) -> None:
        sent = self.call(
            "send", self.sender, self.actor, "take this handoff", "--class", "action"
        )
        socket_path = Path(self.temporary.name) / "app.sock"
        server = FakeAppServer(socket_path, self.thread_id)
        server.start()
        self.assertTrue(server.ready.wait(2))
        broker = subprocess.Popen(
            [
                sys.executable, str(BROKER), "--socket", str(socket_path),
                "--repo", str(self.root), "--thread", self.thread_id,
                "--server-pid", str(os.getpid()), "--poll-seconds", "0.1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertTrue(server.finished.wait(3))
            deadline = time.monotonic() + 3
            state = ""
            while time.monotonic() < deadline:
                state = str(self.call("status", str(sent["message_id"]))["message"]["state"])
                if state == "native_enqueued":
                    break
                time.sleep(0.05)
            self.assertEqual("native_enqueued", state)
            self.assertIn(str(sent["message_id"]), server.turn_input)
            self.assertIn(self.actor, server.turn_input)
            self.assertIsNone(server.error)
        finally:
            broker.terminate()
            broker.wait(timeout=3)
            assert broker.stdout is not None and broker.stderr is not None
            broker.stdout.close()
            broker.stderr.close()


class CodexAppServerLaunchGateTest(unittest.TestCase):
    """Reads only this repository, so it runs on a machine with no broker."""

    def test_launch_path_is_private_repo_gated_and_direct_fallback_remains(self) -> None:
        source = SHPOOL_BASHRC.read_text(encoding="utf-8")
        self.assertIn('config.get("codex_app_server") is not True', source)
        self.assertIn("os.path.realpath(launch_cwd) != repo_root", source)
        self.assertIn('__sk_codex_remote=()', source)
        self.assertIn('echo "[session-kit: Codex App Server unavailable; using direct TUI]"', source)


if __name__ == "__main__":
    unittest.main()
