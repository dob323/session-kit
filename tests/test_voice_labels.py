"""One vocabulary: the renderers may not spell a word a person reads.

`lib/sessionkit_inventory/labels.py` is `docs/voice.md` as code. These tests
hold three lines at once:

* no renderer contains a human label as a literal — the label module is the
  only place a word lives;
* what `sp list` and `sp detail` print is exactly what the label module says,
  asserted against the module rather than against a second hardcoded copy, so
  the strings the cohesion release fixed cannot drift back;
* the bash picker, which cannot import Python, still carries the same table.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import sys
import unittest

from tests.support import REPO


sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_inventory import labels  # noqa: E402
from sessionkit_inventory import render  # noqa: E402


RENDERERS = (
    REPO / "lib" / "sessionkit_inventory" / "render.py",
)
PICKER_RENDER = REPO / "lib" / "sh" / "shpool_login_render.sh"
NOW_MS = 1_800_000_000_000


def code_strings(path: Path) -> list[tuple[int, str]]:
    """Every string literal in a module except its docstrings.

    Comments and docstrings explain the words; only a literal the code can
    print is a second copy of one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            if ast.get_docstring(node, clean=False) is not None:
                docstrings.add(id(node.body[0].value))
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.append((node.lineno, node.value))
    return found


def banned_labels() -> dict[str, str]:
    """Every word the label module owns, mapped to the name that owns it."""
    owned: dict[str, str] = {}

    def remember(name: str, value: object) -> None:
        if isinstance(value, str) and len(value) >= 4:
            owned.setdefault(value, name)
        elif isinstance(value, (tuple, list)):
            for item in value:
                remember(name, item)
        elif isinstance(value, dict):
            for key, item in value.items():
                remember(name, key)
                remember(name, item)

    for name in dir(labels):
        if name.startswith("_") or name.isupper() is False:
            continue
        remember(name, getattr(labels, name))
    return owned


def row(
    shpool_id: str,
    *,
    number: int | None = 1,
    provider: str = "claude",
    availability: str = "ready",
    needs_you: bool = False,
    agent_status: str = "running",
    **extra: object,
) -> dict:
    item = {
        "row": number or 0,
        "terminal_number": number,
        "shpool_id": shpool_id,
        "shpool_id_raw": shpool_id,
        "availability": availability,
        "provider": provider,
        "display_provider": provider,
        "identity": {"uuid": f"uuid-{shpool_id}", "confidence": "exact"},
        "title": f"session {shpool_id}",
        "display_title": f"session {shpool_id}",
        "cwd": "/srv/project",
        "started_at_unix_ms": NOW_MS - 3_600_000,
        "process_age_seconds": 60,
        "recent_output_age_seconds": 30,
        "agent_status": agent_status,
        "needs_you": needs_you,
        "subagents": [],
        "active_subagent_count": 0,
        "account_alias": "acct",
        "provider_title_state": "ready",
    }
    item.update(extra)
    return item


def inventory(*rows: dict, **fields: object) -> dict:
    document = {
        "schema_version": 1,
        "generated_at": "2026-07-28T00:00:00Z",
        "source": "live",
        "stale": False,
        "warnings": [],
        "daemon_generation": {"pid": 7, "process_start_ticks": 70},
        "sessions": list(rows),
        "outside_agents": [],
    }
    document.update(fields)
    return document


def render_list(document: dict, columns: str = "200") -> list[str]:
    previous = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = columns
    try:
        return render.render_inventory(
            document, False, color_enabled=lambda: False
        ).splitlines()
    finally:
        if previous is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = previous


