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

    def test_a_fragment_naming_no_heading_is_rejected(self) -> None:
        """A renamed heading turns every link to it into a link to nowhere."""

        with tempfile.TemporaryDirectory(
            prefix="session-kit-doc-anchors.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            (root / "guide.md").write_text("# Real Heading\n", encoding="utf-8")
            (root / "README.md").write_text(
                "# Top\n"
                "[good](guide.md#real-heading)\n"
                "[gone](guide.md#renamed-heading)\n"
                "[here](#top)\n"
                "[nowhere](#never-existed)\n",
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
            self.assertIn("no such heading: guide.md#renamed-heading", result.stdout)
            self.assertIn("no such heading: #never-existed", result.stdout)
            self.assertNotIn("real-heading", result.stdout)
            self.assertNotIn("#top", result.stdout)

    def test_a_heading_inside_a_fence_is_not_a_heading(self) -> None:
        """Otherwise a shell comment in an example invents anchors."""

        with tempfile.TemporaryDirectory(
            prefix="session-kit-doc-fences.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "# Top\n```bash\n# Not A Heading\n```\n[bad](#not-a-heading)\n",
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
            self.assertIn("no such heading: #not-a-heading", result.stdout)

    def test_repeated_headings_get_numbered_anchors(self) -> None:
        """GitHub suffixes the second one; a checker that does not would lie."""

        with tempfile.TemporaryDirectory(
            prefix="session-kit-doc-repeats.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "# Notes\n## Detail\n## Detail\n"
                "[first](#detail)\n[second](#detail-1)\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(CHECKER), str(root)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_html_navigation_and_images_are_checked(self) -> None:
        """The README's nav is <a href>, and every picture on it is <img src>."""

        with tempfile.TemporaryDirectory(
            prefix="session-kit-doc-html.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            (root / "shot.png").write_bytes(b"\x89PNG\r\n")
            (root / "README.md").write_text(
                "# Top\n"
                '<p><a href="#top">here</a> · <a href="#gone">nowhere</a></p>\n'
                '<img src="shot.png" alt="">\n'
                '<img src="missing.png" alt="">\n'
                '<img src="https://example.com/x.png" alt="">\n'
                '<img src="data:image/svg+xml;base64,AAAA" alt="">\n',
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
            self.assertIn("no such heading: #gone", result.stdout)
            self.assertIn("missing target: missing.png", result.stdout)
            self.assertNotIn("shot.png", result.stdout)
            self.assertNotIn("example.com", result.stdout)
            self.assertNotIn("data:image", result.stdout)

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
