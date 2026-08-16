"""Deliver one message into a RUNNING Claude Code session over its own socket.

Claude Code 2.1.224+ listens on a per-session Unix socket and Claude Code
2.1.228+ authenticates callers on it. That is an interface built for exactly
this: putting a line into a session a person is sitting in, without spawning a
second Claude to do the typing.

What it replaces
----------------
Today's Claude delivery starts a whole `claude -p` sender, tells it to use
SendMessage against a display name, and then reads the TARGET's transcript to
find out whether anything arrived. It costs a model turn per message, it can
only address sessions the registry lists, and its one failure sentence --
"not registered (possible trust prompt)" -- is printed for a missing binary, a
dead session, a refused socket and a real trust prompt alike. All twenty-one
undelivered events in the last audit carried that sentence; seven were measured
and none of them was a trust prompt.

This client keeps the spawned sender available as the fallback and makes every
outcome name its own cause.

The protocol, as read off the running binary (2.1.229, 2026-08-13)
------------------------------------------------------------------
* The session publishes its socket path in its own registry record,
  ``~/.claude/sessions/<pid>.json`` → ``messagingSocketPath``. The default path
  is ``$XDG_RUNTIME_DIR/cc-socks/<pid>.sock``, mode 0600.
* Auth: the session writes a peer token to
  ``<config dir>/sessions/<pid>.<sha256 of the resolved socket path>.key``,
  mode 0600, as ``{"peerToken": "<32 hex>", "procStart": "..."}``. A caller
  sends ``{"type":"auth","token":"<peerToken>"}`` as its FIRST line; when auth
  is required and the first line is anything else, the daemon logs a drop and
  destroys the connection without a word on the wire.
* Messages are JSON lines. A user message is
  ``{"type":"user","message":{"content":"..."},"session_id":"<uuid>", …}``.
  ``session_id`` is checked against the receiver's own id and the message is
  dropped on a mismatch -- which is what makes a recycled pid safe to address.
* Receipts travel back the same way: the receiver connects to the socket named
  in the sender's ``from`` address and writes
  ``{"type":"control","action":"peer_message_status","orig_msg_id":…,
  "status":"delivered"|"held"|"denied"|"expired"}``. So a caller that binds its
  own socket in the same directory gets a real cause -- "held" means the target
  is asking its operator to approve the message, which is a different world
  from "denied".

MEASURED, not assumed (drill against a live 2.1.229 interactive session,
2026-08-13): a message the target ACCEPTS produces no receipt at all. The
session rendered the injected prompt and answered it, and nothing was written
back to the sender's socket. So the receipt channel is, in practice, how a
message that did NOT go through says why; silence on it is not proof of
anything. This client therefore never reports ``delivered`` without an explicit
receipt: an accepted-looking send returns ``sent-no-receipt`` and carries the
``message_uuid`` it used, which is the id the message keeps in the target's own
transcript -- the caller's existing receipt of record confirms it from there.
Claiming delivery on the sender's own say-so is exactly the mistake the
spawned-sender path made.

Nothing here writes to a session's files, and nothing here can start a session.

WHERE THE TOKEN MAY GO. The socket path comes out of a FILE -- the session's
own registry record -- and this module carries the one secret in the round. So
a target is proved to be ours before anything is dialled: the socket must be
owned by this euid, must not be group- or world-writable, must live under this
user's runtime directory, must be reached through no symlink, and its
containing directory must be owned by this euid and writable by nobody else.
Anything else is ``foreign-socket``: no connect, no auth line, no message, no
receipt socket bound beside it. The check is at :func:`_socket_problem`, which
is the one place the capability probe, the send, the rename and the
stale-record disambiguation all pass through, so no path can skip it.

Kill switch: ``SESSION_KIT_CLAUDE_SOCKET=0`` makes :func:`capability` report
the channel off, which is how a caller falls back to the spawned sender
without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import socket as socketmod
import stat as statmod
import sys
import tempfile
import time
from typing import Any, Mapping
import uuid as uuidmod


UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_TEXT_CHARS = 100_000
MAX_RECORD_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 600.0
# How long to wait for a refusal after the auth line. The daemon decides on the
# first frame, so this is a network-free round trip on a local socket; a second
# is generous and it is only ever paid once per send.
AUTH_PROBE_SECONDS = 1.0
PRIORITIES = ("now", "next", "later")

# Every way this can end, each meaning one thing. The whole point of the
# module is that a caller never has to guess which of these happened.
DELIVERED = "delivered"
HELD = "held"
DENIED = "denied"
EXPIRED = "expired"
SENT_NO_RECEIPT = "sent-no-receipt"
CLOSED_AFTER_SEND = "closed-after-send"
RENAME_SENT = "rename-sent"
NO_SESSION_RECORD = "no-session-record"
NO_SOCKET_PATH = "no-socket-path"
SOCKET_MISSING = "socket-missing"
NOT_A_SOCKET = "not-a-socket"
FOREIGN_SOCKET = "foreign-socket"
SOCKET_REFUSED = "socket-refused"
PERMISSION_DENIED = "permission-denied"
NO_AUTH_KEY = "no-auth-key"
AUTH_REJECTED = "auth-rejected"
CHANNEL_OFF = "channel-off"
BAD_REQUEST = "bad-request"
SOCKET_ERROR = "socket-error"

# The whole list, in one place, so "how many outcomes are there" has one
# answer. DELIVERED is the only one that is not a failure; every other name
# below carries its own sentence in FAILURE_DETAIL.
OUTCOMES = (
    DELIVERED,
    HELD,
    DENIED,
    EXPIRED,
    SENT_NO_RECEIPT,
    CLOSED_AFTER_SEND,
    RENAME_SENT,
    NO_SESSION_RECORD,
    NO_SOCKET_PATH,
    SOCKET_MISSING,
    NOT_A_SOCKET,
    FOREIGN_SOCKET,
    SOCKET_REFUSED,
    PERMISSION_DENIED,
    NO_AUTH_KEY,
    AUTH_REJECTED,
    CHANNEL_OFF,
    BAD_REQUEST,
    SOCKET_ERROR,
)

FAILURE_DETAIL = {
    NO_SESSION_RECORD: (
        "Claude has no session record for this conversation on this machine"
    ),
    NO_SOCKET_PATH: (
        "the session's record names no messaging socket "
        "(older Claude Code, or started with --bare)"
    ),
    SOCKET_MISSING: "the session's messaging socket is not on disk",
    NOT_A_SOCKET: "the messaging socket path is not a socket",
    # Never a connect and never a token: the record named a socket this user
    # cannot prove is one of their own sessions', so nothing was written to it.
    FOREIGN_SOCKET: (
        "the record names a socket this user does not own, or one outside this "
        "user's runtime directory; nothing was sent to it"
    ),
    # Two paths end here and the sentence has to be true for both: a connect
    # that was refused outright (the session is gone, its socket file left
    # behind), and a peer that took the token and then went away before the
    # message itself. Nothing was written in either case.
    SOCKET_REFUSED: (
        "the messaging socket stopped answering before the message went in; "
        "nothing was written, so it is safe to send another way"
    ),
    PERMISSION_DENIED: "this user may not connect to that session's socket",
    NO_AUTH_KEY: (
        "the session published no auth key; a message sent now would be dropped "
        "unauthenticated"
    ),
    AUTH_REJECTED: "the session refused the auth token and closed the connection",
    SENT_NO_RECEIPT: "the message was written to the socket but nothing confirmed it",
    CLOSED_AFTER_SEND: (
        "the message was written and the session then closed the connection; "
        "whether it landed is the target's transcript to answer, and sending it "
        "again would risk delivering it twice"
    ),
    RENAME_SENT: (
        "the rename was written to the session's control channel; the session "
        "sends nothing back, so the next snapshot is what shows the new name"
    ),
    HELD: (
        "the session is holding the message for its operator to approve "
        "(crossSessionInbound)"
    ),
    DENIED: "the session's operator declined the message",
    EXPIRED: "the session shut down while still holding the message",
    CHANNEL_OFF: "the socket channel is switched off (SESSION_KIT_CLAUDE_SOCKET=0)",
    BAD_REQUEST: "the message could not be built",
    SOCKET_ERROR: "the socket could not be used",
}


@dataclass
class Outcome:
    delivered: bool
    how: str
    detail: str
    session_id: str = ""
    pid: int | None = None
    socket_path: str = ""
    # "The session did not refuse our token before the body was written" -- NOT
    # "the session confirmed our token". The vendor answers a bad token by
    # destroying the connection and saying nothing, so silence is the only
    # evidence there is, and silence is not proof. `auth_confirmed` in `extra`
    # is the positive form: it is True only when a receipt came back.
    authenticated: bool = False
    receipt: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        record = {
            "delivered": self.delivered,
            "how": self.how,
            "detail": self.detail,
            "session_id": self.session_id,
            "pid": self.pid,
            "socket_path": self.socket_path,
            "authenticated": self.authenticated,
            "receipt": self.receipt,
        }
        record.update(self.extra)
        return record


def _failure(how: str, **kwargs: Any) -> Outcome:
    return Outcome(
        delivered=False, how=how, detail=FAILURE_DETAIL.get(how, how), **kwargs
    )


def _valid_uuid(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if UUID_PATTERN.match(text) else ""


def claude_config_dir(environ: Mapping[str, str], home: Path) -> Path:
    configured = environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else home / ".claude"


def channel_enabled(environ: Mapping[str, str]) -> bool:
    value = (environ.get("SESSION_KIT_CLAUDE_SOCKET") or "").strip().casefold()
    return value not in {"0", "off", "no", "false"}


def _read_json(path: Path, *, no_follow: bool = False) -> Any:
    """One bounded JSON file, or None.

    ``no_follow`` is for the auth key: the token is the one secret this module
    touches, and a symlink where the vendor publishes a key is not a key. The
    writer of our own records is equally careful (attention.write_record).
    """
    try:
        if no_follow:
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            with os.fdopen(descriptor, "rb") as handle:
                payload = handle.read(MAX_RECORD_BYTES + 1)
        else:
            with open(path, "rb") as handle:
                payload = handle.read(MAX_RECORD_BYTES + 1)
    except OSError:
        return None
    if len(payload) > MAX_RECORD_BYTES:
        return None
    try:
        return json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError):
        return None


@dataclass
class Target:
    session_id: str
    pid: int
    socket_path: Path
    record_path: Path
    version: str = ""
    kind: str = ""
    status: str = ""


def find_target(
    session_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    config_dir: Path | None = None,
) -> Target | Outcome:
    """The live session record for one conversation, or the reason there is none.

    The record is the vendor's own registry, one file per PID, and it carries
    the socket path. Reading it (rather than running `claude agents --json`)
    keeps this path free of a subprocess that restarts a daemon.
    """
    exact = _valid_uuid(session_id)
    if not exact:
        return _failure(BAD_REQUEST, session_id=str(session_id or ""))
    environ = environ if environ is not None else os.environ
    home = home if home is not None else Path.home()
    root = config_dir if config_dir is not None else claude_config_dir(environ, home)
    sessions = root / "sessions"
    try:
        candidates = sorted(sessions.glob("*.json"))
    except OSError:
        candidates = []
    found: list[Target] = []
    incomplete: Outcome | None = None
    for path in candidates:
        if path.is_symlink() or not path.name[:-5].isdigit():
            continue
        record = _read_json(path)
        if not isinstance(record, Mapping):
            continue
        if _valid_uuid(record.get("sessionId")) != exact:
            continue
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        socket_path = record.get("messagingSocketPath")
        if not isinstance(socket_path, str) or not socket_path:
            # Remember it, but keep looking: a crash-and-resume leaves the old
            # record on disk beside the live one, and the dead one must not be
            # allowed to answer for the conversation.
            if incomplete is None:
                incomplete = _failure(NO_SOCKET_PATH, session_id=exact, pid=pid)
            continue
        found.append(
            Target(
                session_id=exact,
                pid=pid,
                socket_path=Path(socket_path),
                record_path=path,
                version=str(record.get("version") or ""),
                kind=str(record.get("kind") or ""),
                status=str(record.get("status") or ""),
            )
        )
    if not found:
        return incomplete or _failure(NO_SESSION_RECORD, session_id=exact)
    if len(found) == 1:
        return found[0]
    # Same conversation, several records. The one whose socket ANSWERS is the
    # live session; the others are what a crash left behind. Highest pid first,
    # because a resumed session is the newer process -- but the connect is what
    # decides, not the ordering.
    found.sort(key=lambda item: item.pid, reverse=True)
    for target in found:
        # The gate first: a stale-record probe is still a connect, and a
        # record planted to be the "live" one must not get one either.
        if _socket_problem(target.socket_path, environ=environ):
            continue
        probe = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(os.fspath(target.socket_path))
        except OSError:
            continue
        finally:
            probe.close()
        return target
    return found[0]


def auth_key_path(target: Target, *, config_dir: Path) -> Path:
    """Where the session publishes the token a peer must present.

    Named ``<pid>.<sha256 of the socket path>.key``. The vendor hashes
    ``path.resolve(...)``, which normalises the path but does NOT follow
    symlinks, so this normalises the same way rather than calling ``realpath``:
    on a platform where the runtime directory has a symlinked component -- macOS
    ``/tmp`` and ``/var``, which the vendor's own fallback path uses -- resolving
    the link would produce a key name that does not exist, and the channel would
    report ``no-auth-key`` forever with nothing to point at.
    """
    normalised = os.path.normpath(os.path.abspath(os.fspath(target.socket_path)))
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
    return config_dir / "sessions" / f"{target.pid}.{digest}.key"


def read_peer_token(target: Target, *, config_dir: Path) -> str:
    record = _read_json(auth_key_path(target, config_dir=config_dir), no_follow=True)
    if not isinstance(record, Mapping):
        return ""
    token = record.get("peerToken")
    if isinstance(token, str) and TOKEN_PATTERN.match(token):
        return token
    return ""


def capability(
    session_id: str = "",
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Can this box deliver over the socket -- and to this session in particular.

    Answered from files only: no connection is made, nothing is sent. A caller
    uses it to choose a channel before it has a message to lose.
    """
    environ = environ if environ is not None else os.environ
    home = home if home is not None else Path.home()
    root = config_dir if config_dir is not None else claude_config_dir(environ, home)
    report: dict[str, Any] = {
        "available": False,
        "enabled": channel_enabled(environ),
        "reason": "",
        "session_id": _valid_uuid(session_id),
        "socket_path": "",
        "authenticated": False,
    }
    if not report["enabled"]:
        report["reason"] = CHANNEL_OFF
        return report
    if not report["session_id"]:
        # No session named: report only whether the machine has the channel at
        # all, which is true when any live record publishes a socket THIS gate
        # would let us dial. A record naming somebody else's socket is not a
        # channel this box has -- answering yes on one would send a caller down
        # a path every later step refuses.
        try:
            records = sorted((root / "sessions").glob("*.json"))
        except OSError:
            records = []
        for path in records:
            record = _read_json(path)
            if not isinstance(record, Mapping):
                continue
            published = record.get("messagingSocketPath")
            if not isinstance(published, str) or not published:
                continue
            if _socket_problem(Path(published), environ=environ) in {
                "",
                SOCKET_MISSING,
            }:
                # SOCKET_MISSING is a session that has since exited, not a
                # foreign target: the box still has the channel.
                report["available"] = True
                return report
        report["reason"] = NO_SOCKET_PATH
        return report
    found = find_target(
        session_id, environ=environ, home=home, config_dir=root
    )
    if isinstance(found, Outcome):
        report["reason"] = found.how
        return report
    report["socket_path"] = os.fspath(found.socket_path)
    report["pid"] = found.pid
    problem = _socket_problem(found.socket_path, environ=environ)
    if problem:
        report["reason"] = problem
        return report
    report["authenticated"] = bool(read_peer_token(found, config_dir=root))
    # Without a key there is nothing to authenticate with, and send() refuses
    # rather than writing blind -- so "available" would be a yes a caller
    # cannot use. It reports the same cause the send would.
    report["available"] = report["authenticated"]
    if not report["authenticated"]:
        report["reason"] = NO_AUTH_KEY
    return report