class LabelSourceTests(unittest.TestCase):
    def test_no_renderer_spells_a_word_the_label_module_owns(self) -> None:
        owned = banned_labels()
        offences = []
        for path in RENDERERS:
            for line, value in code_strings(path):
                owner = owned.get(value)
                if owner is not None:
                    offences.append(
                        f"{path.relative_to(REPO)}:{line} spells {value!r}; "
                        f"import labels.{owner} instead"
                    )
        self.assertEqual([], offences, "\n".join(offences))

    def test_a_renderer_may_not_hide_a_phrase_inside_a_longer_literal(self) -> None:
        phrases = {
            value: name
            for value, name in banned_labels().items()
            if " " in value
        }
        offences = []
        for path in RENDERERS:
            for line, value in code_strings(path):
                for phrase, owner in phrases.items():
                    if phrase in value:
                        offences.append(
                            f"{path.relative_to(REPO)}:{line} contains {phrase!r} "
                            f"(labels.{owner})"
                        )
        self.assertEqual([], offences, "\n".join(offences))

    def test_every_state_word_is_named(self) -> None:
        for word in labels.STATE_WORDS.values():
            self.assertIn(word, labels.SESSION_STATES)
        self.assertEqual(
            labels.NEEDS_YOU, labels.STATE_WORDS[labels.STATUS_NEEDS_YOUR_REPLY]
        )
        self.assertEqual(labels.WORKING, labels.STATE_WORDS[labels.STATUS_RUNNING])
        self.assertEqual(
            labels.WORKING, labels.STATE_WORDS[labels.STATUS_REPLY_OPTIONAL]
        )
        # `sp list` renames only the word that contradicted the term sheet.
        self.assertEqual(
            {labels.STATUS_NEEDS_YOUR_REPLY: labels.NEEDS_YOU},
            dict(labels.LIST_STATE_WORDS),
        )

    def test_the_bash_picker_carries_the_same_state_table(self) -> None:
        source = PICKER_RENDER.read_text(encoding="utf-8")
        match = re.search(r"STATE_WORDS = \{(.*?)\n\}", source, re.S)
        if match is None:
            self.skipTest("the picker no longer declares STATE_WORDS")
        table = dict(re.findall(r'"([^"]+)": "([^"]+)"', match.group(1)))
        self.assertEqual(dict(labels.STATE_WORDS), table)

    def test_the_bash_picker_uses_the_same_group_headings(self) -> None:
        source = (REPO / "lib" / "sh" / "shpool_login_view.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'"{labels.GROUP_READY}"', source)
        self.assertIn(f'"{labels.GROUP_OPEN_ELSEWHERE}"', source)


class LabelBehaviourTests(unittest.TestCase):
    def test_provider_names_and_the_placeholder(self) -> None:
        self.assertEqual("Claude", labels.provider_name(labels.PROVIDER_CLAUDE))
        self.assertEqual("Codex", labels.provider_name(labels.PROVIDER_CODEX))
        self.assertEqual("Shell", labels.provider_name(labels.PROVIDER_SHELL))
        self.assertEqual("Unknown", labels.provider_name(labels.PROVIDER_UNKNOWN))
        self.assertEqual("Claude", labels.provider_name("CLAUDE"))
        self.assertEqual(labels.MISSING, labels.provider_name(""))
        self.assertEqual(labels.MISSING, labels.provider_name(None))

    def test_where_a_session_can_be_opened(self) -> None:
        self.assertEqual(
            labels.WHERE_READY, labels.where_word(labels.AVAILABILITY_READY)
        )
        self.assertEqual(
            labels.WHERE_OPEN_ELSEWHERE,
            labels.where_word(labels.AVAILABILITY_ATTACHED),
        )
        self.assertEqual(labels.MISSING, labels.where_word(""))
        self.assertEqual(
            labels.GROUP_READY, labels.group_heading(labels.AVAILABILITY_READY)
        )
        self.assertEqual(
            labels.GROUP_OPEN_ELSEWHERE,
            labels.group_heading(labels.AVAILABILITY_ATTACHED),
        )

    def test_state_precedence(self) -> None:
        state = lambda item: labels.session_state(item, stall_seconds=2700)  # noqa: E731
        self.assertEqual(
            labels.SETUP_INCOMPLETE,
            state({"setup_incomplete": True, "needs_you": True}),
        )
        self.assertEqual(labels.NEEDS_YOU, state({"needs_you": True}))
        self.assertEqual(
            labels.NEEDS_YOU, state({"agent_status": labels.STATUS_NEEDS_YOUR_REPLY})
        )
        self.assertEqual(
            labels.WORKING, state({"agent_status": labels.STATUS_RUNNING})
        )
        self.assertEqual(
            labels.WORKING, state({"agent_status": labels.STATUS_REPLY_OPTIONAL})
        )
        self.assertEqual(
            labels.WORKING,
            state({"agent_status": labels.STATUS_RUNNING, "reply_optional": True}),
        )
        # An optional reply is still the model working, however long it is quiet.
        self.assertEqual(
            labels.WORKING,
            state(
                {
                    "agent_status": labels.STATUS_REPLY_OPTIONAL,
                    "recent_output_age_seconds": 99_999,
                }
            ),
        )
        # A session quiet for the whole stall window has stopped working,
        # and what is left is the person. There is no third word for it.
        self.assertEqual(
            labels.WAITING_ON_YOU,
            state(
                {
                    "agent_status": labels.STATUS_RUNNING,
                    "recent_output_age_seconds": 2700,
                }
            ),
        )
        # The two vocabularies for one situation. Claude's poll parks a
        # session at its prompt as `needs your reply`; Codex's rollout parks
        # the same session as `idle`. One situation, one word.
        self.assertEqual(labels.WAITING_ON_YOU, state({"agent_status": labels.STATUS_IDLE}))
        self.assertEqual(
            state({"agent_status": labels.STATUS_NEEDS_YOUR_REPLY}),
            state({"agent_status": labels.STATUS_IDLE}),
        )
        self.assertEqual(
            labels.WORKING,
            state(
                {
                    "agent_status": labels.STATUS_RUNNING,
                    "recent_output_age_seconds": 2699,
                }
            ),
        )
        self.assertEqual(
            labels.STATUS_UNAVAILABLE,
            state({"agent_status": labels.STATUS_STATE_UNAVAILABLE}),
        )
        self.assertEqual(labels.STATUS_UNAVAILABLE, state({}))
        # A provider that exited is a person's turn: only they can restore or
        # close the shell it left behind. It used to print the collector's own
        # `provider exited` straight onto the screen.
        self.assertEqual(
            labels.WAITING_ON_YOU,
            state({"agent_status": labels.STATUS_PROVIDER_EXITED}),
        )
        # And a word this file has never seen is the kit failing to read a
        # state, not a state. Nothing reaches a row verbatim.
        for unknown in ("busy", "brand-new-vendor-word", "unknown"):
            self.assertEqual(
                labels.STATUS_UNAVAILABLE, state({"agent_status": unknown})
            )

    def test_age_phrases(self) -> None:
        self.assertEqual("0m", labels.duration(0))
        self.assertEqual("59m", labels.duration(59 * 60))
        self.assertEqual("1h 0m", labels.duration(3600))
        self.assertEqual("1h 1m", labels.duration(3661))
        self.assertEqual("1d 1h", labels.duration(90_061))
        self.assertEqual("now", labels.brief_age(59))
        self.assertEqual("1m", labels.brief_age(60))

        self.assertEqual(labels.JUST_NOW, labels.relative_time(NOW_MS, NOW_MS))
        self.assertEqual("1 min ago", labels.relative_time(NOW_MS - 60_000, NOW_MS))
        self.assertEqual("2 hr ago", labels.relative_time(NOW_MS - 7_200_000, NOW_MS))
        self.assertEqual("1 day ago", labels.relative_time(NOW_MS - 86_400_000, NOW_MS))
        self.assertEqual(
            "2 days ago", labels.relative_time(NOW_MS - 172_800_000, NOW_MS)
        )

        self.assertEqual(f"{labels.NEEDS_YOU} under 1 min", labels.waiting_phrase(0))
        self.assertEqual(f"{labels.NEEDS_YOU} 12 min", labels.waiting_phrase(12 * 60))
        self.assertEqual(f"{labels.NEEDS_YOU} 1 hr", labels.waiting_phrase(3600))
        self.assertEqual(f"{labels.NEEDS_YOU} 2 days", labels.waiting_phrase(2 * 86400))
        self.assertEqual(
            f"{labels.NEEDS_YOU} 12 min",
            labels.waiting_since(NOW_MS - 12 * 60_000, NOW_MS),
        )
        self.assertTrue(
            labels.waiting_since(NOW_MS - 40 * 86_400_000, NOW_MS).startswith(
                f"{labels.NEEDS_YOU} since "
            )
        )

    def test_counts_never_print_a_bare_plural(self) -> None:
        self.assertEqual("1 session: 1 ready, 0 open elsewhere",
                         labels.session_count(1, 1, 0))
        self.assertEqual("3 sessions: 2 ready, 1 open elsewhere",
                         labels.session_count(3, 2, 1))
        self.assertEqual("1 subagent", labels.subagent_detail(1))
        self.assertEqual("2 subagents", labels.subagent_detail(2))
        self.assertEqual("Accounts: none.", labels.empty_state("Accounts"))

    def test_refusal_and_cancel_grammar(self) -> None:
        self.assertEqual("Nothing changed.", labels.CANCEL)
        self.assertEqual(
            "There is no session 20 on this screen. Numbers shown here work.",
            labels.no_such_session(20),
        )
        self.assertTrue(labels.NO_MATCHING_SESSION.startswith(labels.ERROR_PREFIX))
        self.assertEqual(
            "session-kit: no session matches that selector",
            labels.NO_MATCHING_SESSION,
        )
        self.assertEqual("session-kit: shpool is unavailable",
                         labels.error("shpool is unavailable"))


class RenderedOutputTests(unittest.TestCase):
    """What the screens print is what the label module says, not a copy."""

    def test_list_headings_counts_and_hints(self) -> None:
        document = inventory(
            row("aaa", number=1, needs_you=True),
            row("bbb", number=2, provider="codex", availability="attached"),
        )
        lines = render_list(document)
        self.assertEqual(f"  {labels.session_count(2, 1, 1)}", lines[0])
        self.assertIn(f"  {labels.GROUP_READY}", lines)
        self.assertIn(f"  {labels.GROUP_OPEN_ELSEWHERE}", lines)
        self.assertTrue(any("| CLD | acct |" in line for line in lines))
        self.assertTrue(any("| CDX | acct |" in line for line in lines))
        self.assertEqual([f"  {hint}" for hint in labels.LIST_HINTS], lines[-2:])

    def test_both_a_waiting_row_and_a_quiet_row_say_waiting_on_you(self) -> None:
        document = inventory(
            row("aaa", number=1, needs_you=True),
            row("bbb", number=2, recent_output_age_seconds=99_999),
        )
        lines = render_list(document)
        waiting = next(line for line in lines if "session aaa" in line)
        self.assertIn(f"{labels.SEPARATOR}{labels.WAITING_ON_YOU}", waiting)
        quiet = next(line for line in lines if "session bbb" in line)
        self.assertIn(labels.WAITING_ON_YOU, quiet)
        self.assertNotIn("quiet", quiet)
        self.assertNotIn("idle", quiet)
        self.assertIn(labels.last_active(99_999), quiet)

    def test_a_row_carries_one_time_and_names_it_the_same_way_every_time(self) -> None:
        document = inventory(
            row("aaa", number=1, needs_you=True, recent_output_age_seconds=11_340),
            row("bbb", number=2, recent_output_age_seconds=30),
        )
        rows_shown = [
            line for line in render_list(document) if "| CLD |" in line
        ]
        self.assertEqual(2, len(rows_shown))
        for line in rows_shown:
            self.assertEqual(1, line.count(labels.LAST_ACTIVE), line)
            self.assertNotIn("process age", line)
            self.assertNotIn("recent output", line)
            self.assertNotIn("opened", line)
        # Same starting column on both rows: that is what "aligned" means.
        columns = {line.index(labels.LAST_ACTIVE) for line in rows_shown}
        self.assertEqual(1, len(columns), rows_shown)

    def test_an_empty_list_and_a_stale_list_use_the_owned_strings(self) -> None:
        self.assertIn(f"  {labels.SESSIONS_EMPTY}", render_list(inventory()))
        stale = render_list(
            inventory(row("aaa"), source="cache", stale=True)
        )
        self.assertEqual(f"  {labels.stale_warning('cache')}", stale[0])

    def test_machine_rows_are_counted_without_names_and_columns_are_bounded(self) -> None:
        person = row(
            "person",
            account_alias="account-alias-that-is-far-too-long",
            model="claude-model-identifier-that-is-far-too-long",
        )
        machine = row("secret-worker", number=2, provider="codex", origin="machine")
        lines = render_list(inventory(person, machine), columns="80")
        text = "\n".join(lines)
        self.assertNotIn("secret-worker", text)
        self.assertIn("1 machine session", text)
        person_line = next(line for line in lines if "| CLD |" in line)
        self.assertIn("| CLD |", person_line)
        self.assertIn("…", person_line)
        self.assertNotIn("account-alias-that-is-far-too-long", person_line)
        self.assertNotIn("claude-model-identifier-that-is-far-too-long", person_line)
        self.assertLessEqual(render._display_width(person_line), 79)

    def test_an_unnumbered_row_shows_the_unnumbered_mark(self) -> None:
        document = inventory(row("aaa", number=1), row("bbb", number=None))
        rows = [line for line in render_list(document) if "session bbb" in line]
        self.assertEqual(1, len(rows))
        self.assertIn(f" {labels.UNNUMBERED}  ", rows[0])

    def test_outside_the_kit_is_one_heading(self) -> None:
        document = inventory(
            row("aaa"),
            outside_agents=[
                {
                    "provider": "claude",
                    "title": "outside",
                    "agent_status": labels.STATUS_NEEDS_YOUR_REPLY,
                    "subagents": [],
                    "active_subagent_count": 0,
                }
            ],
        )
        lines = render_list(document)
        self.assertIn(f"  {labels.GROUP_OUTSIDE_THE_KIT}", lines)
        detail = next(line for line in lines if labels.provider_name("claude") in line
                      and labels.NEEDS_YOU in line)
        self.assertIn(
            f"{labels.provider_name('claude')}{labels.SEPARATOR}{labels.NEEDS_YOU}",
            detail,
        )

    def test_detail_fields_and_values_are_owned(self) -> None:
        document = inventory(
            row(
                "aaa",
                number=9,
                needs_you=True,
                worktree={"branch": "track/b-renderer"},
                subagents=[{"status": "running"}],
            )
        )
        text = render.render_detail(
            document, "9", home_factory=lambda: "/srv", now_ms=NOW_MS
        )
        fields = {
            line.strip().split("  ", 1)[0]: line.strip().split("  ", 1)[1].strip()
            for line in text.splitlines()[1:]
        }
        self.assertEqual(f"  {labels.DETAIL_HEADING}", text.splitlines()[0])
        self.assertEqual("9", fields[labels.DETAIL_SESSION])
        self.assertEqual(labels.provider_name("claude"), fields[labels.DETAIL_PROVIDER])
        self.assertEqual(labels.NEEDS_YOU, fields[labels.DETAIL_STATE])
        self.assertEqual(labels.WHERE_READY, fields[labels.DETAIL_WHERE])
        self.assertEqual(labels.CONVERSATION_EXACT, fields[labels.DETAIL_CONVERSATION])
        self.assertEqual(labels.TITLE_STATE_SHOWING, fields[labels.DETAIL_TITLE_STATE])
        self.assertIn(labels.DETAIL_OPENED, fields)

    def test_detail_lists_each_aged_child_with_its_age_on_the_same_row(self) -> None:
        document = inventory(
            row(
                "aaa",
                number=9,
                aged_children=[
                    {"kind": "shell", "title": "bash", "age_seconds": 7860},
                    {
                        "kind": "worker",
                        "provider": "claude",
                        "title": "Verifier",
                        "age_seconds": 3661,
                    },
                    {"kind": "shell", "title": "young", "age_seconds": 3599},
                ],
            )
        )

        text = render.render_detail(
            document, "9", home_factory=lambda: "/srv", now_ms=NOW_MS
        )
        shell_row = next(
            line for line in text.splitlines() if labels.DETAIL_CHILD_SHELL in line
        )
        worker_row = next(
            line for line in text.splitlines() if labels.DETAIL_CHILD_WORKER in line
        )
        self.assertIn(f"bash{labels.SEPARATOR}2h 11m", shell_row)
        self.assertIn(f"Verifier{labels.SEPARATOR}1h 1m", worker_row)
        self.assertNotIn("young", text)

    def test_detail_of_an_unnumbered_session_and_a_missing_one(self) -> None:
        document = inventory(row("aaa", number=None, row=1))
        text = render.render_detail(
            document, "1", home_factory=lambda: "/srv", now_ms=NOW_MS
        )
        self.assertIn(labels.NOT_NUMBERED, text)
        self.assertIn(labels.NONE, text)
        self.assertEqual(
            labels.NO_MATCHING_SESSION,
            render.render_detail(
                document, "404", home_factory=lambda: "/srv", now_ms=NOW_MS
            ),
        )


if __name__ == "__main__":
    unittest.main()
