from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
SCANNER = REPO / "tools" / "public-scan"
APPROVED_DASHBOARD = REPO / "docs" / "assets" / "session-kit-dashboard.png"


class PublicScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(
            tempfile.mkdtemp(prefix="session-kit-public-scan.", dir=REPO.parent)
        )
        # Auxiliary files live outside the scanned root so a baseline fixture
        # never becomes part of the tree or history under test.
        self.workspace = Path(
            tempfile.mkdtemp(prefix="session-kit-public-scan-aux.", dir=REPO.parent)
        )
        (self.root / "README.md").write_text("public fixture\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root)
        shutil.rmtree(self.workspace)

    def write_baseline(self, body: str, name: str = "baseline") -> Path:
        path = self.workspace / name
        path.write_text(body, encoding="utf-8")
        return path

    def blob_id(self, relative: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), "hash-object", relative],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()

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

    def copy_dashboard(
        self, relative: str = "docs/assets/session-kit-dashboard.png"
    ) -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(APPROVED_DASHBOARD, target)
        return target

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

    def test_approved_dashboard_binary_is_accepted(self) -> None:
        self.copy_dashboard()

        result = self.run_scan()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dashboard_with_changed_digest_is_rejected(self) -> None:
        dashboard = self.copy_dashboard()
        payload = bytearray(dashboard.read_bytes())
        payload[-1] ^= 1
        dashboard.write_bytes(payload)

        result = self.run_scan()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("approved binary digest mismatch", result.stdout)

    def test_approved_dashboard_bytes_at_another_path_are_rejected(self) -> None:
        self.copy_dashboard("docs/assets/other.png")

        result = self.run_scan()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("binary file is not allowed", result.stdout)

    def test_other_binary_is_rejected(self) -> None:
        (self.root / "other.bin").write_bytes(b"fixture\0binary")

        result = self.run_scan()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("binary file is not allowed", result.stdout)

    def test_history_rejects_superseded_dashboard_digest(self) -> None:
        self.init_git()
        dashboard = self.copy_dashboard()
        payload = bytearray(dashboard.read_bytes())
        payload[-1] ^= 1
        dashboard.write_bytes(payload)
        self.commit("add altered dashboard")
        self.copy_dashboard()
        self.commit("restore approved dashboard")

        tree_only = self.run_scan()
        history = self.run_scan("--git-history")

        self.assertEqual(tree_only.returncode, 0, tree_only.stdout + tree_only.stderr)
        self.assertNotEqual(history.returncode, 0)
        self.assertIn("approved binary digest mismatch", history.stdout)

    def test_history_rejects_removed_other_binary(self) -> None:
        self.init_git()
        other = self.root / "other.bin"
        other.write_bytes(b"fixture\0binary")
        self.commit("add other binary")
        other.unlink()
        self.commit("remove other binary")

        tree_only = self.run_scan()
        history = self.run_scan("--git-history")

        self.assertEqual(tree_only.returncode, 0, tree_only.stdout + tree_only.stderr)
        self.assertNotEqual(history.returncode, 0)
        self.assertIn("binary file is not allowed", history.stdout)

    def test_baseline_excuses_only_the_exact_blob_it_names(self) -> None:
        marker = "restricted" + "fixture"
        self.init_git()
        (self.root / "reviewed.txt").write_text(marker + "\n", encoding="utf-8")
        (self.root / "novel.txt").write_text(marker + " two\n", encoding="utf-8")
        reviewed = self.blob_id("reviewed.txt")
        novel = self.blob_id("novel.txt")
        self.commit("add fixtures")
        (self.root / "reviewed.txt").unlink()
        (self.root / "novel.txt").unlink()
        self.commit("remove fixtures")
        baseline = self.write_baseline(
            f"# reviewed on a date\n{reviewed}  reviewed.txt\n"
        )

        unlisted = self.run_scan("--git-history", "--private-markers")
        listed = self.run_scan(
            "--git-history", "--private-markers", "--baseline", str(baseline)
        )

        self.assertNotEqual(unlisted.returncode, 0)
        self.assertIn(reviewed[:12], unlisted.stdout)
        self.assertIn(novel[:12], unlisted.stdout)
        # The reviewed blob is forgiven; the one nobody reviewed still fails.
        self.assertNotEqual(listed.returncode, 0)
        self.assertNotIn(reviewed[:12], listed.stdout)
        self.assertIn(novel[:12], listed.stdout)
        self.assertNotIn(marker, listed.stdout)

    def test_baseline_scan_passes_when_every_flagged_blob_is_reviewed(self) -> None:
        marker = "restricted" + "fixture"
        self.init_git()
        (self.root / "reviewed.txt").write_text(marker + "\n", encoding="utf-8")
        reviewed = self.blob_id("reviewed.txt")
        self.commit("add fixture")
        (self.root / "reviewed.txt").unlink()
        self.commit("remove fixture")
        baseline = self.write_baseline(f"{reviewed}  reviewed.txt\n")

        result = self.run_scan(
            "--git-history", "--private-markers", "--baseline", str(baseline)
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 of 1 baselined history blob(s) excused", result.stdout)

    def test_baseline_never_excuses_a_secret(self) -> None:
        marker = "restricted" + "fixture"
        secret = "ghp_" + ("b" * 20)
        self.init_git()
        (self.root / "both.txt").write_text(f"{marker}\n{secret}\n", encoding="utf-8")
        both = self.blob_id("both.txt")
        self.commit("add fixture")
        (self.root / "both.txt").unlink()
        self.commit("remove fixture")
        baseline = self.write_baseline(f"{both}  both.txt\n")

        result = self.run_scan(
            "--git-history", "--private-markers", "--baseline", str(baseline)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GitHub token", result.stdout)
        self.assertNotIn(secret, result.stdout)

    def test_baseline_does_not_reach_the_working_tree(self) -> None:
        """Forgiving a published blob must not let the same bytes live on."""
        marker = "restricted" + "fixture"
        self.init_git()
        (self.root / "kept.txt").write_text(marker + "\n", encoding="utf-8")
        kept = self.blob_id("kept.txt")
        self.commit("add fixture")
        baseline = self.write_baseline(f"{kept}  kept.txt\n")

        result = self.run_scan(
            "--git-history", "--private-markers", "--baseline", str(baseline)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("kept.txt: private marker", result.stdout)

    def test_baseline_requires_the_history_scan(self) -> None:
        baseline = self.write_baseline(f"{'a' * 40}  somewhere.txt\n")

        result = self.run_scan("--private-markers", "--baseline", str(baseline))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--baseline applies to --git-history scans", result.stderr)

    def test_unusable_baseline_fails_closed(self) -> None:
        blob = "a" * 40
        cases = {
            "missing": (None, "baseline is unreadable"),
            "empty": ("# nothing but a comment\n", "baseline lists no blobs"),
            "malformed": ("not-a-blob-id  somewhere.txt\n", "malformed baseline line"),
            "short": (f"{blob[:39]}  somewhere.txt\n", "malformed baseline line"),
            "uppercase": (
                f"{blob.upper()}  somewhere.txt\n",
                "malformed baseline line",
            ),
            "duplicate": (
                f"{blob}  one.txt\n{blob}  two.txt\n",
                "duplicate baseline blob",
            ),
        }
        for name, (body, reason) in cases.items():
            with self.subTest(baseline=name):
                if body is None:
                    path = self.workspace / "absent"
                else:
                    path = self.write_baseline(body, name=name)

                result = self.run_scan(
                    "--git-history", "--private-markers", "--baseline", str(path)
                )

                # The refusal has to name what is wrong; argparse prints the
                # option in its usage line whatever happens, so matching the
                # word "baseline" alone would pass for the wrong reason.
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason, result.stderr)

    def test_shipped_baseline_is_parsable_and_additive_only(self) -> None:
        """The committed baseline must survive the parser it is written for."""
        shipped = REPO / "tools" / "public-scan-history-baseline"
        self.init_git()
        self.commit("empty history")

        result = self.run_scan(
            "--git-history", "--private-markers", "--baseline", str(shipped)
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # None of the shipped blobs exist in this fixture's history, and the
        # summary has to say so rather than claim the whole list was used.
        self.assertIn("0 of 37 baselined history blob(s) excused", result.stdout)
        entries = [
            line.split("#", 1)[0].strip()
            for line in shipped.read_text(encoding="utf-8").splitlines()
        ]
        blobs = [entry.split()[0] for entry in entries if entry]
        self.assertEqual(sorted(set(blobs)), sorted(blobs))
        self.assertTrue(blobs)


if __name__ == "__main__":
    unittest.main()
