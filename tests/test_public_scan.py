from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
SCANNER = REPO / "tools" / "public-scan"


class PublicScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(
            tempfile.mkdtemp(prefix="session-kit-public-scan.", dir=REPO.parent)
        )
        (self.root / "README.md").write_text("public fixture\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def run_scan(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCANNER), str(self.root)],
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_clean_tree_ignores_tool_and_git_caches(self) -> None:
        for relative in (
            ".git/objects/cache-note",
            ".mypy_cache/cache-note",
            ".ruff_cache/cache-note",
            "__pycache__/cache-note",
        ):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("ghp_" + ("a" * 20) + "\n", encoding="utf-8")
        (self.root / ".coverage").write_text(
            "ghp_" + ("a" * 20) + "\n", encoding="utf-8"
        )

        result = self.run_scan()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "public scan passed\n")

    def test_secret_format_is_rejected(self) -> None:
        (self.root / "notes.txt").write_text(
            "ghp_" + ("a" * 20) + "\n", encoding="utf-8"
        )

        result = self.run_scan()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GitHub token", result.stdout)


if __name__ == "__main__":
    unittest.main()
