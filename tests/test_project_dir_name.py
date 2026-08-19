"""The short project-directory name is proved, never guessed.

Every refusal path prints nothing: an old Claude, an unregistered directory,
an ambiguous registry, a live session, or a name collision all leave the
launch on today's munged-name behaviour. The one mutating step, renaming the
legacy directory, happens only with every proof in hand, because Claude
reads auto memory solely from the resolved name and a half-migrated project
is a session with amnesia.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from lib.sessionkit_inventory import accounts, project_dir_name


def _executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


class Fixture:
    def __init__(self) -> None:
        # The module under test honors SESSION_KIT_PROJECT_DIR_NAME=off as a
        # kill switch. A shell that exports it -- flipped once for debugging
        # and forgotten -- turns every rename in this module into a refusal
        # and fails four tests for a reason that is not a defect (that is
        # exactly how it was first reported, as a suspected regression, on
        # 2026-08-19). Every test therefore starts from the variable unset;
        # the kill-switch test sets it back deliberately through its own
        # patch.
        self._environment = mock.patch.dict(os.environ, {}, clear=False)
        self._environment.start()
        os.environ.pop("SESSION_KIT_PROJECT_DIR_NAME", None)
        self.temp = tempfile.TemporaryDirectory(prefix=".projects-dirname-")
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.profile = self.base / "profile"
        (self.profile / "projects").mkdir(parents=True)
        self.projects_file = self.base / "projects.tsv"
        self.projects_file.write_text(f"sl\tclaude\t{self.root}\n", encoding="utf-8")
        # A launcher new enough for the variable, resolved through the same
        # immutable-release layout the real installer creates.
        versions = self.base / "share" / "versions"
        versions.mkdir(parents=True)
        _executable(versions / "2.1.234")
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        (bin_dir / "claude").symlink_to(versions / "2.1.234")
        self.claude = str(bin_dir / "claude")

    @property
    def munged(self) -> Path:
        munged = project_dir_name.MUNGE_RE.sub("-", os.fspath(self.root.resolve()))
        return self.profile / "projects" / munged

    def close(self) -> None:
        self.temp.cleanup()
        self._environment.stop()

    def resolve(self) -> str | None:
        return project_dir_name.resolve_project_dir_name(
            self.profile, self.root, self.projects_file, self.claude
        )


class VersionGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)

    def test_release_layout_new_enough_passes(self) -> None:
        self.assertTrue(
            project_dir_name.claude_supports_project_dir_name(self.fixture.claude)
        )

    def test_older_release_refuses(self) -> None:
        versions = self.fixture.base / "share" / "versions"
        _executable(versions / "2.1.233")
        launcher = Path(self.fixture.claude)
        launcher.unlink()
        launcher.symlink_to(versions / "2.1.233")
        self.assertFalse(
            project_dir_name.claude_supports_project_dir_name(self.fixture.claude)
        )
        self.assertIsNone(self.fixture.resolve())

    def test_unrecognised_layout_refuses(self) -> None:
        launcher = Path(self.fixture.claude)
        launcher.unlink()
        _executable(launcher)
        self.assertFalse(
            project_dir_name.claude_supports_project_dir_name(self.fixture.claude)
        )

    def test_missing_command_refuses(self) -> None:
        self.assertFalse(
            project_dir_name.claude_supports_project_dir_name(
                str(self.fixture.base / "absent")
            )
        )


class AliasTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)

    def test_registered_root_names_its_alias(self) -> None:
        self.assertEqual(
            project_dir_name.alias_for_root(
                self.fixture.projects_file, self.fixture.root
            ),
            "sl",
        )

    def test_unregistered_directory_refuses(self) -> None:
        elsewhere = self.fixture.base / "elsewhere"
        elsewhere.mkdir()
        self.assertIsNone(
            project_dir_name.alias_for_root(self.fixture.projects_file, elsewhere)
        )

    def test_two_rows_for_one_root_refuse(self) -> None:
        self.fixture.projects_file.write_text(
            f"sl\tclaude\t{self.fixture.root}\nalso\tcodex\t{self.fixture.root}\n",
            encoding="utf-8",
        )
        self.assertIsNone(
            project_dir_name.alias_for_root(
                self.fixture.projects_file, self.fixture.root
            )
        )

    def test_reserved_device_name_refuses(self) -> None:
        self.fixture.projects_file.write_text(
            f"com1\tclaude\t{self.fixture.root}\n", encoding="utf-8"
        )
        self.assertIsNone(
            project_dir_name.alias_for_root(
                self.fixture.projects_file, self.fixture.root
            )
        )

    def test_subdirectory_of_a_root_refuses(self) -> None:
        inside = self.fixture.root / "deeper"
        inside.mkdir()
        self.assertIsNone(
            project_dir_name.alias_for_root(self.fixture.projects_file, inside)
        )


class LivenessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.proc = self.fixture.base / "proc"
        self.proc.mkdir()

    def _process(
        self, pid: int, profile: Path, cwd: Path, comm: str | None = None
    ) -> None:
        entry = self.proc / str(pid)
        entry.mkdir()
        (entry / "environ").write_bytes(
            b"PATH=/usr/bin\0CLAUDE_CONFIG_DIR="
            + os.fsencode(os.fspath(profile))
            + b"\0"
        )
        (entry / "cwd").symlink_to(cwd)
        if comm is not None:
            (entry / "comm").write_text(comm + "\n", encoding="ascii")

    def test_profile_process_inside_root_is_live(self) -> None:
        self._process(4242, self.fixture.profile, self.fixture.root)
        self.assertTrue(
            project_dir_name.profile_live_in_root(
                self.fixture.profile, self.fixture.root.resolve(), self.proc
            )
        )

    def test_other_profile_or_other_directory_is_not(self) -> None:
        elsewhere = self.fixture.base / "elsewhere"
        elsewhere.mkdir()
        self._process(4242, self.fixture.profile, elsewhere)
        self._process(4243, self.fixture.base / "other-profile", self.fixture.root)
        self.assertFalse(
            project_dir_name.profile_live_in_root(
                self.fixture.profile, self.fixture.root.resolve(), self.proc
            )
        )

    def test_own_shell_ancestors_never_count(self) -> None:
        own = next(iter(project_dir_name._ancestor_pids()))
        self._process(own, self.fixture.profile, self.fixture.root, comm="bash")
        self.assertFalse(
            project_dir_name.profile_live_in_root(
                self.fixture.profile, self.fixture.root.resolve(), self.proc
            )
        )

    def test_a_provider_ancestor_in_the_root_is_live(self) -> None:
        """A session inside a session: renaming under it splits ITS store.

        Review lane rv-pdn-1 (2026-08-17) reproduced the split by placing a
        same-profile provider in the excluded ancestor chain. Only shells and
        launch plumbing are excused now; a provider above us counts as live.
        """
        own = next(iter(project_dir_name._ancestor_pids()))
        self._process(own, self.fixture.profile, self.fixture.root, comm="claude")
        self.assertTrue(
            project_dir_name.profile_live_in_root(
                self.fixture.profile, self.fixture.root.resolve(), self.proc
            )
        )

    def test_an_ancestor_without_a_readable_comm_is_live(self) -> None:
        own = next(iter(project_dir_name._ancestor_pids()))
        self._process(own, self.fixture.profile, self.fixture.root)
        self.assertTrue(
            project_dir_name.profile_live_in_root(
                self.fixture.profile, self.fixture.root.resolve(), self.proc
            )
        )

    def test_unreadable_cwd_counts_as_live(self) -> None:
        entry = self.proc / "4242"
        entry.mkdir()
        (entry / "environ").write_bytes(
            b"CLAUDE_CONFIG_DIR=" + os.fsencode(os.fspath(self.fixture.profile)) + b"\0"
        )
        self.assertTrue(
            project_dir_name.profile_live_in_root(
                self.fixture.profile, self.fixture.root.resolve(), self.proc
            )
        )


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        quiet = mock.patch.object(
            project_dir_name, "profile_live_in_root", return_value=False
        )
        self.live = quiet.start()
        self.addCleanup(quiet.stop)

    def test_fresh_project_exports_without_renaming(self) -> None:
        self.assertEqual(self.fixture.resolve(), "sl")
        self.assertFalse(self.fixture.munged.exists())

    def test_legacy_directory_is_renamed_with_its_contents(self) -> None:
        legacy = self.fixture.munged
        (legacy / "memory").mkdir(parents=True)
        (legacy / "memory" / "MEMORY.md").write_text("kept\n", encoding="utf-8")
        self.assertEqual(self.fixture.resolve(), "sl")
        renamed = self.fixture.profile / "projects" / "sl"
        self.assertFalse(legacy.exists())
        self.assertEqual(
            (renamed / "memory" / "MEMORY.md").read_text(encoding="utf-8"), "kept\n"
        )

    def test_live_session_defers_everything(self) -> None:
        self.live.return_value = True
        self.fixture.munged.mkdir(parents=True)
        self.assertIsNone(self.fixture.resolve())
        self.assertTrue(self.fixture.munged.is_dir())

    def test_both_names_existing_refuse(self) -> None:
        self.fixture.munged.mkdir(parents=True)
        (self.fixture.profile / "projects" / "sl").mkdir()
        self.assertIsNone(self.fixture.resolve())
        self.assertTrue(self.fixture.munged.is_dir())

    def test_kill_switch_refuses(self) -> None:
        with mock.patch.dict(os.environ, {"SESSION_KIT_PROJECT_DIR_NAME": "off"}):
            self.assertIsNone(self.fixture.resolve())

    def test_a_failed_rename_refuses_instead_of_exporting(self) -> None:
        self.fixture.munged.mkdir(parents=True)
        with mock.patch.object(Path, "rename", side_effect=OSError):
            self.assertIsNone(self.fixture.resolve())

    def test_a_raw_launch_during_the_rename_is_undone(self) -> None:
        """The scan re-runs on the far side of the rename, and a hit undoes it.

        A raw provider start takes no part in the migration lock, so it can
        begin between the quiet scan and the rename (review lane rv-pdn-1).
        The post-rename scan finds it, the move is undone, and the raw
        session finds the world exactly as it was.
        """
        (self.fixture.munged / "memory").mkdir(parents=True)
        self.live.side_effect = [False, True]
        self.assertIsNone(self.fixture.resolve())
        self.assertTrue((self.fixture.munged / "memory").is_dir())
        self.assertFalse((self.fixture.profile / "projects" / "sl").exists())

    def test_a_failed_undo_leaves_the_visible_both_names_state(self) -> None:
        (self.fixture.munged / "memory").mkdir(parents=True)
        self.live.side_effect = [False, True]
        original = Path.rename
        calls = {"count": 0}

        def rename_once(target: Path, destination: Path) -> None:
            calls["count"] += 1
            if calls["count"] > 1:
                raise OSError("undo refused")
            original(target, destination)

        with mock.patch.object(Path, "rename", rename_once):
            self.assertIsNone(self.fixture.resolve())
        # The forward move happened and the undo could not: both-names is the
        # state the next launch refuses on and doctor reports.
        self.assertTrue((self.fixture.profile / "projects" / "sl").is_dir())
        self.assertFalse(self.fixture.munged.exists())

    def test_an_alias_path_that_is_a_regular_file_refuses(self) -> None:
        (self.fixture.profile / "projects").mkdir(parents=True, exist_ok=True)
        (self.fixture.profile / "projects" / "sl").write_text(
            "not a directory\n", encoding="utf-8"
        )
        self.assertIsNone(self.fixture.resolve())
        self.fixture.munged.mkdir(parents=True)
        self.assertIsNone(self.fixture.resolve())
        self.assertTrue(self.fixture.munged.is_dir())

    def test_simultaneous_launches_migrate_once_and_agree(self) -> None:
        """The rename loser re-reads the winner's world instead of splitting it."""
        (self.fixture.munged / "memory").mkdir(parents=True)
        barrier = threading.Barrier(2)
        results: list[str | None] = []

        def launch() -> None:
            barrier.wait()
            results.append(self.fixture.resolve())

        threads = [threading.Thread(target=launch) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, ["sl", "sl"])
        self.assertFalse(self.fixture.munged.exists())
        self.assertTrue((self.fixture.profile / "projects" / "sl" / "memory").is_dir())


class DoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.accounts_root = self.fixture.base / "accounts"
        self.profile = self.accounts_root / "duck"
        (self.profile / "projects").mkdir(parents=True)
        (self.profile / "settings.json").write_text(
            json.dumps({"autoContinueAtUsageLimit": False}), encoding="utf-8"
        )

    def report(self) -> tuple[str, str]:
        return project_dir_name.doctor_report(
            self.fixture.projects_file, self.accounts_root
        )

    def test_clean_estate_is_ok(self) -> None:
        status, detail = self.report()
        self.assertEqual(status, "ok")
        self.assertIn("1 named projects", detail)

    def test_pending_rename_stays_ok_and_is_counted(self) -> None:
        munged = project_dir_name.MUNGE_RE.sub(
            "-", os.fspath(self.fixture.root.resolve())
        )
        (self.profile / "projects" / munged).mkdir()
        status, detail = self.report()
        self.assertEqual(status, "ok")
        self.assertIn("1 awaiting", detail)

    def test_both_names_warn(self) -> None:
        munged = project_dir_name.MUNGE_RE.sub(
            "-", os.fspath(self.fixture.root.resolve())
        )
        (self.profile / "projects" / munged).mkdir()
        (self.profile / "projects" / "sl").mkdir()
        status, detail = self.report()
        self.assertEqual(status, "warn")
        self.assertIn("both names", detail)

    def test_duplicate_registration_warns(self) -> None:
        self.fixture.projects_file.write_text(
            f"sl\tclaude\t{self.fixture.root}\nalso\tcodex\t{self.fixture.root}\n",
            encoding="utf-8",
        )
        status, detail = self.report()
        self.assertEqual(status, "warn")
        self.assertIn("registered as both", detail)

    def test_missing_auto_continue_key_warns(self) -> None:
        (self.profile / "settings.json").write_text("{}", encoding="utf-8")
        status, detail = self.report()
        self.assertEqual(status, "warn")
        self.assertIn("autoContinueAtUsageLimit", detail)


