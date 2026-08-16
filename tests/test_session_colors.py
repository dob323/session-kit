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
import subprocess
import sys
import tempfile
import threading
import time
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

    def color_records(self, transcript: Path, uuid: str) -> list[str]:
        """Every agent-color value in a transcript, in order."""
        values: list[str] = []
        for line in transcript.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("type") == "agent-color" and item.get("sessionId") == uuid:
                values.append(item["agentColor"])
        return values

    def transcript_fixture(self, base: str, number: int) -> tuple[Path, str, dict]:
        home = Path(base) / "home"
        project_dir = home / ".claude" / "projects" / "-srv-project"
        project_dir.mkdir(parents=True, mode=0o700)
        exact = uuid_for(number)
        transcript = project_dir / f"{exact}.jsonl"
        transcript.write_text(
            '{"type":"user","sessionId":"%s"}\n' % exact, encoding="utf-8"
        )
        return transcript, exact, {"HOME": os.fspath(home)}

    def test_the_color_is_written_once_not_once_per_attach(self) -> None:
        """One record per conversation, not one per pass.

        The push runs on every attach (`sk_push_session_color`), so a session
        a person opened eight times carried eight identical records and grew
        one more on every reattach. Claude honours the last record either way,
        so the repeats never changed what was shown -- they only grew a file
        the kit does not own and cannot compact.
        """
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            transcript, exact, environ = self.transcript_fixture(raw, 81)

            for _ in range(8):
                result = inventory_core.propagate_provider_color(
                    "claude", exact, "blue", environ=environ
                )

            self.assertEqual(["blue"], self.color_records(transcript, exact))
            self.assertEqual(
                ["claude-transcript-color-current"],
                result["provider_color_pushes"],
            )

    def test_a_real_color_change_is_still_written(self) -> None:
        """Idempotence must not freeze the colour: a change is a change."""
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            transcript, exact, environ = self.transcript_fixture(raw, 82)

            for color in ("blue", "pink", "pink"):
                inventory_core.propagate_provider_color(
                    "claude", exact, color, environ=environ
                )

            # The change appends; the repeat of the new colour does not. The
            # earlier record stays -- appending is the only safe edit to a
            # transcript a live provider is also writing to.
            self.assertEqual(["blue", "pink"], self.color_records(transcript, exact))

    def test_another_conversations_record_never_counts_as_this_ones(self) -> None:
        """Records carry a sessionId, so the match has to use it."""
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            transcript, exact, environ = self.transcript_fixture(raw, 83)
            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write(
                    '{"type":"agent-color","agentColor":"blue","sessionId":"%s"}\n'
                    % uuid_for(84)
                )

            inventory_core.propagate_provider_color(
                "claude", exact, "blue", environ=environ
            )

            self.assertEqual(["blue"], self.color_records(transcript, exact))

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

                # A newer explicit choice published after the collector read
                # its empty override map also wins at the lock boundary.
                inventory_core.mutate_canonical_color(
                    config, "codex", exact, "lime"
                )
                inventory_core.record_launch_color(config, shpool_id)
                raced = inventory_core._adopt_launch_colors(config, sessions, {})
                self.assertEqual("lime", raced[f"codex:{exact}"])


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


def colliding_uuids(provider: str, count: int) -> tuple[str, list[str]]:
    """Exact UUIDs whose identity hashes genuinely land on one color.

    Searched rather than hardcoded so the fixture stays true if the palette
    order ever changes; the hash is deterministic, so the answer is stable.
    """
    groups: dict[str, list[str]] = {}
    for number in range(1, 20_000):
        exact = uuid_for(number)
        color = inventory_core.session_color(provider, exact)
        assert color is not None
        groups.setdefault(color, []).append(exact)
        if len(groups[color]) >= count:
            return color, groups[color][:count]
    raise AssertionError(f"no {count} {provider} UUIDs share a color")


