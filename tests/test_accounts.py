from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib.sessionkit_inventory import accounts
from lib.sessionkit_inventory.common import CollectionError


CLAUDE_UUID = "00000000-0000-4000-8000-000000000101"
CODEX_UUID = "00000000-0000-4000-8000-000000000202"


class AccountProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-accounts.")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.data = self.root / "data"
        self.home.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        self.data.mkdir(mode=0o700)
        self.registry = self.state / "accounts.json"
        self.profile_root = self.data / "session-kit" / "accounts"
        self.roster = self.root / "cli_accounts.json"
        self.advice = self.root / "rotation_advice.json"
        self.config = {"state_dir": str(self.state)}
        self.environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_DATA_HOME": str(self.data),
                "SESSION_KIT_ACCOUNT_REGISTRY": str(self.registry),
                "SESSION_KIT_ACCOUNT_ROOT": str(self.profile_root),
                "SESSION_KIT_ACCOUNT_ROSTER": str(self.roster),
                "SESSION_KIT_ROTATION_ADVICE": str(self.advice),
            },
            clear=False,
        )
        self.environment.start()
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "OPENAI_API_KEY",
            "CODEX_ACCESS_TOKEN",
            "SESSION_KIT_ACCOUNT_ADVICE_MAX_AGE_SECONDS",
        ):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def profile_dir(self, provider: str, alias: str) -> Path:
        path = self.profile_root / provider / alias
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def seed_profiles(self, *rows: tuple[str, str, str]) -> None:
        profiles = {}
        for provider, alias, email in rows:
            profile_dir = self.profile_dir(provider, alias)
            if provider == "claude":
                state_path = profile_dir / ".claude.json"
                state_path.write_text(
                    json.dumps({"hasCompletedOnboarding": True}),
                    encoding="utf-8",
                )
                state_path.chmod(0o600)
            profiles[f"{provider}:{alias}"] = {
                "provider": provider,
                "alias": alias,
                "email": email,
                "profile_dir": str(profile_dir),
                "legacy": False,
                "plan": "max" if provider == "claude" else "plus",
                "verified_at_unix_ms": 1_800_000_000_000,
                "enabled": True,
            }
        accounts.write_registry(
            self.config,
            {
                "schema_version": accounts.ACCOUNT_SCHEMA_VERSION,
                "generation": 1,
                "profiles": profiles,
                "bindings": {},
            },
        )

    def write_feed(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)

    def test_registry_round_trip_and_exact_provider_binding(self) -> None:
        self.seed_profiles(
            ("claude", "primary", "primary@invalid.example"),
            ("codex", "primary", "primary@invalid.example"),
        )

        stored = accounts.bind(
            self.config, "claude", CLAUDE_UUID.upper(), "primary", source="new"
        )
        found = accounts.binding_for(self.config, "claude", CLAUDE_UUID)

        self.assertEqual(2, stored["generation"])
        self.assertEqual("primary", found["alias"])
        self.assertEqual("new", found["binding_source"])
        self.assertIsNone(accounts.binding_for(self.config, "codex", CLAUDE_UUID))
        self.assertEqual(0o600, self.registry.stat().st_mode & 0o777)

    def test_legacy_first_switch_proves_unique_artifact_and_binds_source(self) -> None:
        self.seed_profiles(
            ("claude", "source", "source@example.com"),
            ("claude", "target", "target@example.com"),
        )
        transcript = (
            self.profile_root
            / "claude"
            / "source"
            / "projects"
            / "repo"
            / f"{CLAUDE_UUID}.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n", encoding="utf-8")
        transcript.chmod(0o600)

        found = accounts.source_profile_for_thread(self.config, "claude", CLAUDE_UUID)

        self.assertEqual("source", found["alias"])
        self.assertEqual(
            "legacy-first-switch",
            accounts.binding_for(self.config, "claude", CLAUDE_UUID)["binding_source"],
        )

    def test_registry_rejects_escaped_profile_and_cross_provider_binding(self) -> None:
        invalid_profile = {
            "schema_version": accounts.ACCOUNT_SCHEMA_VERSION,
            "generation": 1,
            "profiles": {
                "claude:primary": {
                    "provider": "claude",
                    "alias": "primary",
                    "email": "primary@invalid.example",
                    "profile_dir": str(self.root / "escaped"),
                    "legacy": False,
                    "plan": "max",
                    "verified_at_unix_ms": 1,
                    "enabled": True,
                }
            },
            "bindings": {},
        }
        with self.assertRaisesRegex(CollectionError, "escaped"):
            accounts.write_registry(self.config, invalid_profile)

        self.seed_profiles(
            ("claude", "primary", "primary@invalid.example"),
            ("codex", "work", "work@example.com"),
        )
        invalid_binding = accounts.load_registry(self.config)
        invalid_binding["bindings"][f"claude:{CLAUDE_UUID}"] = {
            "profile": "codex:work",
            "bound_at_unix_ms": 1,
            "source": "test",
        }
        with self.assertRaisesRegex(CollectionError, "binding identity"):
            accounts.write_registry(self.config, invalid_binding)

    def test_choices_preselect_fresh_claude_advice_but_never_codex(self) -> None:
        self.seed_profiles(
            ("claude", "primary", "primary@invalid.example"),
            ("claude", "backup", "backup@invalid.example"),
            ("codex", "primary", "primary@invalid.example"),
        )
        now = 1_800_000_000
        self.write_feed(
            self.roster,
            {
                "ts": now,
                "accounts": [
                    {
                        "email": "primary@invalid.example",
                        "health": "ok",
                        "serving": True,
                        "u7d": 69,
                    },
                    {
                        "email": "backup@invalid.example",
                        "health": "ok",
                        "serving": True,
                        "u7d": 0,
                    },
                ],
                "codex_accounts": [
                    {
                        "email": "primary@invalid.example",
                        "health": "ok",
                        "serving": True,
                        "u7d": 27,
                    }
                ],
            },
        )
        self.write_feed(
            self.advice,
            {
                "ts": now,
                "use_now": {
                    "account": "backup@invalid.example",
                    "why": "most weekly allowance remains",
                },
            },
        )

        with mock.patch.object(accounts.time, "time", return_value=now):
            claude = accounts.account_choices(self.config, "claude")
            codex = accounts.account_choices(self.config, "codex")

        self.assertEqual("backup", claude["recommendation"])
        self.assertEqual(
            "most weekly allowance remains", claude["recommendation_reason"]
        )
        self.assertTrue(
            next(row for row in claude["choices"] if row["alias"] == "backup")[
                "eligible"
            ]
        )
        self.assertIsNone(codex["recommendation"])
        self.assertFalse(any(row["recommended"] for row in codex["choices"]))

    def test_configured_feeds_are_private_and_used_without_environment_overrides(
        self,
    ) -> None:
        self.seed_profiles(("claude", "primary", "primary@invalid.example"))
        now = int(accounts.time.time())
        self.write_feed(
            self.roster,
            {
                "ts": now,
                "accounts": [
                    {
                        "email": "primary@invalid.example",
                        "health": "ok",
                        "serving": True,
                    }
                ],
            },
        )
        self.write_feed(self.advice, {"ts": now})

        configured = accounts.configure_feeds(
            self.config, str(self.roster), str(self.advice)
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SESSION_KIT_ACCOUNT_ROSTER", None)
            os.environ.pop("SESSION_KIT_ROTATION_ADVICE", None)
            choices = accounts.account_choices(self.config, "claude")

        self.assertEqual(str(self.roster), configured["roster_path"])
        self.assertTrue(choices["choices"][0]["eligible"])
        self.assertEqual(
            0o600,
            accounts.feed_config_path(self.config).stat().st_mode & 0o777,
        )

    def test_launch_profile_reverifies_identity_and_scrubs_ambient_tokens(self) -> None:
        self.seed_profiles(("claude", "primary", "primary@invalid.example"))
        self.write_feed(
            self.roster,
            {
                "ts": int(accounts.time.time()),
                "accounts": [
                    {
                        "email": "primary@invalid.example",
                        "health": "ok",
                        "serving": True,
                    }
                ],
            },
        )
        profile_dir = self.profile_root / "claude" / "primary"
        with mock.patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "must-not-pass",
                "ANTHROPIC_AUTH_TOKEN": "must-not-pass",
                "CLAUDE_CODE_OAUTH_TOKEN": "must-not-pass",
            },
            clear=False,
        ):
            environment = accounts._provider_environment("claude", profile_dir)
        self.assertEqual(str(profile_dir), environment["CLAUDE_CONFIG_DIR"])
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", environment)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)

        with mock.patch.object(
            accounts,
            "probe_identity",
            return_value={
                "provider": "claude",
                "email": "primary@invalid.example",
                "plan": "max",
            },
        ) as probe:
            launched = accounts.launch_profile(self.config, "claude", "primary")
        self.assertEqual("primary", launched["alias"])
        self.assertEqual("primary@invalid.example", launched["email"])
        probe.assert_called_once_with("claude", profile_dir)

        with mock.patch.object(
            accounts,
            "probe_identity",
            return_value={
                "provider": "claude",
                "email": "wrong@example.com",
                "plan": "max",
            },
        ):
            with self.assertRaisesRegex(CollectionError, "does not match"):
                accounts.launch_profile(self.config, "claude", "primary")

        self.write_feed(
            self.roster,
            {
                "ts": int(accounts.time.time()),
                "accounts": [
                    {
                        "email": "primary@invalid.example",
                        "health": "error",
                        "serving": False,
                    }
                ],
            },
        )
        with mock.patch.object(
            accounts,
            "probe_identity",
            return_value={
                "provider": "claude",
                "email": "primary@invalid.example",
                "plan": "max",
            },
        ):
            with self.assertRaisesRegex(CollectionError, "not selectable"):
                accounts.launch_profile(self.config, "claude", "primary")

    def test_register_isolated_claude_profile_completes_onboarding(self) -> None:
        profile_dir = self.profile_dir("claude", "primary")
        state_path = profile_dir / ".claude.json"
        state_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "primary@invalid.example"}}),
            encoding="utf-8",
        )
        state_path.chmod(0o600)
        default_state = self.home / ".claude.json"
        default_state.write_text(
            json.dumps(
                {
                    "projects": {
                        "/srv/trusted": {"hasTrustDialogAccepted": True},
                        "/srv/untrusted": {"hasTrustDialogAccepted": False},
                    }
                }
            ),
            encoding="utf-8",
        )
        default_state.chmod(0o644)

        with mock.patch.object(
            accounts,
            "probe_identity",
            return_value={
                "provider": "claude",
                "email": "primary@invalid.example",
                "plan": "max",
            },
        ):
            accounts._register_profile(
                self.config,
                "claude",
                "primary",
                "primary@invalid.example",
                profile_dir,
                legacy=False,
            )

        stored = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(stored["hasCompletedOnboarding"])
        self.assertEqual(
            "primary@invalid.example", stored["oauthAccount"]["emailAddress"]
        )
        self.assertTrue(stored["projects"]["/srv/trusted"]["hasTrustDialogAccepted"])
        self.assertNotIn("/srv/untrusted", stored["projects"])
        self.assertEqual(0o600, state_path.stat().st_mode & 0o777)

    def provision(self, alias: str = "primary") -> Path:
        """Enroll one isolated Claude profile and return its state file."""
        profile_dir = self.profile_dir("claude", alias)
        state_path = profile_dir / ".claude.json"
        if not state_path.exists():
            state_path.write_text(
                json.dumps(
                    {"oauthAccount": {"emailAddress": f"{alias}@invalid.example"}}
                ),
                encoding="utf-8",
            )
            state_path.chmod(0o600)
        with mock.patch.object(
            accounts,
            "probe_identity",
            return_value={
                "provider": "claude",
                "email": f"{alias}@invalid.example",
                "plan": "max",
            },
        ):
            accounts._register_profile(
                self.config,
                "claude",
                alias,
                f"{alias}@invalid.example",
                profile_dir,
                legacy=False,
            )
        return state_path

    def write_default_state(self, **extra: object) -> None:
        default_state = self.home / ".claude.json"
        default_state.write_text(json.dumps(dict(extra)), encoding="utf-8")
        default_state.chmod(0o644)

    def test_a_fresh_profile_does_not_open_onto_a_first_run_offer(self) -> None:
        """A brand-new session came up on "Make auto mode your default
        permission mode?" and the operator could not answer it. A fresh kit
        profile had never been shown that offer; the default profile had."""
        self.write_default_state(hasSeenAutoDefaultNudge=True)

        stored = json.loads(self.provision().read_text(encoding="utf-8"))

        self.assertIs(True, stored["hasSeenAutoDefaultNudge"])
        # Seen, not answered: the permission mode is not chosen here.
        self.assertNotIn("defaultMode", stored)
        self.assertNotIn("permissions", stored)

    def test_an_offer_the_operator_never_saw_is_not_marked_seen(self) -> None:
        """Only what they have already been shown, on their own default profile."""
        self.write_default_state()

        stored = json.loads(self.provision().read_text(encoding="utf-8"))

        self.assertNotIn("hasSeenAutoDefaultNudge", stored)

    def test_a_value_the_profile_already_holds_is_never_overwritten(self) -> None:
        """An existing value is their answer, in either direction."""
        self.write_default_state(hasSeenAutoDefaultNudge=True)
        profile_dir = self.profile_dir("claude", "primary")
        state_path = profile_dir / ".claude.json"
        state_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {"emailAddress": "primary@invalid.example"},
                    "hasSeenAutoDefaultNudge": False,
                }
            ),
            encoding="utf-8",
        )
        state_path.chmod(0o600)

        stored = json.loads(self.provision().read_text(encoding="utf-8"))

        self.assertIs(False, stored["hasSeenAutoDefaultNudge"])

    def test_a_concurrent_provider_write_is_not_lost(self) -> None:
        """Claude owns this file too. A key it adds mid-provision survives.

        The provider writes between this function's read and its write; the
        re-read before the atomic replace is what keeps that key.
        """
        self.write_default_state(hasSeenAutoDefaultNudge=True)
        profile_dir = self.profile_dir("claude", "primary")
        state_path = profile_dir / ".claude.json"
        state_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "primary@invalid.example"}}),
            encoding="utf-8",
        )
        state_path.chmod(0o600)

        real_load = accounts._load_owned_claude_state

        def provider_writes_midway(path):
            """Stand in for Claude Code saving the file while this runs."""
            result = real_load(path)
            current = json.loads(state_path.read_text(encoding="utf-8"))
            current["lastReleaseNotesSeen"] = "2.1.233"
            state_path.write_text(json.dumps(current), encoding="utf-8")
            return result

        with mock.patch.object(
            accounts, "_load_owned_claude_state", side_effect=provider_writes_midway
        ):
            self.provision()

        stored = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("2.1.233", stored["lastReleaseNotesSeen"])  # not clobbered
        self.assertIs(True, stored["hasSeenAutoDefaultNudge"])  # and ours landed
        self.assertTrue(stored["hasCompletedOnboarding"])
        self.assertEqual(0o600, state_path.stat().st_mode & 0o777)

    def test_a_profile_enrolled_before_this_shipped_still_gets_the_marker(
        self,
    ) -> None:
        """The operator hit this on an ALREADY enrolled account.

        A fix that only covers profiles created from now on has not fixed it
        for them, so the marker is applied when a profile is resumed too.
        """
        self.write_default_state(hasSeenAutoDefaultNudge=True)
        profile_dir = self.profile_dir("claude", "primary")
        state_path = profile_dir / ".claude.json"
        # An old profile: onboarded long ago, no first-run marker.
        state_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {"emailAddress": "primary@invalid.example"},
                    "hasCompletedOnboarding": True,
                }
            ),
            encoding="utf-8",
        )
        state_path.chmod(0o600)
        self.seed_profiles(("claude", "primary", "primary@invalid.example"))

        with mock.patch.object(
            accounts,
            "probe_identity",
            return_value={
                "provider": "claude",
                "email": "primary@invalid.example",
                "plan": "max",
            },
        ):
            accounts.resume_profile(self.config, "claude", "primary")

        stored = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIs(True, stored["hasSeenAutoDefaultNudge"])

    def test_a_provider_value_this_code_does_not_understand_survives(self) -> None:
        """`projects` is the provider's. An unexpected shape is not ours to
        normalise away while adding an unrelated marker (Codex finding 7)."""
        self.write_default_state(
            hasSeenAutoDefaultNudge=True,
            projects={"/srv/trusted": {"hasTrustDialogAccepted": True}},
        )
        profile_dir = self.profile_dir("claude", "primary")
        state_path = profile_dir / ".claude.json"
        state_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {"emailAddress": "primary@invalid.example"},
                    "projects": "provider-owned-value",
                }
            ),
            encoding="utf-8",
        )
        state_path.chmod(0o600)

        stored = json.loads(self.provision().read_text(encoding="utf-8"))

        self.assertEqual("provider-owned-value", stored["projects"])
        self.assertIs(True, stored["hasSeenAutoDefaultNudge"])

    def test_a_provider_save_that_lands_after_the_final_read_is_not_lost(
        self,
    ) -> None:
        """The half of the race the re-read never covered (lane B F7, Codex 5).

        Re-reading before the replace narrows the window; it cannot close it,
        because there is always a gap after the LAST read. Claude saves this
        file as `.tmp.<pid>.<hex>` followed by a rename, so its save changes
        the inode -- which is what the compare-and-swap catches. The write is
        thrown away and retried against the new content rather than replacing
        it.
        """
        self.write_default_state(hasSeenAutoDefaultNudge=True)
        profile_dir = self.profile_dir("claude", "primary")
        state_path = profile_dir / ".claude.json"
        state_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "primary@invalid.example"}}),
            encoding="utf-8",
        )
        state_path.chmod(0o600)

        real_read = accounts.read_private_json
        reads = {"of_the_profile": 0}

        def claude_saves_after_the_read(path, **keywords):
            result = real_read(path, **keywords)
            if Path(path) != state_path:
                return result
            reads["of_the_profile"] += 1
            # Read 1 is the function's opening read; read 2 is the one inside
            # the write attempt, and the provider lands immediately after it --
            # in the gap a re-read can never cover.
            if reads["of_the_profile"] == 2:
                current = json.loads(state_path.read_text(encoding="utf-8"))
                current["lastReleaseNotesSeen"] = "2.1.233"
                scratch = state_path.parent / (state_path.name + ".tmp.9999.abc")
                scratch.write_text(json.dumps(current), encoding="utf-8")
                scratch.chmod(0o600)
                os.replace(scratch, state_path)  # exactly how Claude saves it
            return result

        with mock.patch.object(
            accounts, "read_private_json", side_effect=claude_saves_after_the_read
        ):
            self.provision()

        # If the injection never fired, or the swap never retried, this test
        # proves nothing at EITHER revision -- the false-differential trap the
        # round-1 lanes were caught by. Exactly three reads of the profile is
        # the signature of the swap working: the opening read, the attempt the
        # provider beat, and the retry that merged its key and won. Two would
        # mean the collision was never detected.
        self.assertEqual(3, reads["of_the_profile"])
        stored = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("2.1.233", stored["lastReleaseNotesSeen"])  # never lost
        self.assertIs(True, stored["hasSeenAutoDefaultNudge"])  # ours retried in
        self.assertTrue(stored["hasCompletedOnboarding"])
        self.assertEqual(0o600, state_path.stat().st_mode & 0o777)

    def test_nothing_is_written_while_claude_holds_its_own_config_lock(self) -> None:
        """Claude takes a mkdir lock at `<config>.lock` while it saves.

        This code does not TAKE that lock: Claude acquires it with retries 0, so
        a held lock does not make Claude wait, it makes Claude fall back to an
        unlocked write that skips its own re-read. Holding it would turn an
        ordinary collision into a guaranteed overwrite. Its value is as a
        signal, and the answer to the signal is to stand aside.
        """
        self.write_default_state(hasSeenAutoDefaultNudge=True)
        profile_dir = self.profile_dir("claude", "primary")
        state_path = profile_dir / ".claude.json"
        original = json.dumps(
            {"oauthAccount": {"emailAddress": "primary@invalid.example"}}
        )
        state_path.write_text(original, encoding="utf-8")
        state_path.chmod(0o600)
        (profile_dir / (state_path.name + ".lock")).mkdir()

        noise = io.StringIO()
        with contextlib.redirect_stderr(noise):
            self.provision()

        self.assertEqual(original, state_path.read_text(encoding="utf-8"))
        # The exact refusal, not any message this module happens to print.
        self.assertIn("is being written by Claude right now", noise.getvalue())
        self.assertNotIn("hasSeenAutoDefaultNudge", state_path.read_text("utf-8"))

    def test_a_default_state_past_the_old_one_MiB_cap_still_marks_the_offer(
        self,
    ) -> None:
        """Lane B F8. `~/.claude.json` grows with project history and never
        shrinks, and crossing the kit's registry cap used to stop the marker
        silently -- so the only symptom would be this bug coming back with no
        explanation. A real registry has been measured at 90,786 bytes."""
        self.write_default_state(
            hasSeenAutoDefaultNudge=True, filler="x" * (1024 * 1024 + 4096)
        )
        self.assertGreater(
            (self.home / ".claude.json").stat().st_size, accounts.MAX_REGISTRY_BYTES
        )

        stored = json.loads(self.provision().read_text(encoding="utf-8"))

        self.assertIs(True, stored["hasSeenAutoDefaultNudge"])

    def test_a_default_state_it_cannot_use_says_why(self) -> None:
        """Every unmarked offer now has a stated reason. None of them was
        audible before, and silence is how F8 would have reached them."""
        (self.home / ".claude.json").write_text("{not json", encoding="utf-8")
        (self.home / ".claude.json").chmod(0o644)

        noise = io.StringIO()
        with contextlib.redirect_stderr(noise):
            stored = json.loads(self.provision().read_text(encoding="utf-8"))

        self.assertNotIn("hasSeenAutoDefaultNudge", stored)
        self.assertIn("not marking", noise.getvalue())
        self.assertIn("not valid JSON", noise.getvalue())

    def test_kill_switch_blocks_enroll_launch_and_switch_prepare(self) -> None:
        self.seed_profiles(
            ("claude", "source", "source@example.com"),
            ("claude", "target", "target@example.com"),
        )
        accounts.bind(self.config, "claude", CLAUDE_UUID, "source", source="fixture")
        accounts.kill_switch_path(self.config).touch(mode=0o600)

        with mock.patch.object(accounts.subprocess, "run") as provider:
            with self.assertRaisesRegex(CollectionError, "disabled"):
                accounts.enroll(self.config, "claude", "newone", "newone@example.com")
            with self.assertRaisesRegex(CollectionError, "disabled"):
                accounts.launch_profile(self.config, "claude", "source")
            with self.assertRaisesRegex(CollectionError, "disabled"):
                accounts.prepare_switch(
                    self.config,
                    "claude",
                    CLAUDE_UUID,
                    "source",
                    "target",
                    str(self.root),
                    "session-2",
                )
            provider.assert_not_called()
        with mock.patch.object(
            accounts,
            "probe_identity",
            return_value={
                "provider": "claude",
                "email": "source@example.com",
                "plan": "max",
            },
        ):
            self.assertEqual(
                "source",
                accounts.resume_profile(self.config, "claude", "source")["alias"],
            )


