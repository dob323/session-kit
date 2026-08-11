"""App Server socket directories: reaching the live ones, removing the dead ones.

Nothing here touches a real store. Every socket is a disposable fixture in a
private temporary tree, and the fake App Server speaks only enough of the
protocol to prove a rename arrived.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import socket as socketmod
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))
from sessionkit_inventory import names_push, reaper  # noqa: E402

UUID = "00000000-0000-4000-8000-000000000099"
HOUR = 3600.0
# The sweep reads real mtimes, so the fixtures are aged against the real
# clock: one reading, shared by the fixtures and by every assertion.
NOW = time.time()


class FakeAppServer:
    """One-connection App Server: handshake, echo replies, record the rename."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.renames: list[dict] = []
        self.connected = threading.Event()
        self._server = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        self._server.bind(os.fspath(socket_path))
        self._server.listen(4)
        self._server.settimeout(5)
        self._worker = threading.Thread(target=self._serve, daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._server.close()
        self._worker.join(timeout=5)

    def _serve(self) -> None:
        try:
            link, _ = self._server.accept()
        except OSError:
            return
        self.connected.set()
        link.settimeout(5)
        try:
            self._converse(link)
        except OSError:
            pass
        finally:
            link.close()

    def _converse(self, link: socketmod.socket) -> None:
        def read_exact(count: int) -> bytes:
            data = b""
            while len(data) < count:
                chunk = link.recv(count - len(data))
                if not chunk:
                    raise OSError("client closed")
                data += chunk
            return data

        request = b""
        while not request.endswith(b"\r\n\r\n"):
            request += read_exact(1)
        key = next(
            line.split(b":", 1)[1].strip()
            for line in request.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key:")
        )
        accept = base64.b64encode(
            hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
        )
        link.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        while True:
            first, second = read_exact(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", read_exact(8))[0]
            mask = read_exact(4) if second & 0x80 else b""
            payload = read_exact(length)
            if mask:
                payload = bytes(
                    value ^ mask[index % 4] for index, value in enumerate(payload)
                )
            if first & 0x0F != 1:
                continue
            message = json.loads(payload)
            if message.get("id") is not None:
                reply = json.dumps(
                    {"id": message["id"], "result": {}}, separators=(",", ":")
                ).encode()
                link.sendall(bytes([0x81, len(reply)]) + reply)
            if message.get("method") == "thread/name/set":
                self.renames.append(message.get("params") or {})
                return


def dead_socket(socket_path: Path) -> None:
    """A socket file whose server is gone: present, and refusing at once."""
    holder = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
    holder.bind(os.fspath(socket_path))
    holder.listen(1)
    holder.close()


class SocketSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sk-sweep-")
        self.state = Path(self.temporary.name) / "session-kit"
        self.app_root = self.state / "app-server"
        self.app_root.mkdir(parents=True, mode=0o700)
        self.servers: list[FakeAppServer] = []

    def tearDown(self) -> None:
        for server in self.servers:
            server.close()
        self.temporary.cleanup()

    def make_dir(self, name: str, *, live: bool, age_rank: int) -> Path:
        directory = self.app_root / name
        directory.mkdir(mode=0o700)
        socket_path = directory / "app.sock"
        if live:
            self.servers.append(FakeAppServer(socket_path))
        else:
            dead_socket(socket_path)
        stamp = NOW - age_rank * 60
        os.utime(socket_path, (stamp, stamp))
        os.utime(directory, (stamp, stamp))
        return directory

    def push(self, max_sockets: int = 8) -> tuple[list[str], list[str]]:
        return names_push._push_codex_live_rename(
            self.state,
            UUID,
            "Live Name",
            max_sockets=max_sockets,
            max_frame=names_push.MAX_CODEX_LIVE_RENAME_FRAME,
        )

    def test_dead_directories_never_crowd_out_the_newest_live_ones(self) -> None:
        # The shape that broke on 2026-08-07: twelve directories, nine of them
        # abandoned and alphabetically first, three live windows behind them.
        for index in range(9):
            self.make_dir(f"s-dead-{index:02d}", live=False, age_rank=index + 3)
        live = [
            self.make_dir(f"s-live-{index}", live=True, age_rank=index)
            for index in range(3)
        ]
        self.assertEqual(3, len(live))
        pushes, warnings = self.push()
        self.assertEqual((["codex-live-rename"], []), (pushes, warnings))
        for server in self.servers:
            self.assertEqual(
                [{"threadId": UUID, "name": "Live Name"}],
                server.renames,
                msg=os.fspath(server.socket_path),
            )

    def test_the_cap_counts_connections_and_takes_the_newest(self) -> None:
        for index in range(10):
            self.make_dir(f"s-live-{index:02d}", live=True, age_rank=index)
        pushes, _ = self.push()
        self.assertEqual(["codex-live-rename"], pushes)
        contacted = {
            server.socket_path.parent.name
            for server in self.servers
            if server.connected.is_set()
        }
        self.assertEqual(
            {f"s-live-{index:02d}" for index in range(8)},
            contacted,
        )

    def test_a_missing_root_and_a_socketless_directory_stay_silent(self) -> None:
        (self.app_root / "s-empty").mkdir(mode=0o700)
        self.assertEqual(([], []), self.push())
        empty_state = Path(self.temporary.name) / "no-such-kit"
        self.assertEqual(
            ([], []),
            names_push._push_codex_live_rename(
                empty_state,
                UUID,
                "Live Name",
                max_sockets=8,
                max_frame=names_push.MAX_CODEX_LIVE_RENAME_FRAME,
            ),
        )


class StaleDirectorySweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sk-reap-")
        self.state = Path(self.temporary.name) / "session-kit"
        self.app_root = self.state / "app-server"
        self.app_root.mkdir(parents=True, mode=0o700)
        self.servers: list[FakeAppServer] = []

    def tearDown(self) -> None:
        for server in self.servers:
            server.close()
        self.temporary.cleanup()

    def make_dir(
        self,
        name: str,
        *,
        socket: str = "dead",
        age_seconds: float = 4 * HOUR,
    ) -> Path:
        directory = self.app_root / name
        directory.mkdir(mode=0o700)
        stamp = NOW - age_seconds
        log = directory / "app-server.log"
        log.write_text("fixture server output\n", encoding="utf-8")
        log.chmod(0o600)
        os.utime(log, (stamp, stamp))
        if socket != "none":
            socket_path = directory / "app.sock"
            if socket == "live":
                self.servers.append(FakeAppServer(socket_path))
            else:
                dead_socket(socket_path)
            os.utime(socket_path, (stamp, stamp))
        os.utime(directory, (stamp, stamp))
        return directory

    def sweep(self, live: list[str], **overrides: object) -> list[str]:
        return reaper.sweep_stale_app_server_dirs(
            self.state, live, now_unix=NOW, **overrides
        )

    def test_removes_only_directories_with_all_three_proofs(self) -> None:
        abandoned = self.make_dir("s-abandoned")
        socketless = self.make_dir("s-socketless", socket="none")
        attached = self.make_dir("s-attached")
        fresh = self.make_dir("s-fresh", age_seconds=59 * 60)
        answering = self.make_dir("s-answering", socket="live")

        removed = self.sweep(["s-attached", "s20260807-093118-829518"])

        self.assertEqual(["s-abandoned", "s-socketless"], sorted(removed))
        self.assertFalse(abandoned.exists())
        self.assertFalse(socketless.exists())
        for kept in (attached, fresh, answering):
            self.assertTrue(kept.is_dir(), msg=os.fspath(kept))

    def test_a_touched_file_keeps_its_directory(self) -> None:
        directory = self.make_dir("s-logging")
        log = directory / "app-server.log"
        os.utime(log, (NOW - 60, NOW - 60))
        self.assertEqual([], self.sweep([]))
        self.assertTrue(directory.is_dir())

    def test_unexpected_contents_are_left_alone(self) -> None:
        nested = self.make_dir("s-nested")
        (nested / "inner").mkdir(mode=0o700)
        os.utime(nested / "inner", (NOW - 4 * HOUR, NOW - 4 * HOUR))
        os.utime(nested, (NOW - 4 * HOUR, NOW - 4 * HOUR))

        linked = self.make_dir("s-linked")
        (linked / "elsewhere.log").symlink_to(self.app_root / "s-nested")
        os.utime(linked, (NOW - 4 * HOUR, NOW - 4 * HOUR))

        odd_name = self.app_root / ".hidden"
        odd_name.mkdir(mode=0o700)
        os.utime(odd_name, (NOW - 4 * HOUR, NOW - 4 * HOUR))

        self.assertEqual([], self.sweep([]))
        for kept in (nested, linked, odd_name):
            self.assertTrue(kept.is_dir(), msg=os.fspath(kept))

    def test_removals_are_bounded_per_run(self) -> None:
        for index in range(6):
            self.make_dir(f"s-old-{index}")
        removed = self.sweep([], max_removals=2)
        self.assertEqual(2, len(removed))
        self.assertEqual(4, len(list(self.app_root.iterdir())))

    def run_cli(self, shpool_json: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            PYTHONPATH=os.fspath(REPO / "lib"),
            PYTHONDONTWRITEBYTECODE="1",
            SK_REAPER_SHPOOL_JSON=shpool_json,
            SK_REAPER_STATE_DIR=os.fspath(self.state),
        )
        return subprocess.run(
            [sys.executable, "-m", "sessionkit_inventory.reaper", "sweep-app-servers"],
            text=True,
            capture_output=True,
            check=True,
            env=environment,
        )

    def test_an_unreadable_daemon_list_removes_nothing(self) -> None:
        # No proof of what is live, no removal — a malformed list must never
        # read as "no sessions are running".
        self.make_dir("s-abandoned")
        for payload in (
            "{not json",
            json.dumps({"sessions": [{"status": "Attached"}]}),
            json.dumps({"sessions": "everything"}),
            json.dumps({"sessions": [{"name": 17}]}),
        ):
            completed = self.run_cli(payload)
            self.assertEqual("0", completed.stdout.strip(), msg=payload)
            self.assertTrue((self.app_root / "s-abandoned").is_dir())

    def test_the_command_reports_what_it_removed(self) -> None:
        self.make_dir("s-abandoned")
        self.make_dir("s-attached")
        completed = self.run_cli(
            json.dumps(
                {
                    "sessions": [
                        {"name": "s-attached", "status": "Attached"},
                    ]
                }
            )
        )
        self.assertEqual("1", completed.stdout.strip())
        self.assertIn("App Server GC removed s-abandoned", completed.stderr)
        self.assertFalse((self.app_root / "s-abandoned").exists())
        self.assertTrue((self.app_root / "s-attached").is_dir())


class ReaperWiringTests(unittest.TestCase):
    """The sweep is only worth anything if the scheduled reaper runs it."""

    def setUp(self) -> None:
        # The fake shpool has to be executable, and some hosts mount /tmp
        # noexec, so the disposable tree lives on the repository filesystem.
        self.temporary = tempfile.TemporaryDirectory(prefix=".reap-", dir=REPO)
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.state = self.base / "st"
        self.proc = self.base / "proc"
        self.bin = self.base / "bin"
        for path in (self.home, self.state, self.proc, self.bin):
            path.mkdir(mode=0o700)
        self.app_root = self.state / "app-server"
        self.app_root.mkdir(mode=0o700)
        self.shpool = self.bin / "shpool"
        self.shpool.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ $1 == list && $2 == --json ]]; then printf \'%s\\n\' '
            "'{\"sessions\":[{\"name\":\"s-live\",\"status\":\"Attached\","
            '"started_at_unix_ms":1800000000000}]}\'; exit 0; fi\nexit 2\n',
            encoding="utf-8",
        )
        self.shpool.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_dir(self, name: str, age_seconds: float) -> Path:
        directory = self.app_root / name
        directory.mkdir(mode=0o700)
        socket_path = directory / "app.sock"
        dead_socket(socket_path)
        stamp = time.time() - age_seconds
        os.utime(socket_path, (stamp, stamp))
        os.utime(directory, (stamp, stamp))
        return directory

    def test_a_scheduled_report_run_sweeps_and_says_so(self) -> None:
        abandoned = self.make_dir("s-old", 4 * HOUR)
        listed = self.make_dir("s-live", 4 * HOUR)
        environment = os.environ.copy()
        environment.update(
            HOME=os.fspath(self.home),
            SESSION_KIT_STATE_DIR=os.fspath(self.state),
            SESSION_KIT_SHPOOL_CMD=os.fspath(self.shpool),
            SESSION_KIT_PROC_ROOT=os.fspath(self.proc),
            SESSION_KIT_DAEMON_PID="10",
            SESSION_KIT_REAPER_SENTINEL=os.fspath(self.base / "absent"),
            SESSION_KIT_TESTING="1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        completed = subprocess.run(
            [os.fspath(REPO / "bin" / "shpool_reaper"), "--candidates"],
            cwd=REPO,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
            timeout=60,
        )
        self.assertIn("expired_app_server_dirs=1", completed.stderr)
        self.assertFalse(abandoned.exists())
        self.assertTrue(listed.is_dir())


if __name__ == "__main__":
    unittest.main()
