"""Every tracked file either ships in the public export or is listed private.

``public-files.txt`` decides what the export *ships*. Nothing decides what it
*omits*: ``tools/build-public-tree`` skips a tracked file that matches no
pattern and still exits 0, so both ways this goes wrong are silent. A file
meant for the public tree quietly never ships, or a file kept back on purpose
starts shipping the day someone widens a pattern.

The list below is the missing half of that decision. It names every tracked
file the export leaves behind and why, so a new file forces a visible choice
instead of defaulting to whichever outcome its path happens to produce.
"""

from __future__ import annotations

import fnmatch
import subprocess
import unittest

from tests.support import REPO


MANIFEST = REPO / "public-files.txt"

# tools/build-public-tree writes this into the tree it exports; it comes from
# the exporter, not from the manifest, so it has no pattern to match.
GENERATED_BY_THE_EXPORT = {"SOURCE.json"}

# These were deliberately removed from the public source. Broad directory
# patterns must not make a later accidental reintroduction silent.
REMOVED_FROM_THE_PUBLIC_TREE = {
    "docs/build-tracks-2026-08-12.md",
    "LICENSES/MIT-maniple.txt",
    "tools/reset-collection-order.py",
}

# Tracked files the public export deliberately leaves behind, each with the
# reason it is held back. Keep this list exact: an entry that starts matching a
# manifest pattern, or that stops being tracked, fails the staleness test.
PRIVATE_BY_DECISION = {
    "macos/com.session-kit.reaper.plist": (
        "reference copy of a file install.sh generates; shipping it invites an "
        "edit the installer would silently overwrite"
    ),
    "tests/test_picker_dashboard.py": (
        "machine-specific test fixture; never exported"
    ),
    "tests/test_production_checkout_unreachable.py": (
        "machine-specific deployment artifact; never exported. The same "
        "guarantees are tested generically in tests/test_worktree_isolation.py"
    ),
    "phase1-patches/claude_headless_job.sh.patch": (
        "machine-specific deployment artifact; never exported"
    ),
    "phase1-patches/scheduled_jobs_runner.sh.patch": (
        "machine-specific deployment artifact; never exported"
    ),
    "phase1-patches/watcher.py.patch": (
        "machine-specific deployment artifact; never exported"
    ),
    "tools/publish-release": (
        "maintainer-only release tooling; deliberately not shipped"
    ),
}

UNDECIDED = """{count} tracked file(s) are neither exported nor recorded as private:
{listing}
Decide each one:
  ships publicly -> add a matching pattern to public-files.txt
  stays private  -> add it to PRIVATE_BY_DECISION in
                    tests/test_manifest_coverage.py, with the reason
tools/build-public-tree skips an unmatched file without a word, so this test is
the only place the omission is visible."""


def manifest_patterns() -> list[str]:
    return [
        line.strip()
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def exported(path: str, patterns: list[str]) -> bool:
    """Match exactly as tools/build-public-tree.selected does."""
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


class ManifestCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        listing = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if listing.returncode != 0:
            # An unpacked release archive is not a checkout; there is no
            # tracked set to compare the manifest against.
            self.skipTest("not a Git checkout")
        self.tracked = [path for path in listing.stdout.split("\0") if path]
        self.patterns = manifest_patterns()
        # tools/build-public-tree writes SOURCE.json into the tree it exports.
        self.is_export = (REPO / "SOURCE.json").is_file()

    def test_every_tracked_file_is_exported_or_listed_private(self) -> None:
        undecided = sorted(
            path
            for path in self.tracked
            if not exported(path, self.patterns)
            and path not in PRIVATE_BY_DECISION
            and path not in GENERATED_BY_THE_EXPORT
        )
        self.assertEqual(
            [],
            undecided,
            UNDECIDED.format(
                count=len(undecided),
                listing="".join(f"  {path}\n" for path in undecided),
            ),
        )

    def test_private_list_names_only_files_the_export_still_omits(self) -> None:
        shipping = sorted(
            path for path in PRIVATE_BY_DECISION if exported(path, self.patterns)
        )
        self.assertEqual(
            [],
            shipping,
            f"public-files.txt now exports {shipping}; remove them from "
            "PRIVATE_BY_DECISION so the list keeps meaning what it says",
        )
        if self.is_export:
            # A public export tracks only the exported subset, so every private
            # entry is correctly absent there.
            return
        gone = sorted(
            path for path in PRIVATE_BY_DECISION if path not in set(self.tracked)
        )
        self.assertEqual(
            [],
            gone,
            f"PRIVATE_BY_DECISION names untracked paths {gone}; drop them",
        )

    def test_every_private_entry_records_why(self) -> None:
        unexplained = sorted(
            path for path, reason in PRIVATE_BY_DECISION.items() if not reason.strip()
        )
        self.assertEqual([], unexplained, "a held-back file needs its reason")

    def test_removed_files_do_not_return(self) -> None:
        returned = sorted(REMOVED_FROM_THE_PUBLIC_TREE.intersection(self.tracked))
        self.assertEqual(
            [],
            returned,
            f"removed public-tree paths returned: {returned}",
        )

    def test_manifest_and_this_module_agree_on_the_matching_rule(self) -> None:
        """The guard is worthless if it matches differently from the exporter."""
        source = (REPO / "tools" / "build-public-tree").read_text(encoding="utf-8")
        self.assertIn(
            "return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)",
            source,
            "tools/build-public-tree changed how it matches manifest patterns; "
            "update exported() in this module to match, or the guard checks "
            "something the exporter no longer does",
        )


if __name__ == "__main__":
    unittest.main()
