"""The kit's dependence on vendor-internal files, kept visible.

Both vendors say these files are internal and may change between releases. When
one does change shape, nothing announces it: the reader finds no matching
record, returns empty, and a title or a whole history stops working while every
command still exits 0. The inventory in docs/pinned-internal-formats.md is the
answer to that -- but an inventory nobody checks goes stale in exactly the same
silence.

So: every kit file that reaches for one of these paths has to appear in the
document. A new reader added without a line in the table fails here, in the
change that adds it, rather than in a year when a format moves.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from tests.support import REPO


DOCTOR = REPO / "lib" / "sh" / "session_kit_doctor.sh"


def audit_source() -> str:
    """The audit block doctor actually ships, lifted out of its heredoc.

    The block is a stdlib-only program that takes its whole world on argv, so
    a test can point it at a fixture home and read the same rows a person sees
    -- without running doctor's shell, and without touching live state.
    """
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY\n", DOCTOR.read_text(encoding="utf-8"), re.S)
    if not blocks:
        raise AssertionError("session_kit_doctor.sh no longer embeds a python block")
    return max(blocks, key=len)


class FormatFixture:
    """A home shaped exactly like a live estate, one vendor file at a time."""

    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.codex_home = root / "codex"
        (self.home / ".claude" / "sessions").mkdir(parents=True)
        (self.home / ".claude" / "projects" / "-srv-x").mkdir(parents=True)
        (self.codex_home / "sessions" / "2026" / "08" / "13").mkdir(parents=True)

    def session_record(self, **overrides) -> "FormatFixture":
        record = {
            "sessionId": "22222222-3333-4444-8555-666666666666",
            "pid": 4242,
            "messagingSocketPath": "/run/user/1000/cc-socks/4242.sock",
            "statusUpdatedAt": 1786600000000,
            "status": "busy",
        }
        record.update(overrides)
        for field in [key for key, value in record.items() if value is None]:
            del record[field]
        path = self.home / ".claude" / "sessions" / "4242.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return self

    def key_file(self, payload=None) -> "FormatFixture":
        path = self.home / ".claude" / "sessions" / "4242.abc123.key"
        path.write_text(
            json.dumps({"peerToken": "a" * 32} if payload is None else payload),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        return self

    def transcript(self, lines=None, name="conversation.jsonl") -> "FormatFixture":
        if lines is None:
            lines = [{"type": "ai-title", "aiTitle": "x", "sessionId": "u"}]
        path = self.home / ".claude" / "projects" / "-srv-x" / name
        path.write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
        return self

    def rollout(self, lines=None) -> "FormatFixture":
        if lines is None:
            lines = [
                {"timestamp": "t", "type": "session_meta", "payload": {"id": "u"}},
                {"timestamp": "t", "type": "event_msg", "payload": {"type": "x"}},
            ]
        path = (
            self.codex_home
            / "sessions"
            / "2026"
            / "08"
            / "13"
            / "rollout-2026-08-13T11-45-36-01900000.jsonl"
        )
        path.write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
        return self

    def session_index(self, lines=None) -> "FormatFixture":
        if lines is None:
            lines = [{"id": "01900000-0000-7000-8000-000000000001", "thread_name": "x"}]
        (self.codex_home / "session_index.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
        return self

    def healthy(self) -> "FormatFixture":
        return (
            self.session_record()
            .key_file()
            .transcript()
            .rollout()
            .session_index()
        )


class DoctorFormatRowTests(unittest.TestCase):
    """The verdicts a person reads when a vendor moves a format.

    Every case below is a shape that once passed silently: doctor said `ok`
    while the field item 29 or item 30 depends on had already gone.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = audit_source()

    def row(self, fixture: FormatFixture, env=None):
        environment = dict(os.environ)
        environment.pop("SESSION_KIT_DOCTOR_FORMAT_SECONDS", None)
        environment.update(env or {})
        result = subprocess.run(
            [
                "python3",
                "-c",
                self.source,
                str(fixture.home),
                str(fixture.home / ".claude"),
                str(fixture.codex_home),
                "test-release",
                "linux-x86_64",
                str(fixture.home / "install"),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=environment,
        )
        self.assertEqual("", result.stderr.strip(), result.stderr)
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[1] == "internal-formats":
                return parts[0], parts[2]
        raise AssertionError(f"doctor emitted no internal-formats row:\n{result.stdout}")

    def fixture(self) -> FormatFixture:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        return FormatFixture(Path(root.name))

    def test_a_whole_healthy_estate_reports_every_format_checked(self) -> None:
        status, detail = self.row(self.fixture().healthy())
        self.assertEqual("ok", status, detail)
        self.assertIn("all 5", detail)

    def test_a_format_with_no_live_example_is_named_not_silently_passed(self) -> None:
        # Four formats present, one absent: the old row said `ok` as long as
        # ANY format had been checked, so a vanished file read as health.
        status, detail = self.row(
            self.fixture().session_record().key_file().transcript().rollout()
        )
        self.assertEqual("warn", status, detail)
        self.assertIn("Codex session index", detail)
        self.assertIn("4 of 5", detail)

    def test_the_session_record_losing_its_id_fails_and_says_which_field(self) -> None:
        status, detail = self.row(self.fixture().healthy().session_record(sessionId=None))
        self.assertEqual("fail", status, detail)
        self.assertIn("Claude session record", detail)
        self.assertIn("sessionId", detail)

    def test_the_socket_path_going_away_warns_by_name(self) -> None:
        # messagingSocketPath IS socket delivery. The old check read sessionId
        # and nothing else, so this exact loss reported `ok`.
        status, detail = self.row(
            self.fixture().healthy().session_record(messagingSocketPath=None)
        )
        self.assertEqual("warn", status, detail)
        self.assertIn("messagingSocketPath", detail)

    def test_the_attention_tie_breaker_going_away_warns_by_name(self) -> None:
        status, detail = self.row(
            self.fixture().healthy().session_record(statusUpdatedAt=None)
        )
        self.assertEqual("warn", status, detail)
        self.assertIn("statusUpdatedAt", detail)

    def test_an_empty_rollout_record_is_not_a_healthy_rollout(self) -> None:
        # `{}` is a dict, which is all the old check asked of it.
        status, detail = self.row(self.fixture().healthy().rollout(lines=[{}, {}]))
        self.assertEqual("fail", status, detail)
        self.assertIn("Codex rollout", detail)
        self.assertIn("type", detail)

    def test_a_rollout_that_lost_its_payload_fails(self) -> None:
        status, detail = self.row(
            self.fixture().healthy().rollout(lines=[{"type": "session_meta"}])
        )
        self.assertEqual("fail", status, detail)
        self.assertIn("payload", detail)

    def test_a_rollout_is_judged_on_the_lines_the_kit_reads(self) -> None:
        # Rollouts carry several kinds of line; one odd head line is not a
        # moved format as long as the shape the readers dispatch on is there.
        status, detail = self.row(
            self.fixture().healthy().rollout(
                lines=[{"note": "a line no reader looks at"}, {"type": "x", "payload": {}}]
            )
        )
        self.assertEqual("ok", status, detail)

    def test_a_session_index_without_ids_fails(self) -> None:
        status, detail = self.row(
            self.fixture().healthy().session_index(lines=[{"thread_name": "x"}])
        )
        self.assertEqual("fail", status, detail)
        self.assertIn("Codex session index", detail)
        self.assertIn("id", detail)

    def test_a_session_index_that_lost_its_names_warns(self) -> None:
        status, detail = self.row(
            self.fixture().healthy().session_index(lines=[{"id": "u"}])
        )
        self.assertEqual("warn", status, detail)
        self.assertIn("thread_name", detail)

    def test_an_untyped_transcript_record_fails(self) -> None:
        status, detail = self.row(
            self.fixture().healthy().transcript(lines=[{"aiTitle": "x"}])
        )
        self.assertEqual("fail", status, detail)
        self.assertIn("Claude transcript", detail)

    def test_a_key_file_without_a_token_fails(self) -> None:
        status, detail = self.row(self.fixture().healthy().key_file(payload={"x": 1}))
        self.assertEqual("fail", status, detail)
        self.assertIn("peerToken", detail)

    def test_a_transcript_still_being_written_is_not_a_moved_format(self) -> None:
        # The newest *.jsonl on a healthy estate can be a file a session created
        # a second ago. Doctor's loudest verdict must not come from that.
        fixture = self.fixture().healthy()
        fresh = fixture.home / ".claude" / "projects" / "-srv-x" / "brand-new.jsonl"
        fresh.write_bytes(b"")
        status, detail = self.row(fixture)
        self.assertEqual("ok", status, detail)

    def test_a_half_written_line_is_not_a_moved_format(self) -> None:
        fixture = self.fixture().healthy()
        partial = fixture.home / ".claude" / "projects" / "-srv-x" / "partial.jsonl"
        partial.write_text('{"type": "ai-tit', encoding="utf-8")
        status, detail = self.row(fixture)
        self.assertEqual("ok", status, detail)

    def test_the_scan_gives_up_on_a_slow_home_instead_of_hanging(self) -> None:
        # A stalled or enormous home is a doctor that never returns. The scan
        # carries a wall-clock budget and says so when it runs out.
        status, detail = self.row(
            self.fixture().healthy(),
            env={"SESSION_KIT_DOCTOR_FORMAT_SECONDS": "0"},
        )
        self.assertEqual("warn", status, detail)
        self.assertIn("ran out of time", detail)

    def test_a_home_with_thousands_of_rollouts_still_answers_quickly(self) -> None:
        fixture = self.fixture().healthy()
        day = fixture.codex_home / "sessions" / "2026" / "08" / "13"
        for number in range(3000):
            (day / f"rollout-2026-08-13T00-00-{number:04d}-uuid.jsonl").write_bytes(b"")
        started = os.times()
        status, detail = self.row(fixture)
        self.assertLess(os.times().elapsed - started.elapsed, 20.0)
        self.assertIn(status, {"ok", "warn"}, detail)


DOCUMENT = REPO / "docs" / "pinned-internal-formats.md"

# What counts as touching a vendor internal. Each pattern is a path shape that
# only the vendors own.
INTERNAL_PATTERNS = (
    re.compile(r"\.claude[\"'/].{0,40}sessions"),
    re.compile(r"\.claude[\"'/].{0,40}projects"),
    re.compile(r"session_index\.jsonl"),
    re.compile(r"rollout-"),
)

# Files that match a pattern without depending on the format: they pass a path
# through, name it in prose, or check that it exists.
NOT_A_FORMAT_DEPENDENCY = {
    # Reads only the kit's own state file, and only for a colour.
    "config/claude/statusline.sh",
    # Hands paths to the readers; parses nothing itself.
    "lib/sessionkit_inventory/transcripts.py",
    "lib/sessionkit_inventory/collector.py",
    "lib/session_inventory.py",
    # Kit-owned files that happen to live under the vendor's directory
    # (.nameintent, .colorset, .titleset are the kit's own inventions).
    "lib/sessionkit_inventory/projects.py",
    "lib/sessionkit_inventory/closed_sessions.py",
    "lib/sessionkit_inventory/accounts.py",
    "lib/sessionkit_inventory/snapshot.py",
    "lib/sessionkit_tui/runner.py",
    "lib/sh/sp_sessions.sh",
    "lib/sh/sp_provider_bounce.sh",
    # Reads the transcript path the HOOK hands it -- the sanctioned interface,
    # documented as already-migrated.
    "config/claude/nameintent_title.sh",
    # The checker itself: it reads one live example of each format precisely to
    # notice when the shape changes.
    "lib/sh/session_kit_doctor.sh",
}

SEARCH_ROOTS = ("lib", "bin", "config", "tools")


def tracked_sources() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", *SEARCH_ROOTS],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        name
        for name in result.stdout.splitlines()
        if name.endswith((".py", ".sh")) or "/" not in name
    ]


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = DOCUMENT.read_text(encoding="utf-8")

    def test_every_reader_of_a_vendor_internal_is_in_the_document(self) -> None:
        missing = []
        for name in tracked_sources():
            if name in NOT_A_FORMAT_DEPENDENCY:
                continue
            try:
                source = (REPO / name).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not any(pattern.search(source) for pattern in INTERNAL_PATTERNS):
                continue
            # The document names modules, not paths: `providers_claude`,
            # `names_push`, `transcript_text`, `claude_socket`, `self_name`.
            stem = Path(name).stem
            if stem not in self.text:
                missing.append(name)
        self.assertEqual(
            [],
            sorted(missing),
            "these files read or write a vendor internal format and are not in "
            f"{DOCUMENT.relative_to(REPO)}:\n  "
            + "\n  ".join(sorted(missing))
            + "\n\nAdd the path, the exact fields, the sanctioned alternative "
            "(or 'none today'), and what a person sees when it breaks.",
        )

    def test_each_entry_says_what_a_person_sees_when_it_breaks(self) -> None:
        # Five formats, five failure descriptions. An entry without one is an
        # inventory line, not a warning.
        self.assertEqual(5, self.text.count("**When it breaks:**"))
        self.assertEqual(5, self.text.count("**Sanctioned alternative"))

    def test_doctor_reports_the_pinned_formats(self) -> None:
        doctor = (REPO / "lib" / "sh" / "session_kit_doctor.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"internal-formats"', doctor)
        # A shape that has already changed is not a warning: something a person
        # depends on is broken right now.
        self.assertIn('emit("fail", "internal-formats"', doctor)
        self.assertIn("internal-formats", doctor.split("audit_names=(")[1][:400])


if __name__ == "__main__":
    unittest.main()
