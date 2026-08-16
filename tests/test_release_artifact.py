from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest

from tests.support import REPO


class ReleaseArtifactTests(unittest.TestCase):
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
        subprocess.run(["git", "-C", source, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", source, "commit", "-qm", "artifact source"],
            check=True,
        )
        commit = subprocess.check_output(
            ["git", "-C", source, "rev-parse", "HEAD"],
            text=True,
        ).strip()
        return source, commit

    def build(
        self,
        source: Path,
        commit: str,
        output: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(source / "tools/build-release-artifact"),
                "--commit",
                commit,
                "--output-dir",
                str(output),
            ],
            cwd=source,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_artifact_is_reproducible_and_bound_to_commit(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="session-kit-release-artifact.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            source, commit = self.make_source(root)
            first = root / "first"
            second = root / "second"

            result_first = self.build(source, commit, first)
            result_second = self.build(source, commit, second)

            self.assertEqual(
                result_first.returncode,
                0,
                result_first.stdout + result_first.stderr,
            )
            self.assertEqual(
                result_second.returncode,
                0,
                result_second.stdout + result_second.stderr,
            )
            basename = f"session-kit-{commit}"
            archive_name = f"{basename}.tar.gz"
            archive_first = first / archive_name
            archive_second = second / archive_name
            self.assertEqual(archive_first.read_bytes(), archive_second.read_bytes())

            digest = hashlib.sha256(archive_first.read_bytes()).hexdigest()
            self.assertEqual(
                (first / f"{basename}.sha256").read_text(encoding="utf-8"),
                f"{digest}  {archive_name}\n",
            )
            provenance = json.loads(
                (first / f"{basename}.provenance.json").read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["source_commit"], commit)
            self.assertEqual(provenance["builder_commit"], commit)
            self.assertEqual(provenance["archive_sha256"], digest)
            self.assertRegex(provenance["public_tree_sha256"], r"^[0-9a-f]{64}$")
            for path in (
                archive_first,
                first / f"{basename}.sha256",
                first / f"{basename}.provenance.json",
            ):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

            with tarfile.open(archive_first, "r:gz") as archive:
                names = archive.getnames()
                self.assertIn(f"{basename}/SOURCE.json", names)
                self.assertIn(f"{basename}/LICENSES/Apache-2.0.txt", names)
                self.assertIn(f"{basename}/bin/reset-collection-order.py", names)
                self.assertIn(f"{basename}/deploy/session-kit-release", names)
                self.assertIn(
                    f"{basename}/extras/statusline-quota-refresh.example", names
                )
                self.assertNotIn(f"{basename}/LICENSES/MIT-maniple.txt", names)
                self.assertNotIn(
                    f"{basename}/docs/build-tracks-2026-08-12.md", names
                )
                self.assertNotIn(
                    f"{basename}/tools/reset-collection-order.py", names
                )
                self.assertTrue(all(not name.startswith("/") for name in names))
                self.assertTrue(
                    all(".." not in Path(name).parts for name in names),
                    names,
                )

    def test_nonempty_output_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="session-kit-release-artifact.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            source, commit = self.make_source(root)
            output = root / "output"
            output.mkdir()
            (output / "keep").write_text("existing\n", encoding="utf-8")

            result = self.build(source, commit, output)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("absent or empty", result.stderr)
            self.assertEqual(
                (output / "keep").read_text(encoding="utf-8"), "existing\n"
            )

    def test_uncommitted_build_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="session-kit-release-artifact.",
            dir=REPO.parent,
        ) as temporary:
            root = Path(temporary)
            source, commit = self.make_source(root)
            with (source / "public-files.txt").open("a", encoding="utf-8") as manifest:
                manifest.write("# uncommitted change\n")

            result = self.build(source, commit, root / "output")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "release build input differs from requested commit",
                result.stderr,
            )


if __name__ == "__main__":
    unittest.main()
