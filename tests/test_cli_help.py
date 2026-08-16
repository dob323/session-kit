"""The command help, its topics, the shipped completion, and their drift guards.

Help is the one surface that has to work when nothing else does, so these tests
run every entry point's `--help` on a machine with no shpool on PATH and no
Session Kit state to read. The rest are drift guards: a verb the dispatcher
accepts but the help never mentions, a topic advertised but not implemented, a
machine verb that leaked into the human list, an exit status no document
explains, or a completion that offers a verb `sp help` does not.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from tests.support import REPO, without_shpool


SP = REPO / "bin" / "sp"
SESSION_KIT = REPO / "bin" / "session-kit"
STATUS = REPO / "bin" / "shpool_status"
REAPER = REPO / "bin" / "shpool_reaper"
WATCHDOG = REPO / "bin" / "session_kit_watchdog"
RESUME = REPO / "bin" / "codex_resume_here"
LAUNCHER = REPO / "deploy" / "session-kit-launcher"
COMPLETION = REPO / "lib" / "sh" / "sp_completion.bash"
INSTALL_MODULE = REPO / "lib" / "sh" / "session_kit_install.sh"
UNINSTALL_MODULE = REPO / "lib" / "sh" / "session_kit_uninstall.sh"

# Every entry point a person can type, with the arguments that must be a usage
# error. `sp help a b` is sp's: it needs no shpool, so the check stays honest on
# a machine that has none.
ENTRY_POINTS = (
    (SP, ("help", "a", "b")),
    (SESSION_KIT, ("no-such-verb",)),
    (STATUS, ("--bogus",)),
    (REAPER, ("--bogus",)),
    (WATCHDOG, ("--bogus",)),
    (RESUME, ("--bogus",)),
)

TOPIC_LINE = re.compile(r"^  sp help (?P<topic>[a-z-]+) ")
# `  go|resume|attach)` and friends: one dispatcher arm, all of its spellings.
CASE_ARM = re.compile(r"^  ([a-z][a-z0-9|-]*)\)$", re.MULTILINE)
EXIT_STATUS = re.compile(r"\bexit ([0-9]+)\b")


class HelpFixture:
    """An isolated HOME and a PATH with no shpool anywhere on it."""

    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".cli-help-", dir=REPO)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()

    def close(self) -> None:
        self.temp.cleanup()

    def env(self, **extra: str) -> dict[str, str]:
        # `without_shpool` is load-bearing, not decoration. The suite-wide
        # sandbox guard (tests/sandbox_guard.py) normally puts a refusing
        # `shpool` on every child's PATH, and that would make
        # test_help_is_reachable_without_shpool pass for the wrong reason:
        # shpool would be present. This asks the guard for a machine with no
        # session manager at all, which it delivers by removing its own stub
        # AND any real binary, so the absence these tests need is real.
        return without_shpool(
            {
                "HOME": os.fspath(self.home),
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "XDG_STATE_HOME": os.fspath(self.base / "state"),
                "XDG_CONFIG_HOME": os.fspath(self.base / "config"),
                "XDG_DATA_HOME": os.fspath(self.base / "data"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "SESSION_KIT_NONINTERACTIVE": "1",
                **extra,
            }
        )

    def run(self, *argv: object, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [os.fspath(part) for part in argv],
            env=self.env(**extra),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )


class CommandHelpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = HelpFixture()
        cls.overview = cls.fixture.run(SP, "help")
        cls.dispatcher = SP.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.close()

    def topics(self) -> list[str]:
        return [
            match.group("topic")
            for line in self.overview.stdout.splitlines()
            if (match := TOPIC_LINE.match(line))
        ]

    def dispatcher_arms(self) -> list[tuple[str, ...]]:
        """Every verb the second case block accepts, as spelling groups."""
        body = self.dispatcher.split("sk_require_shpool || exit 1", 1)[1]
        return [
            tuple(match.split("|"))
            for match in CASE_ARM.findall(body)
        ]

    def test_shpool_is_absent_from_the_fixture_path(self) -> None:
        """Otherwise the reachability test below proves nothing."""
        found = subprocess.run(
            ["bash", "-c", "command -v shpool || true"],
            env=self.fixture.env(),
            text=True,
            stdout=subprocess.PIPE,
            check=False,
        )
        self.assertEqual("", found.stdout.strip())

    def test_help_is_reachable_without_shpool(self) -> None:
        for flag in ("help", "-h", "--help"):
            with self.subTest(flag=flag):
                completed = self.fixture.run(SP, flag)
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual("", completed.stderr)
                self.assertNotIn("shpool executable unavailable", completed.stdout)
                self.assertIn("Sessions", completed.stdout)
                self.assertEqual(self.overview.stdout, completed.stdout)

    def test_the_overview_groups_the_commands_and_describes_each_one(self) -> None:
        text = self.overview.stdout
        for group in (
            "Sessions",
            "Names and colors",
            "Delegated work",
            "Accounts",
            "History and recovery",
            "Topics",
        ):
            self.assertIn(f"\n{group}\n", text, f"{group} is not a help group")
        described = [
            line
            for line in text.splitlines()
            if re.match(r"^  sp \S.* {2,}\S", line)
        ]
        self.assertGreaterEqual(len(described), 20, text)

    def test_every_advertised_topic_answers(self) -> None:
        topics = self.topics()
        self.assertGreaterEqual(len(topics), 9, topics)
        for topic in topics:
            with self.subTest(topic=topic):
                completed = self.fixture.run(SP, "help", topic)
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual("", completed.stderr)
                self.assertGreater(len(completed.stdout.splitlines()), 3)

    def test_the_unavailable_topic_names_automatic_safe_cleanup(self) -> None:
        """Quarantine documents the automatic path and its proof boundary."""
        completed = self.fixture.run(SP, "help", "unavailable")
        self.assertEqual(0, completed.returncode, completed.stderr)
        text = completed.stdout
        self.assertIn("no session number", text)
        self.assertIn("next cleanup pass", text)
        self.assertIn("attach-and-exit", text)
        self.assertIn("hard timeout", text)
        self.assertIn("any surviving session process", text)

    def test_an_unknown_topic_names_the_topics_it_has(self) -> None:
        completed = self.fixture.run(SP, "help", "no-such-topic")
        self.assertEqual(2, completed.returncode)
        self.assertEqual("", completed.stdout)
        for topic in self.topics():
            self.assertIn(topic, completed.stderr)

    def test_every_human_verb_the_dispatcher_accepts_is_documented(self) -> None:
        """The coverage gap this help replaced: verbs sp took and never named."""
        missing = []
        for spellings in self.dispatcher_arms():
            if spellings[0].startswith("picker-") or spellings[0] == "restore-exact":
                continue
            if not any(
                f"  sp {spelling}" in self.overview.stdout for spelling in spellings
            ):
                missing.append("|".join(spellings))
        self.assertEqual(
            [],
            missing,
            "sp accepts these verbs and `sp help` never mentions them: "
            f"{missing}. Add them to the overview in bin/sp.",
        )

    def test_machine_verbs_are_hidden_from_humans_and_written_down_once(self) -> None:
        machine = [
            spellings[0]
            for spellings in self.dispatcher_arms()
            if spellings[0].startswith("picker-") or spellings[0] == "restore-exact"
        ]
        self.assertGreater(len(machine), 5, machine)
        topic = self.fixture.run(SP, "help", "machine")
        self.assertEqual(0, topic.returncode, topic.stderr)
        for verb in machine:
            with self.subTest(verb=verb):
                self.assertNotIn(f"sp {verb}", self.overview.stdout)
                self.assertIn(f"sp {verb}", topic.stdout)

    def test_sp_list_rejects_arguments_instead_of_silently_drawing_text(self) -> None:
        completed = self.fixture.run(SP, "list", "--json")
        self.assertEqual(2, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertTrue(
            completed.stderr.startswith("session-kit: sp list takes nothing else\n"),
            completed.stderr,
        )

    def test_bare_sp_is_the_same_listing_as_sp_list(self) -> None:
        fake_shpool = self.fixture.bin / "shpool"
        fake_shpool.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        fake_shpool.chmod(0o755)
        fake_core = self.fixture.base / "inventory-core"
        fake_core.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "assert sys.argv[1:] == ['render']\n"
            "print('the unchanged session list')\n",
            encoding="utf-8",
        )
        fake_core.chmod(0o755)
        env = {
            "SESSION_KIT_SHPOOL_CMD": os.fspath(fake_shpool),
            "SESSION_KIT_INVENTORY_CORE": os.fspath(fake_core),
        }
        try:
            bare = self.fixture.run(SP, **env)
            explicit = self.fixture.run(SP, "list", **env)
            self.assertEqual(0, bare.returncode, bare.stderr)
            self.assertEqual(explicit.stdout, bare.stdout)
            self.assertEqual("the unchanged session list\n", bare.stdout)
        finally:
            fake_shpool.unlink(missing_ok=True)
            fake_core.unlink(missing_ok=True)

    def test_usage_lists_every_new_picker_key_and_search_field(self) -> None:
        usage = (REPO / "docs" / "usage.md").read_text(encoding="utf-8")
        self.assertIn(
            "model number    move an idle Claude or Codex conversation",
            usage,
        )
        self.assertIn(
            "x               show or hide machine sessions",
            usage,
        )
        self.assertIn(
            "/text           filter names, providers, accounts, models, and projects",
            usage,
        )


class UniformHelpTests(unittest.TestCase):
    """One contract for every entry point: help on stdout, mistakes on stderr."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = HelpFixture()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.close()

    def test_h_and_long_help_print_to_stdout_and_succeed(self) -> None:
        for command, _ in ENTRY_POINTS:
            for flag in ("-h", "--help"):
                with self.subTest(command=command.name, flag=flag):
                    completed = self.fixture.run(command, flag)
                    self.assertEqual(0, completed.returncode, completed.stderr)
                    self.assertEqual("", completed.stderr)
                    self.assertIn("usage", completed.stdout.lower())

    def test_an_argument_mistake_prints_usage_to_stderr_and_exits_two(self) -> None:
        for command, bad in ENTRY_POINTS:
            with self.subTest(command=command.name):
                completed = self.fixture.run(command, *bad)
                self.assertEqual(2, completed.returncode, completed.stdout)
                self.assertEqual("", completed.stdout)
                self.assertNotEqual("", completed.stderr.strip())

    def test_doctor_does_not_advertise_an_unimplemented_authority_mode(self) -> None:
        dead_authority_mode = "doctor --" + "authority"
        help_result = self.fixture.run(SESSION_KIT, "--help")
        self.assertEqual(0, help_result.returncode, help_result.stderr)
        self.assertNotIn(dead_authority_mode, help_result.stdout)

        report = self.fixture.run(SESSION_KIT, "doctor", "--json")
        self.assertNotEqual(0, report.returncode)
        acceptance = next(
            row
            for row in json.loads(report.stdout)["checks"]
            if row["name"] == "acceptance"
        )
        self.assertEqual("warn", acceptance["status"])
        self.assertIn("release-acceptance.json", acceptance["detail"])
        self.assertNotIn(dead_authority_mode, acceptance["detail"])


