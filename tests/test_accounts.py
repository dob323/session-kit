from __future__ import annotations

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
        transcript = self.profile_root / "claude" / "source" / "projects" / "repo" / f"{CLAUDE_UUID}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n", encoding="utf-8")
        transcript.chmod(0o600)

        found = accounts.source_profile_for_thread(
            self.config, "claude", CLAUDE_UUID
        )

        self.assertEqual("source", found["alias"])
        self.assertEqual(
            "legacy-first-switch",
            accounts.binding_for(self.config, "claude", CLAUDE_UUID)[
                "binding_source"
            ],
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
        self.assertEqual("most weekly allowance remains", claude["recommendation_reason"])
        self.assertTrue(next(row for row in claude["choices"] if row["alias"] == "backup")["eligible"])
        self.assertIsNone(codex["recommendation"])
        self.assertFalse(any(row["recommended"] for row in codex["choices"]))

    def test_configured_feeds_are_private_and_used_without_environment_overrides(self) -> None:
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
            with self.assertRaisesRegex(CollectionError, "not healthy"):
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
        self.assertEqual("primary@invalid.example", stored["oauthAccount"]["emailAddress"])
        self.assertTrue(
            stored["projects"]["/srv/trusted"]["hasTrustDialogAccepted"]
        )
        self.assertNotIn("/srv/untrusted", stored["projects"])
        self.assertEqual(0o600, state_path.stat().st_mode & 0o777)

    def test_kill_switch_blocks_enroll_launch_and_switch_prepare(self) -> None:
        self.seed_profiles(
            ("claude", "source", "source@example.com"),
            ("claude", "target", "target@example.com"),
        )
        accounts.bind(
            self.config, "claude", CLAUDE_UUID, "source", source="fixture"
        )
        accounts.kill_switch_path(self.config).touch(mode=0o600)

        with mock.patch.object(accounts.subprocess, "run") as provider:
            with self.assertRaisesRegex(CollectionError, "disabled"):
                accounts.enroll(
                    self.config, "claude", "newone", "newone@example.com"
                )
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
            artifact = source / "sessions" / "2026" / "08" / "10" / f"rollout-test-{uuid}.jsonl"
        artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        artifact.write_text("source history\n", encoding="utf-8")
        artifact.chmod(0o600)
        return source, target

    def test_prepare_apply_and_commit_move_exact_claude_history_and_binding(self) -> None:
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
            Path("sessions")
            / "2026"
            / "08"
            / "10"
            / f"rollout-test-{CODEX_UUID}.jsonl"
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
            accounts.binding_for(self.config, "codex", CODEX_UUID)[
                "binding_source"
            ],
        )
        self.assertEqual(
            "rolled_back",
            accounts.transaction(self.config, prepared["txid"])["status"],
        )


if __name__ == "__main__":
    unittest.main()