def _bounded_seconds(timeout: object) -> float:
    """Any caller's timeout, made finite. Infinity is not a waiting time."""
    import math

    try:
        seconds = float(timeout)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(seconds):
        return DEFAULT_TIMEOUT_SECONDS
    return min(MAX_TIMEOUT_SECONDS, max(1.0, seconds))


def _peer_closed(connection: socketmod.socket, *, timeout: float) -> bool:
    """Has the peer closed its end? A read that ends with nothing says so."""
    previous = connection.gettimeout()
    connection.settimeout(max(0.05, float(timeout)))
    try:
        return connection.recv(1) == b""
    except (socketmod.timeout, TimeoutError, BlockingIOError):
        return False
    except OSError:
        return True
    finally:
        try:
            connection.settimeout(previous)
        except OSError:
            pass


def _write_auth(connection: socketmod.socket, token: str) -> bool:
    """Send the auth line alone. True when the session refused it.

    Refusal is silent by design (the daemon destroys the connection), so the
    probe is a short read that ends. Nothing else has been written at this
    point, which is what makes a True answer safe to retry on another channel.
    """
    try:
        connection.sendall(
            json.dumps({"type": "auth", "token": token}).encode("utf-8") + b"\n"
        )
    except OSError:
        return True
    return _peer_closed(connection, timeout=AUTH_PROBE_SECONDS)


