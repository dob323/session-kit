from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import tempfile
import termios
import time
import unicodedata
import unittest
from collections.abc import Callable

from tests.support import REPO, run


LOGIN = REPO / "bin" / "shpool_login"
SGR = re.compile(r"\x1b\[([0-9;]*)m")
# Tab-title sequences render zero cells in a real terminal.
OSC_TITLE = re.compile(r"\x1b\][0-9;]*[^\x07\x1b]*(?:\x07|\x1b\\)")
# Cursor moves, erases, and synchronized-frame markers render zero cells too.
# A capture keeps them inline, so a line that carries a screen clear is not
# actually wider on the terminal that draws it.
CSI = re.compile(r"\x1b\[[0-9;?]*[@-~]")


def strip_sgr(text: str) -> str:
    return OSC_TITLE.sub("", SGR.sub("", text))


# Every non-SGR sequence the picker is allowed to emit on a capable terminal,
# enumerated so a test that forbids escapes forbids the ones that came from
# session content. Screen clear and cursor home start a frame; bracketed paste
# is armed around a prompt so a pasted block arrives as data (see
# tests/test_picker_input_safety.py).
PICKER_SCREEN_CONTROL = ("\x1b[2J", "\x1b[H", "\x1b[?2004h", "\x1b[?2004l")


def strip_screen_control(text: str) -> str:
    for sequence in PICKER_SCREEN_CONTROL:
        text = text.replace(sequence, "")
    return text


def display_cells(text: str) -> int:
    text = CSI.sub("", strip_sgr(text))
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def row(
    shpool_id: str,
    *,
    number: int,
    provider: str = "codex",
    availability: str = "ready",
    needs_you: bool = False,
    recent_output_at_unix_ms: int | None = None,
) -> dict:
    suffix = sum(map(ord, shpool_id))
    uuid = (
        f"00000000-0000-4000-8000-{suffix:012d}"
        if provider in {"claude", "codex"}
        else None
    )
    return {
        "row": number,
        "terminal_number": number,
        "shpool_id": shpool_id,
        "shpool_id_raw": shpool_id,
        "display_shpool_id": shpool_id,
        "mutation_allowed": True,
        "mutation_rejection_reason": None,
        "shpool_shell": {
            "pid": 1000 + suffix,
            "process_start_ticks": 10_000 + suffix,
        },
        "started_at_unix_ms": 1_700_000_000_000 + suffix,
        "shpool_status": (
            "Disconnected" if availability == "ready" else "Attached"
        ),
        "availability": availability,
        "provider": provider,
        "identity": {
            "uuid": uuid,
            "pid": 2000 + suffix,
            "process_start_ticks": 20_000 + suffix,
            "provenance": "fixture",
            "confidence": "exact",
        },
        "title": f"{provider.title()} {shpool_id}",
        "native_title": f"{provider.title()} {shpool_id}",
        "cwd": "/srv/project",
        "process_age_seconds": 60,
        "recent_output_at_unix_ms": recent_output_at_unix_ms,
        "recent_output_age_seconds": (
            30 if recent_output_at_unix_ms is not None else None
        ),
        "agent_status": "working",
        "needs_you": needs_you,
        "subagents": [],
        "recovery": {"available": bool(uuid), "provider": provider, "uuid": uuid},
        "diagnostics": [],
    }


def inventory(*rows: dict, stale: bool = False) -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-07-28T00:00:00Z",
        "source": "cache" if stale else "live",
        "stale": stale,
        "warnings": [],
        "daemon_generation": {
            "boot_id": "fixture",
            "pid": 77,
            "process_start_ticks": 770,
        },
        "sessions": list(rows),
        "outside_agents": [],
    }


def _recovery_rows(pending: dict | None) -> dict:
    """Build what `--recovery-pending-list` returns, with the real projection.

    A case writes the STORE it cares about -- a pending record, or a session
    closed on purpose -- and the module both surfaces read turns it into the
    payload. Hand-writing that payload is how a renamed timestamp field
    reached the screen: the fixture went on describing a shape the emitter had
    stopped producing, so the test for the picker's age line kept passing
    against a record the emitter cannot make.
    """
    import sys as _sys

    _sys.path.insert(0, os.fspath(REPO / "lib"))
    from sessionkit_inventory import recovery_list

    document = pending or {"schema_version": 1, "entries": []}
    now_ms = int(time.time() * 1000)
    closed_rows: list[dict] = []
    pending_entries: list[dict] = []
    numbers: dict[str, int] = {}
    for index, row in enumerate(document.get("entries") or [], 1):
        filled = dict(row)
        # A case that says nothing about time still needs an order, and an age
        # that reads like one rather than fifty years.
        filled.setdefault("started_at_unix_ms", now_ms - index * 60_000)
        # A case that does not say otherwise describes numbered rows, which is
        # the normal shape: a conversation keeps its number across the close.
        # `numbered: False` asks for the row whose number was retired.
        uuid = str(filled.get("uuid") or "")
        provider = str(filled.get("provider") or "")
        if uuid and provider and filled.pop("numbered", True):
            numbers[f"ai:{provider}:{uuid}"] = index
        if filled.get("closed_at_unix_ms"):
            filled.setdefault("restorable", True)
            closed_rows.append(filled)
        else:
            filled.setdefault("actionable", True)
            pending_entries.append(filled)
    rows = recovery_list.recovery_rows(
        closed_rows=closed_rows,
        pending_entries=pending_entries,
        numbers=numbers,
    )
    return {**document, "entries": rows}


class LoginFixture:
    def __init__(
        self,
        document: dict,
        *,
        pending: dict | None = None,
        refreshed_document: dict | None = None,
        ack_exit: int = 0,
    ) -> None:
        fixture_root = Path(
            os.environ.get("SESSION_KIT_TEST_EXEC_ROOT", os.fspath(REPO))
        )
        self.temp = tempfile.TemporaryDirectory(
            prefix=".login-", dir=fixture_root
        )
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.journals = self.base / "journals"
        self.journals.mkdir()
        self.projects = self.base / "projects.tsv"
        self.primary_project = self.base / "primary-project"
        self.primary_project.mkdir()
        self.other = self.base / "other"
        self.other.mkdir()
        self.projects.write_text(
            f"main\tclaude\t{self.primary_project}\nother\tcodex\t{self.other}\n",
            encoding="utf-8",
        )
        self.inventory = self.base / "inventory.json"
        self.inventory.write_text(json.dumps(document), encoding="utf-8")
        self.refreshed_inventory = self.base / "refreshed-inventory.json"
        self.refreshed_inventory.write_text(
            json.dumps(refreshed_document or document), encoding="utf-8"
        )
        self.snapshot_count = self.base / "snapshot-count"
        self.pending = self.base / "pending.json"
        self.pending.write_text(
            json.dumps(_recovery_rows(pending)),
            encoding="utf-8",
        )
        self.prompt_quarantine = self.base / "prompt-quarantine.json"
        self.prompt_quarantine.write_text(
            json.dumps({"items": [], "schema_version": 1}), encoding="utf-8"
        )
        self.sp_log = self.base / "sp.log"
        self.status_log = self.base / "status.log"
        self.ack_exit = ack_exit
        self.fake_shpool = self.base / "fake-shpool"
        self.fake_status = self.base / "fake-status"
        self.fake_sp = self.base / "fake-sp"
        write_executable(self.fake_shpool, "#!/usr/bin/env bash\nexit 0\n")
        write_executable(
            self.fake_status,
            """#!/usr/bin/env python3
import json,os,pathlib,sys
args=sys.argv[1:]
with pathlib.Path(os.environ["LOGIN_STATUS_LOG"]).open("a") as log:
    log.write(json.dumps(args)+"\\n")
if args == ["--json"]:
    counter=pathlib.Path(os.environ["LOGIN_SNAPSHOT_COUNT"])
    try: count=int(counter.read_text())
    except (OSError,ValueError): count=0
    count += 1
    counter.write_text(str(count))
    source=(
        pathlib.Path(os.environ["LOGIN_REFRESHED_INVENTORY"])
        if count > 1
        else pathlib.Path(os.environ["LOGIN_INVENTORY"])
    )
    print(source.read_text(),end="")
elif args == ["--recovery-pending-list"]:
    print(pathlib.Path(os.environ["LOGIN_PENDING"]).read_text(),end="")
elif len(args) == 2 and args[0] == "--recovery-remember-printed":
    # The real status command writes down what this screen is about to print,
    # so `sp restore` can check a word typed later against it. It prints
    # nothing to the screen; LOGIN_REMEMBER_EXIT makes it fail on purpose.
    raise SystemExit(int(os.environ.get("LOGIN_REMEMBER_EXIT", "0")))
elif len(args) == 4 and args[0] == "--recovery-pending-ack":
    print("{}")
    raise SystemExit(int(os.environ.get("LOGIN_ACK_EXIT", "0")))
else:
    raise SystemExit(2)
""",
        )
        write_executable(
            self.fake_sp,
            """#!/usr/bin/env python3
import json,os,pathlib,stat,sys
args=sys.argv[1:]
if args == ["prompt-quarantine","list","--json"]:
    print(pathlib.Path(os.environ["LOGIN_PROMPT_QUARANTINE"]).read_text(),end="")
    raise SystemExit(0)
if len(args) == 3 and args[:2] == ["account","choices"]:
    provider=args[2]
    second_ok=os.environ.get("LOGIN_ACCOUNT_SECOND_ELIGIBLE", "1") != "0"
    print(json.dumps({
        "provider":provider,
        "roster_fresh":True,
        "roster_state":"fresh",
        "advice_fresh":True,
        "recommendation":None,
        "recommendation_reason":"",
        "recommendation_blocked":None,
        "choices":[
            {
                "alias":"wren",
                "email":"account@example.com",
                "plan":"subscription",
                "eligible":True,
                "state":"ready",
                "health":"ok",
                "serving":True,
                "u5h":0.0,
                "u7d":0.75,
                "recommended":False,
            },
            {
                "alias":"work",
                "email":"work@example.com",
                "plan":"subscription",
                "eligible":second_ok,
                "state":"ready" if second_ok else "blocked: health error",
                "health":"ok" if second_ok else "error",
                "serving":False,
                "u5h":0.34,
                "u7d":0.36,
                "recommended":False,
            },
        ],
    }))
    raise SystemExit(0)
entry={"args":args}
if args and args[0].startswith("picker-"):
    entry["confirm_id"]=os.environ.get("SESSION_KIT_CONFIRM_ID","")
    proof=pathlib.Path(args[1])
    metadata=proof.lstat()
    entry["proof"]=json.loads(proof.read_text())
    entry["proof_mode"]=stat.S_IMODE(metadata.st_mode)
    entry["proof_owner"]=metadata.st_uid
with pathlib.Path(os.environ["LOGIN_SP_LOG"]).open("a") as log:
    log.write(json.dumps(entry,sort_keys=True)+"\\n")
if args and args[0] == "restore-exact":
    print("restored-fixture")
if len(args) > 1 and args[:2] == ["prompt-quarantine","resume"]:
    print("s20260809-232323-9 f9e8d7c6-b5a4-4938-a271-6f5e4d3c2b1a")
raise SystemExit(int(os.environ.get("LOGIN_SP_EXIT","0")))
""",
        )

    def close(self) -> None:
        self.temp.cleanup()

    def env(
        self, *, lines: int = 24, columns: int = 100, sp_exit: int = 0
    ) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "LINES": str(lines),
                "COLUMNS": str(columns),
                "TERM": "xterm",
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SESSION_KIT_JOURNAL_DIR": str(self.journals),
                "SESSION_KIT_PROJECTS_FILE": str(self.projects),
                "SESSION_KIT_SHPOOL_CMD": str(self.fake_shpool),
                "SESSION_KIT_STATUS_CMD": str(self.fake_status),
                "SESSION_KIT_SP_CMD": str(self.fake_sp),
                "SESSION_KIT_NONINTERACTIVE": "0",
                "SESSION_KIT_NO_COLOR": "1",
                # The stub shpool here cannot carry an event stream, so every
                # fixture that is not about events measures the timed poll --
                # which is also what a machine without the events socket runs.
                # tests/test_picker_events.py turns it back on with a stub that
                # speaks the stream.
                "SESSION_KIT_PICKER_EVENTS": "0",
                "LOGIN_INVENTORY": str(self.inventory),
                "LOGIN_REFRESHED_INVENTORY": str(self.refreshed_inventory),
                "LOGIN_SNAPSHOT_COUNT": str(self.snapshot_count),
                "LOGIN_PENDING": str(self.pending),
                "LOGIN_PROMPT_QUARANTINE": str(self.prompt_quarantine),
                "LOGIN_SP_LOG": str(self.sp_log),
                "LOGIN_STATUS_LOG": str(self.status_log),
                "LOGIN_ACK_EXIT": str(self.ack_exit),
                "LOGIN_SP_EXIT": str(sp_exit),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return environment

    def sp_entries(self) -> list[dict]:
        if not self.sp_log.exists():
            return []
        return [json.loads(line) for line in self.sp_log.read_text().splitlines()]

    def status_entries(self) -> list[list[str]]:
        return [
            json.loads(line) for line in self.status_log.read_text().splitlines()
        ]

    def picker_temps(self) -> list[Path]:
        return sorted(
            path
            for path in self.state.iterdir()
            if path.name.startswith(("login-", "picker-proof."))
        )


