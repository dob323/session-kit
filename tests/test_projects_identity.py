"""One project identity across the two senses the kit grew separately.

These tests pin the membership rule: a directory belongs to the deepest
project root above it, and the trust rule that keeps a cloned repository
from choosing what this host launches.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from lib.sessionkit_projects import identity


class ResolverFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-identity.")
        self.addCleanup(self.temporary.cleanup)
        # A macOS temporary directory is itself a symlink; resolving here keeps
        # the fixture's own paths comparable with the canonical ones under test.
        self.root = Path(self.temporary.name).resolve()
        self.config = self.root / "config"
        self.config.mkdir()
        self.projects_file = self.config / "projects.tsv"

    def directory(self, relative: str) -> Path:
        path = self.root / relative
        path.mkdir(parents=True, exist_ok=True)
        return path

    def manifest(self, relative: str, text: str) -> Path:
        path = self.directory(relative) / identity.MANIFEST_NAME
        path.write_text(text, encoding="utf-8")
        return path

    def shortcuts(self, *rows: tuple[str, str, str]) -> None:
        self.projects_file.write_text(
            "".join(f"{alias}\t{kind}\t{cwd}\n" for alias, kind, cwd in rows),
            encoding="utf-8",
        )

    def resolver(self) -> identity.Resolver:
        return identity.Resolver(self.projects_file, environ={})


class ResolutionTests(ResolverFixture):
    def test_a_subdirectory_resolves_to_the_manifest_above_it(self) -> None:
        self.manifest("repo", 'name = "demo"\n')
        deep = self.directory("repo/a/b/c")
        project = self.resolver().resolve(deep)
        assert project is not None
        self.assertEqual(project.root, os.fspath(self.root / "repo"))
        self.assertEqual(project.name, "demo")
        self.assertEqual(project.source, identity.SOURCE_MANIFEST)

    def test_the_deepest_manifest_wins_over_one_further_up(self) -> None:
        self.manifest("outer", 'name = "outer"\n')
        self.manifest("outer/inner", 'name = "inner"\n')
        project = self.resolver().resolve(self.directory("outer/inner/work"))
        assert project is not None
        self.assertEqual(project.name, "inner")
        self.assertEqual(project.root, os.fspath(self.root / "outer" / "inner"))

    def test_without_a_manifest_the_deepest_shortcut_row_wins(self) -> None:
        outer, inner = self.directory("outer"), self.directory("outer/inner")
        self.shortcuts(
            ("outer", "claude", os.fspath(outer)),
            ("inner", "codex", os.fspath(inner)),
        )
        project = self.resolver().resolve(self.directory("outer/inner/work"))
        assert project is not None
        self.assertEqual(project.root, os.fspath(inner))
        self.assertEqual(project.alias, "inner")
        self.assertEqual(project.source, identity.SOURCE_SHORTCUT)
        self.assertEqual(project.shortcut_provider, "codex")

    def test_a_manifest_wins_over_a_shortcut_further_up(self) -> None:
        self.shortcuts(("outer", "claude", os.fspath(self.directory("outer"))))
        self.manifest("outer/inner", 'name = "inner"\n')
        project = self.resolver().resolve(self.directory("outer/inner"))
        assert project is not None
        self.assertEqual(project.name, "inner")

    def test_a_directory_in_no_project_resolves_to_nothing(self) -> None:
        self.assertIsNone(self.resolver().resolve(self.directory("loose")))

    def test_a_sibling_with_a_shared_prefix_is_not_inside_the_project(self) -> None:
        self.shortcuts(("app", "claude", os.fspath(self.directory("srv/app"))))
        self.assertIsNone(self.resolver().resolve(self.directory("srv/application")))

    def test_an_ignore_row_is_never_a_project(self) -> None:
        ignored = self.directory("scratch")
        self.shortcuts(("scratch", "ignore", os.fspath(ignored)))
        resolver = self.resolver()
        self.assertIsNone(resolver.resolve(ignored))
        self.assertIsNone(resolver.resolve_alias("scratch"))
        self.assertTrue(resolver.is_ignored(os.fspath(ignored)))
        self.assertEqual(resolver.projects(), [])

    def test_an_ignored_directory_still_resolves_through_its_manifest(self) -> None:
        # Ignoring a directory keeps it out of the shortcut list. It does not
        # make the sessions running there unplaceable.
        ignored = self.directory("repo")
        self.manifest("repo", 'name = "demo"\n')
        self.shortcuts(("repo", "ignore", os.fspath(ignored)))
        project = self.resolver().resolve(ignored)
        assert project is not None
        self.assertEqual(project.name, "demo")
        self.assertFalse(project.trusted)

    def test_an_alias_resolves_with_its_manifest(self) -> None:
        repo = self.directory("repo")
        self.manifest("repo", 'name = "demo"\nmodel = "claude-opus-5"\n')
        self.shortcuts(("demo", "claude", os.fspath(repo)))
        project = self.resolver().resolve_alias("demo")
        assert project is not None
        self.assertEqual(project.alias, "demo")
        assert project.manifest is not None
        self.assertEqual(project.manifest["model"], "claude-opus-5")
        self.assertTrue(project.trusted)

    def test_an_unknown_alias_resolves_to_nothing(self) -> None:
        self.assertIsNone(self.resolver().resolve_alias("absent"))

    def test_a_manifest_root_may_point_at_a_subdirectory_of_itself(self) -> None:
        self.directory("repo/src")
        self.manifest("repo", 'name = "demo"\nroot = "src"\n')
        project = self.resolver().resolve(self.directory("repo/src/deep"))
        assert project is not None
        self.assertEqual(project.root, os.fspath(self.root / "repo" / "src"))

    def test_the_display_name_falls_back_from_manifest_to_alias_to_folder(self) -> None:
        self.manifest("named", 'name = "chosen"\n')
        aliased = self.directory("aliased")
        plain = self.directory("plain")
        self.manifest("plain", "# no name\n")
        self.shortcuts(
            ("shortcut-name", "claude", os.fspath(aliased)),
            ("plain-alias", "claude", os.fspath(plain)),
        )
        resolver = self.resolver()
        named = resolver.resolve(self.root / "named")
        by_alias = resolver.resolve(aliased)
        by_manifest_without_name = resolver.resolve(plain)
        assert named is not None and by_alias is not None
        assert by_manifest_without_name is not None
        self.assertEqual(named.name, "chosen")
        self.assertEqual(by_alias.name, "shortcut-name")
        self.assertEqual(by_manifest_without_name.name, "plain-alias")

    def test_resolution_stops_rather_than_walking_forever(self) -> None:
        deep = self.directory("/".join(f"d{index}" for index in range(80)))
        self.manifest("", 'name = "top"\n')
        # The manifest is above the walk bound, so the walk gives up instead of
        # climbing out of the fixture and into the developer's real home.
        self.assertIsNone(self.resolver().resolve(deep))


class AliasVersusPathTests(ResolverFixture):
    """An alias launches its own directory; a path resolves the project above it.

    These two entry points can name different roots when a shortcut points
    *inside* a project that has a manifest further up. Both behaviours are
    deliberate and neither applies a setting the other would not, so the rule
    is pinned here rather than left to be discovered.
    """

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.directory("repo")
        self.tools = self.directory("repo/tools")
        self.manifest("repo", 'name = "demo"\nmodel = "claude-opus-5"\n')

    def test_a_path_inside_the_project_resolves_to_the_project(self) -> None:
        self.shortcuts(("tools", "shell", os.fspath(self.tools)))
        project = self.resolver().resolve(self.tools)
        assert project is not None
        self.assertEqual(project.root, os.fspath(self.repo))
        self.assertEqual(project.name, "demo")

    def test_an_alias_keeps_its_own_directory_as_the_launch_target(self) -> None:
        # Changing this would silently move where `sp new tools` lands.
        self.shortcuts(("tools", "shell", os.fspath(self.tools)))
        project = self.resolver().resolve_alias("tools")
        assert project is not None
        self.assertEqual(project.root, os.fspath(self.tools))

    def test_a_manifest_further_up_governs_neither_alias_launch(self) -> None:
        # Whether the parent is on the list or not, an alias pointing deeper
        # launches with the shortcut's own settings only. The alias directory
        # is itself on the list, so the project is trusted, there is simply
        # no manifest of its own to apply, and the one above it is not
        # reached. Adding /repo/tools is not a statement about who may write
        # /repo's manifest.
        for rows in (
            (("tools", "shell", os.fspath(self.tools)),),
            (
                ("demo", "claude", os.fspath(self.repo)),
                ("tools", "shell", os.fspath(self.tools)),
            ),
        ):
            with self.subTest(rows=len(rows)):
                self.shortcuts(*rows)
                project = self.resolver().resolve_alias("tools")
                assert project is not None
                self.assertIsNone(project.manifest)
                self.assertIsNone(project.manifest_path)
                self.assertEqual(project.source, identity.SOURCE_SHORTCUT)

    def test_an_alias_at_the_manifest_directory_gets_everything(self) -> None:
        self.shortcuts(("demo", "claude", os.fspath(self.repo)))
        project = self.resolver().resolve_alias("demo")
        assert project is not None
        self.assertTrue(project.trusted)
        assert project.manifest is not None
        self.assertEqual(project.manifest["model"], "claude-opus-5")


class TrustTests(ResolverFixture):
    def test_a_manifest_without_a_shortcut_row_is_read_but_not_trusted(self) -> None:
        self.manifest("repo", 'name = "demo"\nmodel = "claude-opus-5"\n')
        project = self.resolver().resolve(self.root / "repo")
        assert project is not None
        self.assertFalse(project.trusted)
        assert project.manifest is not None
        self.assertEqual(project.manifest["model"], "claude-opus-5")
        self.assertTrue(
            any("not on the host's project list" in note for note in project.warnings)
        )

    def test_adding_the_project_is_what_makes_its_manifest_apply(self) -> None:
        repo = self.directory("repo")
        self.manifest("repo", 'name = "demo"\n')
        self.shortcuts(("demo", "claude", os.fspath(repo)))
        project = self.resolver().resolve(repo)
        assert project is not None
        self.assertTrue(project.trusted)
        self.assertEqual(project.warnings, ())

    def test_a_shortcut_deeper_than_the_manifest_root_does_not_confer_trust(
        self,
    ) -> None:
        # Adding /repo/tools says nothing about who may write /repo's manifest.
        self.manifest("repo", 'name = "demo"\n')
        self.shortcuts(("tools", "shell", os.fspath(self.directory("repo/tools"))))
        project = self.resolver().resolve(self.root / "repo" / "tools")
        assert project is not None
        self.assertEqual(project.root, os.fspath(self.root / "repo"))
        self.assertFalse(project.trusted)


class MalformedManifestTests(ResolverFixture):
    def test_a_malformed_manifest_reports_and_applies_nothing(self) -> None:
        repo = self.directory("repo")
        self.manifest("repo", 'name = "demo"\nnonsense = = =\n')
        self.shortcuts(("demo", "claude", os.fspath(repo)))
        project = self.resolver().resolve(repo)
        assert project is not None
        # The project still exists, so its sessions stay placeable.
        self.assertEqual(project.root, os.fspath(repo))
        self.assertIsNone(project.manifest)
        assert project.manifest_error is not None
        self.assertIn("line 2", project.manifest_error)
        self.assertEqual(project.name, "demo")

    def test_a_symlinked_manifest_is_reported_not_followed(self) -> None:
        repo = self.directory("repo")
        elsewhere = self.directory("elsewhere") / "real.toml"
        elsewhere.write_text('name = "attacker"\n', encoding="utf-8")
        (repo / identity.MANIFEST_NAME).symlink_to(elsewhere)
        project = self.resolver().resolve(repo)
        assert project is not None
        self.assertIsNone(project.manifest)
        assert project.manifest_error is not None
        self.assertIn("symlink", project.manifest_error)
        self.assertNotEqual(project.name, "attacker")

    def test_an_unreadable_shortcut_line_is_skipped_with_a_warning(self) -> None:
        good = self.directory("good")
        self.projects_file.write_text(
            "# a comment\n"
            "\n"
            "broken-line-without-tabs\n"
            f"bad-kind\tgemini\t{good}\n"
            "relative\tclaude\tnot/absolute\n"
            f"good\tclaude\t{good}\n",
            encoding="utf-8",
        )
        resolver = self.resolver()
        self.assertEqual([row["alias"] for row in resolver.shortcuts()], ["good"])
        self.assertEqual(len(resolver.warnings), 3)

    def test_a_missing_shortcut_file_is_not_an_error(self) -> None:
        resolver = identity.Resolver(self.config / "absent.tsv", environ={})
        self.assertEqual(resolver.shortcuts(), [])
        self.assertEqual(resolver.warnings, ())


class WorktreeTests(ResolverFixture):
    def link_worktree(self, worktree: Path, main: Path, name: str) -> None:
        (main / ".git" / "worktrees" / name).mkdir(parents=True, exist_ok=True)
        (worktree / ".git").write_text(
            f"gitdir: {main}/.git/worktrees/{name}\n", encoding="utf-8"
        )

    def test_a_linked_worktree_groups_under_the_repository_it_came_from(self) -> None:
        main = self.directory("repo")
        worktree = self.directory("wt-feature")
        self.link_worktree(worktree, main, "feature")
        self.manifest("repo", 'name = "demo"\n')
        self.manifest("wt-feature", 'name = "demo"\n')
        project = self.resolver().resolve(worktree)
        assert project is not None
        self.assertEqual(project.root, os.fspath(worktree))
        self.assertEqual(project.group_root, os.fspath(main))
        self.assertTrue(project.is_worktree)

    def test_a_worktree_inherits_the_trust_of_its_repository(self) -> None:
        main = self.directory("repo")
        worktree = self.directory("wt-feature")
        self.link_worktree(worktree, main, "feature")
        self.manifest("wt-feature", 'name = "demo"\nmodel = "claude-opus-5"\n')
        self.shortcuts(("demo", "claude", os.fspath(main)))
        project = self.resolver().resolve(worktree)
        assert project is not None
        self.assertTrue(project.trusted)

    def test_a_normal_repository_is_its_own_group(self) -> None:
        main = self.directory("repo")
        (main / ".git").mkdir()
        self.manifest("repo", 'name = "demo"\n')
        project = self.resolver().resolve(main)
        assert project is not None
        self.assertFalse(project.is_worktree)
        self.assertEqual(project.group, os.fspath(main))

    def test_a_git_file_that_is_not_a_worktree_pointer_is_ignored(self) -> None:
        repo = self.directory("repo")
        for text in ("gitdir: /elsewhere/.git\n", "not a pointer\n", ""):
            with self.subTest(text=text):
                (repo / ".git").write_text(text, encoding="utf-8")
                self.assertIsNone(identity.main_repository(os.fspath(repo)))

    def test_grouping_collapses_worktrees_under_their_repository(self) -> None:
        main = self.directory("repo")
        first = self.directory("wt-one")
        second = self.directory("wt-two")
        self.link_worktree(first, main, "one")
        self.link_worktree(second, main, "two")
        for relative in ("repo", "wt-one", "wt-two"):
            self.manifest(relative, 'name = "demo"\n')
        self.shortcuts(
            ("demo", "claude", os.fspath(main)),
            ("demo-one", "claude", os.fspath(first)),
            ("demo-two", "claude", os.fspath(second)),
        )
        groups = identity.group_projects(self.resolver().projects())
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["group_root"], os.fspath(main))
        self.assertEqual(len(groups[0]["members"]), 3)


class ListingTests(ResolverFixture):
    def test_the_project_list_is_one_entry_per_shortcut_directory(self) -> None:
        first, second = self.directory("one"), self.directory("two")
        self.manifest("one", 'name = "alpha"\n')
        self.shortcuts(
            ("one", "claude", os.fspath(first)),
            ("two", "codex", os.fspath(second)),
            ("dup", "claude", os.fspath(first)),
        )
        projects = self.resolver().projects()
        self.assertEqual([project.name for project in projects], ["alpha", "two"])
        self.assertEqual(projects[0].source, identity.SOURCE_MANIFEST)
        self.assertEqual(projects[1].source, identity.SOURCE_SHORTCUT)

    def test_assign_resolves_each_directory_once(self) -> None:
        repo = self.directory("repo")
        self.manifest("repo", 'name = "demo"\n')
        deep = self.directory("repo/a")
        loose = self.directory("loose")
        assigned = self.resolver().assign(
            [os.fspath(repo), os.fspath(deep), os.fspath(repo), os.fspath(loose)]
        )
        self.assertEqual(len(assigned), 3)
        self.assertIsNone(assigned[os.fspath(loose)])
        assert assigned[os.fspath(deep)] is not None
        self.assertEqual(assigned[os.fspath(deep)].root, os.fspath(repo))


class PathTests(unittest.TestCase):
    def test_canonical_refuses_what_is_not_an_absolute_path(self) -> None:
        self.assertIsNone(identity.canonical("\x00"))

    def test_membership_compares_whole_path_components(self) -> None:
        self.assertTrue(identity._within("/srv/app/a", "/srv/app"))
        self.assertTrue(identity._within("/srv/app", "/srv/app"))
        self.assertFalse(identity._within("/srv/application", "/srv/app"))
        self.assertTrue(identity._within("/srv", "/"))

    def test_the_default_shortcut_file_follows_the_shell_rules(self) -> None:
        self.assertEqual(
            identity.default_projects_file({"SESSION_KIT_PROJECTS_FILE": "/tmp/p.tsv"}),
            Path("/tmp/p.tsv"),
        )
        self.assertEqual(
            identity.default_projects_file({"XDG_CONFIG_HOME": "/c", "HOME": "/h"}),
            Path("/c/session-kit/projects.tsv"),
        )
        self.assertEqual(
            identity.default_projects_file({"HOME": "/h"}),
            Path("/h/.config/session-kit/projects.tsv"),
        )


if __name__ == "__main__":
    unittest.main()
