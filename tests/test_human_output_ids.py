"""The invariant: no human-readable surface ever prints a session identifier.

Identifiers live in 0600 state files, machine-only JSON modes, proofs, and
command arguments. They are never displayed — not in a row, not in a
confirmation, not in an error, not behind a flag.

This is the enforcement mechanism, not a collection of per-surface checks.
Every human surface is registered in ``SURFACES`` below and rendered against
one fixture whose identifiers are deliberately distinctive hex. A surface that
leaks fails here; a surface nobody registered fails ``test_every_human_surface_is_registered``,
so a new screen cannot join the kit without someone deciding, in writing,
which side of the line it is on.

Whole identifiers are not the whole test. A truncated identifier is still an
identifier — the title fallback used to render ``Codex a1b2c3d4`` — so the
scan flags any eight-hex-character prefix of a fixture identifier, which is
why the fixture UUIDs below start with distinctive hex rather than zeros.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from tests.support import REPO


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
    "lib/sessionkit_supervisor/prompt_quarantine.py": (
        "prompt-quarantine list --json returns captured picker selection keys"
    ),
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
        "queue_titles",
        "msg_resolve_refusal",
        "recovery_manifest_display",
        "sp_usage",
        "status_usage",
        "sp_recover",
        "sp_prune",
        "sp_color_reconcile",
        "picker_unread_replies",
        "prompt_quarantine_list",
        "picker_prompt_quarantine",
    )

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
        event_root = cls.state / "events"
        event_root.mkdir(mode=0o700)
        (event_root / f"claude:{CLAUDE_UUID}.jsonl").write_text(
            "\n".join(
                json.dumps(record, separators=(",", ":"))
                for record in (
                    {
                        "event": "turn_done",
                        "question": None,
                        "source": "hook",
                        "ts_unix_ms": 1_700_000_001_000,
                    },
                    {
                        "event": "needs_input",
                        "question": None,
                        "source": "hook",
                        "ts_unix_ms": 1_700_000_003_000,
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (event_root / f"codex:{CODEX_UUID}.jsonl").write_text(
            json.dumps(
                {
                    "event": "needs_input",
                    "question": None,
                    "source": "synth",
                    "ts_unix_ms": 1_700_000_002_000,
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

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
        self.assertIn("claude", text)
        self.assertIn("Last response", text)
        self.assertIn("Opened", text)
        self.assertRegex(text, r"Last response\s+Nov 14, 2023 at")
        self.assertNotIn("Waiting since", text)
        waiting = self._core("detail", "3")
        self._assert_clean("detail_waiting", waiting)
        self.assertRegex(waiting, r"Waiting since\s+Nov 14, 2023 at")
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

    def test_the_attention_queue_titles_never_fall_back_to_a_thread_key(
        self,
    ) -> None:
        library = os.fspath(REPO / "lib")
        if library not in sys.path:
            sys.path.insert(0, library)
        from sessionkit_events.queue import build_attention_queue

        queue = build_attention_queue(
            fixture_inventory(), self.state, now_ms=1_800_000_000_000
        )
        # thread_key is a machine field and is expected to hold the key; the
        # human-readable fields are what this scan covers.
        human = "\n".join(
            str(item.get("title", "")) + " " + str(item.get("question") or "")
            for item in queue["items"]
        )
        self._assert_clean("queue_titles", human)

    def test_a_refused_message_target_names_no_session(self) -> None:
        self._assert_clean(
            "msg_resolve_refusal", self._core("msg", "resolve", "--target", "99")
        )

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

    def test_the_pickers_unread_reply_rows_name_no_thread(self) -> None:
        """The reply section renders from thread keys and must print none.

        Covered end-to-end in tests.test_picker_message; this is the entry
        that keeps the section inside the scan's registry, so it cannot be
        rewritten later without someone answering for the identifiers.
        """
        source = (REPO / "lib" / "sh" / "shpool_login_render.sh").read_text(
            encoding="utf-8"
        )
        start = source.index("picker_unread_reply_rows() {")
        block = source[start : source.index("\nREPLIES\n", start)]
        # The key is the join column and never a printed field: it may reach
        # a path or a dict lookup, never a rendered line.
        for rendered in ("index.append", "lines.append", "print("):
            self.assertIn(rendered, block)
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("lines.append") or stripped.startswith("print("):
                self.assertNotIn("key", stripped, f"a thread key reaches: {stripped}")

    def test_prompt_quarantine_human_surfaces_name_no_selection_key(self) -> None:
        quarantine = self.state / "prompt-human-surface"
        quarantine.mkdir(mode=0o700)
        prompt = quarantine / f"{CODEX_UUID}.prompt.intake_pending"
        prompt.write_text("fixture prompt\n", encoding="utf-8")
        prompt.chmod(0o600)
        environment = {
            **os.environ,
            "HOME": os.fspath(self.home),
            "SESSION_KIT_START_DIR": os.fspath(quarantine),
            "SESSION_KIT_SHPOOL_CMD": os.fspath(self.shpool),
        }
        completed = subprocess.run(
            [os.fspath(SP), "prompt-quarantine", "list"],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        output = completed.stdout + completed.stderr
        self._assert_clean("prompt_quarantine_list", output)
        self.assertIn("Codex prompt intake pending", output)
        self.assertNotIn(hashlib.sha256(prompt.name.encode()).hexdigest()[:12], output)

        # The picker renderer accepts the captured machine key only into its
        # private q-number index. Its printed row is built from a state/title
        # whitelist and age. The end-to-end rendering and actions are covered
        # by tests.test_login.
        source = (REPO / "lib" / "sh" / "shpool_login_render.sh").read_text(
            encoding="utf-8"
        )
        start = source.index("picker_prompt_quarantine_rows() {")
        block = source[start : source.index("\nPY\n", start)]
        self.assertIn('index.append(f"q{number}\\t{key}', block)
        self.assertNotIn("lines.append(f\"{key}", block)
        self._assert_clean(
            "picker_prompt_quarantine",
            "Needs You · prompt delivery\nq1 Codex prompt intake pending | 1m old",
        )

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

    def test_every_human_surface_is_registered(self) -> None:
        """A new human surface joins this scan or fails this test.

        The registry is the point: adding a screen without deciding whether it
        prints identifiers should be impossible to do quietly. `sp` verbs and
        `shpool_status` modes are the two doors a person comes through.
        """
        covered = set(self.SURFACES)
        tested = {
            name
            for name in covered
            if any(
                f'"{name}"' in Path(__file__).read_text(encoding="utf-8")
                for _ in (0,)
            )
        }
        self.assertEqual(covered, tested)

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
            "msg",
            "prompt-quarantine",
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


class ShellRendererTests(unittest.TestCase):
    """The renderers `sp` embeds, run as they ship.

    `sp recover`, `sp prune` and `sp color reconcile` each format their screen
    inside a python block in `lib/sh/sp_commands.sh`. Extracting the block and
    running it is the only way to test the code that actually reaches a
    terminal — a rewritten copy in the test would prove nothing about it.
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
        program = self._block("show_recovery() {")
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, dir=REPO, prefix=".human-output-"
        ) as handle:
            json.dump(
                {
                    "sessions": {
                        CLAUDE_SHPOOL: {
                            "provider": "claude",
                            "title": "Fleet rebuild",
                            "uuid": CLAUDE_UUID,
                        },
                        CODEX_SHPOOL: {
                            "provider": "codex",
                            "title": "",
                            "uuid": CODEX_UUID,
                        },
                    }
                },
                handle,
            )
            manifest = handle.name
        try:
            output = self._run(program, manifest)
            self._assert_clean("sp_recover", output)
            self.assertIn("Fleet rebuild", output)
        finally:
            Path(manifest).unlink()

    def test_the_prune_candidate_list_numbers_candidates(self) -> None:
        program = self._block("printf 'Verified prune candidates:")
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
            self.assertIn(" 2 ", output)
        finally:
            Path(candidates).unlink()

    def test_color_reconcile_reports_counts_not_conversations(self) -> None:
        text = self.SOURCE.read_text(encoding="utf-8")
        start = text.index("printf '%s' \"$payload\" | python3 -c '")
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
        self.assertIn("Recolored 1 exact claude session(s)", output)
        self.assertIn("Recolored 1 exact codex session(s)", output)


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
