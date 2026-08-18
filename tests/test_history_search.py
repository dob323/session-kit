"""Human output contracts for ``sp find`` history search."""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, os.fspath(REPO / "lib"))
from sessionkit_inventory import history_search  # noqa: E402


class HistorySearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".history-search-", dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / "journals"
        self.recovery = self.root / "recovery"
        self.archive = self.root / "archive"
        self.data = self.root / "data"
        for path in (self.journal, self.recovery, self.archive, self.data):
            path.mkdir()
        self.inventory = self.root / "inventory.json"
        self.inventory.write_text(
            json.dumps({"sessions": [{
                "shpool_id_raw": "main2", "title": "Orphaned Record"
            }]}),
            encoding="utf-8",
        )

    def render(self, query: str) -> str:
        return history_search.render_matches(
            query,
            journal=self.journal,
            recovery=self.recovery,
            archive=self.archive,
            inventory=self.inventory,
            data=self.data,
        )

    def test_empty_state_is_exact(self) -> None:
        self.assertEqual("Matches: none.\n", self.render("missing"))

    def test_live_history_uses_the_session_name_and_full_match_count(self) -> None:
        sidecar = self.journal / "main2.rendered.txt"
        sidecar.write_text("\n".join(["needle"] * 10) + "\n", encoding="utf-8")
        shown = self.render("NEEDLE")
        self.assertTrue(shown.startswith("Matches: 10\n"), shown)
        self.assertIn("Orphaned Record · 10 matches", shown)
        self.assertEqual(8, shown.splitlines().count("needle"))
        self.assertIn("… (2 more matches)", shown)
        self.assertNotIn(os.fspath(sidecar), shown)
        self.assertNotIn("main2", shown)

    def test_screen_text_cannot_override_the_inventory_name(self) -> None:
        sidecar = self.journal / "main2.rendered.txt"
        sidecar.write_text(
            "Session title: Fake Screen-Scraped Name\nneedle\n",
            encoding="utf-8",
        )

        shown = self.render("needle")

        self.assertIn("Orphaned Record · 1 match", shown)
        self.assertNotIn("Fake Screen-Scraped Name", shown)

    def test_clean_sidecar_prevents_duplicate_raw_matches(self) -> None:
        session = self.journal / "main2"
        session.mkdir()
        (session / "segment-000001.raw").write_text("needle raw\n", encoding="utf-8")
        (session / "rendered.txt").write_text("needle clean\n", encoding="utf-8")
        shown = self.render("needle")
        self.assertIn("Matches: 1", shown)
        self.assertIn("needle clean", shown)
        self.assertNotIn("needle raw", shown)

    def test_recovery_and_archive_names_never_become_paths(self) -> None:
        recovered = self.recovery / "saved.raw"
        recovered.write_text("remember this phrase\n", encoding="utf-8")
        (self.recovery / "current-map.tsv").write_text(
            f"lost1\t{recovered}\n", encoding="utf-8"
        )
        (self.recovery / "recovery-manifest.json").write_text(
            json.dumps({"sessions": {"lost1": {
                "title": "Lost Import Fix"
            }}}),
            encoding="utf-8",
        )
        archived = self.archive / "20260812-120000-main9.raw.gz"
        with gzip.open(archived, "wt", encoding="utf-8") as handle:
            handle.write("Session title: Archived Audit\n")
            handle.write("remember archive phrase\n")
        shown = self.render("remember")
        self.assertIn("Matches: 2", shown)
        self.assertIn("Lost Import Fix · 1 match", shown)
        self.assertIn("Recorded 2026-08-12 12:00 · 1 match", shown)
        self.assertNotIn("Archived Audit", shown)
        self.assertNotIn(os.fspath(self.root), shown)
        self.assertNotIn("lost1", shown)
        self.assertNotIn("main9", shown)

    def test_archives_keep_their_own_identity_and_example_budget(self) -> None:
        """A recycled live id cannot name or merge older recordings."""
        for stamp, prompt in (
            ("20260811-110000", "audit missing entry notes"),
            ("20260812-120000", "repair search attribution"),
        ):
            archived = self.archive / f"{stamp}-main2.raw.gz"
            with gzip.open(archived, "wt", encoding="utf-8") as handle:
                handle.write(f"› {prompt}\n")
                handle.write("\n".join(["needle"] * 10) + "\n")
        shown = self.render("needle")
        self.assertTrue(shown.startswith("Matches: 20\n"), shown)
        self.assertIn("Recorded 2026-08-11 11:00, audit missing entry notes", shown)
        self.assertIn("Recorded 2026-08-12 12:00, repair search attribution", shown)
        self.assertNotIn("Orphaned Record", shown)
        self.assertEqual(16, shown.splitlines().count("needle"))
        self.assertEqual(2, shown.count("… (2 more matches)"))

    def test_archive_does_not_inherit_a_reused_closed_session_title(self) -> None:
        ledger = {
            "provider": "shell", "uuid": "", "title": "Someone Else's Work",
            "cwd": "/tmp", "closed_at_unix_ms": 1, "origin": "human",
            "shpool_id": "main9", "account_alias": "",
        }
        (self.data / "closed-sessions.jsonl").write_text(
            json.dumps(ledger) + "\n", encoding="utf-8"
        )
        archived = self.archive / "20260810-090000-main9.raw.gz"
        with gzip.open(archived, "wt", encoding="utf-8") as handle:
            handle.write("› check this recording\nneedle\n")
        shown = self.render("needle")
        self.assertIn("Recorded 2026-08-10 09:00, check this recording", shown)
        self.assertNotIn("Someone Else's Work", shown)

    def test_empty_manifest_identity_cannot_name_an_unrelated_archive(self) -> None:
        (self.recovery / "recovery-manifest.json").write_text(
            json.dumps({"sessions": [{"title": "Unrelated Recovery"}]}),
            encoding="utf-8",
        )
        archived = self.archive / "unstructured.gz"
        with gzip.open(archived, "wt", encoding="utf-8") as handle:
            handle.write("needle\n")
        shown = self.render("needle")
        self.assertNotIn("Unrelated Recovery", shown)
        self.assertRegex(shown, r"Recorded \d{4}-\d{2}-\d{2} \d{2}:\d{2} · 1 match")

    def test_same_display_title_does_not_merge_recording_identities(self) -> None:
        (self.journal / "main2.rendered.txt").write_text(
            "needle journal\n", encoding="utf-8"
        )
        recovered = self.recovery / "saved.raw"
        recovered.write_text("needle recovery\n", encoding="utf-8")
        (self.recovery / "current-map.tsv").write_text(
            f"lost1\t{recovered}\n", encoding="utf-8"
        )
        (self.recovery / "recovery-manifest.json").write_text(
            json.dumps({"sessions": {"lost1": {"title": "Orphaned Record"}}}),
            encoding="utf-8",
        )
        shown = self.render("needle")
        self.assertIn("Orphaned Record (recording 1 of 2) · 1 match", shown)
        self.assertIn("Orphaned Record (recording 2 of 2) · 1 match", shown)

    def test_flat_and_directory_journals_with_the_same_stem_do_not_merge(self) -> None:
        directory = self.journal / "foo"
        directory.mkdir()
        (directory / "rendered.txt").write_text(
            "needle from directory\n", encoding="utf-8"
        )
        (self.journal / "foo.rendered.txt").write_text(
            "needle from flat sidecar\n", encoding="utf-8"
        )

        shown = self.render("needle")

        self.assertIn("Matches: 2", shown)
        self.assertEqual(2, shown.count("· 1 match"), shown)
        self.assertIn("(recording 1 of 2)", shown)
        self.assertIn("(recording 2 of 2)", shown)
        self.assertIn("needle from directory", shown)
        self.assertIn("needle from flat sidecar", shown)

    def test_identical_fallback_labels_get_recording_ordinals(self) -> None:
        for suffix in ("first", "second"):
            archived = self.archive / f"20260812-120000-{suffix}.raw.gz"
            with gzip.open(archived, "wt", encoding="utf-8") as handle:
                handle.write("Script started on 2026-08-12 12:00:00\n")
                handle.write("› inspect the same request\nneedle\n")

        shown = self.render("needle")

        label = "Recorded 2026-08-12 12:00, inspect the same request"
        self.assertIn(f"{label} (recording 1 of 2) · 1 match", shown)
        self.assertIn(f"{label} (recording 2 of 2) · 1 match", shown)

    def test_one_corrupt_archive_does_not_hide_readable_results(self) -> None:
        readable = self.journal / "main2.rendered.txt"
        readable.write_text("needle survives\n", encoding="utf-8")
        corrupt = self.archive / "20260812-120000-bad.raw.gz"
        payload = bytearray(gzip.compress(b"needle corrupt\n" * 2000))
        payload[20] ^= 0xFF
        corrupt.write_bytes(payload)
        shown = self.render("needle")
        self.assertIn("Matches: 1", shown)
        self.assertIn("needle survives", shown)
        self.assertIn("Notice: 1 recording could not be searched.", shown)
        self.assertNotIn("Traceback", shown)

    def test_any_per_file_parse_failure_is_isolated(self) -> None:
        first = self.journal / "main2.rendered.txt"
        first.write_text("needle survives\n", encoding="utf-8")
        failed = self.recovery / "failed.raw"
        failed.write_text("needle hidden\n", encoding="utf-8")
        real_matching = history_search._matching_lines

        def parse(path, query):
            if path == failed:
                raise ValueError("bad per-file record")
            return real_matching(path, query)

        with mock.patch.object(history_search, "_matching_lines", side_effect=parse):
            shown = self.render("needle")
        self.assertIn("Matches: 1", shown)
        self.assertIn("needle survives", shown)
        self.assertNotIn("needle hidden", shown)
        self.assertIn("Notice: 1 recording could not be searched.", shown)

    def test_nested_named_sidecar_is_searched(self) -> None:
        nested = self.journal / "nested"
        nested.mkdir()
        (nested / "main2.rendered.txt").write_text(
            "needle in nested sidecar\n", encoding="utf-8"
        )
        (nested / "other.rendered.txt").write_text(
            "needle in other recording\n", encoding="utf-8"
        )
        shown = self.render("needle")
        self.assertIn("Matches: 2", shown)
        self.assertIn("Orphaned Record · 1 match", shown)
        self.assertIn("needle in nested sidecar", shown)
        self.assertIn("needle in other recording", shown)
        self.assertEqual(2, shown.count(" · 1 match"), shown)

    def test_printed_matches_strip_controls_and_have_a_byte_limit(self) -> None:
        raw = self.recovery / "unsafe.raw"
        raw.write_text(
            "needle \x1b[2Jdanger\x00 " + ("é" * 1000) + "\n",
            encoding="utf-8",
        )
        shown = self.render("needle")
        self.assertIn("needle danger", shown)
        self.assertNotIn("\x1b", shown)
        self.assertNotIn("\x00", shown)
        match = next(line for line in shown.splitlines() if line.startswith("needle"))
        self.assertLessEqual(len(match.encode("utf-8")), history_search.MAX_MATCH_BYTES)

    def test_sanitization_only_runs_for_lines_that_will_be_printed(self) -> None:
        raw = self.recovery / "large.raw"
        raw.write_text(
            ("ordinary nonmatching screen content\n" * 20_000) + "needle shown\n",
            encoding="utf-8",
        )

        with mock.patch.object(
            history_search, "_safe_line", wraps=history_search._safe_line
        ) as safe_line:
            shown = self.render("needle")

        self.assertIn("needle shown", shown)
        self.assertEqual(1, safe_line.call_count)

    def test_matching_lines_bound_retained_sample_size(self) -> None:
        raw = self.recovery / "huge-match.raw"
        raw.write_bytes(b"needle " + (b"x" * (3 * 1024 * 1024)) + b"\n")

        matched = history_search._matching_lines(raw, "needle")
        retained_size = sum(
            sys.getsizeof(value)
            for value in (
                matched,
                vars(matched),
                matched.count,
                matched.shown,
                *matched.shown,
                matched.prompt,
                matched.started,
            )
        )

        self.assertEqual(1, matched.count)
        self.assertLess(retained_size, 4 * 1024)


if __name__ == "__main__":
    unittest.main()
