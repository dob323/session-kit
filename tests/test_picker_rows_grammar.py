"""The six rulings about what a session row says, as tests.

Most of these fail on the commit before the change and pass after it, and that
is the point of the file. Not all of them, and the difference is worth naming
rather than glossing: a handful are REGRESSION GUARDS over behaviour that was
already right and had to stay right, `b` already worked on the account screen,
switch mode already refused to default, the rename prompt already declined to
claim `b`, and no third state existed to begin with. Those pass at both commits
by design. A reviewer counting differentials should not count them, and should
not read this file as claiming they are.

To reproduce the differential: copy this file AND
`lib/sessionkit_inventory/session_model.py` into a tree at the older commit,
without the module the whole file fails at import and proves nothing per test.

They are grouped by the ruling they hold, and each one states the defect it
exists to catch rather than the code it happens to touch:

1. the model column was empty on every row, and had to follow a model changed
   INSIDE a session, not the one it was launched with
2. rows carried two times and truncated at a different point on every line
3. two providers described one situation in two words
4. the account column stays
5. one grammar: `b` goes back, Enter takes the recommended choice
6. columns line up, wide characters included
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unicodedata
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_inventory import labels  # noqa: E402
from sessionkit_inventory import render  # noqa: E402
from sessionkit_inventory import session_model  # noqa: E402
from sessionkit_inventory.model import canonical_session_order_key  # noqa: E402
from sessionkit_tui import frame as tui_frame  # noqa: E402
from sessionkit_tui import rows as tui_rows  # noqa: E402
from sessionkit_tui.model import parse_inventory  # noqa: E402


NOW_MS = 1_800_000_000_000


def cells(text: str) -> int:
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )


def row(
    title: str,
    number: int,
    *,
    provider: str = "claude",
    **extra: object,
) -> dict:
    item = {
        "row": number,
        "terminal_number": number,
        "shpool_id": f"s-{number}",
        "shpool_id_raw": f"s-{number}",
        "display_shpool_id": f"s-{number}",
        "availability": "ready",
        "provider": provider,
        "display_provider": provider,
        "setup_incomplete": False,
        "identity": {
            "uuid": f"00000000-0000-4000-8000-00000000000{number}",
            "pid": 1000 + number,
            "confidence": "exact",
        },
        "title": title,
        "display_title": title,
        "native_title": title,
        "cwd": "/srv/project",
        "account_alias": "primary",
        "agent_status": "running",
        "needs_you": False,
        "subagents": [],
        "active_subagent_count": 0,
        "started_at_unix_ms": NOW_MS - 11_340_000,
        "process_age_seconds": 11_340,
        "recent_output_at_unix_ms": NOW_MS - 11_340_000,
        "recent_output_age_seconds": 11_340,
        "recovery": {"available": False},
        "diagnostics": [],
    }
    item.update(extra)
    return item


def document(*items: dict) -> dict:
    return {
        "schema_version": 1,
        "source": "live",
        "stale": False,
        "sessions": list(items),
        "outside_agents": [],
        "_picker": {},
    }


def picker_lines(*items: dict, columns: int = 130, compact: bool = False) -> list[str]:
    text = render.render_picker_page(
        document(*items),
        page=1,
        page_size=20,
        style_enabled=False,
        compact=compact,
        columns=columns,
        now_ms=NOW_MS,
    )
    return [line for line in text.splitlines() if " | " in line]


def list_lines(*items: dict, columns: int = 130) -> list[str]:
    previous = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = str(columns)
    try:
        text = render.render_inventory(
            document(*items), color_enabled=lambda: False, now_ms=NOW_MS
        )
    finally:
        if previous is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = previous
    return [line for line in text.splitlines() if " | " in line]


def cursor_lines(*items: dict, columns: int = 130) -> list[str]:
    parsed = parse_inventory(document(*items))
    rows = tui_rows.build_rows(parsed.sessions, now_ms=NOW_MS)
    text = tui_frame.build_frame(
        tui_frame.Screen(rows=rows), width=columns, height=40
    ).joined()
    numbers = {item["terminal_number"] for item in items}
    return [
        line
        for line in text.splitlines()
        if (match := __import__("re").match(r"^\s*(\d+)\s\s", line))
        and int(match.group(1)) in numbers
    ]


# --------------------------------------------------------------- ruling 1


class ModelColumnTests(unittest.TestCase):
    """The model cell was `, ` on all twelve of the operator's live sessions."""

    def test_the_row_shows_the_model_a_session_is_running(self) -> None:
        for line in picker_lines(
            row("Ledger Sync Audit", 1, display_model="Opus 5", model="claude-opus-5"),
            row(
                "Codex worker",
                2,
                provider="codex",
                display_model="GPT-5-Codex",
                model="gpt-5-codex",
            ),
        ):
            self.assertNotIn(f"| {labels.MISSING} | {labels.MISSING} |", line)
        joined = "\n".join(picker_lines(
            row("Ledger Sync Audit", 1, display_model="Opus 5", model="claude-opus-5"),
        ))
        self.assertIn("Opus 5", joined)
        self.assertNotIn("claude-opus-5", joined)

    def test_a_model_with_no_evidence_says_so_instead_of_showing_a_dash(self) -> None:
        unreadable = picker_lines(
            row("Claude one", 1, model_state=labels.MODEL_STATE_UNREADABLE)
        )[0]
        self.assertIn("pe…", unreadable)
        fresh = picker_lines(
            row("Claude two", 1, model_state=labels.MODEL_STATE_NO_REPLY_YET)
        )[0]
        self.assertIn("pe…", fresh)
        shell = picker_lines(row("A shell", 1, provider="shell"))[0]
        self.assertIn(labels.MODEL_NOT_APPLICABLE, shell)

    def test_a_model_identifier_becomes_a_name_a_person_reads(self) -> None:
        for identifier, expected in (
            ("claude-opus-5", "Opus 5"),
            ("claude-fable-5", "Fable 5"),
            ("claude-sonnet-4-5-20250929", "Sonnet 4.5"),
            ("claude-3-5-haiku-20241022", "Haiku 3.5"),
        ):
            with self.subTest(identifier=identifier):
                self.assertEqual(
                    expected, session_model.human_model_name("claude", identifier)
                )
        for identifier, expected in (
            ("gpt-5-codex", "GPT-5-Codex"),
            ("gpt-5.6-sol", "GPT-5.6-Sol"),
            ("gpt-5.1-codex-max", "GPT-5.1-Codex-Max"),
        ):
            with self.subTest(identifier=identifier):
                self.assertEqual(
                    expected, session_model.human_model_name("codex", identifier)
                )