class ReconcileTests(unittest.TestCase):
    """One pass that separates sessions already sharing a color."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".reconcile-", dir=REPO)
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        state = base / "state"
        state.mkdir(mode=0o700)
        self.config_file = base / "inventory.json"
        self.config_file.write_text(
            json.dumps({"schema_version": 1, "aliases": {}}), encoding="utf-8"
        )
        self.config_file.chmod(0o600)
        self.config = {"state_dir": state, "max_proc_nodes": 8192}
        patcher = mock.patch.dict(
            os.environ, {"SESSION_KIT_CONFIG": os.fspath(self.config_file)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, colors: dict[str, str]) -> None:
        document = json.loads(self.config_file.read_text(encoding="utf-8"))
        document["colors"] = colors
        self.config_file.write_text(json.dumps(document), encoding="utf-8")

    def rows(self, provider: str, uuids: list[str]) -> list[dict[str, object]]:
        return [
            {"provider": provider, "identity": {"uuid": exact}} for exact in uuids
        ]

    def effective(self, provider: str, uuids: list[str]) -> list[str]:
        stored = inventory_core.canonical_colors(self.config)
        return [
            inventory_core.session_color(provider, exact, stored) for exact in uuids
        ]

    def test_it_separates_sessions_that_share_an_identity_hash_color(self) -> None:
        shared, uuids = colliding_uuids("claude", 3)
        rows = self.rows("claude", uuids)
        self.assertEqual([shared] * 3, self.effective("claude", uuids))

        result = inventory_core.reconcile_session_colors(self.config, rows)

        after = self.effective("claude", uuids)
        self.assertEqual(3, len(set(after)), after)
        for color in after:
            self.assertIn(color, inventory_core.CLAUDE_SESSION_COLORS)
        # The first row in settle order keeps the color it already showed; only
        # the rows that had to move are recorded.
        self.assertEqual(shared, after[0])
        self.assertEqual(2, len(result["moved"]))
        self.assertNotIn(f"claude:{uuids[0]}", result["moved"])

    def test_running_it_twice_does_not_shuffle_anything(self) -> None:
        _, uuids = colliding_uuids("claude", 3)
        rows = self.rows("claude", uuids)
        inventory_core.reconcile_session_colors(self.config, rows)
        settled = self.effective("claude", uuids)
        written = self.config_file.read_bytes()

        again = inventory_core.reconcile_session_colors(self.config, rows)

        self.assertEqual({}, again["moved"])
        self.assertEqual([], again["dropped"])
        self.assertEqual(settled, self.effective("claude", uuids))
        self.assertEqual(written, self.config_file.read_bytes())

    def test_it_recollects_rows_after_taking_the_publishing_lock(self) -> None:
        _, uuids = colliding_uuids("claude", 2)
        stale_rows = self.rows("claude", uuids)
        calls: list[str] = []

        def current_rows() -> list[dict[str, object]]:
            calls.append("under-lock")
            return self.rows("claude", [uuids[0]])

        result = inventory_core.reconcile_session_colors(
            self.config,
            stale_rows,
            revalidate_sessions=current_rows,
        )

        self.assertEqual(["under-lock"], calls)
        self.assertEqual({}, result["moved"])
        self.assertEqual({}, result["colors"])

    def test_a_reversed_arrival_order_settles_the_same_way(self) -> None:
        # Settle order is the identity, not the order the snapshot happened to
        # list rows in, or a second pass over a reordered inventory would move
        # sessions that were already correct.
        _, uuids = colliding_uuids("codex", 3)
        inventory_core.reconcile_session_colors(
            self.config, self.rows("codex", uuids)
        )
        settled = self.effective("codex", uuids)
        written = self.config_file.read_bytes()

        again = inventory_core.reconcile_session_colors(
            self.config, self.rows("codex", list(reversed(uuids)))
        )

        self.assertEqual({}, again["moved"])
        self.assertEqual(settled, self.effective("codex", uuids))
        self.assertEqual(written, self.config_file.read_bytes())

    def test_more_sessions_than_colors_uses_every_color_then_repeats(self) -> None:
        palette = inventory_core.CODEX_SESSION_COLORS
        _, uuids = colliding_uuids("codex", len(palette) + 2)
        rows = self.rows("codex", uuids)

        result = inventory_core.reconcile_session_colors(self.config, rows)

        after = self.effective("codex", uuids)
        # The exhaustion boundary: every color is used exactly once before any
        # repeats, and the two rows past the end fall back to a repeat rather
        # than to no color or to a name outside the palette.
        self.assertEqual(set(palette), set(after))
        self.assertEqual(len(palette), len(set(after)))
        self.assertEqual(len(palette) + 2, len(after))
        for color in after:
            self.assertIn(color, palette)
        self.assertLessEqual(len(result["moved"]), len(uuids))

        # And it still settles: a second pass over a full palette must not
        # start rotating the repeats around.
        written = self.config_file.read_bytes()
        again = inventory_core.reconcile_session_colors(self.config, rows)
        self.assertEqual({}, again["moved"])
        self.assertEqual(after, self.effective("codex", uuids))
        self.assertEqual(written, self.config_file.read_bytes())

    def test_a_session_keeping_its_hash_color_is_not_pinned(self) -> None:
        exact = uuid_for(200)
        rows = self.rows("claude", [exact])

        result = inventory_core.reconcile_session_colors(self.config, rows)

        # No override is written for a row that never had to move, so the
        # stored set stays a list of deviations rather than a copy of the
        # inventory.
        self.assertEqual({}, result["moved"])
        self.assertEqual({}, result["colors"])
        self.assertNotIn("colors", json.loads(self.config_file.read_text()))

    def test_neither_provider_can_take_a_color_from_the_other(self) -> None:
        claude_uuid = uuid_for(201)
        codex_uuid = uuid_for(202)
        rows = [
            *self.rows("claude", [claude_uuid]),
            *self.rows("codex", [codex_uuid]),
        ]

        result = inventory_core.reconcile_session_colors(self.config, rows)

        self.assertEqual({}, result["moved"])
        self.assertIn(
            inventory_core.session_color("claude", claude_uuid),
            inventory_core.CLAUDE_SESSION_COLORS,
        )
        self.assertIn(
            inventory_core.session_color("codex", codex_uuid),
            inventory_core.CODEX_SESSION_COLORS,
        )

    def test_it_clears_stored_colors_outside_the_in_force_palette(self) -> None:
        codex_uuid = uuid_for(203)
        claude_uuid = uuid_for(204)
        keep = uuid_for(205)
        self.seed(
            {
                f"codex:{codex_uuid}": "pink",
                f"claude:{claude_uuid}": "lime",
                f"codex:{keep}": "sand",
            }
        )
        rows = self.rows("codex", [codex_uuid])

        result = inventory_core.reconcile_session_colors(self.config, rows)

        self.assertEqual(
            [f"claude:{claude_uuid}", f"codex:{codex_uuid}"], result["dropped"]
        )
        self.assertEqual({f"codex:{keep}": "sand"}, result["colors"])
        self.assertEqual(
            inventory_core.session_color("codex", codex_uuid),
            self.effective("codex", [codex_uuid])[0],
        )
        again = inventory_core.reconcile_session_colors(self.config, rows)
        self.assertEqual([], again["dropped"])

    def test_rows_without_an_exact_identity_are_skipped(self) -> None:
        exact = uuid_for(206)
        rows = [
            {"provider": "shell", "identity": {"uuid": exact}},
            {"provider": "claude", "identity": {"uuid": "not-a-uuid"}},
            {"provider": "claude"},
            {"provider": "claude", "identity": None},
            "not a row",
        ]

        result = inventory_core.reconcile_session_colors(self.config, rows)

        self.assertEqual({}, result["moved"])
        self.assertEqual({}, result["colors"])

    def test_the_command_refuses_a_stale_inventory_and_pushes_what_it_moves(
        self,
    ) -> None:
        _, uuids = colliding_uuids("claude", 2)
        rows = self.rows("claude", uuids)
        stale = {"source": "cache", "stale": True, "sessions": rows}
        with (
            mock.patch.object(inventory_core, "snapshot", return_value=stale),
            contextlib.redirect_stdout(io.StringIO()) as quiet,
            contextlib.redirect_stderr(io.StringIO()) as complaint,
        ):
            code = inventory_core._color_command(
                argparse.Namespace(color_action="reconcile"), dict(self.config)
            )
        self.assertEqual(1, code)
        self.assertEqual("", quiet.getvalue())
        self.assertIn("stale inventory", complaint.getvalue())
        self.assertNotIn("colors", json.loads(self.config_file.read_text()))

        live = {"source": "live", "stale": False, "sessions": rows}
        pushed: list[tuple[str, str, str]] = []

        def record(provider: str, uuid: str, color: str, **_: object) -> dict:
            pushed.append((provider, uuid, color))
            return {
                "provider_color_pushes": ["claude-transcript-color"],
                "provider_color_warnings": [],
            }

        with (
            mock.patch.object(inventory_core, "snapshot", return_value=live),
            mock.patch.object(
                inventory_core, "propagate_provider_color", side_effect=record
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = inventory_core._color_command(
                argparse.Namespace(color_action="reconcile"), dict(self.config)
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertEqual(1, len(payload["moved"]))
        # Only what actually moved is pushed, so an open window shows the new
        # color at its next start or resume.
        self.assertEqual(1, len(pushed))
        self.assertEqual(["claude-transcript-color"], payload["provider_color_pushes"])
        self.assertEqual([], payload["provider_color_warnings"])


def quoted_names(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r'"([a-z-]+)"', text))


class PaletteSurfaceTests(unittest.TestCase):
    """Every visual surface uses the canonical palette."""

    def section(self, relative: str, pattern: str) -> str:
        text = (REPO / relative).read_text(encoding="utf-8")
        match = re.search(pattern, text, re.DOTALL)
        self.assertIsNotNone(match, f"{relative}: {pattern} did not match")
        assert match is not None
        return match.group(1)

    def test_picker_renderer_tints_every_color(self) -> None:
        source = (REPO / "lib/sh/shpool_login_render.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "from sessionkit_inventory.render import render_picker_page", source
        )
        self.assertNotIn("SESSION_PALETTE =", source)

    def test_dashboard_renderer_tints_every_color(self) -> None:
        body = self.section(
            "lib/sessionkit_inventory/render.py", r"SESSION_PALETTE = \{(.*?)\n\}"
        )
        self.assertEqual(inventory_core.SESSION_COLORS, quoted_names(body))

    def test_installer_installs_a_theme_for_every_color(self) -> None:
        body = self.section("bin/session-kit", r"codex_theme_names=\((.*?)\)")
        self.assertEqual(inventory_core.SESSION_COLORS, tuple(body.split()))

    def test_the_shared_test_theme_list_is_the_palette(self) -> None:
        # tests/test_install.py checks lifecycle theme targets against this
        # list, so it has to stay the palette rather than drift beside it.
        self.assertEqual(inventory_core.SESSION_COLORS, THEME_COLORS)

    def test_codex_launch_accepts_every_codex_color_and_no_claude_color(self) -> None:
        body = self.section(
            "bashrc/shpool.bashrc",
            r'case "\$__sk_theme_color" in\s*(.*?)\)\s*'
            r'__sk_codex_home=',
        )
        accepted = tuple(body.split("|"))
        self.assertEqual(inventory_core.CODEX_SESSION_COLORS, accepted)
        self.assertEqual(
            set(), set(inventory_core.CLAUDE_SESSION_COLORS) & set(accepted)
        )

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

    def test_claude_status_line_hashes_cache_keys_with_sha256sum(self) -> None:
        text = (REPO / "config" / "claude" / "statusline.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("sha256sum", text)
        self.assertIn("shasum -a 256", text)
        self.assertNotRegex(text, r"python3\s+-c")

    def test_claude_status_line_uses_unique_quota_cache_tempfiles(self) -> None:
        text = (REPO / "config" / "claude" / "statusline.sh").read_text(
            encoding="utf-8"
        )
        self.assertRegex(text, r'mktemp\s+"\$QDIR/quota_headers\.tmp\.[X]+"')
        self.assertNotIn('"$QCACHE.tmp"', text)

    def test_shipped_quota_refresher_example_states_the_status_line_contract(
        self,
    ) -> None:
        example = REPO / "extras/statusline-quota-refresh.example"
        text = example.read_text(encoding="utf-8")
        self.assertTrue(example.stat().st_mode & 0o111)
        self.assertIn("TODO: Call the quota endpoint", text)
        for field in (
            "x-probe-account",
            "anthropic-ratelimit-unified-5h-utilization",
            "anthropic-ratelimit-unified-5h-reset",
            "anthropic-ratelimit-unified-7d-utilization",
            "anthropic-ratelimit-unified-7d-reset",
        ):
            self.assertIn(field, text)
        self.assertIn("$HOME/.claude/cache/quota_headers", text)
        self.assertNotRegex(text, r"https?://")

        documentation = (REPO / "config/claude/README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("extension point", documentation)
        self.assertRegex(documentation, r"at most once every 180\s+seconds")
        self.assertIn("`HOME` set to an isolated probe directory", documentation)

    def test_claude_status_line_reports_the_session_profile_account(self) -> None:
        with tempfile.TemporaryDirectory(prefix="session-kit-statusline.") as raw:
            home = Path(raw)
            profile = home / "profile"
            profile.mkdir()
            (home / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": "ambient@example.test"}}),
                encoding="utf-8",
            )
            (profile / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": "profile@example.test"}}),
                encoding="utf-8",
            )
            payload = {
                "model": {"display_name": "fixture"},
                "workspace": {"current_dir": "/tmp/project"},
                "context_window": {"used_percentage": 12},
                "session_id": "",
            }
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "CLAUDE_CONFIG_DIR": str(profile),
                    "USER": "fixture",
                }
            )
            result = subprocess.run(
                [str(REPO / "config/claude/statusline.sh")],
                input=json.dumps(payload),
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("profile@example.test", result.stdout)
            self.assertNotIn("ambient@example.test", result.stdout)

    def run_status_line(self, home: Path, sid: str) -> str:
        payload = {
            "model": {"display_name": "fixture"},
            "workspace": {"current_dir": "/tmp/project"},
            "context_window": {"used_percentage": 12},
            "session_id": sid,
        }
        environment = os.environ.copy()
        environment.update({"HOME": str(home), "USER": "fixture"})
        environment.pop("CLAUDE_CONFIG_DIR", None)
        result = subprocess.run(
            [str(REPO / "config/claude/statusline.sh")],
            input=json.dumps(payload),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_a_pushed_title_reaches_the_status_bar_with_no_restart(self) -> None:
        """End to end, on the surface the person actually watches.

        The status line re-runs every two seconds and reads the name straight
        from the intent file, so a title written to a LIVE session shows up on
        the next refresh -- no restart, no reattach. Before the push the slot
        is empty, which is exactly what a person sees today.
        """
        with tempfile.TemporaryDirectory(prefix="session-kit-statusline.") as raw:
            home = Path(raw)
            (home / ".claude" / "sessions").mkdir(parents=True, mode=0o700)
            project = home / ".claude" / "projects" / "-srv-app"
            project.mkdir(parents=True, mode=0o700)
            (home / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": "fixture@example.test"}}),
                encoding="utf-8",
            )
            exact = uuid_for(92)
            (project / f"{exact}.jsonl").write_text(
                '{"type":"user","sessionId":"%s"}\n' % exact, encoding="utf-8"
            )
            state = home / ".local/state/session-kit"
            state.mkdir(parents=True, mode=0o700)
            (state / "inventory.json").write_text(
                json.dumps(
                    {
                        "sessions": [
                            {
                                "identity": {"uuid": exact},
                                "display_color": "blue",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            before = self.run_status_line(home, exact)
            self.assertNotIn("Count the great lakes", before)

            inventory_core.propagate_provider_title(
                "claude",
                exact,
                "Count the great lakes",
                environ={"HOME": os.fspath(home)},
            )
            after = self.run_status_line(home, exact)

            # The name leads the line, tinted with the session's own colour --
            # blue is 97;166;240 in the kit's calibrated palette.
            self.assertIn("Count the great lakes", after)
            self.assertIn("\033[38;2;97;166;240m", after)
            self.assertTrue(
                after.startswith("\033[38;2;97;166;240mCount the great lakes"),
                after[:80],
            )

    def test_an_account_sessions_derived_title_reaches_the_status_bar(self) -> None:
        """The whole chain for the session the operator was looking at.

        Their session ran on an enrolled profile and had a title the PROVIDER
        derived. The kit read the provider's evidence from the default root
        only, found nothing, and therefore pushed nothing -- so the status bar
        name slot stayed empty while `sp detail` happily printed the title.
        Reading the profile is what turns the rest of the chain on.
        """
        with tempfile.TemporaryDirectory(prefix="session-kit-statusline.") as raw:
            home = Path(raw)
            (home / ".claude" / "sessions").mkdir(parents=True, mode=0o700)
            (home / ".claude" / "projects").mkdir(parents=True, mode=0o700)
            (home / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": "fixture@example.test"}}),
                encoding="utf-8",
            )
            profile = home / ".local/share/session-kit/accounts/claude/primary"
            project = profile / "projects" / "-srv-app"
            project.mkdir(parents=True, mode=0o700)
            exact = uuid_for(93)
            (project / f"{exact}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "ai-title",
                        "aiTitle": "Count the great lakes",
                        "sessionId": exact,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            state = home / ".local/state/session-kit"
            state.mkdir(parents=True, mode=0o700)
            (state / "inventory.json").write_text(
                json.dumps(
                    {
                        "sessions": [
                            {"identity": {"uuid": exact}, "display_color": "blue"}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            environ = {"HOME": os.fspath(home)}

            # What the kit can see of the provider's own title...
            signals = inventory_core.read_claude_transcript_signals(exact, home)
            # ...is what it pushes, and the push is what the bar reads.
            if signals["ai_title"]:
                inventory_core.propagate_provider_title(
                    "claude", exact, signals["ai_title"], environ=environ
                )

            self.assertEqual("Count the great lakes", signals["ai_title"])
            self.assertIn(
                "Count the great lakes", self.run_status_line(home, exact)
            )

    def test_claude_quota_caches_are_isolated_by_profile_and_alias(self) -> None:
        with tempfile.TemporaryDirectory(prefix="session-kit-quota.") as raw:
            home = Path(raw)
            claude_home = home / ".claude"
            claude_home.mkdir()
            refresh_log = home / "refresh.log"
            refresher = claude_home / "statusline-quota-refresh.sh"
            refresher.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "mkdir -p \"$HOME/.claude/cache\"\n"
                "email=$(jq -r '.oauthAccount.emailAddress' \"$HOME/.claude.json\")\n"
                "printf '%s|%s\\n' \"$email\" \"${CLAUDE_CONFIG_DIR:-default}\" >> \"$REFRESH_LOG\"\n"
                "printf 'x-probe-account: %s\\nanthropic-ratelimit-unified-5h-utilization: 0.25\\nanthropic-ratelimit-unified-5h-reset: 4102444800\\n' \"$email\" > \"$HOME/.claude/cache/quota_headers\"\n",
                encoding="utf-8",
            )
            refresher.chmod(0o700)
            profiles = []
            aliases = ("a" * 32 + "-one", "a" * 32 + "-two")
            for alias in aliases:
                profile = home / alias
                profile.mkdir()
                (profile / ".claude.json").write_text(
                    json.dumps(
                        {"oauthAccount": {"emailAddress": f"{alias}@example.test"}}
                    ),
                    encoding="utf-8",
                )
                profiles.append((alias, profile))
            payload = {
                "model": {"display_name": "fixture"},
                "workspace": {"current_dir": "/tmp/project"},
                "context_window": {"used_percentage": 12},
                "session_id": "",
            }

            def render(alias: str, profile: Path) -> subprocess.CompletedProcess[str]:
                environment = os.environ.copy()
                environment.update(
                    {
                        "HOME": str(home),
                        "CLAUDE_CONFIG_DIR": str(profile),
                        "SESSION_KIT_ACCOUNT_ALIAS": alias,
                        "REFRESH_LOG": str(refresh_log),
                        "USER": "fixture",
                    }
                )
                return subprocess.run(
                    [str(REPO / "config/claude/statusline.sh")],
                    input=json.dumps(payload),
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

            for alias, profile in profiles:
                result = render(alias, profile)
                self.assertEqual(0, result.returncode, result.stderr)
            cache_root = claude_home / "cache/session-kit-quota"
            for _ in range(100):
                caches = list(cache_root.glob("*/quota_headers"))
                if len(caches) == 2:
                    break
                time.sleep(0.02)
            self.assertEqual(2, len(caches))
            cache_keys = {path.parent.name for path in caches}
            for alias in aliases:
                self.assertTrue(
                    any(
                        re.fullmatch(re.escape(alias) + r"-[0-9a-f]{64}", key)
                        for key in cache_keys
                    ),
                    cache_keys,
                )
            self.assertEqual(
                {f"{alias}@example.test" for alias in aliases},
                {
                    re.search(r"x-probe-account: (.+)", path.read_text()).group(1)
                    for path in caches
                },
            )
            first_refreshes = refresh_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(2, len(first_refreshes))

            for alias, profile in profiles:
                result = render(alias, profile)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"{alias}@example.test", result.stdout)
                self.assertIn("5h", result.stdout)
            time.sleep(0.05)
            self.assertEqual(
                first_refreshes,
                refresh_log.read_text(encoding="utf-8").splitlines(),
            )


class TranscriptWritesSurviveTornFilesTests(unittest.TestCase):
    """Colour and name writes against transcripts a live provider owns.

    Two failures live here and both are silent, which is what makes them
    expensive: one unreadable byte used to end the naming pass for EVERY
    session, and an append onto a half-written record used to destroy that
    record, lose the colour, and still report success.
    """

    def fixture(self, base: str, number: int) -> tuple[Path, str, dict]:
        home = Path(base) / "home"
        project = home / ".claude" / "projects" / "-srv-project"
        project.mkdir(parents=True, mode=0o700)
        exact = uuid_for(number)
        transcript = project / f"{exact}.jsonl"
        transcript.write_bytes(b'{"type":"user","sessionId":"%s"}\n' % exact.encode())
        return transcript, exact, {"HOME": os.fspath(home)}

    def records(self, transcript: Path, uuid: str, kind: str, field: str) -> list:
        """Every value of one record kind, read the way a torn file allows."""
        values = []
        for raw in transcript.read_bytes().split(b"\n"):
            try:
                item = json.loads(raw.decode("utf-8", "strict"))
            except (UnicodeDecodeError, ValueError):
                continue
            if item.get("type") == kind and item.get("sessionId") == uuid:
                values.append(item[field])
        return values

    def age(self, transcript: Path, seconds: float) -> None:
        stamp = time.time() - seconds
        os.utime(transcript, (stamp, stamp))

    def test_one_unreadable_byte_does_not_stop_the_colour(self) -> None:
        """A killed writer leaves half a character; that is not an answer.

        Text mode raises UnicodeDecodeError from the iteration itself, and a
        UnicodeDecodeError is a ValueError, not an OSError -- so it escaped
        the guard, its caller, and the hydration loop that walks every
        session, and from there on nobody got a colour or a name.
        """
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            transcript, exact, environ = self.fixture(raw, 91)
            with open(transcript, "ab") as handle:
                handle.write(b'{"type":"user","text":"caf\xc3')
            self.age(transcript, 600)

            result = inventory_core.propagate_provider_color(
                "claude", exact, "blue", environ=environ
            )

            self.assertEqual(
                ["blue"], self.records(transcript, exact, "agent-color", "agentColor")
            )
            self.assertIn("claude-transcript-color", result["provider_color_pushes"])

    def test_one_unreadable_byte_does_not_stop_the_other_sessions(self) -> None:
        """The blast radius: one torn file, every other session still named.

        `bin/shpool_status` runs this pass with its output and its errors
        discarded, so anything raised here is invisible -- the symptom is
        that sessions quietly stop getting names, which is the same silence
        the operator already complained about.
        """
        names = sys.modules["sessionkit_inventory.names"]
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            torn, exact_torn, environ = self.fixture(raw, 92)
            healthy_uuid = uuid_for(93)
            healthy = torn.parent / f"{healthy_uuid}.jsonl"
            healthy.write_bytes(
                b'{"type":"user","sessionId":"%s"}\n' % healthy_uuid.encode()
            )
            with open(torn, "ab") as handle:
                handle.write(b'{"type":"user","text":"caf\xc3')
            self.age(torn, 600)
            live = {
                "sessions": [
                    {"provider": "claude", "identity": {"uuid": exact_torn}},
                    {"provider": "claude", "identity": {"uuid": healthy_uuid}},
                ]
            }

            hydrated = names.claude_pending_native_hydrations(
                None,
                environ,
                canonical_colors=lambda settings: list(THEME_COLORS),
                guard_inventory=lambda *a, **k: True,
                load_config=lambda *a, **k: {},
                transcript_signals=inventory_core.read_claude_transcript_signals,
                session_color=lambda provider, uuid, colors: "blue",
                snapshot_inventory=lambda *a, **k: live,
                propagate_color=inventory_core.propagate_provider_color,
                propagate_title=inventory_core.propagate_provider_title,
                reconcile_pending_titles=lambda *a, **k: [],
                human_named=lambda env: frozenset(),
                adopt_native=lambda *a, **k: "",
            )

            self.assertEqual(
                ["blue"],
                self.records(healthy, healthy_uuid, "agent-color", "agentColor"),
            )
            self.assertEqual(
                ["blue"], self.records(torn, exact_torn, "agent-color", "agentColor")
            )
            self.assertTrue(hydrated)

    def test_a_record_being_written_right_now_is_never_written_onto(self) -> None:
        """A file that does not end in a newline has a writer mid-record.

        Concatenating there makes one invalid line out of two records: the
        provider's own event is destroyed AND the colour is lost, and the old
        code called that a successful push, so the window kept its old colour
        while the kit said it had changed it.
        """
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            transcript, exact, environ = self.fixture(raw, 94)
            with open(transcript, "ab") as handle:
                handle.write(
                    b'{"type":"agent-color","agentColor":"pink","sessionId":"%s"}\n'
                    % exact.encode()
                )
                handle.write(b'{"type":"assistant","message":{"content":"half a rec')

            result = inventory_core.propagate_provider_color(
                "claude", exact, "blue", environ=environ
            )

            tail = transcript.read_bytes().split(b"\n")[-1]
            self.assertNotIn(b"agent-color", tail)
            self.assertNotIn(
                "claude-transcript-color", result["provider_color_pushes"]
            )
            self.assertTrue(result["provider_color_warnings"])
            self.assertEqual(
                ["pink"], self.records(transcript, exact, "agent-color", "agentColor")
            )

    def test_a_provider_that_starts_a_record_mid_write_never_costs_the_colour(
        self,
    ) -> None:
        """The window that matters: the instant between the check and the write.

        Checking that the file ends in a newline and then appending is two
        steps, and a live provider can begin a record between them. Then the
        colour lands INSIDE the provider's record, one invalid line is made
        out of two records, and the old code reported a successful push --
        the worst outcome available here, because it damages a transcript a
        live session is writing and then says everything is fine.

        A fixture that finishes the partial write BEFORE calling the code
        cannot catch this; it never races that window. This one holds the
        writer open across it by starting the provider's record from inside
        the tail check itself, which is exactly where the real one can land.
        """
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            transcript, exact, environ = self.fixture(raw, 98)
            with open(transcript, "ab") as handle:
                handle.write(
                    b'{"type":"agent-color","agentColor":"pink","sessionId":"%s"}\n'
                    % exact.encode()
                )
            real_pread = os.pread
            started = []

            def racing_pread(fd, length, offset):
                answer = real_pread(fd, length, offset)
                if not started:
                    # The check has just seen a newline. The provider begins
                    # its record NOW, before the append happens.
                    started.append(True)
                    with open(transcript, "ab") as writer:
                        writer.write(b'{"type":"assistant","message":"split')
                return answer

            with mock.patch.object(os, "pread", racing_pread):
                result = inventory_core.propagate_provider_color(
                    "claude", exact, "blue", environ=environ
                )
            # ...and finishes it after the append, the way a real one would.
            with open(transcript, "ab") as writer:
                writer.write(b' provider record"}\n')

            self.assertTrue(started, "the race was never entered")
            # The colour is a whole, readable record -- not bytes buried in
            # the middle of an invalid line.
            self.assertEqual(
                ["pink", "blue"],
                self.records(transcript, exact, "agent-color", "agentColor"),
            )
            # And what was reported matches what is in the file.
            self.assertIn("claude-transcript-color", result["provider_color_pushes"])
            # The damage that could not be prevented is said out loud.
            self.assertTrue(result["provider_color_warnings"])

    def test_wreckage_nobody_is_writing_is_closed_and_the_colour_lands(self) -> None:
        """Refusing forever would mean a killed session never gets a colour.

        A transcript nothing has touched for a long time is not being written
        to: the half-line is wreckage from a kill, so one write closes it and
        adds the record, and the file is never left worse than it was found.
        """
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            transcript, exact, environ = self.fixture(raw, 95)
            with open(transcript, "ab") as handle:
                handle.write(b'{"type":"assistant","message":{"content":"half a rec')
            self.age(transcript, 600)

            inventory_core.propagate_provider_color(
                "claude", exact, "blue", environ=environ
            )

            lines = [line for line in transcript.read_bytes().split(b"\n") if line]
            self.assertEqual(
                ["blue"], self.records(transcript, exact, "agent-color", "agentColor")
            )
            # The wreckage is still its own line; our record did not join it.
            self.assertTrue(lines[-1].startswith(b'{"type":"agent-color"'))

    def test_the_name_is_written_once_not_once_per_pass(self) -> None:
        """The colour got the "already right, leave it alone" rule and the
        name did not. This runs on every pass too, so an unguarded append
        grew one more identical record every time."""
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            transcript, exact, environ = self.fixture(raw, 96)
            (Path(environ["HOME"]) / ".claude" / "sessions").mkdir(parents=True)

            for _ in range(8):
                result = inventory_core.propagate_provider_title(
                    "claude", exact, "Steady Name", environ=environ
                )

            self.assertEqual(
                ["Steady Name"],
                self.records(transcript, exact, "agent-name", "agentName"),
            )
            self.assertIn(
                "claude-transcript-name-current", result["provider_title_pushes"]
            )

    def test_a_real_name_change_is_still_written(self) -> None:
        """Idempotence must not freeze the name: a change is a change."""
        with tempfile.TemporaryDirectory(prefix=".colors-", dir=REPO) as raw:
            transcript, exact, environ = self.fixture(raw, 97)
            (Path(environ["HOME"]) / ".claude" / "sessions").mkdir(parents=True)

            for title in ("First", "Second", "Second"):
                inventory_core.propagate_provider_title(
                    "claude", exact, title, environ=environ
                )

            self.assertEqual(
                ["First", "Second"],
                self.records(transcript, exact, "agent-name", "agentName"),
            )


if __name__ == "__main__":
    unittest.main()