class AccountSwitchTransactionTests(unittest.TestCase):
    setUp = AccountProfileTests.setUp
    tearDown = AccountProfileTests.tearDown
    profile_dir = AccountProfileTests.profile_dir
    seed_profiles = AccountProfileTests.seed_profiles
    write_feed = AccountProfileTests.write_feed

    def seed_switch(self, provider: str, uuid: str) -> tuple[Path, Path]:
        self.seed_profiles(
            (provider, "source", "source@example.com"),
            (provider, "target", "target@example.com"),
        )
        key = "accounts" if provider == "claude" else "codex_accounts"
        self.write_feed(
            self.roster,
            {
                "ts": int(accounts.time.time()),
                key: [
                    {
                        "email": "target@example.com",
                        "health": "ok",
                        "serving": True,
                    }
                ],
            },
        )
        accounts.bind(self.config, provider, uuid, "source", source="fixture")
        source = self.profile_root / provider / "source"
        target = self.profile_root / provider / "target"
        if provider == "claude":
            artifact = source / "projects" / "-srv-test" / f"{uuid}.jsonl"
        else:
            artifact = (
                source
                / "sessions"
                / "2026"
                / "08"
                / "10"
                / f"rollout-test-{uuid}.jsonl"
            )
        artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        artifact.write_text("source history\n", encoding="utf-8")
        artifact.chmod(0o600)
        return source, target

    def test_prepare_apply_and_commit_move_exact_claude_history_and_binding(
        self,
    ) -> None:
        source, target = self.seed_switch("claude", CLAUDE_UUID)
        transcript = Path("projects") / "-srv-test" / f"{CLAUDE_UUID}.jsonl"

        prepared = accounts.prepare_switch(
            self.config,
            "claude",
            CLAUDE_UUID.upper(),
            "source",
            "target",
            str(self.root),
            "session-2",
        )
        applied = accounts.apply_switch(self.config, prepared["txid"])

        self.assertEqual("target", applied["alias"])
        self.assertEqual("source history\n", (target / transcript).read_text())
        self.assertTrue((source / transcript).exists())
        self.assertEqual(
            "target_launching",
            accounts.transaction(self.config, prepared["txid"])["status"],
        )

        with mock.patch.object(
            accounts,
            "probe_identity",
            return_value={
                "provider": "claude",
                "email": "target@example.com",
                "plan": "max",
            },
        ):
            committed = accounts.commit_switch(self.config, prepared["txid"])

        self.assertEqual("committed", committed["status"])
        self.assertFalse((source / transcript).exists())
        self.assertTrue((target / transcript).exists())
        self.assertEqual(
            "target",
            accounts.binding_for(self.config, "claude", CLAUDE_UUID)["alias"],
        )

    def test_rollback_restores_newest_codex_history_and_original_binding(self) -> None:
        source, target = self.seed_switch("codex", CODEX_UUID)
        rollout = (
            Path("sessions") / "2026" / "08" / "10" / f"rollout-test-{CODEX_UUID}.jsonl"
        )
        prepared = accounts.prepare_switch(
            self.config,
            "codex",
            CODEX_UUID,
            "source",
            "target",
            str(self.root),
            "session-8",
        )
        accounts.apply_switch(self.config, prepared["txid"])
        (target / rollout).write_text("target newest history\n", encoding="utf-8")
        # Model a commit interrupted after it published the target binding.
        accounts.bind(
            self.config,
            "codex",
            CODEX_UUID,
            "target",
            source="interrupted-commit",
        )

        rolled_back = accounts.rollback_switch(self.config, prepared["txid"])

        self.assertEqual("source", rolled_back["alias"])
        self.assertEqual("target newest history\n", (source / rollout).read_text())
        self.assertFalse((target / rollout).exists())
        self.assertEqual(
            "source",
            accounts.binding_for(self.config, "codex", CODEX_UUID)["alias"],
        )
        self.assertEqual(
            "account-switch-rollback",
            accounts.binding_for(self.config, "codex", CODEX_UUID)["binding_source"],
        )
        self.assertEqual(
            "rolled_back",
            accounts.transaction(self.config, prepared["txid"])["status"],
        )