def run_pty(
    fixture: LoginFixture,
    input_bytes: bytes = b"",
    *,
    lines: int = 24,
    columns: int = 100,
    sp_exit: int = 0,
    send_signal: int | None = None,
    signal_marker: str = "❯ ",
    post_signal_bytes: bytes = b"",
    post_signal_marker: str | None = None,
    env_updates: dict[str, str | None] | None = None,
    deferred: tuple[str, bytes] | None = None,
    deferred_delay_seconds: float = 0,
    deferred_action: Callable[[], None] | None = None,
    deferred_check: Callable[[], None] | None = None,
    followup: tuple[str, bytes, float] | None = None,
    pty_winsize: tuple[int, int] | None = None,
) -> tuple[int, str]:
    environment = fixture.env(
        lines=lines, columns=columns, sp_exit=sp_exit
    )
    for key, value in (env_updates or {}).items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    # Every executable capable of an action must be an isolated fixture.
    for key in (
        "SESSION_KIT_SHPOOL_CMD",
        "SESSION_KIT_STATUS_CMD",
        "SESSION_KIT_SP_CMD",
    ):
        executable = Path(environment[key]).resolve()
        if fixture.base.resolve() not in executable.parents:
            raise AssertionError(f"unsafe test executable for {key}: {executable}")
    if Path(environment["SESSION_KIT_SP_CMD"]).resolve() == (REPO / "bin/sp").resolve():
        raise AssertionError("PTY test must never invoke the repository sp command")

    # When a test supplies a window size, do not release the child until the
    # parent has installed it. Setting TIOCSWINSZ after pty.fork otherwise
    # races the child's exec: on the macOS runner the picker sometimes read
    # the launcher's 76-column pty before this fixture pinned 40 columns.
    ready_reader = ready_writer = None
    if pty_winsize is not None:
        ready_reader, ready_writer = os.pipe()
    pid, descriptor = pty.fork()
    if pid == 0:
        # A child that cannot exec must never return: it is a forked copy of
        # the test runner, and returning resumes the whole suite from here.
        try:
            if ready_reader is not None and ready_writer is not None:
                os.close(ready_writer)
                os.read(ready_reader, 1)
                os.close(ready_reader)
            os.chdir(fixture.base)
            if os.environ.get("SESSION_KIT_TEST_EXEC_ROOT"):
                os.execve(
                    "/usr/bin/bash",
                    ["/usr/bin/bash", os.fspath(LOGIN)],
                    environment,
                )
            os.execve(LOGIN, [os.fspath(LOGIN)], environment)
        finally:
            os._exit(127)

    if pty_winsize is not None:
        # Give the slave a real window size so the ioctl path is exercised.
        # The pipe barrier above guarantees the child cannot measure it first.
        winsize_rows, winsize_cols = pty_winsize
        fcntl.ioctl(
            descriptor,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", winsize_rows, winsize_cols, 0, 0),
        )
        assert ready_reader is not None and ready_writer is not None
        os.close(ready_reader)
        os.write(ready_writer, b"1")
        os.close(ready_writer)

    output = bytearray()
    deadline = time.monotonic() + 10
    try:
        if input_bytes:
            os.write(descriptor, input_bytes)
        if deferred is not None:
            # Type only once the picker has painted something on its own,
            # so an unattended repaint can be observed before any keystroke.
            marker, payload = deferred
            marker_deadline = min(deadline, time.monotonic() + 8)
            while marker.encode() not in output and time.monotonic() < marker_deadline:
                ready, _, _ = select.select([descriptor], [], [], 0.05)
                if not ready:
                    continue
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
            if marker.encode() not in output:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                raise AssertionError(
                    f"picker never rendered {marker!r} on its own; "
                    f"output={output.decode(errors='replace')!r}"
                )
            if deferred_action is not None:
                deferred_action()
            if deferred_delay_seconds > 0:
                time.sleep(deferred_delay_seconds)
            if deferred_check is not None:
                deferred_check()
            os.write(descriptor, payload)
        if followup is not None:
            # A second scripted beat for flows that hand the terminal to a
            # NEW process mid-run (the upgrade handoff): wait for a marker
            # only that successor can print, then type into it. The deadline
            # is the followup's own — recovery spans exec boundaries and can
            # honestly take longer than the marker wait above.
            followup_marker, followup_payload, followup_wait = followup
            followup_deadline = time.monotonic() + followup_wait
            while (
                followup_marker.encode() not in output
                and time.monotonic() < followup_deadline
            ):
                ready, _, _ = select.select([descriptor], [], [], 0.05)
                if not ready:
                    continue
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
            if followup_marker.encode() not in output:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                raise AssertionError(
                    f"picker never rendered {followup_marker!r} after the "
                    f"deferred stage; output={output.decode(errors='replace')!r}"
                )
            deadline = max(deadline, time.monotonic() + 8)
            os.write(descriptor, followup_payload)
        if send_signal is not None:
            # Which prompt the signal is aimed at. A modal has to be on screen
            # before an interrupt can be said to have cancelled it, and the
            # home prompt is already there by then.
            prompt = signal_marker.encode()
            signal_deadline = min(deadline, time.monotonic() + 5)
            while prompt not in output and time.monotonic() < signal_deadline:
                ready, _, _ = select.select([descriptor], [], [], 0.05)
                if not ready:
                    continue
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
            if prompt not in output:
                raise AssertionError(
                    f"picker PTY never rendered {signal_marker!r} before the signal; "
                    f"output={output.decode(errors='replace')!r}"
                )
            os.kill(pid, send_signal)
            if post_signal_bytes:
                if post_signal_marker is None:
                    time.sleep(0.3)
                else:
                    # Type only once the picker has answered the signal. A
                    # fixed sleep races the answer, and a keystroke that lands
                    # first is consumed by the very prompt under test.
                    answer = post_signal_marker.encode()
                    answer_deadline = min(deadline, time.monotonic() + 5)
                    while (
                        answer not in output and time.monotonic() < answer_deadline
                    ):
                        ready, _, _ = select.select([descriptor], [], [], 0.05)
                        if not ready:
                            continue
                        try:
                            chunk = os.read(descriptor, 65536)
                        except OSError as exc:
                            if exc.errno != errno.EIO:
                                raise
                            break
                        if not chunk:
                            break
                        output.extend(chunk)
                    if answer not in output:
                        raise AssertionError(
                            f"picker never printed {post_signal_marker!r} after the "
                            f"signal; output={output.decode(errors='replace')!r}"
                        )
                os.write(descriptor, post_signal_bytes)
        status = None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([descriptor], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                if chunk:
                    output.extend(chunk)
            waited, child_status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = child_status
                break
        if status is None:
            os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
            raise AssertionError(
                f"picker PTY timed out; output={output.decode(errors='replace')!r}"
            )
        while True:
            ready, _, _ = select.select([descriptor], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(descriptor, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output.extend(chunk)
        return os.waitstatus_to_exitcode(status), output.decode(
            "utf-8", errors="replace"
        ).replace("\r\n", "\n")
    finally:
        os.close(descriptor)


class LoginPickerTests(unittest.TestCase):
    def test_quit_and_eof_return_regular_shell_without_action(self) -> None:
        for label, payload in (
            ("quit", b"q\n"),
            ("eof", b"\x04"),
        ):
            with self.subTest(label=label):
                fixture = LoginFixture(inventory(row("ready", number=9)))
                try:
                    code, _ = run_pty(fixture, payload)
                    self.assertEqual(0, code)
                    self.assertEqual([], fixture.sp_entries())
                    self.assertEqual([], fixture.picker_temps())
                finally:
                    fixture.close()

    def test_enter_on_the_home_list_opens_the_top_row(self) -> None:
        """Enter takes the most likely option: the row drawn first.

        It used to drop the person into a bare shell, so the one key a menu
        trains you to press was the one that left without doing anything.
        """
        fixture = LoginFixture(
            inventory(
                row("second", number=4),
                row("top", number=9, needs_you=True),
            )
        )
        try:
            code, output = run_pty(fixture, b"\nq\n")
            self.assertEqual(0, code)
            # The footer promised the number, and the open used it.
            self.assertIn("↵ open 9", output)
            entries = fixture.sp_entries()
            self.assertEqual(["picker-open"], [entry["args"][0] for entry in entries])
            self.assertEqual("top", entries[0]["proof"]["shpool_id"])
            # And the picker was still there afterwards: q is what ended it,
            # so the list was drawn again after the open attempt returned.
            self.assertGreaterEqual(output.count("↵ open 9"), 2)
        finally:
            fixture.close()

    def test_enter_on_an_empty_list_opens_the_new_session_screen(self) -> None:
        fixture = LoginFixture(inventory())
        try:
            code, output = run_pty(fixture, b"\nb\nq\n")
            self.assertEqual(0, code)
            self.assertIn("↵ new", output)
            self.assertIn("New session", output)
            self.assertIn("↵ Claude Code", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_the_home_footer_names_the_row_enter_opens_and_the_way_out(self) -> None:
        """The footer promises a number, and the dispatcher opens that one.

        Both read the same page slice, so the promise and the action cannot
        disagree -- and the door out is named for the keys that take it.
        """
        fixture = LoginFixture(
            inventory(
                row("second", number=2),
                row("top", number=7, needs_you=True),
            )
        )
        try:
            code, output = run_pty(fixture, b"q\n", columns=120)
            self.assertEqual(0, code)
            footers = [
                line
                for line in strip_sgr(output).splitlines()
                if line.strip().startswith("↵ open")
            ]
            self.assertTrue(footers, output)
            self.assertTrue(
                footers[-1].startswith("  ↵ open 7 · # · kill k #"), footers[-1]
            )
            self.assertIn("leave q or b", footers[-1])
            self.assertNotIn("back ↵ or b", output)
        finally:
            fixture.close()

    def test_the_home_footer_fits_every_width_and_keeps_the_enter_hint(self) -> None:
        """The hint that names Enter is the last thing a narrow window loses.

        The footer used to be four hand-sized width tiers, and the Enter hint
        -- whose width follows the row number it names -- overflowed the
        narrowest of them the day it landed.
        """
        document = inventory(
            row("second", number=2),
            row("top", number=7, needs_you=True),
        )
        for columns in (30, 40, 50, 60, 80, 120):
            with self.subTest(columns=columns):
                fixture = LoginFixture(document)
                try:
                    code, output = run_pty(fixture, b"q\n", columns=columns)
                    self.assertEqual(0, code)
                    footers = [
                        line
                        for line in strip_sgr(output).splitlines()
                        if line.strip().startswith("↵ open")
                    ]
                    self.assertTrue(footers, output)
                    for line in footers:
                        self.assertLessEqual(display_cells(line), columns - 1, line)
                    self.assertTrue(footers[-1].startswith("  ↵ open 7"), footers[-1])
                    # Segments are ordered by what a person needs, so what a
                    # narrow window drops is the luxury and not the door:
                    # `more` reaches projects, closed sessions, and everything
                    # outside the kit, and it survives where `history h #`
                    # and the rest do not.
                    if columns >= 50:
                        self.assertIn("more m", footers[-1])
                    if columns == 50:
                        self.assertNotIn("needs you a", footers[-1])
                        self.assertNotIn("history h #", footers[-1])
                finally:
                    fixture.close()

    def test_interrupt_redraws_menu_instead_of_exiting(self) -> None:
        # A stray Ctrl-C must never dump the human to a bare terminal: the
        # picker redraws its menu and only a deliberate quit ends it.
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(
                fixture,
                b"",
                send_signal=signal.SIGINT,
                post_signal_bytes=b"q\n",
            )
            self.assertEqual(0, code)
            # The exit reason proves the interrupt was absorbed: the picker
            # ended through the deliberate q that followed, not the signal.
            log = fixture.state / "action-events.jsonl"
            events = log.read_text() if log.exists() else ""
            self.assertIn("quit", events)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_projection_groups_and_search_preserve_stable_terminal_numbers(self) -> None:
        searchable = row("codex10", number=10, provider="codex")
        searchable["title"] = "Searchable task"
        searchable["native_title"] = "Searchable task"
        searchable["display_title"] = "Searchable task"
        unavailable = row(
            "open3",
            number=3,
            provider="codex",
            availability="attached",
        )
        unavailable["agent_status"] = "state unavailable"
        unavailable["process_age_seconds"] = 2 * 86400 + 3 * 3600
        fixture = LoginFixture(
            inventory(
                searchable,
                row("claude2", number=2, provider="claude"),
                row(
                    "claude1",
                    number=1,
                    provider="claude",
                    needs_you=True,
                ),
                unavailable,
            )
        )
        try:
            code, output = run_pty(fixture, b"/codex10\nq\n")
            self.assertEqual(0, code)
            self.assertIn(
                "4 sessions · 3 ready · 1 open elsewhere", output
            )
            self.assertIn("Ready", output)
            self.assertIn("Open elsewhere", output)
            self.assertIn("| CDX | pe… | pe… | pending", output)
            self.assertNotIn("state unavailable", output)
            self.assertLess(output.index("claude1"), output.index("claude2"))
            self.assertIn("1 match of 4 sessions", output)
            self.assertRegex(
                output,
                r"(?m)^\s+10\s+Searchable task \| CDX \| pe… \| pe… \| working \| last active pending$",
            )
            self.assertNotIn("[codex10]", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_refresh_and_provider_regroup_preserve_terminal_number_and_internal_row(self) -> None:
        before = row("transition", number=7401, provider="unknown")
        before["row"] = 1
        before["identity"]["uuid"] = None
        before["identity"]["confidence"] = "unknown"
        after = row(
            "transition",
            number=7401,
            provider="codex",
            availability="attached",
        )
        after["row"] = 88
        fixture = LoginFixture(
            inventory(before),
            refreshed_document=inventory(after),
        )
        try:
            code, output = run_pty(fixture, b"r\n7401\nb\nq\n")
            self.assertEqual(0, code)
            self.assertRegex(output, r"(?m)^\s+7401\s+Unknown transition")
            self.assertRegex(output, r"(?m)^\s+7401\s+Codex transition")
            self.assertIn("Open elsewhere.", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_sparse_page_membership_and_arbitrary_width_number_are_exact(self) -> None:
        huge = 12345678901234567890
        rows = [
            row(f"s{index:02}", number=1000 + index * 13)
            for index in range(1, 25)
        ]
        rows[-1]["terminal_number"] = huge
        fixture = LoginFixture(inventory(*rows))
        try:
            code, output = run_pty(
                fixture,
                f"{huge}\nnext\n{huge}\nq\n".encode(),
                lines=24,
                columns=60,
            )
            self.assertEqual(0, code)
            self.assertIn(
                "on this screen. Numbers shown here work.",
                output,
            )
            # No longer truncated: the title column is sized against the pair
            # rather than against the state word alone.
            self.assertRegex(output, rf"(?m)^\s+{huge}\s+Codex s24")
            entries = fixture.sp_entries()
            self.assertEqual(["picker-open"], [entry["args"][0] for entry in entries])
            self.assertEqual("s24", entries[0]["proof"]["shpool_id"])
            self.assertLessEqual(
                max(
                    display_cells(line)
                    for line in output.splitlines()[2:]
                    # The refusal names the number that was typed, and this one
                    # is twenty digits long. The rule under test is that the
                    # list frame fits the window.
                    if "Numbers shown here work." not in line
                ),
                59,
            )
        finally:
            fixture.close()

    def test_missing_terminal_number_is_not_renumbered_or_actionable(self) -> None:
        unsafe = row("no-number", number=1)
        unsafe["terminal_number"] = None
        unsafe["mutation_allowed"] = False
        unsafe["mutation_rejection_reason"] = "missing-shell-generation"
        fixture = LoginFixture(inventory(unsafe))
        try:
            code, output = run_pty(fixture, b"1\nm\n\nq\n")
            self.assertEqual(0, code)
            self.assertNotIn("Codex no-number", output)
            self.assertIn("Sessions: none.", output)
            self.assertNotIn("0 sessions", output)
            self.assertIn("more m", output)
            self.assertIn(
                "Unavailable: 1 session whose shell is gone",
                output,
            )
            # The one thing the count never said: what clears it, and where to
            # read the rest.
            self.assertIn("sp help unavailable", output)
            self.assertIn("on this screen. Numbers shown here work.", output)
            self.assertNotIn("[no-number]", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_mixed_unavailable_row_does_not_block_exact_kill_proof(self) -> None:
        exact = row("exact", number=9, availability="attached")
        unavailable = row("no-shell", number=2)
        unavailable["terminal_number"] = None
        unavailable["mutation_allowed"] = False
        unavailable["mutation_rejection_reason"] = "missing-shell-generation"
        fixture = LoginFixture(inventory(exact, unavailable))
        try:
            code, output = run_pty(fixture, b"k 2\nk 9\nm\n\nq\n")
            self.assertEqual(0, code)
            self.assertIn(
                "Unavailable: 1 session whose shell is gone",
                output,
            )
            self.assertIn("There is no session 2 on this screen.", output)
            self.assertNotIn("[no-shell]", output)
            entries = fixture.sp_entries()
            self.assertEqual(1, len(entries))
            self.assertEqual("picker-close", entries[0]["args"][0])
            self.assertEqual("exact", entries[0]["proof"]["shpool_id"])
        finally:
            fixture.close()

    def test_paging_rejects_hidden_number_then_opens_with_canonical_proof(self) -> None:
        rows = tuple(row(f"s{number}", number=number) for number in range(1, 26))
        fixture = LoginFixture(inventory(*rows))
        try:
            code, output = run_pty(fixture, b"17\nnext\n17\nq\n", lines=24)
            self.assertEqual(0, code)
            self.assertIn("on this screen. Numbers shown here work.", output)
            self.assertIn("Page 1/2 | next", output)
            self.assertIn("Page 2/2 | prev", output)
            entries = fixture.sp_entries()
            self.assertEqual(1, len(entries))
            self.assertEqual("picker-open", entries[0]["args"][0])
            self.assertEqual(0o600, entries[0]["proof_mode"])
            self.assertEqual(os.geteuid(), entries[0]["proof_owner"])
            proof = entries[0]["proof"]
            self.assertEqual(
                {
                    "daemon_pid",
                    "daemon_process_start_ticks",
                    "proof_type",
                    "provider",
                    "provider_pid",
                    "provider_process_start_ticks",
                    "schema_version",
                    "shell_pid",
                    "shell_process_start_ticks",
                    "shpool_id",
                    "started_at_unix_ms",
                    "uuid",
                },
                set(proof),
            )
            self.assertEqual("session-kit-picker-session-v1", proof["proof_type"])
            self.assertEqual("s17", proof["shpool_id"])
            self.assertEqual([], fixture.picker_temps())
        finally:
            fixture.close()

    def test_rows_fit_the_real_pty_width_when_no_size_is_exported(self) -> None:
        """Production exports no COLUMNS/LINES: with the frame captured to a
        file, the picker must still measure the real terminal, not a probe
        fallback, or every row lays out for a 100-column window."""
        fixture = LoginFixture(
            inventory(
                row(
                    "alpha-with-a-fairly-long-name-that-must-truncate-somewhere",
                    number=3,
                )
            )
        )
        try:
            code, output = run_pty(
                fixture,
                b"q\n",
                env_updates={"COLUMNS": None, "LINES": None},
                pty_winsize=(24, 40),
            )
            self.assertEqual(0, code)
            self.assertRegex(output, r"\s3\s+\S")
            for line in output.splitlines()[2:]:
                self.assertLessEqual(
                    display_cells(line),
                    39,
                    f"line wider than the 40-column terminal: {line!r}",
                )
        finally:
            fixture.close()

    def test_compact_rows_fit_24_and_36_with_exact_directions(self) -> None:
        rows = [
            row(
                "s20260728-022604-447706",
                number=1,
                provider="claude",
                needs_you=True,
            ),
            row(
                "a2",
                number=2,
                provider="claude",
                recent_output_at_unix_ms=1_800_000_000_000,
            ),
            row("a3", number=3, provider="claude"),
            row("b1", number=4, provider="codex"),
            row("b2", number=5, provider="codex"),
            row("b3", number=6, provider="codex"),
            row(
                "c1",
                number=7,
                provider="claude",
                availability="attached",
            ),
            row(
                "c2",
                number=8,
                provider="claude",
                availability="attached",
            ),
            row(
                "d1",
                number=9,
                provider="codex",
                availability="attached",
            ),
            row(
                "d2",
                number=10,
                provider="codex",
                availability="attached",
            ),
            row(
                "d3",
                number=11,
                provider="codex",
                availability="attached",
            ),
        ]
        rows[2]["display_title"] = "Claude wide 界 e\u0301"
        rows[3]["display_provider"] = "unknown"
        document = inventory(*rows)
        for lines in (24, 36):
            for columns in (60, 80, 100, 160):
                with self.subTest(lines=lines, columns=columns):
                    fixture = LoginFixture(document)
                    try:
                        code, output = run_pty(
                            fixture, b"q\n", lines=lines, columns=columns
                        )
                        self.assertEqual(0, code)
                        self.assertNotIn("  Page ", output)
                        rendered = output.splitlines()[2:]
                        self.assertLessEqual(
                            max(display_cells(line) for line in rendered),
                            columns - 1,
                        )
                        self.assertEqual(
                            11,
                            sum(
                                bool(
                                    __import__("re").match(
                                        r"^\s+\d+\s+.+\s+\|", line
                                    )
                                )
                                for line in rendered
                            ),
                        )
                        self.assertNotIn("recent output now", output)
                        self.assertNotIn("last output now", output)
                        self.assertNotIn("[s202…447706]", output)
                        self.assertRegex(
                            output,
                            r"(?m)^\s+1\s+.+\s+\|",
                        )
                    finally:
                        fixture.close()

        extra = [
            row(
                f"z{number}",
                number=number,
                provider="codex",
                availability="attached",
            )
            for number in range(12, 32)
        ]
        fixture = LoginFixture(inventory(*(rows + extra)))
        try:
            code, output = run_pty(
                fixture,
                b"next\nnext\nprev\n/d3\nq\n",
                lines=24,
                columns=80,
            )
            self.assertEqual(0, code)
            self.assertIn("Page 1/2 | next", output)
            self.assertIn("Page 2/2 | prev", output)
            self.assertIn("1 match of 31 sessions", output)
            self.assertNotIn("Page 1/1", output)
            self.assertRegex(
                output,
                r"(?m)^\s+11\s+Codex d3\s+\| CDX \| pe… \| pe… \| working \| last active pending$",
            )
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_blocking_replies_lead_each_availability_group(self) -> None:
        optional = row("optional", number=5, provider="codex")
        optional["agent_status"] = "reply optional"
        fixture = LoginFixture(
            inventory(
                row("ready-claude", number=1, provider="claude"),
                row(
                    "reply-codex",
                    number=2,
                    provider="codex",
                    needs_you=True,
                ),
                row(
                    "open-claude",
                    number=3,
                    provider="claude",
                    availability="attached",
                ),
                row(
                    "reply-open-codex",
                    number=4,
                    provider="codex",
                    availability="attached",
                    needs_you=True,
                ),
                optional,
            )
        )
        try:
            code, output = run_pty(fixture, b"q\n", lines=30, columns=120)
            self.assertEqual(0, code)
            # Three: the footer's "needs you a", and the two blocking rows'
            # state slots — one word for one state since the needs-you rename.
            self.assertEqual(3, output.count("needs you"))
            self.assertLess(output.index("reply-codex"), output.index("ready-claude"))
            self.assertLess(
                output.index("reply-open-codex"), output.index("open-claude")
            )
            self.assertIn("optional", output)
            # "reply optional" is the collector's word for a session that is
            # still working, and the screen speaks one vocabulary.
            self.assertNotIn("reply optional", output)
            self.assertEqual(3, output.count("| working"))
        finally:
            fixture.close()

    def test_automatic_name_states_are_visible_but_never_attention_sorted(self) -> None:
        pending = row("pending", number=1, provider="claude")
        pending["automatic_name_state"] = "pending"
        failed = row("failed", number=2, provider="codex")
        failed["automatic_name_state"] = "failed"
        blocking = row(
            "blocking",
            number=3,
            provider="codex",
            needs_you=True,
        )
        fixture = LoginFixture(inventory(pending, failed, blocking))
        try:
            code, output = run_pty(fixture, b"q\n", columns=120)
            self.assertEqual(0, code)
            self.assertLess(output.index("blocking"), output.index("pending"))
            self.assertLess(output.index("blocking"), output.index("failed"))
            self.assertIn("name pending", output)
            self.assertIn("name failed", output)
            self.assertEqual(1, output.count("| needs you |"))
        finally:
            fixture.close()

    def test_pending_provider_bar_title_is_internal_in_normal_rows(self) -> None:
        pending = row(
            "title-pending",
            number=6,
            provider="codex",
            availability="attached",
        )
        pending["provider_title_state"] = "pending"
        fixture = LoginFixture(inventory(pending))
        try:
            code, output = run_pty(fixture, b"q\n", columns=120)
            self.assertEqual(0, code)
            self.assertIn("working", output)
            self.assertNotIn("title pending", output)
        finally:
            fixture.close()

    def test_deferred_provider_bar_title_is_not_shown_as_actionable(self) -> None:
        deferred = row(
            "title-deferred",
            number=6,
            provider="codex",
            availability="attached",
        )
        deferred["provider_title_state"] = "deferred"
        fixture = LoginFixture(inventory(deferred))
        try:
            code, output = run_pty(fixture, b"q\n", columns=120)
            self.assertEqual(0, code)
            self.assertNotIn("title pending", output)
            self.assertIn("working", output)
        finally:
            fixture.close()

    def test_enter_on_the_open_elsewhere_panel_moves_the_session_here(self) -> None:
        """Enter takes the most likely option on that panel.

        A person who picked a session that is open elsewhere almost always
        wants it here; Enter used to walk back out of the panel instead.
        """
        elsewhere = row(
            "open-elsewhere",
            number=6,
            provider="codex",
            availability="attached",
        )
        fixture = LoginFixture(inventory(elsewhere))
        try:
            code, output = run_pty(fixture, b"6\n\nq\n", columns=120)
            self.assertEqual(0, code)
            self.assertIn("↵ move it here · b back", output)
            entries = fixture.sp_entries()
            self.assertEqual(
                ["picker-takeover"], [entry["args"][0] for entry in entries]
            )
            self.assertEqual("open-elsewhere", entries[0]["proof"]["shpool_id"])
        finally:
            fixture.close()

    def test_b_on_the_open_elsewhere_panel_still_goes_back(self) -> None:
        elsewhere = row(
            "open-elsewhere",
            number=6,
            provider="codex",
            availability="attached",
        )
        fixture = LoginFixture(inventory(elsewhere))
        try:
            code, output = run_pty(fixture, b"6\nb\nq\n", columns=120)
            self.assertEqual(0, code)
            self.assertIn("Open elsewhere.", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_open_elsewhere_pending_title_offers_proof_bound_refresh(self) -> None:
        pending = row(
            "title-pending",
            number=6,
            provider="codex",
            availability="attached",
        )
        pending["provider_title_state"] = "pending"
        fixture = LoginFixture(inventory(pending))
        try:
            code, output = run_pty(fixture, b"6\n5\nq\n", columns=120)
            self.assertEqual(0, code)
            self.assertIn("Apply the pending title", output)
            entries = fixture.sp_entries()
            self.assertEqual(1, len(entries))
            self.assertEqual("picker-title-refresh", entries[0]["args"][0])
            self.assertEqual("title-pending", entries[0]["proof"]["shpool_id"])
        finally:
            fixture.close()

    def test_open_elsewhere_account_change_is_proof_bound(self) -> None:
        waiting = row(
            "account-change",
            number=6,
            provider="codex",
            availability="attached",
        )
        waiting["agent_status"] = "idle"
        waiting["account_alias"] = "wren"
        waiting["account_switch_capable"] = True
        fixture = LoginFixture(inventory(waiting))
        try:
            code, output = run_pty(
                fixture, b"6\n4\n2\nq\n", columns=120
            )
            self.assertEqual(0, code)
            self.assertIn("Change subscription account", output)
            self.assertNotIn("[y/N]", output)
            entries = fixture.sp_entries()
            self.assertEqual(1, len(entries))
            self.assertEqual("picker-account-switch", entries[0]["args"][0])
            self.assertEqual("work", entries[0]["args"][2])
            self.assertEqual("account-change", entries[0]["proof"]["shpool_id"])
            self.assertEqual("account-change", entries[0]["confirm_id"])
        finally:
            fixture.close()

    def test_open_elsewhere_refuses_an_unhealthy_target_account(self) -> None:
        waiting = row(
            "account-change",
            number=6,
            provider="codex",
            availability="attached",
        )
        waiting["agent_status"] = "idle"
        waiting["account_alias"] = "wren"
        waiting["account_switch_capable"] = True
        fixture = LoginFixture(inventory(waiting))
        try:
            # One Enter to leave the account step the refusal kept open, one
            # more to leave the picker.
            code, output = run_pty(
                fixture,
                b"6\n4\n2\n\nq\n",
                columns=120,
                env_updates={"LOGIN_ACCOUNT_SECOND_ELIGIBLE": "0"},
            )
            self.assertEqual(0, code)
            # The refusal quotes that row's own state, and the step stays open
            # for another pick instead of unwinding to the dashboard.
            self.assertIn("is not selectable: blocked: health error", output)
            self.assertEqual(2, output.count("target account ❯"))
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

        combined = row(
            "combined",
            number=4,
            provider="codex",
            needs_you=True,
        )
        combined["automatic_name_state"] = "failed"
        combined["display_title"] = "A Long Combined Attention And Naming State"
        fixture = LoginFixture(inventory(combined))
        try:
            code, output = run_pty(fixture, b"q\n", columns=60)
            self.assertEqual(0, code)
            # One phrase for the attention state on every surface (picker-rows
            # ruling): the row says "needs you", never "needs you".
            self.assertIn("needs you", output)
        finally:
            fixture.close()

    def test_colored_picker_uses_only_the_approved_sgr_allowlist(self) -> None:
        unsafe = row("unsafe", number=7, provider="codex", needs_you=True)
        unsafe["display_title"] = "Unsafe\x1b[31m title\x1b]0;bad\u0007"
        fixture = LoginFixture(inventory(unsafe, stale=True))
        try:
            code, output = run_pty(
                fixture,
                b"q\n",
                columns=100,
                env_updates={
                    "NO_COLOR": None,
                    "SESSION_KIT_NO_COLOR": None,
                    "TERM": "xterm-ghostty",
                },
            )
            self.assertEqual(0, code)
            codes = set(SGR.findall(output))
            self.assertTrue(
                {"0", "1", "32", "33", "38;2;64;216;209"}.issubset(
                    codes
                )
            )
            self.assertLessEqual(
                codes,
                {"0", "1", "32", "33", "36", "38;2;64;216;209"},
            )
            self.assertRegex(
                output,
                r"\x1b\[1m\x1b\[32m\s*7\x1b\[0m",
            )
            plain = strip_sgr(output)
            # The startup screen clear (erase display + cursor home) is the
            # one approved non-SGR sequence on capable terminals; nothing
            # else may survive the strip.
            plain = strip_screen_control(plain)
            self.assertNotIn("\x1b", plain)
            self.assertIn("Unsafe [31m title ]0;bad", plain)
            self.assertIn("needs you", plain)
            self.assertLessEqual(
                max(display_cells(line) for line in output.splitlines()[2:]),
                99,
            )
        finally:
            fixture.close()

    def test_provider_abbreviations_use_distinct_contrasting_colors(self) -> None:
        fixture = LoginFixture(
            inventory(
                row("claude-color", number=1, provider="claude"),
                row("codex-color", number=2, provider="codex"),
            )
        )
        try:
            code, output = run_pty(
                fixture,
                b"q\n",
                columns=120,
                env_updates={
                    "NO_COLOR": None,
                    "SESSION_KIT_NO_COLOR": None,
                    "TERM": "xterm-ghostty",
                },
            )
            self.assertEqual(0, code)
            self.assertIn(
                "\x1b[1m\x1b[38;2;249;215;108mCLD\x1b[0m",
                output,
            )
            self.assertIn(
                "\x1b[1m\x1b[38;2;64;216;209mCDX\x1b[0m",
                output,
            )
        finally:
            fixture.close()

    def test_unknown_provider_badge_is_neutral_not_claude_yellow(self) -> None:
        unknown = row("unknown-color", number=4, provider="other")
        fixture = LoginFixture(inventory(unknown))
        try:
            code, output = run_pty(
                fixture,
                b"q\n",
                columns=120,
                env_updates={
                    "NO_COLOR": None,
                    "SESSION_KIT_NO_COLOR": None,
                    "TERM": "xterm-ghostty",
                },
            )
            self.assertEqual(0, code)
            self.assertIn(
                "\x1b[1m\x1b[38;2;205;210;220mUNK\x1b[0m", output
            )
            self.assertNotIn(
                "\x1b[1m\x1b[38;2;249;215;108mUNK\x1b[0m", output
            )
        finally:
            fixture.close()

    def test_color_is_disabled_by_presence_or_unsupported_terminal(self) -> None:
        cases = (
            (
                "session-kit-empty",
                {
                    "SESSION_KIT_NO_COLOR": "",
                    "NO_COLOR": None,
                    "TERM": "xterm",
                },
            ),
            (
                "no-color-empty",
                {
                    "SESSION_KIT_NO_COLOR": None,
                    "NO_COLOR": "",
                    "TERM": "xterm",
                },
            ),
            (
                "dumb",
                {
                    "SESSION_KIT_NO_COLOR": None,
                    "NO_COLOR": None,
                    "TERM": "dumb",
                },
            ),
            (
                "missing-term",
                {
                    "SESSION_KIT_NO_COLOR": None,
                    "NO_COLOR": None,
                    "TERM": None,
                },
            ),
        )
        for label, updates in cases:
            with self.subTest(label=label):
                fixture = LoginFixture(
                    inventory(
                        row(
                            f"reply-{label}",
                            number=1,
                            provider="codex",
                            needs_you=True,
                        )
                    )
                )
                try:
                    code, output = run_pty(
                        fixture, b"q\n", env_updates=updates
                    )
                    self.assertEqual(0, code)
                    if updates.get("TERM") in {"dumb", None}:
                        self.assertNotIn("\x1b", output)
                    else:
                        plain = strip_screen_control(output)
                        self.assertNotIn("\x1b", plain)
                    self.assertIn("needs you", output)
                finally:
                    fixture.close()

    def test_output_age_is_conditional_and_quiet_threshold_is_protected(self) -> None:
        current = row("current", number=1, recent_output_at_unix_ms=4)
        minute = row("minute", number=2, recent_output_at_unix_ms=3)
        minute["recent_output_age_seconds"] = 60
        forty_four = row("forty-four", number=3, recent_output_at_unix_ms=2)
        forty_four["recent_output_age_seconds"] = 2699
        quiet = row("quiet", number=4, recent_output_at_unix_ms=1)
        quiet["recent_output_age_seconds"] = 2700
        fixture = LoginFixture(inventory(current, minute, forty_four, quiet))
        try:
            code, output = run_pty(fixture, b"q\n", columns=180)
            self.assertEqual(0, code)
            self.assertNotIn("recent output", output)
            self.assertNotIn("last output now", output)
            self.assertNotIn("last output", output)
            self.assertIn("| CDX | pe… | pe… | working", output)
        finally:
            fixture.close()

        crowded = row("crowded", number=8, recent_output_at_unix_ms=1)
        crowded["recent_output_age_seconds"] = 1800
        crowded["display_title"] = (
            "A deliberately long title that gives optional output age no room"
        )
        crowded["subagents"] = [{"status": "open"} for _ in range(20)]
        fixture = LoginFixture(inventory(crowded))
        try:
            code, output = run_pty(fixture, b"q\n", columns=60)
            self.assertEqual(0, code)
            self.assertIn("| working", output)
            self.assertNotIn("last output 30m", output)
            self.assertIn("working | 20 subagents | 30m", output)
        finally:
            fixture.close()

    def test_main_rows_show_state_alias_and_opened_age_in_aligned_columns(self) -> None:
        """Every row carries one labelled age, and the columns line up.

        The response and waiting ages this row once showed came from the
        retired attention queue; `opened` is the one age the collector still
        records, so it is the one every row is checked against here.
        """
        now_ms = int(time.time() * 1000)
        ready = row("recent-ready", number=2, provider="claude")
        ready["agent_status"] = "idle"
        ready["account_alias"] = "main"
        ready["started_at_unix_ms"] = now_ms - 55 * 60 * 1000
        attached = row(
            "working-elsewhere",
            number=8,
            provider="codex",
            availability="attached",
        )
        attached["agent_status"] = "working"
        attached["account_alias"] = "backup"
        attached["started_at_unix_ms"] = now_ms - 3 * 3600 * 1000
        waiting = row(
            "waiting-ready",
            number=9,
            provider="claude",
            needs_you=True,
        )
        waiting["account_alias"] = "spare"
        waiting["started_at_unix_ms"] = now_ms - 12 * 3600 * 1000
        shell = row("plain-shell", number=10, provider="shell")
        shell["agent_status"] = "idle"
        shell["started_at_unix_ms"] = now_ms - 2 * 3600 * 1000
        fixture = LoginFixture(inventory(ready, attached, waiting, shell))
        try:
            code, output = run_pty(fixture, b"q\n", columns=200)
            self.assertEqual(0, code)
            self.assertIn("Ready", output)
            self.assertIn("Open elsewhere", output)
            self.assertRegex(output, r"needs you \| last active pending")
            self.assertRegex(output, r"working\s+\| last active pending")
            self.assertIn("| CLD | main   | pending  | needs you", output)
            self.assertIn("| CDX | backup | pending  | working", output)
            self.assertIn("| CLD | spare  | pending  | needs you", output)
            self.assertRegex(output, r"\| SHL \| pendi… \| no model\s+\| needs you")
            # One time phrase, one starting column, on every row.
            rows_shown = [
                line for line in strip_sgr(output).splitlines() if " | CLD | " in line
                or " | CDX | " in line or " | SHL | " in line
            ]
            self.assertEqual(4, len(rows_shown), rows_shown)
            self.assertEqual(
                1, len({line.index("last active") for line in rows_shown}), rows_shown
            )
            lines = {
                name: next(line for line in output.splitlines() if name in line)
                for name in (
                    "recent-ready",
                    "working-elsewhere",
                    "waiting-ready",
                    "plain-shell",
                )
            }
            self.assertEqual(
                1,
                len(
                    {
                        lines["recent-ready"].index("CLD"),
                        lines["working-elsewhere"].index("CDX"),
                        lines["waiting-ready"].index("CLD"),
                        lines["plain-shell"].index("SHL"),
                    }
                ),
            )
            # The state column starts at one place on every row -- and there
            # are two words for it now, not three.
            self.assertEqual(
                1,
                len(
                    {
                        lines["recent-ready"].index("needs you"),
                        lines["working-elsewhere"].index(
                            "working", lines["working-elsewhere"].index("CDX")
                        ),
                        lines["waiting-ready"].index("needs you"),
                        lines["plain-shell"].index("needs you"),
                    }
                ),
            )
            # So does the one time each row carries.
            self.assertEqual(
                1,
                len({line.index("last active") for line in lines.values()}),
            )
        finally:
            fixture.close()

    def test_narrow_main_row_drops_age_before_the_primary_state(self) -> None:
        now_ms = int(time.time() * 1000)
        item = row("narrow-age", number=8, provider="codex")
        item["display_title"] = "A useful descriptive title that should keep the available room"
        item["started_at_unix_ms"] = now_ms - 30 * 60 * 1000
        fixture = LoginFixture(inventory(item))
        try:
            code, output = run_pty(fixture, b"q\n", columns=60)
            self.assertEqual(0, code)
            # The model column costs real width, so the title yields a little
            # -- but never below the point where it names anything.
            # One or two cells shorter than before: the state-and-time pair is
            # reserved ahead of the title now, and at 60 columns the title sits
            # on its floor.
            self.assertIn("A useful de", output)
            self.assertIn("| working", output)
            self.assertNotIn("30 min ago", output)
            self.assertNotIn("last active", output)
        finally:
            fixture.close()

    def test_main_age_uses_readable_unit_and_date_boundaries(self) -> None:
        now_ms = int(time.time() * 1000)
        offsets = (5, 2 * 60, 2 * 3600, 2 * 86400, 31 * 86400)
        items = [
            row(f"age-{index}", number=index, provider="claude")
            for index in range(1, 6)
        ]
        for item, seconds in zip(items, offsets, strict=True):
            item["agent_status"] = "idle"
            item["started_at_unix_ms"] = now_ms - seconds * 1000
        fixture = LoginFixture(inventory(*items))
        try:
            code, output = run_pty(fixture, b"q\n", columns=220)
            self.assertEqual(0, code)
            # One phrase, one shape, whatever the span: this row used to
            # switch between `just now`, `2 hr ago` and a bare date.
            for line in strip_sgr(output).splitlines():
                if " | CLD | " not in line:
                    continue
                self.assertIn("| last active ", line)
        finally:
            fixture.close()

    def test_snapshot_control_text_is_sanitized_at_render_boundary(self) -> None:
        unsafe = row("main1", number=1)
        unsafe.update(
            {
                "display_provider": "codex\x1b[31m",
                "display_title": "Unsafe\x1b]0;title\u0007界 e\u0301",
                "display_shpool_id": "main\x1b[2J1",
                "agent_status": "working\x1b[99m",
            }
        )
        document = inventory(unsafe, stale=True)
        document["source"] = "cache\x1b[31m"
        document["outside_agents"] = [
            {
                "provider": "codex",
                "display_provider": "bad\u202eprovider",
                "identity": {
                    "uuid": "22222222-2222-4222-8222-22222222\x1b22",
                    "pid": 9001,
                    "process_start_ticks": 90010,
                },
                "title": "Outside",
                "display_title": "Outside\x1b]2;bad\u0007",
                "cwd": "/srv/\x1b[2Joutside",
                "agent_status": "running",
                "subagents": [],
            }
        ]
        fixture = LoginFixture(document)
        try:
            code, output = run_pty(
                fixture, b"q\n", lines=24, columns=60
            )
            self.assertEqual(0, code)
            plain = strip_screen_control(output)
            self.assertNotIn("\x1b", plain)
            self.assertIn("↵ open 1 · # · kill k #", output)
            self.assertFalse(
                any(
                    unicodedata.category(character).startswith("C")
                    for character in plain.replace("\n", "").replace("\r", "")
                )
            )
            self.assertIn("| pending | pending", output)
            self.assertIn("Showing a cached list.", output)
            self.assertNotIn("[22222222]", output)
            self.assertLessEqual(
                max(display_cells(line) for line in output.splitlines()[2:]),
                59,
            )
        finally:
            fixture.close()

    def test_context_history_close_and_ai_rename_use_proof_bound_children(self) -> None:
        fixture = LoginFixture(
            inventory(
                row(
                    "open1",
                    number=9,
                    provider="codex",
                    availability="attached",
                )
            )
        )
        try:
            code, output = run_pty(
                fixture,
                b"9\n2\nk 9\nname 9\nBetter name\nq\n",
            )
            self.assertEqual(0, code)
            self.assertIn("Open elsewhere.", output)
            entries = fixture.sp_entries()
            self.assertEqual(
                ["picker-history", "picker-close", "picker-name"],
                [entry["args"][0] for entry in entries],
            )
            self.assertEqual("Better name", entries[-1]["args"][2])
            self.assertTrue(
                all(entry["proof"]["shpool_id"] == "open1" for entry in entries)
            )
        finally:
            fixture.close()

    def test_the_close_key_is_proof_bound(self) -> None:
        for command in ("k 9", "K 9", "k9", "K9"):
            with self.subTest(command=command):
                fixture = LoginFixture(
                    inventory(
                        row(
                            "open1",
                            number=9,
                            provider="codex",
                            availability="attached",
                        )
                    )
                )
                try:
                    # The close, then q: nothing stands between the typed
                    # number and the close, and leaving is its own key.
                    code, output = run_pty(
                        fixture,
                        f"{command}\nq\n".encode(),
                    )
                    self.assertEqual(0, code)
                    self.assertNotIn("There is no such key", output)
                    self.assertNotIn("[y/N]", output)
                    self.assertNotIn("↵ again leaves the picker.", output)
                    entries = fixture.sp_entries()
                    self.assertEqual(1, len(entries))
                    self.assertEqual("picker-close", entries[0]["args"][0])
                    self.assertEqual("open1", entries[0]["proof"]["shpool_id"])
                finally:
                    fixture.close()

    def test_kill_accepts_lists_and_ranges_and_refuses_on_one_bad_token(
        self,
    ) -> None:
        fixture = LoginFixture(
            inventory(
                row("open1", number=4, provider="codex"),
                row("open2", number=5, provider="codex"),
                row("open3", number=6, provider="claude"),
            )
        )
        try:
            code, output = run_pty(fixture, b"k 4, 6\nq\n")
            self.assertEqual(0, code)
            entries = fixture.sp_entries()
            self.assertEqual(2, len(entries))
            self.assertEqual(
                {("picker-close", "open1"), ("picker-close", "open3")},
                {
                    (entry["args"][0], entry["proof"]["shpool_id"])
                    for entry in entries
                },
            )
        finally:
            fixture.close()
        # A range expands; one unshown number refuses the WHOLE request.
        fixture = LoginFixture(
            inventory(
                row("open1", number=4, provider="codex"),
                row("open2", number=5, provider="codex"),
            )
        )
        try:
            code, output = run_pty(fixture, b"k 4-9\nk 4-5\nq\n")
            self.assertEqual(0, code)
            self.assertIn("There is no session 6 on this screen.", output)
            entries = fixture.sp_entries()
            self.assertEqual(2, len(entries))
            self.assertEqual(
                {"open1", "open2"},
                {entry["proof"]["shpool_id"] for entry in entries},
            )
        finally:
            fixture.close()

    def test_kill_all_closes_exactly_what_the_page_is_showing(self) -> None:
        """`all` is the same word the recovery and project screens take."""
        fixture = LoginFixture(
            inventory(
                row("open1", number=4, provider="codex"),
                row("open2", number=5, provider="codex"),
                row("open3", number=6, provider="claude"),
            )
        )
        try:
            code, output = run_pty(fixture, b"k all\nq\n")
            self.assertEqual(0, code)
            self.assertEqual(
                {"open1", "open2", "open3"},
                {entry["proof"]["shpool_id"] for entry in fixture.sp_entries()},
            )
            self.assertNotIn("There is no session number in that", output)
        finally:
            fixture.close()

    def test_closing_asks_nothing_and_closes_exactly_the_number_typed(
        self,
    ) -> None:
        """No confirmation, anywhere: the number was typed against the rows.

        The close used to ask twice in two grammars -- here, and again inside
        the proof-bound child -- while the screen promised the session was
        restorable.
        """
        fixture = LoginFixture(
            inventory(
                row("open1", number=4, provider="codex"),
                row("open2", number=5, provider="codex"),
            )
        )
        try:
            code, output = run_pty(fixture, b"k 4\nq\n")
            self.assertEqual(0, code)
            self.assertNotIn("[y/N]", output)
            self.assertNotIn("cannot be undone", output)
            self.assertNotIn("Nothing closed.", output)
            entries = fixture.sp_entries()
            self.assertEqual(
                [("picker-close", "open1")],
                [(entry["args"][0], entry["proof"]["shpool_id"]) for entry in entries],
            )
            # The child's guard is answered by naming the exact session, so a
            # close behaves the same whether or not a terminal is attached.
            self.assertEqual("open1", entries[0]["confirm_id"])
        finally:
            fixture.close()

    def test_kill_all_refuses_a_page_that_moved_under_the_typing(self) -> None:
        """`all` means the page that was read, not the page that arrived.

        The background refresh re-sorts the list on its own, so a mass close
        resolved at Enter could close a set nobody had seen -- and nothing
        stands between that Enter and the kill.
        """
        first = row("alpha", number=1, provider="codex")
        second = row("bravo", number=2, provider="codex")
        fixture = LoginFixture(
            inventory(first), refreshed_document=inventory(first, second)
        )
        try:
            code, output = run_pty(
                fixture,
                b"k al",
                env_updates={
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "2",
                    "SESSION_KIT_NO_COLOR": None,
                    "SESSION_KIT_PICKER_FILTER_LIVE": "0",
                },
                deferred=("Codex bravo", b"l\nq\n"),
            )
            self.assertEqual(0, code)
            self.assertIn("The list changed. Nothing changed.", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_kill_shortcut_refuses_invalid_or_unshown_numbers(self) -> None:
        fixture = LoginFixture(inventory(row("open1", number=9)))
        try:
            code, output = run_pty(fixture, b"k nope\nk 99\nq\n")
            self.assertEqual(0, code)
            self.assertIn(
                "There is no session number in that. Numbers shown here work.",
                output,
            )
            self.assertIn(
                "There is no session 99 on this screen. Numbers shown here work.",
                output,
            )
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_help_presents_the_close_key_and_nothing_that_is_not_a_key(
        self,
    ) -> None:
        fixture = LoginFixture(inventory())
        try:
            code, output = run_pty(fixture, b"?\n\nq\n", columns=110)
            self.assertEqual(0, code)
            self.assertIn(
                "k numbers     Kill sessions: k 5 · k 5, 6, 8 · k 4-7 · k all",
                output,
            )
            # Rows of the open-session action menu are not list keys, and the
            # x alias is gone: help names keys, and only keys.
            self.assertNotIn("Compatibility alias", output)
            self.assertNotIn("action 4", output)
            self.assertNotIn("action 5", output)
            self.assertNotIn("after confirming", output)
        finally:
            fixture.close()

    def test_name_reset_and_fork_use_exact_proof_bound_terminal_number(self) -> None:
        exact = row("exact-ai", number=88005553535, provider="claude")
        exact["row"] = 1
        fixture = LoginFixture(inventory(exact))
        try:
            code, output = run_pty(
                fixture,
                b"name reset 88005553535\nfork 88005553535\nq\n",
                columns=80,
            )
            self.assertEqual(0, code)
            entries = fixture.sp_entries()
            self.assertEqual(
                ["picker-name-reset", "picker-fork"],
                [entry["args"][0] for entry in entries],
            )
            self.assertTrue(
                all(
                    entry["proof"]["shpool_id"] == "exact-ai"
                    and entry["proof"]["uuid"] == exact["identity"]["uuid"]
                    for entry in entries
                )
            )
            self.assertNotIn("Only exact Claude", output)
        finally:
            fixture.close()

    def test_fork_and_name_reset_refuse_nonexact_or_hidden_numbers(self) -> None:
        shell = row("plain", number=91, provider="shell")
        hidden = row("hidden", number=700, provider="codex")
        fixture = LoginFixture(inventory(shell, hidden))
        try:
            code, output = run_pty(
                fixture,
                b"/plain\nfork 91\nname reset 91\nfork 700\nq\n",
            )
            self.assertEqual(0, code)
            self.assertIn(
                "Only exact Claude and Codex conversations can be forked.",
                output,
            )
            self.assertIn(
                "Only exact Claude and Codex conversations can reset a custom name.",
                output,
            )
            self.assertIn(
                "There is no session 700 on this screen. Numbers shown here work.",
                output,
            )
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_long_silent_running_session_reports_its_silence(self) -> None:
        """"running" must not be printed for a session that stopped producing.

        A Codex run froze for eight hours while the list kept calling it
        running, which is how it went unnoticed overnight.
        """
        stalled = row("ready1", number=1, recent_output_at_unix_ms=1)
        stalled["agent_status"] = "running"
        stalled["recent_output_age_seconds"] = 28_170  # 7h 49m
        fixture = LoginFixture(inventory(stalled))
        try:
            code, output = run_pty(fixture, b"q\n", columns=200)
            self.assertEqual(0, code)
            self.assertIn("| needs you", output)
            self.assertNotIn("| running", output)
            self.assertNotIn("| idle", output)
        finally:
            fixture.close()

    def test_briefly_quiet_running_session_is_still_running(self) -> None:
        """A slow tool call or a long model turn must never be flagged."""
        busy = row("ready1", number=1, recent_output_at_unix_ms=1)
        busy["agent_status"] = "running"
        busy["recent_output_age_seconds"] = 1800  # 30m
        fixture = LoginFixture(inventory(busy))
        try:
            code, output = run_pty(fixture, b"q\n", columns=200)
            self.assertEqual(0, code)
            self.assertIn("| working", output)
            self.assertNotIn("| idle", output)
        finally:
            fixture.close()

    def test_narrow_terminal_keeps_subagents_and_staleness(self) -> None:
        stalled = row("ready1", number=1, recent_output_at_unix_ms=1)
        stalled["agent_status"] = "running"
        stalled["recent_output_age_seconds"] = 28_170
        stalled["subagents"] = [
            {"provider": "codex", "status": "open", "title": f"agent{index}"}
            for index in range(20)
        ]
        stalled["title"] = "A deliberately long session title that crowds the row"
        stalled["native_title"] = stalled["title"]
        fixture = LoginFixture(inventory(stalled))
        try:
            code, output = run_pty(fixture, b"q\n", columns=80)
            self.assertEqual(0, code)
            self.assertIn("| needs you", output)
            self.assertIn("20 subagents", output)
            self.assertIn("7h 49m", output)
        finally:
            fixture.close()

    def test_refused_action_refreshes_instead_of_exiting_picker(self) -> None:
        fixture = LoginFixture(inventory(row("ready1", number=1)))
        try:
            code, output = run_pty(fixture, b"1\nq\n", sp_exit=74)
            self.assertEqual(0, code)
            self.assertIn("changed or failed a safety check", output)
            self.assertNotIn("terminal died", output)
            snapshots = [
                entry for entry in fixture.status_entries() if entry == ["--json"]
            ]
            self.assertGreaterEqual(len(snapshots), 2)
        finally:
            fixture.close()

    def test_attach_failure_is_not_diagnosed_as_a_dead_terminal(self) -> None:
        fixture = LoginFixture(inventory(row("ready1", number=1)))
        try:
            code, output = run_pty(fixture, b"1\nq\n", sp_exit=75)
            self.assertEqual(0, code)
            self.assertIn("Could not open session 1. It was left alone.", output)
            # The picker never diagnoses a session it could not reach.
            self.assertNotIn("dead", output)
            entries = fixture.sp_entries()
            commands = [entry["args"][0] for entry in entries]
            self.assertIn("picker-open", commands)
            self.assertNotIn("picker-recover", commands)
        finally:
            fixture.close()

    def test_unknown_open_failure_never_offers_unproven_recovery(self) -> None:
        unknown = row("unknown1", number=4, provider="unknown")
        unknown["identity"]["uuid"] = None
        unknown["display_provider"] = "codex"
        unknown["setup_incomplete"] = True
        fixture = LoginFixture(inventory(unknown))
        try:
            code, output = run_pty(fixture, b"4\nq\n", sp_exit=7)
            self.assertEqual(0, code)
            self.assertIn("Could not open session 4. It was left alone.", output)
            self.assertNotIn("terminal died", output)
            commands = [entry["args"][0] for entry in fixture.sp_entries()]
            self.assertNotIn("picker-recover", commands)
        finally:
            fixture.close()

    def test_cached_row_selection_is_read_only_before_proof_or_sp(self) -> None:
        fixture = LoginFixture(inventory(row("ready1", number=1), stale=True))
        try:
            code, output = run_pty(fixture, b"1\nk 1\nq\n")
            self.assertEqual(0, code)
            self.assertIn("Showing a cached list. Actions are off until it refreshes.", output)
            self.assertIn("Nothing changed", output)
            self.assertEqual([], fixture.sp_entries())
            self.assertNotIn("terminal died", output)
        finally:
            fixture.close()

    def test_ordinary_rows_hide_ids_but_search_still_matches_them(self) -> None:
        hidden = row("private-session-id", number=8)
        hidden["title"] = "Picker safety work"
        hidden["display_title"] = "Picker safety work"
        fixture = LoginFixture(inventory(hidden))
        try:
            code, output = run_pty(fixture, b"q\n", columns=120)
            self.assertEqual(0, code)
            self.assertIn("Picker safety work", output)
            self.assertNotIn("private-session-id", output)
        finally:
            fixture.close()

        fixture = LoginFixture(inventory(hidden))
        try:
            code, output = run_pty(
                fixture, b"/private-session-id\nq\n", columns=120
            )
            self.assertEqual(0, code)
            self.assertIn("1 match of 1 session", output)
            self.assertIn("Picker safety work", output)
        finally:
            fixture.close()

    def test_needs_you_opens_the_review_screen_and_its_back_key_goes_back(self) -> None:
        """The screen advertises `Back: Enter or q`, and that must hold.

        The Codex prompt-quarantine rows this screen once carried retired with
        the intake surface; what survives is the review screen itself and the
        promise that `a` reaches it and `q` returns to the list.
        """
        waiting = row("waits", number=6, needs_you=True)
        waiting["title"] = "Deploy review"
        waiting["agent_status"] = "needs your reply"
        fixture = LoginFixture(inventory(waiting))
        try:
            code, output = run_pty(fixture, b"a\nq\nq\n", columns=140)
            self.assertEqual(0, code)
            self.assertIn("Deploy review", output)
            self.assertIn("needs you:", output)
            self.assertNotIn("Unknown choice", output)
            # q returned to the list rather than dropping to a bare shell, so
            # the q after it is what ends the picker.
            self.assertIn("# · kill k #", output)
        finally:
            fixture.close()

    def test_available_unknown_provider_delegates_exact_proof_open(self) -> None:
        unknown = row("unknown1", number=4, provider="unknown")
        unknown["identity"]["uuid"] = None
        unknown["display_provider"] = "codex"
        unknown["display_title"] = "Codex pending"
        unknown["setup_incomplete"] = True
        fixture = LoginFixture(inventory(unknown))
        try:
            code, output = run_pty(fixture, b"4\nq\n")
            self.assertEqual(0, code)
            self.assertIn("Codex pending", output)
            self.assertIn("| CDX | pe… | pe… | pending", output)
            entries = fixture.sp_entries()
            self.assertEqual(1, len(entries))
            self.assertEqual("picker-open", entries[0]["args"][0])
            self.assertEqual("unknown", entries[0]["proof"]["provider"])
            self.assertEqual("", entries[0]["proof"]["uuid"])
            self.assertEqual("unknown1", entries[0]["proof"]["shpool_id"])
        finally:
            fixture.close()

    def test_outside_provider_root_uses_separate_private_read_only_view(self) -> None:
        document = inventory()
        document["outside_agents"] = [
            {
                "provider": "codex",
                "identity": {
                    "uuid": "22222222-2222-4222-8222-222222222222",
                    "pid": 9001,
                    "process_start_ticks": 90010,
                },
                "title": (
                    "Outside private-account "
                    "/home/private-account/secret-work/project-alpha"
                ),
                "cwd": "/home/private-account/secret-work/project-alpha",
                "agent_status": "running",
                "subagents": [],
            }
        ]
        fixture = LoginFixture(document)
        try:
            code, output = run_pty(fixture, b"o\n\n1\nq\n")
            self.assertEqual(0, code)
            self.assertIn("Sessions: none.", output)
            self.assertNotIn("0 sessions", output)
            self.assertIn("more m", output)
            self.assertIn("Outside the kit", output)
            self.assertIn(
                "Live sessions outside the kit. Not openable here.",
                output,
            )
            self.assertIn("Outside account project-alpha", output)
            self.assertIn("project: project-alpha | not openable here", output)
            self.assertNotIn("/home/private-account", output)
            self.assertNotIn("private-account", output)
            self.assertNotIn("Page 1/", output)
            self.assertIn("on this screen. Numbers shown here work.", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_many_outside_roots_do_not_change_main_count_or_paging(self) -> None:
        document = inventory()
        document["outside_agents"] = [
            {
                "provider": "claude" if number % 2 else "codex",
                "identity": {
                    "uuid": f"22222222-2222-4222-8222-{number:012d}",
                    "pid": 9000 + number,
                    "process_start_ticks": 90_000 + number,
                },
                "title": f"Outside provider {number}",
                "cwd": f"/home/private-account/projects/project-{number}",
                "agent_status": "running",
                "subagents": [],
            }
            for number in range(1, 31)
        ]
        fixture = LoginFixture(document)
        try:
            code, output = run_pty(fixture, b"q\n", lines=12)
            self.assertEqual(0, code)
            self.assertIn("Sessions: none.", output)
            self.assertNotIn("0 sessions", output)
            self.assertIn("more m", output)
            self.assertNotIn("Page 1/", output)
            self.assertNotIn("Outside provider 1", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_guided_new_uses_first_project_default_and_child_sp(self) -> None:
        fixture = LoginFixture(inventory())
        try:
            code, output = run_pty(fixture, b"n\n2\n1\n\nq\n")
            self.assertEqual(0, code)
            self.assertIn("New session", output)
            self.assertIn("use main:", output)
            self.assertIn("Name it later with name number.", output)
            self.assertNotIn("Type yes to start", output)
            self.assertNotIn("New session cancelled", output)
            self.assertEqual(
                [["new", "codex", "main", "--account", "wren"]],
                [entry["args"] for entry in fixture.sp_entries()],
            )
        finally:
            fixture.close()

    def test_guided_new_takes_claude_code_on_enter(self) -> None:
        """Enter on the provider screen sent the most common act backwards.

        It answered `back`, so the one key a menu trains you to press left
        the New session screen without starting anything.
        """
        fixture = LoginFixture(inventory())
        try:
            code, output = run_pty(fixture, b"n\n\n1\n\nq\n")
            self.assertEqual(0, code)
            self.assertIn("↵ Claude Code · b back", output)
            # Past the provider screen, not back out of it: the account step
            # is what a Claude session asks next.
            self.assertIn("account ❯", output)
            self.assertEqual(
                [["new", "claude", "main", "--account", "wren"]],
                [entry["args"] for entry in fixture.sp_entries()],
            )
        finally:
            fixture.close()

    def test_guided_new_still_goes_back_on_b(self) -> None:
        fixture = LoginFixture(inventory())
        try:
            code, output = run_pty(fixture, b"n\nb\nq\n")
            self.assertEqual(0, code)
            self.assertIn("New session", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_guided_new_lists_a_directory_once_across_providers(self) -> None:
        fixture = LoginFixture(inventory())
        try:
            # The provider is chosen before the project list, so a second row
            # for the same directory would render as a duplicate line.
            fixture.projects.write_text(
                f"main\tclaude\t{fixture.primary_project}\n"
                f"main-codex\tcodex\t{fixture.primary_project}\n"
                f"other\tcodex\t{fixture.other}\n",
                encoding="utf-8",
            )
            code, output = run_pty(fixture, b"n\n2\n1\n\nq\n")
            self.assertEqual(0, code)
            self.assertNotIn("main-codex:", output)
            self.assertIn(f"   1  main:  {fixture.primary_project}", output)
            self.assertIn(f"   2  other: {fixture.other}", output)
            self.assertEqual(
                [["new", "codex", "main", "--account", "wren"]],
                [entry["args"] for entry in fixture.sp_entries()],
            )
        finally:
            fixture.close()

    def test_projects_screen_adds_a_directory_with_a_suggested_name(self) -> None:
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        try:
            target = fixture.base / "newsite"
            target.mkdir()
            # More, Projects, Add, the directory, accept the suggested short
            # name, back out, quit. There is no provider to accept: which one
            # opens a directory is answered when a session starts.
            code, output = run_pty(
                fixture,
                f"m\np\na\n{target}\n\n\n\nq\n".encode(),
                env_updates={"SESSION_KIT_PICKER_REFRESH_SECONDS": "0"},
            )
            self.assertEqual(0, code)
            self.assertIn("Projects", output)
            self.assertIn(f"main:  {fixture.primary_project}", output)
            self.assertIn("Short name [newsite]", output)
            self.assertIn(f"Added newsite: {target}", output)
            self.assertIn(
                f"newsite\tany\t{target}",
                fixture.projects.read_text(encoding="utf-8"),
            )
        finally:
            fixture.close()

    # The undo-a-drop round trip lives in tests/test_picker_projects.py, on
    # the confirmed-hide flow that superseded the immediate drop.

    def test_projects_screen_drops_a_directory_for_good(self) -> None:
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        try:
            code, output = run_pty(
                fixture,
                b"m\np\nd 2\ny\n\n\nq\n",
                env_updates={"SESSION_KIT_PICKER_REFRESH_SECONDS": "0"},
            )
            self.assertEqual(0, code)
            self.assertIn("stays out of the project list", output)
            contents = fixture.projects.read_text(encoding="utf-8")
            self.assertIn(f"other\tignore\t{fixture.other}", contents)
            self.assertIn(f"main\tclaude\t{fixture.primary_project}", contents)
        finally:
            fixture.close()

    def test_every_keystroke_draws_one_frame_instead_of_stacking_menus(self) -> None:
        """The screen the picker leaves behind is the same every time.

        The menu used to be appended below whatever was already there, while
        the background repaint replaced the screen in place: the same key left
        a different screen depending on when it was pressed."""
        fixture = LoginFixture(inventory(row("one", number=1)))
        try:
            code, output = run_pty(fixture, b"c\nc\nq\n")
            self.assertEqual(0, code)
            # Startup, both compact toggles, and the q that leaves.
            self.assertEqual(4, output.count("\033[2J"))
            frame = output.rsplit("\033[2J", 2)[1]
            self.assertIn("Compact rows off.", frame)
            self.assertEqual(
                1, frame.count("# · kill k # · new n · more m")
            )
        finally:
            fixture.close()

    def test_idle_menu_repaints_itself_when_the_estate_changes(self) -> None:
        first = row("alpha", number=1)
        second = row("bravo", number=2)
        fixture = LoginFixture(
            inventory(first), refreshed_document=inventory(first, second)
        )
        try:
            code, output = run_pty(
                fixture,
                env_updates={
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "2",
                    # A repaint replaces the menu with terminal control
                    # sequences, so it only runs on a terminal that accepts them.
                    "SESSION_KIT_NO_COLOR": None,
                },
                deferred=("Codex bravo", b"q\n"),
            )
            # No key was pressed until the second session appeared by itself.
            self.assertIn("Codex alpha", output)
            self.assertIn("Codex bravo", output)
            self.assertEqual(0, code)
            # The first draw clears once. The automatic replacement is a
            # synchronized in-place frame whose content arrives WITH the
            # cursor-home -- the screen is never erased ahead of the new
            # frame, so a slow render cannot flash a blank screen.
            before_repaint = output.index("Codex bravo")
            self.assertEqual(1, output[:before_repaint].count("\033[2J"))
            self.assertIn("\033[?2026h\033[H  ", output[:before_repaint])
            self.assertNotIn("\033[?2026h\033[H\033[J", output)
            self.assertIn("\033[J\033[?2026l", output[before_repaint:])
            self.assertEqual(1, output.count("Codex bravo"))
        finally:
            fixture.close()

    def test_hidden_other_provider_changes_do_not_repaint_home(self) -> None:
        first = inventory(row("alpha", number=1))
        refreshed = inventory(row("alpha", number=1))
        refreshed["outside_agents"] = [
            {
                "provider": "codex",
                "identity": {
                    "uuid": "22222222-2222-4222-8222-222222222222",
                    "pid": 2222,
                    "process_start_ticks": 22222,
                },
                "title": "Invisible outside process",
                "cwd": "/tmp/outside",
            }
        ]
        fixture = LoginFixture(first, refreshed_document=refreshed)
        try:
            code, output = run_pty(
                fixture,
                env_updates={
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "2",
                    "SESSION_KIT_NO_COLOR": None,
                },
                deferred=("Codex alpha", b"q\n"),
                deferred_delay_seconds=3,
            )
            self.assertEqual(0, code)
            # Nothing repainted: no synchronized in-place frame was drawn at
            # all, and the screen cleared exactly twice -- once at startup and
            # once for the keystroke that ended the picker.
            self.assertNotIn("\033[?2026h", output)
            self.assertEqual(2, output.count("\033[2J"))
            self.assertNotIn("Invisible outside process", output)
        finally:
            fixture.close()

    def test_help_and_more_remain_modal_across_refresh_intervals(self) -> None:
        first = inventory(row("alpha", number=1))
        refreshed = inventory(
            row("alpha", number=1), row("bravo", number=2)
        )
        for command, marker in ((b"?\n", "Picker help"), (b"m\n", "  More")):
            with self.subTest(command=command):
                fixture = LoginFixture(first, refreshed_document=refreshed)
                try:
                    code, output = run_pty(
                        fixture,
                        command,
                        env_updates={
                            "SESSION_KIT_PICKER_REFRESH_SECONDS": "2",
                            "SESSION_KIT_NO_COLOR": None,
                        },
                        deferred=(marker, b"\nq\n"),
                        deferred_delay_seconds=3,
                    )
                    self.assertEqual(0, code)
                    modal_start = output.index(marker)
                    modal_end = output.index("Codex alpha", modal_start)
                    modal = output[modal_start:modal_end]
                    self.assertNotIn("\033[2J", modal)
                    self.assertNotIn("Codex bravo", modal)
                finally:
                    fixture.close()

    def test_every_picker_submenu_uses_interrupt_resistant_modal_reads(self) -> None:
        modules = (
            REPO / "lib/sh/shpool_login_actions.sh",
            REPO / "lib/sh/shpool_login_render.sh",
            REPO / "lib/sh/shpool_login_projects.sh",
            REPO / "lib/sh/shpool_login_recovery.sh",
        )
        offenders = []
        for path in modules:
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if "picker_read " in line:
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual([], offenders)

    def test_idle_picker_reexecs_the_release_selected_by_current(self) -> None:
        rows = tuple(
            row(f"session-{number}", number=number) for number in range(1, 51)
        )
        fixture = LoginFixture(
            inventory(*rows)
        )
        release_id = "a1b2c3d4e5f6789012345678901234567890abcd"
        install_root = fixture.base / "session-kit"
        new_release = install_root / "releases" / release_id
        launcher = new_release / "bin" / "shpool_login_launcher"
        record = fixture.base / "upgraded-picker"
        launcher.parent.mkdir(parents=True)
        write_executable(
            launcher,
            "#!/usr/bin/env bash\n"
            "printf '%s|%s|%s\\n' \"$SESSION_KIT_PICKER_RESUME_QUERY\" "
            "\"$SESSION_KIT_PICKER_RESUME_PAGE\" "
            "\"$SESSION_KIT_PICKER_COMPACT\" > \"$UPGRADE_RECORD\"\n"
            "printf 'NEW RELEASE PICKER RAN\\n'\n"
            # Outlive the handoff probation, or a truthful exit reads as a
            # corpse and the wrapper reloads the old picker.
            "sleep 1.5\n",
        )
        current = install_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(REPO)

        def activate() -> None:
            replacement = install_root / ".current-next"
            replacement.symlink_to(new_release)
            os.replace(replacement, current)

        try:
            code, output = run_pty(
                fixture,
                b"/session-\nnext\n",
                env_updates={
                    "SESSION_KIT_ROOT": os.fspath(install_root),
                    "SESSION_KIT_PICKER_COMPACT": "1",
                    "UPGRADE_RECORD": os.fspath(record),
                    "SESSION_KIT_PICKER_HANDOFF_PROBATION_SECONDS": "1",
                },
                deferred=("Page 2/", b""),
                deferred_action=activate,
            )
            self.assertEqual(0, code)
            self.assertIn(
                "Reloading the picker into release a1b2c3d4e5f6.",
                output,
            )
            self.assertIn("NEW RELEASE PICKER RAN", output)
            self.assertEqual("session-|2|1", record.read_text().strip())
            events = (fixture.state / "action-events.jsonl").read_text()
            self.assertIn("release_upgrade", events)
        finally:
            fixture.close()

    def test_release_upgrade_waits_until_a_modal_prompt_closes(self) -> None:
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        release_id = "b1c2d3e4f5a6789012345678901234567890abcd"
        install_root = fixture.base / "session-kit"
        new_release = install_root / "releases" / release_id
        launcher = new_release / "bin" / "shpool_login_launcher"
        record = fixture.base / "upgraded-picker"
        launcher.parent.mkdir(parents=True)
        write_executable(
            launcher,
            "#!/usr/bin/env bash\n"
            ": > \"$UPGRADE_RECORD\"\n"
            "printf 'NEW RELEASE PICKER RAN\\n'\n"
            "sleep 1.5\n",
        )
        current = install_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(REPO)

        def activate() -> None:
            replacement = install_root / ".current-next"
            replacement.symlink_to(new_release)
            os.replace(replacement, current)

        try:
            code, output = run_pty(
                fixture,
                b"?\n",
                env_updates={
                    "SESSION_KIT_ROOT": os.fspath(install_root),
                    "UPGRADE_RECORD": os.fspath(record),
                    "SESSION_KIT_PICKER_HANDOFF_PROBATION_SECONDS": "1",
                },
                deferred=("Picker help", b"\n"),
                deferred_action=activate,
                deferred_delay_seconds=2,
                deferred_check=lambda: self.assertFalse(record.exists()),
            )
            self.assertEqual(0, code)
            self.assertTrue(record.exists())
            self.assertLess(
                output.index("Picker help"),
                output.index("Reloading the picker into release b1c2d3e4f5a6"),
            )
        finally:
            fixture.close()

    def test_broken_current_release_warns_once_and_keeps_old_picker_alive(self) -> None:
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        release_id = "c1d2e3f4a5b6789012345678901234567890abcd"
        install_root = fixture.base / "session-kit"
        broken_release = install_root / "releases" / release_id
        broken_release.mkdir(parents=True)
        current = install_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(REPO)

        def activate() -> None:
            replacement = install_root / ".current-next"
            replacement.symlink_to(broken_release)
            os.replace(replacement, current)

        warning = (
            "Release c1d2e3f4a5b6 is unavailable. Continuing with this picker."
        )
        try:
            code, output = run_pty(
                fixture,
                env_updates={
                    "SESSION_KIT_ROOT": os.fspath(install_root),
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "0",
                },
                deferred=("Codex alpha", b"q\n"),
                deferred_action=activate,
                deferred_delay_seconds=2,
            )
            self.assertEqual(0, code)
            self.assertEqual(1, output.count(warning))
            events = (fixture.state / "action-events.jsonl").read_text()
            self.assertIn("quit", events)
            self.assertNotIn("release_upgrade", events)
        finally:
            fixture.close()

    def test_unstartable_launcher_recovers_and_never_retries_the_same_target(self) -> None:
        """No static probe proves a program will serve; the handoff does.

        A launcher whose shebang names a missing interpreter — or whose env
        command is absent — can pass every file check and die a beat after
        exec. Review lanes rv-rn-2 and rv-rn2-* proved the two failure
        shapes: a success line followed by a dead window, and a relaunch
        storm retrying the same corpse every idle beat. The supervised
        handoff runs the new picker as a foreground child: a quick abnormal
        exit is announced as the failure it is, the window gets this
        release's picker back, and the failed target is remembered across
        the exec so it is warned about once, never retried."""
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        release_id = "d1e2f3a4b5c6789012345678901234567890abcd"
        install_root = fixture.base / "session-kit"
        broken_release = install_root / "releases" / release_id
        (broken_release / "bin").mkdir(parents=True)
        launcher = broken_release / "bin" / "shpool_login_launcher"
        launcher.write_text(
            "#!/definitely/missing/interpreter\nexit 0\n", encoding="utf-8"
        )
        launcher.chmod(0o755)
        current = install_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(REPO)

        def activate() -> None:
            replacement = install_root / ".current-next"
            replacement.symlink_to(broken_release)
            os.replace(replacement, current)

        try:
            code, output = run_pty(
                fixture,
                env_updates={
                    "SESSION_KIT_ROOT": os.fspath(install_root),
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "0",
                },
                deferred=("Codex alpha", b""),
                deferred_action=activate,
                deferred_delay_seconds=2,
                # The recovered picker needs idle beats to prove it does NOT
                # retry, then a quit typed into it proves it is serving.
                followup=("Continuing with this picker", b"q\n", 6),
            )
            self.assertEqual(0, code)
            self.assertIn(
                "Reloading the picker into release d1e2f3a4b5c6.", output
            )
            self.assertIn("exited immediately (status", output)
            self.assertEqual(
                1,
                output.count("Reloading the picker into release d1e2f3a4b5c6."),
                "the failed target must never be retried",
            )
            self.assertEqual(
                1,
                output.count(
                    "Release d1e2f3a4b5c6 could not serve this window."
                    " Continuing with this picker."
                ),
            )
            events = (fixture.state / "action-events.jsonl").read_text()
            self.assertIn("release_upgrade_failed", events)
            self.assertIn("quit", events)
        finally:
            fixture.close()

    def test_zero_exit_corpse_inside_probation_recovers_the_picker(self) -> None:
        """A corpse wearing a success code is still a corpse.

        A launcher that exits 0 instantly used to read as the person leaving
        the new picker, so the wrapper exited 0 and the window lost its list
        with no refusal (review lane rv-rn3-2). A person cannot open and
        leave a picker faster than it draws: any exit inside probation is a
        failed handoff, whatever the status says."""
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        release_id = "e2f3a4b5c6d7789012345678901234567890abcd"
        install_root = fixture.base / "session-kit"
        corpse_release = install_root / "releases" / release_id
        (corpse_release / "bin").mkdir(parents=True)
        write_executable(
            corpse_release / "bin" / "shpool_login_launcher",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        current = install_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(REPO)

        def activate() -> None:
            replacement = install_root / ".current-next"
            replacement.symlink_to(corpse_release)
            os.replace(replacement, current)

        try:
            code, output = run_pty(
                fixture,
                env_updates={
                    "SESSION_KIT_ROOT": os.fspath(install_root),
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "0",
                },
                deferred=("Codex alpha", b""),
                deferred_action=activate,
                deferred_delay_seconds=2,
                followup=("Continuing with this picker", b"q\n", 6),
            )
            self.assertEqual(0, code)
            self.assertIn(
                "The release e2f3a4b5c6d7 picker ended before it could serve.",
                output,
            )
            self.assertEqual(
                1,
                output.count("Reloading the picker into release e2f3a4b5c6d7."),
                "the failed target must never be retried",
            )
            events = (fixture.state / "action-events.jsonl").read_text()
            self.assertIn("release_upgrade_failed", events)
            self.assertIn("quit", events)
        finally:
            fixture.close()

    def test_octal_looking_probation_neither_aborts_nor_serves_a_corpse(self) -> None:
        """"08" is decimal eight here, not broken octal.

        Bash arithmetic reads a leading zero as octal and aborts on "08" —
        review round five drove the picker to die after the reload line with
        no recovery. The value is forced to base ten; the corpse still
        recovers under the eight-second probation it now means."""
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        release_id = "b5c6d7e8f9a0789012345678901234567890abcd"
        install_root = fixture.base / "session-kit"
        corpse_release = install_root / "releases" / release_id
        (corpse_release / "bin").mkdir(parents=True)
        write_executable(
            corpse_release / "bin" / "shpool_login_launcher",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        current = install_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(REPO)

        def activate() -> None:
            replacement = install_root / ".current-next"
            replacement.symlink_to(corpse_release)
            os.replace(replacement, current)

        try:
            code, output = run_pty(
                fixture,
                env_updates={
                    "SESSION_KIT_ROOT": os.fspath(install_root),
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "0",
                    "SESSION_KIT_PICKER_HANDOFF_PROBATION_SECONDS": "08",
                },
                deferred=("Codex alpha", b""),
                deferred_action=activate,
                deferred_delay_seconds=2,
                followup=("Continuing with this picker", b"q\n", 10),
            )
            self.assertEqual(0, code)
            self.assertIn(
                "The release b5c6d7e8f9a0 picker ended before it could serve.",
                output,
            )
            events = (fixture.state / "action-events.jsonl").read_text()
            self.assertIn("release_upgrade_failed", events)
            self.assertIn("quit", events)
        finally:
            fixture.close()

    def test_zero_probation_is_invalid_and_still_catches_the_corpse(self) -> None:
        """A probation of zero is no probation at all, so it is invalid.

        "0" passed the digit check, zeroed the window, and an instant
        status-0 corpse was announced as served — the picker ended with no
        refusal and no recovery (review lane rv-rn6-1). The window exists to
        catch corpses; a value that cannot catch one falls back to the
        default five seconds. "000000" reaches the same rejected-zero branch
        after base-ten forcing."""
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        release_id = "c6d7e8f9a0b1789012345678901234567890abcd"
        install_root = fixture.base / "session-kit"
        corpse_release = install_root / "releases" / release_id
        (corpse_release / "bin").mkdir(parents=True)
        write_executable(
            corpse_release / "bin" / "shpool_login_launcher",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        current = install_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(REPO)

        def activate() -> None:
            replacement = install_root / ".current-next"
            replacement.symlink_to(corpse_release)
            os.replace(replacement, current)

        try:
            code, output = run_pty(
                fixture,
                env_updates={
                    "SESSION_KIT_ROOT": os.fspath(install_root),
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "0",
                    "SESSION_KIT_PICKER_HANDOFF_PROBATION_SECONDS": "0",
                },
                deferred=("Codex alpha", b""),
                deferred_action=activate,
                deferred_delay_seconds=2,
                followup=("Continuing with this picker", b"q\n", 10),
            )
            self.assertEqual(0, code)
            self.assertIn(
                "The release c6d7e8f9a0b1 picker ended before it could serve.",
                output,
            )
            self.assertEqual(
                1,
                output.count("Reloading the picker into release c6d7e8f9a0b1."),
                "the failed target must never be retried",
            )
            events = (fixture.state / "action-events.jsonl").read_text()
            self.assertIn("release_upgrade_failed", events)
            self.assertIn("quit", events)
        finally:
            fixture.close()

    def test_missing_bash5_clock_still_catches_the_corpse(self) -> None:
        """A shell without EPOCHREALTIME must not die reading it.

        EPOCHREALTIME is a Bash 5.0 variable and the kit promises Bash 4:
        under nounset the bare expansion aborted the picker right after the
        reload line — no refusal, no recovery, the list gone (review lane
        rv-rn6-2, reproduced by unsetting the variable through BASH_ENV on
        the real production path). The guarded expansion now falls back to
        the SECONDS builtin, and an instant status-0 corpse still recovers."""
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        release_id = "d7e8f9a0b1c2789012345678901234567890abcd"
        install_root = fixture.base / "session-kit"
        corpse_release = install_root / "releases" / release_id
        (corpse_release / "bin").mkdir(parents=True)
        write_executable(
            corpse_release / "bin" / "shpool_login_launcher",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        bash4_env = fixture.base / "bash4-env.sh"
        bash4_env.write_text("unset EPOCHREALTIME\n", encoding="utf-8")
        current = install_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(REPO)

        def activate() -> None:
            replacement = install_root / ".current-next"
            replacement.symlink_to(corpse_release)
            os.replace(replacement, current)

        try:
            code, output = run_pty(
                fixture,
                env_updates={
                    "SESSION_KIT_ROOT": os.fspath(install_root),
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "0",
                    "BASH_ENV": os.fspath(bash4_env),
                },
                deferred=("Codex alpha", b""),
                deferred_action=activate,
                deferred_delay_seconds=2,
                followup=("Continuing with this picker", b"q\n", 10),
            )
            self.assertEqual(0, code)
            self.assertNotIn("unbound variable", output)
            self.assertIn(
                "The release d7e8f9a0b1c2 picker ended before it could serve.",
                output,
            )
            self.assertEqual(
                1,
                output.count("Reloading the picker into release d7e8f9a0b1c2."),
                "the failed target must never be retried",
            )
            events = (fixture.state / "action-events.jsonl").read_text()
            self.assertIn("release_upgrade_failed", events)
            self.assertIn("quit", events)
        finally:
            fixture.close()

    def test_missing_bash5_clock_still_serves_a_real_picker(self) -> None:
        """The seconds fallback must serve, not only recover.

        If a Bash-4 shell treated every handoff as zero elapsed, a person
        who used the new picker for an hour and quit would get the old
        picker relaunched at them and the target falsely marked failed. The
        SECONDS fallback surrenders one whole second before comparing — the
        rv-rn4-2 rounding credit cannot return — so the launcher here must
        outlive probation by more than a full second to prove serving."""
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        release_id = "e8f9a0b1c2d3789012345678901234567890abcd"
        install_root = fixture.base / "session-kit"
        new_release = install_root / "releases" / release_id
        (new_release / "bin").mkdir(parents=True)
        write_executable(
            new_release / "bin" / "shpool_login_launcher",
            "#!/usr/bin/env bash\n"
            "printf 'NEW RELEASE PICKER RAN\\n'\n"
            "sleep 3\n",
        )
        bash4_env = fixture.base / "bash4-env.sh"
        bash4_env.write_text("unset EPOCHREALTIME\n", encoding="utf-8")
        current = install_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(REPO)

        def activate() -> None:
            replacement = install_root / ".current-next"
            replacement.symlink_to(new_release)
            os.replace(replacement, current)

        try:
            code, output = run_pty(
                fixture,
                env_updates={
                    "SESSION_KIT_ROOT": os.fspath(install_root),
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "0",
                    "SESSION_KIT_PICKER_HANDOFF_PROBATION_SECONDS": "1",
                    "BASH_ENV": os.fspath(bash4_env),
                },
                deferred=("Codex alpha", b""),
                deferred_action=activate,
                deferred_delay_seconds=2,
            )
            self.assertEqual(0, code)
            self.assertNotIn("unbound variable", output)
            self.assertIn("NEW RELEASE PICKER RAN", output)
            self.assertNotIn("ended before it could serve", output)
            self.assertNotIn("Continuing with this picker", output)
            self.assertEqual(
                1,
                output.count("Reloading the picker into release e8f9a0b1c2d3."),
            )
        finally:
            fixture.close()

    def test_tampered_seconds_variable_cannot_credit_a_corpse(self) -> None:
        """The fallback clock is the system's, never mutable shell state.

        Review lanes rv-rn7-1/2 turned SECONDS into a nameref to LINENO
        through BASH_ENV: the source-line difference across the handoff read
        as fifteen elapsed seconds and an instant status-0 corpse was
        announced as served, ending the list with a lone release_upgrade
        event. The fallback now reads builtin printf's %(%s)T strftime
        clock, so no shell variable an adversary can retarget participates
        in the serving decision, and this corpse recovers."""
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        release_id = "f9a0b1c2d3e4789012345678901234567890abcd"
        install_root = fixture.base / "session-kit"
        corpse_release = install_root / "releases" / release_id
        (corpse_release / "bin").mkdir(parents=True)
        write_executable(
            corpse_release / "bin" / "shpool_login_launcher",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        tampered_env = fixture.base / "tampered-env.sh"
        tampered_env.write_text(
            "unset EPOCHREALTIME SECONDS\ndeclare -n SECONDS=LINENO\n",
            encoding="utf-8",
        )
        current = install_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(REPO)

        def activate() -> None:
            replacement = install_root / ".current-next"
            replacement.symlink_to(corpse_release)
            os.replace(replacement, current)

        try:
            code, output = run_pty(
                fixture,
                env_updates={
                    "SESSION_KIT_ROOT": os.fspath(install_root),
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "0",
                    "BASH_ENV": os.fspath(tampered_env),
                },
                deferred=("Codex alpha", b""),
                deferred_action=activate,
                deferred_delay_seconds=2,
                followup=("Continuing with this picker", b"q\n", 10),
            )
            self.assertEqual(0, code)
            self.assertIn(
                "The release f9a0b1c2d3e4 picker ended before it could serve.",
                output,
            )
            self.assertEqual(
                1,
                output.count("Reloading the picker into release f9a0b1c2d3e4."),
                "the failed target must never be retried",
            )
            events = (fixture.state / "action-events.jsonl").read_text()
            self.assertIn("release_upgrade_failed", events)
            self.assertIn("quit", events)
        finally:
            fixture.close()

    def test_corpse_just_under_probation_is_not_rounded_into_service(self) -> None:
        """Integer seconds rounded a 4.2-second corpse up to the probation.

        Review lane rv-rn4-2 demonstrated it: SECONDS granularity credited a
        status-0 exit at 4.201s with the full five-second probation and the
        window lost its list. Elapsed time is measured in milliseconds now;
        a corpse dying at eighty percent of probation recovers, every time."""
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        release_id = "a4b5c6d7e8f9789012345678901234567890abcd"
        install_root = fixture.base / "session-kit"
        corpse_release = install_root / "releases" / release_id
        (corpse_release / "bin").mkdir(parents=True)
        write_executable(
            corpse_release / "bin" / "shpool_login_launcher",
            "#!/usr/bin/env bash\nsleep 1.6\nexit 0\n",
        )
        current = install_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(REPO)

        def activate() -> None:
            replacement = install_root / ".current-next"
            replacement.symlink_to(corpse_release)
            os.replace(replacement, current)

        try:
            code, output = run_pty(
                fixture,
                env_updates={
                    "SESSION_KIT_ROOT": os.fspath(install_root),
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "0",
                    "SESSION_KIT_PICKER_HANDOFF_PROBATION_SECONDS": "2",
                },
                deferred=("Codex alpha", b""),
                deferred_action=activate,
                deferred_delay_seconds=2,
                followup=("Continuing with this picker", b"q\n", 10),
            )
            self.assertEqual(0, code)
            self.assertIn(
                "The release a4b5c6d7e8f9 picker ended before it could serve.",
                output,
            )
            events = (fixture.state / "action-events.jsonl").read_text()
            self.assertIn("release_upgrade_failed", events)
            self.assertIn("quit", events)
        finally:
            fixture.close()

    def test_signal_death_after_probation_recovers_the_picker(self) -> None:
        """Death by signal is never a served picker's own ending.

        A launcher that hangs without serving survives probation; when the
        person kills it, the old design propagated 128+N and the window lost
        its list (review lane rv-rn3-2's non-serving hang). A real picker
        handles its signals — so a signal exit takes the recovery path at
        any age."""
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        release_id = "f3a4b5c6d7e8789012345678901234567890abcd"
        install_root = fixture.base / "session-kit"
        hang_release = install_root / "releases" / release_id
        (hang_release / "bin").mkdir(parents=True)
        write_executable(
            hang_release / "bin" / "shpool_login_launcher",
            "#!/usr/bin/env bash\nsleep 1.5\nkill -TERM $$\n",
        )
        current = install_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(REPO)

        def activate() -> None:
            replacement = install_root / ".current-next"
            replacement.symlink_to(hang_release)
            os.replace(replacement, current)

        try:
            code, output = run_pty(
                fixture,
                env_updates={
                    "SESSION_KIT_ROOT": os.fspath(install_root),
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "0",
                    "SESSION_KIT_PICKER_HANDOFF_PROBATION_SECONDS": "1",
                },
                deferred=("Codex alpha", b""),
                deferred_action=activate,
                deferred_delay_seconds=2,
                followup=("Continuing with this picker", b"q\n", 8),
            )
            self.assertEqual(0, code)
            self.assertIn("exited immediately (status 1", output)
            events = (fixture.state / "action-events.jsonl").read_text()
            self.assertIn("release_upgrade_failed", events)
            self.assertIn("quit", events)
        finally:
            fixture.close()

    def test_a_self_upgrade_ends_its_collector_and_frees_its_temps(self) -> None:
        """`exec` keeps the process ID, so the child would outlive its parent.

        An in-flight collector left running writes a state snapshot into a
        file the exiting picker has just unlinked, and is then inherited,
        unreaped, by the picker that replaced it.
        """
        with tempfile.TemporaryDirectory(prefix=".upgrade-", dir=REPO) as raw:
            base = Path(raw)
            live_file = base / "live.json"
            live_done = base / "live.done"
            live_file.write_text("{}", encoding="utf-8")
            live_done.write_text("", encoding="utf-8")
            pid_file = base / "collector.pid"
            completed = run(
                [
                    "bash",
                    "-c",
                    'source "$LOGIN_LIVE_MODULE"; '
                    "{ sleep 45 & echo $! > \"$COLLECTOR_PID\"; wait; } & "
                    "LIVE_PID=$!; "
                    "while [ ! -s \"$COLLECTOR_PID\" ]; do sleep 0.05; done; "
                    "picker_live_stop; "
                    'printf \'%s|%s\\n\' "${LIVE_PID:-gone}" "$(cat "$COLLECTOR_PID")"',
                ],
                env={
                    "LOGIN_LIVE_MODULE": os.fspath(
                        REPO / "lib" / "sh" / "shpool_login_live.sh"
                    ),
                    "LIVE_FILE": os.fspath(live_file),
                    "LIVE_DONE": os.fspath(live_done),
                    "COLLECTOR_PID": os.fspath(pid_file),
                },
            )
            reported, collector = completed.stdout.strip().split("|")
            self.assertEqual("gone", reported)
            self.assertFalse(live_file.exists())
            self.assertFalse(live_done.exists())
            # The timed status run under the collector is ended too.
            self.assertFalse(Path(f"/proc/{collector}").exists())

    def test_the_view_a_person_set_up_survives_a_self_upgrade(self) -> None:
        completed = run(
            [
                "bash",
                "-c",
                'source "$LOGIN_LIVE_MODULE"; '
                "build_view() { return 0; }; "
                "QUERY=''; PAGE=1; picker_resume_view; "
                'printf \'%s|%s|%s|%s\\n\' "$QUERY" "$PAGE" '
                '"${SESSION_KIT_PICKER_RESUME_QUERY-unset}" '
                '"${SESSION_KIT_PICKER_RESUME_PAGE-unset}"',
            ],
            env={
                "LOGIN_LIVE_MODULE": os.fspath(
                    REPO / "lib" / "sh" / "shpool_login_live.sh"
                ),
                "SESSION_KIT_PICKER_RESUME_QUERY": "deploy",
                "SESSION_KIT_PICKER_RESUME_PAGE": "3",
            },
        )
        self.assertEqual(
            "deploy|3|unset|unset",
            completed.stdout.strip(),
        )

    def test_a_private_picker_key_does_not_change_the_visible_fingerprint(self) -> None:
        """The fingerprint covers what is drawn, and only that.

        Terminal 1 was once reserved for the retired Fleet Supervisor and the
        fingerprint re-ordered rows to match the pinned screen. Nothing pins a
        row now, so the invariant that remains is the narrower one: a private
        `_picker_*` annotation is not visible content and must not repaint.
        """
        first = row("first", number=1, provider="claude")
        second = row("second", number=2, provider="codex")
        before = inventory(first, second)
        annotated_first = dict(first)
        annotated_first["_picker_private_annotation"] = True
        after = inventory(annotated_first, second)
        fixture = LoginFixture(before)
        pinned_path = fixture.base / "annotated.json"
        pinned_path.write_text(json.dumps(after), encoding="utf-8")

        def fingerprint(path: Path) -> str:
            completed = run(
                [
                    "bash",
                    "-c",
                    'source "$LOGIN_LIVE_MODULE"; picker_view_fingerprint',
                ],
                env={
                    "LOGIN_LIVE_MODULE": os.fspath(
                        REPO / "lib" / "sh" / "shpool_login_live.sh"
                    ),
                    "VIEW": os.fspath(path),
                    "PAGE": "1",
                    "QUERY": "",
                },
            )
            return completed.stdout.strip()

        try:
            self.assertEqual(
                fingerprint(fixture.inventory),
                fingerprint(pinned_path),
            )
        finally:
            fixture.close()

    def test_account_alias_changes_fingerprint_but_quota_does_not(self) -> None:
        first = row("account", number=1, provider="codex")
        first["account_alias"] = "main"
        same_account = dict(first)
        same_account["account_quota"] = {"weekly_used": 0.99}
        other_account = dict(first)
        other_account["account_alias"] = "backup"
        fixture = LoginFixture(inventory(first))
        same_path = fixture.base / "same-account.json"
        other_path = fixture.base / "other-account.json"
        same_path.write_text(json.dumps(inventory(same_account)), encoding="utf-8")
        other_path.write_text(json.dumps(inventory(other_account)), encoding="utf-8")

        def fingerprint(path: Path) -> str:
            completed = run(
                [
                    "bash",
                    "-c",
                    'source "$LOGIN_LIVE_MODULE"; picker_view_fingerprint',
                ],
                env={
                    "LOGIN_LIVE_MODULE": os.fspath(
                        REPO / "lib" / "sh" / "shpool_login_live.sh"
                    ),
                    "VIEW": os.fspath(path),
                    "PAGE": "1",
                    "QUERY": "",
                },
            )
            return completed.stdout.strip()

        try:
            initial = fingerprint(fixture.inventory)
            self.assertEqual(initial, fingerprint(same_path))
            self.assertNotEqual(initial, fingerprint(other_path))
        finally:
            fixture.close()

    def test_a_half_typed_command_survives_an_automatic_repaint(self) -> None:
        first = row("alpha", number=1)
        second = row("bravo", number=2)
        fixture = LoginFixture(
            inventory(first), refreshed_document=inventory(first, second)
        )
        try:
            # "/alp" is typed and left unsent. The repaint clears the screen
            # underneath it; the search must still run on the whole word.
            code, output = run_pty(
                fixture,
                b"/alp",
                env_updates={
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "2",
                    "SESSION_KIT_NO_COLOR": None,
                    # This is the repaint guarantee, not the filter preview:
                    # with type-ahead filtering on, "/alp" would hide the
                    # arriving row on purpose and there would be no repaint to
                    # survive. tests/test_picker_ux.py covers the preview.
                    "SESSION_KIT_PICKER_FILTER_LIVE": "0",
                },
                deferred=("Codex bravo", b"ha\nq\n"),
            )
            self.assertEqual(0, code)
            # A repaint clears the screen under the queued characters, but it
            # must never consume them: the finished search still runs on the
            # whole word and hides the row it excludes.
            trailing = output[output.rindex("Codex bravo") :]
            self.assertIn("Codex alpha", trailing)
            self.assertNotIn("Unknown choice", trailing)
        finally:
            fixture.close()

    def test_a_terminal_without_escapes_never_auto_repaints(self) -> None:
        first = row("alpha", number=1)
        second = row("bravo", number=2)
        fixture = LoginFixture(
            inventory(first), refreshed_document=inventory(first, second)
        )
        try:
            # The menu cannot be cleared here, so repainting would stack
            # copies down the screen. It stays static instead.
            code, output = run_pty(
                fixture,
                b"q\n",
                env_updates={
                    "SESSION_KIT_PICKER_REFRESH_SECONDS": "2",
                    "TERM": "dumb",
                },
            )
            self.assertIn("Codex alpha", output)
            self.assertNotIn("Codex bravo", output)
            self.assertEqual(0, code)
        finally:
            fixture.close()

    def test_refresh_off_leaves_the_menu_exactly_as_drawn(self) -> None:
        first = row("alpha", number=1)
        second = row("bravo", number=2)
        fixture = LoginFixture(
            inventory(first), refreshed_document=inventory(first, second)
        )
        try:
            code, output = run_pty(
                fixture,
                b"q\n",
                env_updates={"SESSION_KIT_PICKER_REFRESH_SECONDS": "0"},
            )
            self.assertIn("Codex alpha", output)
            self.assertNotIn("Codex bravo", output)
            self.assertEqual(0, code)
        finally:
            fixture.close()

    def test_guided_new_never_lists_an_ignored_directory(self) -> None:
        fixture = LoginFixture(inventory())
        try:
            fixture.projects.write_text(
                f"main\tclaude\t{fixture.primary_project}\n"
                f"other\tignore\t{fixture.other}\n",
                encoding="utf-8",
            )
            code, output = run_pty(fixture, b"n\n2\n1\n\nq\n")
            self.assertEqual(0, code)
            self.assertNotIn("other:", output)
            self.assertIn(f"   1  main: {fixture.primary_project}", output)
            self.assertEqual(
                [["new", "codex", "main", "--account", "wren"]],
                [entry["args"] for entry in fixture.sp_entries()],
            )
        finally:
            fixture.close()

    def test_recovery_open_and_empty_input_do_not_ack_or_restore(self) -> None:
        pending = {
            "schema_version": 1,
            "entries": [
                {
                    "source_generation_key": "generation",
                    "old_shpool_id": "old1",
                    "display_old_shpool_id": "old1",
                    "provider": "claude",
                    "uuid": "11111111-1111-4111-8111-111111111111",
                    "cwd": "/srv/project",
                    "title": "Recover me",
                    "started_at_unix_ms": int(time.time() * 1000)
                    - (51 * 3600 * 1000),
                }
            ],
        }
        fixture = LoginFixture(inventory(), pending=pending)
        try:
            code, output = run_pty(fixture, b"u\n\nq\n")
            self.assertEqual(0, code)
            self.assertIn("more m", output)
            # The row's own event, in the words `sp recover` uses for it.
            self.assertIn("[Claude] Recover me [lost 2 days ago]", output)
            self.assertNotIn("old1", output)
            self.assertIn(
                "restore numbers · one with no number goes by the selector"
                " beside it · restore all a · ↵ back",
                output,
            )
            self.assertNotIn("Traceback", output)
            self.assertEqual([], fixture.sp_entries())
            self.assertFalse(
                any(
                    entry and entry[0] == "--recovery-pending-ack"
                    for entry in fixture.status_entries()
                )
            )
            self.assertEqual([], fixture.picker_temps())
        finally:
            fixture.close()

    def test_a_selection_the_screen_cannot_read_restores_nothing(self) -> None:
        """A typo is not a command, on the real screen.

        The parser's refusal was caught and then undone: every fragment that
        happened to be a number was added back, so "1,garbage" restored
        session 1 and "1,,2" restored two conversations.
        """
        pending = {
            "schema_version": 1,
            "entries": [
                {
                    "source_generation_key": "generation",
                    "old_shpool_id": f"old-{index}",
                    "provider": "claude",
                    "uuid": f"1111111{index}-1111-4111-8111-111111111111",
                    "cwd": "/srv/project",
                    "title": f"Work {index}",
                }
                for index in (1, 2)
            ],
        }
        for answer in (b"1,garbage", b"1,,2"):
            fixture = LoginFixture(inventory(), pending=pending)
            try:
                code, output = run_pty(fixture, b"u\n" + answer + b"\nq\n")
                self.assertEqual(0, code)
                self.assertIn("There is no such closed session on this screen", output)
                self.assertEqual([], fixture.sp_entries())
                self.assertFalse(
                    any(
                        entry and entry[0] == "--recovery-pending-ack"
                        for entry in fixture.status_entries()
                    )
                )
            finally:
                fixture.close()

    def test_a_row_with_no_number_is_restored_by_the_name_it_shows(self) -> None:
        """The screen printed a dash where the selector belongs.

        The parser quietly accepted a short id the screen never showed, so the
        only way to act on the row was to read the JSON behind it.
        """
        pending = {
            "schema_version": 1,
            "entries": [
                {
                    "source_generation_key": "generation",
                    "old_shpool_id": "old-1",
                    "provider": "claude",
                    "uuid": "22222222-2222-4222-8222-222222222222",
                    "cwd": "/srv/project",
                    "title": "Older work",
                    "numbered": False,
                }
            ],
        }
        fixture = LoginFixture(inventory(), pending=pending)
        try:
            code, output = run_pty(fixture, b"u\nolder work\nq\n", columns=120)
            self.assertEqual(0, code)
            self.assertIn("—  [Claude] Older work", output)
            self.assertIn('Restored closed session "Older work".', output)
            self.assertEqual(
                [
                    [
                        "restore-exact",
                        "claude",
                        "22222222-2222-4222-8222-222222222222",
                        "/srv/project",
                    ]
                ],
                [entry["args"] for entry in fixture.sp_entries()],
            )
            # And no conversation identifier reached the screen to get there.
            self.assertNotIn("22222222", output)
        finally:
            fixture.close()

    def test_recovery_selection_opens_exact_active_managed_uuid_without_duplicate(self) -> None:
        active = row("active-one", number=4107, provider="codex")
        pending = {
            "schema_version": 1,
            "entries": [
                {
                    "source_generation_key": "generation",
                    "old_shpool_id": "old-active",
                    "display_old_shpool_id": "old-active",
                    "provider": "codex",
                    "uuid": active["identity"]["uuid"],
                    "cwd": "/srv/project",
                    "title": "Already running",
                }
            ],
        }
        fixture = LoginFixture(inventory(active), pending=pending)
        try:
            code, output = run_pty(fixture, b"u\nall\nq\n")
            self.assertEqual(0, code)
            self.assertIn(
                "Closed session 1 is already open as session 4107. Opening it.",
                output,
            )
            self.assertIn(
                "Use fork 4107 from the picker to start a separate writable fork.",
                output,
            )
            self.assertIn(
                "Confirmed session 4107 is open; its recovery evidence was retained.",
                output,
            )
            self.assertIn(
                [
                    "--recovery-pending-ack",
                    "generation",
                    "old-active",
                    active["identity"]["uuid"],
                ],
                fixture.status_entries(),
            )
            entries = fixture.sp_entries()
            self.assertEqual(["picker-open"], [entry["args"][0] for entry in entries])
            self.assertFalse(
                any(entry["args"][0] == "restore-exact" for entry in entries)
            )
            self.assertEqual("active-one", entries[0]["proof"]["shpool_id"])
            self.assertNotIn("old-active", output)
        finally:
            fixture.close()

    def test_active_managed_recovery_reports_and_retains_failed_ack(self) -> None:
        active = row("active-one", number=4107, provider="codex")
        pending = {
            "schema_version": 1,
            "entries": [
                {
                    "source_generation_key": "generation",
                    "old_shpool_id": "old-active",
                    "display_old_shpool_id": "old-active",
                    "provider": "codex",
                    "uuid": active["identity"]["uuid"],
                    "cwd": "/srv/project",
                    "title": "Already running",
                }
            ],
        }
        fixture = LoginFixture(inventory(active), pending=pending, ack_exit=1)
        try:
            code, output = run_pty(fixture, b"u\na\nq\n")
            self.assertEqual(0, code)
            self.assertIn(
                "Session 4107 is open, but its live proof was incomplete. Its recovery evidence was retained.",
                output,
            )
            self.assertIn(
                [
                    "--recovery-pending-ack",
                    "generation",
                    "old-active",
                    active["identity"]["uuid"],
                ],
                fixture.status_entries(),
            )
            self.assertEqual(
                ["picker-open"],
                [entry["args"][0] for entry in fixture.sp_entries()],
            )
        finally:
            fixture.close()

    def test_recovery_selection_refuses_duplicate_active_outside_manager(self) -> None:
        exact_uuid = "22222222-2222-4222-8222-222222222222"
        document = inventory()
        document["outside_agents"] = [
            {
                "provider": "claude",
                "identity": {
                    "uuid": exact_uuid,
                    "pid": 9001,
                    "process_start_ticks": 90010,
                    "confidence": "exact",
                },
                "title": "Outside Claude",
                "cwd": "/srv/outside",
                "agent_status": "working",
                "subagents": [],
            }
        ]
        pending = {
            "schema_version": 1,
            "entries": [
                {
                    "source_generation_key": "generation",
                    "old_shpool_id": "outside-old",
                    "display_old_shpool_id": "outside-old",
                    "provider": "claude",
                    "uuid": exact_uuid,
                    "cwd": "/srv/outside",
                    "title": "Outside Claude",
                }
            ],
        }
        fixture = LoginFixture(document, pending=pending)
        try:
            code, output = run_pty(fixture, b"u\na\nq\n")
            self.assertEqual(0, code)
            self.assertIn(
                "Closed session 1 is already open outside the kit. Nothing changed.",
                output,
            )
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_recovery_duplicate_check_is_scoped_to_exact_provider_and_uuid(self) -> None:
        active = row("codex-active", number=22, provider="codex")
        pending = {
            "schema_version": 1,
            "entries": [
                {
                    "source_generation_key": "generation",
                    "old_shpool_id": "claude-old",
                    "display_old_shpool_id": "claude-old",
                    "provider": "claude",
                    "uuid": active["identity"]["uuid"],
                    "cwd": "/srv/project",
                    "title": "Same UUID, other provider",
                }
            ],
        }
        fixture = LoginFixture(inventory(active), pending=pending)
        try:
            code, output = run_pty(fixture, b"u\na\nq\n")
            self.assertEqual(0, code)
            self.assertIn("Restored closed session 1.", output)
            self.assertNotIn("claude-old", output)
            entries = fixture.sp_entries()
            self.assertEqual(
                [["restore-exact", "claude", active["identity"]["uuid"], "/srv/project"]],
                [entry["args"] for entry in entries],
            )
        finally:
            fixture.close()

    def test_recovery_comma_selection_restores_each_selected_item_only(self) -> None:
        entries = []
        for number in range(1, 4):
            entries.append(
                {
                    "source_generation_key": "generation",
                    "old_shpool_id": f"old-{number}",
                    "provider": "codex",
                    "uuid": f"00000000-0000-4000-8000-{number:012d}",
                    "cwd": "/srv/project",
                    "title": f"Recovery {number}",
                }
            )
        fixture = LoginFixture(
            inventory(), pending={"schema_version": 1, "entries": entries}
        )
        try:
            code, output = run_pty(fixture, b"u\n1,3\nq\n")
            self.assertEqual(0, code)
            restores = [
                entry["args"]
                for entry in fixture.sp_entries()
                if entry["args"][0] == "restore-exact"
            ]
            self.assertEqual(
                [
                    ["restore-exact", "codex", entries[0]["uuid"], "/srv/project"],
                    ["restore-exact", "codex", entries[2]["uuid"], "/srv/project"],
                ],
                restores,
            )
            self.assertIn("Restored closed session 1.", output)
            self.assertIn("Restored closed session 3.", output)
            self.assertNotIn("Restored closed session 2.", output)
            self.assertNotIn("old-1", output)
            self.assertNotIn("old-3", output)
        finally:
            fixture.close()

    def test_incomplete_recovery_record_is_retained_without_nounset_exit(self) -> None:
        pending = {
            "schema_version": 1,
            "entries": [
                {
                    "source_generation_key": "generation",
                    "old_shpool_id": "old1",
                    "display_old_shpool_id": "old1",
                    "provider": "codex",
                    "uuid": "11111111-1111-4111-8111-111111111111",
                    "cwd": "",
                    "title": "Missing cwd",
                }
            ],
        }
        fixture = LoginFixture(inventory(), pending=pending)
        try:
            code, output = run_pty(fixture, b"u\na\nq\n")
            self.assertEqual(0, code)
            self.assertIn("Closed session 1 is incomplete. Nothing changed.", output)
            self.assertEqual([], fixture.sp_entries())
            self.assertFalse(
                any(
                    entry and entry[0] == "--recovery-pending-ack"
                    for entry in fixture.status_entries()
                )
            )
            self.assertEqual([], fixture.picker_temps())
        finally:
            fixture.close()

    def test_conflicting_duplicate_recovery_is_retained_for_manual_review(self) -> None:
        pending = {
            "schema_version": 1,
            "entries": [
                {
                    "source_generation_key": "generation",
                    "old_shpool_id": "old1",
                    "display_old_shpool_id": "old1",
                    "provider": "codex",
                    "uuid": "11111111-1111-4111-8111-111111111111",
                    "cwd": "/srv/project",
                    "title": "Conflicting evidence",
                    "actionable": False,
                    "conflict_fields": ["cwd", "command"],
                }
            ],
        }
        fixture = LoginFixture(inventory(), pending=pending)
        try:
            code, output = run_pty(fixture, b"u\na\nq\n")
            self.assertEqual(0, code)
            self.assertIn(
                "review required: conflicting cwd, command", output
            )
            # The sentence the list carries for this row, and the same one
            # again when they act on it -- with the fields that disagree named.
            self.assertIn(
                "its launch records disagree with each other, so it needs a"
                " look by hand",
                output,
            )
            self.assertIn(
                "Closed session 1: its launch records disagree with each"
                " other, so it needs a look by hand (cwd, command)."
                " Nothing changed.",
                output,
            )
            self.assertNotIn("a plain shell", output)
            self.assertEqual([], fixture.sp_entries())
            self.assertFalse(
                any(
                    entry and entry[0] == "--recovery-pending-ack"
                    for entry in fixture.status_entries()
                )
            )
            self.assertEqual([], fixture.picker_temps())
        finally:
            fixture.close()

    def test_closed_session_row_uses_its_recorded_close_age(self) -> None:
        pending = {
            "schema_version": 1,
            "entries": [
                {
                    "source_generation_key": "generation",
                    "old_shpool_id": "old1",
                    "provider": "codex",
                    "uuid": "11111111-1111-4111-8111-111111111111",
                    "cwd": "/srv/project",
                    "title": "Closed two hours",
                    "closed_at_unix_ms": int(time.time() * 1000) - 2 * 3600 * 1000,
                }
            ],
        }
        fixture = LoginFixture(inventory(), pending=pending)
        try:
            code, output = run_pty(fixture, b"u\n\nq\n", columns=120)
            self.assertEqual(0, code)
            self.assertIn("Closed two hours [closed 2 hr ago]", output)
        finally:
            fixture.close()

    def test_prompt_waiting_cache_survives_global_cleanup_under_nounset(self) -> None:
        fixture = LoginFixture(inventory())
        try:
            xdg_state = fixture.base / "xdg-state"
            cache = xdg_state / "session-kit" / "waiting-count"
            cache.parent.mkdir(parents=True)
            cache.write_text("2\n", encoding="utf-8")
            status = fixture.home / ".local" / "bin" / "shpool_status"
            status.parent.mkdir(parents=True)
            write_executable(
                status,
                "#!/usr/bin/env bash\nprintf 'unexpected status refresh\\n' >&2\nexit 91\n",
            )
            environment = {
                "HOME": str(fixture.home),
                "XDG_STATE_HOME": str(xdg_state),
                "SHPOOL_SESSION_NAME": "main",
                "SHPOOL_JOURNAL": "disabled",
                "SESSION_KIT_STATE_DIR": str(fixture.state),
                "PS1": "fixture$ ",
                "TERM": "dumb",
            }
            checked = run(
                [
                    "bash",
                    "--noprofile",
                    "--norc",
                    "-u",
                    "-i",
                    "-c",
                    (
                        'source "$1"; '
                        '[[ -z ${__sk_state_root+x} ]]; '
                        "__sk_waiting"
                    ),
                    "prompt-state-test",
                    REPO / "bashrc/shpool.bashrc",
                ],
                env=environment,
            )
            self.assertIn("●2", checked.stdout)
            self.assertNotIn("unexpected status refresh", checked.stderr)
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
