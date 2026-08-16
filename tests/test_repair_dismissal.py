"""Dismissing a watchdog row acknowledges the row that was READ, or nothing.

The `a` screen hands out `d1`, `d2`, … and the dismissal used to resolve them
to bare array positions in watchdog-repairs.json, captured when the screen was
drawn. Retirement then began deleting records from the middle of that file on
roughly every 60-second sweep, while the modal read blocks with no timeout for
as long as a person reads the screen. So the numbers on screen stopped matching
the file: `d2` typed against a drawn list acknowledged whatever had shifted
into slot 2 -- another session's warning -- and printed "Dismissed 1 repair
failure." either way. The one they wanted gone stayed; one they never saw went.

Found in review, 2026-08-15. Before that there was no test for dismissal at
all, which is why a second concurrent writer could be added to a positionally
indexed file without anything objecting.

These drive the real shell functions, sourcing the same two files the picker
sources, so the identity check is exercised where it actually runs.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]


def record(shpool_id: str, title: str, *, age_days: float = 0.0) -> dict:
    return {
        "at_unix_ms": int((time.time() - age_days * 86400) * 1000),
        "old_shpool_id": shpool_id,
        "new_shpool_id": "",
        "title": title,
        "provider": "codex",
        "outcome": "reported",
        "reason": "no output for far longer than any normal pause",
        "acknowledged": False,
    }


class RepairDismissalTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.repairs = self.base / "watchdog-repairs.json"

    def write(self, *records: dict) -> None:
        self.repairs.write_text(
            json.dumps({"schema_version": 1, "repairs": list(records)}, indent=2),
            encoding="utf-8",
        )

    def acknowledged(self) -> list[str]:
        data = json.loads(self.repairs.read_text(encoding="utf-8"))
        return [
            item["title"] for item in data["repairs"] if item.get("acknowledged")
        ]

    def bash(self, script: str) -> str:
        body = (
            f"source {REPO}/lib/sh/shpool_login_render.sh\n"
            f"source {REPO}/lib/sh/shpool_login_actions.sh\n"
            f'REPAIR_FILE={self.repairs}\n'
            "REPAIR_INDEX=$(picker_repair_failure_rows | tail -n +2 | "
            "grep -E '^d[0-9]+\t')\n" + script
        )
        result = subprocess.run(
            ["bash", "-c", body],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.base)},
            timeout=120,
        )
        return result.stdout + result.stderr

    def test_dismissing_acknowledges_the_row_that_was_read(self) -> None:
        self.write(
            record("alpha", "Blueprint Audit"),
            record("bravo", "Config Sweep"),
        )
        output = self.bash("dismiss_repair_failure 1")
        self.assertIn("Dismissed 1 repair failure.", output)
        self.assertEqual(["Blueprint Audit"], self.acknowledged())

    def test_a_list_that_moved_dismisses_nothing_and_says_so(self) -> None:
        """The blocker: retirement shifts every index after the deletion.

        The screen was drawn with Blueprint Audit at d2. A sweep then retires
        the record above it, so slot 1 now holds Config Sweep. Typing d2 must
        acknowledge NEITHER -- the person was reading a list that no longer
        exists, and the honest answer is to say so and redraw.
        """
        self.write(
            record("ghost", "Ghost A", age_days=9),
            record("alpha", "Blueprint Audit"),
            record("bravo", "Config Sweep"),
        )
        output = self.bash(
            # Render first, THEN mutate: exactly the order the race takes.
            "python3 - <<'EDIT'\n"
            "import json, pathlib\n"
            f"path = pathlib.Path('{self.repairs}')\n"
            "data = json.loads(path.read_text())\n"
            "data['repairs'] = data['repairs'][1:]\n"
            "path.write_text(json.dumps(data))\n"
            "EDIT\n"
            "dismiss_repair_failure 2"
        )
        self.assertIn("That list changed while it was on screen", output)
        self.assertNotIn("Dismissed 1 repair failure.", output)
        # The important half: nothing else was acknowledged in its place.
        self.assertEqual([], self.acknowledged())

    def test_a_row_that_vanished_entirely_dismisses_nothing(self) -> None:
        self.write(record("alpha", "Blueprint Audit"), record("bravo", "Config Sweep"))
        output = self.bash(
            "python3 - <<'EDIT'\n"
            "import json, pathlib\n"
            f"path = pathlib.Path('{self.repairs}')\n"
            "data = json.loads(path.read_text())\n"
            "data['repairs'] = [data['repairs'][0]]\n"
            "path.write_text(json.dumps(data))\n"
            "EDIT\n"
            "dismiss_repair_failure 2"
        )
        self.assertNotIn("Dismissed 1 repair failure.", output)
        self.assertEqual([], self.acknowledged())

    def test_a_number_that_was_never_on_the_screen_is_refused(self) -> None:
        self.write(record("alpha", "Blueprint Audit"))
        output = self.bash("dismiss_repair_failure 7")
        self.assertIn("There is no repair 7 on this screen", output)
        self.assertEqual([], self.acknowledged())


if __name__ == "__main__":
    unittest.main()
