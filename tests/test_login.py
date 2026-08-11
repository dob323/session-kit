from __future__ import annotations

import errno
from datetime import datetime
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import tempfile
import time
import unicodedata
import unittest

from tests.support import REPO, run


LOGIN = REPO / "bin" / "shpool_login"
SGR = re.compile(r"\x1b\[([0-9;]*)m")
# Tab-title sequences render zero cells in a real terminal.
OSC_TITLE = re.compile(r"\x1b\][0-9;]*[^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_sgr(text: str) -> str:
    return OSC_TITLE.sub("", SGR.sub("", text))


def display_cells(text: str) -> int:
    text = strip_sgr(text)
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


def write_session_event(
    fixture: "LoginFixture",
    item: dict,
    event: str,
    timestamp_ms: int,
) -> None:
    event_root = fixture.state / "events"
    event_root.mkdir(exist_ok=True)
    identity = item.get("identity") or {}
    key = f"{item.get('provider')}:{identity.get('uuid')}"
    path = event_root / f"{key}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event": event,
                    "question": None,
                    "source": "hook" if item.get("provider") == "claude" else "synth",
                    "ts_unix_ms": timestamp_ms,
                },
                separators=(",", ":"),
            )
            + "\n"
        )


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
            json.dumps(pending or {"schema_version": 1, "entries": []}),
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
    print(json.dumps({
        "provider":provider,
        "recommendation":None,
        "choices":[
            {
                "alias":"duck",
                "email":"account@example.com",
                "plan":"subscription",
                "eligible":True,
                "health":"ok",
                "recommended":False,
            },
            {
                "alias":"work",
                "email":"work@example.com",
                "plan":"subscription",
                "eligible":os.environ.get("LOGIN_ACCOUNT_SECOND_ELIGIBLE", "1") != "0",
                "health":"ok" if os.environ.get("LOGIN_ACCOUNT_SECOND_ELIGIBLE", "1") != "0" else "error",
                "recommended":False,
            },
        ],
    }))
    raise SystemExit(0)
