from __future__ import annotations

from pathlib import Path
import re
import unittest


REPO = Path(__file__).resolve().parents[1]


def bounded_restore_settings(path: Path) -> dict[str, int]:
    text = path.read_text(encoding="utf-8")
    patterns = {
        "restore_lines": (
            r"(?m)^session_restore_mode\s*=\s*"
            r"\{\s*lines\s*=\s*([0-9]+)\s*\}\s*$"
        ),
        "output_spool_lines": r"(?m)^output_spool_lines\s*=\s*([0-9]+)\s*$",
        "vt100_output_spool_width": (
            r"(?m)^vt100_output_spool_width\s*=\s*([0-9]+)\s*$"
        ),
    }
    values: dict[str, int] = {}
    for name, pattern in patterns.items():
        matches = re.findall(pattern, text)
        if len(matches) != 1:
            raise ValueError(f"{path}: expected one {name} assignment")
        values[name] = int(matches[0])
    return values


class ShpoolExampleConfigTests(unittest.TestCase):
    def test_bounded_restore_contract(self) -> None:
        for relative in ("config/shpool.example.toml", "shpool/config.toml"):
            with self.subTest(relative=relative):
                config = bounded_restore_settings(REPO / relative)
                self.assertEqual(500, config["restore_lines"])
                self.assertEqual(1000, config["output_spool_lines"])
                self.assertEqual(200, config["vt100_output_spool_width"])
                self.assertLessEqual(
                    config["restore_lines"],
                    config["output_spool_lines"],
                )


if __name__ == "__main__":
    unittest.main()