class ModelFollowsTheSessionTests(unittest.TestCase):
    """"It also needs to change when I change the model within the session."""

    def claude_transcript(self, path: Path, *models: str) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            for model in models:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {"model": model, "content": []},
                        }
                    )
                    + "\n"
                )

    def test_a_model_changed_inside_a_claude_session_is_the_one_shown(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            transcript = root / "conversation.jsonl"
            cache = root / "state"
            self.claude_transcript(transcript, "claude-opus-5", "claude-opus-5")
            model, found = session_model.current_model(
                "claude", key="one", locate=lambda: transcript, cache_dir=cache
            )
            self.assertTrue(found)
            self.assertEqual("claude-opus-5", model)

            # The person types /model inside the session. Nothing about the
            # process changes; the next assistant record does.
            self.claude_transcript(transcript, "claude-fable-5")
            model, _ = session_model.current_model(
                "claude", key="one", locate=lambda: transcript, cache_dir=cache
            )
            self.assertEqual("claude-fable-5", model)

    def test_a_model_changed_inside_a_codex_session_is_the_one_shown(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rollout = root / "rollout.jsonl"
            cache = root / "state"
            with open(rollout, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n"
                )
                handle.write(
                    json.dumps(
                        {"type": "turn_context", "payload": {"model": "gpt-5-codex"}}
                    )
                    + "\n"
                )
            model, found = session_model.current_model(
                "codex", key="two", locate=lambda: rollout, cache_dir=cache
            )
            self.assertTrue(found)
            self.assertEqual("gpt-5-codex", model)

            with open(rollout, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "thread_settings_applied",
                                "thread_settings": {"model": "gpt-5.6-sol"},
                            },
                        }
                    )
                    + "\n"
                )
            model, _ = session_model.current_model(
                "codex", key="two", locate=lambda: rollout, cache_dir=cache
            )
            self.assertEqual("gpt-5.6-sol", model)

    def test_a_resumed_read_costs_only_the_bytes_the_provider_appended(self) -> None:
        """The reason this can run on every refresh at all."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            transcript = root / "conversation.jsonl"
            cache = root / "state"
            self.claude_transcript(transcript, *["claude-opus-5"] * 200)
            located: list[Path] = []

            def locate() -> Path:
                located.append(transcript)
                return transcript

            session_model.current_model(
                "claude", key="three", locate=locate, cache_dir=cache
            )
            self.assertEqual(1, len(located))
            # Nothing appended: the file is not read again, and the expensive
            # part -- finding the transcript at all -- is not repeated.
            session_model.current_model(
                "claude", key="three", locate=locate, cache_dir=cache
            )
            self.assertEqual(1, len(located))

    def test_a_synthetic_assistant_record_is_not_a_model(self) -> None:
        """Measured on a live transcript: Claude writes `<synthetic>` itself."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            transcript = root / "conversation.jsonl"
            self.claude_transcript(transcript, "claude-opus-5", "<synthetic>")
            model, _ = session_model.current_model(
                "claude", key="four", locate=lambda: transcript, cache_dir=None
            )
            self.assertEqual("claude-opus-5", model)


