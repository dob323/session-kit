"""The invariant: no human-readable surface ever prints a session identifier.

Identifiers live in 0600 state files, machine-only JSON modes, proofs, and
command arguments. They are never displayed, not in a row, not in a
confirmation, not in an error, not behind a flag.

This is the enforcement mechanism, not a collection of per-surface checks.
Every human surface is registered in ``SURFACES`` below and rendered against
one fixture whose identifiers are deliberately distinctive hex. A surface that
leaks fails here; a surface nobody registered fails ``test_every_human_surface_is_registered``,
so a new screen cannot join the kit without someone deciding, in writing,
which side of the line it is on.

Whole identifiers are not the whole test. A truncated identifier is still an
identifier, the title fallback used to render ``Codex a1b2c3d4``, so the
scan flags any eight-hex-character prefix of a fixture identifier, which is
why the fixture UUIDs below start with distinctive hex rather than zeros.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import REPO, run


CORE = REPO / "lib" / "session_inventory.py"
SP = REPO / "bin" / "sp"
STATUS = REPO / "bin" / "shpool_status"

# Distinctive hex, so an eight-character prefix of any of these is unmistakable
# in output and cannot collide with a timestamp, a count, or a colour name.
CLAUDE_UUID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
CODEX_UUID = "f9e8d7c6-b5a4-4938-a271-6f5e4d3c2b1a"
SHELL_UUID = "c0ffee11-dead-4bee-9fad-1234567890ab"
CLAUDE_SHPOOL = "s20260809-121212-7"
CODEX_SHPOOL = "s20260809-131313-8"
SHELL_SHPOOL = "s20260809-141414-9"

# Every identifier the fixture contains, in the form it would leak.
SECRETS = (
    CLAUDE_UUID,
    CODEX_UUID,
    SHELL_UUID,
    CLAUDE_SHPOOL,
    CODEX_SHPOOL,
    SHELL_SHPOOL,
    f"claude:{CLAUDE_UUID}",
    f"codex:{CODEX_UUID}",
)
MIN_PREFIX = 8

# The channels that legitimately carry an identifier, each one a value handed
# to a caller rather than a line shown to a person. Recorded here so the
# decision is written down and a reviewer can check it, not rediscovered.
MACHINE_CHANNELS = {
    "lib/sh/sp_core.sh": "history_files returns journal paths to its caller",
    "lib/sh/sp_provider_bounce.sh": "writes the pending uuid into a 0600 state file",
    "lib/sh/sp_sessions.sh": (
        "start_new under SESSION_KIT_BACKGROUND=1, and restore_exact, return "
        "the new session id to the script that asked for it"
    ),
    "bin/shpool_status": "--json, --strict-json, --guard-json, --lookup",
    "lib/session_inventory.py": "every JSON subcommand, including lookup",
}


def leaks(text: str) -> list[str]:
    """Every fixture identifier, or eight-character prefix of one, in `text`."""
    found: list[str] = []
    for secret in SECRETS:
        # Compare on the identifier's own characters, so a UUID broken across
        # a line or a column boundary still counts as printed.
        for candidate in (secret, secret.replace("-", "")):
            if len(candidate) < MIN_PREFIX:
                continue
            for start in range(0, len(candidate) - MIN_PREFIX + 1):
                window = candidate[start : start + MIN_PREFIX]
                # Only hex-ish windows are identifier-shaped; a window that is
                # all digits could be an innocent timestamp.
                if re.fullmatch(r"[0-9]+", window):
                    continue
                if window in text:
                    found.append(f"{secret} (via {window!r})")
                    break
            else:
                continue
            break
    return found


def session(
    *,
    number: int,
    provider: str,
    uuid: str | None,
    shpool_id: str,
    title: str,
    availability: str = "ready",
    status: str = "idle",
    aged_children: list[dict] | None = None,
) -> dict:
    identity = (
        {
            "uuid": uuid,
            "confidence": "exact",
            "pid": 2000 + number,
            "process_start_ticks": 20_000 + number,
        }
        if uuid
        else {"uuid": None, "confidence": "none"}
    )
    return {
        "row": number,
        "terminal_number": number,
        "shpool_id": shpool_id,
        "shpool_id_raw": shpool_id,
        "display_shpool_id": shpool_id,
        "mutation_allowed": True,
        "mutation_rejection_reason": None,
        "shpool_status": "Disconnected" if availability == "ready" else "Attached",
        "shpool_shell": {"pid": 1000 + number, "process_start_ticks": 10_000 + number},
        "started_at_unix_ms": 1_700_000_000_000 + number,
        "availability": availability,
        "provider": provider,
        "display_provider": provider,
        "identity": identity,
        "title": title,
        "display_title": title,
        "native_title": "",
        "provider_title_state": "ready",
        "agent_status": status,
        "cwd": "/srv/project",
        "needs_you": status == "needs your reply",
        "subagents": [],
        "aged_children": list(aged_children or ()),
        "setup_incomplete": False,
        "color": "blue",
        "display_color": "blue",
    }


def fixture_inventory() -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-08-09T00:00:00Z",
        "source": "live",
        "stale": False,
        "warnings": [],
        "daemon_generation": {"boot_id": "fixture", "pid": 77, "process_start_ticks": 770},
        "sessions": [
            session(
                number=2,
                provider="claude",
                uuid=CLAUDE_UUID,
                shpool_id=CLAUDE_SHPOOL,
                title="Fleet rebuild",
                aged_children=[
                    {
                        "kind": "worker",
                        "provider": "codex",
                        "title": "Verifier",
                        "age_seconds": 2 * 3600 + 11 * 60,
                        # Machine evidence may remain in the snapshot; detail
                        # renders only the safe title and age.
                        "pid": 98765,
                        "uuid": CODEX_UUID,
                    }
                ],
            ),
            session(
                number=3,
                provider="codex",
                uuid=CODEX_UUID,
                shpool_id=CODEX_SHPOOL,
                title="Ledger work",
                availability="attached",
                status="needs your reply",
            ),
            # No title and no alias: this is the row whose label used to fall
            # back to a truncated UUID.
            session(
                number=4,
                provider="codex",
                uuid=SHELL_UUID,
                shpool_id=SHELL_SHPOOL,
                title="",
            ),
        ],
        "outside_agents": [
            {
                "provider": "claude",
                "identity": {"uuid": CLAUDE_UUID, "confidence": "exact"},
                "title": "",
                "agent_status": "working",
                "subagents": [{"provider": "claude", "uuid": CODEX_UUID}],
                "cwd": "/srv/project",
            }
        ],
    }


class HumanOutputHasNoIdentifiersTests(unittest.TestCase):
    """One fixture, every registered human surface, one invariant."""

    # name -> (argv builder, whether a nonzero exit is acceptable)
    SURFACES = (
        "render",
        "render_rows",
        "detail_by_number",
        "detail_unknown",
        "waiting_count",
        "recovery_manifest_display",
        "sp_usage",
        "status_usage",
        "sp_recover",
        "sp_prune",
        "sp_color_reconcile",
        "picker_needs_you",
    )
    SURFACE_TESTS = {
        "render": "HumanOutputHasNoIdentifiersTests.test_the_dashboard_and_its_rows_name_sessions_without_identifiers",
        "render_rows": "HumanOutputHasNoIdentifiersTests.test_the_dashboard_and_its_rows_name_sessions_without_identifiers",
        "detail_by_number": "HumanOutputHasNoIdentifiersTests.test_detail_shows_a_person_everything_except_the_identifiers",
        "detail_unknown": "HumanOutputHasNoIdentifiersTests.test_a_selector_that_matches_nothing_says_so_without_echoing_state",
        "waiting_count": "HumanOutputHasNoIdentifiersTests.test_the_waiting_count_is_a_number",
        "recovery_manifest_display": "HumanOutputHasNoIdentifiersTests.test_the_recovery_display_fields_carry_no_identifier",
        "sp_usage": "HumanOutputHasNoIdentifiersTests.test_the_command_help_teaches_selectors_without_printing_one",
        "status_usage": "HumanOutputHasNoIdentifiersTests.test_the_command_help_teaches_selectors_without_printing_one",
        "sp_recover": "ShellRendererTests.test_the_recovery_list_names_conversations_not_identifiers",
        "sp_prune": "ShellRendererTests.test_the_prune_candidate_list_names_candidates",
        "sp_color_reconcile": "ShellRendererTests.test_color_reconcile_reports_counts_not_conversations",
        "picker_needs_you": "HumanOutputHasNoIdentifiersTests.test_picker_needs_you_redacts_question_and_stall_identifiers",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(
            prefix=".human-output-", dir=REPO
        )
        base = Path(cls.temporary.name)
        cls.snapshot = base / "inventory.json"
        cls.snapshot.write_text(
            json.dumps(fixture_inventory()), encoding="utf-8"
        )
        cls.home = base / "home"
        cls.home.mkdir(mode=0o700)
        cls.state = base / "state"
        cls.state.mkdir(mode=0o700)
        # Commands refuse to run without a session manager binary, and a CI
        # runner has none. Nothing below asks the manager a question, so a
        # stand-in that answers nothing is the whole dependency.
        cls.shpool = base / "shpool"
        cls.shpool.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        cls.shpool.chmod(0o755)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def _core(self, *argv: str) -> str:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": os.fspath(self.home),
                "SESSION_KIT_STATE_DIR": os.fspath(self.state),
                "SESSION_KIT_INPUT_SNAPSHOT": os.fspath(self.snapshot),
                "PYTHONPATH": os.fspath(REPO / "lib"),
                "SESSION_KIT_CODEX_AUTOTITLE": "0",
                "SESSION_KIT_AUTO_NAME": "0",
            }
        )
        completed = subprocess.run(
            [sys.executable, os.fspath(CORE), *argv],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return completed.stdout + completed.stderr

    def _assert_clean(self, surface: str, text: str) -> None:
        found = leaks(text)
        self.assertEqual(
            [],
            found,
            f"{surface} printed session identifiers: {found}\n--- output ---\n{text}",
        )

    def test_the_dashboard_and_its_rows_name_sessions_without_identifiers(
        self,
    ) -> None:
        for surface, argv in (
            ("render", ("render",)),
            ("render_rows", ("render", "--rows")),
        ):
            with self.subTest(surface=surface):
                text = self._core(*argv)
                self._assert_clean(surface, text)
                # The rows are still useful: numbers and titles survive.
                self.assertIn("Fleet rebuild", text)

    def test_detail_shows_a_person_everything_except_the_identifiers(self) -> None:
        text = self._core("detail", "2")
        self._assert_clean("detail_by_number", text)
        self.assertIn("Fleet rebuild", text)
        self.assertIn("Claude", text)
        self.assertIn("Opened", text)
        self.assertIn("Verifier", text)
        self.assertIn("2h 11m", text)
        self.assertRegex(text, r"Opened\s+Nov 14, 2023 at")
        # Last response and Waiting since came from the retired attention
        # queue; a detail view now renders from the inventory row alone.
        self.assertNotIn("Last response", text)
        self.assertNotIn("Waiting since", text)
        waiting = self._core("detail", "3")
        self._assert_clean("detail_waiting", waiting)
        # The collector records the provider's own words; the screen speaks
        # the kit's one vocabulary.
        self.assertIn("needs you", waiting)
        self.assertNotIn("needs your reply", waiting)
        # `sp detail` used to be `lookup`, which is the machine mode and does
        # carry them. Prove the two are genuinely different surfaces.
        machine = self._core("lookup", "2")
        self.assertIn(CLAUDE_UUID, machine)

    def test_a_selector_that_matches_nothing_says_so_without_echoing_state(
        self,
    ) -> None:
        self._assert_clean("detail_unknown", self._core("detail", CLAUDE_SHPOOL))

    def test_the_waiting_count_is_a_number(self) -> None:
        self._assert_clean("waiting_count", self._core("waiting-count"))

    def test_the_recovery_display_fields_carry_no_identifier(self) -> None:
        library = os.fspath(REPO / "lib")
        if library not in sys.path:
            sys.path.insert(0, library)
        import session_inventory as core

        manifest = core.recovery_manifest(fixture_inventory())
        # Only the fields a screen prints: the manifest itself is machine JSON.
        human = "\n".join(
            str(entry.get("title", ""))
            for entry in manifest.get("sessions", {}).values()
        )
        self._assert_clean("recovery_manifest_display", human)

    def test_the_command_help_teaches_selectors_without_printing_one(self) -> None:
        for surface, argv in (
            ("sp_usage", [os.fspath(SP), "help"]),
            ("status_usage", [os.fspath(STATUS), "--bogus"]),
        ):
            with self.subTest(surface=surface):
                completed = subprocess.run(
                    argv,
                    env={**os.environ, "HOME": os.fspath(self.home)},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self._assert_clean(surface, completed.stdout + completed.stderr)

    def test_picker_needs_you_redacts_question_and_stall_identifiers(self) -> None:
        """Drive the real PTY screen; a copied renderer cannot prove S7."""
        from tests.test_login import LoginFixture, inventory, row, run_pty

        first = row(SHELL_SHPOOL, number=1, provider="shell")
        first["title"] = first["display_title"] = "Import monitor"
        second = row("ordinary", number=2, provider="codex")
        second["identity"]["uuid"] = CODEX_UUID
        second["title"] = second["display_title"] = "Release monitor"
        fixture = LoginFixture(inventory(first, second))
        fleet = fixture.home / ".local" / "state" / "fleet"
        inbox = fleet / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "question.json").write_text(
            json.dumps(
                {
                    "state": "open",
                    "asked_at": time.time() - 300,
                    "header": f"Decision for {CODEX_UUID[:8]}",
                    "session": {"uuid": CLAUDE_UUID, "title": CLAUDE_UUID[:8]},
                }
            ),
            encoding="utf-8",
        )
        (inbox / "ordinary-numbers.json").write_text(
            json.dumps(
                {
                    "state": "open",
                    "asked_at": time.time() - 120,
                    "header": "Backfill 20260813 decaface",
                    "session": {"uuid": "", "title": None},
                }
            ),
            encoding="utf-8",
        )
        (fleet / "stalls.json").write_text(
            json.dumps(
                {
                    "generated_at": time.time(),
                    "stalled": [
                        {
                            "key": SHELL_SHPOOL,
                            "reason": SHELL_UUID[:8],
                            "since": time.time() - 600,
                        },
                        {
                            "key": CODEX_UUID,
                            "reason": "silent",
                            "since": time.time() - 900,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        try:
            code, output = run_pty(fixture, b"a\n\nq\n", columns=140)
            self.assertEqual(0, code)
            self._assert_clean("picker_needs_you", output)
            self.assertIn("Question · Decision for", output)
            self.assertIn("Question · Decision for Decision · a session", output)
            self.assertIn("Question · Backfill 20260813 decaface · a session", output)
            self.assertNotIn("· None ·", output)
            self.assertIn(
                "Stalled · Import monitor · Managed shell · attention overdue",
                output,
            )
            self.assertIn("Stalled · Release monitor · Codex · response overdue", output)
        finally:
            fixture.close()

    def test_every_human_surface_is_registered(self) -> None:
        """A new human surface joins this scan or fails this test.

        The registry is the point: adding a screen without deciding whether it
        prints identifiers should be impossible to do quietly. `sp` verbs and
        `shpool_status` modes are the two doors a person comes through.
        """
        covered = set(self.SURFACES)
        self.assertEqual(covered, set(self.SURFACE_TESTS))
        for surface, qualified in self.SURFACE_TESTS.items():
            class_name, method_name = qualified.split(".", 1)
            owner = globals().get(class_name)
            self.assertIsNotNone(owner, f"{surface}: missing test class {class_name}")
            self.assertTrue(
                hasattr(owner, method_name),
                f"{surface}: missing test {qualified}",
            )

        # The `sp` verbs that print to a person, and the mode each one is.
        sp_source = (REPO / "bin" / "sp").read_text(encoding="utf-8")
        human_verbs = {
            "list",
            "detail",
            "health",
            "recover",
            "prune",
            "find",
            "history",
        }
        for verb in human_verbs:
            self.assertIn(f"  sp {verb}", sp_source, f"sp {verb} left the usage text")

        # And the machine modes, which are allowed to carry identifiers.
        status_source = (REPO / "bin" / "shpool_status").read_text(encoding="utf-8")
        for machine_mode in ("--json", "--strict-json", "--guard-json", "--lookup"):
            self.assertIn(machine_mode, status_source)

        # Every file granted a machine channel still has to exist; a rename
        # that quietly drops one of these notes is itself a review event.
        for path in MACHINE_CHANNELS:
            self.assertTrue((REPO / path).is_file(), path)

    def test_the_invariant_catches_an_injected_leaking_row(self) -> None:
        """Prove the scanner fails, rather than only proving fixtures pass."""
        original = self.snapshot.read_bytes()
        leaking = fixture_inventory()
        leaking["sessions"][0]["title"] = f"Leak {CLAUDE_UUID}"
        leaking["sessions"][0]["display_title"] = f"Leak {CLAUDE_UUID}"
        try:
            self.snapshot.write_text(json.dumps(leaking), encoding="utf-8")
            with self.assertRaises(AssertionError):
                self._assert_clean("injected_leak", self._core("render"))
        finally:
            self.snapshot.write_bytes(original)


class ShellRendererTests(unittest.TestCase):
    """The renderers `sp` embeds, run as they ship.

    `sp recover`, `sp prune` and `sp color reconcile` each format their screen
    inside a python block in `lib/sh/sp_commands.sh`. Extracting the block and
    running it is the only way to test the code that actually reaches a
    terminal, a rewritten copy in the test would prove nothing about it.
    """

    SOURCE = REPO / "lib" / "sh" / "sp_commands.sh"

    def _block(self, marker: str, opener: str = "<<'PY'", closer: str = "\nPY\n") -> str:
        text = self.SOURCE.read_text(encoding="utf-8")
        start = text.index(marker)
        body_start = text.index(opener, start) + len(opener)
        return text[body_start : text.index(closer, body_start)]

    def _run(self, program: str, *argv: str, stdin: str = "") -> str:
        completed = subprocess.run(
            [sys.executable, "-c", program, *argv],
            text=True,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return completed.stdout + completed.stderr

    def _assert_clean(self, surface: str, text: str) -> None:
        found = leaks(text)
        self.assertEqual(
            [],
            found,
            f"{surface} printed session identifiers: {found}\n--- output ---\n{text}",
        )

    def test_the_recovery_list_names_conversations_not_identifiers(self) -> None:
        """The closed list is a screen, so it is driven as one.

        `sp recover` now merges two sources, the conversations somebody closed
        on purpose and the ones a crash took, and the merge itself carries
        identifiers so a restore can use them. What must never carry one is the
        list a person reads, so the whole command is run and its output judged.
        """
        from tests.test_commands import CommandFixture

        fixture = CommandFixture()
        try:
            manifest = fixture.state / "recovery-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "sessions": {
                            CODEX_SHPOOL: {
                                "provider": "codex",
                                "title": "",
                                "uuid": CODEX_UUID,
                                "cwd": "/srv/project",
                                "crashed_at_unix_ms": 1_000,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manifest.chmod(0o600)
            # The ledger itself, not a stub: `sp recover` reads the one
            # projection now, and that reads the file.
            ledger = fixture.home / ".local" / "share" / "session-kit"
            ledger.mkdir(parents=True, exist_ok=True)
            (ledger / "closed-sessions.jsonl").write_text(
                json.dumps(
                    {
                        "provider": "claude",
                        "uuid": CLAUDE_UUID,
                        "title": "Fleet rebuild",
                        "title_source": "alias",
                        "cwd": "/srv/project",
                        "closed_at_unix_ms": 2_000,
                        "origin": "human",
                        "shpool_id": "",
                        "account_alias": "",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            transcripts = fixture.home / ".claude" / "projects" / "-srv-project"
            transcripts.mkdir(parents=True, exist_ok=True)
            (transcripts / f"{CLAUDE_UUID}.jsonl").write_text(
                '{"type":"user"}\n', encoding="utf-8"
            )
            env = fixture.env()
            shown = run([SP, "recover"], env=env)
            output = shown.stdout + shown.stderr
            self._assert_clean("sp_recover", output)
            self.assertIn("Fleet rebuild", output)
            # A crashed session with no title is still named, never blank.
            self.assertIn("Codex", output)
        finally:
            fixture.close()

    def test_the_prune_candidate_list_names_candidates(self) -> None:
        program = self._block("printf 'Idle and empty for")
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, dir=REPO, prefix=".human-output-"
        ) as handle:
            json.dump(
                {
                    "candidates": [
                        {"shpool_id": CLAUDE_SHPOOL, "title": "Fleet rebuild"},
                        {"shpool_id": CODEX_SHPOOL, "title": ""},
                    ]
                },
                handle,
            )
            candidates = handle.name
        try:
            output = self._run(program, candidates)
            self._assert_clean("sp_prune", output)
            self.assertIn("Fleet rebuild", output)
            # A candidate with no title is still named, never left blank.
            self.assertIn("empty shell", output)
        finally:
            Path(candidates).unlink()

    def test_color_reconcile_reports_counts_not_conversations(self) -> None:
        text = self.SOURCE.read_text(encoding="utf-8")
        # Anchor on the block that actually prints this summary. Another command
        # gained an identically shaped `payload | python3 -c '` block ahead of
        # this one, and searching from the top of the file then extracted that
        # program instead -- which failed to compile and looked like a broken
        # renderer rather than a test looking in the wrong place.
        marker = text.index("Recolored")
        start = text.rindex("printf '%s' \"$payload\" | python3 -c '", 0, marker)
        body_start = text.index("'", text.index("python3 -c '", start) + 11) + 1
        program = text[body_start : text.index("\n'\n", body_start)]
        output = self._run(
            program,
            stdin=json.dumps(
                {
                    "moved": {
                        f"claude:{CLAUDE_UUID}": "blue",
                        f"codex:{CODEX_UUID}": "lime",
                    },
                    "dropped": [],
                }
            ),
        )
        self._assert_clean("sp_color_reconcile", output)
        self.assertIn("Recolored 1 Claude session: blue", output)
        self.assertIn("Recolored 1 Codex session: lime", output)
        self.assertNotIn("session(s)", output)


class NoRevealFlagTests(unittest.TestCase):
    """There is no way to ask Session Kit to print an identifier."""

    def test_no_source_file_offers_an_id_reveal_switch(self) -> None:
        banned = ("SESSION_KIT_SHOW_IDS", "sk_show_ids", "--show-ids", "--reveal-ids")
        offenders: list[str] = []
        for directory in ("bin", "lib", "docs", "tests"):
            for path in sorted((REPO / directory).rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                if path.name == Path(__file__).name:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for word in banned:
                    if word in text:
                        offenders.append(f"{path.relative_to(REPO)}: {word}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
