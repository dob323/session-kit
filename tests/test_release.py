from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import unittest
from unittest import mock

from tests.support import (
    HELPERS,
    IsolatedTree,
    RELEASE_TOOL,
    REPO,
    commit_runtime,
    file_mode,
    json_stdout,
    make_backup_bundle,
    make_git_repo,
    make_sentinels,
    run,
)

RELEASE_LOADER = importlib.machinery.SourceFileLoader(
    "session_kit_release", os.fspath(RELEASE_TOOL)
)
RELEASE_SPEC = importlib.util.spec_from_loader(
    RELEASE_LOADER.name, RELEASE_LOADER
)
assert RELEASE_SPEC is not None and RELEASE_SPEC.loader is not None
release_module = importlib.util.module_from_spec(RELEASE_SPEC)
RELEASE_SPEC.loader.exec_module(release_module)


def entry_evidence(path: Path) -> tuple:
    if path.is_symlink():
        metadata = path.lstat()
        return (
            "symlink",
            os.readlink(path),
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_mtime_ns,
        )
    if not path.exists():
        return ("absent",)
    metadata = path.stat()
    return (
        "file",
        path.read_bytes(),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_mtime_ns,
    )


def write_new_launch_pair(home: Path, shpool_id: str = "main2") -> tuple[Path, Path]:
    launch_dir = home / ".local/state/shpool-start"
    launch_dir.mkdir(parents=True, mode=0o700)
    launch_dir.chmod(0o700)
    source_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    start = launch_dir / shpool_id
    expected = launch_dir / f"{shpool_id}.expected"
    start.write_text(
        f"claude\t/srv/project\t{source_uuid}\tfork\n",
        encoding="utf-8",
    )
    expected.write_text(
        "claude\t/srv/project\tfixture-boot\t1700000000000"
        f"\t1001\t10010\t10\t100\t{source_uuid}\tfork\n",
        encoding="utf-8",
    )
    start.chmod(0o600)
    expected.chmod(0o600)
    return start, expected


def rewrite_backup_home(bundle: Path, source: str, target: str) -> None:
    """Rewrite the HOME prefix in a complete test backup."""
    original_archive = bundle / "installed-files.tar"
    replacement_archive = bundle / "installed-files.rewritten.tar"
    with (
        tarfile.open(original_archive, mode="r:") as original,
        tarfile.open(replacement_archive, mode="w") as replacement,
    ):
        for member in original:
            payload = original.extractfile(member)
            assert payload is not None
            content = payload.read().replace(source.encode(), target.encode())
            member.name = member.name.replace(source, target)
            member.size = len(content)
            replacement.addfile(member, io.BytesIO(content))
    os.replace(replacement_archive, original_archive)

    checksum_lines: list[str] = []
    stat_lines: list[str] = []
    with tarfile.open(original_archive, mode="r:") as archive:
        for member in archive:
            payload = archive.extractfile(member)
            assert payload is not None
            content = payload.read()
            checksum_lines.append(
                f"{hashlib.sha256(content).hexdigest()}  {member.name}\n"
            )
            stat_lines.append(
                f"{member.mode & 0o777:o}\\t{member.uid}\\t{member.gid}"
                f"\\t{member.size}\\t1\\t2020-01-01 00:00:00 +0000"
                f"\\t{member.name}\n"
            )
    checksum_payload = "".join(checksum_lines)
    (bundle / "source-files.before.sha256").write_text(
        checksum_payload, encoding="utf-8"
    )
    (bundle / "source-files.after.sha256").write_text(
        checksum_payload, encoding="utf-8"
    )
    (bundle / "source-files.stat").write_text(
        "".join(stat_lines), encoding="utf-8"
    )

    manifest = bundle / "MANIFEST.sha256"
    payload_names = sorted(
        path.relative_to(bundle).as_posix()
        for path in bundle.iterdir()
        if path.is_file()
        and path.name not in {"MANIFEST.sha256", "BACKUP_COMPLETE.json"}
    )
    manifest.write_text(
        "".join(
            f"{hashlib.sha256((bundle / name).read_bytes()).hexdigest()}  {name}\n"
            for name in payload_names
        ),
        encoding="utf-8",
    )
    marker_path = bundle / "BACKUP_COMPLETE.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")


class ReleaseToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = IsolatedTree()
        self.tree.state_dir.parent.mkdir(parents=True, mode=0o700)
        self.tree.state_dir.parent.chmod(0o700)
        self.repo, self.sha1 = make_git_repo(self.tree.base)
        self.backup = make_backup_bundle(self.tree.base, self.sha1)

    def tearDown(self) -> None:
        self.tree.close()

    def test_build_is_from_exact_commit_and_immutable(self) -> None:
        release = self.tree.build(self.repo, self.sha1)
        result = json_stdout(run([RELEASE_TOOL, "verify", release, "--commit", self.sha1]))
        self.assertTrue(result["ok"])
        metadata = json.loads((release / "RELEASE.json").read_text())
        self.assertEqual(metadata["commit"], self.sha1)
        self.assertEqual(3, metadata["schema_version"])
        self.assertEqual(file_mode(release), 0o555)
        self.assertTrue(
            (release / "lib/sessionkit_inventory/__init__.py").is_file()
        )
        self.assertTrue((release / "lib/sessionkit_inventory/common.py").is_file())
        self.assertTrue(
            (release / "lib/sessionkit_inventory/lifecycle.py").is_file()
        )
        self.assertTrue(
            (release / "lib/sessionkit_inventory/providers.py").is_file()
        )
        self.assertTrue(
            (release / "lib/sessionkit_inventory/state_io.py").is_file()
        )
        for path in release.rglob("*"):
            self.assertFalse(file_mode(path) & 0o222, path)

        # Dirty working-tree content is not included: the release came from SHA.
        (self.repo / "README.md").write_text("uncommitted\n", encoding="utf-8")
        self.assertEqual((release / "README.md").read_text(), "fixture v1\n")

    def test_current_verifier_accepts_bounded_legacy_release_schema(self) -> None:
        from tests.support import git

        legacy_base = self.tree.base / "legacy-release"
        legacy_base.mkdir()
        legacy_repo, _ = make_git_repo(legacy_base)
        for relative in (
            "lib/sessionkit_inventory/__init__.py",
            "lib/sessionkit_inventory/common.py",
            "lib/sessionkit_inventory/lifecycle.py",
            "lib/sessionkit_inventory/providers.py",
            "lib/sessionkit_inventory/state_io.py",
        ):
            (legacy_repo / relative).unlink()
        git(legacy_repo, "add", "lib/sessionkit_inventory")
        git(legacy_repo, "commit", "-qm", "create legacy release layout")
        legacy_sha = git(legacy_repo, "rev-parse", "HEAD")
        with mock.patch.object(
            release_module,
            "CURRENT_RELEASE_SCHEMA_VERSION",
            1,
        ):
            release_module.build_release(
                legacy_repo,
                legacy_sha,
                self.tree.release_root,
                False,
            )
        legacy_release = self.tree.release_root / legacy_sha
        metadata = release_module.verify_release(
            legacy_release,
            legacy_sha,
        )
        self.assertEqual(1, metadata["schema_version"])
        self.assertFalse(
            (legacy_release / "lib/sessionkit_inventory").exists()
        )

        incomplete_base = self.tree.base / "incomplete-legacy-release"
        incomplete_base.mkdir()
        incomplete_repo, _ = make_git_repo(incomplete_base)
        for relative in (
            "lib/sessionkit_inventory/__init__.py",
            "lib/sessionkit_inventory/common.py",
            "lib/sessionkit_inventory/lifecycle.py",
            "lib/sessionkit_inventory/providers.py",
            "lib/sessionkit_inventory/state_io.py",
            "lib/session_inventory.py",
        ):
            (incomplete_repo / relative).unlink()
        git(
            incomplete_repo,
            "add",
            "lib/session_inventory.py",
            "lib/sessionkit_inventory",
        )
        git(incomplete_repo, "commit", "-qm", "remove legacy required payload")
        incomplete_sha = git(incomplete_repo, "rev-parse", "HEAD")
        with (
            mock.patch.object(
                release_module,
                "CURRENT_RELEASE_SCHEMA_VERSION",
                1,
            ),
            self.assertRaisesRegex(
                release_module.ReleaseError,
                "lib/session_inventory.py",
            ),
        ):
            release_module.build_release(
                incomplete_repo,
                incomplete_sha,
                self.tree.release_root,
                False,
            )
        self.assertFalse(
            (self.tree.release_root / incomplete_sha).exists()
        )

    def test_verifier_rejects_unsupported_release_schema_types(self) -> None:
        release = self.tree.build(self.repo, self.sha1)
        metadata_path = release / "RELEASE.json"
        original = metadata_path.read_bytes()
        for schema in (0, 4, True, "1"):
            with self.subTest(schema=repr(schema)):
                metadata = json.loads(original)
                metadata["schema_version"] = schema
                metadata_path.chmod(0o644)
                metadata_path.write_text(
                    json.dumps(metadata, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                metadata_path.chmod(0o444)
                with self.assertRaisesRegex(
                    release_module.ReleaseError,
                    "unsupported release metadata",
                ):
                    release_module.verify_release(release, self.sha1)
        metadata_path.chmod(0o644)
        metadata_path.write_bytes(original)
        metadata_path.chmod(0o444)
        release_module.verify_release(release, self.sha1)

    def test_release_tree_is_fsynced_bottom_up_before_publication(self) -> None:
        real_fsync = os.fsync
        real_rename = os.rename
        fsynced: set[Path] = set()
        checkpoints: list[tuple[str, Path]] = []

        def observe_fsync(fd: int) -> None:
            with contextlib.suppress(OSError):
                fsynced.add(Path(os.readlink(f"/proc/self/fd/{fd}")))
            real_fsync(fd)

        def observe_checkpoint(kind: str, path: Path) -> None:
            self.assertIn(path, fsynced)
            checkpoints.append((kind, path))

        def observe_rename(source: os.PathLike, destination: os.PathLike) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            if destination_path == self.tree.release_root / self.sha1:
                expected_files = {
                    path for path in source_path.rglob("*") if path.is_file()
                }
                expected_directories = {
                    source_path,
                    *(path for path in source_path.rglob("*") if path.is_dir()),
                }
                observed_files = {
                    path for kind, path in checkpoints if kind == "file"
                }
                observed_directories = {
                    path for kind, path in checkpoints if kind == "directory"
                }
                self.assertEqual(expected_files, observed_files)
                self.assertEqual(expected_directories, observed_directories)
                file_positions = [
                    index
                    for index, (kind, _) in enumerate(checkpoints)
                    if kind == "file"
                ]
                directory_positions = [
                    index
                    for index, (kind, _) in enumerate(checkpoints)
                    if kind == "directory"
                ]
                self.assertLess(max(file_positions), min(directory_positions))
                self.assertEqual(
                    source_path,
                    [path for kind, path in checkpoints if kind == "directory"][-1],
                )
                self.assertIn(source_path / "bin", observed_directories)
            real_rename(source, destination)

        with mock.patch.object(
            release_module.os, "fsync", side_effect=observe_fsync
        ), mock.patch.object(
            release_module,
            "release_durability_checkpoint",
            side_effect=observe_checkpoint,
        ), mock.patch.object(
            release_module.os, "rename", side_effect=observe_rename
        ):
            release_module.build_release(
                self.repo, self.sha1, self.tree.release_root, False
            )
            release = self.tree.release_root / self.sha1
        self.assertTrue(checkpoints)
        self.assertEqual(self.sha1, json.loads((release / "RELEASE.json").read_text())["commit"])

    def test_verify_rejects_checksum_and_mode_tampering(self) -> None:
        release = self.tree.build(self.repo, self.sha1)
        target = release / "README.md"
        target.chmod(0o644)
        target.write_text("tampered\n", encoding="utf-8")
        bad_sum = run([RELEASE_TOOL, "verify", release], check=False)
        self.assertNotEqual(bad_sum.returncode, 0)
        self.assertIn("checksum mismatch", bad_sum.stderr)

        # Rebuild in a second tree, then change only an executable mode.
        tree2 = IsolatedTree()
        try:
            repo2, sha2 = make_git_repo(tree2.base)
            release2 = tree2.build(repo2, sha2)
            helper = release2 / "bin/sp"
            helper.chmod(0o444)
            bad_mode = run([RELEASE_TOOL, "verify", release2], check=False)
            self.assertNotEqual(bad_mode.returncode, 0)
            self.assertIn("mode mismatch", bad_mode.stderr)
        finally:
            tree2.close()

    def test_backup_requires_complete_marker_manifest_and_payload(self) -> None:
        good = json_stdout(run([RELEASE_TOOL, "verify-backup", self.backup]))
        self.assertTrue(good["ok"])
        (self.backup / "canonical-state.txt").write_text("changed\n", encoding="utf-8")
        bad = run([RELEASE_TOOL, "verify-backup", self.backup], check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("payload checksum mismatch", bad.stderr)

    def test_build_rejects_non_sha_and_missing_required_payload(self) -> None:
        option_like = run(
            [
                RELEASE_TOOL,
                "build",
                "--repo",
                self.repo,
                "--sha=--help",
                "--release-root",
                self.tree.release_root,
            ],
            check=False,
        )
        self.assertNotEqual(option_like.returncode, 0)
        self.assertIn("full lowercase commit ID", option_like.stderr)
        self.assertFalse(self.tree.release_root.exists())

        (self.repo / "lib/session_inventory.py").unlink()
        from tests.support import git

        git(self.repo, "add", "lib/session_inventory.py")
        git(self.repo, "commit", "-qm", "remove required payload")
        incomplete_sha = git(self.repo, "rev-parse", "HEAD")
        incomplete = run(
            [
                RELEASE_TOOL,
                "build",
                "--repo",
                self.repo,
                "--sha",
                incomplete_sha,
                "--release-root",
                self.tree.release_root,
            ],
            check=False,
        )
        self.assertNotEqual(incomplete.returncode, 0)
        self.assertIn("missing required payload", incomplete.stderr)
        self.assertFalse((self.tree.release_root / incomplete_sha).exists())

    def test_build_rejects_partial_inventory_package(self) -> None:
        for index, relative in enumerate(
            (
                "lib/sessionkit_inventory/__init__.py",
                "lib/sessionkit_inventory/common.py",
                "lib/sessionkit_inventory/lifecycle.py",
                "lib/sessionkit_inventory/providers.py",
                "lib/sessionkit_inventory/state_io.py",
            ),
            start=1,
        ):
            with self.subTest(relative=relative):
                case_base = self.tree.base / f"missing-package-{index}"
                case_base.mkdir()
                repo, _ = make_git_repo(case_base)
                (repo / relative).unlink()
                from tests.support import git

                git(repo, "add", relative)
                git(repo, "commit", "-qm", f"remove {Path(relative).name}")
                incomplete_sha = git(repo, "rev-parse", "HEAD")
                incomplete = run(
                    [
                        RELEASE_TOOL,
                        "build",
                        "--repo",
                        repo,
                        "--sha",
                        incomplete_sha,
                        "--release-root",
                        self.tree.release_root,
                    ],
                    check=False,
                )
                self.assertNotEqual(incomplete.returncode, 0)
                self.assertIn(relative, incomplete.stderr)
                self.assertFalse(
                    (self.tree.release_root / incomplete_sha).exists()
                )

    def test_dry_run_changes_nothing_and_reports_missing_sentinels(self) -> None:
        self.tree.build(self.repo, self.sha1)
        result = json_stdout(
            run(
                [
                    RELEASE_TOOL,
                    "bootstrap",
                    *self.tree.activation_args(self.sha1, self.backup),
                    "--dry-run",
                ]
            )
        )
        self.assertEqual(len(result["missing_sentinels"]), 2)
        self.assertFalse(self.tree.root.joinpath("current").exists())
        self.assertFalse(self.tree.bin_dir.exists())

    def test_bootstrap_activate_and_rollback_pin_exact_releases(self) -> None:
        release1 = self.tree.build(self.repo, self.sha1)
        sha2 = commit_runtime(self.repo, "v2")
        release2 = self.tree.build(self.repo, sha2)
        make_sentinels(self.tree.home)

        boot = json_stdout(
            run(
                [
                    RELEASE_TOOL,
                    "bootstrap",
                    *self.tree.activation_args(self.sha1, self.backup),
                ]
            )
        )
        self.assertEqual(boot["result"], "launcher-installed-and-current-switched")
        self.assertEqual(self.tree.root.joinpath("current").resolve(), release1)
        self.assertEqual(
            (self.tree.root / "launcher").read_bytes(),
            (release1 / "deploy/session-kit-launcher").read_bytes(),
        )
        for helper in HELPERS:
            self.assertTrue((self.tree.bin_dir / helper).is_symlink())
        env = {"HOME": str(self.tree.home), "SESSION_KIT_ROOT": str(self.tree.root)}
        self.assertEqual(run([self.tree.bin_dir / "sp", "health"], env=env).stdout, "v1:sp:health\n")

        activated = json_stdout(
            run(
                [
                    RELEASE_TOOL,
                    "activate",
                    *self.tree.activation_args(sha2, self.backup),
                ]
            )
        )
        self.assertEqual(activated["previous_commit"], self.sha1)
        self.assertEqual(self.tree.root.joinpath("current").resolve(), release2)
        self.assertEqual(run([self.tree.bin_dir / "sp", "health"], env=env).stdout, "v2:sp:health\n")

        rolled = json_stdout(
            run(
                [
                    RELEASE_TOOL,
                    "rollback",
                    *self.tree.activation_args(self.sha1, self.backup),
                ]
            )
        )
        self.assertEqual(rolled["action"], "rollback")
        self.assertEqual(rolled["previous_commit"], sha2)
        self.assertEqual(run([self.tree.bin_dir / "sp", "health"], env=env).stdout, "v1:sp:health\n")

    def test_rollback_dry_run_refuses_new_launch_records_without_mutation(self) -> None:
        release1 = self.tree.build(self.repo, self.sha1)
        sha2 = commit_runtime(self.repo, "v2")
        release2 = self.tree.build(self.repo, sha2)
        make_sentinels(self.tree.home)
        run(
            [
                RELEASE_TOOL,
                "bootstrap",
                *self.tree.activation_args(self.sha1, self.backup),
            ]
        )
        run(
            [
                RELEASE_TOOL,
                "activate",
                *self.tree.activation_args(sha2, self.backup),
            ]
        )
        start, expected = write_new_launch_pair(self.tree.home)
        marker = self.tree.state_dir.parent / "integration-ready-v1"
        receipt = self.tree.state_dir / "last-activation.json"
        creation_lock = self.tree.state_dir.parent / "create.lock"
        paths = [
            self.tree.root / "current",
            marker,
            receipt,
            start,
            expected,
            self.tree.home / ".no_shpool",
            self.tree.home / ".no_shpool_reaper",
        ]
        before = {path: entry_evidence(path) for path in paths}

        refused = run(
            [
                RELEASE_TOOL,
                "rollback",
                *self.tree.activation_args(self.sha1, self.backup),
                "--dry-run",
            ],
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn(
            "new-format retained launch pair blocks older-release rollback",
            refused.stderr,
        )
        self.assertEqual(before, {path: entry_evidence(path) for path in paths})
        self.assertFalse(creation_lock.exists())
        self.assertFalse((self.tree.state_dir / "activation-transaction.json").exists())
        self.assertEqual(release2, (self.tree.root / "current").resolve())
        self.assertNotEqual(release1, (self.tree.root / "current").resolve())

    def test_rollback_apply_refuses_new_launch_records_without_mutation(self) -> None:
        release1 = self.tree.build(self.repo, self.sha1)
        sha2 = commit_runtime(self.repo, "v2")
        release2 = self.tree.build(self.repo, sha2)
        make_sentinels(self.tree.home)
        run(
            [
                RELEASE_TOOL,
                "bootstrap",
                *self.tree.activation_args(self.sha1, self.backup),
            ]
        )
        run(
            [
                RELEASE_TOOL,
                "activate",
                *self.tree.activation_args(sha2, self.backup),
            ]
        )
        start, expected = write_new_launch_pair(self.tree.home)
        marker = self.tree.state_dir.parent / "integration-ready-v1"
        receipt = self.tree.state_dir / "last-activation.json"
        creation_lock = self.tree.state_dir.parent / "create.lock"
        paths = [
            self.tree.root / "current",
            marker,
            receipt,
            start,
            expected,
            self.tree.home / ".no_shpool",
            self.tree.home / ".no_shpool_reaper",
        ]
        before = {path: entry_evidence(path) for path in paths}

        refused = run(
            [
                RELEASE_TOOL,
                "rollback",
                *self.tree.activation_args(self.sha1, self.backup),
            ],
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn(
            "new-format retained launch pair blocks older-release rollback",
            refused.stderr,
        )
        self.assertEqual(before, {path: entry_evidence(path) for path in paths})
        self.assertFalse(creation_lock.exists())
        self.assertFalse((self.tree.state_dir / "activation-transaction.json").exists())
        self.assertEqual(release2, (self.tree.root / "current").resolve())
        self.assertNotEqual(release1, (self.tree.root / "current").resolve())

    def test_rollback_launch_gate_allows_legacy_and_refuses_unsafe_pairs(self) -> None:
        launch_dir = self.tree.home / ".local/state/shpool-start"
        launch_dir.mkdir(parents=True, mode=0o700)
        launch_dir.chmod(0o700)
        start = launch_dir / "main2"
        expected = launch_dir / "main2.expected"
        exact_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        start.write_text(
            f"claude\t/srv/project\t{exact_uuid}\n",
            encoding="utf-8",
        )
        expected.write_text(
            "claude\t/srv/project\tfixture-boot\t1700000000000"
            f"\t1001\t10010\t10\t100\t{exact_uuid}\n",
            encoding="utf-8",
        )
        start.chmod(0o600)
        expected.chmod(0o600)
        self.assertEqual(
            1,
            release_module.validate_rollback_launch_records(self.tree.home),
        )

        expected.write_text(
            "claude\t/srv/project\tfixture-boot\t1700000000000"
            f"\t1001\t10010\t10\t100\t{exact_uuid}\tresume\n",
            encoding="utf-8",
        )
        expected.chmod(0o600)
        with self.assertRaisesRegex(
            release_module.ReleaseError,
            "mismatched retained launch formats",
        ):
            release_module.validate_rollback_launch_records(self.tree.home)

        expected.unlink()
        os.symlink("/does/not/exist", expected)
        with self.assertRaisesRegex(
            release_module.ReleaseError,
            "unsafe retained launch record symlink",
        ):
            release_module.validate_rollback_launch_records(self.tree.home)

    def test_rollback_dry_run_refuses_unsafe_existing_creation_lock_read_only(
        self,
    ) -> None:
        self.tree.build(self.repo, self.sha1)
        sha2 = commit_runtime(self.repo, "v2")
        release2 = self.tree.build(self.repo, sha2)
        make_sentinels(self.tree.home)
        run(
            [
                RELEASE_TOOL,
                "bootstrap",
                *self.tree.activation_args(self.sha1, self.backup),
            ]
        )
        run(
            [
                RELEASE_TOOL,
                "activate",
                *self.tree.activation_args(sha2, self.backup),
            ]
        )
        creation_lock = self.tree.state_dir.parent / "create.lock"
        creation_lock.write_text("unsafe mode\n", encoding="utf-8")
        creation_lock.chmod(0o644)
        marker = self.tree.state_dir.parent / "integration-ready-v1"
        receipt = self.tree.state_dir / "last-activation.json"
        paths = [
            self.tree.root / "current",
            marker,
            receipt,
            creation_lock,
            self.tree.home / ".no_shpool",
            self.tree.home / ".no_shpool_reaper",
        ]
        before = {path: entry_evidence(path) for path in paths}

        refused = run(
            [
                RELEASE_TOOL,
                "rollback",
                *self.tree.activation_args(self.sha1, self.backup),
                "--dry-run",
            ],
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn(
            "session creation lock must be current-owner mode-0600",
            refused.stderr,
        )
        self.assertEqual(before, {path: entry_evidence(path) for path in paths})
        self.assertFalse((self.tree.state_dir / "activation-transaction.json").exists())
        self.assertEqual(release2, (self.tree.root / "current").resolve())

    def test_rollback_apply_refuses_unsafe_state_root_without_repair(self) -> None:
        self.tree.build(self.repo, self.sha1)
        sha2 = commit_runtime(self.repo, "v2")
        release2 = self.tree.build(self.repo, sha2)
        make_sentinels(self.tree.home)
        run(
            [
                RELEASE_TOOL,
                "bootstrap",
                *self.tree.activation_args(self.sha1, self.backup),
            ]
        )
        run(
            [
                RELEASE_TOOL,
                "activate",
                *self.tree.activation_args(sha2, self.backup),
            ]
        )
        state_root = self.tree.state_dir.parent
        state_root.chmod(0o755)
        marker = state_root / "integration-ready-v1"
        receipt = self.tree.state_dir / "last-activation.json"
        creation_lock = state_root / "create.lock"
        paths = [
            self.tree.root / "current",
            marker,
            receipt,
            self.tree.home / ".no_shpool",
            self.tree.home / ".no_shpool_reaper",
        ]
        before = {path: entry_evidence(path) for path in paths}

        refused = run(
            [
                RELEASE_TOOL,
                "rollback",
                *self.tree.activation_args(self.sha1, self.backup),
            ],
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn(
            "session-kit state directory must be current-owner mode-0700",
            refused.stderr,
        )
        self.assertEqual(before, {path: entry_evidence(path) for path in paths})
        self.assertEqual(0o755, stat.S_IMODE(state_root.stat().st_mode))
        self.assertFalse(creation_lock.exists())
        self.assertFalse((self.tree.state_dir / "activation-transaction.json").exists())
        self.assertEqual(release2, (self.tree.root / "current").resolve())

    def test_bootstrap_baseexception_after_every_replacement_restores_prestate(self) -> None:
        self.tree.build(self.repo, self.sha1)
        make_sentinels(self.tree.home)
        self.tree.root.mkdir(parents=True, exist_ok=True)
        self.tree.bin_dir.mkdir(parents=True)
        launcher = self.tree.root / "launcher"
        launcher.write_bytes(b"old launcher\n")
        launcher.chmod(0o751)
        helper_paths = [self.tree.bin_dir / helper for helper in HELPERS]
        helper_paths[0].write_bytes(b"old sp\n")
        helper_paths[0].chmod(0o711)
        os.symlink("/old/target", helper_paths[1])
        helper_paths[2].write_bytes(b"old status\n")
        helper_paths[2].chmod(0o700)
        paths = [
            self.tree.root / "current",
            launcher,
            *helper_paths,
            self.tree.state_dir / "last-activation.json",
        ]
        before = {path: entry_evidence(path) for path in paths}
        args = release_module.parser().parse_args(
            [
                "bootstrap",
                *[os.fspath(value) for value in self.tree.activation_args(self.sha1, self.backup)],
            ]
        )
        checkpoints = [
            "current",
            "launcher",
            *(f"helper:{helper}" for helper in HELPERS),
            "receipt",
        ]

        class InjectedCrash(BaseException):
            pass

        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                def inject(label: str) -> None:
                    if label == checkpoint:
                        raise InjectedCrash(label)

                with mock.patch.object(
                    release_module, "bootstrap_checkpoint", side_effect=inject
                ):
                    with self.assertRaises(InjectedCrash):
                        release_module.bootstrap(args)
                after = {path: entry_evidence(path) for path in paths}
                self.assertEqual(before, after)
                self.assertFalse(
                    (self.tree.state_dir / "bootstrap-transaction.json").exists()
                )

    def test_each_new_helper_is_runnable_in_current_launcher_helper_order(self) -> None:
        release = self.tree.build(self.repo, self.sha1)
        make_sentinels(self.tree.home)
        args = release_module.parser().parse_args(
            [
                "bootstrap",
                *[os.fspath(value) for value in self.tree.activation_args(self.sha1, self.backup)],
            ]
        )
        observed: list[str] = []
        env = {
            "HOME": str(self.tree.home),
            "SESSION_KIT_ROOT": str(self.tree.root),
        }

        def inspect_checkpoint(label: str) -> None:
            observed.append(label)
            if label.startswith("helper:"):
                helper = label.split(":", 1)[1]
                proc = run([self.tree.bin_dir / helper, "probe"], env=env)
                self.assertEqual(f"v1:{helper}:probe\n", proc.stdout)

        with mock.patch.object(
            release_module, "bootstrap_checkpoint", side_effect=inspect_checkpoint
        ):
            release_module.bootstrap(args)
        self.assertEqual(
            [
                "current",
                "launcher",
                *(f"helper:{helper}" for helper in HELPERS),
                "receipt",
                "committed",
            ],
            observed,
        )
        self.assertEqual(release, (self.tree.root / "current").resolve())

    def test_crash_after_final_helper_blocks_activate_then_recovers_prestate(self) -> None:
        self.tree.build(self.repo, self.sha1)
        make_sentinels(self.tree.home)
        args = release_module.parser().parse_args(
            [
                "bootstrap",
                *[os.fspath(value) for value in self.tree.activation_args(self.sha1, self.backup)],
            ]
        )
        paths = release_module.bootstrap_transaction_paths(args)
        before = {path: entry_evidence(path) for path in paths}

        def crash_after_final(label: str) -> None:
            if label == f"helper:{HELPERS[-1]}":
                raise KeyboardInterrupt("simulated host death")

        with mock.patch.object(
            release_module, "bootstrap_checkpoint", side_effect=crash_after_final
        ), mock.patch.object(
            release_module,
            "restore_entries",
            side_effect=OSError("simulated process death before rollback"),
        ):
            with self.assertRaises(release_module.ReleaseError):
                release_module.bootstrap(args)
        journal = self.tree.state_dir / "bootstrap-transaction.json"
        self.assertTrue(journal.is_file())
        current_before_activate = os.readlink(self.tree.root / "current")
        refused = run(
            [
                RELEASE_TOOL,
                "activate",
                *self.tree.activation_args(self.sha1, self.backup),
                "--allow-unbootstrapped",
            ],
            check=False,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("pending bootstrap transaction", refused.stderr)
        self.assertEqual(current_before_activate, os.readlink(self.tree.root / "current"))

        self.assertEqual(
            "restored-incomplete",
            release_module.recover_bootstrap_transaction(args),
        )
        self.assertFalse(journal.exists())
        after = {path: entry_evidence(path) for path in paths}
        self.assertEqual(before, after)

    def test_pending_bootstrap_restores_before_target_preconditions(self) -> None:
        self.tree.build(self.repo, self.sha1)
        make_sentinels(self.tree.home)
        args = release_module.parser().parse_args(
            [
                "bootstrap",
                *[os.fspath(value) for value in self.tree.activation_args(self.sha1, self.backup)],
            ]
        )
        paths = release_module.bootstrap_transaction_paths(args)
        before = {path: entry_evidence(path) for path in paths}

        def crash_after_current(label: str) -> None:
            if label == "current":
                raise KeyboardInterrupt("simulated process death")

        with mock.patch.object(
            release_module, "bootstrap_checkpoint", side_effect=crash_after_current
        ), mock.patch.object(
            release_module,
            "restore_entries",
            side_effect=OSError("simulated death before rollback"),
        ):
            with self.assertRaises(release_module.ReleaseError):
                release_module.bootstrap(args)
        journal = self.tree.state_dir / "bootstrap-transaction.json"
        self.assertTrue(journal.is_file())

        (self.tree.home / ".no_shpool").unlink()
        with self.assertRaisesRegex(release_module.ReleaseError, "missing safety sentinels"):
            release_module.bootstrap(args)
        self.assertFalse(journal.exists())
        self.assertEqual(before, {path: entry_evidence(path) for path in paths})

    def test_incomplete_bootstrap_divergence_retains_journal(self) -> None:
        release = self.tree.build(self.repo, self.sha1)
        make_sentinels(self.tree.home)
        args = release_module.parser().parse_args(
            [
                "bootstrap",
                *[os.fspath(value) for value in self.tree.activation_args(self.sha1, self.backup)],
            ]
        )

        def crash_after_current(label: str) -> None:
            if label == "current":
                raise KeyboardInterrupt("simulated process death")

        with mock.patch.object(
            release_module, "bootstrap_checkpoint", side_effect=crash_after_current
        ), mock.patch.object(
            release_module,
            "restore_entries",
            side_effect=OSError("simulated death before rollback"),
        ):
            with self.assertRaises(release_module.ReleaseError):
                release_module.bootstrap(args)

        journal = self.tree.state_dir / "bootstrap-transaction.json"
        receipt = self.tree.state_dir / "last-activation.json"
        receipt.write_text('{"unrelated":true}\n', encoding="utf-8")
        receipt.chmod(0o600)
        with self.assertRaisesRegex(release_module.ReleaseError, "diverged"):
            release_module.recover_bootstrap_transaction(args)
        self.assertTrue(journal.is_file())
        self.assertEqual(release, (self.tree.root / "current").resolve())
        self.assertEqual('{"unrelated":true}\n', receipt.read_text())

    def test_next_invocation_finishes_durable_committed_bootstrap(self) -> None:
        self.tree.build(self.repo, self.sha1)
        make_sentinels(self.tree.home)
        args = release_module.parser().parse_args(
            [
                "bootstrap",
                *[os.fspath(value) for value in self.tree.activation_args(self.sha1, self.backup)],
            ]
        )
        real_clear = release_module.clear_bootstrap_journal
        with mock.patch.object(release_module, "clear_bootstrap_journal"):
            release_module.bootstrap(args)
        journal = self.tree.state_dir / "bootstrap-transaction.json"
        self.assertTrue(journal.is_file())
        payload = json.loads(journal.read_text())
        self.assertEqual("committed", payload["phase"])
        with mock.patch.object(
            release_module, "clear_bootstrap_journal", side_effect=real_clear
        ):
            outcome = release_module.recover_bootstrap_transaction(args)
        self.assertEqual("finished-committed", outcome)
        self.assertFalse(journal.exists())
        self.assertEqual(self.sha1, release_module.release_commit_from_current(self.tree.root))

    def test_crash_after_committed_journal_is_finished_not_rolled_back(self) -> None:
        release = self.tree.build(self.repo, self.sha1)
        make_sentinels(self.tree.home)
        args = release_module.parser().parse_args(
            [
                "bootstrap",
                *[os.fspath(value) for value in self.tree.activation_args(self.sha1, self.backup)],
            ]
        )

        class SimulatedHostDeath(BaseException):
            pass

        def crash(label: str) -> None:
            if label == "committed":
                raise SimulatedHostDeath(label)

        with mock.patch.object(
            release_module, "bootstrap_checkpoint", side_effect=crash
        ):
            with self.assertRaises(SimulatedHostDeath):
                release_module.bootstrap(args)
        journal = self.tree.state_dir / "bootstrap-transaction.json"
        self.assertEqual("committed", json.loads(journal.read_text())["phase"])
        self.assertEqual(release, (self.tree.root / "current").resolve())
        self.assertEqual(
            "finished-committed",
            release_module.recover_bootstrap_transaction(args),
        )
        self.assertFalse(journal.exists())

    def test_committed_bootstrap_corruption_retains_journal_without_rollback(self) -> None:
        release = self.tree.build(self.repo, self.sha1)
        make_sentinels(self.tree.home)
        args = release_module.parser().parse_args(
            [
                "bootstrap",
                *[os.fspath(value) for value in self.tree.activation_args(self.sha1, self.backup)],
            ]
        )
        with mock.patch.object(release_module, "clear_bootstrap_journal"):
            release_module.bootstrap(args)
        journal = self.tree.state_dir / "bootstrap-transaction.json"
        launcher = self.tree.root / "launcher"
        launcher.chmod(0o755)
        launcher.write_bytes(b"corrupt committed launcher\n")
        launcher.chmod(0o555)

        with self.assertRaisesRegex(
            release_module.ReleaseError, "poststate is inconsistent"
        ):
            release_module.recover_bootstrap_transaction(args)
        self.assertTrue(journal.is_file())
        self.assertEqual(release, (self.tree.root / "current").resolve())
        self.assertEqual(b"corrupt committed launcher\n", launcher.read_bytes())

    def test_launcher_temp_is_fsynced_before_atomic_replacement(self) -> None:
        self.tree.build(self.repo, self.sha1)
        make_sentinels(self.tree.home)
        args = release_module.parser().parse_args(
            [
                "bootstrap",
                *[os.fspath(value) for value in self.tree.activation_args(self.sha1, self.backup)],
            ]
        )
        real_fsync = os.fsync
        real_replace = os.replace
        fsynced_launcher = False

        def observe_fsync(fd: int) -> None:
            nonlocal fsynced_launcher
            with contextlib.suppress(OSError):
                opened = os.readlink(f"/proc/self/fd/{fd}")
                if "/.launcher." in opened:
                    fsynced_launcher = True
            real_fsync(fd)

        def observe_replace(source: os.PathLike, destination: os.PathLike) -> None:
            if Path(destination) == self.tree.root / "launcher":
                self.assertTrue(fsynced_launcher)
            real_replace(source, destination)

        with mock.patch.object(
            release_module.os, "fsync", side_effect=observe_fsync
        ), mock.patch.object(
            release_module.os, "replace", side_effect=observe_replace
        ):
            release_module.bootstrap(args)
        self.assertTrue(fsynced_launcher)

    def test_activation_marker_and_current_are_one_rollback_transaction(self) -> None:
        release1 = self.tree.build(self.repo, self.sha1)
        sha2 = commit_runtime(self.repo, "v2")
        release2 = self.tree.build(self.repo, sha2)
        make_sentinels(self.tree.home)
        run(
            [
                RELEASE_TOOL,
                "bootstrap",
                *self.tree.activation_args(self.sha1, self.backup),
            ]
        )
        marker = self.tree.state_dir.parent / "integration-ready-v1"
        marker.write_text(
            f"session-kit-integration-v1 {self.sha1}\n", encoding="utf-8"
        )
        marker.chmod(0o600)
        receipt = self.tree.state_dir / "last-activation.json"
        args = release_module.parser().parse_args(
            [
                "activate",
                *[os.fspath(value) for value in self.tree.activation_args(sha2, self.backup)],
            ]
        )

        class InjectedCrash(BaseException):
            pass

        for checkpoint in ("marker-invalidated", "current", "marker", "receipt"):
            with self.subTest(checkpoint=checkpoint):
                before = {
                    path: entry_evidence(path)
                    for path in (self.tree.root / "current", marker, receipt)
                }

                def crash(label: str) -> None:
                    if label == checkpoint:
                        raise InjectedCrash(label)

                with mock.patch.object(
                    release_module, "activation_checkpoint", side_effect=crash
                ):
                    with self.assertRaises(InjectedCrash):
                        release_module.activate(args)
                self.assertEqual(
                    before,
                    {
                        path: entry_evidence(path)
                        for path in (self.tree.root / "current", marker, receipt)
                    },
                )
                self.assertEqual(release1, (self.tree.root / "current").resolve())

        release_module.activate(args)
        self.assertEqual(release2, (self.tree.root / "current").resolve())
        self.assertEqual(
            f"session-kit-integration-v1 {sha2}\n", marker.read_text()
        )
        self.assertEqual(0o600, stat.S_IMODE(marker.stat().st_mode))

    def test_incomplete_activation_recovers_before_target_preconditions(self) -> None:
        release1 = self.tree.build(self.repo, self.sha1)
        sha2 = commit_runtime(self.repo, "v2")
        self.tree.build(self.repo, sha2)
        make_sentinels(self.tree.home)
        run(
            [
                RELEASE_TOOL,
                "bootstrap",
                *self.tree.activation_args(self.sha1, self.backup),
            ]
        )
        marker = self.tree.state_dir.parent / "integration-ready-v1"
        marker.write_text(
            f"session-kit-integration-v1 {self.sha1}\n", encoding="utf-8"
        )
        marker.chmod(0o600)
        args = release_module.parser().parse_args(
            [
                "activate",
                *[os.fspath(value) for value in self.tree.activation_args(sha2, self.backup)],
            ]
        )
        paths = release_module.activation_transaction_paths(args)
        before = {path: entry_evidence(path) for path in paths}

        def crash_after_current(label: str) -> None:
            if label == "current":
                raise KeyboardInterrupt("simulated process death")

        with mock.patch.object(
            release_module, "activation_checkpoint", side_effect=crash_after_current
        ), mock.patch.object(
            release_module,
            "restore_entries",
            side_effect=OSError("simulated death before rollback"),
        ):
            with self.assertRaises(release_module.ReleaseError):
                release_module.activate(args)
        journal = self.tree.state_dir / "activation-transaction.json"
        self.assertTrue(journal.is_file())
        self.assertNotEqual(before, {path: entry_evidence(path) for path in paths})

        (self.tree.home / ".no_shpool").unlink()
        assert args.integration_proof is not None
        args.integration_proof.unlink()
        with self.assertRaisesRegex(release_module.ReleaseError, "missing safety sentinels"):
            release_module.activate(args)
        self.assertFalse(journal.exists())
        self.assertEqual(before, {path: entry_evidence(path) for path in paths})
        self.assertEqual(release1, (self.tree.root / "current").resolve())

    def test_incomplete_activation_divergence_retains_journal(self) -> None:
        self.tree.build(self.repo, self.sha1)
        sha2 = commit_runtime(self.repo, "v2")
        release2 = self.tree.build(self.repo, sha2)
        make_sentinels(self.tree.home)
        run(
            [
                RELEASE_TOOL,
                "bootstrap",
                *self.tree.activation_args(self.sha1, self.backup),
            ]
        )
        args = release_module.parser().parse_args(
            [
                "activate",
                *[os.fspath(value) for value in self.tree.activation_args(sha2, self.backup)],
            ]
        )

        def crash_after_current(label: str) -> None:
            if label == "current":
                raise KeyboardInterrupt("simulated process death")

        with mock.patch.object(
            release_module, "activation_checkpoint", side_effect=crash_after_current
        ), mock.patch.object(
            release_module,
            "restore_entries",
            side_effect=OSError("simulated death before rollback"),
        ):
            with self.assertRaises(release_module.ReleaseError):
                release_module.activate(args)

        journal = self.tree.state_dir / "activation-transaction.json"
        receipt = self.tree.state_dir / "last-activation.json"
        receipt.write_text('{"unrelated":true}\n', encoding="utf-8")
        receipt.chmod(0o600)
        with self.assertRaisesRegex(release_module.ReleaseError, "diverged"):
            release_module.recover_activation_transaction(args)
        self.assertTrue(journal.is_file())
        self.assertEqual(release2, (self.tree.root / "current").resolve())
        self.assertEqual('{"unrelated":true}\n', receipt.read_text())

    def test_committed_rollback_journal_is_finished_not_rolled_back(self) -> None:
        release1 = self.tree.build(self.repo, self.sha1)
        sha2 = commit_runtime(self.repo, "v2")
        release2 = self.tree.build(self.repo, sha2)
        make_sentinels(self.tree.home)
        run(
            [
                RELEASE_TOOL,
                "bootstrap",
                *self.tree.activation_args(self.sha1, self.backup),
            ]
        )
        run(
            [
                RELEASE_TOOL,
                "activate",
                *self.tree.activation_args(sha2, self.backup),
            ]
        )
        args = release_module.parser().parse_args(
            [
                "rollback",
                *[os.fspath(value) for value in self.tree.activation_args(self.sha1, self.backup)],
            ]
        )
        with mock.patch.object(release_module, "clear_activation_journal"):
            release_module.activate(args, action="rollback")
        journal = self.tree.state_dir / "activation-transaction.json"
        payload = json.loads(journal.read_text())
        self.assertEqual("committed", payload["phase"])
        self.assertEqual("rollback", payload["action"])
        self.assertEqual(release1, (self.tree.root / "current").resolve())

        self.assertEqual(
            "finished-committed",
            release_module.recover_activation_transaction(args),
        )
        self.assertFalse(journal.exists())
        self.assertEqual(release1, (self.tree.root / "current").resolve())
        self.assertNotEqual(release2, (self.tree.root / "current").resolve())

    def test_committed_activation_corruption_retains_journal_without_rollback(self) -> None:
        release1 = self.tree.build(self.repo, self.sha1)
        sha2 = commit_runtime(self.repo, "v2")
        release2 = self.tree.build(self.repo, sha2)
        make_sentinels(self.tree.home)
        run(
            [
                RELEASE_TOOL,
                "bootstrap",
                *self.tree.activation_args(self.sha1, self.backup),
            ]
        )
        args = release_module.parser().parse_args(
            [
                "activate",
                *[os.fspath(value) for value in self.tree.activation_args(sha2, self.backup)],
            ]
        )
        with mock.patch.object(release_module, "clear_activation_journal"):
            release_module.activate(args)
        journal = self.tree.state_dir / "activation-transaction.json"
        marker = self.tree.state_dir.parent / "integration-ready-v1"
        payload = json.loads(journal.read_text())
        self.assertEqual("committed", payload["phase"])
        self.assertEqual(
            json.loads(
                (self.tree.state_dir / "last-activation.json").read_text()
            ),
            payload["postimage"]["receipt"],
        )

        marker.write_text("corrupt committed marker\n", encoding="utf-8")
        marker.chmod(0o600)
        with self.assertRaisesRegex(
            release_module.ReleaseError, "poststate is inconsistent"
        ):
            release_module.recover_activation_transaction(args)
        self.assertTrue(journal.is_file())
        self.assertEqual(release2, (self.tree.root / "current").resolve())
        self.assertNotEqual(release1, (self.tree.root / "current").resolve())
        self.assertEqual("corrupt committed marker\n", marker.read_text())

    def test_activation_journal_rejects_wrong_mode_and_extra_schema(self) -> None:
        self.tree.build(self.repo, self.sha1)
        sha2 = commit_runtime(self.repo, "v2")
        release2 = self.tree.build(self.repo, sha2)
        make_sentinels(self.tree.home)
        run(
            [
                RELEASE_TOOL,
                "bootstrap",
                *self.tree.activation_args(self.sha1, self.backup),
            ]
        )
        args = release_module.parser().parse_args(
            [
                "activate",
                *[os.fspath(value) for value in self.tree.activation_args(sha2, self.backup)],
            ]
        )
        with mock.patch.object(release_module, "clear_activation_journal"):
            release_module.activate(args)
        journal = self.tree.state_dir / "activation-transaction.json"
        journal.chmod(0o644)
        with self.assertRaisesRegex(release_module.ReleaseError, "mode or owner"):
            release_module.recover_activation_transaction(args)
        self.assertTrue(journal.is_file())
        self.assertEqual(release2, (self.tree.root / "current").resolve())

        payload = json.loads(journal.read_text())
        payload["unexpected"] = True
        journal.write_text(json.dumps(payload), encoding="utf-8")
        journal.chmod(0o600)
        with self.assertRaisesRegex(release_module.ReleaseError, "journal schema"):
            release_module.recover_activation_transaction(args)
        self.assertTrue(journal.is_file())
        self.assertEqual(release2, (self.tree.root / "current").resolve())

    def test_activation_refuses_missing_or_wrong_target_integration_proof(self) -> None:
        self.tree.build(self.repo, self.sha1)
        make_sentinels(self.tree.home)
        argv = [
            "activate",
            *[os.fspath(value) for value in self.tree.activation_args(self.sha1, self.backup)],
            "--allow-unbootstrapped",
        ]
        args = release_module.parser().parse_args(argv)
        assert args.integration_proof is not None
        args.integration_proof.unlink()
        with self.assertRaisesRegex(release_module.ReleaseError, "integration proof"):
            release_module.activate(args)
        args.integration_proof.write_text(
            f"session-kit-integration-v1 {'b' * 40}\n", encoding="utf-8"
        )
        args.integration_proof.chmod(0o600)
        with self.assertRaisesRegex(release_module.ReleaseError, "does not match"):
            release_module.activate(args)
        args.integration_proof.write_text(
            f"session-kit-integration-v1 {self.sha1}\n", encoding="utf-8"
        )
        args.integration_proof.chmod(0o644)
        with self.assertRaisesRegex(release_module.ReleaseError, "mode 0600"):
            release_module.activate(args)

    def test_backup_restore_rehearsal_rejects_duplicate_and_unsafe_members(self) -> None:
        expected, metadata = release_module.installed_source_evidence(self.backup)
        digests = release_module.rehearse_installed_restore(
            self.backup / "installed-files.tar", expected, metadata
        )
        self.assertEqual(set(expected), set(digests))

        target = next(name for name in expected if name.endswith("/.local/bin/sp"))
        for member_name in (
            target,
            f"/{target}",
        ):
            with self.subTest(member_name=member_name):
                archive_path = self.tree.base / f"bad-{len(member_name)}.tar"
                with tarfile.open(archive_path, mode="w") as archive:
                    info = tarfile.TarInfo(member_name)
                    content = b"bad"
                    info.size = len(content)
                    info.mode = 0o755
                    archive.addfile(info, io.BytesIO(content))
                    if not member_name.startswith("/"):
                        archive.addfile(info, io.BytesIO(content))
                with self.assertRaises(release_module.ReleaseError):
                    release_module.rehearse_installed_restore(
                        archive_path, expected, metadata
                    )

    def test_backup_restore_rehearsal_uses_independent_digest_and_metadata(self) -> None:
        expected, metadata = release_module.installed_source_evidence(self.backup)
        target = next(name for name in expected if name.endswith("/.local/bin/sp"))
        wrong_digest = dict(expected)
        wrong_digest[target] = "0" * 64
        with self.assertRaisesRegex(
            release_module.ReleaseError, "byte-for-byte"
        ):
            release_module.rehearse_installed_restore(
                self.backup / "installed-files.tar", wrong_digest, metadata
            )
        wrong_metadata = {name: dict(values) for name, values in metadata.items()}
        wrong_metadata[target]["mode"] = 0o700
        with self.assertRaisesRegex(release_module.ReleaseError, "mode mismatch"):
            release_module.rehearse_installed_restore(
                self.backup / "installed-files.tar", expected, wrong_metadata
            )

    def test_backup_rollback_targets_accept_an_arbitrary_safe_home(self) -> None:
        source_home = "home/sessionuser"
        alternate_home = "srv/users/sessionuser"
        rewrite_backup_home(self.backup, source_home, alternate_home)

        marker = release_module.verify_backup_bundle(self.backup)
        expected, metadata = release_module.installed_source_evidence(self.backup)
        digests = release_module.rehearse_installed_restore(
            self.backup / "installed-files.tar", expected, metadata
        )

        self.assertTrue(marker["complete"])
        self.assertEqual(set(expected), set(digests))
        self.assertTrue(
            all(name.startswith(f"{alternate_home}/") for name in digests)
        )
        self.assertNotIn(source_home, "\n".join(digests))

    def test_backup_rollback_targets_reject_multiple_homes(self) -> None:
        expected, _ = release_module.installed_source_evidence(self.backup)
        mixed = set(expected)
        target = next(name for name in mixed if name.endswith("/.local/bin/sp"))
        mixed.remove(target)
        mixed.add(target.replace("home/sessionuser/", "srv/users/otheruser/"))

        with self.assertRaisesRegex(
            release_module.ReleaseError,
            "one exact home",
        ):
            release_module.required_installed_targets(mixed)

    def test_backup_rollback_targets_reject_broad_or_unexpected_suffix(self) -> None:
        expected, _ = release_module.installed_source_evidence(self.backup)
        malformed = set(expected)
        target = next(name for name in malformed if name.endswith("/.local/bin/sp"))
        malformed.remove(target)
        malformed.add("sp")

        with self.assertRaisesRegex(
            release_module.ReleaseError,
            "unexpected suffix",
        ):
            release_module.required_installed_targets(malformed)

    def test_activation_receipt_failure_restores_prior_current_pointer(self) -> None:
        release1 = self.tree.build(self.repo, self.sha1)
        sha2 = commit_runtime(self.repo, "v2")
        self.tree.build(self.repo, sha2)
        make_sentinels(self.tree.home)
        run(
            [
                RELEASE_TOOL,
                "bootstrap",
                *self.tree.activation_args(self.sha1, self.backup),
            ]
        )
        original_target = os.readlink(self.tree.root / "current")
        args = release_module.parser().parse_args(
            [
                "activate",
                *[os.fspath(value) for value in self.tree.activation_args(sha2, self.backup)],
            ]
        )
        real_atomic_json = release_module.atomic_json

        def fail_receipt(path: Path, payload: dict, mode: int = 0o600) -> None:
            if path == self.tree.state_dir / "last-activation.json":
                raise OSError("injected receipt failure")
            real_atomic_json(path, payload, mode)

        with mock.patch.object(
            release_module, "atomic_json", side_effect=fail_receipt
        ):
            with self.assertRaises(OSError):
                release_module.activate(args)
        self.assertEqual(original_target, os.readlink(self.tree.root / "current"))
        self.assertEqual(release1, (self.tree.root / "current").resolve())
        self.assertFalse(
            (self.tree.state_dir / "activation-transaction.json").exists()
        )

    def test_activation_requires_sentinels_and_bootstrap(self) -> None:
        self.tree.build(self.repo, self.sha1)
        missing = run(
            [
                RELEASE_TOOL,
                "activate",
                *self.tree.activation_args(self.sha1, self.backup),
                "--allow-unbootstrapped",
            ],
            check=False,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("missing safety sentinels", missing.stderr)

        make_sentinels(self.tree.home)
        no_bootstrap = run(
            [RELEASE_TOOL, "activate", *self.tree.activation_args(self.sha1, self.backup)],
            check=False,
        )
        self.assertNotEqual(no_bootstrap.returncode, 0)
        self.assertIn("not bootstrapped", no_bootstrap.stderr)

    def test_activation_rejects_pathlike_or_optionlike_commit(self) -> None:
        for bad_commit in ("../" + self.sha1, "--help"):
            with self.subTest(commit=bad_commit):
                proc = run(
                    [
                        RELEASE_TOOL,
                        "activate",
                        *self.tree.activation_args(bad_commit, self.backup),
                        "--allow-unbootstrapped",
                    ],
                    check=False,
                )
                self.assertNotEqual(0, proc.returncode)
                self.assertTrue(
                    "full lowercase commit ID" in proc.stderr
                    or "expected one argument" in proc.stderr
                )

    def test_lock_refuses_concurrent_activation(self) -> None:
        self.tree.build(self.repo, self.sha1)
        make_sentinels(self.tree.home)
        self.tree.state_dir.mkdir(parents=True)
        lock_path = self.tree.state_dir / "deploy.lock"
        with lock_path.open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            proc = run(
                [
                    RELEASE_TOOL,
                    "activate",
                    *self.tree.activation_args(self.sha1, self.backup),
                    "--allow-unbootstrapped",
                ],
                check=False,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("another deployment holds", proc.stderr)
        self.assertFalse((self.tree.root / "current").exists())

    def test_current_link_cannot_escape_release_root(self) -> None:
        self.tree.build(self.repo, self.sha1)
        make_sentinels(self.tree.home)
        self.tree.root.mkdir(parents=True, exist_ok=True)
        os.symlink("/", self.tree.root / "current")
        proc = run(
            [
                RELEASE_TOOL,
                "activate",
                *self.tree.activation_args(self.sha1, self.backup),
                "--allow-unbootstrapped",
            ],
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("current release link is invalid", proc.stderr)

    def test_deploy_code_has_no_live_session_or_service_operation(self) -> None:
        source = RELEASE_TOOL.read_text(encoding="utf-8")
        launcher = (REPO / "deploy/session-kit-launcher").read_text(encoding="utf-8")
        forbidden = (
            "systemctl",
            "launchctl",
            "brew services",
            "shpool kill",
            "shpool detach",
            "shpool daemon",
        )
        for phrase in forbidden:
            self.assertNotIn(phrase, source)
            self.assertNotIn(phrase, launcher)


if __name__ == "__main__":
    unittest.main()
