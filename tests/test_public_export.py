from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.support import REPO


class PublicExportTests(unittest.TestCase):
    def make_source(self, root: Path) -> tuple[Path, str]:
        source = root / "source"
        shutil.copytree(
            REPO,
            source,
            ignore=shutil.ignore_patterns(
                ".git",
                ".claude",
                ".mypy_cache",
                ".ruff_cache",
                "__pycache__",
                "*.pyc",
            ),
        )
        subprocess.run(["git", "init", "-q", source], check=True)
        subprocess.run(
            ["git", "-C", source, "config", "user.name", "Session Kit Tests"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                source,
                "config",
                "user.email",
                "tests@example.invalid",
            ],
            check=True,
        )
        self.commit(source, "complete source")
        return source, self.head(source)

    def commit(self, source: Path, message: str) -> None:
        subprocess.run(["git", "-C", source, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", source, "commit", "-qm", message],
            check=True,
        )

    def head(self, source: Path) -> str:
        return subprocess.check_output(
            ["git", "-C", source, "rev-parse", "HEAD"],
            text=True,
        ).strip()

    def export(
        self,
        source: Path,
        commit: str,
        destination: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(source / "tools/build-public-tree"),
                "--commit",
                commit,
                "--destination",
                str(destination),
            ],
            cwd=source,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_export_contains_runtime_release_and_provenance_inputs(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="session-kit-public-export.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            source, commit = self.make_source(root)
            destination = root / "public"

            result = self.export(source, commit, destination)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for relative in (
                "LICENSES/Apache-2.0.txt",
                "deploy/session-kit-release",
                "lib/sessionkit_inventory/lifecycle.py",
                "lib/sessionkit_inventory/providers.py",
                "lib/sessionkit_inventory/reaper.py",
                "lib/sessionkit_inventory/state_io.py",
                "public-files.txt",
                "shpool/config.toml",
                "tests/support.py",
                "tests/test_lifecycle_shell.py",
                "tests/test_macos_preview.py",
                "tests/test_reaper_autoclose.py",
                "tests/test_release.py",
                "tools/build-public-tree",
                "tools/build-release-artifact",
                "tools/check-doc-links",
            ):
                self.assertTrue((destination / relative).is_file(), relative)
            source_record = json.loads(
                (destination / "SOURCE.json").read_text(encoding="utf-8")
            )
            self.assertEqual(source_record["source_commit"], commit)
            self.assertGreater(source_record["exported_files"], 30)
            self.assertFalse((destination / "public/tests-support.py").exists())

    def test_exported_tree_can_export_itself(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="session-kit-public-export.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            source, commit = self.make_source(root)
            first = root / "public-first"
            second = root / "public-second"
            first_result = self.export(source, commit, first)
            self.assertEqual(
                first_result.returncode,
                0,
                first_result.stdout + first_result.stderr,
            )
            subprocess.run(["git", "init", "-q", first], check=True)
            subprocess.run(
                ["git", "-C", first, "config", "user.name", "Session Kit Tests"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    first,
                    "config",
                    "user.email",
                    "tests@example.invalid",
                ],
                check=True,
            )
            subprocess.run(["git", "-C", first, "add", "."], check=True)
            subprocess.run(
                ["git", "-C", first, "commit", "-qm", "public source"],
                check=True,
            )
            public_commit = self.head(first)
            strict_scan = subprocess.run(
                [
                    str(first / "tools/public-scan"),
                    str(first),
                    "--git-history",
                    "--private-markers",
                ],
                cwd=first,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(
                strict_scan.returncode,
                0,
                strict_scan.stdout + strict_scan.stderr,
            )

            second_result = self.export(first, public_commit, second)

            self.assertEqual(
                second_result.returncode,
                0,
                second_result.stdout + second_result.stderr,
            )
            self.assertTrue((second / "tests/support.py").is_file())
            self.assertFalse((second / "public/tests-support.py").exists())

    def test_export_uses_manifest_from_requested_commit(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="session-kit-public-export.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            source, commit = self.make_source(root)
            (source / "public-files.txt").write_text("README.md\n", encoding="utf-8")
            destination = root / "public"

            result = self.export(source, commit, destination)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((destination / "bin/session-kit").is_file())
            self.assertTrue((destination / "LICENSES/Apache-2.0.txt").is_file())

    def test_export_rejects_partial_inventory_package(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="session-kit-public-export.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            source, _ = self.make_source(root)
            common = source / "lib/sessionkit_inventory/common.py"
            common.unlink()
            subprocess.run(
                ["git", "-C", source, "add", "lib/sessionkit_inventory/common.py"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", source, "commit", "-qm", "remove common module"],
                check=True,
            )
            destination = root / "public"

            result = self.export(source, self.head(source), destination)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lib/sessionkit_inventory/common.py", result.stderr)
            self.assertFalse(destination.exists())

    def test_export_rejects_manifest_pattern_that_matches_nothing(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="session-kit-public-export.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            source, _ = self.make_source(root)
            with (source / "public-files.txt").open("a", encoding="utf-8") as manifest:
                manifest.write("missing-public-file.txt\n")
            self.commit(source, "add invalid public pattern")
            destination = root / "public"

            result = self.export(source, self.head(source), destination)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("pattern matches nothing", result.stderr)
            self.assertFalse(destination.exists())

    def test_export_rejects_private_marker_without_echoing_it(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="session-kit-public-export.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            source, _ = self.make_source(root)
            marker = "restricted" + "fixture"
            with (source / "README.md").open("a", encoding="utf-8") as readme:
                readme.write(marker + "\n")
            self.commit(source, "add private marker fixture")
            destination = root / "public"

            result = self.export(source, self.head(source), destination)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("private marker", result.stdout + result.stderr)
            self.assertNotIn(marker, result.stdout + result.stderr)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