# --------------------------------------------------------------- ruling 2


class OneTimeColumnTests(unittest.TestCase):
    """Rows carried `recent output Xh Ym ago` AND `process age Xh Ym`."""

    def sample(self) -> tuple[dict, ...]:
        return (
            row("Ledger Sync Audit", 1, needs_you=True, agent_status="needs your reply"),
            row("Orphaned Record Audit", 2, recent_output_age_seconds=0,
                recent_output_at_unix_ms=NOW_MS),
            row("Codex worker", 3, provider="codex", agent_status="idle"),
        )

    def test_a_row_carries_exactly_one_time(self) -> None:
        for lines in (picker_lines(*self.sample()), list_lines(*self.sample())):
            self.assertEqual(3, len(lines))
            for line in lines:
                self.assertEqual(1, line.count(labels.LAST_ACTIVE), line)
                self.assertNotIn("process age", line)
                self.assertNotIn("recent output", line)
                self.assertNotIn("opened", line)

    def test_every_row_words_that_time_the_same_way(self) -> None:
        for lines in (picker_lines(*self.sample()), list_lines(*self.sample())):
            starts = {line.index(labels.LAST_ACTIVE) for line in lines}
            self.assertEqual(1, len(starts), lines)

    # A worst-case row shape: the longest session name a row has to carry,
    # the longest model string, and a wide-character (CJK) title. A fixture
    # narrower than the widest real row is how the previous version of this
    # test passed while the time column was missing on screen: it needed 101
    # columns to show a time and this shape needs more.
    OPERATOR_WIDTHS = (80, 100, 120, 140)

    @staticmethod
    def has_time(line: str) -> bool:
        # The compact token is the ruled narrow-width form; it is still an
        # explicit age, not an absent or clipped label.
        return labels.LAST_ACTIVE in line or "3h 9m" in line

    def operator_shape(self) -> tuple[dict, ...]:
        return (
            row("Ledger Sync Audit", 1, needs_you=True,
                agent_status="needs your reply", display_model="Opus 5"),
            row("Nightly Index Rebuild Coordination Planner", 2, provider="codex",
                agent_status="idle", display_model="GPT-5.6-Sol-Preview"),
            row("\u5728\u5eab\u7ba1\u7406\u30b7\u30b9\u30c6\u30e0\u70b9\u691c", 3,
                display_model="Sonnet 4.5"),
            row("A plain shell", 4, provider="shell"),
        )

    def test_the_time_is_on_every_row_at_the_widths_that_matter(self) -> None:
        """The ruling was keep ONE time column, not drop both.

        The version of this test that shipped in `e1e3edd` accepted "no row has
        a time" as a pass, and its fixture was too small to reach the width
        where the column disappeared. It was green while no row on screen had
        a time. This one names the widths and demands the column.
        """
        for columns in self.OPERATOR_WIDTHS:
            for compact in (False, True):
                with self.subTest(columns=columns, compact=compact):
                    lines = picker_lines(
                        *self.operator_shape(), columns=columns, compact=compact
                    )
                    self.assertEqual(4, len(lines))
                    missing = [line for line in lines if not self.has_time(line)]
                    self.assertEqual([], missing, "\n".join(lines))

    def test_the_widest_row_costs_its_own_tail_and_not_the_column(self) -> None:
        """The one-character cliff: a 41-character name used to delete the time
        from every row on the page at 120 columns."""
        for length in (20, 39, 40, 41, 42, 60, 90):
            with self.subTest(longest_title=length):
                items = list(self.operator_shape())
                items[1] = row("x" * length, 2, provider="codex",
                               display_model="GPT-5.6-Sol-Preview")
                lines = picker_lines(*items, columns=120)
                self.assertTrue(
                    all(self.has_time(line) for line in lines), "\n".join(lines)
                )

    def test_every_width_60_to_200_keeps_a_time_on_both_pickers(self) -> None:
        """The compact tier closes the old 60–91 all-row disappearance."""
        for columns in range(60, 201):
            with self.subTest(columns=columns):
                for lines in (
                    picker_lines(*self.operator_shape(), columns=columns),
                    cursor_lines(*self.operator_shape(), columns=columns),
                ):
                    self.assertEqual(4, len(lines), "\n".join(lines))
                    self.assertTrue(
                        all(self.has_time(line) for line in lines),
                        "\n".join(lines),
                    )

    def test_the_narrowest_supported_width_shows_the_time(self) -> None:
        def threshold(compact: bool) -> int:
            for columns in range(60, 241):
                lines = picker_lines(
                    *self.operator_shape(), columns=columns, compact=compact
                )
                if lines and all(self.has_time(line) for line in lines):
                    return columns
            raise AssertionError("no width shows the time")

        for compact in (False, True):
            with self.subTest(compact=compact):
                self.assertEqual(60, threshold(compact))

    def test_the_two_surfaces_use_one_phrase(self) -> None:
        self.assertEqual("last active 3h 9m ago", labels.last_active(11_340))
        self.assertEqual("last active just now", labels.last_active(4))
        self.assertEqual("last active pending", labels.last_active(None))


