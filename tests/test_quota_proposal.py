"""The proposal surface: the numbers reach the operator, not just the verdict."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from lib.sessionkit_supervisor import duty_scoring as ds
from lib.sessionkit_supervisor import quota_proposal as qp
from lib.sessionkit_supervisor import quota_sources as qs


NOW = 1_800_000_000

CATALOG = (
    ds.ModelOption("claude", "deep", "vendor-deep-1", strength=4, cost=4),
    ds.ModelOption("claude", "mid", "vendor-mid-1", strength=2, cost=2),
    ds.ModelOption("codex", "other", "vendor-other-1", strength=3, cost=2),
)


class StubSource:
    """A source with fixed answers, so a proposal test reads no real state."""

    name = "stub"

    def __init__(self, readings: dict[str, list[dict]]):
        self.readings = readings

    def read(self, ref: qs.AccountRef, *, now: int) -> list[qs.QuotaReading]:
        return [
            qs.QuotaReading(
                provider=ref.provider,
                account=ref.alias,
                source=self.name,
                confidence=row.get("confidence", qs.MEASURED),
                observed_at_unix=now,
                window=row["window"],
                used_fraction=row.get("used"),
                resets_at_unix=row.get("resets"),
                load_units=row.get("load"),
                load_unit="tokens/h" if row.get("load") is not None else "",
                exhausted=row.get("exhausted", False),
            )
            for row in self.readings.get(ref.alias, [])
        ]


def accounts() -> list[qs.AccountRef]:
    return [
        qs.AccountRef(provider="claude", alias="one", email="one@invalid.example"),
        qs.AccountRef(provider="claude", alias="two", email="two@invalid.example"),
        qs.AccountRef(provider="codex", alias="three", email="three@invalid.example"),
    ]


def source() -> StubSource:
    return StubSource(
        {
            "one": [
                {"window": qs.WEEKLY, "used": 0.18},
                {"window": qs.FIVE_HOUR, "used": 0.91, "resets": NOW + 720},
            ],
            "two": [
                {"window": qs.WEEKLY, "used": 0.74},
                {"window": qs.FIVE_HOUR, "used": 0.09},
            ],
            "three": [
                {"window": qs.WEEKLY, "used": 0.04, "confidence": qs.MEASURED},
            ],
        }
    )


def proposal(*duties: ds.Duty) -> dict:
    return qp.build_proposal(
        {"state_dir": "/nonexistent-for-tests"},
        list(duties),
        refs=accounts(),
        sources=[source()],
        catalog=CATALOG,
        weights=ds.DEFAULT_WEIGHTS,
        now=NOW,
    )


class BuildTests(unittest.TestCase):
    def test_a_proposal_carries_its_inputs_alongside_its_answer(self) -> None:
        built = proposal(ds.Duty("d1", "Design the engine", "design"))

        self.assertEqual(qp.PROPOSAL_SCHEMA_VERSION, built["schema_version"])
        self.assertEqual(NOW, built["generated_at_unix"])
        self.assertEqual(["stub"], built["sources"])
        self.assertEqual(3, len(built["accounts"]))
        self.assertEqual(len(CATALOG), len(built["catalog"]))
        self.assertEqual(ds.DEFAULT_WEIGHTS, built["weights"])
        self.assertTrue(built["quota"]["readings"])

    def test_every_placement_is_kept_so_the_skipped_ones_can_be_shown(self) -> None:
        built = proposal(ds.Duty("d1", "Design the engine", "design"))

        considered = built["duties"][0]["considered"]
        placements = {
            (row["provider"], row["model"], row["account"]) for row in considered
        }
        # Two claude models across two claude accounts, one codex model on one
        # codex account: nothing silently dropped.
        self.assertEqual(5, len(placements))

    def test_a_serialised_proposal_round_trips_through_json(self) -> None:
        built = proposal(ds.Duty("d1", "Design the engine", "design"))

        restored = json.loads(json.dumps(built))

        self.assertEqual(qp.render_proposal(built), qp.render_proposal(restored))


class RenderTests(unittest.TestCase):
    def render(self, *duties: ds.Duty) -> str:
        return qp.render_proposal(proposal(*duties))

    def test_the_pick_shows_both_windows_and_the_arithmetic(self) -> None:
        text = self.render(ds.Duty("d1", "Design the engine", "design"))

        self.assertIn("Design the engine [design]", text)
        self.assertIn("→ claude/deep/one", text)
        self.assertIn("weekly 82% left (measured)", text)
        self.assertIn("fit 1.00×0.45=0.450", text)
        self.assertIn("window 0.35×0.2=0.070", text)

    def test_a_scoring_adjustment_never_wears_the_provider_s_name(self) -> None:
        """A floored window prints what was read AND what was scored."""
        text = self.render(ds.Duty("d1", "Design the engine", "design"))

        self.assertIn("5h 9% left (measured), scored as 35%", text)
        self.assertNotIn("5h 35% left (measured)", text)

    def test_the_runners_up_carry_their_own_numbers(self) -> None:
        text = self.render(ds.Duty("d1", "Design the engine", "design"))

        self.assertIn("considered:", text)
        self.assertIn("claude/deep/two", text)
        self.assertIn("weekly 26% left (measured)", text)
        self.assertIn("5h 91% left (measured)", text)

    def test_an_unread_window_prints_as_unknown_not_as_zero(self) -> None:
        text = self.render(ds.Duty("d1", "Review a diff", "review"))

        self.assertIn("codex/other/three", text)
        self.assertIn("5h unknown", text)

    def test_a_reset_time_is_shown_in_human_terms(self) -> None:
        text = self.render(ds.Duty("d1", "Design the engine", "design"))

        self.assertIn("5h resets in 12m", text)

    def test_a_skipped_account_prints_its_reason(self) -> None:
        built = qp.build_proposal(
            {"state_dir": "/nonexistent-for-tests"},
            [ds.Duty("d1", "Design the engine", "design")],
            refs=accounts(),
            sources=[
                StubSource(
                    {
                        "one": [{"window": qs.WEEKLY, "used": 0.2}],
                        "two": [
                            {
                                "window": qs.FIVE_HOUR,
                                "used": 1.0,
                                "exhausted": True,
                                "resets": NOW + 3600,
                            }
                        ],
                    }
                )
            ],
            catalog=CATALOG,
            weights=ds.DEFAULT_WEIGHTS,
            now=NOW,
        )

        text = qp.render_proposal(built)

        self.assertIn("claude/deep/two  skipped — five_hour window is exhausted", text)
        self.assertIn("(back in 1h00m)", text)
        self.assertIn("→ claude/deep/one", text)

    def test_a_source_failure_is_printed_rather_than_swallowed(self) -> None:
        class Broken:
            name = "broken"

            def read(self, ref: qs.AccountRef, *, now: int) -> list[qs.QuotaReading]:
                raise RuntimeError("permission denied")

        built = qp.build_proposal(
            {"state_dir": "/nonexistent-for-tests"},
            [ds.Duty("d1", "Docs", "docs")],
            refs=accounts()[:1],
            sources=[Broken()],
            catalog=CATALOG,
            weights=ds.DEFAULT_WEIGHTS,
            now=NOW,
        )

        text = qp.render_proposal(built)

        self.assertIn("quota source error", text)
        self.assertIn("permission denied", text)

    def test_no_eligible_placement_says_so_plainly(self) -> None:
        built = qp.build_proposal(
            {"state_dir": "/nonexistent-for-tests"},
            [ds.Duty("d1", "Docs", "docs")],
            refs=[qs.AccountRef(provider="claude", alias="off", enabled=False)],
            sources=[StubSource({})],
            catalog=CATALOG,
            weights=ds.DEFAULT_WEIGHTS,
            now=NOW,
        )

        text = qp.render_proposal(built)

        self.assertIn("no eligible placement", text)


class DutyParsingTests(unittest.TestCase):
    def test_both_command_line_spellings_parse(self) -> None:
        short = qp.parse_duty("design:Build the thing", 1)
        full = qp.parse_duty("api:mechanical:Rename a key", 2)

        self.assertEqual(
            ("d1", "design", "Build the thing"),
            (short.duty_id, short.work_type, short.title),
        )
        self.assertEqual(
            ("api", "mechanical", "Rename a key"),
            (full.duty_id, full.work_type, full.title),
        )

    def test_a_malformed_duty_is_refused(self) -> None:
        with self.assertRaises(ds.ScoringError):
            qp.parse_duty("no-colon-here", 1)
        with self.assertRaises(ds.ScoringError):
            qp.parse_duty("vibes:Do something", 1)


class CommandLineTests(unittest.TestCase):
    """The entry point, run against an empty home so it reads no real state."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-cli.")
        self.root = self.temporary.name
        self.environment = mock.patch.dict(
            os.environ,
            {
                "HOME": self.root,
                "XDG_DATA_HOME": self.root,
                "SESSION_KIT_ACCOUNT_REGISTRY": f"{self.root}/accounts.json",
                "SESSION_KIT_ACCOUNT_ROOT": f"{self.root}/profiles",
                "SESSION_KIT_ACCOUNT_ROSTER": f"{self.root}/roster.json",
                "SESSION_KIT_ROTATION_ADVICE": f"{self.root}/advice.json",
            },
            clear=False,
        )
        self.environment.start()
        for name in ("SESSION_KIT_MODEL_CATALOG", "SESSION_KIT_DUTY_WEIGHTS"):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qp.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_it_refuses_to_run_without_a_duty(self) -> None:
        code, _, err = self.run_main("--state-dir", "/nonexistent-for-tests")

        self.assertEqual(2, code)
        self.assertIn("at least one --duty", err)

    def test_a_bad_duty_exits_without_reading_any_state(self) -> None:
        code, _, err = self.run_main(
            "--duty", "vibes:whatever", "--state-dir", "/nonexistent-for-tests"
        )

        self.assertEqual(2, code)
        self.assertIn("unknown work type", err)

    def test_json_output_is_the_proposal_structure(self) -> None:
        code, out, _ = self.run_main(
            "--duty",
            "docs:Write the page",
            "--state-dir",
            "/nonexistent-for-tests",
            "--now",
            str(NOW),
            "--json",
        )

        self.assertEqual(0, code)
        payload = json.loads(out)
        self.assertEqual(qp.PROPOSAL_SCHEMA_VERSION, payload["schema_version"])
        self.assertEqual(NOW, payload["generated_at_unix"])
        self.assertEqual(1, len(payload["duties"]))


if __name__ == "__main__":
    unittest.main()
