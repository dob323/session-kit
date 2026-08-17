"""The kit owns the terminal tab name, on both providers (K3).

The design rests on one measurement, made live on 2026-08-13 against Claude
Code 2.1.229 with ``tools/title-ownership-drill``:
``CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1`` does not silence only the vendor's own
titles, it silences the window's title writes entirely -- the kit hook's
``sessionTitle`` and a hook-emitted ``terminalSequence`` included. With the
switch unset both mechanisms produced ``ESC]0;<glyph> <kit title>BEL``; with it
set the same session wrote no title sequence at all.

So the kit never sets that variable, writes the tab name itself at the moments
no provider hook fires, and hands Codex the item list it deployed. These tests
pin all three halves plus the single kill switch.
"""

from __future__ import annotations

import os
from pathlib import Path
import pty
import re
import shutil
import subprocess
import tempfile

try:
    import tomllib
except ImportError:  # Python 3.10 is supported and has no tomllib.
    tomllib = None  # type: ignore[assignment]
import unittest

from tests.support import REPO


# Item identifiers codex-cli 0.145.0 accepts for tui.terminal_title, enumerated
# live on 2026-08-13, one id per run, with its own doctor as the oracle: an id
# it does not know produces `terminal title invalid items "<id>"` and the Notes
# line `terminal title configuration contains unknown item identifiers`, and an
# accepted id produces `✓ title  configured`. Raw doctor output for every id is
# archived beside the drill evidence.
#
# `cwd`, `account` and `none` are NOT here on purpose: an earlier pass listed
# them as valid because its probe treated "the error string is absent" as
# acceptance, so a run that ended early read as a pass. Codex rejects all three.
# Any future change to this set is re-probed the same way, with the doctor
# output kept, and an inconclusive run is never read as acceptance.
CODEX_TITLE_ITEMS = {
    "spinner",
    "activity",
    "thread",
    "thread-title",
    "project",
    "project-name",
    "model",
    "model-with-reasoning",
    "git-branch",
    "context-used",
    "weekly-limit",
    "five-hour-limit",
}
CODEX_TITLE_ITEMS_REJECTED = {"cwd", "account", "none"}

OSC_TITLE = re.compile(rb"\x1b\]0;([^\x07]*)\x07")

SHIPPED_TREES = ("bin", "lib", "bashrc", "config", "deploy", "systemd", "macos")


