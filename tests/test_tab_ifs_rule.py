"""The tab-IFS review rule, enforced instead of remembered.

The rule, in one line: **`IFS=$'\t' read` on a record with a possibly-empty
field is the husk bug again.** Tab is IFS whitespace, so a run of tabs
collapses to one separator and every empty field between them disappears —
the fields after it shift left, and a record armed perfectly is read as
garbage. The kit lost three launches in one night to exactly that, and the
fix is the `\034` translation idiom in `bashrc/shpool.bashrc` (translate tabs
to a non-whitespace delimiter first, then split on that).

This test does not ban the read. It bans an *unreviewed* one: every site is
listed below with the reason it is safe, or listed as an open husk with the
repro that proves it is not. A new site fails until somebody writes down which
it is. `CONTRIBUTING.md` carries the rule in prose.
"""

from __future__ import annotations

import re
import subprocess
import unittest

from tests.support import REPO


# `IFS=$'\t' read`, the whole class, in one greppable pattern.
TAB_READ = re.compile(r"IFS=\$'\\t'\s+read\s+(?:-[A-Za-z]+\s+)*(?P<first>\w+)")

# Where shell lives. Extensionless helpers are included by name.
SHELL_GLOBS = (
    "*.sh",
    "*.bashrc",
    "bin/*",
    "bashrc/*",
    "tools/*",
    "deploy/*",
    "install.sh",
    "tests/run",
)

# Each site is keyed by (path, the first variable the read fills), which
# survives edits above it in the file the way a line number does not.
REVIEWED_SAFE = {
    ("bashrc/shpool.bashrc", "__sk_start_provider"): (
        "start record: only the provider is read for meaning and it is never "
        "empty (the writer validates claude|codex|shell); the rest of the line "
        "lands in one catch-all"
    ),
    ("bashrc/shpool.bashrc", "__sk_account_provider"): (
        "account record: two fields, both validated non-empty by the regexes "
        "immediately below the read"
    ),
    ("bin/session_kit_watchdog", "_"): (
        "automatic-move receipt: the writer always fills all seven fields, and "
        "the provider, conversation UUID and token are rejected if empty"
    ),
    ("bin/session_kit_watchdog", "provider"): (
        "pending-notice row: provider, conversation UUID and token are always "
        "non-empty; only the final sentence may be empty, so no later field shifts"
    ),
    ("lib/sh/sp_provider_bounce.sh", "refresh_pid"): (
        "two numeric fields from the platform helper, both checked against "
        "^[0-9]+$ after the read"
    ),
    ("lib/sh/sp_picker.sh", "signal_pid"): (
        "two numeric fields from a proof record, both validated after the read"
    ),
    ("lib/sh/session_kit_doctor.sh", "linger_status"): (
        "status and detail; only the trailing detail can be empty, and a "
        "trailing empty field survives the collapse unchanged"
    ),
    ("lib/sh/session_kit_doctor.sh", "shpool_status"): (
        "status and detail; only the trailing detail can be empty"
    ),
    ("lib/sh/session_kit_doctor.sh", "status"): (
        "check rows written by add_check, which always fills all three fields; "
        "the reader also rejects a row with an empty detail"
    ),
    ("lib/sh/session_kit_checks.sh", "shpool_status"): (
        "status and detail; only the trailing detail can be empty"
    ),
    ("lib/sh/session_kit_checks.sh", "linger_status"): (
        "status and detail; only the trailing detail can be empty"
    ),
    ("lib/sh/shpool_login_render.sh", "section"): (
        "help table generated in the same file; section and key are never "
        "empty and the loop skips a row without a key"
    ),
    ("lib/sh/shpool_login_actions.sh", "token"): (
        "repair index rows: token and index, both non-empty by construction "
        "and both validated in the loop"
    ),
    ("lib/sh/shpool_login_actions.sh", "number"): (
        "attention index rows: two numeric fields, both validated in the loop"
    ),
    ("tools/install-matrix", "distro"): (
        "matrix summary written three fields at a time by the same tool; no "
        "field can be empty"
    ),
}

# Sites that ARE the husk, with the evidence. Listed so the guardrail stays
# green while the owner fixes them, and so a fix cannot be forgotten: the
# staleness test below fails the day an entry stops matching.
OPEN_HUSKS: dict[tuple[str, str], str] = {}


def shell_files():
    listing = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", *SHELL_GLOBS],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    for name in sorted(set(listing)):
        path = REPO / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        yield name, text


def tab_reads():
    """Every `IFS=$'\t' read` in shell code, keyed by file and first variable."""
    found = {}
    for name, text in shell_files():
        for number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue  # the comment explaining the bug is not the bug
            match = TAB_READ.search(line)
            if match:
                found.setdefault((name, match.group("first")), number)
    return found


class TabIfsRuleTests(unittest.TestCase):
    def test_every_tab_read_is_reviewed(self) -> None:
        found = tab_reads()
        known = set(REVIEWED_SAFE) | set(OPEN_HUSKS)
        unreviewed = sorted(key for key in found if key not in known)
        self.assertEqual(
            [],
            unreviewed,
            "New `IFS=$'\\t' read` site(s):\n"
            + "\n".join(f"  {path}:{found[(path, var)]} ({var})" for path, var in unreviewed)
            + "\n\nTab is IFS whitespace: a run of tabs collapses and empty fields\n"
            "vanish. Either translate tabs to \\034 first (see the launch path in\n"
            "bashrc/shpool.bashrc), or add the site to REVIEWED_SAFE in\n"
            "tests/test_tab_ifs_rule.py with the reason no field can be empty.",
        )

    def test_the_review_list_is_not_stale(self) -> None:
        found = tab_reads()
        gone = sorted(key for key in set(REVIEWED_SAFE) | set(OPEN_HUSKS) if key not in found)
        self.assertEqual(
            [],
            gone,
            "Reviewed site(s) no longer present; delete the entry:\n"
            + "\n".join(f"  {path} ({var})" for path, var in gone),
        )

    def test_every_reviewed_site_carries_a_reason(self) -> None:
        for key, reason in {**REVIEWED_SAFE, **OPEN_HUSKS}.items():
            with self.subTest(site=key):
                self.assertGreater(len(reason.split()), 6, f"{key} needs a real reason")

    def test_the_launch_path_still_translates_before_it_splits(self) -> None:
        """The idiom the rule points at has to exist to be pointed at."""
        text = (REPO / "bashrc" / "shpool.bashrc").read_text(encoding="utf-8")
        self.assertIn("//$'\\t'/$'\\034'", text)
        self.assertIn("IFS=$'\\034' read -r __sk_launch_provider", text)
        self.assertIn("IFS=$'\\034' read -r __sk_provider", text)


if __name__ == "__main__":
    unittest.main()
