"""The selection equation: what it prefers, what it refuses, what it admits."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib.sessionkit_supervisor import duty_scoring as ds
from lib.sessionkit_supervisor import quota_sources as qs


NOW = 1_800_000_000

CATALOG = (
    ds.ModelOption("claude", "deep", "vendor-deep-1", strength=4, cost=4),
    ds.ModelOption("claude", "mid", "vendor-mid-1", strength=2, cost=2),
    ds.ModelOption("codex", "other", "vendor-other-1", strength=3, cost=2),
)


def refs(*rows: tuple[str, str]) -> list[qs.AccountRef]:
    return [
        qs.AccountRef(provider=provider, alias=alias, email=f"{alias}@invalid.example")
        for provider, alias in rows
    ]


def snapshot(*readings: qs.QuotaReading) -> qs.QuotaSnapshot:
    return qs.QuotaSnapshot(taken_at_unix=NOW, readings=list(readings))


def reading(
    provider: str,
    alias: str,
    window: str,
    *,
    used: float | None = None,
    load: float | None = None,
    resets: int | None = None,
    exhausted: bool = False,
    confidence: str = qs.MEASURED,
) -> qs.QuotaReading:
    return qs.QuotaReading(
        provider=provider,
        account=alias,
        window=window,
        source="test",
        confidence=confidence,
        observed_at_unix=NOW,
        used_fraction=used,
        load_units=load,
        load_unit="tokens/h" if load is not None else "",
        resets_at_unix=resets,
        exhausted=exhausted,
    )


def score(duty: ds.Duty, accounts: list[qs.AccountRef], snap: qs.QuotaSnapshot):
    return ds.score_duty(
        duty, accounts, snap, catalog=CATALOG, weights=ds.DEFAULT_WEIGHTS
    )


class DutyTests(unittest.TestCase):
    def test_an_unknown_work_type_is_refused_at_construction(self) -> None:
        with self.assertRaises(ds.ScoringError) as caught:
            ds.Duty(duty_id="d1", title="whatever", work_type="vibes")

        self.assertIn("design", str(caught.exception))

    def test_a_duty_needs_an_id(self) -> None:
        with self.assertRaises(ds.ScoringError):
            ds.Duty(duty_id="", title="x", work_type="docs")


class FitTests(unittest.TestCase):
    def test_deep_work_goes_to_the_strong_model_and_mechanical_to_the_cheap_one(
        self,
    ) -> None:
        accounts = refs(("claude", "one"))
        snap = snapshot(
            reading("claude", "one", qs.WEEKLY, used=0.1),
            reading("claude", "one", qs.FIVE_HOUR, used=0.1),
        )

        design = score(ds.Duty("d1", "Design the engine", "design"), accounts, snap)
        mechanical = score(ds.Duty("d2", "Rename a key", "mechanical"), accounts, snap)

        assert design.chosen is not None and mechanical.chosen is not None
        self.assertEqual("deep", design.chosen.model)
        self.assertEqual("mid", mechanical.chosen.model)

    def test_quota_alone_does_not_move_deep_work_onto_a_weaker_model(self) -> None:
        """A spare account is not a reason to design on a weaker model."""
        accounts = refs(("claude", "busy"), ("claude", "spare"))
        snap = snapshot(
            reading("claude", "busy", qs.WEEKLY, used=0.7),
            reading("claude", "busy", qs.FIVE_HOUR, used=0.5),
            reading("claude", "spare", qs.WEEKLY, used=0.0),
            reading("claude", "spare", qs.FIVE_HOUR, used=0.0),
        )

        result = score(ds.Duty("d1", "Design the engine", "design"), accounts, snap)

        assert result.chosen is not None
        self.assertEqual("deep", result.chosen.model)
        # Quota still decides *which account* runs the strong model.
        self.assertEqual("spare", result.chosen.account)


class QuotaTests(unittest.TestCase):
    def test_the_account_with_more_left_wins_a_tie_on_everything_else(self) -> None:
        accounts = refs(("claude", "low"), ("claude", "high"))
        snap = snapshot(
            reading("claude", "low", qs.WEEKLY, used=0.9),
            reading("claude", "low", qs.FIVE_HOUR, used=0.9),
            reading("claude", "high", qs.WEEKLY, used=0.1),
            reading("claude", "high", qs.FIVE_HOUR, used=0.1),
        )

        result = score(ds.Duty("d1", "Design", "design"), accounts, snap)

        assert result.chosen is not None
        self.assertEqual("high", result.chosen.account)

    def test_an_exhausted_account_is_skipped_but_still_shown_with_its_reason(
        self,
    ) -> None:
        """Decision 10 asks for the skipped accounts too, with their numbers."""
        accounts = refs(("claude", "spent"), ("claude", "fresh"))
        snap = snapshot(
            reading("claude", "spent", qs.FIVE_HOUR, exhausted=True, used=1.0),
            reading("claude", "spent", qs.WEEKLY, used=0.4),
            reading("claude", "fresh", qs.WEEKLY, used=0.2),
            reading("claude", "fresh", qs.FIVE_HOUR, used=0.2),
        )

        result = score(ds.Duty("d1", "Design", "design"), accounts, snap)

        assert result.chosen is not None
        self.assertEqual("fresh", result.chosen.account)
        skipped = [row for row in result.considered if row.account == "spent"]
        self.assertTrue(skipped)
        self.assertTrue(all(row.excluded for row in skipped))
        self.assertIn("exhausted", skipped[0].exclusion_reason)
        # The readings travel with the skipped row so the proposal can print
        # what the account actually had left.
        self.assertTrue(skipped[0].readings)
        self.assertIn("skipped", skipped[0].rationale())

    def test_an_almost_spent_window_excludes_before_it_hits_zero(self) -> None:
        accounts = refs(("claude", "one"))
        snap = snapshot(reading("claude", "one", qs.WEEKLY, used=0.99))

        result = score(ds.Duty("d1", "Design", "design"), accounts, snap)

        self.assertIsNone(result.chosen)
        self.assertTrue(all(row.excluded for row in result.considered))
        self.assertIn("no eligible placement", " ".join(result.notes))

    def test_a_disabled_account_never_runs_work(self) -> None:
        accounts = [
            qs.AccountRef(provider="claude", alias="off", enabled=False),
            qs.AccountRef(provider="claude", alias="on"),
        ]
        snap = snapshot(reading("claude", "on", qs.WEEKLY, used=0.5))

        result = score(ds.Duty("d1", "Design", "design"), accounts, snap)

        assert result.chosen is not None
        self.assertEqual("on", result.chosen.account)
        off = next(row for row in result.considered if row.account == "off")
        self.assertIn("disabled", off.exclusion_reason)

    def test_a_window_about_to_reset_is_not_written_off(self) -> None:
        accounts = refs(("claude", "resetting"))
        snap = snapshot(
            reading("claude", "resetting", qs.WEEKLY, used=0.1),
            reading(
                "claude", "resetting", qs.FIVE_HOUR, used=0.95, resets=NOW + 5 * 60
            ),
        )

        result = score(ds.Duty("d1", "Design", "design"), accounts, snap)

        assert result.chosen is not None
        window = next(term for term in result.chosen.terms if term.name == "window")
        self.assertAlmostEqual(ds.IMMINENT_RESET_FLOOR, window.value)
        self.assertIn("resets in 5m", window.detail)


class UnknownAndRelativeTests(unittest.TestCase):
    def test_an_unknown_window_scores_a_neutral_prior_and_says_so(self) -> None:
        accounts = refs(("claude", "one"))
        snap = snapshot(reading("claude", "one", qs.WEEKLY, used=0.2))

        result = score(ds.Duty("d1", "Design", "design"), accounts, snap)

        assert result.chosen is not None
        window = next(term for term in result.chosen.terms if term.name == "window")
        self.assertEqual("unknown", window.basis)
        self.assertAlmostEqual(ds.UNKNOWN_PRIOR, window.value)
        self.assertIn("neutral prior", " ".join(result.notes))

    def test_accounts_with_no_published_allowance_rank_by_relative_load(self) -> None:
        accounts = refs(("claude", "hot"), ("claude", "cold"))
        snap = snapshot(
            reading("claude", "hot", qs.WEEKLY, load=1_000_000.0),
            reading("claude", "cold", qs.WEEKLY, load=1_000.0),
        )

        result = score(ds.Duty("d1", "Design", "design"), accounts, snap)

        assert result.chosen is not None
        self.assertEqual("cold", result.chosen.account)
        weekly = next(term for term in result.chosen.terms if term.name == "weekly")
        self.assertEqual("relative-load", weekly.basis)
        self.assertIn("no published allowance", weekly.detail)

    def test_relative_load_never_claims_to_be_a_percentage(self) -> None:
        accounts = refs(("claude", "one"))
        snap = snapshot(reading("claude", "one", qs.WEEKLY, load=5_000.0))

        result = score(ds.Duty("d1", "Design", "design"), accounts, snap)

        assert result.chosen is not None
        weekly = next(term for term in result.chosen.terms if term.name == "weekly")
        self.assertNotIn("%", weekly.detail)


class CodexEqualityTests(unittest.TestCase):
    def test_a_codex_account_is_scored_on_the_same_terms_as_a_claude_one(self) -> None:
        """The audit finding, at the scoring layer: no provider is a special case."""
        accounts = refs(("claude", "one"), ("codex", "two"))
        snap = snapshot(
            reading("claude", "one", qs.WEEKLY, used=0.5),
            reading("claude", "one", qs.FIVE_HOUR, used=0.5),
            reading("codex", "two", qs.WEEKLY, used=0.5),
            reading("codex", "two", qs.FIVE_HOUR, used=0.5),
        )

        result = score(ds.Duty("d1", "Review a diff", "review"), accounts, snap)

        providers = {row.provider for row in result.considered}
        self.assertEqual({"claude", "codex"}, providers)
        # Same quota, same work: the codex model's strength 3 matches a review
        # duty exactly, so it wins on fit rather than on provider identity.
        assert result.chosen is not None
        self.assertEqual("codex", result.chosen.provider)
        codex_terms = {term.name for term in result.chosen.terms}
        self.assertEqual({"fit", "weekly", "window", "cost"}, codex_terms)

    def test_a_codex_account_out_of_quota_loses_to_claude_and_vice_versa(self) -> None:
        accounts = refs(("claude", "one"), ("codex", "two"))
        snap = snapshot(
            reading("claude", "one", qs.WEEKLY, used=0.1),
            reading("claude", "one", qs.FIVE_HOUR, used=0.1),
            reading("codex", "two", qs.WEEKLY, used=1.0, exhausted=True),
        )

        result = score(ds.Duty("d1", "Review a diff", "review"), accounts, snap)

        assert result.chosen is not None
        self.assertEqual("claude", result.chosen.provider)


class ModelScopedLimitTests(unittest.TestCase):
    def scoped(self, hint: str) -> qs.QuotaReading:
        return qs.QuotaReading(
            provider="claude",
            account="one",
            window=qs.FIVE_HOUR,
            source="test",
            confidence=qs.OBSERVED,
            observed_at_unix=NOW,
            used_fraction=1.0,
            exhausted=True,
            model_hint=hint,
        )

    def test_one_model_s_limit_leaves_the_account_s_other_models_running(self) -> None:
        accounts = refs(("claude", "one"))
        snap = snapshot(
            reading("claude", "one", qs.WEEKLY, used=0.1),
            reading("claude", "one", qs.FIVE_HOUR, used=0.1),
            self.scoped("vendordeep1"),
        )

        result = score(ds.Duty("d1", "Design", "design"), accounts, snap)

        assert result.chosen is not None
        # The strong model is out; the account is not.
        self.assertEqual("mid", result.chosen.model)
        deep = next(row for row in result.considered if row.model == "deep")
        self.assertTrue(deep.excluded)
        self.assertIn("this model's own limit", deep.exclusion_reason)

    def test_a_model_limit_does_not_drag_down_the_account_s_headroom(self) -> None:
        snap = snapshot(
            reading("claude", "one", qs.FIVE_HOUR, used=0.2),
            self.scoped("vendordeep1"),
        )

        best = snap.best("claude", "one", qs.FIVE_HOUR)

        assert best is not None
        self.assertAlmostEqual(0.2, best.used_fraction or 0.0)
        self.assertFalse(best.exhausted)


class DeterminismTests(unittest.TestCase):
    def test_the_same_inputs_produce_the_same_order_every_time(self) -> None:
        accounts = refs(("claude", "b"), ("claude", "a"), ("codex", "c"))
        snap = snapshot(
            *[
                reading("claude", alias, window, used=0.5)
                for alias in ("a", "b")
                for window in qs.WINDOWS
            ],
            *[reading("codex", "c", window, used=0.5) for window in qs.WINDOWS],
        )
        duty = ds.Duty("d1", "Design", "design")

        first = [row.key for row in score(duty, accounts, snap).considered]
        second = [row.key for row in score(duty, accounts, snap).considered]

        self.assertEqual(first, second)
        # A tie breaks on the name, so the order is readable rather than
        # dependent on which account happened to be registered first.
        tied = [key for key in first if key.startswith("claude/deep")]
        self.assertEqual(sorted(tied), tied)

    def test_scoring_many_duties_reuses_one_snapshot(self) -> None:
        accounts = refs(("claude", "one"))
        snap = snapshot(reading("claude", "one", qs.WEEKLY, used=0.3))
        duties = [
            ds.Duty("d1", "Design", "design"),
            ds.Duty("d2", "Docs", "docs"),
        ]

        results = ds.score_duties(duties, accounts, snap, catalog=CATALOG)

        self.assertEqual(["d1", "d2"], [row.duty.duty_id for row in results])
        self.assertEqual("deep", results[0].chosen.model)  # type: ignore[union-attr]
        self.assertEqual("mid", results[1].chosen.model)  # type: ignore[union-attr]


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-scoring.")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_the_default_catalog_names_both_providers(self) -> None:
        providers = {option.provider for option in ds.DEFAULT_CATALOG}

        self.assertEqual({"claude", "codex"}, providers)

    def test_an_operator_catalog_replaces_the_defaults_wholesale(self) -> None:
        path = self.root / "catalog.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "provider": "codex",
                        "model": "only",
                        "model_id": "vendor-only-1",
                        "strength": 3,
                        "cost": 1,
                    }
                ]
            ),
            encoding="utf-8",
        )

        catalog = ds.load_catalog({"SESSION_KIT_MODEL_CATALOG": str(path)})

        self.assertEqual(1, len(catalog))
        self.assertEqual("only", catalog[0].model)

    def test_a_broken_catalog_is_refused_rather_than_silently_ignored(self) -> None:
        """Falling back to defaults would run work on a model that was removed."""
        path = self.root / "catalog.json"
        path.write_text("[]", encoding="utf-8")

        with self.assertRaises(ds.ScoringError):
            ds.load_catalog({"SESSION_KIT_MODEL_CATALOG": str(path)})
        with self.assertRaises(ds.ScoringError):
            ds.load_catalog({"SESSION_KIT_MODEL_CATALOG": str(self.root / "gone")})
        path.write_text(
            json.dumps([{"provider": "codex", "model": "x", "strength": 9, "cost": 1}]),
            encoding="utf-8",
        )
        with self.assertRaises(ds.ScoringError):
            ds.load_catalog({"SESSION_KIT_MODEL_CATALOG": str(path)})

    def test_weights_are_overridable_and_validated(self) -> None:
        weights = ds.load_weights({"SESSION_KIT_DUTY_WEIGHTS": '{"fit": 0.9}'})

        self.assertAlmostEqual(0.9, weights["fit"])
        self.assertAlmostEqual(ds.DEFAULT_WEIGHTS["cost"], weights["cost"])
        for bad in ('{"fit": -1}', '{"nope": 1}', "not json", "[1]"):
            with self.assertRaises(ds.ScoringError):
                ds.load_weights({"SESSION_KIT_DUTY_WEIGHTS": bad})

    def test_weights_are_normalised_so_a_score_stays_on_a_zero_to_one_scale(
        self,
    ) -> None:
        accounts = refs(("claude", "one"))
        snap = snapshot(
            reading("claude", "one", qs.WEEKLY, used=0.0),
            reading("claude", "one", qs.FIVE_HOUR, used=0.0),
        )

        result = ds.score_duty(
            ds.Duty("d1", "Design", "design"),
            accounts,
            snap,
            catalog=CATALOG,
            weights={"fit": 10.0, "weekly": 10.0, "window": 10.0, "cost": 10.0},
        )

        assert result.chosen is not None
        self.assertLessEqual(result.chosen.score, 1.0)
        self.assertAlmostEqual(
            1.0, sum(term.weight for term in result.chosen.terms), places=3
        )

    def test_the_environment_supplies_the_defaults_when_nothing_is_passed(self) -> None:
        with mock.patch.dict(os.environ, {"SESSION_KIT_DUTY_WEIGHTS": '{"cost": 0}'}):
            weights = ds.load_weights()

        self.assertEqual(0.0, weights["cost"])


if __name__ == "__main__":
    unittest.main()
