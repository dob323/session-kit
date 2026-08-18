from __future__ import annotations

from pathlib import Path
import re
import unittest


REPO = Path(__file__).resolve().parents[1]


def restore_settings(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    modes = re.findall(r"(?m)^session_restore_mode\s*=\s*(.+?)\s*$", text)
    if len(modes) != 1:
        raise ValueError(f"{path}: expected one session_restore_mode assignment")
    values: dict[str, object] = {"mode": modes[0]}
    for name, pattern in {
        "output_spool_lines": r"(?m)^output_spool_lines\s*=\s*([0-9]+)\s*$",
        "vt100_output_spool_width": (
            r"(?m)^vt100_output_spool_width\s*=\s*([0-9]+)\s*$"
        ),
    }.items():
        matches = re.findall(pattern, text)
        if len(matches) != 1:
            raise ValueError(f"{path}: expected one {name} assignment")
        values[name] = int(matches[0])
    return values


class ShpoolExampleConfigTests(unittest.TestCase):
    def test_simple_restore_contract(self) -> None:
        """Reattach must never replay spooled TUI frames.

        Replaying the vt100 spool interleaves cells from different frames of
        a cell-diffing TUI (Claude Code, Codex), the braided corruption seen
        live on 2026-08-12. "simple" emits nothing and lets the program
        redraw itself; clean reading lives in `sp history`. The spool bounds
        stay finite so daemon memory is bounded.
        """
        for relative in ("config/shpool.example.toml", "shpool/config.toml"):
            with self.subTest(relative=relative):
                config = restore_settings(REPO / relative)
                self.assertEqual('"simple"', config["mode"])
                self.assertEqual(1000, config["output_spool_lines"])
                self.assertEqual(200, config["vt100_output_spool_width"])


if __name__ == "__main__":
    unittest.main()
