"""Rotation advice reaches both providers, not only Claude.

The roster feed has always carried a ``codex_accounts`` list, but the advice
beside it was read only when the caller asked about Claude. A Codex account
could be enabled, healthy, and serving and still never be recommended — the
audit finding these tests pin down.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib.sessionkit_inventory import accounts


NOW = 1_800_000_000


class ProviderAdviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-advice.")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.data = self.root / "data"
        for path in (self.home, self.state, self.data):
            path.mkdir(mode=0o700)
        self.profile_root = self.data / "session-kit" / "accounts"
        self.roster = self.root / "roster.json"
        self.advice = self.root / "advice.json"
        self.config = {"state_dir": str(self.state)}
        self.environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_DATA_HOME": str(self.data),
                "SESSION_KIT_ACCOUNT_REGISTRY": str(self.state / "accounts.json"),
                "SESSION_KIT_ACCOUNT_ROOT": str(self.profile_root),
                "SESSION_KIT_ACCOUNT_ROSTER": str(self.roster),
                "SESSION_KIT_ROTATION_ADVICE": str(self.advice),
            },
            clear=False,
        )
        self.environment.start()
        os.environ.pop("SESSION_KIT_ACCOUNT_ADVICE_MAX_AGE_SECONDS", None)
        self.seed()
        self.write_roster()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def seed(self) -> None:
        profiles = {}
        for provider, alias, email in (
            ("claude", "one", "one@invalid.example"),
            ("claude", "two", "two@invalid.example"),
            ("codex", "three", "three@invalid.example"),
            ("codex", "four", "four@invalid.example"),
        ):
            profile_dir = self.profile_root / provider / alias
            profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
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

    def write_roster(self) -> None:
        healthy = {"health": "ok", "serving": True}
        self.write(
            self.roster,
            {
                "ts": NOW,
                "accounts": [
                    dict(healthy, email="one@invalid.example"),
                    dict(healthy, email="two@invalid.example"),
                ],
                "codex_accounts": [
                    dict(healthy, email="three@invalid.example"),
                    dict(healthy, email="four@invalid.example"),
                ],
            },
        )

    def write(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)

    def choices(self, provider: str) -> dict:
        with mock.patch.object(accounts.time, "time", return_value=NOW):
            return accounts.account_choices(self.config, provider)

    def test_a_provider_keyed_recommendation_reaches_codex(self) -> None:
        self.write(
            self.advice,
            {
                "ts": NOW,
                "use_now": {"account": "one@invalid.example", "why": "claude pick"},
                "use_now_codex": {
                    "account": "four@invalid.example",
                    "why": "codex pick",
                },
            },
        )

        claude = self.choices("claude")
        codex = self.choices("codex")

        self.assertEqual("one", claude["recommendation"])
        self.assertEqual("claude pick", claude["recommendation_reason"])
        self.assertEqual("four", codex["recommendation"])
        self.assertEqual("codex pick", codex["recommendation_reason"])

    def test_a_nested_providers_block_works_too(self) -> None:
        self.write(
            self.advice,
            {
                "ts": NOW,
                "providers": {
                    "codex": {
                        "use_now": {
                            "account": "three@invalid.example",
                            "why": "most left",
                        }
                    }
                },
            },
        )

        codex = self.choices("codex")

        self.assertEqual("three", codex["recommendation"])
        self.assertEqual("most left", codex["recommendation_reason"])

    def test_the_legacy_top_level_key_stays_claude_only(self) -> None:
        """A single-provider feed means Claude; reading it for Codex would
        recommend a Claude account for a Codex session."""
        self.write(
            self.advice,
            {"ts": NOW, "use_now": {"account": "one@invalid.example", "why": "x"}},
        )

        self.assertEqual("one", self.choices("claude")["recommendation"])
        self.assertIsNone(self.choices("codex")["recommendation"])

    def test_the_reason_is_read_under_either_spelling(self) -> None:
        """Feeds ship ``reason``; the reader only accepted ``why``, so every
        recommendation arrived with a blank explanation."""
        self.write(
            self.advice,
            {
                "ts": NOW,
                "use_now": {
                    "account": "two@invalid.example",
                    "reason": "soonest weekly reset with most unused",
                },
            },
        )

        claude = self.choices("claude")

        self.assertEqual("two", claude["recommendation"])
        self.assertEqual(
            "soonest weekly reset with most unused", claude["recommendation_reason"]
        )

    def test_stale_advice_recommends_nobody_for_either_provider(self) -> None:
        self.write(
            self.advice,
            {
                "ts": NOW - 86_400,
                "use_now": {"account": "one@invalid.example", "why": "old"},
                "use_now_codex": {"account": "three@invalid.example", "why": "old"},
            },
        )

        self.assertIsNone(self.choices("claude")["recommendation"])
        self.assertIsNone(self.choices("codex")["recommendation"])
        self.assertFalse(self.choices("codex")["advice_fresh"])

    def test_advice_naming_an_unhealthy_account_does_not_recommend_it(self) -> None:
        self.write(
            self.roster,
            {
                "ts": NOW,
                "codex_accounts": [
                    {
                        "email": "three@invalid.example",
                        "health": "expired",
                        "serving": False,
                    }
                ],
            },
        )
        self.write(
            self.advice,
            {
                "ts": NOW,
                "use_now_codex": {"account": "three@invalid.example", "why": "stale"},
            },
        )

        codex = self.choices("codex")

        self.assertIsNone(codex["recommendation"])
        self.assertFalse(any(row["recommended"] for row in codex["choices"]))

    def test_a_malformed_advice_block_is_ignored_rather_than_crashing(self) -> None:
        self.write(self.advice, {"ts": NOW, "use_now_codex": ["not", "an", "object"]})

        self.assertIsNone(self.choices("codex")["recommendation"])
        self.assertEqual("", self.choices("codex")["recommendation_reason"])


if __name__ == "__main__":
    unittest.main()
