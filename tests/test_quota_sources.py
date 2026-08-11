"""The quota readers, and the provenance rules that keep their output honest."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib.sessionkit_inventory import accounts
from lib.sessionkit_supervisor import quota_sources as qs


NOW = 1_800_000_000


def iso(unix: int) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(unix, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    )


class ReadingRulesTests(unittest.TestCase):
    def reading(self, **overrides: object) -> qs.QuotaReading:
        values: dict[str, object] = {
            "provider": "claude",
            "account": "primary",
            "window": qs.WEEKLY,
            "source": "test",
            "confidence": qs.MEASURED,
            "observed_at_unix": NOW,
        }
        values.update(overrides)
        return qs.QuotaReading(**values)  # type: ignore[arg-type]

    def test_reading_refuses_an_unknown_window_or_confidence(self) -> None:
        with self.assertRaises(qs.QuotaError):
            self.reading(window="monthly")
        with self.assertRaises(qs.QuotaError):
            self.reading(confidence="guessed")

    def test_best_prefers_provenance_then_recency_and_demotes_stale(self) -> None:
        snapshot = qs.QuotaSnapshot(taken_at_unix=NOW, max_age_seconds=3600)
        snapshot.readings.extend(
            [
                self.reading(confidence=qs.OBSERVED, source="inferred"),
                self.reading(confidence=qs.FEED, source="roster"),
                self.reading(confidence=qs.MEASURED, source="provider"),
            ]
        )

        best = snapshot.best("claude", "primary", qs.WEEKLY)

        self.assertIsNotNone(best)
        assert best is not None
        self.assertEqual("provider", best.source)

        # A stale measurement loses to a fresh inference: the number the kit
        # can still stand behind beats a better-sourced one from hours ago.
        stale = qs.QuotaSnapshot(taken_at_unix=NOW, max_age_seconds=3600)
        stale.readings.extend(
            [
                self.reading(
                    confidence=qs.MEASURED,
                    source="provider",
                    observed_at_unix=NOW - 7200,
                ),
                self.reading(confidence=qs.OBSERVED, source="inferred"),
            ]
        )

        chosen = stale.best("claude", "primary", qs.WEEKLY)

        assert chosen is not None
        self.assertEqual("inferred", chosen.source)


class CodexRateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-quota.")
        self.root = Path(self.temporary.name)
        self.home = self.root / "codex"
        (self.home / "sessions" / "2026" / "08" / "11").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def ref(self) -> qs.AccountRef:
        return qs.AccountRef(provider="codex", alias="primary", home=self.home)

    def write_rollout(self, name: str, *records: object) -> Path:
        path = self.home / "sessions" / "2026" / "08" / "11" / f"rollout-{name}.jsonl"
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        return path

    def token_count(self, limits: dict[str, object], *, at: int = NOW) -> dict:
        return {
            "timestamp": iso(at),
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": limits},
        }

    def test_reads_both_windows_the_provider_published(self) -> None:
        self.write_rollout(
            "one",
            {"timestamp": iso(NOW - 60), "type": "response_item"},
            self.token_count(
                {
                    "primary": {
                        "used_percent": 41.0,
                        "window_minutes": 10080,
                        "resets_at": NOW + 3600,
                    },
                    "secondary": {
                        "used_percent": 7.5,
                        "window_minutes": 300,
                        "resets_at": NOW + 600,
                    },
                }
            ),
        )

        readings = qs.CodexRateLimitSource().read(self.ref(), now=NOW)

        by_window = {reading.window: reading for reading in readings}
        self.assertEqual({qs.WEEKLY, qs.FIVE_HOUR}, set(by_window))
        self.assertAlmostEqual(0.41, by_window[qs.WEEKLY].used_fraction or 0.0)
        self.assertAlmostEqual(0.075, by_window[qs.FIVE_HOUR].used_fraction or 0.0)
        self.assertEqual(NOW + 600, by_window[qs.FIVE_HOUR].resets_at_unix)
        self.assertEqual(qs.MEASURED, by_window[qs.WEEKLY].confidence)
        self.assertFalse(by_window[qs.WEEKLY].exhausted)

    def test_last_block_in_the_file_wins_and_a_limit_hit_is_exhausted(self) -> None:
        self.write_rollout(
            "one",
            self.token_count(
                {"primary": {"used_percent": 10.0, "window_minutes": 10080}},
                at=NOW - 600,
            ),
            self.token_count(
                {
                    "primary": {"used_percent": 99.0, "window_minutes": 10080},
                    "rate_limit_reached_type": "weekly",
                },
                at=NOW - 30,
            ),
        )

        readings = qs.CodexRateLimitSource().read(self.ref(), now=NOW)

        self.assertEqual(1, len(readings))
        self.assertAlmostEqual(0.99, readings[0].used_fraction or 0.0)
        self.assertTrue(readings[0].exhausted)

    def test_ignores_other_providers_and_an_empty_home(self) -> None:
        self.write_rollout(
            "one",
            self.token_count(
                {"primary": {"used_percent": 5.0, "window_minutes": 10080}}
            ),
        )
        source = qs.CodexRateLimitSource()

        claude = qs.AccountRef(provider="claude", alias="primary", home=self.home)
        missing = qs.AccountRef(
            provider="codex", alias="primary", home=self.root / "absent"
        )

        self.assertEqual([], source.read(claude, now=NOW))
        self.assertEqual([], source.read(missing, now=NOW))

    def test_a_malformed_line_does_not_lose_the_readable_one(self) -> None:
        path = self.home / "sessions" / "2026" / "08" / "11" / "rollout-broken.jsonl"
        path.write_text(
            json.dumps(
                self.token_count(
                    {"primary": {"used_percent": 22.0, "window_minutes": 10080}}
                )
            )
            + "\n{ this is not json but mentions rate_limits\n",
            encoding="utf-8",
        )

        readings = qs.CodexRateLimitSource().read(self.ref(), now=NOW)

        self.assertEqual(1, len(readings))
        self.assertAlmostEqual(0.22, readings[0].used_fraction or 0.0)


class ClaudeTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-quota.")
        self.root = Path(self.temporary.name)
        self.home = self.root / "claude"
        self.projects = self.home / "projects" / "-work-thing"
        self.projects.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def ref(self, alias: str = "primary") -> qs.AccountRef:
        return qs.AccountRef(provider="claude", alias=alias, home=self.home)

    def turn(self, *, at: int, tokens: int) -> dict:
        return {
            "type": "assistant",
            "timestamp": iso(at),
            "message": {
                "usage": {
                    "input_tokens": tokens,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                }
            },
        }

    def write(self, name: str, *records: object) -> None:
        path = self.projects / f"{name}.jsonl"
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        os.utime(path, (NOW, NOW))

    def test_reports_a_comparable_rate_never_a_fabricated_percentage(self) -> None:
        self.write(
            "session",
            self.turn(at=NOW - 3600, tokens=1000),
            self.turn(at=NOW - 1800, tokens=3000),
        )

        readings = qs.ClaudeTranscriptUsageSource().read(self.ref(), now=NOW)

        by_window = {reading.window: reading for reading in readings}
        self.assertEqual({qs.FIVE_HOUR, qs.WEEKLY}, set(by_window))
        for reading in readings:
            # Claude publishes no local allowance: a percentage here would be
            # invented, so the fraction stays unknown and the load is a rate.
            self.assertIsNone(reading.used_fraction)
            self.assertEqual("tokens/h", reading.load_unit)
            self.assertEqual(qs.OBSERVED, reading.confidence)
        # 4000 tokens over the one hour the scan reached back.
        self.assertAlmostEqual(4000.0, by_window[qs.FIVE_HOUR].load_units or 0.0, 0)
        self.assertIn("scan reached back", by_window[qs.WEEKLY].detail)

    def test_a_rate_is_per_hour_of_coverage_not_a_raw_total(self) -> None:
        """Unequal sample spans must not rank the longer sample as busier."""
        self.write(
            "busy",
            self.turn(at=NOW - 3600, tokens=10_000),
        )
        busy = qs.ClaudeTranscriptUsageSource().read(self.ref(), now=NOW)

        self.projects_alt = self.root / "other" / "projects" / "-work-thing"
        self.projects_alt.mkdir(parents=True)
        path = self.projects_alt / "quiet.jsonl"
        path.write_text(
            json.dumps(self.turn(at=NOW - 4 * 3600, tokens=12_000)) + "\n",
            encoding="utf-8",
        )
        os.utime(path, (NOW, NOW))
        quiet = qs.ClaudeTranscriptUsageSource().read(
            qs.AccountRef(provider="claude", alias="quiet", home=self.root / "other"),
            now=NOW,
        )

        busy_rate = next(r for r in busy if r.window == qs.FIVE_HOUR).load_units or 0.0
        quiet_rate = (
            next(r for r in quiet if r.window == qs.FIVE_HOUR).load_units or 0.0
        )

        # The quiet account spent MORE tokens in total but over four times the
        # span. By rate the busy account is correctly the heavier one.
        self.assertGreater(busy_rate, quiet_rate)

    def refusal(self, *, at: int, text: str) -> dict:
        return {
            "type": "assistant",
            "timestamp": iso(at),
            "error": "rate_limit",
            "apiErrorStatus": 429,
            "message": {"usage": {}, "content": [{"type": "text", "text": text}]},
        }

    def test_a_refusal_inside_the_short_window_marks_the_account_out(self) -> None:
        self.write(
            "session",
            self.turn(at=NOW - 7200, tokens=500),
            {
                "type": "assistant",
                "timestamp": iso(NOW - 600),
                "error": "rate_limit",
                "apiErrorStatus": 429,
                "message": {"usage": {}},
            },
        )

        readings = qs.ClaudeTranscriptUsageSource().read(self.ref(), now=NOW)

        short = next(r for r in readings if r.window == qs.FIVE_HOUR)
        week = next(r for r in readings if r.window == qs.WEEKLY)
        self.assertTrue(short.exhausted)
        self.assertEqual(1.0, short.used_fraction)
        self.assertIn("refused the session limit", short.detail)
        # An unreadable refusal is read as the short window, which expires on
        # its own, rather than as a week-long outage.
        self.assertFalse(week.exhausted)

    def test_the_refusal_text_supplies_the_window_and_the_reset_time(self) -> None:
        """Claude Code states both; guessing either would be a worse answer."""
        self.write(
            "session",
            self.turn(at=NOW - 3600, tokens=10),
            self.refusal(
                at=NOW - 300,
                text="You've hit your session limit · resets 2:50am (America/Chicago)",
            ),
        )

        readings = qs.ClaudeTranscriptUsageSource().read(self.ref(), now=NOW)

        short = next(r for r in readings if r.window == qs.FIVE_HOUR)
        self.assertTrue(short.exhausted)
        self.assertIsNotNone(short.resets_at_unix)
        assert short.resets_at_unix is not None
        self.assertGreater(short.resets_at_unix, NOW - 300)
        # A session limit resets within hours, never days.
        self.assertLess(short.resets_at_unix - (NOW - 300), 24 * 3600)
        self.assertIn("resets at unix", short.detail)

    def test_a_weekly_refusal_does_not_masquerade_as_the_short_window(self) -> None:
        self.write(
            "session",
            self.turn(at=NOW - 3600, tokens=10),
            self.refusal(
                at=NOW - 300,
                text="You've hit your weekly limit · resets Jul 24, 11pm (America/Chicago)",
            ),
        )

        readings = qs.ClaudeTranscriptUsageSource().read(self.ref(), now=NOW)

        week = next(r for r in readings if r.window == qs.WEEKLY)
        short = next(r for r in readings if r.window == qs.FIVE_HOUR)
        self.assertTrue(week.exhausted)
        self.assertFalse(short.exhausted)

    def test_one_model_s_limit_does_not_strand_the_whole_account(self) -> None:
        self.write(
            "session",
            self.turn(at=NOW - 3600, tokens=10),
            self.refusal(
                at=NOW - 300,
                text=(
                    "You've reached your Fable 5 limit. Run /usage-credits to "
                    "continue or switch models with /model."
                ),
            ),
        )

        readings = qs.ClaudeTranscriptUsageSource().read(self.ref(), now=NOW)

        account_wide = [row for row in readings if not row.model_hint]
        scoped = [row for row in readings if row.model_hint]
        self.assertFalse(any(row.exhausted for row in account_wide))
        self.assertEqual(1, len(scoped))
        self.assertEqual("fable5", scoped[0].model_hint)
        self.assertTrue(scoped[0].exhausted)

    def test_a_billed_turn_after_a_refusal_clears_it(self) -> None:
        """The account answered, so whatever ran out has come back."""
        self.write(
            "session",
            self.refusal(
                at=NOW - 1800,
                text="You've hit your session limit · resets 2:50am (America/Chicago)",
            ),
            self.turn(at=NOW - 60, tokens=500),
        )

        readings = qs.ClaudeTranscriptUsageSource().read(self.ref(), now=NOW)

        self.assertFalse(any(reading.exhausted for reading in readings))

    def test_a_refusal_with_nothing_after_it_still_binds(self) -> None:
        self.write(
            "session",
            self.turn(at=NOW - 1800, tokens=500),
            self.refusal(
                at=NOW - 60,
                text="You've hit your session limit · resets 2:50am (America/Chicago)",
            ),
        )

        readings = qs.ClaudeTranscriptUsageSource().read(self.ref(), now=NOW)

        short = next(r for r in readings if r.window == qs.FIVE_HOUR)
        self.assertTrue(short.exhausted)

    def test_a_refusal_past_its_stated_reset_no_longer_binds(self) -> None:
        """Tonight's window flip: once the stated time passes, the account is back."""
        refusal = qs._Refusal(
            at_unix=NOW - 3600, window=qs.FIVE_HOUR, resets_at_unix=NOW - 60
        )

        self.assertFalse(refusal.binding(NOW))
        self.assertTrue(refusal.binding(NOW - 120))

    def test_an_unresolvable_zone_leaves_the_reset_unknown_not_invented(self) -> None:
        self.assertIsNone(qs.parse_reset("2:50am", "Nowhere/Imaginary", NOW))
        self.assertIsNone(qs.parse_reset("half past two", "America/Chicago", NOW))

    def test_a_refusal_older_than_the_short_window_is_not_held_against_it(self) -> None:
        self.write(
            "session",
            {
                "type": "assistant",
                "timestamp": iso(NOW - 6 * 3600),
                "error": "rate_limit",
                "apiErrorStatus": 429,
                "message": {"usage": {}},
            },
            self.turn(at=NOW - 60, tokens=100),
        )

        readings = qs.ClaudeTranscriptUsageSource().read(self.ref(), now=NOW)

        self.assertFalse(any(reading.exhausted for reading in readings))

    def test_records_older_than_the_week_are_not_counted(self) -> None:
        self.write("session", self.turn(at=NOW - 30 * 24 * 3600, tokens=99_000))

        self.assertEqual([], qs.ClaudeTranscriptUsageSource().read(self.ref(), now=NOW))

    def test_only_the_tail_of_a_large_transcript_is_read(self) -> None:
        path = self.projects / "huge.jsonl"
        filler = json.dumps(self.turn(at=NOW - 4 * 3600, tokens=1)) + "\n"
        recent = json.dumps(self.turn(at=NOW - 60, tokens=7)) + "\n"
        path.write_text(filler * 4000 + recent, encoding="utf-8")
        os.utime(path, (NOW, NOW))

        source = qs.ClaudeTranscriptUsageSource(tail_bytes=2048)
        readings = source.read(self.ref(), now=NOW)

        short = next(r for r in readings if r.window == qs.FIVE_HOUR)
        self.assertIn("bounded scan", short.detail)
        # The whole file would total far more than the tail's few turns.
        self.assertLess(short.load_units or 0.0, 4000.0)


class AccountFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-feed.")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.data = self.root / "data"
        for path in (self.home, self.state, self.data):
            path.mkdir(mode=0o700)
        self.registry = self.state / "accounts.json"
        self.profile_root = self.data / "session-kit" / "accounts"
        self.roster = self.root / "roster.json"
        self.advice = self.root / "advice.json"
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
        os.environ.pop("SESSION_KIT_ACCOUNT_ADVICE_MAX_AGE_SECONDS", None)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def seed(self, *rows: tuple[str, str, str]) -> None:
        profiles = {}
        for provider, alias, email in rows:
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

    def write_roster(self, ts: int) -> None:
        self.roster.write_text(
            json.dumps(
                {
                    "ts": ts,
                    "accounts": [
                        {
                            "email": "one@invalid.example",
                            "health": "ok",
                            "serving": True,
                            "u5h": 0.6,
                            "u7d": 0.2,
                        }
                    ],
                    "codex_accounts": [
                        {
                            "email": "two@invalid.example",
                            "health": "ok",
                            "serving": True,
                            "u7d": 0.05,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.roster.chmod(0o600)

    def test_both_providers_get_readings_from_the_same_roster(self) -> None:
        self.seed(
            ("claude", "one", "one@invalid.example"),
            ("codex", "two", "two@invalid.example"),
        )
        self.write_roster(NOW)
        source = qs.AccountFeedQuotaSource(self.config)

        with mock.patch.object(accounts.time, "time", return_value=NOW):
            claude = source.read(qs.AccountRef(provider="claude", alias="one"), now=NOW)
            codex = source.read(qs.AccountRef(provider="codex", alias="two"), now=NOW)

        self.assertEqual(
            {qs.FIVE_HOUR, qs.WEEKLY}, {reading.window for reading in claude}
        )
        # The Codex row carries only a weekly figure, and that is what it gets:
        # a missing window is absent, never a zero standing in for one.
        self.assertEqual([qs.WEEKLY], [reading.window for reading in codex])
        self.assertAlmostEqual(0.05, codex[0].used_fraction or 0.0)
        self.assertEqual(qs.FEED, codex[0].confidence)

    def test_a_percentage_spelled_as_a_whole_number_is_not_read_as_spent(self) -> None:
        """A roster writing 27 for 27% must not read as 100% used."""
        self.seed(("claude", "one", "one@invalid.example"))
        self.roster.write_text(
            json.dumps(
                {
                    "ts": NOW,
                    "accounts": [
                        {
                            "email": "one@invalid.example",
                            "health": "ok",
                            "serving": True,
                            "u7d": 27,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.roster.chmod(0o600)
        source = qs.AccountFeedQuotaSource(self.config)

        with mock.patch.object(accounts.time, "time", return_value=NOW):
            readings = source.read(
                qs.AccountRef(provider="claude", alias="one"), now=NOW
            )

        self.assertAlmostEqual(0.27, readings[0].used_fraction or 0.0)
        self.assertFalse(readings[0].exhausted)

    def test_the_roster_is_read_once_per_provider_per_pass(self) -> None:
        """Every account of a provider answers from the same roster file."""
        self.seed(
            ("claude", "one", "one@invalid.example"),
            ("claude", "two", "two@invalid.example"),
        )
        self.write_roster(NOW)
        source = qs.AccountFeedQuotaSource(self.config)
        calls = []
        real = accounts.account_choices

        def counted(config, provider):
            calls.append(provider)
            return real(config, provider)

        with mock.patch.object(accounts.time, "time", return_value=NOW):
            with mock.patch.object(accounts, "account_choices", counted):
                for alias in ("one", "two"):
                    source.read(qs.AccountRef(provider="claude", alias=alias), now=NOW)
                # A later pass must not reuse the earlier answer: roster
                # freshness is exactly what this reader exists to check.
                source.read(qs.AccountRef(provider="claude", alias="one"), now=NOW + 1)

        self.assertEqual(["claude", "claude"], calls)

    def test_a_stale_roster_produces_nothing(self) -> None:
        self.seed(("claude", "one", "one@invalid.example"))
        self.write_roster(NOW - 86_400)
        source = qs.AccountFeedQuotaSource(self.config)

        with mock.patch.object(accounts.time, "time", return_value=NOW):
            readings = source.read(
                qs.AccountRef(provider="claude", alias="one"), now=NOW
            )

        self.assertEqual([], readings)

    def test_account_refs_include_a_provider_with_no_registered_profile(self) -> None:
        """Codex must stay schedulable without an isolated profile.

        Leaving a signed-in provider out of the candidate list is exactly the
        Claude-only bias this engine exists to remove: it would look like an
        even contest that Codex could never enter.
        """
        self.seed(("claude", "one", "one@invalid.example"))
        (self.home / ".codex").mkdir(mode=0o700)

        refs = qs.account_refs(self.config)

        self.assertIn("codex:default", [ref.key for ref in refs])
        codex = next(ref for ref in refs if ref.provider == "codex")
        self.assertFalse(codex.registered)
        self.assertTrue(codex.enabled)


class ModuleResolutionTests(unittest.TestCase):
    """One file, two import names, two module objects — pick our own.

    The kit runs with ``lib`` on ``sys.path`` and the tests run with the repo
    root on it, so ``sessionkit_inventory.accounts`` and
    ``lib.sessionkit_inventory.accounts`` are the same file loaded twice, with
    separate module state. Any code that picks by a fixed name order will,
    once some other module has imported the other spelling, silently start
    talking to a different copy than its caller.
    """

    def test_the_accounts_module_comes_from_our_own_package_root(self) -> None:
        import sys

        from lib.sessionkit_inventory import accounts as ours

        lib_dir = os.fspath(Path(qs.__file__).resolve().parents[2] / "lib")
        added = lib_dir not in sys.path
        if added:
            sys.path.insert(0, lib_dir)
        try:
            # Load the other spelling, exactly as another test module would.
            __import__("sessionkit_inventory.accounts")
            self.assertIn("sessionkit_inventory.accounts", sys.modules)
            self.assertIsNot(ours, sys.modules["sessionkit_inventory.accounts"])

            # Ours is still the one the reader resolves.
            self.assertIs(ours, qs._accounts_module())
        finally:
            if added:
                sys.path.remove(lib_dir)


class CollectionTests(unittest.TestCase):
    class Boom:
        name = "explodes"

        def read(self, ref: qs.AccountRef, *, now: int) -> list[qs.QuotaReading]:
            raise RuntimeError("unreadable")

    class Fine:
        name = "fine"

        def read(self, ref: qs.AccountRef, *, now: int) -> list[qs.QuotaReading]:
            return [
                qs.QuotaReading(
                    provider=ref.provider,
                    account=ref.alias,
                    window=qs.WEEKLY,
                    source=self.name,
                    confidence=qs.MEASURED,
                    observed_at_unix=now,
                    used_fraction=0.25,
                )
            ]

    def test_a_broken_source_is_recorded_and_the_others_still_answer(self) -> None:
        ref = qs.AccountRef(provider="claude", alias="one")

        snapshot = qs.collect({}, [ref], sources=[self.Boom(), self.Fine()], now=NOW)

        self.assertEqual(1, len(snapshot.readings))
        self.assertEqual(1, len(snapshot.errors))
        self.assertIn("explodes", snapshot.errors[0])
        self.assertIn("claude:one", snapshot.errors[0])

    def test_a_registered_source_joins_the_defaults(self) -> None:
        marker = self.Fine()
        qs.register_source(lambda config: marker)
        try:
            with mock.patch.dict(os.environ, {"SESSION_KIT_QUOTA_SOURCES": "fine"}):
                sources = qs.default_sources({})
        finally:
            qs.clear_registered_sources()

        self.assertEqual([marker], sources)


if __name__ == "__main__":
    unittest.main()
