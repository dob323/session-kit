"""The example desktop notifier the watchdog can be pointed at.

The attention queue this file was named for is gone with the event store, and
with it the watchdog's opt-in queue alerts. What survives is the notifier
contract itself: the watchdog hands an alert to whatever
SESSION_KIT_WATCHDOG_NOTIFY names, and the shipped example has to keep parsing
flags it does not recognize so the contract can grow without breaking the
machine's own notifier. The watchdog's own alerting is covered in
tests/test_watchdog.py.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest

REPO = Path(__file__).resolve().parent.parent


class NotifierExampleTests(unittest.TestCase):
    def test_the_example_notifier_ignores_flags_it_does_not_know(self) -> None:
        notifier = REPO / "extras" / "notify-desktop"
        self.assertTrue(os.access(notifier, os.X_OK))
        # No notify-send in a container: the point is that an unknown flag is
        # parsed rather than refused, so the contract can grow.
        result = subprocess.run(
            ["bash", "-n", str(notifier)], capture_output=True, text=True
        )
        self.assertEqual(0, result.returncode, result.stderr)
        source = notifier.read_text(encoding="utf-8")
        self.assertIn("--title=*", source)
        self.assertIn("--body=*", source)
        self.assertIn("*) ;;", source)


if __name__ == "__main__":
    unittest.main()
