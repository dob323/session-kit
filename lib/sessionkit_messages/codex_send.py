"""Deliver one operator envelope to one live Codex thread over its own socket.

The client is the hardened Unix-WebSocket pattern the coordination broker
already proved in production: the handshake is validated against the
computed ``Sec-WebSocket-Accept``, frames are capped, and the socket carries
short timeouts. Each target is reached ONLY through its own
``app-server/<shpool_id>/app.sock`` — one send never fans out across
sockets.

Phase 0 captured three facts this module is built around (codex-cli 0.145.0):
``turn/steer`` requires ``expectedTurnId``; a steer issued before the turn is
really running fails with "no active turn to steer"; and ``turn/completed``
never arrives, so delivery is recorded fire-and-forget and the reply comes
back through ``sp msg reply``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import socket as socketmod
import stat as statmod
import struct
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

MAX_FRAME = 1_048_576
SOCKET_TIMEOUT_SECONDS = 2.0
REQUEST_DEADLINE_SECONDS = 8.0
STEER_RETRY_DELAY_SECONDS = 1.5
IDLE_FALLBACK_SECONDS = 10.0
IDLE_POLL_SECONDS = 0.5
NO_SOCKET_DETAIL = "no app-server socket (plain-TUI Codex)"
NOT_LOADED_DETAIL = "thread not loaded on its socket"
UNKNOWN_OUTCOME_DETAIL = "dispatch attempted, outcome unknown"
CLIENT_INFO = {
    "name": "session-kit-msg",
    "title": "Session Kit operator message",
    "version": "1.0",
}


class AppServerError(RuntimeError):
    """The app server refused, closed, or never answered."""


def socket_path(state_dir: Path | str, shpool_id: object) -> Path | None:
    """The one socket a target may be reached on, or None when unsafe."""
    if not isinstance(shpool_id, str) or not shpool_id:
        return None
    if "/" in shpool_id or shpool_id.startswith(".") or len(shpool_id) > 128:
        return None
    return Path(state_dir) / "app-server" / shpool_id / "app.sock"


def live_socket(path: Path | None) -> bool:
    """True only for an existing, owner-held, non-symlink Unix socket."""
    if path is None:
        return False
    try:
        if path.is_symlink():
            return False
        metadata = os.stat(path)
    except OSError:
        return False
    return statmod.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid()


class AppServerClient:
    """A minimal JSON-RPC client over a Unix-socket WebSocket."""

    def __init__(self, path: Path, *, timeout: float = SOCKET_TIMEOUT_SECONDS):
        self.socket = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        self.socket.settimeout(timeout)
        try:
            self.socket.connect(os.fspath(path))
        except OSError as exc:
            self.close()
            raise AppServerError(f"app-server socket refused a connection: {exc}") from exc
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.socket.sendall(
            (
                "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        response = self._read_until(b"\r\n\r\n", 16_384)
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode("ascii")
        if not response.startswith(b"HTTP/1.1 101 ") or (
            f"sec-websocket-accept: {expected}".lower().encode()
            not in response.lower()
        ):
            self.close()
            raise AppServerError("app-server rejected the Unix WebSocket handshake")
        self.next_id = 1

    # ---- framing -------------------------------------------------------

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        data = bytearray()
        while marker not in data:
            try:
                chunk = self.socket.recv(4096)
            except OSError as exc:
                raise AppServerError(f"app-server handshake failed: {exc}") from exc
            if not chunk:
                raise AppServerError("app-server closed during the handshake")
            data.extend(chunk)
            if len(data) > limit:
                raise AppServerError("app-server handshake exceeded its limit")
        return bytes(data)

    def _read_exact(self, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            chunk = self.socket.recv(length - len(data))
            if not chunk:
                raise AppServerError("app-server closed the WebSocket")
            data.extend(chunk)
        return bytes(data)

    def send_frame(self, payload: bytes, opcode: int = 1) -> None:
        if len(payload) > MAX_FRAME:
            raise AppServerError("outbound WebSocket frame exceeded the limit")
        mask = os.urandom(4)
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def receive_frame(self) -> tuple[int | None, bytes]:
        """Read one frame, or (None, b"") when the socket was simply quiet.

        Only the two header bytes may time out: once a frame has started, a
        timeout would leave the stream desynchronised, so it is fatal rather
        than something to retry around.
        """
        try:
            header = self._read_exact(2)
        except socketmod.timeout:
            return None, b""
        first, second = header
        opcode = first & 0x0F
        if not first & 0x80:
            raise AppServerError("fragmented WebSocket frames are unsupported")
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > MAX_FRAME:
            raise AppServerError("inbound WebSocket frame exceeded the limit")
        mask = self._read_exact(4) if second & 0x80 else b""
        payload = self._read_exact(length)
        if mask:
            payload = bytes(
                value ^ mask[index % 4] for index, value in enumerate(payload)
            )
        return opcode, payload

    # ---- JSON-RPC ------------------------------------------------------

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        deadline_seconds: float = REQUEST_DEADLINE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """Send one request and return its result, or raise with the exact error.

        The server interleaves ``thread/status/changed`` notifications with
        replies, so unmatched messages are skipped rather than treated as the
        answer; the overall deadline bounds that loop.
        """
        request_id = self.next_id
        self.next_id += 1
        self.send_frame(
            json.dumps(
                {"method": method, "id": request_id, "params": dict(params or {})},
                separators=(",", ":"),
            ).encode()
        )
        end = monotonic() + deadline_seconds
        while monotonic() < end:
            try:
                opcode, payload = self.receive_frame()
            except OSError as exc:
                raise AppServerError(f"app-server {method} failed: {exc}") from exc
            if opcode is None:
                continue
            if opcode == 8:
                raise AppServerError(f"app-server closed the WebSocket during {method}")
            if opcode == 9:
                self.send_frame(payload, opcode=10)
                continue
            if opcode != 1:
                continue
            try:
                message = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if message.get("error") is not None:
                raise AppServerError(
                    f"{method}: {json.dumps(message['error'], sort_keys=True)}"
                )
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        raise AppServerError(f"app-server {method} timed out")

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        self.send_frame(
            json.dumps(
                {"method": method, "params": dict(params or {})},
                separators=(",", ":"),
            ).encode()
        )

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.socket.close()


def loaded_thread_ids(result: Mapping[str, Any]) -> set[str]:
    """Thread ids from ``thread/loaded/list``, tolerating both shapes."""
    values: Sequence[Any] = ()
    for field in ("data", "threads", "threadIds"):
        candidate = result.get(field)
        if isinstance(candidate, list):
            values = candidate
            break
    ids: set[str] = set()
    for value in values:
        if isinstance(value, str):
            ids.add(value.lower())
        elif isinstance(value, Mapping):
            for field in ("id", "threadId", "sessionId"):
                candidate = value.get(field)
                if isinstance(candidate, str):
                    ids.add(candidate.lower())
                    break
    return ids


def thread_status(result: Mapping[str, Any]) -> str:
    thread = result.get("thread")
    if not isinstance(thread, Mapping):
        return ""
    status = thread.get("status")
    if not isinstance(status, Mapping):
        return ""
    value = status.get("type")
    return value if isinstance(value, str) else ""


def active_turn_id(result: Mapping[str, Any]) -> str:
    """The newest in-progress turn id from ``thread/read includeTurns:true``."""
    thread = result.get("thread")
    if not isinstance(thread, Mapping):
        return ""
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return ""
    for turn in reversed(turns):
        if not isinstance(turn, Mapping):
            continue
        if turn.get("status") != "inProgress":
            continue
        identifier = turn.get("id")
        if isinstance(identifier, str) and identifier:
            return identifier
    return ""


def _input(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


def deliver(
    *,
    uuid: str,
    envelope: str,
    path: Path | None,
    connect: Callable[[Path], AppServerClient] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    idle_fallback_seconds: float = IDLE_FALLBACK_SECONDS,
) -> dict[str, str | None]:
    """Deliver one envelope to one thread and report the exact outcome.

    Never waits for ``turn/completed`` (Phase 0 proved it never arrives): the
    RPC reply is the receipt, and an indeterminate attempt is reported as
    ``failed`` rather than retried.
    """
    if not live_socket(path) or path is None:
        return {"status": "unreachable", "method": None, "detail": NO_SOCKET_DETAIL}
    opener = connect or (lambda target: AppServerClient(target))
    try:
        client = opener(path)
    except (AppServerError, OSError) as exc:
        return {
            "status": "unreachable",
            "method": None,
            "detail": f"{NO_SOCKET_DETAIL}: {exc}",
        }
    try:
        return _deliver_over(
            client,
            uuid=uuid,
            envelope=envelope,
            sleep=sleep,
            monotonic=monotonic,
            idle_fallback_seconds=idle_fallback_seconds,
        )
    finally:
        client.close()


def _deliver_over(
    client: AppServerClient,
    *,
    uuid: str,
    envelope: str,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    idle_fallback_seconds: float,
) -> dict[str, str | None]:
    try:
        client.request("initialize", {"clientInfo": CLIENT_INFO})
        client.notify("initialized", {})
        loaded = loaded_thread_ids(client.request("thread/loaded/list", {}))
    except (AppServerError, OSError) as exc:
        return {"status": "unreachable", "method": None, "detail": str(exc)}
    if uuid.lower() not in loaded:
        return {"status": "unreachable", "method": None, "detail": NOT_LOADED_DETAIL}
    try:
        status = thread_status(
            client.request("thread/read", {"threadId": uuid, "includeTurns": False})
        )
    except (AppServerError, OSError) as exc:
        return {"status": "failed", "method": None, "detail": str(exc)}

    if status == "idle":
        return _start_turn(client, uuid=uuid, envelope=envelope)
    return _steer_turn(
        client,
        uuid=uuid,
        envelope=envelope,
        sleep=sleep,
        monotonic=monotonic,
        idle_fallback_seconds=idle_fallback_seconds,
    )


def _start_turn(
    client: AppServerClient, *, uuid: str, envelope: str, method: str = "wake"
) -> dict[str, str | None]:
    try:
        client.request(
            "turn/start", {"threadId": uuid, "input": _input(envelope)},
        )
    except (AppServerError, OSError) as exc:
        return {"status": "failed", "method": method, "detail": str(exc)}
    return {
        "status": "delivered-woke",
        "method": method,
        "detail": "turn/start accepted on an idle thread",
    }


def _steer_turn(
    client: AppServerClient,
    *,
    uuid: str,
    envelope: str,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    idle_fallback_seconds: float,
) -> dict[str, str | None]:
    last_error = "no in-progress turn to steer"
    for attempt in range(2):
        if attempt:
            # The "no active turn to steer" race: the turn id exists before
            # the turn is steerable. One bounded retry, never a loop.
            sleep(STEER_RETRY_DELAY_SECONDS)
        try:
            turn_id = active_turn_id(
                client.request("thread/read", {"threadId": uuid, "includeTurns": True})
            )
        except (AppServerError, OSError) as exc:
            last_error = str(exc)
            continue
        if not turn_id:
            continue
        try:
            client.request(
                "turn/steer",
                {
                    "threadId": uuid,
                    "expectedTurnId": turn_id,
                    "input": _input(envelope),
                },
            )
        except (AppServerError, OSError) as exc:
            last_error = str(exc)
            continue
        return {
            "status": "delivered-midturn",
            "method": "steer",
            "detail": f"turn/steer accepted for turn {turn_id}",
        }
    outcome = _wait_for_idle_then_start(
        client,
        uuid=uuid,
        envelope=envelope,
        sleep=sleep,
        monotonic=monotonic,
        idle_fallback_seconds=idle_fallback_seconds,
    )
    if outcome is not None:
        return outcome
    return {"status": "failed", "method": "steer", "detail": last_error}


def _wait_for_idle_then_start(
    client: AppServerClient,
    *,
    uuid: str,
    envelope: str,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    idle_fallback_seconds: float,
) -> dict[str, str | None] | None:
    end = monotonic() + idle_fallback_seconds
    while True:
        try:
            status = thread_status(
                client.request(
                    "thread/read", {"threadId": uuid, "includeTurns": False}
                )
            )
        except (AppServerError, OSError):
            return None
        if status == "idle":
            return _start_turn(client, uuid=uuid, envelope=envelope)
        if monotonic() >= end:
            return None
        sleep(IDLE_POLL_SECONDS)