entry={"args":args}
if args and args[0].startswith("picker-"):
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
    post_signal_bytes: bytes = b"",
    env_updates: dict[str, str | None] | None = None,
    deferred: tuple[str, bytes] | None = None,
    deferred_delay_seconds: float = 0,
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

    pid, descriptor = pty.fork()
    if pid == 0:
        os.chdir(fixture.base)
        if os.environ.get("SESSION_KIT_TEST_EXEC_ROOT"):
            os.execve(
                "/usr/bin/bash",
                ["/usr/bin/bash", os.fspath(LOGIN)],
                environment,
            )
        os.execve(LOGIN, [os.fspath(LOGIN)], environment)

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
            if deferred_delay_seconds > 0:
                time.sleep(deferred_delay_seconds)
            os.write(descriptor, payload)
        if send_signal is not None:
            prompt = "❯ ".encode()
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
                    "picker PTY did not render its prompt before the signal"
                )
            os.kill(pid, send_signal)
            if post_signal_bytes:
                time.sleep(0.3)
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
    def test_enter_and_eof_return_regular_shell_without_action(self) -> None:
        for label, payload in (
            ("enter", b"\n"),
            ("eof", b"\x04"),
        ):
            with self.subTest(label=label):
                fixture = LoginFixture(inventory(row("ready", number=9)))
                try:
                    code, _ = run_pty(fixture, payload)
                    self.assertEqual(2, code)
                    self.assertEqual([], fixture.sp_entries())
                    self.assertEqual([], fixture.picker_temps())
                finally:
                    fixture.close()

    def test_interrupt_redraws_menu_instead_of_exiting(self) -> None:
        # A stray Ctrl-C must never dump the human to a bare terminal: the
        # picker redraws its menu and only a deliberate Enter/quit ends it.
        fixture = LoginFixture(inventory(row("ready", number=9)))
        try:
            code, output = run_pty(
                fixture,
                b"",
                send_signal=signal.SIGINT,
                post_signal_bytes=b"\n",
            )
            self.assertEqual(2, code)
            # The exit reason proves the interrupt was absorbed: the picker
            # ended through the deliberate Enter that followed, not the signal.
            log = fixture.state / "action-events.jsonl"
            events = log.read_text() if log.exists() else ""
            self.assertIn("terminal_requested", events)
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
            code, output = run_pty(fixture, b"/codex10\n\n")
            self.assertEqual(2, code)
            self.assertIn(
                "4 sessions · 3 ready here · 1 open elsewhere", output
            )
            self.assertIn("Ready to open", output)
            self.assertIn("Open elsewhere", output)
            self.assertIn("status unavailable", output)
            self.assertIn("| CDX | unknown | status unavailable", output)
            self.assertNotIn("state unavailable", output)
            self.assertLess(output.index("claude1"), output.index("claude2"))
            self.assertIn("1 match of 4 sessions", output)
            self.assertRegex(
                output,
                r"(?m)^\s+10\s+Searchable task\s+\| CDX \| unknown \| working\s+\| opened Nov 14, 2023$",
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
            code, output = run_pty(fixture, b"r\n7401\n\n\n")
            self.assertEqual(2, code)
            self.assertRegex(output, r"(?m)^\s+7401\s+Unknown transition")
            self.assertRegex(output, r"(?m)^\s+7401\s+Codex transition")
            self.assertIn("Already open in another SSH window", output)
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
                f"{huge}\nnext\n{huge}\n\n".encode(),
                lines=24,
                columns=60,
            )
            self.assertEqual(2, code)
            self.assertIn(
                "Choose a number shown here. Nothing changed.",
                output,
            )
            self.assertRegex(output, rf"(?m)^\s+{huge}\s+Codex s…")
            entries = fixture.sp_entries()
            self.assertEqual(["picker-open"], [entry["args"][0] for entry in entries])
            self.assertEqual("s24", entries[0]["proof"]["shpool_id"])
            self.assertLessEqual(
                max(display_cells(line) for line in output.splitlines()[2:]),
                59,
            )
        finally:
            fixture.close()

    def test_missing_terminal_number_is_not_renumbered_or_actionable(self) -> None:
        unsafe = row("no-number", number=1)
        unsafe["terminal_number"] = None
        fixture = LoginFixture(inventory(unsafe))
        try:
            code, output = run_pty(fixture, b"1\nm\n\n\n")
            self.assertEqual(2, code)
            self.assertIn(
                "0 sessions · 0 ready here · 0 open elsewhere", output
            )
            self.assertIn("More m", output)
            self.assertIn(
                "Unavailable: 1 session record without a live shell (no actions",
                output,
            )
            self.assertIn("Choose a number shown here. Nothing changed.", output)
            self.assertNotIn("[no-number]", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_mixed_unavailable_row_does_not_block_exact_kill_proof(self) -> None:
        exact = row("exact", number=9, availability="attached")
        unavailable = row("no-shell", number=2)
        unavailable["terminal_number"] = None
        fixture = LoginFixture(inventory(exact, unavailable))
        try:
            code, output = run_pty(fixture, b"k 2\nk 9\nm\n\n\n")
            self.assertEqual(2, code)
            self.assertIn(
                "Unavailable: 1 session record without a live shell (no actions",
                output,
            )
            self.assertIn("2 is not shown here. Nothing changed.", output)
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
            code, output = run_pty(fixture, b"17\nnext\n17\n\n", lines=24)
            self.assertEqual(2, code)
            self.assertIn("Choose a number shown here", output)
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
                            fixture, b"\n", lines=lines, columns=columns
                        )
                        self.assertEqual(2, code)
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
                b"next\nnext\nprev\n/d3\n\n",
                lines=24,
                columns=80,
            )
            self.assertEqual(2, code)
            self.assertIn("Page 1/2 | next", output)
            self.assertIn("Page 2/2 | prev", output)
            self.assertIn("1 match of 31 sessions", output)
            self.assertNotIn("Page 1/1", output)
            self.assertRegex(
                output,
                r"(?m)^\s+11\s+Codex d3\s+\| CDX \| unknown \| working\s+\| opened Nov 14, 2023$",
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
            code, output = run_pty(fixture, b"\n", lines=30, columns=120)
            self.assertEqual(2, code)
            self.assertEqual(2, output.count("needs your reply"))
            self.assertLess(output.index("reply-codex"), output.index("ready-claude"))
            self.assertLess(
                output.index("reply-open-codex"), output.index("open-claude")
            )
            self.assertIn("optional", output)
            self.assertIn("reply optional", output)
            self.assertNotIn("! reply optional", output)
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
            code, output = run_pty(fixture, b"\n", columns=120)
            self.assertEqual(2, code)
            self.assertLess(output.index("blocking"), output.index("pending"))
            self.assertLess(output.index("blocking"), output.index("failed"))
            self.assertRegex(output, r"working\s+\| opened .* \| name pending")
            self.assertRegex(output, r"working\s+\| opened .* \| name failed")
            self.assertEqual(1, output.count("needs your reply"))
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
            code, output = run_pty(fixture, b"\n", columns=120)
            self.assertEqual(2, code)
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
            code, output = run_pty(fixture, b"\n", columns=120)
            self.assertEqual(2, code)
            self.assertNotIn("title pending", output)
            self.assertIn("working", output)
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
            code, output = run_pty(fixture, b"6\n5\n\n", columns=120)
            self.assertEqual(2, code)
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
        waiting["account_alias"] = "duck"
        waiting["account_switch_capable"] = True
        fixture = LoginFixture(inventory(waiting))
        try:
            code, output = run_pty(
                fixture, b"6\n4\n2\nswitch\n\n\n", columns=120
            )
            self.assertEqual(2, code)
            self.assertIn("Change subscription account", output)
            self.assertIn("Type switch to confirm", output)
            entries = fixture.sp_entries()
            self.assertEqual(1, len(entries))
            self.assertEqual("picker-account-switch", entries[0]["args"][0])
            self.assertEqual("work", entries[0]["args"][2])
            self.assertEqual("account-change", entries[0]["proof"]["shpool_id"])
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
        waiting["account_alias"] = "duck"
        waiting["account_switch_capable"] = True
        fixture = LoginFixture(inventory(waiting))
        try:
            code, output = run_pty(
                fixture,
                b"6\n4\n2\n\n",
                columns=120,
                env_updates={"LOGIN_ACCOUNT_SECOND_ELIGIBLE": "0"},
            )
            self.assertEqual(2, code)
            self.assertIn("Choose a healthy account number", output)
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
            code, output = run_pty(fixture, b"\n", columns=60)
            self.assertEqual(2, code)
            self.assertIn("needs your reply", output)
            self.assertIn("needs your reply", output)
        finally:
            fixture.close()

    def test_colored_picker_uses_only_the_approved_sgr_allowlist(self) -> None:
        unsafe = row("unsafe", number=7, provider="codex", needs_you=True)
        unsafe["display_title"] = "Unsafe\x1b[31m title\x1b]0;bad\u0007"
        fixture = LoginFixture(inventory(unsafe, stale=True))
        try:
            code, output = run_pty(
                fixture,
                b"\n",
                columns=100,
                env_updates={
                    "NO_COLOR": None,
                    "SESSION_KIT_NO_COLOR": None,
                    "TERM": "xterm-ghostty",
                },
            )
            self.assertEqual(2, code)
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
            plain = plain.replace("\x1b[2J", "").replace("\x1b[H", "")
            self.assertNotIn("\x1b", plain)
            self.assertIn("Unsafe [31m title ]0;bad", plain)
            self.assertIn("needs your reply", plain)
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
                b"\n",
                columns=120,
                env_updates={
                    "NO_COLOR": None,
                    "SESSION_KIT_NO_COLOR": None,
                    "TERM": "xterm-ghostty",
                },
            )
            self.assertEqual(2, code)
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
                        fixture, b"\n", env_updates=updates
                    )
                    self.assertEqual(2, code)
                    if updates.get("TERM") in {"dumb", None}:
                        self.assertNotIn("\x1b", output)
                    else:
                        plain = output.replace("\x1b[2J", "").replace("\x1b[H", "")
                        self.assertNotIn("\x1b", plain)
                    self.assertIn("needs your reply", output)
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
            code, output = run_pty(fixture, b"\n", columns=180)
            self.assertEqual(2, code)
            self.assertNotIn("recent output", output)
            self.assertNotIn("last output now", output)
            self.assertNotIn("last output", output)
            self.assertIn("| CDX | unknown | working", output)
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
            code, output = run_pty(fixture, b"\n", columns=60)
            self.assertEqual(2, code)
            self.assertIn("| working", output)
            self.assertNotIn("last output 30m", output)
            self.assertNotIn("20 subagents", output)
        finally:
            fixture.close()

    def test_main_rows_show_state_aware_response_waiting_and_opened_ages(self) -> None:
        now_ms = int(time.time() * 1000)
        ready = row("recent-ready", number=2, provider="claude")
        ready["agent_status"] = "idle"
        ready["account_alias"] = "main"
        attached = row(
            "working-elsewhere",
            number=8,
            provider="codex",
            availability="attached",
        )
        attached["agent_status"] = "working"
        attached["account_alias"] = "backup"
        waiting = row(
            "waiting-ready",
            number=9,
            provider="claude",
            needs_you=True,
        )
        waiting["account_alias"] = "spare"
        shell = row("plain-shell", number=10, provider="shell")
        shell["agent_status"] = "idle"
        shell["started_at_unix_ms"] = now_ms - 2 * 3600 * 1000
        fixture = LoginFixture(inventory(ready, attached, waiting, shell))
        write_session_event(
            fixture, ready, "turn_done", now_ms - 55 * 60 * 1000
        )
        write_session_event(
            fixture, attached, "turn_done", now_ms - 3 * 3600 * 1000
        )
        write_session_event(
            fixture, waiting, "needs_input", now_ms - 12 * 3600 * 1000
        )
        try:
            code, output = run_pty(fixture, b"\n", columns=200)
            self.assertEqual(2, code)
            self.assertIn("Ready to open", output)
            self.assertIn("Open elsewhere", output)
            self.assertRegex(output, r"idle\s+\| 55 min ago")
            self.assertRegex(output, r"working\s+\| 3 hr ago")
            self.assertIn("needs your reply | waiting 12 hr", output)
            self.assertRegex(output, r"idle\s+\| opened 2 hr ago")
            self.assertIn("| CLD | main   | idle", output)
            self.assertIn("| CDX | backup | working", output)
            self.assertIn("| CLD | spare  | needs your reply", output)
            self.assertRegex(output, r"\| SHL \|\s{8}\| idle")
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
            self.assertEqual(
                1,
                len(
                    {
                        lines["recent-ready"].index("idle"),
                        lines["working-elsewhere"].index(
                            "working", lines["working-elsewhere"].index("CDX")
                        ),
                        lines["waiting-ready"].index("needs your reply"),
                        lines["plain-shell"].index("idle"),
                    }
                ),
            )
            self.assertEqual(
                1,
                len(
                    {
                        lines["recent-ready"].index("55 min ago"),
                        lines["working-elsewhere"].index("3 hr ago"),
                        lines["waiting-ready"].index("waiting 12 hr"),
                        lines["plain-shell"].index("opened 2 hr ago"),
                    }
                ),
            )
        finally:
            fixture.close()

    def test_narrow_main_row_drops_age_before_the_primary_state(self) -> None:
        now_ms = int(time.time() * 1000)
        item = row("narrow-age", number=8, provider="codex")
        item["display_title"] = "A useful descriptive title that should keep the available room"
        fixture = LoginFixture(inventory(item))
        write_session_event(
            fixture, item, "turn_done", now_ms - 30 * 60 * 1000
        )
        try:
            code, output = run_pty(fixture, b"\n", columns=60)
            self.assertEqual(2, code)
            self.assertIn("A useful descript", output)
            self.assertIn("| working", output)
            self.assertNotIn("30 min ago", output)
        finally:
            fixture.close()

    def test_main_response_age_uses_readable_unit_and_date_boundaries(self) -> None:
        now_ms = int(time.time() * 1000)
        offsets = (5, 2 * 60, 2 * 3600, 2 * 86400, 31 * 86400)
        items = [
            row(f"age-{index}", number=index, provider="claude")
            for index in range(1, 6)
        ]
        for item in items:
            item["agent_status"] = "idle"
        fixture = LoginFixture(inventory(*items))
        for item, seconds in zip(items, offsets, strict=True):
            write_session_event(
                fixture, item, "turn_done", now_ms - seconds * 1000
            )
        old_local = datetime.fromtimestamp(
            (now_ms - offsets[-1] * 1000) / 1000
        ).astimezone()
        old_date = f"{old_local.strftime('%b')} {old_local.day}"
        try:
            code, output = run_pty(fixture, b"\n", columns=220)
            self.assertEqual(2, code)
            self.assertIn("| just now", output)
            self.assertIn("| 2 min ago", output)
            self.assertIn("| 2 hr ago", output)
            self.assertIn("| 2 days ago", output)
            self.assertIn(f"| {old_date}", output)
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
                fixture, b"\n", lines=24, columns=60
            )
            self.assertEqual(2, code)
            plain = output.replace("\x1b[2J", "").replace("\x1b[H", "")
            self.assertNotIn("\x1b", plain)
            self.assertIn("Open number · New n · Needs a (0) · More m · Help ?", output)
            self.assertFalse(
                any(
                    unicodedata.category(character).startswith("C")
                    for character in plain.replace("\n", "").replace("\r", "")
                )
            )
            self.assertIn("| UNK |", output)
            self.assertIn("Warning: showing cached inventory", output)
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
                b"9\n2\nx 9\nname 9\nBetter name\n\n",
            )
            self.assertEqual(2, code)
            self.assertIn("Already open in another SSH window", output)
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

    def test_kill_shortcut_and_close_alias_are_proof_bound(self) -> None:
        for command in ("k 9", "K 9", "x 9", "X 9"):
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
                    # Two Enters after a kill: the first is forgiven once (a
                    # late confirm Enter must not silently end the picker),
                    # the second deliberately exits.
                    code, output = run_pty(
                        fixture,
                        f"{command}\n\n\n".encode(),
                    )
                    self.assertEqual(2, code)
                    self.assertNotIn("Unknown choice", output)
                    self.assertIn("Enter again for a regular terminal", output)
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
            code, output = run_pty(fixture, b"k 4, 6\n\n\n")
            self.assertEqual(2, code)
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
            code, output = run_pty(fixture, b"k 4-9\nk 4-5\n\n\n")
            self.assertEqual(2, code)
            self.assertIn("not shown here", output)
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
            code, output = run_pty(fixture, b"k all\n\n\n")
            self.assertEqual(2, code)
            self.assertEqual(
                {"open1", "open2", "open3"},
                {entry["proof"]["shpool_id"] for entry in fixture.sp_entries()},
            )
            self.assertNotIn("Use k with visible numbers", output)
        finally:
            fixture.close()

    def test_kill_shortcut_refuses_invalid_or_unshown_numbers(self) -> None:
        fixture = LoginFixture(inventory(row("open1", number=9)))
        try:
            code, output = run_pty(fixture, b"k nope\nk 99\n\n")
            self.assertEqual(2, code)
            self.assertIn(
                "Use k with visible numbers (k 5, 6, 8). Nothing changed.",
                output,
            )
            self.assertIn("99 is not shown here. Nothing changed.", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_help_presents_kill_shortcut_and_close_alias(self) -> None:
        fixture = LoginFixture(inventory())
        try:
            code, output = run_pty(fixture, b"?\n\n\n")
            self.assertEqual(2, code)
            self.assertIn(
                "k <numbers>   Close displayed sessions: k 5 · k 5, 6, 8 · k 4-7",
                output,
            )
            self.assertIn("x <number>    Compatibility alias for k", output)
            self.assertIn("action 4      Change the subscription account", output)
            self.assertIn("action 5      Apply a pending Codex bar title", output)
        finally:
            fixture.close()

    def test_name_reset_and_fork_use_exact_proof_bound_terminal_number(self) -> None:
        exact = row("exact-ai", number=88005553535, provider="claude")
        exact["row"] = 1
        fixture = LoginFixture(inventory(exact))
        try:
            code, output = run_pty(
                fixture,
                b"name reset 88005553535\nfork 88005553535\n\n",
                columns=80,
            )
            self.assertEqual(2, code)
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
                b"/plain\nfork 91\nname reset 91\nfork 700\n\n",
            )
            self.assertEqual(2, code)
            self.assertIn(
                "Only exact Claude and Codex conversations can be forked.",
                output,
            )
            self.assertIn(
                "Only exact Claude and Codex conversations can reset a custom name.",
                output,
            )
            self.assertIn(
                "Choose a number shown here. Nothing changed.",
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
            code, output = run_pty(fixture, b"\n", columns=200)
            self.assertEqual(2, code)
            self.assertIn("quiet 7h 49m", output)
            self.assertNotIn("| running", output)
        finally:
            fixture.close()

    def test_briefly_quiet_running_session_is_still_running(self) -> None:
        """A slow tool call or a long model turn must never be flagged."""
        busy = row("ready1", number=1, recent_output_at_unix_ms=1)
        busy["agent_status"] = "running"
        busy["recent_output_age_seconds"] = 1800  # 30m
        fixture = LoginFixture(inventory(busy))
        try:
            code, output = run_pty(fixture, b"\n", columns=200)
            self.assertEqual(2, code)
            self.assertIn("running", output)
            self.assertNotIn("quiet 7h", output)
        finally:
            fixture.close()

    def test_narrow_terminal_drops_subagents_before_staleness(self) -> None:
        """How long a session has been quiet is never the field that is cut.

        At a crowded width the picker used to drop "recent output 7h 52m ago"
        and keep "20 subagents", hiding the only evidence that a session was
        dead.
        """
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
            code, output = run_pty(fixture, b"\n", columns=80)
            self.assertEqual(2, code)
            self.assertIn("quiet 7h 49m", output)
            self.assertNotIn("20 subagents", output)
        finally:
            fixture.close()

    def test_refused_action_refreshes_instead_of_exiting_picker(self) -> None:
        fixture = LoginFixture(inventory(row("ready1", number=1)))
        try:
            code, output = run_pty(fixture, b"1\n\n", sp_exit=74)
            self.assertEqual(2, code)
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
            code, output = run_pty(fixture, b"1\n\n", sp_exit=75)
            self.assertEqual(2, code)
            self.assertIn("could not connect", output)
            self.assertIn("does not prove the session is dead", output)
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
            code, output = run_pty(fixture, b"4\n\n", sp_exit=7)
            self.assertEqual(2, code)
            self.assertIn("failed without a verified cause", output)
            self.assertNotIn("terminal died", output)
            commands = [entry["args"][0] for entry in fixture.sp_entries()]
            self.assertNotIn("picker-recover", commands)
        finally:
            fixture.close()

    def test_cached_row_selection_is_read_only_before_proof_or_sp(self) -> None:
        fixture = LoginFixture(inventory(row("ready1", number=1), stale=True))
        try:
            code, output = run_pty(fixture, b"1\nk 1\n\n")
            self.assertEqual(2, code)
            self.assertIn("Cached rows are read-only", output)
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
            code, output = run_pty(fixture, b"\n", columns=120)
            self.assertEqual(2, code)
            self.assertIn("Picker safety work", output)
            self.assertNotIn("private-session-id", output)
        finally:
            fixture.close()

        fixture = LoginFixture(inventory(hidden))
        try:
            code, output = run_pty(
                fixture, b"/private-session-id\n\n", columns=120
            )
            self.assertEqual(2, code)
            self.assertIn("1 match of 1 session", output)
            self.assertIn("Picker safety work", output)
        finally:
            fixture.close()

    def test_prompt_quarantine_is_a_selectable_needs_you_row_without_ids(self) -> None:
        existing = row("private-existing-id", number=8)
        existing["title"] = "User chosen session title"
        existing["display_title"] = "User chosen session title"
        fixture = LoginFixture(inventory(existing))
        secret_key = "f9e8d7c6b5a4"
        fixture.prompt_quarantine.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "age_seconds": 185,
                            "key": secret_key,
                            "state": "intake_pending",
                            "title": "Codex prompt intake pending",
                        }
                    ],
                    "schema_version": 1,
                }
            ),
            encoding="utf-8",
        )
        try:
            code, output = run_pty(fixture, b"a\nq1\n1\n\n\n", columns=140)
            self.assertEqual(2, code)
            self.assertIn("Prompt delivery", output)
            self.assertIn("q1", output)
            self.assertIn("Codex prompt intake pending", output)
            self.assertIn("3m old", output)
            self.assertIn("User chosen session title", output)
            self.assertIn("ingested without sending it to Codex again", output)
            self.assertNotIn(secret_key, output)
            self.assertNotIn("f9e8d7c6-b5a4", output)
            actions = [entry["args"] for entry in fixture.sp_entries()]
            self.assertIn(["prompt-quarantine", "ingest", secret_key], actions)
        finally:
            fixture.close()

    def test_prompt_resume_and_prune_capture_machine_output(self) -> None:
        fixture = LoginFixture(inventory())
        secret_key = "f9e8d7c6b5a4"
        fixture.prompt_quarantine.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "age_seconds": 1,
                            "key": secret_key,
                            "state": "intake_pending",
                            "title": "Codex prompt intake pending",
                        }
                    ],
                    "schema_version": 1,
                }
            ),
            encoding="utf-8",
        )
        try:
            code, output = run_pty(fixture, b"a\nq1\n2\n\n\nqp\n\n", columns=140)
            self.assertEqual(2, code)
            self.assertIn("exact Codex conversation was resumed", output)
            self.assertIn("Expired prompt recovery records were pruned", output)
            self.assertNotIn(secret_key, output)
            self.assertNotIn("s20260809-232323-9", output)
            self.assertNotIn("f9e8d7c6-b5a4", output)
            actions = [entry["args"][:2] for entry in fixture.sp_entries()]
            self.assertIn(["prompt-quarantine", "resume"], actions)
            self.assertIn(["prompt-quarantine", "prune"], actions)
        finally:
            fixture.close()

    def test_available_unknown_provider_delegates_exact_proof_open(self) -> None:
        unknown = row("unknown1", number=4, provider="unknown")
        unknown["identity"]["uuid"] = None
        unknown["display_provider"] = "codex"
        unknown["display_title"] = "Codex setup incomplete"
        unknown["setup_incomplete"] = True
        fixture = LoginFixture(inventory(unknown))
        try:
            code, output = run_pty(fixture, b"4\n\n")
            self.assertEqual(2, code)
            self.assertIn("Codex setup incomplete", output)
            self.assertIn("| CDX | unknown | setup incomplete", output)
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
            code, output = run_pty(fixture, b"o\n\n1\n\n")
            self.assertEqual(2, code)
            self.assertIn(
                "0 sessions · 0 ready here · 0 open elsewhere", output
            )
            self.assertIn("More m", output)
            self.assertIn("Other provider sessions", output)
            self.assertIn(
                "Detected live provider roots outside the session manager; they are not attachable here.",
                output,
            )
            self.assertIn("Outside account project-alpha", output)
            self.assertIn("project: project-alpha | not attachable here", output)
            self.assertNotIn("/home/private-account", output)
            self.assertNotIn("private-account", output)
            self.assertNotIn("Page 1/", output)
            self.assertIn("Choose a number shown here", output)
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
            code, output = run_pty(fixture, b"\n", lines=12)
            self.assertEqual(2, code)
            self.assertIn(
                "0 sessions · 0 ready here · 0 open elsewhere", output
            )
            self.assertIn("More m", output)
            self.assertNotIn("Page 1/", output)
            self.assertNotIn("Outside provider 1", output)
            self.assertEqual([], fixture.sp_entries())
        finally:
            fixture.close()

    def test_guided_new_uses_first_project_default_and_child_sp(self) -> None:
        fixture = LoginFixture(inventory())
        try:
            code, output = run_pty(fixture, b"n\n2\n1\n\nq\n")
            self.assertEqual(2, code)
            self.assertIn("New session", output)
            self.assertIn("Use main:", output)
            self.assertIn("use name <number>", output)
            self.assertNotIn("Type yes to start", output)
            self.assertNotIn("New session cancelled", output)
            self.assertEqual(
                [["new", "codex", "main", "--account", "duck"]],
                [entry["args"] for entry in fixture.sp_entries()],
            )
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
            self.assertEqual(2, code)
            self.assertNotIn("main-codex:", output)
            self.assertIn(f"   1  main: {fixture.primary_project}", output)
            self.assertIn(f"   2  other: {fixture.other}", output)
            self.assertEqual(
                [["new", "codex", "main", "--account", "duck"]],
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
            # name and provider, back out, quit.
            code, output = run_pty(
                fixture,
                f"m\np\na\n{target}\n\n\n\n\nq\n".encode(),
                env_updates={"SESSION_KIT_PICKER_REFRESH_SECONDS": "0"},
            )
            self.assertEqual(2, code)
            self.assertIn("Projects", output)
            self.assertIn(f"main: {fixture.primary_project}", output)
            self.assertIn("Short name [newsite]", output)
            self.assertIn(f"Added newsite: claude in {target}", output)
            self.assertIn(
                f"newsite\tclaude\t{target}",
                fixture.projects.read_text(encoding="utf-8"),
            )
        finally:
            fixture.close()

    def test_projects_screen_drops_a_directory_for_good(self) -> None:
        fixture = LoginFixture(inventory(row("alpha", number=1)))
        try:
            code, output = run_pty(
                fixture,
                b"m\np\nd 2\n\n\nq\n",
                env_updates={"SESSION_KIT_PICKER_REFRESH_SECONDS": "0"},
            )
            self.assertEqual(2, code)
            self.assertIn("stays out of the project list", output)
            contents = fixture.projects.read_text(encoding="utf-8")
            self.assertIn(f"other\tignore\t{fixture.other}", contents)
            self.assertIn(f"main\tclaude\t{fixture.primary_project}", contents)
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
            self.assertEqual(2, code)
            # The first draw clears once. The automatic replacement is a
            # synchronized in-place frame and never exposes a blank screen.
            before_repaint = output.index("Codex bravo")
            self.assertEqual(1, output.count("\033[2J"))
            self.assertIn("\033[?2026h\033[H\033[J", output[:before_repaint])
            self.assertIn("\033[?2026l", output[before_repaint:])
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
            self.assertEqual(2, code)
            self.assertEqual(1, output.count("\033[2J"))
            self.assertNotIn("Invisible outside process", output)
        finally:
            fixture.close()

    def test_help_and_more_remain_modal_across_refresh_intervals(self) -> None:
        first = inventory(row("alpha", number=1))
        refreshed = inventory(
            row("alpha", number=1), row("bravo", number=2)
        )
        for command, marker in ((b"?\n", "Session picker help"), (b"m\n", "  More")):
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
                    self.assertEqual(2, code)
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

    def test_supervisor_pin_does_not_change_the_visible_fingerprint(self) -> None:
        supervisor = row("supervisor", number=1, provider="claude")
        other = row("other", number=2, provider="codex")
        before = inventory(other, supervisor)
        pinned_supervisor = dict(supervisor)
        pinned_supervisor["_picker_supervisor_pin"] = True
        after = inventory(pinned_supervisor, other)
        fixture = LoginFixture(before)
        pinned_path = fixture.base / "pinned.json"
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
                },
                deferred=("Codex bravo", b"ha\nq\n"),
            )
            self.assertEqual(2, code)
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
            self.assertEqual(2, code)
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
            self.assertEqual(2, code)
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
            self.assertEqual(2, code)
            self.assertNotIn("other:", output)
            self.assertIn(f"   1  main: {fixture.primary_project}", output)
            self.assertEqual(
                [["new", "codex", "main", "--account", "duck"]],
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
            code, output = run_pty(fixture, b"u\n\n\n")
            self.assertEqual(2, code)
            self.assertIn("More m", output)
            self.assertIn("[Claude] Recover me [logged in 2d 3h ago]", output)
            self.assertNotIn("old1", output)
            self.assertIn("1,3,5 or 2-4", output)
            self.assertIn("all (or a)", output)
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
            code, output = run_pty(fixture, b"u\nall\n\n")
            self.assertEqual(2, code)
            self.assertIn(
                "already active as session 4107; opening the existing session instead",
                output,
            )
            self.assertIn(
                "Use fork 4107 from the picker to start a separate writable fork.",
                output,
            )
            self.assertIn(
                "Cleared the recovery record for exact active session 4107.",
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
            code, output = run_pty(fixture, b"u\na\n\n")
            self.assertEqual(2, code)
            self.assertIn(
                "Exact active session 4107 was found, but its recovery record could not be cleared; the record was retained.",
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
            code, output = run_pty(fixture, b"u\na\n\n")
            self.assertEqual(2, code)
            self.assertIn(
                "already active outside the session manager; no duplicate was started",
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
            code, output = run_pty(fixture, b"u\na\n\n")
            self.assertEqual(2, code)
            self.assertIn("Started recovery item 1.", output)
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
            code, output = run_pty(fixture, b"u\n1,3\n\n")
            self.assertEqual(2, code)
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
            self.assertIn("Started recovery item 1.", output)
            self.assertIn("Started recovery item 3.", output)
            self.assertNotIn("Started recovery item 2.", output)
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
            code, output = run_pty(fixture, b"u\na\n\n")
            self.assertEqual(2, code)
            self.assertIn("Recovery record 1 is incomplete; it was retained.", output)
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
            code, output = run_pty(fixture, b"u\na\n\n")
            self.assertEqual(2, code)
            self.assertIn(
                "review required: conflicting cwd, command", output
            )
            self.assertIn(
                "Recovery record 1 has conflicting launch metadata "
                "(cwd, command); it requires manual review and was retained.",
                output,
            )
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
