"""The machine snapshot is a contract: additive only, and one `needs you`.

The login picker parses this document live. A renamed or dropped field blinds
the screen a person is looking at, so these tests hold the existing shape exactly
and allow only additions. They also hold the fold that makes the contract worth
having: a session the fleet flagged as stalled comes back `needs_you`, with its
reasons, so no screen has to do that arithmetic itself and no two screens can
disagree about what needs a person.

`lib/sessionkit_inventory/SNAPSHOT.md` is the written form of the same contract;
one test reads it, so the document cannot fall behind the code.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.support import REPO


sys.path.insert(0, os.fspath(REPO / "lib"))

CORE_PATH = REPO / "lib" / "session_inventory.py"
CORE_SPEC = importlib.util.spec_from_file_location("session_inventory", CORE_PATH)
assert CORE_SPEC is not None and CORE_SPEC.loader is not None
inventory_core = importlib.util.module_from_spec(CORE_SPEC)
sys.modules[CORE_SPEC.name] = inventory_core
CORE_SPEC.loader.exec_module(inventory_core)

from sessionkit_inventory import labels  # noqa: E402
from sessionkit_inventory import render  # noqa: E402


SNAPSHOT_DOC = REPO / "lib" / "sessionkit_inventory" / "SNAPSHOT.md"
NOW_MS = 1_800_000_000_000

# The fields the login picker reads out of this document today. It is bash and
# Python-in-a-heredoc reading a JSON file; nothing type-checks it at the seam,
# so the list lives here and a removal fails loudly instead of silently.
PICKER_FIELDS = (
    "terminal_number",
    "row",
    "shpool_id",
    "shpool_id_raw",
    "display_shpool_id",
    "availability",
    "provider",
    "display_provider",
    "identity",
    "title",
    "display_title",
    "native_title",
    "cwd",
    "account_alias",
    "agent_status",
    "needs_you",
    "setup_incomplete",
    "started_at_unix_ms",
    "process_age_seconds",
    "recent_output_at_unix_ms",
    "recent_output_age_seconds",
    "subagents",
    "active_subagent_count",
    "recovery",
    "diagnostics",
)

PUBLISHED_FIELDS = (
    "account_alias",
    "age_seconds",
    "attached",
    "display_model",
    "launch_model",
    "model",
    "model_source",
    "model_state",
    "needs_you",
    "needs_you_reasons",
    "number",
    "origin",
    "state",
    "subagent_count",
)


def collected_row(
    shpool_id: str = "s20260812-101010-1",
    *,
    number: int | None = 3,
    uuid: str = "11111111-2222-4333-8444-555555555555",
    **extra: object,
) -> dict:
    """A row shaped the way the collectors write one."""
    item = {
        "row": 1,
        "terminal_number": number,
        "shpool_id": shpool_id,
        "shpool_id_raw": shpool_id,
        "display_shpool_id": shpool_id,
        "mutation_allowed": True,
        "mutation_rejection_reason": None,
        "shpool_status": "Disconnected",
        "availability": "ready",
        "provider": "claude",
        "display_provider": "claude",
        "setup_incomplete": False,
        "identity": {
            "uuid": uuid,
            "pid": 4242,
            "process_start_ticks": 99,
            "provenance": "transcript",
            "confidence": "exact",
        },
        "title": "Nightly index rebuild",
        "title_source": "automatic",
        "display_title": "Nightly index rebuild",
        "native_title": "Nightly index rebuild",
        "cwd": "/srv/project",
        "started_at_unix_ms": NOW_MS - 7_200_000,
        "process_age_seconds": 600,
        "recent_output_at_unix_ms": NOW_MS - 30_000,
        "recent_output_age_seconds": 30,
        "agent_status": "running",
        "needs_you": False,
        "subagents": [{"status": "running"}, {"status": "idle"}],
        "active_subagent_count": 1,
        "recovery": {"available": True, "provider": "claude", "uuid": uuid},
        "diagnostics": [],
        "shpool_shell": {"pid": 41, "process_start_ticks": 9},
        "account_alias": "work",
        "account_email": "person@example.invalid",
        "account_plan": "Max",
        "display_color": "cyan",
    }
    item.update(extra)
    return item


def document(*rows: dict) -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-08-12T00:00:00Z",
        "source": "live",
        "stale": False,
        "warnings": [],
        "daemon_generation": {"boot_id": "b", "pid": 7, "process_start_ticks": 70},
        "sessions": list(rows),
        "outside_agents": [],
    }


class FleetStallsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fleet = Path(tempfile.mkdtemp(prefix="session-kit-fleet.", dir=REPO.parent))
        self.addCleanup(self._remove)
        patcher = mock.patch.dict(
            os.environ, {"SESSION_KIT_FLEET_DIR": os.fspath(self.fleet)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _remove(self) -> None:
        for path in sorted(self.fleet.rglob("*"), reverse=True):
            path.unlink()
        self.fleet.rmdir()

    def write_stalls(self, payload: object) -> None:
        (self.fleet / "stalls.json").write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )

    def test_no_file_is_no_flags(self) -> None:
        self.assertEqual({}, inventory_core.read_fleet_stalls())

    def test_the_three_degrees_of_needs_you_are_returned(self) -> None:
        now = time.time()
        self.write_stalls(
            {
                "generated_at": int(now),
                "stalled": [
                    {"key": "a", "reason": "unsurfaced", "since": now - 100},
                    {"key": "b", "reason": "unanswered", "since": now - 100},
                    {"key": "c", "reason": "silent", "since": now - 100},
                ],
            }
        )
        self.assertEqual(
            {"a": ["unsurfaced"], "b": ["unanswered"], "c": ["silent"]},
            inventory_core.read_fleet_stalls(now=now),
        )

    def test_an_orphan_is_a_dead_session_not_a_persons_turn(self) -> None:
        now = time.time()
        self.write_stalls(
            {
                "generated_at": int(now),
                "stalled": [{"key": "a", "reason": "orphan", "since": now}],
            }
        )
        self.assertEqual({}, inventory_core.read_fleet_stalls(now=now))

    def test_a_flagger_that_stopped_running_is_no_evidence(self) -> None:
        now = time.time()
        self.write_stalls(
            {
                "generated_at": int(now) - inventory_core.FLEET_STALLS_FRESH_SECONDS,
                "stalled": [{"key": "a", "reason": "silent", "since": now}],
            }
        )
        self.assertEqual({}, inventory_core.read_fleet_stalls(now=now))

    def test_a_corrupt_or_oversized_file_hides_nothing_else(self) -> None:
        self.write_stalls("{not json")
        self.assertEqual({}, inventory_core.read_fleet_stalls())
        self.write_stalls([1, 2, 3])
        self.assertEqual({}, inventory_core.read_fleet_stalls())
        now = time.time()
        self.write_stalls(
            {
                "generated_at": int(now),
                "stalled": [{"key": "a", "reason": "silent"}]
                + [{"padding": "x" * 64}] * 20_000,
            }
        )
        self.assertGreater(
            (self.fleet / "stalls.json").stat().st_size,
            inventory_core.FLEET_STALLS_MAX_BYTES,
        )
        self.assertEqual({}, inventory_core.read_fleet_stalls(now=now))

    def test_only_the_first_records_are_read(self) -> None:
        now = time.time()
        self.write_stalls(
            {
                "generated_at": int(now),
                "stalled": [
                    {"key": f"k{index}", "reason": "silent"}
                    for index in range(inventory_core.FLEET_STALLS_MAX_RECORDS + 5)
                ],
            }
        )
        flags = inventory_core.read_fleet_stalls(now=now)
        self.assertEqual(inventory_core.FLEET_STALLS_MAX_RECORDS, len(flags))

    def test_a_flagged_session_needs_you_with_its_reasons(self) -> None:
        now = time.time()
        uuid = "11111111-2222-4333-8444-555555555555"
        self.write_stalls(
            {
                "generated_at": int(now),
                "stalled": [{"key": uuid, "reason": "unsurfaced", "since": now}],
            }
        )
        published = inventory_core.publish_view_fields(
            document(collected_row(uuid=uuid)), now_ms=NOW_MS
        )
        item = published["sessions"][0]
        self.assertIs(True, item["needs_you"])
        self.assertEqual(["unsurfaced"], item["needs_you_reasons"])
        self.assertEqual(labels.NEEDS_YOU, item["state"])

    def test_a_flag_finds_a_session_by_any_identifier_the_flagger_could_use(
        self,
    ) -> None:
        for key in (
            "11111111-2222-4333-8444-555555555555",
            "s20260812-101010-1",
            "Nightly index rebuild",
        ):
            with self.subTest(key=key):
                published = inventory_core.publish_view_fields(
                    document(collected_row()),
                    stalls={key: ["silent"]},
                    now_ms=NOW_MS,
                )
                self.assertIs(True, published["sessions"][0]["needs_you"])

    def test_a_flag_for_another_machine_touches_nothing(self) -> None:
        published = inventory_core.publish_view_fields(
            document(collected_row()),
            stalls={"someone-elses-uuid": ["silent"]},
            now_ms=NOW_MS,
        )
        item = published["sessions"][0]
        self.assertIs(False, item["needs_you"])
        self.assertEqual([], item["needs_you_reasons"])


class PublishedViewTests(unittest.TestCase):
    def published(self, *rows: dict, **kwargs: object) -> dict:
        return inventory_core.publish_view_fields(
            document(*rows), stalls={}, now_ms=NOW_MS, **kwargs
        )["sessions"][0]

    def test_nothing_the_collectors_wrote_is_renamed_or_dropped(self) -> None:
        original = collected_row()
        before = json.loads(json.dumps(original))
        item = self.published(original)
        for field, value in before.items():
            with self.subTest(field=field):
                self.assertIn(field, item)
                self.assertEqual(value, item[field])

    def test_every_field_the_picker_reads_survives(self) -> None:
        item = self.published(collected_row())
        for field in PICKER_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, item)

    def test_the_published_fields_are_all_present(self) -> None:
        item = self.published(collected_row())
        for field in PUBLISHED_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, item)
        self.assertEqual(3, item["number"])
        self.assertIs(False, item["attached"])
        self.assertEqual(labels.WORKING, item["state"])
        self.assertEqual(7200, item["age_seconds"])
        self.assertEqual(1, item["subagent_count"])
        self.assertEqual("work", item["account_alias"])
        self.assertIsNone(item["model"])
        self.assertIsNone(item["origin"])
        self.assertEqual([], item["needs_you_reasons"])

    def test_an_unnumbered_session_publishes_no_number(self) -> None:
        item = self.published(collected_row(number=None))
        self.assertIsNone(item["number"])
        item = self.published(collected_row(number=0))
        self.assertIsNone(item["number"])
        item = self.published(collected_row(number=True))
        self.assertIsNone(item["number"])

    def test_an_attached_session_is_open_elsewhere(self) -> None:
        item = self.published(collected_row(availability="attached"))
        self.assertIs(True, item["attached"])

    def test_model_and_origin_pass_through_when_a_collector_records_them(self) -> None:
        item = self.published(collected_row(model="opus-5", origin="machine"))
        self.assertEqual("opus-5", item["model"])
        self.assertEqual("machine", item["origin"])
        blank = self.published(collected_row(model="   ", origin=""))
        self.assertIsNone(blank["model"])
        self.assertIsNone(blank["origin"])

    def test_a_missing_account_alias_is_null_not_a_word(self) -> None:
        item = self.published(collected_row(account_alias=""))
        self.assertIsNone(item["account_alias"])

    def test_state_is_the_one_state_word(self) -> None:
        cases = (
            ({"needs_you": True}, labels.NEEDS_YOU),
            ({"setup_incomplete": True}, labels.SETUP_INCOMPLETE),
            ({"agent_status": "running"}, labels.WORKING),
            ({"agent_status": "reply optional"}, labels.WORKING),
            ({"agent_status": "needs your reply"}, labels.NEEDS_YOU),
            ({"agent_status": "state unavailable"}, labels.STATUS_UNAVAILABLE),
            (
                {
                    "agent_status": "running",
                    "recent_output_age_seconds": render.stall_threshold_seconds(),
                },
                labels.WAITING_ON_YOU,
            ),
            ({"agent_status": "idle"}, labels.WAITING_ON_YOU),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(expected, self.published(collected_row(**overrides))["state"])

    def test_age_is_null_when_the_open_time_is_unknown(self) -> None:
        self.assertIsNone(self.published(collected_row(started_at_unix_ms=None))["age_seconds"])
        self.assertIsNone(self.published(collected_row(started_at_unix_ms=0))["age_seconds"])

    def test_publishing_twice_changes_nothing(self) -> None:
        first = inventory_core.publish_view_fields(
            document(collected_row()), stalls={}, now_ms=NOW_MS
        )
        again = inventory_core.publish_view_fields(
            json.loads(json.dumps(first)), stalls={}, now_ms=NOW_MS
        )
        self.assertEqual(first, again)

    def test_outside_agents_are_left_alone(self) -> None:
        doc = document(collected_row())
        doc["outside_agents"] = [{"provider": "claude", "title": "outside"}]
        published = inventory_core.publish_view_fields(doc, stalls={}, now_ms=NOW_MS)
        self.assertEqual([{"provider": "claude", "title": "outside"}],
                         published["outside_agents"])

    def test_a_malformed_document_is_returned_unharmed(self) -> None:
        self.assertEqual({}, inventory_core.publish_view_fields({}, stalls={}))
        broken = {"sessions": ["not a row", None, 7]}
        self.assertEqual(broken, inventory_core.publish_view_fields(broken, stalls={}))

    def test_publishing_does_not_change_what_sp_list_prints(self) -> None:
        """The human list is unchanged by the machine fields beside it."""
        os.environ["COLUMNS"] = "200"
        try:
            before = render.render_inventory(
                document(collected_row()), False, color_enabled=lambda: False
            )
            after = render.render_inventory(
                inventory_core.publish_view_fields(
                    document(collected_row()), stalls={}, now_ms=NOW_MS
                ),
                False,
                color_enabled=lambda: False,
            )
        finally:
            os.environ.pop("COLUMNS", None)
        self.assertEqual(before, after)


class ServingBoundaryTests(unittest.TestCase):
    """Every document that leaves for a screen is published, not just the ones
    a caller remembered to publish."""

    def test_the_command_line_publishes_what_it_hands_out(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".snapshot-input-", dir=REPO) as raw:
            source = Path(raw) / "inventory.json"
            source.write_text(json.dumps(document(collected_row())), encoding="utf-8")
            fleet = Path(raw) / "fleet"
            fleet.mkdir()
            environment = dict(os.environ)
            environment["SESSION_KIT_FLEET_DIR"] = os.fspath(fleet)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, os.fspath(CORE_PATH), "lookup", "--input",
                 os.fspath(source), "3"],
                capture_output=True,
                text=True,
                env=environment,
                cwd=REPO,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        item = json.loads(result.stdout)
        for field in PUBLISHED_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, item)
        self.assertEqual(labels.WORKING, item["state"])
        self.assertEqual(3, item["number"])


class SnapshotDocumentTests(unittest.TestCase):
    def test_the_document_describes_every_published_field(self) -> None:
        text = SNAPSHOT_DOC.read_text(encoding="utf-8")
        section = text.split("### Published view")[1].split("`project` is")[0]
        documented = set(re.findall(r"^\| `([a-z_]+)`", section, re.M))
        self.assertEqual(set(PUBLISHED_FIELDS), documented)

    def test_the_document_names_the_additive_rule_and_the_stall_reasons(self) -> None:
        text = SNAPSHOT_DOC.read_text(encoding="utf-8")
        self.assertIn("Additive only", text)
        for reason in labels.STALL_REASONS_NEEDS_YOU:
            self.assertIn(reason, text)
        self.assertIn("SESSION_KIT_FLEET_DIR", text)

    def test_the_document_prints_no_identifier_of_its_own(self) -> None:
        text = SNAPSHOT_DOC.read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", text),
            "SNAPSHOT.md must not carry a real conversation identifier",
        )


if __name__ == "__main__":
    unittest.main()
