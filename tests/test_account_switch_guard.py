"""The account-switch tree guard: what counts as a recognized child.

The guard refuses a switch while the managed shell's subtree holds a process
it cannot name, because the switch kills and relaunches that tree. The
allowlist is therefore a contract: everything a healthy provider session
legitimately runs must be on it, or switching becomes impossible in exactly
the sessions people use most. This file pins both directions.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.support import REPO

PICKER = REPO / "lib" / "sh" / "sp_picker.sh"


def run_guard(processes: list[dict], provider: str = "claude") -> int:
    """Invoke account_switch_safe_tree against a canned process table."""
    with tempfile.TemporaryDirectory(prefix=".switch-guard-", dir=REPO) as raw:
        base = Path(raw)
        table = {"processes": processes}
        core = base / "core.py"
        core.write_text(
            "import json,sys\n"
            "if sys.argv[1:3] == ['platform', 'process-table']:\n"
            f"    print(json.dumps({json.dumps(table)}))\n",
            encoding="utf-8",
        )
        script = f"""
set -u
SK_STATE_DIR={base}
INVENTORY_CORE={core}
PICKER_REFUSED_STATUS=74
sk_die() {{ printf '%s\\n' "$*" >&2; return 1; }}
source_region() {{
  # Only the guard function is needed; source the whole file with its
  # entry-point execution disabled by running under `return`-safe sourcing.
  :
}}
source {PICKER} 2>/dev/null || true
account_switch_safe_tree {provider} 100 1000 200 2000
"""
        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return completed.returncode


def tree(*extra_children: dict) -> list[dict]:
    rows = [
        {"pid": 100, "ppid": 1, "start_ticks": 1000, "comm": "bash", "cmdline": ["bash"]},
        {
            "pid": 200,
            "ppid": 100,
            "start_ticks": 2000,
            "comm": "claude",
            "cmdline": ["claude", "--resume", "x"],
        },
    ]
    rows.extend(extra_children)
    return rows


class SwitchGuardTests(unittest.TestCase):
    def test_a_bare_provider_tree_is_allowed(self) -> None:
        self.assertEqual(0, run_guard(tree()))

    def test_a_project_hook_child_is_the_provider_working(self) -> None:
        # Claude Code spawns .claude/hooks/ scripts itself; one running must
        # not make the session unswitchable.
        self.assertEqual(
            0,
            run_guard(
                tree(
                    {
                        "pid": 300,
                        "ppid": 200,
                        "start_ticks": 3000,
                        "comm": "python3",
                        "cmdline": [
                            "python3",
                            "/srv/example/.claude/hooks/coordination-rewake.py",
                        ],
                    }
                )
            ),
        )

    def test_an_unknown_child_still_refuses(self) -> None:
        self.assertNotEqual(
            0,
            run_guard(
                tree(
                    {
                        "pid": 300,
                        "ppid": 200,
                        "start_ticks": 3000,
                        "comm": "python3",
                        "cmdline": ["python3", "/home/someone/miner.py"],
                    }
                )
            ),
        )


if __name__ == "__main__":
    unittest.main()