# --------------------------------------------------------------- ruling 3


class OneStateVocabularyTests(unittest.TestCase):
    """Four Claude rows said `needs you`; four Codex rows in the same
    situation said `idle`."""

    def test_notification_idle_is_retired_but_transcript_idle_is_a_state(self) -> None:
        self.assertEqual(
            (
                labels.QUESTION,
                labels.WAITING_ON_YOU,
                labels.WORKING,
                labels.IDLE,
                labels.SETUP_INCOMPLETE,
                labels.STATUS_UNAVAILABLE,
            ),
            labels.SESSION_STATES,
        )

    def test_both_providers_say_one_word_for_one_situation(self) -> None:
        claude = {"agent_status": labels.STATUS_NEEDS_YOUR_REPLY, "needs_you": True}
        codex = {"agent_status": labels.STATUS_IDLE, "needs_you": False}
        state = lambda item: labels.session_state(item, stall_seconds=2700)  # noqa: E731
        self.assertEqual(labels.WAITING_ON_YOU, state(claude))
        self.assertEqual(labels.WAITING_ON_YOU, state(codex))
        self.assertEqual(
            labels.IDLE,
            state({**codex, "transcript_idle": True}),
        )

    def test_the_rendered_rows_agree_too(self) -> None:
        lines = picker_lines(
            row("Claude at its prompt", 1, needs_you=True,
                agent_status="needs your reply"),
            row("Codex at its prompt", 2, provider="codex", agent_status="idle"),
        )
        self.assertEqual(2, len(lines))
        for line in lines:
            self.assertIn(labels.WAITING_ON_YOU, line)
            self.assertNotIn("idle", line)

    def test_the_cursor_picker_agrees_with_the_text_picker(self) -> None:
        parsed = parse_inventory(
            document(
                row("Claude at its prompt", 1, needs_you=True,
                    agent_status="needs your reply"),
                row("Codex at its prompt", 2, provider="codex", agent_status="idle"),
            )
        )
        self.assertEqual(
            [labels.WAITING_ON_YOU, labels.WAITING_ON_YOU],
            [session.state_word() for session in parsed.sessions],
        )

    def test_no_third_state_was_invented(self) -> None:
        """A `finished` state was considered and declined."""
        for word in labels.STATE_WORDS.values():
            self.assertIn(word, labels.SESSION_STATES)
        self.assertNotIn("finished", labels.SESSION_STATES)