class StatusModeDocumentationTests(unittest.TestCase):
    """shpool_status modes that look like reads and are not, said out loud."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = HelpFixture()
        cls.usage = cls.fixture.run(STATUS, "--help").stdout
        cls.source = STATUS.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.close()

    def section(self, heading: str) -> str:
        start = self.usage.index(heading)
        rest = self.usage[start + len(heading) :]
        end = rest.find("\n\n")
        return rest if end < 0 else rest[:end]

    def modes(self, heading: str) -> list[str]:
        """The mode each entry under a heading is about, not every word in it."""
        return [
            match.group(1)
            for line in self.section(heading).splitlines()
            if (match := re.match(r"^  (--[a-z-]+|\(no mode\))", line))
        ]

    def test_the_read_only_modes_are_the_ones_that_never_build_state(self) -> None:
        self.assertEqual(
            [
                "(no mode)",
                "--rows",
                "--strict-json",
                "--guard-json",
                "--render-file",
                "--lookup-file",
                "--recovery-pending-list",
            ],
            self.modes("Read-only modes"),
            "the dashboards, replay paths, and no-write probes belong under read-only",
        )
        self.assertEqual(
            ["--waiting-count", "--detail", "--json", "--lookup"],
            self.modes("refresh the cached inventory as they run:"),
        )

    def test_naming_runs_on_refreshes_but_never_on_a_listing(self) -> None:
        core = self.fixture.base / "status-core"
        log = self.fixture.base / "status-core.log"
        core.write_text(
            "#!/usr/bin/env python3\n"
            "import json,os,pathlib,sys\n"
            "with pathlib.Path(os.environ['STATUS_CORE_LOG']).open('a') as out:\n"
            "    out.write(json.dumps(sys.argv[1:])+'\\n')\n"
            "print('{}')\n",
            encoding="utf-8",
        )
        core.chmod(0o755)
        env = {
            "SESSION_KIT_INVENTORY_CORE": os.fspath(core),
            "STATUS_CORE_LOG": os.fspath(log),
        }

        expected = {
            (): [["render"]],
            ("--rows",): [["render", "--rows"]],
            ("--json",): [
                ["automatic-title", "claude-pending"],
                ["automatic-title", "codex-pending"],
                ["snapshot"],
            ],
            ("--lookup", "7"): [
                ["automatic-title", "claude-pending"],
                ["automatic-title", "codex-pending"],
                ["lookup", "7"],
            ],
        }
        for argv, calls in expected.items():
            with self.subTest(argv=argv):
                log.unlink(missing_ok=True)
                completed = self.fixture.run(STATUS, *argv, **env)
                self.assertEqual(0, completed.returncode, completed.stderr)
                observed = [
                    json.loads(line)
                    for line in log.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(calls, observed)

    def test_the_mutating_recovery_modes_are_named_as_such(self) -> None:
        mutating = self.section("change recovery state")
        self.assertIn("--recovery-pending-ack", mutating)
        # Writing down what a screen printed is a write, and a person reading
        # this page is entitled to find it where the writes are listed.
        self.assertIn("--recovery-remember-printed", mutating)


class ExitCodeDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = HelpFixture()
        cls.topic = cls.fixture.run(SP, "help", "exit-codes").stdout

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.close()

    def documented(self) -> set[str]:
        return {
            match.group(1)
            for line in self.topic.splitlines()
            if (match := re.match(r"^  ([0-9]+) {2,}\S", line))
        }

    def test_every_status_a_command_returns_is_explained(self) -> None:
        sources = [*sorted((REPO / "bin").iterdir()), LAUNCHER]
        sources += sorted((REPO / "lib" / "sh").glob("*.sh"))
        used: set[str] = set()
        for path in sources:
            if not path.is_file():
                continue
            used.update(EXIT_STATUS.findall(path.read_text(encoding="utf-8")))
        # The picker's two statuses are constants, not literals in an exit line.
        used.update({"74", "75"})
        used.discard("0")
        undocumented = sorted(used - self.documented(), key=int)
        self.assertEqual(
            [],
            undocumented,
            f"these statuses are returned and never explained: {undocumented}. "
            "Document them in the exit-codes topic in bin/sp.",
        )

    def test_the_picker_statuses_are_the_ones_the_code_uses(self) -> None:
        source = SP.read_text(encoding="utf-8")
        self.assertIn("PICKER_REFUSED_STATUS=74", source)
        self.assertIn("PICKER_ATTACH_FAILED_STATUS=75", source)


class CompletionTests(unittest.TestCase):
    """The shipped completion: what it offers, and what it must never do."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = HelpFixture()
        cls.overview = cls.fixture.run(SP, "help").stdout

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.close()

    def complete(self, function: str, *words: str, **extra: str) -> list[str]:
        script = (
            f'source "{COMPLETION}"\n'
            "COMP_WORDS=(" + " ".join(f'"{word}"' for word in words) + ")\n"
            "COMP_CWORD=$(( ${#COMP_WORDS[@]} - 1 ))\n"
            "COMPREPLY=()\n"
            f"{function}\n"
            'printf "%s\\n" "${COMPREPLY[@]:-}"\n'
        )
        completed = subprocess.run(
            ["bash", "-c", script],
            env=self.fixture.env(**extra),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        return [line for line in completed.stdout.split("\n") if line]

    def test_it_registers_every_command_it_ships_for(self) -> None:
        listed = subprocess.run(
            ["bash", "-c", f'source "{COMPLETION}"; complete -p'],
            env=self.fixture.env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, listed.returncode, listed.stderr)
        for command in (
            "sp",
            "session-kit",
            "shpool_status",
            "shpool_reaper",
            "codex_resume_here",
        ):
            self.assertRegex(listed.stdout, rf"complete .*\b{re.escape(command)}$|"
                             rf"complete .*\b{re.escape(command)}\n")

    def test_the_first_word_offers_human_verbs_and_no_machine_verb(self) -> None:
        offered = self.complete("_sp", "sp", "")
        self.assertIn("list", offered)
        self.assertIn("help", offered)
        for verb in offered:
            self.assertFalse(verb.startswith("picker-"), verb)
            self.assertNotEqual("restore-exact", verb)
        for verb in offered:
            self.assertIn(
                f"  sp {verb}",
                self.overview,
                f"completion offers `sp {verb}` and `sp help` never names it",
            )

    def test_the_topics_it_offers_are_the_topics_help_has(self) -> None:
        advertised = [
            match.group("topic")
            for line in self.overview.splitlines()
            if (match := TOPIC_LINE.match(line))
        ]
        self.assertEqual(sorted(advertised), sorted(self.complete("_sp", "sp", "help", "")))

    def test_it_completes_providers_colors_and_project_aliases(self) -> None:
        projects = self.fixture.base / "projects.tsv"
        projects.write_text(
            "# alias\tprovider\tcwd\nnotes\tclaude\t/srv/notes\n",
            encoding="utf-8",
        )
        offered = self.complete(
            "_sp", "sp", "new", "", SESSION_KIT_PROJECTS_FILE=os.fspath(projects)
        )
        self.assertEqual(["claude", "codex", "shell", "notes"], offered)
        self.assertIn("magenta", self.complete("_sp", "sp", "color", "2", ""))
        self.assertEqual(
            ["enable", "disable", "status"],
            self.complete("_session_kit", "session-kit", "services", ""),
        )

    def test_a_tab_press_never_runs_a_session_command(self) -> None:
        """A completion that shelled out would build state from inside a prompt."""
        marker = self.fixture.base / "ran"
        for name in ("sp", "shpool_status", "session-kit", "shpool_reaper"):
            trap = self.fixture.bin / name
            trap.write_text(
                f'#!/bin/sh\necho "$0" >> "{marker}"\nexit 1\n', encoding="utf-8"
            )
            trap.chmod(0o755)
        for function, words in (
            ("_sp", ("sp", "")),
            ("_sp", ("sp", "go", "")),
            ("_sp", ("sp", "close", "")),
            ("_sp", ("sp", "new", "")),
            ("_session_kit", ("session-kit", "")),
            ("_shpool_status", ("shpool_status", "--")),
        ):
            self.complete(function, *words)
        self.assertFalse(
            marker.exists(),
            f"completion executed: {marker.read_text() if marker.exists() else ''}",
        )

    def test_it_parses_under_the_bash_that_ships_with_macos(self) -> None:
        """A completion file that fails to parse breaks the prompt that sourced it."""
        code = "\n".join(
            line
            for line in COMPLETION.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        for bash_four_only in ("mapfile", "readarray", "declare -A", ",,}"):
            self.assertNotIn(bash_four_only, code, bash_four_only)


class CompletionInstallTests(unittest.TestCase):
    """The copies the installer writes, and the ones the uninstaller may remove."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".cli-help-install-", dir=REPO)
        self.base = Path(self.temp.name)
        self.release = self.base / "root" / "releases" / ("a" * 40)
        (self.release / "lib" / "sh").mkdir(parents=True)
        (self.release / "lib" / "sh" / "sp_completion.bash").write_text(
            COMPLETION.read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.completions = self.base / "completions"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def shell(self, call: str) -> subprocess.CompletedProcess[str]:
        script = (
            "set -u\n"
            f'install_root="{self.base / "root"}"\n'
            f'completion_dir="{self.completions}"\n'
            "completion_names=(sp session-kit shpool_status)\n"
            "completion_marker='# session-kit managed bash completion v1'\n"
            f'source "{INSTALL_MODULE}"\n'
            f'source "{UNINSTALL_MODULE}"\n'
            f"{call}\n"
        )
        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=REPO,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed

    def test_install_writes_one_copy_per_command_name(self) -> None:
        self.shell(f'install_completions "{"a" * 40}"')
        for name in ("sp", "session-kit", "shpool_status"):
            copy = self.completions / name
            self.assertTrue(copy.is_file(), name)
            self.assertIn("session-kit managed bash completion", copy.read_text())
            self.assertEqual(0o644, copy.stat().st_mode & 0o777)

    def test_a_file_that_is_not_ours_is_never_replaced_or_removed(self) -> None:
        self.completions.mkdir()
        foreign = self.completions / "sp"
        foreign.write_text("# somebody else's completion\n", encoding="utf-8")
        self.shell(f'install_completions "{"a" * 40}"')
        self.assertEqual("# somebody else's completion\n", foreign.read_text())
        self.shell("remove_completions")
        self.assertTrue(foreign.is_file())
        self.assertFalse((self.completions / "session-kit").exists())

    def test_uninstall_removes_the_copies_it_wrote(self) -> None:
        self.shell(f'install_completions "{"a" * 40}"')
        self.shell("remove_completions")
        for name in ("sp", "session-kit", "shpool_status"):
            self.assertFalse((self.completions / name).exists(), name)

    def test_none_turns_the_whole_thing_off(self) -> None:
        script = (
            "set -u\n"
            f'install_root="{self.base / "root"}"\n'
            'completion_dir=none\n'
            "completion_names=(sp)\n"
            "completion_marker='# session-kit managed bash completion v1'\n"
            f'source "{INSTALL_MODULE}"\n'
            f'install_completions "{"a" * 40}"\n'
        )
        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=REPO,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertFalse((self.base / "none").exists())

    def test_the_source_check_knows_the_completion_file_has_to_be_there(self) -> None:
        manager = SESSION_KIT.read_text(encoding="utf-8")
        block = manager.split("shell_modules=(", 1)[1].split(")", 1)[0]
        self.assertIn("lib/sh/sp_completion.bash", block)


if __name__ == "__main__":
    unittest.main()
