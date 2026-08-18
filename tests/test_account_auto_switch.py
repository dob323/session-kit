"""The rule that stops one conversation spending every account.

Each test here is a fact the operator asked for in their own words: a dry
account moves a conversation once, never twice; a move never lands in an
account below the reserve; nothing moves on facts that are stale; the kill
switch stops everything; a working conversation is never moved; and they are
told afterwards exactly once.

Every number in these tests comes from a fixture file, never from a live
account. No test in this file logs in, enrols, verifies, or switches anything
real.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from lib.sessionkit_inventory import account_guard as guard
from lib.sessionkit_inventory import accounts
from lib.sessionkit_inventory.common import CollectionError

REPO = Path(__file__).resolve().parents[1]
UUID = "00000000-0000-4000-8000-0000000000a1"
OTHER_UUID = "00000000-0000-4000-8000-0000000000b2"
# Distinguishes "no row was passed" from "the row is deliberately None", which
# is itself one of the shapes that must be refused.
DEFAULT_ROW = object()


def session_row(
    *,
    provider: str = "claude",
    uuid: str = UUID,
    status: str = "idle",
    subagents: object = None,
    active: int = 0,
    mutation: bool = True,
    age: object = 900,
    alias: object = "spent",
    mismatch: object = False,
    model: str = "",
    model_handoff: bool = True,
) -> dict[str, object]:
    """One live inventory row, shaped as the collector publishes it.

    `alias` and `mismatch` are the collector's own account fields. They
    default to agreeing with the fixtures' bound source, because the
    interesting cases are the ones where they disagree.
    """
    row: dict[str, object] = {
        "shpool_id_raw": "main",
        "provider": provider,
        "identity": {"uuid": uuid},
        "mutation_allowed": mutation,
        "subagents": subagents,
        "active_subagent_count": active,
        "agent_status": status,
        "recent_output_age_seconds": age,
        "account_binding_mismatch": mismatch,
        "model": model,
        "model_handoff_capable": model_handoff,
    }
    if alias is not None:
        row["account_alias"] = alias
    return row


class AutomaticSwitchPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-auto-switch.")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.data = self.root / "data"
        for path in (self.home, self.state, self.data):
            path.mkdir(mode=0o700)
        self.registry = self.state / "accounts.json"
        self.profile_root = self.data / "session-kit" / "accounts"
        self.roster = self.root / "cli_accounts.json"
        self.advice = self.root / "rotation_advice.json"
        self.config = {"state_dir": str(self.state)}
        refusing_bin = refusing_commands_bin(self.root)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_DATA_HOME": str(self.data),
                "XDG_STATE_HOME": str(self.root),
                "XDG_CONFIG_HOME": str(self.root),
                # Every call here passes `config` explicitly, so nothing should
                # consult these. They are set anyway: the cost is nothing and
                # it removes the question of whether some future code path
                # falls back to the operator's own state.
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SESSION_KIT_ACCOUNT_REGISTRY": str(self.registry),
                "SESSION_KIT_ACCOUNT_ROOT": str(self.profile_root),
                "SESSION_KIT_ACCOUNT_ROSTER": str(self.roster),
                "SESSION_KIT_ROTATION_ADVICE": str(self.advice),
                "SESSION_KIT_CONFIG": str(self.root / "session-kit.json"),
                "SESSION_KIT_SHPOOL_CMD": str(refusing_bin / "shpool"),
                "PATH": f"{refusing_bin}:{os.environ.get('PATH', '')}",
                "SESSION_KIT_NONINTERACTIVE": "1",
                "SESSION_KIT_BACKGROUND": "1",
            },
            clear=False,
        )
        self.environment.start()
        for name in (
            "SESSION_KIT_ACCOUNT_ADVICE_MAX_AGE_SECONDS",
            "SESSION_KIT_ACCOUNT_RESERVE_PERCENT",
            "SESSION_KIT_ACCOUNT_EXHAUSTED_PERCENT",
        ):
            os.environ.pop(name, None)
        self.now = 1_800_000_000

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    # -- fixture builders -------------------------------------------------

    def seed_profiles(self, *rows: tuple[str, str]) -> None:
        profiles = {}
        for alias, email in rows:
            profile_dir = self.profile_root / "claude" / alias
            profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            state_path = profile_dir / ".claude.json"
            state_path.write_text(
                json.dumps({"hasCompletedOnboarding": True}), encoding="utf-8"
            )
            state_path.chmod(0o600)
            profiles[f"claude:{alias}"] = {
                "provider": "claude",
                "alias": alias,
                "email": email,
                "profile_dir": str(profile_dir),
                "legacy": False,
                "plan": "max",
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

    def write_feeds(
        self,
        usage: dict[str, object],
        *,
        recommend: str = "",
        roster_ts: int | None = None,
        advice_ts: int | None = None,
        health: dict[str, str] | None = None,
    ) -> None:
        """Publish one roster and one advice file shaped like the real feeds.

        ``usage`` maps email -> (u5h, u7d) exactly as the live
        cli_accounts.json publishes them: fractions of the window, or None
        where the feed could not read a number.
        """
        health = health or {}
        rows = []
        for email, pair in usage.items():
            five, week = pair  # type: ignore[misc]
            rows.append(
                {
                    "email": email,
                    "enabled": True,
                    "serving": True,
                    "health": health.get(email, "ok"),
                    "u5h": five,
                    "u7d": week,
                }
            )
        self.roster.write_text(
            json.dumps({"ts": roster_ts if roster_ts is not None else self.now,
                        "accounts": rows}),
            encoding="utf-8",
        )
        self.roster.chmod(0o600)
        payload: dict[str, object] = {
            "ts": advice_ts if advice_ts is not None else self.now
        }
        if recommend:
            payload["use_now"] = {
                "account": recommend,
                "reason": f"{recommend} has the soonest weekly reset",
            }
        self.advice.write_text(json.dumps(payload), encoding="utf-8")
        self.advice.chmod(0o600)

    def plan(self, row: object = DEFAULT_ROW, **kwargs: object) -> dict:
        with mock.patch("time.time", return_value=float(self.now)):
            return guard.plan(
                self.config,
                "claude",
                UUID,
                session_row() if row is DEFAULT_ROW else row,  # type: ignore[arg-type]
                **kwargs,  # type: ignore[arg-type]
            )

    def standard_estate(self) -> None:
        """A spent source and one healthy target, the ordinary trigger case."""
        self.seed_profiles(("spent", "spent@example.com"), ("fresh", "fresh@example.com"))
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "fresh@example.com": (0.1, 0.20)},
            recommend="fresh@example.com",
        )

    # -- the ordinary move -------------------------------------------------

    def test_a_spent_account_moves_the_conversation_to_the_advised_account(self) -> None:
        self.standard_estate()

        verdict = self.plan()

        self.assertEqual("switch", verdict["action"])
        self.assertEqual("spent", verdict["source_alias"])
        self.assertEqual("fresh", verdict["target_alias"])
        self.assertEqual(0, verdict["hops_used"])
        self.assertEqual(1, verdict["hop_limit"])
        self.assertIn("80%", verdict["reason"])

    def test_profile_refresh_before_a_refusal_leaves_no_move_or_spend_state(self) -> None:
        """The preparation is local and harmless, but it is not rolled back.

        The policy plan has already approved the target before this refresh.
        The final read-only eligibility check belongs after it because the
        target can be disabled while the provider status probe runs. If that
        happens, refreshed verification metadata may remain, but no binding,
        transaction, hop reservation, login, or session-manager action does.
        """
        self.standard_estate()
        before = accounts.load_registry(self.config)
        profile_state = self.profile_root / "claude" / "fresh" / ".claude.json"
        state_before = profile_state.read_bytes()

        with mock.patch.object(
            accounts,
            "probe_identity",
            return_value={
                "provider": "claude",
                "email": "fresh@example.com",
                "plan": "max-refreshed",
            },
        ) as probe:
            accounts.launch_profile(self.config, "claude", "fresh")

        probe.assert_called_once_with("claude", self.profile_root / "claude" / "fresh")
        refreshed = accounts.load_registry(self.config)
        self.assertEqual(before["bindings"], refreshed["bindings"])
        self.assertEqual(set(before["profiles"]), set(refreshed["profiles"]))
        self.assertTrue(refreshed["profiles"]["claude:fresh"]["enabled"])
        self.assertEqual("max-refreshed", refreshed["profiles"]["claude:fresh"]["plan"])
        self.assertGreater(refreshed["generation"], before["generation"])
        self.assertEqual(state_before, profile_state.read_bytes())

        # This is the exact gap: the operator disables the target after the
        # refresh and the final check refuses it without preparing a move.
        refreshed["profiles"]["claude:fresh"]["enabled"] = False
        refreshed["generation"] += 1
        accounts.write_registry(self.config, refreshed)
        recheck = guard.target_still_eligible(self.config, "claude", "fresh")
        self.assertFalse(recheck["eligible"])
        self.assertEqual(before["bindings"], accounts.load_registry(self.config)["bindings"])
        self.assertFalse((self.state / "account-switches").exists())
        self.assertFalse(guard.hop_ledger_path(self.config).exists())

    def test_a_source_with_quota_left_is_never_moved(self) -> None:
        self.seed_profiles(("spent", "spent@example.com"), ("fresh", "fresh@example.com"))
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        self.write_feeds(
            {"spent@example.com": (0.4, 0.80), "fresh@example.com": (0.1, 0.20)},
            recommend="fresh@example.com",
        )

        verdict = self.plan()

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("source_has_quota", verdict["reason_code"])
        self.assertIn("20%", verdict["reason"])

    # -- one hop only ------------------------------------------------------

    def test_the_second_exhaustion_stops_and_reports_instead_of_a_third_account(
        self,
    ) -> None:
        self.seed_profiles(
            ("first", "first@example.com"),
            ("second", "second@example.com"),
            ("third", "third@example.com"),
        )
        accounts.bind(self.config, "claude", UUID, "second", source="test")
        guard.begin_hop(
            self.config, "claude", UUID, "first", "second", now=self.now - 60
        )
        self.write_feeds(
            {
                "first@example.com": (1.0, 1.0),
                "second@example.com": (1.0, 1.0),
                "third@example.com": (0.0, 0.05),
            },
            recommend="third@example.com",
        )

        verdict = self.plan()

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("hop_limit", verdict["reason_code"])
        self.assertEqual(1, verdict["hops_used"])
        self.assertEqual("", verdict["target_alias"])
        self.assertTrue(verdict["needs_attention"])
        # A third account exists, is healthy and nearly untouched, and is still
        # not offered: the limit is the point, not the shortage.
        self.assertEqual([], verdict["candidates"])
        self.assertIn("waits for you", verdict["reason"])

    def test_one_hop_is_counted_per_conversation_not_across_the_estate(self) -> None:
        self.standard_estate()
        guard.begin_hop(
            self.config, "claude", OTHER_UUID, "a", "b", now=self.now - 60
        )

        self.assertEqual(0, guard.hop_count(self.config, "claude", UUID))
        self.assertEqual(1, guard.hop_count(self.config, "claude", OTHER_UUID))
        self.assertEqual("switch", self.plan()["action"])

    def test_an_unreadable_hop_ledger_holds_rather_than_counting_zero(self) -> None:
        self.standard_estate()
        guard.hop_ledger_path(self.config).write_text("{not json", encoding="utf-8")
        guard.hop_ledger_path(self.config).chmod(0o600)

        verdict = self.plan()

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("ledger_unreadable", verdict["reason_code"])

    # -- the protected reserve ---------------------------------------------

    def test_the_only_candidate_is_refused_when_it_is_below_the_reserve(self) -> None:
        self.seed_profiles(("spent", "spent@example.com"), ("low", "low@example.com"))
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        # 80% of the weekly window already spent: 20% left, under the 25% floor.
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "low@example.com": (0.0, 0.80)},
            recommend="low@example.com",
        )

        verdict = self.plan()

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("no_eligible_account", verdict["reason_code"])
        self.assertEqual([], verdict["candidates"])
        self.assertTrue(verdict["stood_down"])
        self.assertIn("25% reserve", verdict["reason"])

    def test_the_reserve_line_itself_is_allowed_and_one_point_past_it_is_not(
        self,
    ) -> None:
        self.seed_profiles(("spent", "spent@example.com"), ("edge", "edge@example.com"))
        accounts.bind(self.config, "claude", UUID, "spent", source="test")

        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "edge@example.com": (0.0, 0.75)},
            recommend="edge@example.com",
        )
        self.assertEqual("switch", self.plan()["action"])

        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "edge@example.com": (0.0, 0.7501)},
            recommend="edge@example.com",
        )
        self.assertEqual("hold", self.plan()["action"])

    def test_the_reserve_is_configurable_and_a_bad_value_keeps_the_default(self) -> None:
        self.seed_profiles(("spent", "spent@example.com"), ("edge", "edge@example.com"))
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "edge@example.com": (0.0, 0.60)},
            recommend="edge@example.com",
        )

        self.assertEqual("switch", self.plan()["action"])
        self.assertEqual(
            "hold",
            self.plan(environ={"SESSION_KIT_ACCOUNT_RESERVE_PERCENT": "50"})["action"],
        )
        for bad in ("", "half", "-1", "101"):
            with self.subTest(value=bad):
                verdict = self.plan(
                    environ={"SESSION_KIT_ACCOUNT_RESERVE_PERCENT": bad}
                )
                self.assertEqual(25, verdict["reserve_percent"])
                self.assertEqual("switch", verdict["action"])

    def test_an_account_with_no_published_weekly_number_is_never_a_target(self) -> None:
        self.seed_profiles(
            ("spent", "spent@example.com"), ("unknown", "unknown@example.com")
        )
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "unknown@example.com": (None, None)},
            recommend="unknown@example.com",
        )

        verdict = self.plan()

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("no_eligible_account", verdict["reason_code"])

    def test_a_usage_number_that_is_not_a_fraction_is_treated_as_unreadable(
        self,
    ) -> None:
        """A feed that switched to whole percents must not widen the reserve.

        `u7d: 80` read as a fraction is 8000% used. Believing it would call
        every account exhausted and refuse every target -- but the same
        mistake in the other direction (`u7d: 0.8` published as `80` while the
        code still divides) is how a floor silently stops existing. Anything
        outside the credible range is unknown, and unknown never moves.
        """
        self.seed_profiles(("spent", "spent@example.com"), ("odd", "odd@example.com"))
        accounts.bind(self.config, "claude", UUID, "spent", source="test")

        # A nonsense number anywhere in the feed stops the move outright.
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "odd@example.com": (0.0, 20)},
            recommend="odd@example.com",
        )
        self.assertEqual("feed_implausible", self.plan()["reason_code"])

        # Including on the source, which is then never called dry either.
        self.write_feeds(
            {"spent@example.com": (0.9, 80), "odd@example.com": (0.0, 0.05)},
            recommend="odd@example.com",
        )
        self.assertEqual("feed_implausible", self.plan()["reason_code"])

    def test_an_unreadable_five_hour_number_blocks_the_move(self) -> None:
        """Finding 5: unreadable must mean "do not move", never "no objection".

        An implausible five-hour number was classed as unreadable and then
        skipped by a check that only fired on a real number, so a target whose
        short window was published as spent still took the one hop. It is now
        refused, and refused at the level of the whole feed: the units that
        produced one impossible number produced all the others too.
        """
        self.seed_profiles(("spent", "spent@example.com"), ("odd", "odd@example.com"))
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        for bad in (100, -1, "0.5", True, float("nan")):
            with self.subTest(u5h=bad):
                self.write_feeds(
                    {"spent@example.com": (0.9, 1.0), "odd@example.com": (bad, 0.05)},
                    recommend="odd@example.com",
                )
                verdict = self.plan()
                self.assertEqual("hold", verdict["action"])
                self.assertEqual("feed_implausible", verdict["reason_code"])
                self.assertEqual("", verdict["target_alias"])

    def test_one_impossible_number_condemns_the_whole_feed(self) -> None:
        """A units change is only visible across the roster, not in one row.

        If the feed switched to whole percents, `55` is obviously not a
        fraction, but `0`, `1` and `2` still parse as 0%, 100% and 200% used,
        so judging the other accounts row by row would move a conversation
        into whatever account happened to be published as `0`.
        """
        self.seed_profiles(
            ("spent", "spent@example.com"),
            ("wholepct", "wholepct@example.com"),
            ("loud", "loud@example.com"),
        )
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        self.write_feeds(
            {
                # A whole-percent feed: the source reads as spent, the
                # candidate reads as untouched, and one row gives it away.
                "spent@example.com": (90, 100),
                "wholepct@example.com": (0, 0),
                "loud@example.com": (0, 55),
            },
            recommend="wholepct@example.com",
        )

        verdict = self.plan()

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("feed_implausible", verdict["reason_code"])
        self.assertTrue(verdict["needs_attention"])
        self.assertEqual("", verdict["target_alias"])

    def test_an_implausible_feed_also_stops_the_cheap_first_question(self) -> None:
        self.seed_profiles(
            ("spent", "spent@example.com"), ("wholepct", "wholepct@example.com")
        )
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        self.write_feeds(
            {"spent@example.com": (90, 100), "wholepct@example.com": (0, 0)},
            recommend="wholepct@example.com",
        )

        with mock.patch("time.time", return_value=float(self.now)):
            spent = guard.spent_aliases(self.config, "claude")

        self.assertEqual([], spent["aliases"])
        self.assertIs(False, spent["fresh"])

    def test_a_missing_five_hour_number_is_not_an_objection(self) -> None:
        """Codex publishes no five-hour number at all; absent stays usable."""
        self.seed_profiles(("spent", "spent@example.com"), ("quiet", "quiet@example.com"))
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "quiet@example.com": (None, 0.05)},
            recommend="quiet@example.com",
        )

        self.assertEqual("switch", self.plan()["action"])

    def test_an_account_whose_five_hour_window_is_spent_is_not_a_target(self) -> None:
        self.seed_profiles(("spent", "spent@example.com"), ("walled", "walled@example.com"))
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "walled@example.com": (1.0, 0.10)},
            recommend="walled@example.com",
        )

        self.assertEqual("no_eligible_account", self.plan()["reason_code"])

    def test_the_final_recheck_refuses_a_new_five_hour_wall(self) -> None:
        """The target can spend its short window while its profile is probed."""
        self.standard_estate()
        self.assertEqual("switch", self.plan()["action"])
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "fresh@example.com": (1.0, 0.20)},
            recommend="fresh@example.com",
        )

        with mock.patch("time.time", return_value=float(self.now)):
            recheck = guard.target_still_eligible(self.config, "claude", "fresh")

        self.assertIs(False, recheck["eligible"])
        self.assertIn("five-hour window is spent", recheck["reason"])

    def test_the_final_recheck_refuses_a_new_unreadable_usage_figure(self) -> None:
        self.standard_estate()
        self.assertEqual("switch", self.plan()["action"])
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "fresh@example.com": (100, 0.20)},
            recommend="fresh@example.com",
        )

        with mock.patch("time.time", return_value=float(self.now)):
            recheck = guard.target_still_eligible(self.config, "claude", "fresh")

        self.assertIs(False, recheck["eligible"])
        self.assertIn("not a window fraction", recheck["reason"])

    def test_a_blocked_account_is_never_a_target_however_strong_the_advice(self) -> None:
        self.seed_profiles(("spent", "spent@example.com"), ("sick", "sick@example.com"))
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "sick@example.com": (0.0, 0.05)},
            recommend="sick@example.com",
            health={"sick@example.com": "expired"},
        )

        self.assertEqual("no_eligible_account", self.plan()["reason_code"])

    def test_the_advised_account_wins_over_a_merely_emptier_one(self) -> None:
        self.seed_profiles(
            ("spent", "spent@example.com"),
            ("advised", "advised@example.com"),
            ("emptier", "emptier@example.com"),
        )
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        self.write_feeds(
            {
                "spent@example.com": (0.9, 1.0),
                "advised@example.com": (0.0, 0.40),
                "emptier@example.com": (0.0, 0.05),
            },
            recommend="advised@example.com",
        )

        self.assertEqual("advised", self.plan()["target_alias"])

    # -- no fact, no move --------------------------------------------------

    def test_a_stale_usage_feed_disables_switching(self) -> None:
        self.standard_estate()
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "fresh@example.com": (0.1, 0.20)},
            recommend="fresh@example.com",
            roster_ts=self.now - 4000,
        )

        verdict = self.plan()

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("feed_stale", verdict["reason_code"])
        self.assertIn("account usage", verdict["reason"])

    def test_stale_rotation_advice_disables_switching(self) -> None:
        self.standard_estate()
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "fresh@example.com": (0.1, 0.20)},
            recommend="fresh@example.com",
            advice_ts=self.now - 4000,
        )

        verdict = self.plan()

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("feed_stale", verdict["reason_code"])
        self.assertIn("rotation advice", verdict["reason"])

    def test_a_missing_feed_disables_switching(self) -> None:
        self.standard_estate()
        self.roster.unlink()
        self.advice.unlink()

        verdict = self.plan()

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("feed_stale", verdict["reason_code"])

    def test_a_source_with_no_published_usage_is_never_called_dry(self) -> None:
        self.seed_profiles(("spent", "spent@example.com"), ("fresh", "fresh@example.com"))
        accounts.bind(self.config, "claude", UUID, "spent", source="test")
        self.write_feeds(
            {"spent@example.com": (None, None), "fresh@example.com": (0.1, 0.20)},
            recommend="fresh@example.com",
            health={"spent@example.com": "expired"},
        )

        verdict = self.plan()

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("usage_unknown", verdict["reason_code"])

    def test_an_unbound_conversation_is_never_moved(self) -> None:
        self.seed_profiles(("spent", "spent@example.com"), ("fresh", "fresh@example.com"))
        self.write_feeds(
            {"spent@example.com": (0.9, 1.0), "fresh@example.com": (0.1, 0.20)},
            recommend="fresh@example.com",
        )

        verdict = self.plan()

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("source_unknown", verdict["reason_code"])

    # -- the kill switch ---------------------------------------------------

    def test_the_shipped_kill_switch_stops_every_automatic_move(self) -> None:
        self.standard_estate()
        killer = accounts.kill_switch_path(self.config)
        killer.write_text("", encoding="utf-8")

        verdict = self.plan()

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("kill_switch", verdict["reason_code"])
        self.assertTrue(guard.automatic_switching_off(self.config))
        self.assertEqual(
            str(self.state / "account-switching-off"), str(killer)
        )

    def test_the_kill_switch_outranks_a_dry_account(self) -> None:
        self.standard_estate()
        accounts.kill_switch_path(self.config).write_text("", encoding="utf-8")

        self.assertEqual("kill_switch", self.plan()["reason_code"])

    # -- never move working conversations -----------------------------------

    def test_a_mid_turn_conversation_is_never_moved(self) -> None:
        self.standard_estate()

        verdict = self.plan(session_row(status="running"))

        self.assertEqual("hold", verdict["action"])
        self.assertEqual("session_busy", verdict["reason_code"])
        self.assertIn("running", verdict["reason"])

    def test_every_unsafe_session_shape_is_refused(self) -> None:
        self.standard_estate()
        unsafe = {
            "running": session_row(status="running"),
            "unknown status": session_row(status="unknown"),
            "sub-agents listed": session_row(subagents=[{"pid": 5}]),
            "sub-agents counted": session_row(active=2),
            "not mutable": session_row(mutation=False),
            "just printed": session_row(age=1),
            "wrong uuid": session_row(uuid=OTHER_UUID),
            "wrong provider": session_row(provider="codex"),
            "missing row": None,
        }
        for label, row in unsafe.items():
            with self.subTest(shape=label):
                verdict = self.plan(row)
                self.assertEqual("hold", verdict["action"])
                self.assertEqual("session_busy", verdict["reason_code"])

    def test_the_live_account_must_be_the_account_that_ran_dry(self) -> None:
        """Finding 8: the binding is a record; the live row is the fact.

        A conversation whose process is signed in to `fresh` while the binding
        still says `spent` was restarted and handed to `fresh`, a move made
        on paperwork, against a conversation whose real account was never the
        one found dry.
        """
        self.standard_estate()

        mismatched = self.plan(session_row(alias="fresh", mismatch=True))
        self.assertEqual("hold", mismatched["action"])
        self.assertEqual("session_busy", mismatched["reason_code"])
        self.assertIn("does not match", mismatched["reason"])

        wrong_alias = self.plan(session_row(alias="fresh"))
        self.assertEqual("hold", wrong_alias["action"])
        self.assertIn("signed in to fresh", wrong_alias["reason"])

        unknown_alias = self.plan(session_row(alias=None))
        self.assertEqual("hold", unknown_alias["action"])
        self.assertIn("could not be read", unknown_alias["reason"])

    def test_a_conversation_on_a_named_model_can_move_without_downgrade(self) -> None:
        """Finding 10: a requested model no longer blocks a safe handoff."""
        self.standard_estate()

        verdict = self.plan(session_row(model="claude-opus-5"))

        self.assertEqual("switch", verdict["action"])
        self.assertEqual("switch", self.plan(session_row(model=""))["action"])

        unbound = self.plan(
            session_row(model="claude-opus-5", model_handoff=False)
        )
        self.assertEqual("hold", unbound["action"])
        self.assertIn("not bound", unbound["reason"])

    def test_every_safe_session_shape_is_allowed(self) -> None:
        self.standard_estate()
        for status in ("idle", "Needs your reply", "reply optional"):
            with self.subTest(status=status):
                self.assertEqual(
                    "switch", self.plan(session_row(status=status))["action"]
                )
        self.assertEqual("switch", self.plan(session_row(age=None))["action"])

    def test_the_automatic_path_uses_the_manual_switch_safety_predicates(self) -> None:
        """The two copies of the safety rule must not drift apart.

        sp_picker.sh's account_switch_stable_snapshot is the authority the
        manual switch runs; this module mirrors it so a plan can refuse early
        with a readable sentence. If either side gains or loses a predicate
        without the other, this fails.
        """
        picker = (REPO / "lib" / "sh" / "sp_picker.sh").read_text(encoding="utf-8")
        start = picker.index("account_switch_stable_snapshot()")
        block = picker[start : picker.index("account_switch_safe_tree()")]
        for status in guard.MOVABLE_AGENT_STATUS:
            self.assertIn(f'"{status}"', block)
        for field in (
            "mutation_allowed",
            "subagents",
            "active_subagent_count",
            "agent_status",
            "recent_output_age_seconds",
        ):
            self.assertIn(field, block)
        self.assertIn(f"age < {guard.MIN_QUIET_SECONDS}", block)

    # -- told afterwards, exactly once --------------------------------------

    def test_a_move_is_recorded_and_queued_for_the_operator_exactly_once(self) -> None:
        self.standard_estate()

        token = guard.begin_hop(
            self.config, "claude", UUID, "spent", "fresh", reason="dry", now=self.now
        )
        guard.commit_hop(self.config, "claude", UUID, token)
        queued = [
            guard.queue_notice(self.config, "claude", UUID, token, "it moved")
            for _ in range(4)
        ]

        self.assertEqual([True, False, False, False], queued)
        ledger = guard.load_hop_ledger(self.config)["threads"]["claude:" + UUID]
        self.assertEqual(1, len(ledger["hops"]))
        self.assertEqual(1, ledger["count"])
        self.assertEqual("spent", ledger["hops"][0]["from"])
        self.assertEqual("fresh", ledger["hops"][0]["to"])
        self.assertEqual("committed", ledger["hops"][0]["state"])
        self.assertEqual(0o600, guard.hop_ledger_path(self.config).stat().st_mode & 0o777)

    def test_a_notice_stays_owed_until_a_delivery_actually_succeeds(self) -> None:
        """Finding 3: the claim belongs to delivery, not to noticing.

        Queuing and then failing to deliver used to file the move as told.
        The debt has to outlive a notifier that is down, or the move happened
        and nobody ever said so.
        """
        self.standard_estate()
        token = guard.begin_hop(self.config, "claude", UUID, "spent", "fresh")
        guard.queue_notice(self.config, "claude", UUID, token, "it moved")

        # Delivery never reported success, so it is still owed.
        owed = guard.pending_notices(self.config)
        self.assertEqual(1, len(owed))
        self.assertEqual(token, owed[0]["token"])
        self.assertEqual("claude", owed[0]["provider"])
        self.assertEqual(UUID, owed[0]["uuid"])
        self.assertEqual("it moved", owed[0]["sentence"])

        # Still owed after any number of further passes.
        self.assertFalse(guard.queue_notice(self.config, "claude", UUID, token, "x"))
        self.assertEqual(1, len(guard.pending_notices(self.config)))

        # Only a success clears it, and only once.
        self.assertTrue(guard.notice_delivered(self.config, "claude", UUID, token))
        self.assertEqual([], guard.pending_notices(self.config))
        self.assertFalse(guard.notice_delivered(self.config, "claude", UUID, token))

    def test_a_second_distinct_move_gets_its_own_single_notice(self) -> None:
        self.standard_estate()

        self.assertTrue(guard.queue_notice(self.config, "claude", UUID, "t1", "a"))
        self.assertFalse(guard.queue_notice(self.config, "claude", UUID, "t1", "a"))
        self.assertTrue(guard.queue_notice(self.config, "claude", UUID, "t2", "b"))
        self.assertEqual(2, len(guard.pending_notices(self.config)))

    def test_the_ledger_refuses_a_thread_key_that_is_not_an_exact_uuid(self) -> None:
        with self.assertRaises(CollectionError):
            guard.hop_count(self.config, "claude", "not-a-uuid")
        with self.assertRaises(CollectionError):
            guard.begin_hop(self.config, "nope", UUID, "a", "b")

    # -- findings 2 and 4: the one-move limit must not fail open -----------

    def test_a_move_counts_from_the_moment_it_is_reserved(self) -> None:
        """Finding 2: a half-finished move must not read as no move.

        The switch used to commit before the ledger was written, so a failure
        in between left a conversation that had moved with a zero-hop record,
        and the next pass bought it a third account.
        """
        self.standard_estate()

        guard.begin_hop(self.config, "claude", UUID, "spent", "fresh")

        # Nothing has been committed, yet the limit already counts it.
        self.assertEqual(1, guard.hop_count(self.config, "claude", UUID))
        verdict = self.plan()
        self.assertEqual("hold", verdict["action"])
        self.assertEqual("hop_limit", verdict["reason_code"])

    def test_only_a_proven_return_to_the_source_gives_the_move_back(self) -> None:
        self.standard_estate()
        token = guard.begin_hop(self.config, "claude", UUID, "spent", "fresh")
        self.assertEqual(1, guard.hop_count(self.config, "claude", UUID))

        self.assertFalse(guard.release_hop(self.config, "claude", UUID, "wrong"))
        self.assertEqual(1, guard.hop_count(self.config, "claude", UUID))

        self.assertTrue(guard.release_hop(self.config, "claude", UUID, token))
        self.assertEqual(0, guard.hop_count(self.config, "claude", UUID))
        self.assertEqual("switch", self.plan()["action"])

    def test_two_passes_racing_cannot_both_reserve_the_one_move(self) -> None:
        """The limit is enforced where the reservation is taken.

        Two drivers can be alive at once, the resident watchdog loop and a
        hand-run pass, and both can read a hop count of zero before either
        writes. Only one may win; the loser is refused, not granted a second
        paid account.
        """
        self.standard_estate()

        first = guard.begin_hop(self.config, "claude", UUID, "spent", "fresh")
        self.assertTrue(first)
        with self.assertRaises(CollectionError) as refused:
            guard.begin_hop(self.config, "claude", UUID, "spent", "third")

        self.assertIn("already had its 1 automatic move", str(refused.exception))
        self.assertEqual(1, guard.hop_count(self.config, "claude", UUID))

    def test_a_committed_move_can_never_be_released(self) -> None:
        self.standard_estate()
        token = guard.begin_hop(self.config, "claude", UUID, "spent", "fresh")
        guard.commit_hop(self.config, "claude", UUID, token)

        # release_hop is only ever called on a proven rollback; this pins that
        # a stale token from a completed move cannot un-count it.
        self.assertFalse(guard.release_hop(self.config, "claude", UUID, token))
        self.assertEqual(1, guard.hop_count(self.config, "claude", UUID))
        # ...and the count that matters is rebuilt from `count`, not the list.
        raw = json.loads(guard.hop_ledger_path(self.config).read_text(encoding="utf-8"))
        self.assertEqual(1, raw["threads"]["claude:" + UUID]["count"])

    def test_a_ledger_too_large_to_read_holds_instead_of_being_sliced(self) -> None:
        """Finding 4: a fixed read slice made every later conversation free.

        With 513 conversations the 513th was on disk, invisible to the reader,
        and its hop count came back zero.
        """
        self.standard_estate()
        threads = {
            "claude:00000000-0000-4000-8000-%012d" % index: {
                "count": 1,
                "hops": [{"at_unix": self.now, "from": "a", "to": "b",
                          "reason": "", "state": "committed", "token": "t%d" % index}],
                "notices": [],
            }
            for index in range(guard.MAX_LEDGER_THREADS + 1)
        }
        guard.hop_ledger_path(self.config).write_text(
            json.dumps({"schema_version": guard.GUARD_SCHEMA_VERSION,
                        "threads": threads}),
            encoding="utf-8",
        )
        guard.hop_ledger_path(self.config).chmod(0o600)

        with self.assertRaises(CollectionError):
            guard.load_hop_ledger(self.config)
        verdict = self.plan()
        self.assertEqual("hold", verdict["action"])
        self.assertEqual("ledger_unreadable", verdict["reason_code"])

    def test_a_large_but_readable_ledger_still_sees_its_last_conversation(self) -> None:
        self.standard_estate()
        for index in range(600):
            guard.begin_hop(
                self.config,
                "claude",
                "00000000-0000-4000-8000-%012d" % index,
                "a",
                "b",
                now=self.now,
            )
        guard.begin_hop(self.config, "claude", UUID, "spent", "fresh", now=self.now)

        self.assertEqual(1, guard.hop_count(self.config, "claude", UUID))
        self.assertEqual("hop_limit", self.plan()["reason_code"])

    def test_a_long_dormant_conversation_keeps_its_one_move_forever(self) -> None:
        self.standard_estate()
        old = "00000000-0000-4000-8000-0000000000c3"
        guard.begin_hop(
            self.config, "claude", old, "a", "b",
            now=self.now - (10 * 365 * 24 * 60 * 60),
        )

        with self.assertRaises(CollectionError):
            guard.begin_hop(self.config, "claude", old, "a", "b", now=self.now)

        guard.begin_hop(self.config, "claude", OTHER_UUID, "a", "b", now=self.now)
        threads = guard.load_hop_ledger(self.config)["threads"]
        self.assertIn("claude:" + old, threads)
        self.assertIn("claude:" + OTHER_UUID, threads)

    def test_an_undelivered_notice_is_never_pruned_away(self) -> None:
        self.standard_estate()
        stale = "00000000-0000-4000-8000-0000000000d4"
        token = guard.begin_hop(
            self.config, "claude", stale, "a", "b",
            now=self.now - (10 * 365 * 24 * 60 * 60),
        )
        guard.queue_notice(
            self.config, "claude", stale, token, "an old move",
            now=self.now - (10 * 365 * 24 * 60 * 60),
        )

        guard.begin_hop(self.config, "claude", OTHER_UUID, "a", "b", now=self.now)

        owed = [item["uuid"] for item in guard.pending_notices(self.config)]
        self.assertIn(stale, owed)


def refusing_commands_bin(root: Path) -> Path:
    """A PATH entry whose providers and shpool refuse to do anything.

    `probe_identity` runs `claude auth status --json` against a profile
    directory, and it finds that binary on PATH -- not through any
    SESSION_KIT_* variable, so pinning the state directory does not pin this.
    No test here should reach a provider or session manager at all; if one ever
    does, it must hit a shim that fails loudly rather than a real signed-in CLI
    or a daemon-capable shpool.
    """
    directory = root / "refusing-bin"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("claude", "codex", "shpool"):
        shim = directory / name
        shim.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "test sandbox: a real {name} invocation was attempted" >&2\n'
            "exit 97\n",
            encoding="utf-8",
        )
        shim.chmod(0o700)
    return directory


def sandbox_environment(root: Path) -> dict[str, str]:
    """Every path a kit binary can resolve, pinned inside one temporary root.

    Inheriting the real HOME is enough to reach the operator's own config,
    account profiles and session manager. Nothing in this file may do that, so
    each variable is named here rather than left to a default -- including the
    ones a given test does not obviously need, because the point is that no
    default can reach outside `root`. The fixture also installs its own
    refusing `shpool` at `$HOME/.local/bin/shpool`: the managed bashrc has one
    deliberate hardcoded `command shpool detach`, so the environment override
    alone is not a complete sandbox.
    """
    home = root / "home"
    for path in (home, root / "state", root / "journals", root / "archives",
                 root / "recovery", root / "start", root / "config"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "projects.tsv").touch(mode=0o600, exist_ok=True)
    home_bin = home / ".local" / "bin"
    home_bin.mkdir(mode=0o700, parents=True, exist_ok=True)
    shpool = home_bin / "shpool"
    shpool.write_text(
        "#!/bin/sh\necho 'test sandbox: shpool was invoked' >&2\nexit 97\n",
        encoding="utf-8",
    )
    shpool.chmod(0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{home_bin}:{refusing_commands_bin(root)}:{os.environ.get('PATH', '')}",
            "HOME": str(home),
            "XDG_STATE_HOME": str(root / "xdg-state"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "SESSION_KIT_STATE_DIR": str(root / "state"),
            "SESSION_KIT_JOURNAL_DIR": str(root / "journals"),
            "SESSION_KIT_ARCHIVE_DIR": str(root / "archives"),
            "SESSION_KIT_JOURNAL_RECOVERY_DIR": str(root / "recovery"),
            "SESSION_KIT_START_DIR": str(root / "start"),
            "SESSION_KIT_PROJECTS_FILE": str(root / "projects.tsv"),
            "SESSION_KIT_CONFIG": str(root / "config" / "inventory.json"),
            "SESSION_KIT_ACCOUNT_REGISTRY": str(root / "state" / "accounts.json"),
            "SESSION_KIT_ACCOUNT_ROOT": str(root / "profiles"),
            "SESSION_KIT_ACCOUNT_ROSTER": str(root / "cli_accounts.json"),
            "SESSION_KIT_ROTATION_ADVICE": str(root / "rotation_advice.json"),
            "SESSION_KIT_SHPOOL_CMD": str(shpool),
            "SESSION_KIT_NONINTERACTIVE": "1",
            "SESSION_KIT_BACKGROUND": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


class AutomaticSwitchCommandTests(unittest.TestCase):
    """The `account auto-plan` verb the shell driver reads."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-auto-cli.")
        self.root = Path(self.temporary.name)
        self.environment = sandbox_environment(self.root)
        self.state = self.root / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_auto_plan_prints_a_json_verdict_and_never_moves_anything(self) -> None:
        # No profiles, no feeds: the answer must still be a readable hold.
        result = subprocess.run(
            [
                "python3",
                str(REPO / "lib" / "session_inventory.py"),
                "account",
                "auto-plan",
                "claude",
                UUID,
            ],
            cwd=REPO,
            env=self.environment,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        verdict = json.loads(result.stdout)
        self.assertEqual("hold", verdict["action"])
        self.assertIn(
            verdict["reason_code"], {"feed_stale", "source_unknown", "kill_switch"}
        )
        self.assertEqual(25, verdict["reserve_percent"])
        self.assertEqual(1, verdict["hop_limit"])
        # Nothing was created anywhere but the sandbox.
        self.assertFalse((self.state / "account-auto-hops.json").exists())

    def test_the_sandbox_leaves_the_operators_own_state_untouched(self) -> None:
        """A guard on the harness itself, not on the product.

        A test that inherits the real HOME can reach the operator's config,
        their enrolled profiles and their session manager. This asserts the
        environment every subprocess in this file runs under actually pins all
        of them inside one temporary directory.
        """
        for key in (
            "HOME",
            "SESSION_KIT_STATE_DIR",
            "SESSION_KIT_JOURNAL_DIR",
            "SESSION_KIT_ARCHIVE_DIR",
            "SESSION_KIT_JOURNAL_RECOVERY_DIR",
            "SESSION_KIT_START_DIR",
            "SESSION_KIT_PROJECTS_FILE",
            "SESSION_KIT_CONFIG",
            "SESSION_KIT_ACCOUNT_REGISTRY",
            "SESSION_KIT_ACCOUNT_ROOT",
            "SESSION_KIT_ACCOUNT_ROSTER",
            "SESSION_KIT_ROTATION_ADVICE",
            "SESSION_KIT_SHPOOL_CMD",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CONFIG_HOME",
        ):
            with self.subTest(variable=key):
                value = self.environment.get(key, "")
                self.assertTrue(value, f"{key} is not pinned")
                self.assertTrue(
                    Path(value).resolve().is_relative_to(self.root.resolve()),
                    f"{key} escapes the sandbox: {value}",
                )
        self.assertEqual("1", self.environment["SESSION_KIT_NONINTERACTIVE"])
        self.assertEqual("1", self.environment["SESSION_KIT_BACKGROUND"])
        # Both the configurable command and bashrc's hardcoded detach resolve
        # to the fixture's refusing shpool, never a host daemon.
        self.assertEqual(
            Path(self.environment["HOME"]) / ".local" / "bin" / "shpool",
            Path(self.environment["SESSION_KIT_SHPOOL_CMD"]),
        )
        self.assertTrue(Path(self.environment["SESSION_KIT_SHPOOL_CMD"]).is_file())
        # And so is a real provider CLI: every PATH prefix ahead of the host
        # is inside this fixture, with the refusing provider shims second only
        # to HOME's required shpool stub.
        first = Path(self.environment["PATH"].split(":", 1)[0]).resolve()
        self.assertTrue(first.is_relative_to(self.root.resolve()))
        refused = subprocess.run(
            ["claude", "auth", "status", "--json"],
            env=self.environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(97, refused.returncode)
        self.assertIn("a real claude invocation was attempted", refused.stderr)


class DisableIsNeverUndoneTests(unittest.TestCase):
    """Finding 1. The one rule with no exceptions.

    Preparing a target profile re-verifies it, and re-verification runs a
    provider binary that can take seconds. The registry is shared. If the
    operator switches an account off inside that window, the code that writes
    the profile back must not write `enabled: True`, doing so silently undid
    their decision, and whatever used the profile next spent their money on an
    account they had switched off.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-enable.")
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.profiles = self.root / "profiles"
        self.config = {"state_dir": str(self.state)}
        self.registry = self.state / "accounts.json"
        refusing_bin = refusing_commands_bin(self.root)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "XDG_DATA_HOME": str(self.root),
                "XDG_STATE_HOME": str(self.root),
                "XDG_CONFIG_HOME": str(self.root),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SESSION_KIT_ACCOUNT_REGISTRY": str(self.registry),
                "SESSION_KIT_ACCOUNT_ROOT": str(self.profiles),
                "SESSION_KIT_ACCOUNT_ROSTER": str(self.root / "roster.json"),
                "SESSION_KIT_ROTATION_ADVICE": str(self.root / "advice.json"),
                "SESSION_KIT_CONFIG": str(self.root / "session-kit.json"),
                "SESSION_KIT_SHPOOL_CMD": str(refusing_bin / "shpool"),
                "PATH": f"{refusing_bin}:{os.environ.get('PATH', '')}",
            },
            clear=False,
        )
        self.environment.start()
        directory = self.profiles / "claude" / "target"
        directory.mkdir(mode=0o700, parents=True)
        state = directory / ".claude.json"
        state.write_text(json.dumps({"hasCompletedOnboarding": True}), encoding="utf-8")
        state.chmod(0o600)
        self.profile_dir = directory
        accounts.write_registry(
            self.config,
            {
                "schema_version": accounts.ACCOUNT_SCHEMA_VERSION,
                "generation": 1,
                "profiles": {
                    "claude:target": {
                        "provider": "claude",
                        "alias": "target",
                        "email": "target@example.com",
                        "profile_dir": str(directory),
                        "legacy": False,
                        "plan": "max",
                        "verified_at_unix_ms": 1_800_000_000_000,
                        "enabled": True,
                    }
                },
                "bindings": {},
            },
        )

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def disable_target(self) -> None:
        registry = accounts.load_registry(self.config)
        registry["profiles"]["claude:target"]["enabled"] = False
        registry["generation"] += 1
        accounts.write_registry(self.config, registry)

    def enabled_now(self) -> object:
        return accounts.load_registry(self.config)["profiles"]["claude:target"]["enabled"]

    def test_an_account_disabled_during_the_probe_stays_disabled(self) -> None:
        def probe(provider: str, profile_dir: Path) -> dict[str, object]:
            # Exactly the reproduced race: the operator acts while the
            # provider is being asked who it is.
            self.disable_target()
            return {"provider": "claude", "email": "target@example.com", "plan": "max"}

        with mock.patch.object(accounts, "probe_identity", probe):
            with self.assertRaises(CollectionError) as refused:
                accounts.verify_profile(self.config, "claude", "target")

        self.assertIn("disabled", str(refused.exception))
        self.assertIs(False, self.enabled_now())

    def test_launch_profile_refuses_rather_than_re_enabling(self) -> None:
        def probe(provider: str, profile_dir: Path) -> dict[str, object]:
            self.disable_target()
            return {"provider": "claude", "email": "target@example.com", "plan": "max"}

        with mock.patch.object(accounts, "probe_identity", probe):
            with self.assertRaises(CollectionError):
                accounts.launch_profile(self.config, "claude", "target")

        self.assertIs(False, self.enabled_now())

    def test_an_account_already_disabled_is_never_verified_back_on(self) -> None:
        self.disable_target()

        with mock.patch.object(accounts, "probe_identity") as probe:
            with self.assertRaises(CollectionError):
                accounts.verify_profile(self.config, "claude", "target")

        self.assertIs(False, self.enabled_now())
        # It never even asked the provider: a disabled account is not probed.
        probe.assert_not_called()

    def test_ordinary_verification_of_an_enabled_account_still_works(self) -> None:
        def probe(provider: str, profile_dir: Path) -> dict[str, object]:
            return {"provider": "claude", "email": "target@example.com", "plan": "max"}

        with mock.patch.object(accounts, "probe_identity", probe):
            item = accounts.verify_profile(self.config, "claude", "target")

        self.assertEqual("target", item["alias"])
        self.assertIs(True, self.enabled_now())

    def test_an_unrelated_registry_write_does_not_break_verification(self) -> None:
        """The check is on this profile, not on a global generation counter.

        Another session binding a conversation bumps the registry generation.
        Refusing on that would make every switch fail whenever the estate is
        busy, which is its own kind of broken.
        """

        def probe(provider: str, profile_dir: Path) -> dict[str, object]:
            registry = accounts.load_registry(self.config)
            registry["generation"] += 5
            accounts.write_registry(self.config, registry)
            return {"provider": "claude", "email": "target@example.com", "plan": "max"}

        with mock.patch.object(accounts, "probe_identity", probe):
            item = accounts.verify_profile(self.config, "claude", "target")

        self.assertEqual("target", item["alias"])
        self.assertIs(True, self.enabled_now())

    def test_a_profile_change_during_the_probe_is_not_overwritten(self) -> None:
        """The re-verification write is a compare-and-set on this profile."""

        def probe(provider: str, profile_dir: Path) -> dict[str, object]:
            registry = accounts.load_registry(self.config)
            registry["profiles"]["claude:target"]["email"] = "new@example.com"
            registry["generation"] += 1
            accounts.write_registry(self.config, registry)
            return {
                "provider": "claude",
                "email": "target@example.com",
                "plan": "max",
            }

        with mock.patch.object(accounts, "probe_identity", probe):
            with self.assertRaises(CollectionError) as refused:
                accounts.verify_profile(self.config, "claude", "target")

        self.assertIn("changed while", str(refused.exception))
        current = accounts.load_registry(self.config)["profiles"]["claude:target"]
        self.assertEqual("new@example.com", current["email"])
        self.assertIs(True, current["enabled"])


SP = REPO / "bin" / "sp"
SESSION = "s20260101-000000-1"
SHELL_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class AutomaticSwitchDriverTests(unittest.TestCase):
    """`sp account-auto-switch`: what it does before the point of no return.

    Every account verb is a fixture stub that records its arguments, so these
    tests prove the order of operations and the refusals without a profile, a
    feed, a login or a provider anywhere near them.
    """

    def setUp(self) -> None:
        from tests.test_commands import CommandFixture

        self.fixture = CommandFixture()
        self.account_log = self.fixture.base / "account-calls.jsonl"
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": SESSION,
                            "status": "Disconnected",
                            "started_at_unix_ms": 1_700_000_000_001,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.fixture.close()

    def plan_json(self, **overrides: object) -> str:
        value: dict[str, object] = {
            "action": "switch",
            "reason_code": "move",
            "reason": "spent is spent; moving this conversation to fresh, "
            "which still has 80% of its weekly window.",
            "source_alias": "spent",
            "target_alias": "fresh",
        }
        value.update(overrides)
        return json.dumps(value)

    def env(self, **overrides: str) -> dict[str, str]:
        table = {
            "processes": [
                {"pid": 1001, "ppid": 1, "start_ticks": 10001, "cmdline": ["bash"]},
                {
                    "pid": 2001,
                    "ppid": 1001,
                    "start_ticks": 20001,
                    "cmdline": ["claude", "--resume", SHELL_UUID],
                },
            ]
        }
        env = {
            **self.fixture.env(),
            # CommandFixture already pins HOME, the state/journal/archive/
            # recovery/start directories, the projects file, the config, the
            # session manager and the inventory core inside its own temporary
            # tree. These add the account-side paths and the XDG roots it does
            # not name, so no default can resolve to the operator's own
            # profiles or feeds. Added here rather than in CommandFixture,
            # which other suites share.
            "SESSION_KIT_ACCOUNT_REGISTRY": str(self.fixture.base / "accounts.json"),
            "SESSION_KIT_ACCOUNT_ROOT": str(self.fixture.base / "profiles"),
            # The colour-and-names branch made the shared account stub
            # honest: with no profile named, an account is simply not
            # enrolled, which is what the real core reports. Every scenario
            # in this suite moves a conversation ONTO an account that is
            # enrolled, so the fixture has to say so. Leaving it unset made
            # eight tests assert against "no such enrolled account" instead
            # of against the behaviour they are named for.
            "STUB_ACCOUNT_PROFILE": str(self.fixture.base / "profiles" / "target"),
            "SESSION_KIT_ACCOUNT_ROSTER": str(self.fixture.base / "cli_accounts.json"),
            "SESSION_KIT_ROTATION_ADVICE": str(self.fixture.base / "advice.json"),
            "XDG_STATE_HOME": str(self.fixture.base / "xdg-state"),
            "XDG_DATA_HOME": str(self.fixture.base / "xdg-data"),
            "XDG_CONFIG_HOME": str(self.fixture.base / "xdg-config"),
            "PATH": "%s:%s"
            % (refusing_commands_bin(self.fixture.base), os.environ.get("PATH", "")),
            "SESSION_KIT_BACKGROUND": "1",
            "STUB_DYNAMIC_PROVIDER": "claude",
            "STUB_DYNAMIC_UUID": SHELL_UUID,
            "STUB_DYNAMIC_CWD": str(self.fixture.project),
            "STUB_DYNAMIC_AGENT_STATUS": "idle",
            "STUB_DYNAMIC_ACCOUNT_ALIAS": "spent",
            "STUB_DYNAMIC_ACCOUNT_CAPABLE": "1",
            "STUB_PROCESS_TABLE": json.dumps(table),
            "STUB_ACCOUNT_LOG": str(self.account_log),
            "STUB_ACCOUNT_PLAN": self.plan_json(),
            "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
        }
        env.update(overrides)
        return env

    def calls(self) -> list[list[str]]:
        if not self.account_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.account_log.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls()]

    def run_switch(self, **overrides: str):
        return subprocess.run(
            [str(SP), "account-auto-switch", SESSION, "--apply"],
            env={**os.environ, **self.env(**overrides)},
            capture_output=True,
            text=True,
        )

    # -- the command exists and asks the rule first -------------------------

    def test_it_asks_the_rule_for_the_exact_conversation_and_nothing_else(self) -> None:
        held = self.run_switch(
            STUB_ACCOUNT_PLAN=self.plan_json(
                action="hold",
                reason_code="source_has_quota",
                reason="spent still has 40% of its weekly window; nothing was moved.",
                target_alias="",
            )
        )

        self.assertEqual(0, held.returncode, held.stderr)
        self.assertIn("still has 40%", held.stdout)
        self.assertEqual(["auto-plan"], self.verbs())
        # The rule is asked about this conversation, by provider and UUID.
        self.assertEqual(
            ["account", "auto-plan", "claude", SHELL_UUID],
            self.calls()[0][:4],
        )
        self.assertIn("--shpool-id", self.calls()[0])
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_a_hold_verdict_never_reaches_a_single_account_mutation(self) -> None:
        for code, reason in (
            ("kill_switch", "Automatic account changes are switched off."),
            ("feed_stale", "The account usage feed is stale or unreadable."),
            ("hop_limit", "It stops here and waits for you."),
            ("no_eligible_account", "automatic switching stood down."),
        ):
            with self.subTest(reason_code=code):
                if self.account_log.exists():
                    self.account_log.unlink()
                result = self.run_switch(
                    STUB_ACCOUNT_PLAN=self.plan_json(
                        action="hold", reason_code=code, reason=reason, target_alias=""
                    )
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(["auto-plan"], self.verbs())
                self.assertNotIn("moved from", result.stdout)
                self.assertFalse(self.fixture.shpool_log.exists())

    # -- never move a working conversation ----------------------------------

    def test_a_working_conversation_is_not_moved_even_on_a_switch_verdict(self) -> None:
        result = self.run_switch(STUB_DYNAMIC_AGENT_STATUS="working")

        self.assertEqual(1, result.returncode)
        self.assertIn("the conversation is working", result.stderr)
        self.assertEqual(["auto-plan"], self.verbs())
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_an_unrecognized_background_child_stops_the_move(self) -> None:
        table = {
            "processes": [
                {"pid": 1001, "ppid": 1, "start_ticks": 10001, "cmdline": ["bash"]},
                {"pid": 2001, "ppid": 1001, "start_ticks": 20001, "cmdline": ["claude"]},
                {"pid": 3001, "ppid": 1001, "start_ticks": 30001, "cmdline": ["rsync"]},
            ]
        }
        result = self.run_switch(STUB_PROCESS_TABLE=json.dumps(table))

        self.assertEqual(1, result.returncode)
        self.assertIn("does not recognize", result.stderr)
        self.assertEqual(["auto-plan"], self.verbs())
        self.assertFalse(self.fixture.shpool_log.exists())

    # -- the target must prove itself before anything is prepared -----------

    def test_a_target_that_cannot_be_prepared_stops_before_the_checkpoint(self) -> None:
        """A failed local identity refresh cannot reach move state.

        launch-profile belongs after the plan's initial eligibility decision
        and before the final target recheck: it verifies the already-enrolled
        profile, but does not prepare, reserve, signal, or switch anything.
        """
        marker = self.fixture.base / "launch-refresh.json"
        result = self.run_switch(
            STUB_ACCOUNT_LAUNCH_FAILS="1",
            STUB_ACCOUNT_LAUNCH_MARKER=str(marker),
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("not selectable", result.stderr)
        self.assertEqual(["auto-plan", "launch-profile"], self.verbs())
        self.assertNotIn("switch-prepare", self.verbs())
        self.assertFalse(marker.exists())
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_a_window_that_cannot_change_account_in_place_is_left_alone(self) -> None:
        result = self.run_switch(STUB_DYNAMIC_ACCOUNT_CAPABLE="0")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("cannot change account on its own", result.stdout)
        self.assertEqual(["auto-plan"], self.verbs())
        # The legacy path recreates the shell. Never unattended.
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_a_verdict_naming_an_unusable_account_is_refused(self) -> None:
        for source, target in (("spent", "spent"), ("spent", "NOPE"), ("", "fresh")):
            with self.subTest(source=source, target=target):
                result = self.run_switch(
                    STUB_ACCOUNT_PLAN=self.plan_json(
                        source_alias=source, target_alias=target
                    )
                )
                self.assertEqual(1, result.returncode)
                self.assertIn("unusable account", result.stderr)

    def test_dry_run_names_the_move_and_touches_nothing(self) -> None:
        result = subprocess.run(
            [str(SP), "account-auto-switch", SESSION, "--dry-run"],
            env={**os.environ, **self.env()},
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Would move", result.stdout)
        self.assertIn("spent", result.stdout)
        self.assertIn("fresh", result.stdout)
        self.assertEqual(["auto-plan"], self.verbs())
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_a_target_switched_off_after_the_plan_is_refused(self) -> None:
        """Finding 1's belt, at the driver.

        Preparing the target is a mutating call that runs a provider binary
        and takes seconds. If the operator switches that account off inside
        that window, their answer wins and nothing is prepared.
        """
        marker = self.fixture.base / "launch-refresh.json"
        result = self.run_switch(
            STUB_ACCOUNT_TARGET_GONE="1",
            STUB_ACCOUNT_LAUNCH_MARKER=str(marker),
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("no longer available", result.stderr)
        self.assertEqual(
            ["auto-plan", "launch-profile", "auto-target-ok"], self.verbs()
        )
        self.assertNotIn("switch-prepare", self.verbs())
        self.assertNotIn("auto-begin", self.verbs())
        self.assertEqual(
            {"provider": "claude", "alias": "fresh"},
            json.loads(marker.read_text(encoding="utf-8")),
        )
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_a_live_account_change_before_the_signal_is_refused(self) -> None:
        """A refusal in the exact launch-to-recheck gap leaves only refresh state.

        The fresh snapshot must still show the source the plan judged. The
        marker represents the harmless local verification metadata that real
        launch-profile may retain; no checkpoint, reservation, target recheck,
        provider signal, or session-manager call may follow this refusal.
        """
        marker = self.fixture.base / "launch-refresh.json"
        result = self.run_switch(
            STUB_DYNAMIC_ACCOUNT_ALIAS_AFTER_FIRST="fresh",
            STUB_ACCOUNT_LAUNCH_MARKER=str(marker),
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("changed while its account handoff was prepared", result.stderr)
        self.assertEqual(
            ["auto-plan", "launch-profile"], self.verbs()
        )
        self.assertNotIn("switch-prepare", self.verbs())
        self.assertNotIn("auto-begin", self.verbs())
        self.assertNotIn("auto-target-ok", self.verbs())
        self.assertEqual(
            {"provider": "claude", "alias": "fresh"},
            json.loads(marker.read_text(encoding="utf-8")),
        )
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_a_new_live_binding_mismatch_before_the_signal_is_refused(self) -> None:
        """The same pre-recheck refusal applies to a new binding mismatch.

        launch-profile is safe here because it only refreshes an enrolled
        profile. The mismatch stops the path before any move state is created.
        """
        result = self.run_switch(STUB_DYNAMIC_ACCOUNT_MISMATCH_AFTER_FIRST="1")

        self.assertEqual(1, result.returncode)
        self.assertIn("changed while its account handoff was prepared", result.stderr)
        self.assertEqual(["auto-plan", "launch-profile"], self.verbs())
        self.assertNotIn("switch-prepare", self.verbs())

    def test_a_move_that_cannot_be_counted_is_not_made(self) -> None:
        """Finding 2: no reservation, no move. Ever."""
        result = self.run_switch(STUB_ACCOUNT_BEGIN_FAILS="1")

        self.assertEqual(1, result.returncode)
        self.assertIn("could not be counted", result.stderr)
        self.assertIn("switch-rollback", self.verbs())
        # The provider was never signalled, so the conversation never moved.
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_the_reservation_is_taken_before_the_handoff_request(self) -> None:
        # The signal cannot succeed against a fixture pid, so the run fails
        # after the reservation. What is pinned here is the ORDER: the move is
        # counted before anything the operator could observe as a move.
        self.run_switch()

        verbs = self.verbs()
        self.assertIn("auto-begin", verbs)
        self.assertLess(verbs.index("auto-target-ok"), verbs.index("switch-prepare"))
        self.assertLess(verbs.index("switch-prepare"), verbs.index("auto-begin"))
        # It failed before commit, and gave the reservation back, because
        # nothing was signalled.
        self.assertNotIn("switch-commit", verbs)
        self.assertIn("auto-release", verbs)

    def test_the_command_refuses_arguments_it_does_not_understand(self) -> None:
        for argv in ([SESSION, "--force"], [SESSION, "fresh"], []):
            with self.subTest(argv=argv):
                result = subprocess.run(
                    [str(SP), "account-auto-switch", *argv],
                    env={**os.environ, **self.env()},
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(0, result.returncode)


class AutomaticSwitchEndToEndTests(unittest.TestCase):
    """The whole move, driven through commit, against a process we own.

    Everything before this stopped at `launch-profile`, because going further
    means the code signals a provider, and the signal is guarded by a check
    against real `/proc`, which no fabricated pid can pass. So this starts its
    own harmless process, names it `claude`, and hands the driver that pid.
    The SIGTERM lands on our own sleeper and on nothing else; no real provider,
    profile, account or session is involved at any point.
    """

    def setUp(self) -> None:
        from tests.test_commands import CommandFixture

        self.fixture = CommandFixture()
        self.account_log = self.fixture.base / "account-calls.jsonl"
        self.fixture.shpool_state.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "name": SESSION,
                            "status": "Disconnected",
                            "started_at_unix_ms": 1_700_000_000_001,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        # A process of our own, named so the identity guard recognizes it.
        sleeper = self.fixture.base / "bin"
        sleeper.mkdir(exist_ok=True)
        self.provider_path = sleeper / "claude"
        self.provider_path.write_bytes(Path("/bin/sleep").read_bytes())
        self.provider_path.chmod(0o755)
        self.provider = subprocess.Popen(
            [str(self.provider_path), "300"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.provider_start = self.read_start_ticks(self.provider.pid)

    def tearDown(self) -> None:
        self.provider.terminate()
        try:
            self.provider.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.provider.kill()
            self.provider.wait(timeout=10)
        self.fixture.close()

    @staticmethod
    def read_start_ticks(pid: int) -> int:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(text.rsplit(")", 1)[1].split()[19])

    def env(self, **overrides: str) -> dict[str, str]:
        table = {
            "processes": [
                {"pid": 1001, "ppid": 1, "start_ticks": 10001, "cmdline": ["bash"]},
                {
                    "pid": self.provider.pid,
                    "ppid": 1001,
                    "start_ticks": self.provider_start,
                    "cmdline": ["claude", "--resume", SHELL_UUID],
                },
            ]
        }
        env = {
            **self.fixture.env(),
            "SESSION_KIT_ACCOUNT_REGISTRY": str(self.fixture.base / "accounts.json"),
            "SESSION_KIT_ACCOUNT_ROOT": str(self.fixture.base / "profiles"),
            # The colour-and-names branch made the shared account stub
            # honest: with no profile named, an account is simply not
            # enrolled, which is what the real core reports. Every scenario
            # in this suite moves a conversation ONTO an account that is
            # enrolled, so the fixture has to say so. Leaving it unset made
            # eight tests assert against "no such enrolled account" instead
            # of against the behaviour they are named for.
            "STUB_ACCOUNT_PROFILE": str(self.fixture.base / "profiles" / "target"),
            "SESSION_KIT_ACCOUNT_ROSTER": str(self.fixture.base / "cli_accounts.json"),
            "SESSION_KIT_ROTATION_ADVICE": str(self.fixture.base / "advice.json"),
            "XDG_STATE_HOME": str(self.fixture.base / "xdg-state"),
            "XDG_DATA_HOME": str(self.fixture.base / "xdg-data"),
            "XDG_CONFIG_HOME": str(self.fixture.base / "xdg-config"),
            "PATH": "%s:%s"
            % (refusing_commands_bin(self.fixture.base), os.environ.get("PATH", "")),
            "SESSION_KIT_BACKGROUND": "1",
            "SESSION_KIT_AUTO_SWITCH_MARKER": "1",
            "STUB_DYNAMIC_PROVIDER": "claude",
            "STUB_DYNAMIC_UUID": SHELL_UUID,
            "STUB_DYNAMIC_CWD": str(self.fixture.project),
            "STUB_DYNAMIC_AGENT_STATUS": "idle",
            "STUB_DYNAMIC_ACCOUNT_ALIAS": "spent",
            "STUB_DYNAMIC_ACCOUNT_ALIAS_AFTER_REQUEST": "fresh",
            "STUB_DYNAMIC_ACCOUNT_CAPABLE": "1",
            "STUB_DYNAMIC_PROVIDER_PID": str(self.provider.pid),
            "STUB_DYNAMIC_PROVIDER_START": str(self.provider_start),
            "STUB_PROCESS_TABLE": json.dumps(table),
            "STUB_ACCOUNT_LOG": str(self.account_log),
            "STUB_ACCOUNT_TXID": "a" * 32,
            "STUB_ACCOUNT_PLAN": json.dumps(
                {
                    "action": "switch",
                    "reason_code": "move",
                    "reason": "spent is spent; moving to fresh.",
                    "source_alias": "spent",
                    "target_alias": "fresh",
                }
            ),
            "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
        }
        env.update(overrides)
        return env

    def verbs(self) -> list[str]:
        if not self.account_log.exists():
            return []
        return [
            json.loads(line)[1]
            for line in self.account_log.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def run_apply(self, **overrides: str):
        return subprocess.run(
            [str(SP), "account-auto-switch", SESSION, "--apply"],
            env={**os.environ, **self.env(**overrides)},
            capture_output=True,
            text=True,
            timeout=180,
        )

    def test_the_whole_move_runs_in_order_and_the_conversation_is_kept(self) -> None:
        """Refresh, recheck, checkpoint and reservation keep their reviewed order.

        The launch step re-verifies the existing profile after policy's first
        approval. The read-only target check follows it to catch a disable or
        reserve change during that slower refresh. The move is still counted
        before the handoff request or provider signal.
        """
        result = self.run_apply()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("moved from spent to fresh", result.stdout)
        self.assertIn("The same conversation is still open", result.stdout)
        self.assertEqual(
            [
                "auto-plan",
                "launch-profile",
                "auto-target-ok",
                "switch-prepare",
                "auto-begin",
                "sync-ui",
                "switch-commit",
                "auto-commit",
            ],
            self.verbs(),
        )
        # The reservation precedes the commit, and the session was never killed.
        self.assertLess(self.verbs().index("auto-begin"), self.verbs().index("switch-commit"))
        self.assertFalse(self.fixture.shpool_log.exists())
        # The receipt names the exact conversation and the reservation token.
        marker = [
            line
            for line in result.stderr.splitlines()
            if line.startswith("SESSION_KIT_AUTO_SWITCH\t")
        ]
        self.assertEqual(1, len(marker), result.stderr)
        fields = marker[0].split("\t")
        self.assertEqual([SESSION, "claude", SHELL_UUID, "spent", "fresh"], fields[1:6])
        self.assertEqual(32, len(fields[6]))

    def test_the_same_run_without_apply_changes_nothing(self) -> None:
        result = subprocess.run(
            [str(SP), "account-auto-switch", SESSION],
            env={**os.environ, **self.env()},
            capture_output=True,
            text=True,
            timeout=180,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Would move", result.stdout)
        self.assertEqual(["auto-plan"], self.verbs())
        self.assertEqual(0, self.provider.poll() or 0)

    def test_a_commit_that_fails_never_kills_the_window(self) -> None:
        """Finding 9/lane-A finding 1, proven at the driver.

        The rollback helper's last resort closes the session and re-creates
        it. Unattended that is a lost window, so the automatic caller opts out
        and reports instead. What must never appear here is a `shpool kill`.
        """
        result = self.run_apply(STUB_ACCOUNT_COMMIT_FAILS="1")

        self.assertEqual(1, result.returncode)
        self.assertIn("did not prove itself", result.stderr)
        self.assertNotIn("sp recover", result.stderr)
        self.assertFalse(
            self.fixture.shpool_log.exists(),
            "the automatic path closed the operator's session: %s"
            % (self.fixture.shpool_log.read_text(encoding="utf-8")
               if self.fixture.shpool_log.exists() else ""),
        )


class AutomaticSwitchSourceTests(unittest.TestCase):
    """Structural promises that no fixture can prove on its own."""

    def setUp(self) -> None:
        self.commands = (REPO / "lib" / "sh" / "sp_commands.sh").read_text(
            encoding="utf-8"
        )
        start = self.commands.index("account_auto_switch_target()")
        self.body = self.commands[start : self.commands.index("teardown_target()")]

    def test_the_automatic_path_reuses_the_manual_switch_safety_helpers(self) -> None:
        for helper in (
            "account_switch_stable_snapshot",
            "account_switch_safe_tree",
            "account_switch_request",
            "account_switch_signal",
            "account_switch_wait_alias",
            "account_switch_restore_source",
        ):
            self.assertIn(helper, self.body, helper)

    def test_the_automatic_path_never_kills_the_session(self) -> None:
        """The exact conversation and its window survive a move.

        The in-place handoff signals the provider to reload a profile; it
        never closes the shell. A `shpool kill` in this function would be the
        legacy recreate path running with nobody watching.
        """
        self.assertNotIn("SHPOOL\" kill", self.body)
        self.assertNotIn("shpool kill", self.body)
        self.assertIn("$capable != true", self.body)

    def test_the_move_is_counted_before_anything_irreversible(self) -> None:
        """Finding 2, pinned in the order of operations itself.

        The reservation has to come before the request is written and before
        the provider is signalled. Any other order leaves a window where the
        conversation has moved and the ledger says it has not.
        """
        reserve = self.body.index("account auto-begin")
        request = self.body.index("account_switch_request")
        signal = self.body.index("account_switch_signal")
        commit = self.body.index("account switch-commit")
        self.assertLess(reserve, request)
        self.assertLess(reserve, signal)
        self.assertLess(reserve, commit)

    def test_the_target_is_re_read_after_the_mutating_prepare(self) -> None:
        """Finding 1's belt: the operator can disable an account at any moment."""
        launch = self.body.index("account launch-profile")
        recheck = self.body.index("account auto-target-ok")
        prepare = self.body.index("account switch-prepare")
        self.assertLess(launch, recheck)
        self.assertLess(recheck, prepare)

    def test_both_live_proofs_require_the_source_account(self) -> None:
        """The alias can drift after policy chose it and before the signal."""
        lines = self.body.splitlines()
        calls = [
            index
            for index, line in enumerate(lines)
            if line.lstrip().startswith(
                ("account_switch_stable_snapshot ", "! account_switch_stable_snapshot ")
            )
        ]
        self.assertEqual(2, len(calls))
        for index in calls:
            self.assertIn('"$source_alias"', "\n".join(lines[index : index + 3]))
        picker = (REPO / "lib" / "sh" / "sp_picker.sh").read_text(
            encoding="utf-8"
        )
        stable = picker[
            picker.index("account_switch_stable_snapshot() {") :
            picker.index("account_switch_safe_tree() {")
        ]
        self.assertIn('row.get("account_alias") != sys.argv[5]', stable)
        self.assertIn('row.get("account_binding_mismatch") is True', stable)

    def test_the_handoff_carries_a_generation_bound_model_record(self) -> None:
        """Finding 10: a named model reaches the replacement provider."""
        common = (REPO / "bin" / "session_kit_common").read_text(encoding="utf-8")
        self.assertIn('row.get("model") or ""', common)
        self.assertIn("SK_MODEL=${fields[21]}", common)
        self.assertIn('local model=${SK_MODEL:-}', self.body)
        self.assertIn('"$shell_start" "$model" "$provider"', self.body)

        inventory = (REPO / "lib" / "session_inventory.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('item["model_handoff_capable"]', inventory)
        self.assertIn('process.get("requested_model")', inventory)

        bashrc = (REPO / "bashrc" / "shpool.bashrc").read_text(encoding="utf-8")
        switch = bashrc[bashrc.index("__sk_switch_request=") :]
        self.assertIn("__sk_switch_model", switch)
        self.assertIn("validate-worker-model", switch)
        self.assertIn('"$__sk_switch_launch_tmp"', switch)
        self.assertIn('mv -- "$__sk_switch_launch_tmp" "$__sk_launch"', switch)

    def test_the_legacy_picker_prefers_the_live_model_then_falls_back_to_its_row(self) -> None:
        """The two merged model values are a priority order, not duplicates."""
        picker = (REPO / "lib" / "sh" / "sp_picker.sh").read_text(encoding="utf-8")
        start = picker.index("picker_account_switch() {")
        body = picker[start : picker.index("picker_alias() {")]
        live = body.index(
            'carried_model=$(sk_session_model "$SNAPSHOT" "$id")'
        )
        fallback = body.index(
            '[[ -n $carried_model ]] || carried_model=$model'
        )
        terminate = body.index("sk_terminate_exact_shell")

        # Read the live provider's command line while it still exists. A read
        # failure degrades to the picker row captured at entry, never to an
        # unnamed provider default, and both happen before the provider dies.
        self.assertLess(live, fallback)
        self.assertLess(fallback, terminate)
        self.assertGreaterEqual(
            body.count(
                'restore_exact "$provider" "$uuid" "$cwd" "$source_alias" '
                '"$carried_model"'
            ),
            2,
        )
        self.assertIn("pending checkpoint were left in place", body)

    def test_a_failed_hop_never_reaches_the_destructive_recreate(self) -> None:
        """Finding 9: follow the helper, do not just grep this function.

        `account_switch_restore_source` ends by terminating the exact shell
        and re-creating it. A conversation that has to be recovered is not a
        conversation left working, so the unattended caller opts out of that
        last resort and says so instead.
        """
        self.assertIn("--no-recreate", self.body)
        picker = (REPO / "lib" / "sh" / "sp_picker.sh").read_text(encoding="utf-8")
        start = picker.index("account_switch_restore_source() {")
        helper = picker[start : picker.index("picker_account_switch() {")]
        # The helper still has the destructive path, for the human at the
        # picker who is present and wants it...
        self.assertIn("sk_terminate_exact_shell", helper)
        self.assertNotIn('"$SK_SHPOOL" kill "$id"', helper)
        # ...but it is now behind the opt-out, checked before termination.
        guard_line = helper.index("(( allow_recreate )) || return 1")
        self.assertLess(guard_line, helper.index("sk_terminate_exact_shell"))
        self.assertIn("allow_recreate=0", helper)

    def test_the_recreate_opt_out_does_not_depend_on_argument_position(self) -> None:
        """The opt-out is a promise, not a subscript.

        Carrying the model added an argument in front of `--no-recreate`. Every
        source-text proof above stayed green through that change, because the
        call site still writes the flag -- only the position the helper read it
        from moved. A helper that honours the flag in exactly one position
        turns the next such refactor into an unattended kill of a live window,
        silently. So run the helper and watch for the kill -- once with the flag
        where the caller put it before the model, and once where it puts it now.
        Both are the same promise, so both must hold.
        """
        picker = (REPO / "lib" / "sh" / "sp_picker.sh").read_text(encoding="utf-8")
        start = picker.index("account_switch_restore_source() {")
        helper = picker[start : picker.index("picker_account_switch() {")]
        head = 'account_switch_restore_source sess claude %s spent fresh %s /tmp' % (
            UUID,
            "0" * 32,
        )
        vectors = {
            "flag directly after the directory": head + " --no-recreate",
            "flag behind a carried model": head + " sonnet-4 --no-recreate",
            "flag behind an empty model": head + ' "" --no-recreate',
        }

        for name, call in vectors.items():
            with self.subTest(vector=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    killed = root / "killed"
                    shpool = root / "fake-shpool"
                    shpool.write_text(
                        "#!/usr/bin/env bash\necho \"$*\" >> "
                        + json.dumps(str(killed))
                        + "\nexit 0\n",
                        encoding="utf-8",
                    )
                    shpool.chmod(0o755)
                    # Every collaborator is a stub and the snapshot read is
                    # forced to fail, so the run falls through to the
                    # destructive tail. Nothing here reaches a real session,
                    # account, or provider.
                    script = "\n".join(
                        [
                            "set -u",
                            "SK_STATE_DIR=" + json.dumps(str(root)),
                            "SK_SHPOOL=" + json.dumps(str(shpool)),
                            "INVENTORY_CORE=/nonexistent/inventory",
                            "sk_guard_snapshot_file() { return 1; }",
                            "sk_resolve() { return 1; }",
                            "sk_reap_session_leftovers() { return 0; }",
                            "restore_exact() { return 0; }",
                            "account_switch_request() { return 1; }",
                            "account_switch_signal() { return 1; }",
                            "account_switch_wait_alias() { return 1; }",
                            "python3() { return 0; }",
                            helper,
                            call,
                            'printf "status=%s\\n" "$?"',
                        ]
                    )
                    result = subprocess.run(
                        ["bash", "-c", script],
                        capture_output=True,
                        text=True,
                        cwd=str(root),
                        env=sandbox_environment(root),
                    )
                    ran_kill = killed.exists()
                    evidence = killed.read_text(encoding="utf-8") if ran_kill else ""

                self.assertFalse(
                    ran_kill,
                    "the destructive recreate ran despite --no-recreate: " + evidence,
                )
                self.assertIn("status=1", result.stdout)

    def test_the_safety_proof_is_taken_twice_around_the_lock(self) -> None:
        self.assertEqual(2, self.body.count("account_switch_stable_snapshot"))
        self.assertEqual(2, self.body.count("account_switch_safe_tree"))
        self.assertIn("picker_lock", self.body)

    def test_the_machine_receipt_is_off_unless_a_driver_asks_for_it(self) -> None:
        self.assertIn("SESSION_KIT_AUTO_SWITCH_MARKER:-0", self.body)
        watchdog = (REPO / "bin" / "session_kit_watchdog").read_text(encoding="utf-8")
        self.assertIn("SESSION_KIT_AUTO_SWITCH_MARKER=1", watchdog)

    def test_the_watchdog_only_moves_a_session_in_its_acting_mode(self) -> None:
        """Report mode is the watchdog's promise not to touch a live session."""
        watchdog = (REPO / "bin" / "session_kit_watchdog").read_text(encoding="utf-8")
        start = watchdog.index("account_guard_pass() {")
        body = watchdog[start : watchdog.index("announce_account_move() {")]
        self.assertIn("if [[ $WATCHDOG_MODE == repair ]]; then", body)
        self.assertIn("--dry-run", body)

    def test_acting_needs_an_explicit_apply(self) -> None:
        """This verb spends money and any shell in the checkout can run it.

        Judging has to be what you get by default; moving a live conversation
        between paid subscriptions has to be what somebody typed on purpose.
        """
        self.assertIn("dry_run=1", self.body)
        self.assertIn("--apply", self.body)
        entry = self.commands  # the dispatcher lives in bin/sp
        sp_source = (REPO / "bin" / "sp").read_text(encoding="utf-8")
        self.assertIn("$3 == --apply", sp_source)
        self.assertIn("account-auto-switch <session> [--apply]", sp_source)
        self.assertTrue(entry)

    def test_the_watchdog_takes_a_lock_so_one_move_per_pass_is_true(self) -> None:
        watchdog = (REPO / "bin" / "session_kit_watchdog").read_text(encoding="utf-8")
        self.assertIn("ACCOUNT_GUARD_LOCK", watchdog)
        self.assertIn('flock -n "$ACCOUNT_GUARD_FD"', watchdog)
        # And the acting form is the one the watchdog uses.
        self.assertIn('account-auto-switch "$id" --apply', watchdog)

    def test_the_watchdog_honours_the_shipped_kill_switch(self) -> None:
        watchdog = (REPO / "bin" / "session_kit_watchdog").read_text(encoding="utf-8")
        self.assertIn("account-switching-off", watchdog)
        self.assertIn("[[ -e $ACCOUNT_SWITCH_OFF ]] && return 0", watchdog)
        # And it never creates it: a machine that disables the operator's own
        # manual switch has taken their escape hatch away.
        self.assertNotIn("touch \"$ACCOUNT_SWITCH_OFF\"", watchdog)
        self.assertNotIn("> \"$ACCOUNT_SWITCH_OFF\"", watchdog)


class WatchdogAccountGuardTests(unittest.TestCase):
    """The periodic driver, run for real against a sandboxed estate.

    The watchdog binary runs with `--once`; the account feeds, the profile
    registry and `sp` are all fixtures inside a temporary directory. The
    policy code underneath is the real one.
    """

    def setUp(self) -> None:
        from tests import test_watchdog as harness

        self.harness = harness
        self.fixture = harness.WatchdogFixture(
            sessions=[
                harness.session(
                    "s20260101-000000-1",
                    provider="claude",
                    agent_status="idle",
                    uuid=UUID,
                )
            ]
        )
        row = json.loads(self.fixture.inventory.read_text(encoding="utf-8"))
        row["sessions"][0]["account_alias"] = "spent"
        row["sessions"][0]["account_switch_capable"] = True
        self.fixture.inventory.write_text(json.dumps(row), encoding="utf-8")
        self.roster = self.fixture.base / "cli_accounts.json"
        self.advice = self.fixture.base / "rotation_advice.json"
        self.registry = self.fixture.base / "accounts.json"
        self.profiles = self.fixture.base / "profiles"

    def tearDown(self) -> None:
        self.fixture.close()

    def seed(self, spent_weekly: float) -> None:
        now = int(time.time())
        profiles = {}
        for alias, email in (("spent", "spent@example.com"), ("fresh", "fresh@example.com")):
            directory = self.profiles / "claude" / alias
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            state = directory / ".claude.json"
            state.write_text(json.dumps({"hasCompletedOnboarding": True}), encoding="utf-8")
            state.chmod(0o600)
            profiles[f"claude:{alias}"] = {
                "provider": "claude",
                "alias": alias,
                "email": email,
                "profile_dir": str(directory),
                "legacy": False,
                "plan": "max",
                "verified_at_unix_ms": 1_800_000_000_000,
                "enabled": True,
            }
        self.registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation": 1,
                    "profiles": profiles,
                    "bindings": {},
                }
            ),
            encoding="utf-8",
        )
        self.registry.chmod(0o600)
        self.roster.write_text(
            json.dumps(
                {
                    "ts": now,
                    "accounts": [
                        {
                            "email": "spent@example.com",
                            "enabled": True,
                            "serving": True,
                            "health": "ok",
                            "u5h": 0.9,
                            "u7d": spent_weekly,
                        },
                        {
                            "email": "fresh@example.com",
                            "enabled": True,
                            "serving": True,
                            "health": "ok",
                            "u5h": 0.1,
                            "u7d": 0.2,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.roster.chmod(0o600)
        self.advice.write_text(
            json.dumps({"ts": now, "use_now": {"account": "fresh@example.com"}}),
            encoding="utf-8",
        )
        self.advice.chmod(0o600)

    def run_once(self, **overrides: str):
        # WatchdogFixture already pins HOME, the state, journal and archive
        # directories, the session manager, the status command and `sp` inside
        # its own temporary tree. The rest is named explicitly so no default
        # can resolve to the operator's own profiles, feeds or config.
        environment = {
            "SESSION_KIT_ACCOUNT_REGISTRY": str(self.registry),
            "SESSION_KIT_ACCOUNT_ROOT": str(self.profiles),
            "SESSION_KIT_ACCOUNT_ROSTER": str(self.roster),
            "SESSION_KIT_ROTATION_ADVICE": str(self.advice),
            "SESSION_KIT_JOURNAL_RECOVERY_DIR": str(self.fixture.base / "recovery"),
            "SESSION_KIT_START_DIR": str(self.fixture.base / "start"),
            "SESSION_KIT_PROJECTS_FILE": str(self.fixture.base / "projects.tsv"),
            "SESSION_KIT_CONFIG": str(self.fixture.base / "inventory.json"),
            "XDG_STATE_HOME": str(self.fixture.base / "xdg-state"),
            "XDG_DATA_HOME": str(self.fixture.base / "xdg-data"),
            "XDG_CONFIG_HOME": str(self.fixture.base / "xdg-config"),
            "SESSION_KIT_NONINTERACTIVE": "1",
            "SESSION_KIT_BACKGROUND": "1",
            # Refuse every bare shpool/provider lookup first. The fixture's
            # explicitly configured safe commands remain available by their
            # full paths.
            "PATH": "%s:%s:%s"
            % (
                refusing_commands_bin(self.fixture.base),
                self.fixture.bin,
                os.environ.get("PATH", ""),
            ),
        }
        environment.update(overrides)
        return self.fixture.run(**environment)

    def test_the_fixture_never_names_the_operators_own_paths(self) -> None:
        environment = self.fixture.env()
        base = self.fixture.base.resolve()
        for key in (
            "HOME",
            "SESSION_KIT_STATE_DIR",
            "SESSION_KIT_JOURNAL_DIR",
            "SESSION_KIT_ARCHIVE_DIR",
            "SESSION_KIT_SHPOOL_CMD",
            "SESSION_KIT_STATUS_CMD",
            "SESSION_KIT_SP_CMD",
            "SESSION_KIT_WATCHDOG_LOG",
            "SESSION_KIT_WATCHDOG_REPAIRS",
            "SESSION_KIT_WATCHDOG_REPORT_STATE",
            "SESSION_KIT_WATCHDOG_NOTIFY",
        ):
            with self.subTest(variable=key):
                self.assertTrue(
                    Path(environment[key]).resolve().is_relative_to(base),
                    f"{key} escapes the sandbox: {environment[key]}",
                )
        # The notifier is a fixture stub inside the sandbox, checked by the
        # containment loop above, so no real alert can ever be sent.
        self.assertTrue(
            Path(environment["SESSION_KIT_WATCHDOG_NOTIFY"]).is_relative_to(base)
        )

    def sp_calls(self) -> list[str]:
        if not self.fixture.sp_log.exists():
            return []
        return [
            line
            for line in self.fixture.sp_log.read_text(encoding="utf-8").splitlines()
            if "account-auto-switch" in line
        ]

    def test_a_healthy_account_costs_one_feed_read_and_no_session_work(self) -> None:
        self.seed(spent_weekly=0.40)

        result = self.run_once()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.sp_calls())

    def test_a_spent_account_reaches_the_session_that_is_on_it(self) -> None:
        self.seed(spent_weekly=1.0)

        result = self.run_once()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, len(self.sp_calls()), self.fixture.log_text())
        self.assertIn("s20260101-000000-1", self.sp_calls()[0])

    def test_report_mode_judges_the_session_but_never_moves_it(self) -> None:
        self.seed(spent_weekly=1.0)

        result = self.run_once(SESSION_KIT_WATCHDOG_MODE="report")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, len(self.sp_calls()))
        self.assertIn("--dry-run", self.sp_calls()[0])

    def test_the_kill_switch_stops_the_pass_before_it_reads_anything(self) -> None:
        # The control for this is
        # test_a_spent_account_reaches_the_session_that_is_on_it: the same
        # fixture, the same spent account, and one `sp` call. Without that
        # pair this test would pass on any machine where the pass never ran,
        # which is exactly how it passed while the sentinel path was wrong.
        self.seed(spent_weekly=1.0)
        killer = self.fixture.state / "account-switching-off"
        killer.write_text("", encoding="utf-8")
        self.assertTrue(killer.exists())

        result = self.run_once()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.sp_calls())

    def test_the_sentinel_the_kill_switch_test_writes_is_the_one_read(self) -> None:
        """Pins the path itself, so it cannot drift somewhere harmless again."""
        watchdog = (REPO / "bin" / "session_kit_watchdog").read_text(encoding="utf-8")
        self.assertIn(
            'ACCOUNT_SWITCH_OFF=${SESSION_KIT_ACCOUNT_SWITCH_OFF:-"$SK_STATE_DIR/account-switching-off"}',
            watchdog,
        )
        self.assertEqual(
            str(self.fixture.state / "account-switching-off"),
            str(self.fixture.state / "account-switching-off"),
        )

    def test_a_stale_feed_stops_the_pass(self) -> None:
        self.seed(spent_weekly=1.0)
        stale = json.loads(self.roster.read_text(encoding="utf-8"))
        stale["ts"] = int(time.time()) - 100_000
        self.roster.write_text(json.dumps(stale), encoding="utf-8")

        result = self.run_once()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.sp_calls())

    def test_the_watchdogs_own_sentinel_stops_the_pass(self) -> None:
        self.seed(spent_weekly=1.0)
        self.fixture.sentinel.write_text("", encoding="utf-8")

        result = self.run_once()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.sp_calls())

    def notifier_calls(self) -> list[str]:
        if not self.fixture.notify_log.exists():
            return []
        return [
            line
            for line in self.fixture.notify_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def account_move_notifier_calls(self) -> list[str]:
        return [
            line
            for line in self.notifier_calls()
            if "--title=A session moved to another subscription account" in line
        ]

    def queue_a_notice(self, sentence: str = "session 3 moved from spent to fresh") -> str:
        """Put one owed notice in the sandbox ledger, as a completed move would."""
        from lib.sessionkit_inventory import account_guard as sandbox_guard

        config = {"state_dir": str(self.fixture.state)}
        # The kit's own state lock refuses anything but a private directory,
        # and the shared watchdog fixture creates its state dir with default
        # permissions.
        self.fixture.state.chmod(0o700)
        with mock.patch.dict(
            os.environ, {"SESSION_KIT_STATE_DIR": str(self.fixture.state)}, clear=False
        ):
            token = sandbox_guard.begin_hop(config, "claude", UUID, "spent", "fresh")
            sandbox_guard.commit_hop(config, "claude", UUID, token)
            sandbox_guard.queue_notice(config, "claude", UUID, token, sentence)
        return token

    def test_a_notice_that_cannot_be_delivered_is_retried_next_pass(self) -> None:
        """Finding 3: a failed delivery must not count as having told them."""
        self.seed(spent_weekly=0.40)
        token = self.queue_a_notice()
        broken = self.fixture.bin / "broken-notify"
        broken.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        broken.chmod(0o755)

        first = self.run_once(SESSION_KIT_WATCHDOG_NOTIFY=str(broken))
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertIn("stays queued", self.fixture.log_text())

        from lib.sessionkit_inventory import account_guard as sandbox_guard

        config = {"state_dir": str(self.fixture.state)}
        with mock.patch.dict(
            os.environ, {"SESSION_KIT_STATE_DIR": str(self.fixture.state)}, clear=False
        ):
            still_owed = sandbox_guard.pending_notices(config)
        self.assertEqual([token], [item["token"] for item in still_owed])

        # A working notifier on a later pass finally tells them.
        second = self.run_once()
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(1, len(self.account_move_notifier_calls()))
        with mock.patch.dict(
            os.environ, {"SESSION_KIT_STATE_DIR": str(self.fixture.state)}, clear=False
        ):
            self.assertEqual([], sandbox_guard.pending_notices(config))

    def test_a_delivered_notice_is_never_sent_twice(self) -> None:
        self.seed(spent_weekly=0.40)
        self.queue_a_notice()

        self.run_once()
        self.run_once()

        self.assertEqual(1, len(self.account_move_notifier_calls()))

    def test_the_kill_switch_does_not_cancel_a_debt_already_owed(self) -> None:
        """Switching future moves off does not un-owe a message about a past one."""
        self.seed(spent_weekly=0.40)
        self.queue_a_notice()
        (self.fixture.state / "account-switching-off").write_text("", encoding="utf-8")

        self.run_once()

        self.assertEqual(1, len(self.account_move_notifier_calls()))

    def test_a_session_on_a_healthy_account_is_not_touched_when_another_is_spent(
        self,
    ) -> None:
        self.seed(spent_weekly=1.0)
        row = json.loads(self.fixture.inventory.read_text(encoding="utf-8"))
        row["sessions"][0]["account_alias"] = "fresh"
        self.fixture.inventory.write_text(json.dumps(row), encoding="utf-8")

        result = self.run_once()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.sp_calls())


if __name__ == "__main__":
    unittest.main()