class OneListOneTruthTests(unittest.TestCase):
    """The two surfaces computed the same cell separately and disagreed on 14
    of them. They read one function now, and this walks every state a collector
    can write plus the shapes that broke."""

    def rows(self) -> tuple[dict, ...]:
        base = dict(display_model="Opus 5", model="claude-opus-5")
        cases = [
            row("Running", 1, agent_status="running", **base),
            row("Idle", 2, agent_status="idle", **base),
            row("Needs reply", 3, agent_status="needs your reply",
                needs_you=True, **base),
            row("Reply optional", 4, agent_status="reply optional", **base),
            row("Provider exited", 5, agent_status="provider exited", **base),
            row("Unknown", 6, agent_status="unknown", **base),
            row("State unavailable", 7, agent_status="state unavailable", **base),
            row("No status at all", 8, agent_status="", **base),
            row("Setup incomplete", 9, setup_incomplete=True, **base),
            row("Vendor busy", 10, agent_status="busy", **base),
            row("A word from the future", 11, agent_status="brand-new-state", **base),
            row("No reply yet", 12, display_model="", model="",
                model_state=labels.MODEL_STATE_NO_REPLY_YET),
            row("Unreadable", 13, display_model="", model="",
                model_state=labels.MODEL_STATE_UNREADABLE),
            row("A shell", 14, provider="shell", display_model="", model=""),
            row("Quiet past the stall window", 15, agent_status="running",
                recent_output_age_seconds=99_999,
                recent_output_at_unix_ms=NOW_MS - 99_999_000, **base),
            row("Future stamp", 16, recent_output_at_unix_ms=NOW_MS + 600_000,
                recent_output_age_seconds=900, **base),
        ]
        return tuple(cases)

    def test_the_two_surfaces_agree_on_every_cell(self) -> None:
        stall = render.stall_threshold_seconds()
        items = self.rows()
        shell_items = sorted(items, key=canonical_session_order_key)
        shell = [
            (
                item["terminal_number"],
                labels.session_state(item, stall_seconds=stall),
                labels.model_cell(item),
                labels.row_last_active(item, NOW_MS),
            )
            for item in shell_items
        ]
        drawn = tui_rows.build_rows(
            parse_inventory(document(*items)).sessions, now_ms=NOW_MS, stall=stall
        )
        tui = [
            (drawn_row.number, drawn_row.state, drawn_row.model, drawn_row.age)
            for drawn_row in drawn
            if drawn_row.kind == tui_rows.SESSION
        ]
        self.assertEqual(shell, tui)

    def test_setup_incomplete_reaches_the_cursor_picker(self) -> None:
        """It could not appear there at all: `state_word` built a synthetic dict
        without the key `session_state` tests first."""
        parsed = parse_inventory(document(row("Half-built", 1, setup_incomplete=True)))
        self.assertEqual(
            labels.SETUP_INCOMPLETE, parsed.sessions[0].state_word()
        )

    def test_a_session_with_no_reply_yet_is_not_called_unreadable(self) -> None:
        """The cursor picker guessed from the provider and reported a healthy
        new conversation as a broken transcript."""
        parsed = parse_inventory(
            document(
                row("Fresh", 1, display_model="", model="",
                    model_state=labels.MODEL_STATE_NO_REPLY_YET)
            )
        )
        self.assertEqual(labels.MODEL_NO_REPLY_YET, parsed.sessions[0].model_text)


class TotalStateMappingTests(unittest.TestCase):
    """Two words, and no collector word able to leak past them."""

    def test_every_collector_status_maps_into_the_vocabulary(self) -> None:
        for status in labels.COLLECTOR_STATUSES:
            with self.subTest(status=status):
                self.assertIn(labels.state_word(status), labels.SESSION_STATES)

    def test_a_word_this_file_does_not_know_is_never_printed(self) -> None:
        for status in ("busy", "brand-new-state", "Working On It", "", "idle "):
            with self.subTest(status=status):
                self.assertIn(labels.state_word(status), labels.SESSION_STATES)

    def test_no_row_can_render_a_word_outside_the_vocabulary(self) -> None:
        stall = render.stall_threshold_seconds()
        for status in (*labels.COLLECTOR_STATUSES, "busy", "anything at all", ""):
            for needs_you in (True, False):
                with self.subTest(status=status, needs_you=needs_you):
                    state = labels.session_state(
                        {"agent_status": status, "needs_you": needs_you},
                        stall_seconds=stall,
                    )
                    self.assertIn(state, labels.SESSION_STATES)

    def test_the_two_words_carry_every_ordinary_session(self) -> None:
        """`pending` and `unknown` are the kit saying it
        cannot read a row -- never one of the two states."""
        stall = render.stall_threshold_seconds()
        ordinary = ("running", "working", "reply optional", "idle",
                    "needs your reply", "provider exited")
        for status in ordinary:
            with self.subTest(status=status):
                self.assertIn(
                    labels.session_state(
                        {"agent_status": status, "needs_you": False},
                        stall_seconds=stall,
                    ),
                    (labels.WAITING_ON_YOU, labels.WORKING),
                )


