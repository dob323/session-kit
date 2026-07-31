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

    def run_scan(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCANNER), str(self.root), *args],
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def init_git(self) -> None:
        subprocess.run(["git", "init", "-q", self.root], check=True)
        subprocess.run(
            ["git", "-C", self.root, "config", "user.name", "Session Kit Tests"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                self.root,
                "config",
                "user.email",
                "tests@example.invalid",
            ],
            check=True,
        )

    def commit(self, message: str) -> None:
        subprocess.run(["git", "-C", self.root, "add", "."], check=True)
        subprocess.run(["git", "-C", self.root, "commit", "-qm", message], check=True)

    def test_clean_tree_ignores_tool_and_git_caches(self) -> None:
        secret = "ghp_" + ("a" * 20)
        for relative in (
            ".git/objects/cache-note",
            ".mypy_cache/cache-note",
            ".ruff_cache/cache-note",
            "__pycache__/cache-note",
        ):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(secret + "\n", encoding="utf-8")
        (self.root / ".coverage").write_text(secret + "\n", encoding="utf-8")

        result = self.run_scan()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "public scan passed (tree)\n")

    def test_secret_format_is_rejected_without_echoing_value(self) -> None:
        secret = "ghp_" + ("a" * 20)
        (self.root / "notes.txt").write_text(secret + "\n", encoding="utf-8")

        result = self.run_scan()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GitHub token", result.stdout)
        self.assertNotIn(secret, result.stdout)

    def test_private_marker_check_is_explicit(self) -> None:
        marker = "restricted" + "fixture"
        (self.root / "notes.txt").write_text(marker + "\n", encoding="utf-8")

        default = self.run_scan()
        strict = self.run_scan("--private-markers")

        self.assertEqual(default.returncode, 0, default.stdout + default.stderr)
        self.assertNotEqual(strict.returncode, 0)
        self.assertIn("private marker", strict.stdout)
        self.assertNotIn(marker, strict.stdout)

    def test_history_scan_finds_removed_secret(self) -> None:
        self.init_git()
        secret = "sk-" + ("A" * 24)
        (self.root / "removed.txt").write_text(secret + "\n", encoding="utf-8")
        self.commit("add fixture")
        (self.root / "removed.txt").unlink()
        self.commit("remove fixture")

        tree_only = self.run_scan()
        history = self.run_scan("--git-history")

        self.assertEqual(tree_only.returncode, 0, tree_only.stdout + tree_only.stderr)
        self.assertNotEqual(history.returncode, 0)
        self.assertIn("OpenAI key", history.stdout)
        self.assertIn("history ", history.stdout)
        self.assertNotIn(secret, history.stdout)

    def test_symlink_is_rejected(self) -> None:
        (self.root / "linked").symlink_to("README.md")

        result = self.run_scan()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink is not allowed", result.stdout)

    def test_directory_symlink_is_rejected(self) -> None:
        directory = self.root / "real-directory"
        directory.mkdir()
        (directory / "content.txt").write_text("fixture\n", encoding="utf-8")
        (self.root / "linked-directory").symlink_to(directory, target_is_directory=True)

        result = self.run_scan()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("linked-directory: symlink is not allowed", result.stdout)


if __name__ == "__main__":
    unittest.main()