def run_on_pty(script: str, env: dict[str, str] | None = None) -> bytes:
    """Run a bash snippet with a real terminal on stdout, return what it wrote.

    ``sk_tab_title`` deliberately refuses to write when stdout is not a
    terminal, so a plain pipe would prove nothing.
    """
    parent, child = pty.openpty()
    environment = dict(os.environ)
    environment.setdefault("SESSION_KIT_CODEX_AUTOTITLE", "0")
    environment.update(env or {})
    process = subprocess.Popen(
        ["bash", "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=child,
        stderr=subprocess.DEVNULL,
        env=environment,
    )
    os.close(child)
    collected = b""
    try:
        while True:
            try:
                chunk = os.read(parent, 65536)
            except OSError:
                break
            if not chunk:
                break
            collected += chunk
    finally:
        process.wait(timeout=30)
        os.close(parent)
    return collected


def drill_module():
    """Load the drill script (no .py suffix) as a module."""
    import importlib.machinery
    import importlib.util
    import sys

    if "sk_title_drill" in sys.modules:
        return sys.modules["sk_title_drill"]
    path = REPO / "tools" / "title-ownership-drill"
    loader = importlib.machinery.SourceFileLoader("sk_title_drill", os.fspath(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def drill_run(mode: str, *, marker: bool, titles: int = 1) -> dict:
    written = [f"✳ {'SKDRILLMARKER' if marker else 'something else'}-{mode}"]
    return {
        "mode": mode,
        "disable_env": mode.endswith("-off"),
        "hook_events_seen": ["SessionStart"],
        "titles_written": written[:titles],
        "marker_present": marker and titles > 0,
        "vendor_titles": [],
        "capture": f"pty-{mode}.bin",
        "capture_bytes": 3700,
    }


class DrillVerdictTests(unittest.TestCase):
    """Each verdict cites the run it came from.

    The drill is the instrument for re-answering item 18 after a vendor
    upgrade. An earlier version read the terminalSequence verdict out of the
    switch-UNSET run, so it printed "survives" while its own switch-set capture
    held no title at all.
    """

    def verdict(self, runs: list) -> tuple[str, int]:
        import contextlib
        import io

        module = drill_module()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = module.report(runs, Path("/tmp/evidence"))
        return out.getvalue(), status

    def test_a_suppressed_sequence_is_never_reported_as_surviving(self) -> None:
        text, status = self.verdict(
            [
                drill_run("title-on", marker=True),
                drill_run("title-off", marker=False, titles=0),
                drill_run("sequence-on", marker=True),
                drill_run("sequence-off", marker=False, titles=0),
            ]
        )
        self.assertEqual(0, status)
        self.assertIn("terminalSequence: control sequence-on", text)
        self.assertIn("sequence-off wrote 0 — marker suppressed", text)
        self.assertNotIn("marker survives", text)
        self.assertIn("suppresses the kit hook title too", text)

    def test_a_surviving_sequence_is_reported_from_its_own_run(self) -> None:
        text, status = self.verdict(
            [
                drill_run("title-on", marker=True),
                drill_run("title-off", marker=False, titles=0),
                drill_run("sequence-on", marker=True),
                drill_run("sequence-off", marker=True),
            ]
        )
        self.assertEqual(0, status)
        self.assertIn("terminalSequence:", text)
        self.assertIn("marker survives", text)
        self.assertIn("suppresses some kit titles but not all", text)
        self.assertIn("surviving: terminalSequence", text)

    def test_a_control_that_wrote_nothing_is_inconclusive(self) -> None:
        text, status = self.verdict(
            [
                drill_run("title-on", marker=True),
                drill_run("title-off", marker=False, titles=0),
                drill_run("sequence-on", marker=False, titles=0),
                drill_run("sequence-off", marker=False, titles=0),
            ]
        )
        self.assertEqual(2, status)
        self.assertIn("inconclusive", text)
        self.assertIn("sequence-on", text)

    def test_the_borrowed_credential_is_deleted_even_on_failure(self) -> None:
        source = (REPO / "tools" / "title-ownership-drill").read_text(encoding="utf-8")
        body = source.split("def run_variant(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("finally:", body)
        self.assertIn("borrowed.unlink()", body)
        # The unlink has to sit in the finally, not before it.
        self.assertLess(body.index("finally:"), body.index("borrowed.unlink()"))


class TabTitleOwnershipTests(unittest.TestCase):
    def test_kit_never_sets_the_claude_title_off_switch(self) -> None:
        """The switch that silences the vendor also silences us.

        Turning it on would leave every kit-launched Claude window with a tab
        the kit cannot write either -- the exact failure the drill measured.
        Documentation and the drill itself may name the variable; nothing that
        ships may set it.
        """
        offenders: list[str] = []
        pattern = re.compile(
            r"(?:export\s+|^\s*|;\s*|&&\s*|\benv\s+)CLAUDE_CODE_DISABLE_TERMINAL_TITLE\s*="
        )
        for tree in SHIPPED_TREES:
            root = REPO / tree
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        offenders.append(f"{path.relative_to(REPO)}:{number}")
        self.assertEqual(
            offenders,
            [],
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 silences the kit's own title "
            "hook too (drill, 2026-08-13, Claude Code 2.1.229). Set here: "
            + ", ".join(offenders),
        )

    def test_tab_title_writes_the_name_as_an_osc_sequence(self) -> None:
        written = run_on_pty(
            f'source "{REPO}/bin/session_kit_common"; sk_tab_title "Log Review"'
        )
        self.assertIn(b"\x1b]0;Log Review\x07", written)

    def test_tab_title_kill_switch_writes_nothing(self) -> None:
        written = run_on_pty(
            f'source "{REPO}/bin/session_kit_common"; sk_tab_title "Log Review"',
            env={"SESSION_KIT_TAB_TITLE": "off"},
        )
        self.assertEqual(OSC_TITLE.findall(written), [])

    def test_tab_title_scrubs_control_bytes_and_bounds_the_name(self) -> None:
        """A session title is provider metadata; it can carry terminal commands."""
        written = run_on_pty(
            f'source "{REPO}/bin/session_kit_common"; '
            "sk_tab_title \"$(printf 'ev\\007il\\033]0;pwned\\007 %s' "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')\""
        )
        titles = OSC_TITLE.findall(written)
        self.assertEqual(len(titles), 1, written)
        self.assertNotIn(b"\x1b", titles[0])
        self.assertNotIn(b"\x07", titles[0])
        self.assertLessEqual(len(titles[0]), 64)

    def test_tab_title_stays_silent_without_a_terminal(self) -> None:
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{REPO}/bin/session_kit_common"; sk_tab_title "Name"',
            ],
            capture_output=True,
        )
        self.assertEqual(result.stdout, b"")

    def test_attach_puts_the_kit_name_on_the_tab(self) -> None:
        """`sp go`, `sp takeover` and a fresh `sp new` all end in attach_id."""
        source = (REPO / "lib" / "sh" / "sp_core.sh").read_text(encoding="utf-8")
        attach = source.split("attach_id() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("sk_tab_title", attach)
        self.assertIn("SK_TITLE", attach)

    def test_no_surface_writes_a_title_behind_the_kill_switch(self) -> None:
        """One writer, so one switch covers every surface.

        The login picker used to write the tab with its own `printf ESC]0;`,
        which honoured neither the kill switch nor the scrub -- so the docs
        claimed a coverage the code did not have.
        """
        offenders: list[str] = []
        for tree in ("bin", "lib", "bashrc"):
            for path in sorted((REPO / tree).rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                if path.name == "session_kit_common":
                    continue  # the one writer
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if "\\033]0;" in line or "\\e]0;" in line or "\\x1b]0;" in line:
                        offenders.append(f"{path.relative_to(REPO)}:{number}")
        self.assertEqual(
            offenders, [], "raw tab-title writes bypass the kill switch and the scrub"
        )


class TabTitleSurfaceTests(unittest.TestCase):
    """Every way into a session writes the kit's name, on both providers.

    These drive the real shell functions on a real pty behind stubs, rather
    than reading the source: a claim about which surfaces are covered is worth
    exactly as much as the run that shows it.
    """

    def surface_script(
        self, base: Path, *, function: str, provider: str, title: str, number: str
    ) -> str:
        stub = base / "fake-shpool"
        stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        stub.chmod(0o755)
        core = base / "fake-core"
        core.write_text("#!/usr/bin/env bash\nexit 0\n")
        core.chmod(0o755)
        state = base / "state"
        state.mkdir(exist_ok=True)
        return (
            f'export SESSION_KIT_SHPOOL_CMD="{stub}"\n'
            f'export SESSION_KIT_INVENTORY_CORE="{core}"\n'
            f'export SESSION_KIT_STATE_DIR="{state}"\n'
            f'source "{REPO}/bin/session_kit_common"\n'
            f'INVENTORY_CORE="{core}"\n'
            f'source "{REPO}/lib/sh/sp_core.sh"\n'
            f'source "{REPO}/lib/sh/sp_picker.sh"\n'
            f'SK_TITLE="{title}"; SK_PROVIDER="{provider}"; SK_NUMBER="{number}"\n'
            f'SK_PROOF_PROVIDER="{provider}"; SK_PROOF_UUID=""\n'
            f"{function} drill-session /tmp\n"
        )

    def run_surface(self, **kwargs) -> list[bytes]:
        env = kwargs.pop("env", None)
        with tempfile.TemporaryDirectory(prefix=".tab-surface-", dir=REPO) as raw:
            written = run_on_pty(self.surface_script(Path(raw), **kwargs), env=env)
        return OSC_TITLE.findall(written)

    def test_open_and_attach_write_the_name_on_both_providers(self) -> None:
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider, surface="sp go / sp new / attach"):
                titles = self.run_surface(
                    function="attach_id",
                    provider=provider,
                    title="Log Review",
                    number="3",
                )
                self.assertEqual([b"session 3 (Log Review)"], titles)

    def test_picker_open_and_takeover_write_the_name_on_both_providers(self) -> None:
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider, surface="picker open / takeover"):
                titles = self.run_surface(
                    function="picker_attach_id",
                    provider=provider,
                    title="Log Review",
                    number="3",
                )
                self.assertEqual([b"session 3 (Log Review)"], titles)

    def test_an_unnamed_session_still_gets_a_tab_it_can_be_found_by(self) -> None:
        titles = self.run_surface(
            function="attach_id", provider="codex", title="", number="7"
        )
        self.assertEqual([b"session 7"], titles)

    def test_rename_writes_no_tab_title(self) -> None:
        """Renaming is done from a different terminal than the session's.

        `sp name` runs at a prompt, not inside the window it renames, so a tab
        write there would label the wrong terminal. The name reaches the live
        window through the provider's own hook and, when that cannot apply it,
        through the one safe restart. Completing the surface matrix: this is
        the moment the kit deliberately does NOT write.
        """
        commands = (REPO / "lib" / "sh" / "sp_commands.sh").read_text(encoding="utf-8")
        rename = commands.split("name_target() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("sk_tab_title", rename)
        self.assertIn("sk_refresh_provider_title", rename)

    def test_restore_writes_no_tab_title(self) -> None:
        """A restore ends at the picker or a prompt, not inside the session."""
        sessions = (REPO / "lib" / "sh" / "sp_sessions.sh").read_text(encoding="utf-8")
        restore = sessions.split("restore_exact() {", 1)[1]
        self.assertNotIn("sk_tab_title", restore)
        # What it does instead: both identity writes before the launch.
        self.assertIn('"$INVENTORY_CORE" alias push', restore)
        self.assertIn('"$INVENTORY_CORE" color propagate', restore)

    def test_the_kill_switch_covers_every_surface(self) -> None:
        for function in ("attach_id", "picker_attach_id"):
            with self.subTest(function=function):
                titles = self.run_surface(
                    function=function,
                    provider="claude",
                    title="Log Review",
                    number="3",
                    env={"SESSION_KIT_TAB_TITLE": "off"},
                )
                self.assertEqual([], titles)

    def test_the_kill_switch_is_read_the_same_way_everywhere(self) -> None:
        """Doctor calls it off case-insensitively; so does the writer."""
        for value in ("off", "OFF", "Off", " off "):
            with self.subTest(value=value):
                written = run_on_pty(
                    f'source "{REPO}/bin/session_kit_common"; sk_tab_title "Name"',
                    env={"SESSION_KIT_TAB_TITLE": value},
                )
                self.assertEqual([], OSC_TITLE.findall(written))

    def test_a_crash_reopen_carries_the_same_title_items(self) -> None:
        """Otherwise that one window repaints its tab from the personal config.

        A Codex provider that crashes is reopened in place, and the reopen
        builds its own command line. Without the kit's items, Codex writes the
        personal config's title over the name attach_id had just set, and that
        window disagrees with the picker until it is closed and opened again.
        """
        import importlib.util
        import sys

        if "session_inventory" in sys.modules:
            core = sys.modules["session_inventory"]
        else:
            spec = importlib.util.spec_from_file_location(
                "session_inventory", REPO / "lib" / "session_inventory.py"
            )
            assert spec is not None and spec.loader is not None
            core = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = core
            spec.loader.exec_module(core)

        with tempfile.TemporaryDirectory(prefix=".reopen-title-", dir=REPO) as raw:
            codex_home = Path(raw) / "codex"
            (codex_home / "session-kit").mkdir(parents=True, mode=0o700)
            environment = {"SESSION_KIT_CODEX_HOME": os.fspath(codex_home)}
            # No template deployed: the built-in value.
            self.assertEqual(
                '["activity", "thread"]', core._codex_title_items(environment)
            )
            # A deployed template decides instead.
            (codex_home / "session-kit" / "terminal-title.toml").write_text(
                '[tui]\nterminal_title = ["thread", "project"]\n', encoding="utf-8"
            )
            self.assertEqual(
                '["thread", "project"]', core._codex_title_items(environment)
            )
            # A damaged template falls back rather than breaking the reopen.
            (codex_home / "session-kit" / "terminal-title.toml").write_text(
                '[tui]\nterminal_title = ["thread]\n', encoding="utf-8"
            )
            self.assertEqual(
                '["activity", "thread"]', core._codex_title_items(environment)
            )
            # And the kill switch reaches this path too.
            self.assertEqual(
                "",
                core._codex_title_items(
                    {**environment, "SESSION_KIT_TAB_TITLE": "off"}
                ),
            )

        source = (REPO / "lib" / "session_inventory.py").read_text(encoding="utf-8")
        reopen = source.split('lifecycle_action == "reopen"', 1)[1]
        self.assertIn("tui.terminal_title=", reopen.split("subprocess.run", 1)[0])

    def test_the_login_picker_restores_its_own_name_through_the_writer(self) -> None:
        actions = (REPO / "lib" / "sh" / "shpool_login_actions.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('sk_tab_title "$title"', actions)
        self.assertIn('sk_tab_title "session kit"', actions)

    def test_a_c1_control_byte_cannot_end_the_title_string(self) -> None:
        """0x9C is ST and 0x9B is CSI: C1 bytes are terminal commands too."""
        written = run_on_pty(
            f'source "{REPO}/bin/session_kit_common"; '
            "sk_tab_title \"$(printf 'A\\234\\2330;pwned\\234 B')\""
        )
        titles = OSC_TITLE.findall(written)
        self.assertEqual(1, len(titles), written)
        for byte in (0x9C, 0x9B, 0x1B, 0x07):
            self.assertNotIn(bytes([byte]), titles[0])

    def test_codex_launch_carries_the_kit_title_override(self) -> None:
        bashrc = (REPO / "bashrc" / "shpool.bashrc").read_text(encoding="utf-8")
        # Command lines wrap; judge each whole command, not each screen line.
        joined = bashrc.replace("\\\n", " ")
        invocations = [
            line
            for line in joined.splitlines()
            if "__sk_codex_theme[@]" in line and "codex " in line
        ]
        self.assertTrue(invocations)
        for line in invocations:
            self.assertIn(
                "__sk_codex_title[@]",
                line,
                "every kit Codex launch passes the kit-owned title items",
            )

    def test_the_item_set_excludes_what_codex_rejects(self) -> None:
        """Three ids Codex rejects were once listed here as valid."""
        self.assertTrue(CODEX_TITLE_ITEMS.isdisjoint(CODEX_TITLE_ITEMS_REJECTED))
        doctor = (REPO / "lib" / "sh" / "session_kit_doctor.sh").read_text(
            encoding="utf-8"
        )
        shipped = doctor.split("CODEX_TITLE_ITEMS = {", 1)[1].split("}", 1)[0]
        shipped_items = {
            piece.strip().strip('"').strip("'")
            for piece in shipped.replace("\n", " ").split(",")
            if piece.strip()
        }
        self.assertEqual(CODEX_TITLE_ITEMS, shipped_items)
        for rejected in CODEX_TITLE_ITEMS_REJECTED:
            self.assertNotIn(rejected, shipped_items)

    @unittest.skipIf(tomllib is None, "tomllib arrived in Python 3.11")
    def test_deployed_template_names_only_items_codex_accepts(self) -> None:
        template = REPO / "config" / "codex" / "terminal-title.toml"
        parsed = tomllib.loads(template.read_text(encoding="utf-8"))
        items = parsed["tui"]["terminal_title"]
        self.assertTrue(items)
        for item in items:
            self.assertIn(item, CODEX_TITLE_ITEMS)
        self.assertIn("thread", items, "the tab has to carry the kit's name")

    def launcher_parse(self, text: str) -> subprocess.CompletedProcess:
        """Run the launcher's own extraction against one template."""
        bashrc = (REPO / "bashrc" / "shpool.bashrc").read_text(encoding="utf-8")
        program = bashrc.split("<<'SKTITLE' 2>/dev/null\n", 1)[1].split("SKTITLE", 1)[0]
        with tempfile.NamedTemporaryFile(
            "w", suffix=".toml", dir=REPO, delete=False
        ) as handle:
            handle.write(text)
            path = handle.name
        try:
            return subprocess.run(
                ["python3", "-c", program, path], capture_output=True, text=True
            )
        finally:
            os.unlink(path)

    def test_a_malformed_template_never_reaches_the_command_line(self) -> None:
        """`["thread]` passes any regex and stops Codex from starting."""
        for broken in (
            'terminal_title = ["thread]\n',
            '[tui]\nterminal_title = ["thread]\n',
            '[tui]\nterminal_title = "thread"\n',
            "[tui]\nterminal_title = []\n",
            '[tui]\nterminal_title = ["thread", 7]\n',
            '[tui]\nterminal_title = ["thread; rm -rf /"]\n',
            '[tui]\nterminal_title = ["THREAD"]\n',
            "not toml at all\n",
        ):
            with self.subTest(template=broken.strip()[:40]):
                result = self.launcher_parse(broken)
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertEqual("", result.stdout.strip())

    def test_a_good_template_is_re_emitted_canonically(self) -> None:
        result = self.launcher_parse(
            "# a comment\n[tui]\nterminal_title = [ 'activity' , 'thread' ]\n"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('["activity", "thread"]', result.stdout.strip())

    def test_the_shipped_template_parses_through_the_launcher(self) -> None:
        result = self.launcher_parse(
            (REPO / "config" / "codex" / "terminal-title.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('["activity", "thread"]', result.stdout.strip())

    def test_launcher_reads_the_deployed_template(self) -> None:
        bashrc = (REPO / "bashrc" / "shpool.bashrc").read_text(encoding="utf-8")
        self.assertIn("session-kit/terminal-title.toml", bashrc)
        self.assertIn("import tomllib", bashrc)

    @unittest.skipIf(tomllib is None, "tomllib arrived in Python 3.11")
    def test_installer_deploys_the_template_without_touching_config_toml(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".tab-install-", dir=REPO) as raw:
            base = Path(raw)
            release = base / "install" / "releases" / "r1"
            (release / "config" / "codex").mkdir(parents=True)
            shutil.copyfile(
                REPO / "config" / "codex" / "terminal-title.toml",
                release / "config" / "codex" / "terminal-title.toml",
            )
            codex_home = base / "codex"
            codex_home.mkdir(mode=0o700)
            personal = codex_home / "config.toml"
            personal.write_text('[tui]\nterminal_title = ["spinner", "cwd"]\n')
            before = personal.read_text()
            script = (
                f'install_root="{base / "install"}"\n'
                f'source "{REPO}/lib/sh/session_kit_install.sh"\n'
                "install_codex_terminal_title r1\n"
            )
            subprocess.run(
                ["bash", "-c", script],
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "SESSION_KIT_CODEX_HOME": os.fspath(codex_home),
                    "HOME": os.fspath(base),
                },
            )
            deployed = codex_home / "session-kit" / "terminal-title.toml"
            self.assertTrue(deployed.is_file())
            self.assertEqual(
                tomllib.loads(deployed.read_text())["tui"]["terminal_title"],
                tomllib.loads(
                    (REPO / "config" / "codex" / "terminal-title.toml").read_text()
                )["tui"]["terminal_title"],
            )
            self.assertEqual(personal.read_text(), before)

    def test_installer_leaves_an_unowned_or_shared_destination_alone(self) -> None:
        """The refusal rule it states, enforced: type alone was checked."""
        with tempfile.TemporaryDirectory(prefix=".tab-modes-", dir=REPO) as raw:
            base = Path(raw)
            release = base / "install" / "releases" / "r1"
            (release / "config" / "codex").mkdir(parents=True)
            shutil.copyfile(
                REPO / "config" / "codex" / "terminal-title.toml",
                release / "config" / "codex" / "terminal-title.toml",
            )
            codex_home = base / "codex"
            (codex_home / "session-kit").mkdir(parents=True, mode=0o700)
            destination = codex_home / "session-kit" / "terminal-title.toml"
            destination.write_text("# somebody else's file\n")
            destination.chmod(0o666)
            script = (
                f'install_root="{base / "install"}"\n'
                f'source "{REPO}/lib/sh/session_kit_install.sh"\n'
                "install_codex_terminal_title r1\n"
            )
            subprocess.run(
                ["bash", "-c", script],
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "SESSION_KIT_CODEX_HOME": os.fspath(codex_home),
                    "HOME": os.fspath(base),
                },
            )
            self.assertEqual("# somebody else's file\n", destination.read_text())

    def test_a_template_that_cannot_be_written_fails_the_install(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".tab-fail-", dir=REPO) as raw:
            base = Path(raw)
            release = base / "install" / "releases" / "r1"
            (release / "config" / "codex").mkdir(parents=True)
            shutil.copyfile(
                REPO / "config" / "codex" / "terminal-title.toml",
                release / "config" / "codex" / "terminal-title.toml",
            )
            codex_home = base / "codex"
            deployed = codex_home / "session-kit"
            deployed.mkdir(parents=True, mode=0o700)
            script = (
                f'install_root="{base / "install"}"\n'
                f'source "{REPO}/lib/sh/session_kit_install.sh"\n'
                "install_codex_terminal_title r1\n"
            )
            try:
                deployed.chmod(0o500)  # readable, not writable: mkstemp fails
                result = subprocess.run(
                    ["bash", "-c", script],
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "SESSION_KIT_CODEX_HOME": os.fspath(codex_home),
                        "HOME": os.fspath(base),
                    },
                )
            finally:
                deployed.chmod(0o700)
            self.assertNotEqual(0, result.returncode, result.stdout)
            self.assertIn("could not be written", result.stderr)

    def test_installer_leaves_a_symlinked_destination_alone(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".tab-symlink-", dir=REPO) as raw:
            base = Path(raw)
            release = base / "install" / "releases" / "r1"
            (release / "config" / "codex").mkdir(parents=True)
            shutil.copyfile(
                REPO / "config" / "codex" / "terminal-title.toml",
                release / "config" / "codex" / "terminal-title.toml",
            )
            codex_home = base / "codex"
            (codex_home / "session-kit").mkdir(parents=True, mode=0o700)
            elsewhere = base / "elsewhere.toml"
            elsewhere.write_text("# not ours\n")
            link = codex_home / "session-kit" / "terminal-title.toml"
            link.symlink_to(elsewhere)
            script = (
                f'install_root="{base / "install"}"\n'
                f'source "{REPO}/lib/sh/session_kit_install.sh"\n'
                "install_codex_terminal_title r1\n"
            )
            subprocess.run(
                ["bash", "-c", script],
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "SESSION_KIT_CODEX_HOME": os.fspath(codex_home),
                    "HOME": os.fspath(base),
                },
            )
            self.assertTrue(link.is_symlink())
            self.assertEqual(elsewhere.read_text(), "# not ours\n")


if __name__ == "__main__":
    unittest.main()
