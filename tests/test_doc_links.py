from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "tools" / "check-doc-links"


class DocumentationLinkTests(unittest.TestCase):
    def test_claude_statusline_preservation_and_ledgers_are_documented(self) -> None:
        changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        updates = (REPO / "docs/update-and-rollback.md").read_text(encoding="utf-8")
        uninstall = (REPO / "docs/uninstall.md").read_text(encoding="utf-8")

        for text in (changelog, updates):
            self.assertIn("--force", text)
            self.assertIn("claude-statusline-backups.json", text)
            self.assertIn("claude-integration.json", text)
        self.assertIn("claude-statusline-backups.json", uninstall)
        self.assertIn("claude-integration.json", uninstall)
        self.assertIn("restores", uninstall)
        self.assertIn("session-kit-quota", uninstall)

    def test_repository_local_links_resolve(self) -> None:
        result = subprocess.run(
            [str(CHECKER), str(REPO)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("documentation link check passed", result.stdout)

    def test_missing_and_escaping_links_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="session-kit-doc-links.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "[missing](docs/missing.md)\n"
                "[escape](../outside.md)\n"
                "[external](https://example.com/)\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(CHECKER), str(root)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing target", result.stdout)
        self.assertIn("link escapes repository", result.stdout)
        self.assertNotIn("example.com", result.stdout)


if __name__ == "__main__":
    unittest.main()