def _allowed_socket_roots(environ: Mapping[str, str]) -> list[Path]:
    """The directories a session's own messaging socket may live in.

    The vendor's documented path is ``$XDG_RUNTIME_DIR/cc-socks/<pid>.sock``
    (module docstring), so the runtime directory is the root. It is read from
    the environment when it is there and derived when it is not: a cron or a
    non-login shell -- which is what runs the fleet's delivery -- does not
    always export it, and an empty root list would switch the channel off for
    every real session rather than refusing a fake one. macOS has no
    ``/run/user`` and no ``XDG_RUNTIME_DIR``; its per-user ``$TMPDIR`` (0700,
    owned by the user) is where the vendor's fallback path goes, and it is
    still held to every ownership and mode check below.
    """
    roots: list[Path] = []
    configured = str(environ.get("XDG_RUNTIME_DIR") or "").strip()
    if configured:
        roots.append(Path(configured))
        return roots
    default = Path(f"/run/user/{os.geteuid()}")
    if default.is_dir():
        roots.append(default)
    if sys.platform == "darwin":
        temporary = str(environ.get("TMPDIR") or "").strip()
        if temporary:
            roots.append(Path(temporary))
    return roots


def _absolute(path: Path) -> Path:
    """Textually absolute and normalised -- no symlink is followed here.

    ``..`` is removed by text, which is what makes "is it under the root"
    answerable; a component that is a symlink is then rejected outright by
    :func:`_symlink_between`, so the two together cannot be walked around.
    """
    return Path(os.path.normpath(os.path.abspath(os.fspath(path))))