class RulesSyncTests(unittest.TestCase):
    """One rules file has to reach every provider home and every profile.

    The interesting cases are not "did it copy": they are whether a rulebook's
    own text survives the write, whether adopting the sync destroys what a
    provider already relied on, and whether drift is actually reported.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-rules.")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.data = self.root / "data"
        self.config_home = self.root / "config"
        for path in (self.home, self.state, self.data, self.config_home):
            path.mkdir(mode=0o700, parents=True)
        (self.home / ".claude").mkdir(mode=0o700)
        (self.home / ".codex").mkdir(mode=0o700)
        self.registry = self.state / "accounts.json"
        self.profile_root = self.data / "session-kit" / "accounts"
        self.rules = self.config_home / "agent-rules" / "universal-rules.md"
        self.rules.parent.mkdir(mode=0o700, parents=True)
        self.config = {"state_dir": str(self.state)}
        self.environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_DATA_HOME": str(self.data),
                "XDG_CONFIG_HOME": str(self.config_home),
                "SESSION_KIT_ACCOUNT_REGISTRY": str(self.registry),
                "SESSION_KIT_ACCOUNT_ROOT": str(self.profile_root),
            },
            clear=False,
        )
        self.environment.start()
        os.environ.pop("SESSION_KIT_RULES_FILE", None)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def claude_rulebook(self) -> Path:
        return self.home / ".claude" / "CLAUDE.md"

    def codex_rulebook(self) -> Path:
        return self.home / ".codex" / "AGENTS.md"

    def write_default_rulebooks(self, claude: str, codex: str) -> None:
        self.claude_rulebook().write_text(claude, encoding="utf-8")
        self.codex_rulebook().write_text(codex, encoding="utf-8")

    def test_missing_rules_file_changes_nothing(self) -> None:
        self.write_default_rulebooks("claude text\n", "codex text\n")
        result = accounts.sync_rules(self.config)
        self.assertFalse(result["ok"])
        self.assertEqual(result["targets"], [])
        self.assertEqual(
            self.claude_rulebook().read_text(encoding="utf-8"), "claude text\n"
        )

    def test_first_sync_keeps_the_existing_rulebook_text(self) -> None:
        self.rules.write_text("# Shared\n\nbe careful\n", encoding="utf-8")
        self.write_default_rulebooks("provider only note\n", "codex only note\n")
        result = accounts.sync_rules(self.config)
        self.assertTrue(result["ok"])
        claude = self.claude_rulebook().read_text(encoding="utf-8")
        self.assertIn("be careful", claude)
        self.assertIn("provider only note", claude)
        codex = self.codex_rulebook().read_text(encoding="utf-8")
        self.assertIn("be careful", codex)
        self.assertIn("codex only note", codex)

    def test_resync_replaces_only_the_block(self) -> None:
        self.rules.write_text("first rule\n", encoding="utf-8")
        self.write_default_rulebooks("tail note\n", "codex tail\n")
        accounts.sync_rules(self.config)
        self.rules.write_text("second rule\n", encoding="utf-8")
        accounts.sync_rules(self.config)
        claude = self.claude_rulebook().read_text(encoding="utf-8")
        self.assertIn("second rule", claude)
        self.assertNotIn("first rule", claude)
        self.assertIn("tail note", claude)
        self.assertEqual(claude.count(accounts.RULES_END), 1)

    def test_a_symlinked_rulebook_is_skipped_by_name_not_aborted_on(self) -> None:
        self.rules.write_text("a rule\n", encoding="utf-8")
        self.write_default_rulebooks("claude tail\n", "codex tail\n")
        elsewhere = self.root / "dotfiles-CLAUDE.md"
        elsewhere.write_text("managed elsewhere\n", encoding="utf-8")
        self.claude_rulebook().unlink()
        self.claude_rulebook().symlink_to(elsewhere)
        result = accounts.sync_rules(self.config)
        self.assertTrue(result["ok"])
        states = {t["path"]: t["state"] for t in result["targets"]}
        self.assertEqual(states[str(self.claude_rulebook())], "skipped")
        # The link and its target are both untouched, and the other rulebook
        # was still written -- one refused target does not end the run.
        self.assertEqual(elsewhere.read_text(encoding="utf-8"), "managed elsewhere\n")
        self.assertIn("a rule", self.codex_rulebook().read_text(encoding="utf-8"))

    def test_a_rulebook_that_is_not_text_is_skipped_not_a_traceback(self) -> None:
        self.rules.write_text("a rule\n", encoding="utf-8")
        self.write_default_rulebooks("claude tail\n", "codex tail\n")
        self.claude_rulebook().write_bytes(b"\xff\xfe\x00 not text")
        result = accounts.sync_rules(self.config)
        self.assertTrue(result["ok"])
        states = {t["path"]: t["state"] for t in result["targets"]}
        self.assertEqual(states[str(self.claude_rulebook())], "skipped")
        self.assertEqual(self.claude_rulebook().read_bytes(), b"\xff\xfe\x00 not text")
        self.assertIn("a rule", self.codex_rulebook().read_text(encoding="utf-8"))

    def test_check_reports_drift_without_writing(self) -> None:
        self.rules.write_text("first rule\n", encoding="utf-8")
        self.write_default_rulebooks("tail\n", "tail\n")
        accounts.sync_rules(self.config)
        self.rules.write_text("second rule\n", encoding="utf-8")
        before = self.claude_rulebook().read_text(encoding="utf-8")
        result = accounts.sync_rules(self.config, check=True)
        self.assertEqual(result["drifted"], 2)
        self.assertEqual(self.claude_rulebook().read_text(encoding="utf-8"), before)

    def test_check_is_clean_right_after_a_sync(self) -> None:
        self.rules.write_text("a rule\n", encoding="utf-8")
        self.write_default_rulebooks("tail\n", "tail\n")
        accounts.sync_rules(self.config)
        result = accounts.sync_rules(self.config, check=True)
        self.assertEqual(result["drifted"], 0)
        self.assertTrue(all(item["state"] == "current" for item in result["targets"]))

    def test_enrolled_profiles_are_included(self) -> None:
        self.rules.write_text("a rule\n", encoding="utf-8")
        self.write_default_rulebooks("tail\n", "tail\n")
        profile = self.profile_root / "claude" / "work"
        profile.mkdir(mode=0o700, parents=True)
        (profile / "CLAUDE.md").write_text("profile tail\n", encoding="utf-8")
        with mock.patch.object(
            accounts,
            "list_profiles",
            return_value=[
                {
                    "provider": "claude",
                    "alias": "work",
                    "profile_dir": str(profile),
                }
            ],
        ):
            result = accounts.sync_rules(self.config)
        labels = {item["label"] for item in result["targets"]}
        self.assertIn("claude:work", labels)
        text = (profile / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("a rule", text)
        self.assertIn("profile tail", text)

    def test_rules_file_can_be_pointed_elsewhere(self) -> None:
        elsewhere = self.root / "elsewhere.md"
        elsewhere.write_text("relocated rule\n", encoding="utf-8")
        os.environ["SESSION_KIT_RULES_FILE"] = str(elsewhere)
        try:
            self.write_default_rulebooks("tail\n", "tail\n")
            accounts.sync_rules(self.config)
        finally:
            os.environ.pop("SESSION_KIT_RULES_FILE", None)
        self.assertIn(
            "relocated rule", self.claude_rulebook().read_text(encoding="utf-8")
        )

    def test_an_implausibly_large_rules_file_is_refused(self) -> None:
        self.rules.write_text("x" * (accounts.MAX_RULES_BYTES + 1), encoding="utf-8")
        self.write_default_rulebooks("tail\n", "tail\n")
        with self.assertRaises(CollectionError):
            accounts.sync_rules(self.config)


if __name__ == "__main__":
    unittest.main()