class ModelTruthTests(unittest.TestCase):
    """A wrong model name is worse than the empty column they complained about."""

    def test_an_identifier_this_kit_does_not_understand_keeps_its_raw_form(self) -> None:
        for provider, identifier in (
            ("claude", "claude-opus-experimental-5"),
            ("claude", "claude-sonnet-enterprise-4-9"),
            ("codex", "gpt-5-quantumthing"),
            ("codex", "some-vendor-model-x"),
        ):
            with self.subTest(identifier=identifier):
                self.assertEqual(
                    identifier, session_model.human_model_name(provider, identifier)
                )

    def test_the_launch_argument_is_never_shown_as_the_live_model(self) -> None:
        """No local record is proof of nothing, not proof that the model a
        session was started with is the one it runs."""
        import session_inventory

        item = {
            "provider": "claude",
            "identity": {"uuid": "11111111-2222-4333-8444-555555555555", "pid": 42},
        }
        table = {42: {"cmdline": ["claude", "--model", "claude-opus-5"],
                      "requested_model": "claude-opus-5"}}
        fields = session_inventory._live_model_fields(item, table, None)
        self.assertEqual("", fields["model"])
        self.assertEqual("", fields["display_model"])
        self.assertEqual("claude-opus-5", fields["launch_model"])
        self.assertEqual(
            labels.MODEL_UNREADABLE, labels.model_cell({**item, **fields})
        )

    def test_a_transcript_replaced_at_the_same_path_is_re_read(self) -> None:
        """An inode can be reused after unlink+create, so it is not an identity
        on its own; the cache must not answer from the deleted file.

        Filesystems are free not to reuse the inode immediately.  Pin that
        input here so this exercises the same-inode case on every runner.
        """
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / "cache"
            transcript = root / "t.jsonl"
            real_fstat = os.fstat
            generation = 1

            def pinned_fstat(descriptor: int) -> SimpleNamespace:
                info = real_fstat(descriptor)
                return SimpleNamespace(
                    st_ino=12345,
                    st_dev=info.st_dev,
                    st_ctime_ns=generation,
                    st_size=info.st_size,
                )

            def write(model: str) -> None:
                transcript.write_text(
                    json.dumps(
                        {"type": "assistant", "message": {"model": model, "content": []}}
                    )
                    + "\n",
                    encoding="utf-8",
                )

            with mock.patch.object(session_model.os, "fstat", side_effect=pinned_fstat):
                write("claude-fable-5")
                self.assertEqual(
                    "claude-fable-5",
                    session_model.current_model(
                        "claude", key="k", locate=lambda: transcript, cache_dir=cache
                    )[0],
                )
                os.unlink(transcript)
                write("claude-sonnet-4-5-20250929")
                generation = 2
                self.assertEqual(
                    "claude-sonnet-4-5-20250929",
                    session_model.current_model(
                        "claude", key="k", locate=lambda: transcript, cache_dir=cache
                    )[0],
                )

    def test_a_first_record_caught_mid_write_is_read_once_it_completes(self) -> None:
        """The cache claimed the file size as scanned when nothing complete had
        been read, so the resumed reader started inside the record and the
        conversation said `no reply yet` forever."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / "cache"
            transcript = root / "t.jsonl"
            transcript.write_text(
                '{"type":"assistant","message":{"model":"claude-fable-5"',
                encoding="utf-8",
            )
            self.assertEqual(
                "",
                session_model.current_model(
                    "claude", key="p", locate=lambda: transcript, cache_dir=cache
                )[0],
            )
            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write(',"content":[]}}\n')
            self.assertEqual(
                "claude-fable-5",
                session_model.current_model(
                    "claude", key="p", locate=lambda: transcript, cache_dir=cache
                )[0],
            )

    def test_a_clock_that_runs_ahead_does_not_make_every_row_look_fresh(self) -> None:
        """A future stamp used to clamp to zero and read `just now`, which turns
        a screen of waiting sessions into a screen of busy ones."""
        item = row("Skewed", 1, recent_output_at_unix_ms=NOW_MS + 600_000,
                   recent_output_age_seconds=900)
        self.assertEqual(900, labels.last_active_seconds(item, NOW_MS))
        self.assertEqual("last active 15m ago", labels.row_last_active(item, NOW_MS))


# --------------------------------------------------------------- ruling 4


class AccountColumnTests(unittest.TestCase):
    def test_the_account_column_stays_on_every_row(self) -> None:
        for lines in (
            picker_lines(row("Alpha", 1), row("Bravo", 2, account_alias="wren")),
            list_lines(row("Alpha", 1), row("Bravo", 2, account_alias="wren")),
        ):
            self.assertTrue(any("primary" in line for line in lines), lines)
            self.assertTrue(any("wren" in line for line in lines), lines)


# --------------------------------------------------------------- ruling 5


def account_choice(action: str, payload: dict, mode: str, argument: str):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(payload, handle)
        path = handle.name
    try:
        return subprocess.run(
            [
                "bash",
                "-c",
                f'. "{REPO}/lib/sh/shpool_login_actions.sh"; '
                f'account_choice_ui "$1" "$2" "$3" "$4"',
                "bash",
                action,
                path,
                mode,
                argument,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "COLUMNS": "140"},
        )
    finally:
        os.unlink(path)


def accounts(*, recommendation: str = "") -> dict:
    return {
        "choices": [
            {
                "alias": "primary",
                "email": "primary@example.com",
                "plan": "max",
                "eligible": True,
                "state": "ready",
                "u5h": 0.25,
                "u7d": 0.48,
                "recommended": recommendation == "primary",
            },
            {
                "alias": "wren",
                "email": "wren@example.com",
                "plan": "max",
                "eligible": True,
                "state": "ready",
                "u5h": 0.06,
                "u7d": 0.10,
                "recommended": recommendation == "wren",
            },
        ],
        "recommendation": recommendation,
        "roster_state": "fresh",
        "advice_fresh": True,
    }


class OneGrammarTests(unittest.TestCase):
    """Enter takes the most likely option, and every screen names it.

    It used to mean `back` on one screen and `use the default` on the next,
    with nothing on the screen saying which.
    """

    SCREENS = {
        "picker help": ("lib/sh/shpool_login_render.sh", "↵ back · b back ❯ "),
        "needs you": ("lib/sh/shpool_login_render.sh", "↵ back · b back · help ?"),
        "more": ("lib/sh/shpool_login_render.sh", "↵ back · b back"),
        "open elsewhere": (
            "lib/sh/shpool_login_actions.sh",
            "↵ move it here · b back",
        ),
        "provider": (
            "lib/sh/shpool_login_actions.sh",
            "↵ Claude Code · b back",
        ),
        "model": (
            "lib/sh/shpool_login_actions.sh",
            "Model number or exact name · ↵ back · b back ❯ ",
        ),
        "project": ("lib/sh/shpool_login_actions.sh", "use that project · b back"),
        "closed sessions": (
            "lib/sh/shpool_login_recovery.sh",
            "restore numbers · one with no number goes by the selector beside it · restore all a · ↵ back · b back",
        ),
        "projects": ("lib/sh/shpool_login_projects.sh", "↵ back · b back"),
        "not on your list": (
            "lib/sh/shpool_login_projects.sh",
            "add numbers · add all a · never list x · ↵ back · b back",
        ),
        "hidden directories": (
            "lib/sh/shpool_login_projects.sh",
            "restore number · ↵ back · b back",
        ),
    }

    def test_every_picker_screen_names_the_back_key(self) -> None:
        for screen, (relative, literal) in self.SCREENS.items():
            with self.subTest(screen=screen):
                source = (REPO / relative).read_text(encoding="utf-8")
                self.assertIn(literal, source)

    # Two prompts take a typed NAME, where `b` is a one-letter name and not a
    # key. They say what Enter does and do not claim a key they cannot honour.
    TYPED_NAME_PROMPTS = ("New name (↵ back) ❯",)

    def test_no_screen_still_offers_back_without_naming_b(self) -> None:
        """The old grammar: `↵ back` with no way to type it."""
        for relative in {path for path, _ in self.SCREENS.values()}:
            for number, line in enumerate(
                (REPO / relative).read_text(encoding="utf-8").splitlines(), 1
            ):
                if "↵ back" not in line or line.lstrip().startswith("#"):
                    continue
                if any(prompt in line for prompt in self.TYPED_NAME_PROMPTS):
                    continue
                with self.subTest(where=f"{relative}:{number}"):
                    self.assertIn("b back", line, line)

    def test_a_prompt_that_takes_a_name_does_not_claim_the_back_key(self) -> None:
        source = (REPO / "lib/sh/shpool_login_actions.sh").read_text(encoding="utf-8")
        self.assertIn("New name (↵ back) ❯", source)
        self.assertNotIn("New name (↵ back · b back)", source)

    def test_the_top_screen_names_the_way_out(self) -> None:
        source = (REPO / "lib/sh/shpool_login_render.sh").read_text(encoding="utf-8")
        # Enter opens the top row here, so the door out is named for the keys
        # that take it -- q and b -- and not for Enter.
        self.assertIn("leave q or b", source)
        self.assertIn("leave $(picker_green q) or $(picker_green b)", source)
        self.assertNotIn("back %s or %s", source)

    def test_b_goes_back_from_the_account_screen(self) -> None:
        for typed in ("b", "B", "back"):
            with self.subTest(typed=typed):
                result = account_choice("pick", accounts(), "use", typed)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertEqual("", result.stdout.strip())

    def test_enter_takes_the_recommended_account(self) -> None:
        result = account_choice("pick", accounts(recommendation="wren"), "use", "")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("wren", result.stdout.strip())

    def test_the_footer_says_what_enter_takes(self) -> None:
        result = account_choice(
            "render", accounts(recommendation="wren"), "use", "Account"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("↵ use wren", result.stdout)
        self.assertIn("b back", result.stdout)

    def test_a_screen_with_nothing_to_recommend_says_what_enter_does(self) -> None:
        result = account_choice("render", accounts(), "use", "Account")
        self.assertIn("↵ back", result.stdout)
        self.assertIn("b back", result.stdout)
        taken = account_choice("pick", accounts(), "use", "")
        self.assertEqual(2, taken.returncode)

    def test_switching_a_running_session_never_defaults_to_a_change(self) -> None:
        result = account_choice(
            "pick", accounts(recommendation="wren"), "switch", ""
        )
        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout.strip())


# --------------------------------------------------------------- ruling 6


class AlignedColumnsTests(unittest.TestCase):
    """`primary: primary@example.com | max | ready | …` ragged-edged after the
    variable-width name."""

    def pipe_columns(self, line: str) -> tuple[int, ...]:
        found: list[int] = []
        width = 0
        for character in line:
            if character == "|":
                found.append(width)
            width += cells(character)
        return tuple(found)

    def test_the_account_screen_is_columns(self) -> None:
        payload = accounts(recommendation="primary")
        payload["choices"].append(
            {
                "alias": "a-very-long-alias-indeed",
                "email": "someone.with.a.long.address@example.com",
                "plan": "pro",
                "eligible": False,
                "state": "blocked: health expired",
            }
        )
        payload["choices"].append(
            {
                "alias": "東京アカウント",
                "email": "tokyo@example.jp",
                "plan": "max",
                "eligible": True,
                "state": "ready",
                "u5h": 0.5,
                "u7d": 0.5,
            }
        )
        result = account_choice("render", payload, "use", "Account")
        self.assertEqual(0, result.returncode, result.stderr)
        rows_shown = [
            line
            for line in result.stdout.splitlines()
            if line.startswith("   ") and "|" in line
        ]
        self.assertEqual(4, len(rows_shown), result.stdout)
        columns = {self.pipe_columns(line)[:3] for line in rows_shown}
        self.assertEqual(1, len(columns), rows_shown)

    def test_the_session_rows_are_columns_including_a_wide_name(self) -> None:
        items = (
            row("x", 1, display_model="Opus 5"),
            row("a much longer session name than that one", 2,
                display_model="GPT-5.6-Sol", provider="codex"),
            row("東京の作業セッション", 3, display_model="Fable 5"),
        )
        for lines in (picker_lines(*items), list_lines(*items)):
            columns = {self.pipe_columns(line) for line in lines}
            self.assertEqual(1, len(columns), lines)

    def test_padding_counts_columns_and_not_characters(self) -> None:
        self.assertEqual(4, cells("東京"))
        self.assertEqual("東京  ", render._pad("東京", 6))
        self.assertEqual("ab    ", render._pad("ab", 6))

    def test_the_cursor_picker_pads_in_columns_too(self) -> None:
        from sessionkit_tui import frame

        self.assertEqual("東京  ", frame._pad("東京", 6))
        parsed = parse_inventory(
            document(
                row("x", 1, display_model="Opus 5"),
                row("東京の作業", 2, display_model="Fable 5"),
            )
        )
        drawn = tui_rows.build_rows(parsed.sessions)
        widths = frame.detail_widths(drawn)
        self.assertGreaterEqual(widths.model, len("Fable 5"))


if __name__ == "__main__":
    unittest.main()