class EnrollmentSettingsTest(unittest.TestCase):
    def test_new_profiles_start_with_auto_continue_off(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".projects-dirname-") as raw:
            base = Path(raw)
            source = base / "source"
            source.mkdir()
            (source / "settings.json").write_text(
                json.dumps({"model": "opus"}), encoding="utf-8"
            )
            profile = base / "profile"
            with mock.patch.object(
                accounts, "_default_profile_dir", return_value=source
            ):
                accounts.sync_profile_configuration("claude", profile)
            written = json.loads(
                (profile / "settings.json").read_text(encoding="utf-8")
            )
            self.assertIs(written["autoContinueAtUsageLimit"], False)
            self.assertEqual(written["model"], "opus")

    def test_a_source_without_settings_still_gets_the_switch(self) -> None:
        """The default is a promise about every profile, not a copy transform.

        Review lane rv-pdn-2 (2026-08-17): a source profile with no
        settings.json enrolled a profile without the file, so a limited
        session could resume itself when its usage window reset.
        """
        with tempfile.TemporaryDirectory(prefix=".projects-dirname-") as raw:
            base = Path(raw)
            source = base / "source"
            source.mkdir()
            profile = base / "profile"
            with mock.patch.object(
                accounts, "_default_profile_dir", return_value=source
            ):
                accounts.sync_profile_configuration("claude", profile)
            written = json.loads(
                (profile / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual({"autoContinueAtUsageLimit": False}, written)


if __name__ == "__main__":
    unittest.main()
