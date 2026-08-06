"""Session colors: stable identity hash, overrides, provider-native push."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tests.support import REPO, THEME_COLORS

CORE_PATH = REPO / "lib" / "session_inventory.py"
if "session_inventory" in sys.modules:
    inventory_core = sys.modules["session_inventory"]
else:
    CORE_SPEC = importlib.util.spec_from_file_location(
        "session_inventory", CORE_PATH
    )
    assert CORE_SPEC is not None and CORE_SPEC.loader is not None
    inventory_core = importlib.util.module_from_spec(CORE_SPEC)
    sys.modules[CORE_SPEC.name] = inventory_core
    CORE_SPEC.loader.exec_module(inventory_core)


def uuid_for(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


class SessionColorTests(unittest.TestCase):
    def test_session_color_is_stable_and_override_wins(self) -> None:
        exact = uuid_for(61)
        first = inventory_core.session_color("codex", exact)
        second = inventory_core.session_color("codex", exact)
        self.assertEqual(first, second)
        self.assertIn(first, inventory_core.CODEX_SESSION_COLORS)
        self.assertIn(
            inventory_core.session_color("claude", exact),
            inventory_core.CLAUDE_SESSION_COLORS,
        )
        wanted = next(
            color
            for color in inventory_core.CODEX_SESSION_COLORS
            if color != first
        )
        override = {"codex:" + exact: wanted}
        self.assertEqual(
            wanted, inventory_core.session_color("codex", exact, override)
        )
        self.assertIsNone(inventory_core.session_color("shell", exact))
        self.assertIsNone(inventory_core.session_color("codex", "bad-uuid"))

    def test_valid_colors_rejects_malformed_entries(self) -> None:
        exact = uuid_for(62)
        cleaned = inventory_core._valid_colors(
            {
                f"codex:{exact}": "sand",
                f"claude:{exact}": "plaid",
                "codex:not-a-uuid": "red",
                "unknown:" + exact: "blue",
                42: "green",
            }
        )
        self.assertEqual({f"codex:{exact}": "sand"}, cleaned)

    def test_color_command_set_effective_delete_and_claude_push(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            base = Path(raw)
            home = base / "home"
            project_dir = home / ".claude" / "projects" / "-srv-project"
            project_dir.mkdir(parents=True, mode=0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_file = base / "inventory.json"
            config_file.write_text(
                json.dumps({"schema_version": 1, "aliases": {}}),
                encoding="utf-8",
            )
            config_file.chmod(0o600)
            config = {
                "state_dir": state,
                "max_proc_nodes": 8192,
                "max_proc_depth": 32,
            }
            exact = uuid_for(63)
            transcript = project_dir / f"{exact}.jsonl"
            transcript.write_text(
                '{"type":"user","sessionId":"%s"}\n' % exact, encoding="utf-8"
            )
            environment = {
                "SESSION_KIT_CONFIG": os.fspath(config_file),
                "HOME": os.fspath(home),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    inventory_core._color_command(
                        argparse.Namespace(
                            color_action="set",
                            provider="claude",
                            uuid=exact,
                            color="pink",
                        ),
                        dict(config),
                    )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(
                    {"claude:" + exact: "pink"}, payload["colors"]
                )
                self.assertEqual(
                    ["claude-transcript-color"],
                    payload["provider_color_pushes"],
                )
                lines = transcript.read_text(encoding="utf-8").splitlines()
                appended = json.loads(lines[-1])
                self.assertEqual(
                    {
                        "type": "agent-color",
                        "agentColor": "pink",
                        "sessionId": exact,
                    },
                    appended,
                )
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = inventory_core._color_command(
                        argparse.Namespace(
                            color_action="effective",
                            provider="claude",
                            uuid=exact,
                        ),
                        dict(config),
                    )
                self.assertEqual(0, code)
                self.assertEqual("pink", stdout.getvalue().strip())
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    inventory_core._color_command(
                        argparse.Namespace(
                            color_action="delete", provider="claude", uuid=exact
                        ),
                        dict(config),
                    )
                payload = json.loads(stdout.getvalue())
                self.assertEqual({}, payload["colors"])
                hash_color = inventory_core.session_color("claude", exact)
                self.assertEqual(
                    hash_color,
                    json.loads(
                        transcript.read_text(encoding="utf-8").splitlines()[-1]
                    )["agentColor"],
                )

    def test_codex_color_push_is_a_clean_no_op(self) -> None:
        result = inventory_core.propagate_provider_color(
            "codex", uuid_for(64), "sand", environ={"HOME": "/nonexistent"}
        )
        self.assertEqual([], result["provider_color_pushes"])
        self.assertEqual([], result["provider_color_warnings"])

    def test_color_push_fails_open_without_transcript(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            home = Path(raw) / "home"
            (home / ".claude" / "projects").mkdir(parents=True, mode=0o700)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = inventory_core.propagate_provider_color(
                    "claude",
                    uuid_for(65),
                    "red",
                    environ={"HOME": os.fspath(home)},
                )
            self.assertEqual([], result["provider_color_pushes"])
            self.assertEqual(1, len(result["provider_color_warnings"]))
            self.assertIn("session inventory:", stderr.getvalue())

    def test_inventory_sessions_carry_display_color(self) -> None:
        exact = uuid_for(66)
        overrides = {"codex:" + exact: "sea"}
        self.assertEqual(
            "sea",
            inventory_core.session_color("codex", exact, overrides),
        )
        self.assertEqual(
            inventory_core.session_color("codex", exact),
            inventory_core.session_color(
                "codex", exact, {"codex:" + uuid_for(67): "red"}
            ),
        )

    def test_launch_color_is_deterministic_and_valid(self) -> None:
        first = inventory_core.launch_color_for("s20260731-172651-3345413")
        second = inventory_core.launch_color_for("s20260731-172651-3345413")
        self.assertEqual(first, second)
        # A launch color is picked before Codex has booted far enough to have a
        # conversation ID, so it can only ever come from the Codex palette.
        self.assertIn(first, inventory_core.CODEX_SESSION_COLORS)
        self.assertIsNone(inventory_core.record_launch_color({}, "../evil"))
        self.assertIsNone(inventory_core.record_launch_color({}, ""))

    def test_launch_reservations_serialize_and_repeat_only_when_full(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".color-race-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            config = {"state_dir": state}
            barrier = threading.Barrier(2)
            colors: list[str | None] = []

            def reserve(name: str) -> None:
                barrier.wait()
                colors.append(inventory_core.record_launch_color(config, name))

            threads = [
                threading.Thread(target=reserve, args=("new-one",)),
                threading.Thread(target=reserve, args=("new-two",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(2, len(set(colors)))
            first = inventory_core.record_launch_color(config, "repeat", {"lime"})
            repeated = inventory_core.record_launch_color(
                config, "repeat", inventory_core.CODEX_SESSION_COLORS
            )
            self.assertEqual(first, repeated)
            preferred = inventory_core.launch_color_for("full-palette")
            self.assertEqual(
                preferred,
                inventory_core.launch_color_for(
                    "full-palette", inventory_core.CODEX_SESSION_COLORS
                ),
            )
            # Claude colors are not Codex colors, so a full Claude palette
            # leaves every Codex launch color free.
            self.assertEqual(
                preferred,
                inventory_core.launch_color_for(
                    "full-palette", inventory_core.CLAUDE_SESSION_COLORS
                ),
            )

    def test_conversation_pick_persists_the_prebaked_claude_override(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".conversation-color-", dir=REPO) as raw:
            base = Path(raw)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_file = base / "inventory.json"
            config_file.write_text('{"schema_version":1,"aliases":{}}\n')
            config_file.chmod(0o600)
            config = {"state_dir": state}
            exact = uuid_for(72)
            live = {"source": "live", "stale": False, "sessions": [], "outside_agents": []}
            with (
                mock.patch.dict(os.environ, {"SESSION_KIT_CONFIG": str(config_file)}),
                mock.patch.object(inventory_core, "snapshot", return_value=live),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                code = inventory_core._color_command(
                    argparse.Namespace(
                        color_action="conversation-pick",
                        provider="claude",
                        uuid=exact,
                    ),
                    config,
                )
            payload = json.loads(output.getvalue())
            document = json.loads(config_file.read_text())
            self.assertEqual(0, code)
            self.assertEqual(payload["color"], document["colors"][f"claude:{exact}"])

            with (
                mock.patch.dict(os.environ, {"SESSION_KIT_CONFIG": str(config_file)}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                mismatch = inventory_core._color_command(
                    argparse.Namespace(
                        color_action="conversation-release",
                        provider="claude",
                        uuid=exact,
                        color=next(
                            item
                            for item in inventory_core.CLAUDE_SESSION_COLORS
                            if item != payload["color"]
                        ),
                    ),
                    config,
                )
            self.assertEqual(1, mismatch)
            self.assertEqual(
                payload["color"],
                json.loads(config_file.read_text())["colors"][f"claude:{exact}"],
            )
            with (
                mock.patch.dict(os.environ, {"SESSION_KIT_CONFIG": str(config_file)}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                released = inventory_core._color_command(
                    argparse.Namespace(
                        color_action="conversation-release",
                        provider="claude",
                        uuid=exact,
                        color=payload["color"],
                    ),
                    config,
                )
            self.assertEqual(0, released)
            self.assertNotIn(
                f"claude:{exact}",
                json.loads(config_file.read_text()).get("colors", {}),
            )

    def test_launch_color_marker_is_adopted_into_override(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".launch-", dir=REPO) as raw:
            base = Path(raw)
            home = base / "home"
            home.mkdir()
            state = base / "state"
            state.mkdir(mode=0o700)
            config_file = base / "inventory.json"
            config_file.write_text(
                json.dumps({"schema_version": 1, "aliases": {}}),
                encoding="utf-8",
            )
            config_file.chmod(0o600)
            config = {
                "state_dir": state,
                "max_proc_nodes": 8192,
                "max_proc_depth": 32,
            }
            shpool_id = "s20260731-170000-1234567"
            exact = uuid_for(71)
            environment = {
                "SESSION_KIT_CONFIG": os.fspath(config_file),
                "HOME": os.fspath(home),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                color = inventory_core.record_launch_color(config, shpool_id)
                self.assertIn(color, inventory_core.CODEX_SESSION_COLORS)
                marker = state / "launch-color" / shpool_id
                self.assertEqual(color, marker.read_text().strip())
                sessions = [
                    {
                        "provider": "codex",
                        "shpool_id": shpool_id,
                        "identity": {"uuid": exact},
                    }
                ]
                adopted = inventory_core._adopt_launch_colors(
                    config, sessions, {}
                )
                self.assertEqual(color, adopted.get(f"codex:{exact}"))
                self.assertFalse(marker.exists())
                # A second run with the override in place changes nothing.
                again = inventory_core._adopt_launch_colors(
                    config, sessions, adopted
                )
                self.assertEqual(adopted, again)
                # An existing explicit override outranks a fresh marker.
                inventory_core.record_launch_color(config, shpool_id)
                kept = inventory_core._adopt_launch_colors(
                    config,
                    sessions,
                    {f"codex:{exact}": "sand"},
                )
                self.assertEqual("sand", kept[f"codex:{exact}"])
                self.assertFalse(marker.exists())


class PaletteSplitTests(unittest.TestCase):
    """The two palettes, and why a caller must say which provider it means."""

    def test_claude_palette_is_exactly_what_the_provider_accepts(self) -> None:
        # Measured against Claude Code 2.1.223: twenty-two names probed with
        # known-good and known-bad controls, every other name rejected, and
        # gray/grey resolving to `default`, which is no color. Changing this
        # tuple means re-probing the provider, not editing a preference.
        self.assertEqual(
            ("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan"),
            inventory_core.CLAUDE_SESSION_COLORS,
        )

    def test_the_two_palettes_share_no_name(self) -> None:
        claude = set(inventory_core.CLAUDE_SESSION_COLORS)
        codex = set(inventory_core.CODEX_SESSION_COLORS)
        self.assertEqual(set(), claude & codex)
        self.assertEqual(
            len(claude) + len(codex), len(set(inventory_core.SESSION_COLORS))
        )
        self.assertEqual(
            inventory_core.CLAUDE_SESSION_COLORS + inventory_core.CODEX_SESSION_COLORS,
            inventory_core.SESSION_COLORS,
        )

    def test_every_codex_color_ships_the_theme_that_renders_it(self) -> None:
        # A Codex name without its theme file leaves the window untinted, so
        # the palette and config/codex-themes have to agree exactly.
        themes = REPO / "config" / "codex-themes"
        for color in inventory_core.CODEX_SESSION_COLORS:
            self.assertTrue((themes / f"sk-{color}.tmTheme").is_file(), color)
        # The eight Claude-named themes stay shipped so a rollback to a release
        # that still assigns them finds them.
        for color in inventory_core.CLAUDE_SESSION_COLORS:
            self.assertTrue((themes / f"sk-{color}.tmTheme").is_file(), color)

    def test_a_provider_without_a_palette_is_handed_nothing(self) -> None:
        # Empty rather than a default: a caller that arrives without a real
        # provider has no color to assign, and a populated palette would let it
        # assign one the provider cannot display.
        self.assertEqual(
            inventory_core.CLAUDE_SESSION_COLORS,
            inventory_core.palette_for_provider("claude"),
        )
        self.assertEqual(
            inventory_core.CODEX_SESSION_COLORS,
            inventory_core.palette_for_provider("codex"),
        )
        self.assertEqual((), inventory_core.palette_for_provider("shell"))
        self.assertEqual((), inventory_core.palette_for_provider(""))
        self.assertEqual((), inventory_core.palette_for_provider("CLAUDE"))


class FirstFreeColorTests(unittest.TestCase):
    """Keep the identity-hash color unless a live same-provider session has it."""

    PALETTE = ("one", "two", "three")

    def free(self, preferred: str, occupied: object = (), palette: object = None):
        return inventory_core.first_free_color(
            preferred,
            occupied,
            palette=self.PALETTE if palette is None else palette,
        )

    def test_a_free_preference_is_kept(self) -> None:
        self.assertEqual("two", self.free("two"))
        self.assertEqual("two", self.free("two", {"one", "three"}))

    def test_a_taken_preference_takes_the_next_free_name_in_order(self) -> None:
        self.assertEqual("two", self.free("one", {"one"}))
        self.assertEqual("three", self.free("one", {"one", "two"}))

    def test_the_search_wraps_past_the_end_of_the_palette(self) -> None:
        self.assertEqual("one", self.free("three", {"three"}))
        self.assertEqual("two", self.free("three", {"three", "one"}))

    def test_occupancy_outside_the_palette_is_ignored(self) -> None:
        # A Claude session holding `pink` must not push a Codex session off
        # `lime`; only same-palette names count as taken.
        self.assertEqual("one", self.free("one", {"pink", "lime", ""}))

    def test_a_preference_outside_the_palette_is_returned_unchanged(self) -> None:
        self.assertEqual("plaid", self.free("plaid", {"one"}))
        self.assertEqual("lime", self.free("lime", (), palette=()))

    def test_one_short_of_full_always_finds_the_single_free_name(self) -> None:
        # The off-by-one boundary: with exactly one name left, every possible
        # preference has to land on it.
        for palette in (
            self.PALETTE,
            inventory_core.CLAUDE_SESSION_COLORS,
            inventory_core.CODEX_SESSION_COLORS,
        ):
            for free_name in palette:
                occupied = set(palette) - {free_name}
                for preferred in palette:
                    self.assertEqual(
                        free_name,
                        inventory_core.first_free_color(
                            preferred, occupied, palette=palette
                        ),
                        (palette, free_name, preferred),
                    )

    def test_a_full_palette_repeats_the_preference_instead_of_looping(self) -> None:
        # The exhaustion boundary. There is no free color to give, so the
        # answer is the identity-hash color and the repeat is allowed. It must
        # terminate, and it must stay a function of identity rather than of
        # arrival order, so a session that has to share shares with the same
        # partner every time.
        for palette in (
            self.PALETTE,
            inventory_core.CLAUDE_SESSION_COLORS,
            inventory_core.CODEX_SESSION_COLORS,
        ):
            occupied = set(palette)
            for preferred in palette:
                self.assertEqual(
                    preferred,
                    inventory_core.first_free_color(
                        preferred, occupied, palette=palette
                    ),
                    (palette, preferred),
                )

    def test_a_whole_round_of_one_preference_uses_every_name_exactly_once(
        self,
    ) -> None:
        # The measured failure this rule exists to fix: eight Claude sessions
        # landed on seven colors, two on pink and blue unused. Every session
        # here wants the same color, and the palette still comes out fully and
        # evenly used before anything repeats.
        for palette in (
            inventory_core.CLAUDE_SESSION_COLORS,
            inventory_core.CODEX_SESSION_COLORS,
        ):
            taken: set[str] = set()
            for _ in palette:
                taken.add(
                    inventory_core.first_free_color(
                        palette[0], taken, palette=palette
                    )
                )
            self.assertEqual(set(palette), taken)
            self.assertEqual(len(palette), len(taken))
            # One more than the palette holds returns rather than hangs.
            self.assertEqual(
                palette[0],
                inventory_core.first_free_color(palette[0], taken, palette=palette),
            )


class StoredOverrideMigrationTests(unittest.TestCase):
    """A stored color outside the in-force palette migrates with no code."""

    def test_an_override_the_provider_cannot_show_is_ignored_not_corrected(
        self,
    ) -> None:
        exact = uuid_for(80)
        # `pink` was a legal Codex color before the split and is Claude-only
        # now. Sessions carrying one do not need a migration pass: the override
        # simply stops matching the palette, and the identity hash applies.
        stale = {"codex:" + exact: "pink"}
        migrated = inventory_core.session_color("codex", exact, stale)
        self.assertEqual(inventory_core.session_color("codex", exact), migrated)
        self.assertIn(migrated, inventory_core.CODEX_SESSION_COLORS)
        self.assertEqual({}, inventory_core._valid_colors(stale))

    def test_a_stale_document_reads_back_clean_from_disk(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".stale-colors-", dir=REPO) as raw:
            base = Path(raw)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_file = base / "inventory.json"
            codex_uuid = uuid_for(81)
            claude_uuid = uuid_for(82)
            keep = uuid_for(83)
            config_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {},
                        "colors": {
                            # Written by a release that shared one palette.
                            f"codex:{codex_uuid}": "pink",
                            f"claude:{claude_uuid}": "lime",
                            f"codex:{keep}": "sand",
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_file.chmod(0o600)
            config = {"state_dir": state}
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": os.fspath(config_file)}
            ):
                colors = inventory_core.canonical_colors(config)
                self.assertEqual({f"codex:{keep}": "sand"}, colors)
                self.assertEqual(
                    inventory_core.session_color("codex", codex_uuid),
                    inventory_core.session_color("codex", codex_uuid, colors),
                )
                self.assertEqual(
                    inventory_core.session_color("claude", claude_uuid),
                    inventory_core.session_color("claude", claude_uuid, colors),
                )
                self.assertEqual(
                    "sand", inventory_core.session_color("codex", keep, colors)
                )

    def test_setting_a_color_the_provider_cannot_show_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".reject-color-", dir=REPO) as raw:
            base = Path(raw)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_file = base / "inventory.json"
            config_file.write_text('{"schema_version":1,"aliases":{}}\n')
            config_file.chmod(0o600)
            config = {"state_dir": state}
            exact = uuid_for(84)
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": os.fspath(config_file)}
            ):
                # Storing it would look like it worked and then read back as
                # the hash color, so the write has to fail instead.
                with self.assertRaises(inventory_core.CollectionError):
                    inventory_core.mutate_canonical_color(
                        config, "claude", exact, "lime"
                    )
                with self.assertRaises(inventory_core.CollectionError):
                    inventory_core.mutate_canonical_color(
                        config, "codex", exact, "pink"
                    )
                self.assertEqual(
                    {f"claude:{exact}": "cyan"},
                    inventory_core.mutate_canonical_color(
                        config, "claude", exact, "cyan"
                    ),
                )


def quoted_names(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r'"([a-z-]+)"', text))


class PaletteSurfaceTests(unittest.TestCase):
    """A palette that is fourteen in Python and eight in Bash still passes
    every unit test above and still looks wrong on screen. Each check here
    parses one non-Python surface and compares it to the palette itself."""

    def section(self, relative: str, pattern: str) -> str:
        text = (REPO / relative).read_text(encoding="utf-8")
        match = re.search(pattern, text, re.DOTALL)
        self.assertIsNotNone(match, f"{relative}: {pattern} did not match")
        assert match is not None
        return match.group(1)

    def test_picker_renderer_tints_every_color(self) -> None:
        body = self.section(
            "lib/sh/shpool_login_render.sh", r"SESSION_PALETTE = \{(.*?)\n\}"
        )
        self.assertEqual(inventory_core.SESSION_COLORS, quoted_names(body))

    def test_dashboard_renderer_tints_every_color(self) -> None:
        body = self.section(
            "lib/sessionkit_inventory/render.py", r"session_palette = \(\s*\{(.*?)\n        \}"
        )
        self.assertEqual(inventory_core.SESSION_COLORS, quoted_names(body))

    def test_installer_installs_a_theme_for_every_color(self) -> None:
        body = self.section("bin/session-kit", r"codex_theme_names=\((.*?)\)")
        self.assertEqual(inventory_core.SESSION_COLORS, tuple(body.split()))

    def test_the_shared_test_theme_list_is_the_palette(self) -> None:
        # tests/test_install.py checks lifecycle theme targets against this
        # list, so it has to stay the palette rather than drift beside it.
        self.assertEqual(inventory_core.SESSION_COLORS, THEME_COLORS)

    def test_doctor_checks_a_theme_for_every_color(self) -> None:
        body = self.section("lib/sh/session_kit_doctor.sh", r"theme_names = \((.*?)\)")
        self.assertEqual(inventory_core.SESSION_COLORS, quoted_names(body))

    def test_lifecycle_expects_a_theme_for_every_color(self) -> None:
        body = self.section(
            "lib/sh/session_kit_lifecycle.sh",
            r"expected_themes = \[.*?for color in \((.*?)\)",
        )
        self.assertEqual(inventory_core.SESSION_COLORS, quoted_names(body))

    def test_sp_usage_offers_each_palette_under_its_own_provider(self) -> None:
        text = (REPO / "bin" / "sp").read_text(encoding="utf-8")
        claude = re.search(r"^\s*Claude:\s*(\S+)$", text, re.MULTILINE)
        codex = re.search(r"^\s*Codex:\s*(\S+)$", text, re.MULTILINE)
        self.assertIsNotNone(claude)
        self.assertIsNotNone(codex)
        assert claude is not None and codex is not None
        self.assertEqual(
            inventory_core.CLAUDE_SESSION_COLORS, tuple(claude.group(1).split("|"))
        )
        self.assertEqual(
            inventory_core.CODEX_SESSION_COLORS, tuple(codex.group(1).split("|"))
        )

    def test_the_claude_status_line_covers_the_claude_palette(self) -> None:
        # This surface runs inside a Claude Code window and looks the row up by
        # Claude Code's own session_id, so it can only ever be handed a Claude
        # color. It must cover those eight and needs none of the Codex names.
        text = (REPO / "config" / "claude" / "statusline.sh").read_text(
            encoding="utf-8"
        )
        cases = tuple(re.findall(r"^\s*([a-z]+>?\))\s*tint=", text, re.MULTILINE))
        named = tuple(case[:-1] for case in cases if case != "*)")
        self.assertEqual(inventory_core.CLAUDE_SESSION_COLORS, named)
        for color in inventory_core.CODEX_SESSION_COLORS:
            self.assertNotIn(f"    {color})", text)


if __name__ == "__main__":
    unittest.main()
