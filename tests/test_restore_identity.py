"""A restored conversation comes back as itself: same name, same color.

The live regression (2026-08-12) was a restored session opening nameless while
the picker still showed its name. Restore starts a NEW provider process for an
old conversation, and that process reads its name and its color from the
provider's own store once, at start -- so both have to be in that store BEFORE
the launch, and a restore that only wrote the color left the window unnamed.

Nothing here starts a real session: the command fixture's fake shpool stands in
for the session manager, and the provider-store half is exercised directly
against a sandbox HOME, so no live session, daemon, or provider profile is
touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
import unittest

from tests.support import REPO, run
from tests.test_commands import CommandFixture, write_executable

SP = REPO / "bin" / "sp"
CORE = REPO / "lib" / "session_inventory.py"
UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class RestoreBringsBackNameAndColorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CommandFixture()
        self.addCleanup(self.fixture.close)

    def restore_env(self, **extra: str) -> dict[str, str]:
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "2",
                "STUB_DYNAMIC_PROVIDER": "codex",
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_DYNAMIC_UUID": UUID,
                "STUB_COLOR_LOG": str(self.fixture.base / "color.log"),
            }
        )
        env.update(extra)
        return env

    def restore(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return run(
            [SP, "restore-exact", "codex", UUID, self.fixture.project],
            env=self.restore_env(**extra),
            check=False,
        )

    def test_restore_writes_both_the_name_and_the_color(self) -> None:
        result = self.restore()
        self.assertEqual(0, result.returncode, result.stderr)
        colors = (self.fixture.base / "color.log").read_text()
        names = self.fixture.name_push_log.read_text()
        self.assertIn(json.dumps(["color", "propagate", "codex", UUID]), colors)
        self.assertIn(json.dumps(["alias", "push", "codex", UUID]), names)

    def test_a_name_that_could_not_be_written_is_said_out_loud(self) -> None:
        result = self.restore(STUB_ALIAS_PUSH_FAIL="1")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("the window opens unnamed", result.stderr)

    def test_a_half_written_name_is_said_out_loud(self) -> None:
        """Codex writes two surfaces and the status bar reads one of them."""
        result = self.restore(STUB_ALIAS_PUSH_PARTIAL="1")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("reached only part of this conversation", result.stderr)

    def test_a_color_that_could_not_be_written_is_said_out_loud(self) -> None:
        result = self.restore(STUB_COLOR_PROPAGATE_FAIL="1")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("the window opens in its own color", result.stderr)

    def test_both_writes_happen_before_the_provider_is_launched(self) -> None:
        """Order is the whole point: the provider reads these once, at start."""
        events = self.fixture.base / "restore-order.log"
        result = self.restore(
            STUB_COLOR_LOG=os.fspath(events),
            STUB_NAME_PUSH_LOG=os.fspath(events),
            FAKE_SHPOOL_LOG=os.fspath(events),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        observed = events.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            ["color", "propagate", "codex", UUID], json.loads(observed[0])
        )
        self.assertEqual(["alias", "push", "codex", UUID], json.loads(observed[1]))
        self.assertRegex(observed[2], r"^attach s[0-9]{8}-[0-9]{6}-[0-9]+$")
        self.assertEqual(3, len(observed), observed)


class CreationSaysWhatHappenedTests(unittest.TestCase):
    """Track C: a prebake or color failure at creation must say so.

    The restore half was pinned from the start; these two lines shipped
    untested, which is how the same class of silence gets back in.
    """

    def setUp(self) -> None:
        self.fixture = CommandFixture()
        self.addCleanup(self.fixture.close)

    def create(self, **extra: str) -> subprocess.CompletedProcess[str]:
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_NO_PREBAKE": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "2",
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_UUID": UUID,
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
            }
        )
        env.update(extra)
        return run(
            [SP, "new", "claude"],
            env=env,
            cwd=self.fixture.project,
            check=False,
        )

    def test_a_color_that_did_not_land_at_creation_is_said_out_loud(self) -> None:
        result = self.create(STUB_COLOR_PROPAGATE_FAIL="1")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("takes the kit color from its next start", result.stderr)

    def test_a_color_that_landed_says_nothing(self) -> None:
        result = self.create()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("could not be written", result.stderr)

    def test_a_disabled_prebake_is_quiet(self) -> None:
        """Choosing the kill switch is not a failed creation step."""
        result = self.create()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("first-frame color step", result.stderr)

    def test_a_failed_prebake_is_said_out_loud(self) -> None:
        """Run the attempted path and make its throwaway TUI fail harmlessly."""
        fake_bin = self.fixture.base / "prebake-bin"
        fake_bin.mkdir()
        for command in ("claude", "script", "sleep"):
            write_executable(fake_bin / command, "#!/usr/bin/env bash\nexit 1\n")
        result = self.create(
            SESSION_KIT_NO_PREBAKE="0",
            PATH=f"{fake_bin}:{os.environ['PATH']}",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("The first-frame color step did not finish", result.stderr)

    def create_on_account(self, profile: Path, **extra: str):
        """`sp new claude --account fixture`, on a stubbed enrolled profile."""
        env = self.fixture.env()
        env.update(
            {
                "SESSION_KIT_BACKGROUND": "1",
                "SESSION_KIT_NO_PREBAKE": "1",
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "2",
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_UUID": UUID,
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "STUB_ACCOUNT_PROFILE": str(profile),
            }
        )
        env.update(extra)
        return run(
            [SP, "new", "claude", "--account", "fixture"],
            env=env,
            cwd=self.fixture.project,
            check=False,
        )

    def test_a_session_on_an_account_also_gets_the_first_frame_step(self) -> None:
        """The gap the operator hit: their session runs on an enrolled profile.

        The first-frame colour step was skipped outright whenever an account
        was named, so a session on a chosen profile opened with no colour at
        all while a default-profile one opened already coloured. Proven by
        making the throwaway TUI fail: a step that is SKIPPED says nothing,
        and a step that RUNS and fails says so.
        """
        profile = self.fixture.base / "account-profile"
        (profile / "projects").mkdir(parents=True)
        fake_bin = self.fixture.base / "prebake-account-bin"
        fake_bin.mkdir()
        for command in ("claude", "script", "sleep"):
            write_executable(fake_bin / command, "#!/usr/bin/env bash\nexit 1\n")

        result = self.create_on_account(
            profile,
            SESSION_KIT_NO_PREBAKE="0",
            PATH=f"{fake_bin}:{os.environ['PATH']}",
        )

        self.assertIn("The first-frame color step did not finish", result.stderr)

    def test_an_account_session_with_the_step_off_stays_quiet(self) -> None:
        """The kill switch still means the same thing on every session."""
        profile = self.fixture.base / "quiet-profile"
        (profile / "projects").mkdir(parents=True)

        result = self.create_on_account(profile)

        self.assertNotIn("first-frame color step", result.stderr)

    def prebake_tui(self, directory: Path) -> Path:
        """A throwaway TUI that behaves like the real one.

        It honours CLAUDE_CONFIG_DIR, answers `/color`, and -- the property
        that used to cost the whole timeout -- does NOT exit when its input
        closes. Only a signal ends it.

        The property this stub exists to get right: **no transcript until it
        is typed at.** Real Claude Code creates the transcript file when it
        writes its first record, and in a throwaway the first record is the
        one `/color` itself produces -- verified against claude 2.1.233, where
        a throwaway that is never typed at writes no transcript at all. This
        stub used to write its transcript before reading a byte of input,
        which is a shape the real binary cannot produce. That is the single
        property the whole feeder depends on, so a feeder that waited for the
        transcript BEFORE typing passed this test while shipping a
        first-frame colour step that could never land a colour on anything.
        """
        directory.mkdir(exist_ok=True)
        write_executable(
            directory / "claude",
            "#!/usr/bin/env bash\n"
            'uuid=""\n'
            'while [[ $# -gt 0 ]]; do [[ $1 == --session-id ]] && uuid=$2; shift; done\n'
            'root=${CLAUDE_CONFIG_DIR:-$HOME/.claude}\n'
            'directory="$root/projects/-fixture"\n'
            'transcript="$directory/$uuid.jsonl"\n'
            "while IFS= read -r line; do\n"
            "  line=${line%$'\\r'}\n"
            '  case "$line" in\n'
            "    /color*)\n"
            '      mkdir -p "$directory" || exit 1\n'
            '      [[ -s "$transcript" ]] ||'
            ' printf \'{"type":"attachment","sessionId":"%s"}\\n\''
            ' "$uuid" > "$transcript"\n'
            "      printf '"
            '{"type":"agent-color","agentColor":"%s","sessionId":"%s"}\\n\' '
            '"${line#/color }" "$uuid" >> "$transcript"\n'
            "      ;;\n"
            "  esac\n"
            "done\n"
            "sleep 300\n",
        )
        return directory

    def test_a_prebake_that_succeeds_on_an_account_leaves_no_process_behind(
        self,
    ) -> None:
        """The happy path of the central mechanism, which nothing covered.

        Every other pre-bake test stubs `claude`, `script` and `sleep` to
        `exit 1`, so they prove only that the step RUNS and FAILS. This one
        proves the half that matters: the throwaway mints its conversation
        INSIDE the account's own profile with the colour the kit picked, the
        step reports no failure, and nothing is left running afterwards --
        the leak that put a four-day-old provider pair on the operator's box.
        """
        profile = self.fixture.base / "success-profile"
        (profile / "projects").mkdir(parents=True)
        fake_bin = self.prebake_tui(self.fixture.base / "prebake-success-bin")

        started = time.monotonic()
        result = self.create_on_account(
            profile,
            SESSION_KIT_NO_PREBAKE="0",
            PATH=f"{fake_bin}:{os.environ['PATH']}",
        )
        elapsed = time.monotonic() - started

        self.assertNotIn("The first-frame color step did not finish", result.stderr)
        # What the person actually notices. The step used to sleep seven
        # fixed seconds and then wait out a fourteen-second timeout killing a
        # TUI that does not exit when its input closes -- about fifteen
        # seconds of silence before the window opened, on every session. It
        # now stops as soon as the colour record exists.
        self.assertLess(elapsed, 10.0, f"creation took {elapsed:.1f}s")
        minted = sorted((profile / "projects" / "-fixture").glob("*.jsonl"))
        self.assertEqual(1, len(minted), f"transcripts: {minted}")
        uuid = minted[0].stem
        records = [
            json.loads(line)
            for line in minted[0].read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertIn(
            {"type": "agent-color", "agentColor": "orange", "sessionId": uuid},
            records,
        )
        # Nothing ambient: the conversation belongs to the account profile.
        self.assertEqual(
            [], sorted((self.fixture.home / ".claude" / "projects").glob("*/*.jsonl"))
        )
        # The window is armed to RESUME that exact conversation, which is what
        # makes the colour native in its very first frame.
        records = sorted(
            path for path in self.fixture.start.iterdir() if path.is_file()
        )
        armed = [
            path.read_text(encoding="utf-8")
            for path in records
            if not path.name.endswith((".expected", ".launch", ".account"))
        ]
        self.assertTrue(armed, f"no start record in {records}")
        self.assertIn(f"\t{uuid}\tresume\n", armed[0])
        # And the throwaway is gone -- this is the leak that put a four-day-old
        # provider pair on the operator's box.
        self.assertEqual([], self.processes_carrying(uuid))
        # The fake shpool never really opens the pre-baked conversation, so
        # the post-launch proof cannot pass in this fixture. That single
        # complaint is the fixture's limit; anything else would be real.
        self.assertIn("could not confirm that Claude started", result.stderr)

    def processes_carrying(self, uuid: str) -> list[str]:
        """Every live process whose argv still names this conversation."""
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                argv = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if uuid.encode() in argv:
                found.append(argv.replace(b"\0", b" ").decode(errors="replace"))
        return found


class ProviderStoreRoundTripTests(unittest.TestCase):
    """The half a fake shpool cannot prove: what the restored window reads."""

    def sandbox(self, base: Path) -> tuple[dict[str, str], Path, Path]:
        home = base / "home"
        (home / ".claude" / "sessions").mkdir(parents=True, mode=0o700)
        project = home / ".claude" / "projects" / "-srv-project"
        project.mkdir(parents=True, mode=0o700)
        transcript = project / f"{UUID}.jsonl"
        transcript.write_text(
            json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n",
            encoding="utf-8",
        )
        config = base / "session-kit.json"
        config.write_text(
            json.dumps({"schema_version": 1, "aliases": {}}), encoding="utf-8"
        )
        config.chmod(0o600)
        state = base / "state"
        state.mkdir(mode=0o700)
        state.chmod(0o700)
        env = {
            **os.environ,
            "HOME": os.fspath(home),
            "SESSION_KIT_CONFIG": os.fspath(config),
            "SESSION_KIT_STATE_DIR": os.fspath(state),
            "SESSION_KIT_CODEX_AUTOTITLE": "0",
        }
        return env, home, transcript

    def core(self, env: dict[str, str], *arguments: str, check: bool = True):
        return run([CORE, *arguments], env=env, check=check)

    def records(self, transcript: Path, kind: str) -> list[dict]:
        found = []
        for line in transcript.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("type") == kind:
                found.append(item)
        return found

    def test_a_named_and_colored_conversation_survives_a_wiped_store(self) -> None:
        with self.subTest("round trip"):
            import tempfile

            with tempfile.TemporaryDirectory(prefix=".restore-id-", dir=REPO) as raw:
                env, home, transcript = self.sandbox(Path(raw))
                self.core(env, "alias", "set", "claude", UUID, "Log Review")
                self.core(env, "color", "set", "claude", UUID, "cyan")

                # The state a restore finds after the provider replaced its own
                # store contents: the kit still holds the name and the color,
                # the provider surfaces no longer carry either.
                (home / ".claude" / "sessions" / f"{UUID}.nameintent").unlink()
                transcript.write_text(
                    json.dumps({"type": "user", "message": {"content": "hello"}})
                    + "\n",
                    encoding="utf-8",
                )

                self.core(env, "alias", "push", "claude", UUID)
                self.core(env, "color", "propagate", "claude", UUID)

                intent = home / ".claude" / "sessions" / f"{UUID}.nameintent"
                self.assertTrue(intent.is_file())
                self.assertEqual("Log Review", intent.read_text().strip())
                self.assertEqual(
                    ["Log Review"],
                    [item["agentName"] for item in self.records(transcript, "agent-name")],
                )
                self.assertEqual(
                    ["cyan"],
                    [
                        item["agentColor"]
                        for item in self.records(transcript, "agent-color")
                    ],
                )

    def test_push_says_nothing_when_nothing_named_the_conversation(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix=".restore-none-", dir=REPO) as raw:
            env, _, _ = self.sandbox(Path(raw))
            result = self.core(env, "alias", "push", "claude", UUID)
            payload = json.loads(result.stdout)
            self.assertEqual("", payload["title"])
            self.assertEqual([], payload["provider_title_pushes"])

    def test_a_partial_push_is_not_reported_as_success(self) -> None:
        """Index written, database title not: exit 3, not 0."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix=".restore-partial-", dir=REPO) as raw:
            base = Path(raw)
            env, home, _ = self.sandbox(base)
            codex_uuid = UUID
            (home / ".codex").mkdir(parents=True, mode=0o700)
            # A thread store with no row for this conversation: the index append
            # lands, the title update finds nothing to update.
            import sqlite3

            database = home / ".codex" / "state_5.sqlite"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT,"
                " first_user_message TEXT, updated_at REAL)"
            )
            connection.commit()
            connection.close()
            self.core(env, "alias", "set", "codex", codex_uuid, "Log Review")
            result = self.core(env, "alias", "push", "codex", codex_uuid, check=False)
            self.assertEqual(3, result.returncode, result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual("Log Review", payload["title"])
            self.assertTrue(payload["provider_title_pushes"])
            self.assertTrue(payload["provider_title_warnings"])

    def test_a_name_typed_in_the_window_is_never_overwritten(self) -> None:
        """The automatic tier is not safe to re-assert over a human rename.

        A person renames in the window; no inventory build has adopted it yet.
        Pushing the kit's derived title would replace their name in the
        provider's own store -- and the evidence adoption needs to recover it.
        """
        import tempfile

        with tempfile.TemporaryDirectory(prefix=".restore-human-", dir=REPO) as raw:
            env, home, _ = self.sandbox(Path(raw))
            # The kit holds a derived title; the person has since renamed the
            # conversation in the window, and the ownership record says so.
            config = json.loads(Path(env["SESSION_KIT_CONFIG"]).read_text())
            config["automatic_titles"] = {f"claude:{UUID}": "Auto Name"}
            config["name_ownership"] = {
                f"claude:{UUID}": {"owner": "human", "at": "2026-08-13T00:00:00Z"}
            }
            Path(env["SESSION_KIT_CONFIG"]).write_text(json.dumps(config))
            result = self.core(env, "alias", "push", "claude", UUID)
            self.assertEqual("", json.loads(result.stdout)["title"])
            self.assertFalse(
                (home / ".claude" / "sessions" / f"{UUID}.nameintent").exists()
            )

    def test_push_fails_loudly_when_a_held_name_reaches_no_surface(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix=".restore-fail-", dir=REPO) as raw:
            base = Path(raw)
            env, home, _ = self.sandbox(base)
            self.core(env, "alias", "set", "claude", UUID, "Log Review")
            # A provider profile that has gone: no sessions directory, no
            # transcripts. The kit still holds the name, and a restore into
            # this state produces a nameless window -- which the caller must
            # be able to report.
            for path in (home / ".claude" / "sessions", home / ".claude" / "projects"):
                subprocess.run(["rm", "-rf", os.fspath(path)], check=True)
            result = self.core(env, "alias", "push", "claude", UUID, check=False)
            self.assertEqual(1, result.returncode, result.stdout)
            self.assertEqual("Log Review", json.loads(result.stdout)["title"])


if __name__ == "__main__":
    unittest.main()