def _under(child: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath([os.fspath(child), os.fspath(root)])
    except (ValueError, OSError):
        return False
    return common == os.fspath(root) and child != root


def _containing_root(absolute: Path, environ: Mapping[str, str]) -> Path | None:
    """Which allowed root holds this path, in its own or its resolved form.

    Both forms are tried because a platform may publish either: macOS's
    ``/var`` and ``/tmp`` are symlinks into ``/private``, so the root as
    configured and the root as resolved are two different strings for one
    directory. Resolving the ROOT is safe; resolving the path below it is not,
    which is why only the root is put through ``realpath``.
    """
    for root in _allowed_socket_roots(environ):
        raw = _absolute(root)
        candidates = [raw]
        try:
            resolved = Path(os.path.realpath(os.fspath(raw)))
        except OSError:
            resolved = raw
        if resolved != raw:
            candidates.append(resolved)
        for candidate in candidates:
            if _under(absolute, candidate):
                return candidate
    return None


def _symlink_between(root: Path, absolute: Path) -> bool:
    """True when any component below the root is a symlink.

    Walked one component at a time from the root down, so a link planted
    anywhere on the way -- ``$XDG_RUNTIME_DIR/cc-socks`` replaced by a link
    into /tmp, or the socket name itself being a link -- is caught. The root
    itself is not examined: it is allowed to be a symlinked system path
    (macOS), and it is not a component an attacker gets to add.
    """
    try:
        relative = os.path.relpath(os.fspath(absolute), os.fspath(root))
    except (ValueError, OSError):
        return True
    current = root
    for part in Path(relative).parts:
        if part in ("..", "/"):
            return True
        current = current / part
        try:
            if os.path.islink(current):
                return True
        except OSError:
            return True
    return False


def _foreign_reason(path: Path, info: os.stat_result, environ: Mapping[str, str]) -> str:
    """Empty when this socket is provably one of THIS user's own sessions'.

    Empty is the only answer that lets a connect happen; the sentence returned
    otherwise is for a test or a person reading a probe, never for the wire.
    """
    euid = os.geteuid()
    if info.st_uid != euid:
        return "the socket is owned by another user"
    if info.st_mode & (statmod.S_IWGRP | statmod.S_IWOTH):
        return "the socket is writable by other users"
    absolute = _absolute(path)
    root = _containing_root(absolute, environ)
    if root is None:
        return "the socket is outside this user's runtime directory"
    if _symlink_between(root, absolute):
        return "a symlink stands between the runtime directory and the socket"
    try:
        directory = os.stat(absolute.parent)
    except OSError:
        return "the socket's directory cannot be read"
    if directory.st_uid != euid:
        return "the socket's directory is owned by another user"
    if directory.st_mode & (statmod.S_IWGRP | statmod.S_IWOTH):
        return "the socket's directory is writable by other users"
    return ""


def _socket_problem(path: Path, *, environ: Mapping[str, str] | None = None) -> str:
    """The one gate every path to the wire passes through.

    ``capability``, ``send``, ``rename`` and ``find_target``'s stale-record
    probe all call this before they touch the socket, which is what makes the
    ownership check impossible to skip: there is no second route to a connect.
    """
    environ = environ if environ is not None else os.environ
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return SOCKET_MISSING
    except PermissionError:
        return PERMISSION_DENIED
    except OSError:
        return SOCKET_MISSING
    if statmod.S_ISLNK(info.st_mode):
        # A session publishes its own socket, not a link to one. Refused before
        # the target is even looked at, so what it points to never matters.
        return FOREIGN_SOCKET
    if not statmod.S_ISSOCK(info.st_mode):
        return NOT_A_SOCKET
    if _foreign_reason(path, info, environ):
        return FOREIGN_SOCKET
    return ""


class _ReceiptListener:
    """Our own socket, so the target has somewhere to send the receipt.

    The receiver only answers into its own namespace -- same directory as its
    socket, name ending in .sock -- so this binds beside the target's socket
    and removes itself afterwards. That directory is only ever the target's,
    and a target only exists after _socket_problem proved the directory is
    owned by this euid and writable by nobody else; the umask below closes the
    remaining instant between bind and chmod, so the socket is never reachable
    at the process umask even for the length of one syscall.
    """

    def __init__(self, directory: Path) -> None:
        self.path: Path | None = None
        self.socket: socketmod.socket | None = None
        self.directory = directory

    def __enter__(self) -> "_ReceiptListener":
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(
                prefix="session-kit-receipt.", suffix=".sock", dir=self.directory
            )
            os.close(handle)
            os.unlink(name)
            listener = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
            # bind() takes the mode from the umask, so a permissive one would
            # publish this socket world-writable until the chmod lands. The
            # chmod stays as the belt: this is the brace.
            previous_umask = os.umask(0o077)
            try:
                listener.bind(name)
            finally:
                os.umask(previous_umask)
            os.chmod(name, 0o600)
            listener.listen(4)
            listener.setblocking(False)
            self.path = Path(name)
            self.socket = listener
        except OSError:
            self.close()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None
        if self.path is not None:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.path = None

    def address(self) -> str:
        return f"uds:{self.path}" if self.path is not None else ""

    def wait_for_status(self, msg_id: str, deadline: float) -> str:
        """The status the target reports for this exact message, or ""."""
        if self.socket is None:
            return ""
        import selectors

        selector = selectors.DefaultSelector()
        selector.register(self.socket, selectors.EVENT_READ)
        buffers: dict[socketmod.socket, bytes] = {}
        try:
            while time.monotonic() < deadline:
                for key, _ in selector.select(
                    timeout=max(0.05, min(0.5, deadline - time.monotonic()))
                ):
                    if key.fileobj is self.socket:
                        try:
                            connection, _ = self.socket.accept()
                        except OSError:
                            continue
                        connection.setblocking(False)
                        buffers[connection] = b""
                        selector.register(connection, selectors.EVENT_READ)
                        continue
                    connection = key.fileobj  # type: ignore[assignment]
                    try:
                        block = connection.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        block = b""
                    if not block:
                        selector.unregister(connection)
                        buffers.pop(connection, None)
                        connection.close()
                        continue
                    buffers[connection] = (buffers.get(connection, b"") + block)[
                        :MAX_RECORD_BYTES
                    ]
                    while b"\n" in buffers[connection]:
                        line, _, rest = buffers[connection].partition(b"\n")
                        buffers[connection] = rest
                        status = _receipt_status(line, msg_id)
                        if status:
                            return status
            return ""
        finally:
            for connection in list(buffers):
                try:
                    selector.unregister(connection)
                except (KeyError, ValueError):
                    pass
                connection.close()
            selector.close()


def _receipt_status(line: bytes, msg_id: str) -> str:
    try:
        record = json.loads(line.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(record, Mapping):
        return ""
    if record.get("type") != "control":
        return ""
    if record.get("action") != "peer_message_status":
        return ""
    if str(record.get("orig_msg_id") or "") != msg_id:
        return ""
    status = str(record.get("status") or "")
    return status if status in {DELIVERED, HELD, DENIED, EXPIRED} else ""


def send(
    session_id: str,
    text: str,
    *,
    priority: str = "next",
    sender: str = "session-kit",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    want_receipt: bool = True,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    config_dir: Path | None = None,
) -> Outcome:
    """Put one message into a running session. Every failure names itself."""
    environ = environ if environ is not None else os.environ
    home = home if home is not None else Path.home()
    root = config_dir if config_dir is not None else claude_config_dir(environ, home)
    if not channel_enabled(environ):
        return _failure(CHANNEL_OFF, session_id=_valid_uuid(session_id))
    if not isinstance(text, str) or not text.strip():
        return _failure(BAD_REQUEST, session_id=_valid_uuid(session_id))
    if len(text) > MAX_TEXT_CHARS:
        return _failure(BAD_REQUEST, session_id=_valid_uuid(session_id))
    if priority not in PRIORITIES:
        priority = "next"
    found = find_target(session_id, environ=environ, home=home, config_dir=root)
    if isinstance(found, Outcome):
        return found
    # Before the key is even read: a foreign target never gets a connect, and
    # the token is never carried past this line for one.
    problem = _socket_problem(found.socket_path, environ=environ)
    if problem:
        return _failure(
            problem,
            session_id=found.session_id,
            pid=found.pid,
            socket_path=os.fspath(found.socket_path),
        )
    token = read_peer_token(found, config_dir=root)
    if not token:
        # Sending anyway would be dropped by any 2.1.228+ session and look
        # exactly like a session that ignored us.
        return _failure(
            NO_AUTH_KEY,
            session_id=found.session_id,
            pid=found.pid,
            socket_path=os.fspath(found.socket_path),
        )
    msg_id = uuidmod.uuid4().hex
    # The id the message keeps once the target queues it, so a caller can find
    # this exact message in the target's own transcript.
    message_uuid = str(uuidmod.uuid4())
    deadline = time.monotonic() + _bounded_seconds(timeout)
    listener = _ReceiptListener(found.socket_path.parent)
    with listener:
        message = {
            "type": "user",
            "message": {"content": text},
            # Checked against the receiver's own id: a recycled pid cannot be
            # handed a message meant for the conversation that used to own it.
            "session_id": found.session_id,
            "uuid": message_uuid,
            "msg_id": msg_id,
            "priority": priority,
            "from": listener.address() or sender,
        }
        connection = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        connection.settimeout(min(10.0, _bounded_seconds(timeout)))
        try:
            connection.connect(os.fspath(found.socket_path))
        except ConnectionRefusedError:
            connection.close()
            return _failure(
                SOCKET_REFUSED,
                session_id=found.session_id,
                pid=found.pid,
                socket_path=os.fspath(found.socket_path),
            )
        except PermissionError:
            connection.close()
            return _failure(
                PERMISSION_DENIED,
                session_id=found.session_id,
                pid=found.pid,
                socket_path=os.fspath(found.socket_path),
            )
        except OSError as error:
            connection.close()
            outcome = _failure(
                SOCKET_ERROR,
                session_id=found.session_id,
                pid=found.pid,
                socket_path=os.fspath(found.socket_path),
            )
            outcome.detail = f"{FAILURE_DETAIL[SOCKET_ERROR]}: {error}"
            return outcome
        # THE AUTH WINDOW AND THE SEND WINDOW ARE SEPARATE, and this is the
        # whole safety of the channel. The daemon answers a bad token by
        # destroying the connection without a word, so "the peer closed" is the
        # only refusal signal there is -- but it is also what a peer does after
        # accepting a message, or when the session exits a second later. Sent
        # together, one close would have to mean both "refused, safe to resend"
        # and "accepted, resending would deliver it twice" (B13/C13).
        #
        # So the auth line goes first, ALONE, and the body is written only if
        # the connection is still open. A close before the body is a refusal
        # and nothing was sent. A close after it is never a refusal again.
        rejected = _write_auth(connection, token)
        if rejected:
            connection.close()
            return _failure(
                AUTH_REJECTED,
                session_id=found.session_id,
                pid=found.pid,
                socket_path=os.fspath(found.socket_path),
                authenticated=False,
                extra={"sent": False},
            )
        try:
            connection.sendall(json.dumps(message).encode("utf-8") + b"\n")
        except BrokenPipeError:
            # The peer went away between the auth probe and the body: the
            # message did NOT go in, so this is safe to retry elsewhere -- and
            # it is not an auth refusal either, because the token was accepted.
            connection.close()
            return _failure(
                SOCKET_REFUSED,
                session_id=found.session_id,
                pid=found.pid,
                socket_path=os.fspath(found.socket_path),
                authenticated=True,
                extra={"sent": False},
            )
        except OSError as error:
            connection.close()
            outcome = _failure(
                SOCKET_ERROR,
                session_id=found.session_id,
                pid=found.pid,
                socket_path=os.fspath(found.socket_path),
                authenticated=True,
                extra={"sent": False},
            )
            outcome.detail = f"{FAILURE_DETAIL[SOCKET_ERROR]}: {error}"
            return outcome
        status = ""
        if want_receipt and listener.path is not None:
            status = listener.wait_for_status(msg_id, deadline)
        # After the body is on the wire, a closed connection says only that the
        # conversation with the socket is over -- never that the message was
        # refused. Whether it landed is the transcript's answer, not ours.
        closed_after_send = _peer_closed(connection, timeout=0.2)
        connection.close()
    if status == DELIVERED:
        return Outcome(
            delivered=True,
            how=DELIVERED,
            detail="the session confirmed the message reached its queue",
            session_id=found.session_id,
            pid=found.pid,
            socket_path=os.fspath(found.socket_path),
            authenticated=True,
            receipt=status,
            extra={"message_uuid": message_uuid, "sent": True, "auth_confirmed": True},
        )
    if status in {HELD, DENIED, EXPIRED}:
        return _failure(
            status,
            session_id=found.session_id,
            pid=found.pid,
            socket_path=os.fspath(found.socket_path),
            authenticated=True,
            receipt=status,
            extra={"message_uuid": message_uuid, "sent": True, "auth_confirmed": True},
        )
    # Written, authenticated, nothing came back. Never reported as delivered:
    # the receipt is the only proof, and a caller that must be sure confirms it
    # from the target's own transcript rather than sending it again.
    return _failure(
        CLOSED_AFTER_SEND if closed_after_send else SENT_NO_RECEIPT,
        session_id=found.session_id,
        pid=found.pid,
        socket_path=os.fspath(found.socket_path),
        authenticated=True,
        extra={
            "message_uuid": message_uuid,
            "sent": True,
            # Nothing came back, so the token was never positively confirmed --
            # a late close cannot be told apart from a session that accepted
            # the message and then finished with the connection.
            "auth_confirmed": False,
        },
    )


def rename(
    session_id: str,
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    config_dir: Path | None = None,
) -> Outcome:
    """Rename a RUNNING session through the vendor's own control channel.

    The kit's name push appends an ``agent-name`` record to the session's
    transcript -- a file both vendors document as internal (see
    docs/pinned-internal-formats.md). This is the supported way to say the same
    thing, and it reaches a session that is already on screen rather than the
    next one that starts.

    The receiver applies the name and sends nothing back, so this call can
    never report a rename as done: a successful return means the frame was
    written to a session that accepted our token, and the name itself is
    confirmed by the next inventory snapshot. ``delivered`` stays False for the
    same reason it does on the send path -- a module whose thesis is "never
    claim delivery without a receipt" may not make one exception for itself.
    """
    environ = environ if environ is not None else os.environ
    home = home if home is not None else Path.home()
    root = config_dir if config_dir is not None else claude_config_dir(environ, home)
    if not channel_enabled(environ):
        return _failure(CHANNEL_OFF, session_id=_valid_uuid(session_id))
    cleaned = " ".join(str(name or "").split())[:120]
    if not cleaned:
        return _failure(BAD_REQUEST, session_id=_valid_uuid(session_id))
    found = find_target(session_id, environ=environ, home=home, config_dir=root)
    if isinstance(found, Outcome):
        return found
    problem = _socket_problem(found.socket_path, environ=environ)
    if problem:
        return _failure(
            problem,
            session_id=found.session_id,
            pid=found.pid,
            socket_path=os.fspath(found.socket_path),
        )
    token = read_peer_token(found, config_dir=root)
    if not token:
        return _failure(
            NO_AUTH_KEY,
            session_id=found.session_id,
            pid=found.pid,
            socket_path=os.fspath(found.socket_path),
        )
    connection = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
    connection.settimeout(5.0)
    try:
        connection.connect(os.fspath(found.socket_path))
        # Same two windows as send(): the token is offered alone, and the
        # control frame follows only if the session kept the connection.
        if _write_auth(connection, token):
            connection.close()
            return _failure(
                AUTH_REJECTED,
                session_id=found.session_id,
                pid=found.pid,
                socket_path=os.fspath(found.socket_path),
                extra={"sent": False},
            )
        connection.sendall(
            json.dumps(
                {
                    "type": "control",
                    "action": "rename",
                    "name": cleaned,
                    "session_id": found.session_id,
                }
            ).encode("utf-8")
            + b"\n"
        )
    except ConnectionRefusedError:
        return _failure(
            SOCKET_REFUSED,
            session_id=found.session_id,
            pid=found.pid,
            socket_path=os.fspath(found.socket_path),
        )
    except OSError as error:
        outcome = _failure(
            SOCKET_ERROR,
            session_id=found.session_id,
            pid=found.pid,
            socket_path=os.fspath(found.socket_path),
        )
        outcome.detail = f"{FAILURE_DETAIL[SOCKET_ERROR]}: {error}"
        return outcome
    finally:
        connection.close()
    return _failure(
        RENAME_SENT,
        session_id=found.session_id,
        pid=found.pid,
        socket_path=os.fspath(found.socket_path),
        authenticated=True,
        extra={"name": cleaned, "sent": True},
    )


def _timeout_argument(value: str) -> float:
    """A timeout has to be a real, bounded number of seconds.

    `float("inf")` parses, and an infinite deadline makes the receipt wait --
    the ordinary accepted-but-no-receipt path -- run until something kills the
    process. A refusal here costs one error message; the alternative costs a
    hung delivery nobody is watching.
    """
    import argparse
    import math

    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not a number of seconds: {value!r}")
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"seconds must be above 0 and at most {MAX_TIMEOUT_SECONDS:g}"
        )
    return seconds


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="claude_socket",
        description="Deliver one message into a running Claude Code session.",
    )
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--text")
    parser.add_argument("--priority", default="next", choices=PRIORITIES)
    parser.add_argument(
        "--timeout", type=_timeout_argument, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument("--no-receipt", action="store_true")
    parser.add_argument(
        "--capability",
        action="store_true",
        help="report whether this session can be reached, and send nothing",
    )
    arguments = parser.parse_args(argv)
    if arguments.capability:
        print(json.dumps(capability(arguments.session_id), sort_keys=True))
        return 0
    if not arguments.text:
        parser.error("--text is required unless --capability is given")
    outcome = send(
        arguments.session_id,
        arguments.text,
        priority=arguments.priority,
        timeout=arguments.timeout,
        want_receipt=not arguments.no_receipt,
    )
    print(json.dumps(outcome.as_dict(), sort_keys=True))
    return 0 if outcome.delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())
