from __future__ import annotations

from pathlib import Path
import tomllib
import unittest


REPO = Path(__file__).resolve().parents[1]


class ShpoolExampleConfigTests(unittest.TestCase):
    def test_bounded_restore_contract(self) -> None:
        for relative in ("config/shpool.example.toml", "shpool/config.toml"):
            with self.subTest(relative=relative):
                with (REPO / relative).open("rb") as handle:
                    config = tomllib.load(handle)
                self.assertEqual({"lines": 500}, config["session_restore_mode"])
                self.assertEqual(1000, config["output_spool_lines"])
                self.assertEqual(200, config["vt100_output_spool_width"])
                self.assertLessEqual(
                    config["session_restore_mode"]["lines"],
                    config["output_spool_lines"],
                )


if __name__ == "__main__":
    unittest.main()
