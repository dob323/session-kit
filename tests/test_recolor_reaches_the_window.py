"""A color change reaches the window, named session or not — and never at a cost.

Both provider windows read their color once, when the process starts, so the
kit's one safe restart is what applies a recolor to a session that is already
open. That restart used to demand a title first -- a guard that belongs to the
RENAME case, where restarting a Codex thread before it has a real name burns the
one-shot on a prompt echo. Applied to a recolor it meant an unnamed session
could never take the color the picker showed.

Everything here drives the real bounce functions with a stubbed session manager
and a fake `kill`, so what is asserted is what the code does: which snapshots
it refuses, that a refusal never signals anything, that the one-shot marker
survives a restart that applied no name, and that the fresh pre-signal proof
catches work that started during the race window.

The Codex half of a LIVE recolor (no restart at all) is WS-F's app-server work.
What is here is the seam it plugs into, which answers with the real reason
instead of a false success.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.support import REPO


BOUNCE = REPO / "lib" / "sh" / "sp_provider_bounce.sh"
UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def session_row(**overrides) -> dict:
    """A row the bounce would accept, before the test spoils one field."""
    row = {
        "shpool_id": "drill-session",
        "availability": "ready",
        "agent_status": "idle",
        "subagents": [],
        "recent_output_age_seconds": 900,
        "identity": {
            "uuid": UUID,
            "pid": 4242,
            "process_start_ticks": 99000,
        },
    }
    row.update(overrides)
    return row


class BounceHarness:
    """Run a real bounce against fixture state, and record every signal."""

    def __init__(self, base: Path, *, provider: str) -> None:
        self.base = base
        self.provider = provider
        self.state = base / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.markers = self.state / "provider-untitled"
        self.markers.mkdir(exist_ok=True)
        self.marker = self.markers / "drill-session"
        self.marker.write_text("", encoding="utf-8")
        self.kill_log = base / "kill.log"
        self.snapshot = base / "snapshot.json"
        self.recheck = base / "recheck.json"
        self.core = base / "fake-core"
        self.collector = base / "collect.py"
        self.pending = self.state / "provider-bounce" / "drill-session"

    def write_collector(self) -> None:
        """The real collection the real recheck runs, at the real moment.

        `picker_bounce_codex` writes the bounce marker, then takes a FRESH
        guard snapshot to re-prove the session is still idle, and only then
        asks the window to exit. That snapshot is a full collection, and it
        runs while the old window is still up -- so it publishes
        `app_server_window`, which is what tells collection a bounce has
        finished. This is that collection, with nothing simulated but the
        session list.
        """
        self.collector.write_text(
            "import sys\n"
            f"sys.path.insert(0, {os.fspath(REPO / 'lib')!r})\n"
            "from pathlib import Path\n"
            "from sessionkit_inventory import origins\n"
            "origins.apply_session_origins(\n"
            "    {'sessions': [{'shpool_id_raw': 'drill-session',\n"
            "                   'app_server_window': True}]},\n"
            f"    state_dir=Path({os.fspath(self.state)!r}),\n"
            ")\n",
            encoding="utf-8",
        )

    def write_state(self, first: dict, second: dict | None = None) -> None:
        self.snapshot.write_text(json.dumps({"sessions": [first]}), encoding="utf-8")
        self.recheck.write_text(
            json.dumps({"sessions": [second if second is not None else first]}),
            encoding="utf-8",
        )

    def write_core(self, *, bounce_title: str = "A Real Name", color_ready: bool = True,
                   bounce_status: int = 0) -> None:
        """The inventory core the bounce calls -- run through python3, as it is."""
        self.core.write_text(
            "import sys\n"
            "argv = sys.argv[1:]\n"
            'if argv[:2] == ["platform", "codex-refresh-target"]:\n'
            '    print("\\t".join(argv[4:6]))\n'
            "    raise SystemExit(0)\n"
            'if argv[:2] == ["platform", "process-is"]:\n'
            "    raise SystemExit(0)\n"
            'if argv[:2] == ["color", "bounce-ready"]:\n'
            f"    raise SystemExit({0 if color_ready else 1})\n"
            'if argv[:1] in (["codex-bounce-title"], ["claude-bounce-title"]):\n'
            f"    title = {bounce_title!r}\n"
            "    if title:\n"
            "        print(title)\n"
            f"    raise SystemExit({bounce_status})\n"
            "raise SystemExit(0)\n"
        )

    def run(
        self,
        *,
        reason: str = "color",
        mode: str = "automatic",
        collect_during_recheck: bool = False,
    ) -> subprocess.CompletedProcess:
        function = f"picker_bounce_{self.provider}"
        recheck_stub = (
            f'sk_guard_snapshot_file() {{ python3 "{self.collector}"; '
            f'cat "{self.recheck}" > "$1"; }}'
            if collect_during_recheck
            else f'sk_guard_snapshot_file() {{ cat "{self.recheck}" > "$1"; }}'
        )
        script = f"""
set -uo pipefail
export SESSION_KIT_STATE_DIR="{self.state}"
export SESSION_KIT_INVENTORY_CORE="{self.core}"
export SESSION_KIT_SHPOOL_CMD=/bin/true
source "{REPO}/bin/session_kit_common"
INVENTORY_CORE="{self.core}"
SNAPSHOT="{self.snapshot}"
source "{BOUNCE}"
# The session manager, the action log and the signal itself are the three
# things this test must observe rather than perform.
{recheck_stub}
picker_action_event() {{ :; }}
kill() {{ printf '%s\\n' "$*" >> "{self.kill_log}"; return 0; }}
SK_PROOF_PROVIDER={self.provider}
SK_PROOF_UUID={UUID}
SK_PROOF_PROVIDER_PID=4242
SK_PROOF_PROVIDER_START=99000
SK_PROOF_SHELL_PID=4200
SK_PROOF_SHELL_START=98000
{function} drill-session {mode} {reason}
echo "rc=$?"
"""
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True
        )

    @property
    def signalled(self) -> bool:
        return self.kill_log.is_file() and "-TERM" in self.kill_log.read_text()

    @property
    def marker_survives(self) -> bool:
        return self.marker.is_file()


class ColorBounceBehaviorTests(unittest.TestCase):
    def harness(self, provider: str, **core) -> BounceHarness:
        raw = tempfile.mkdtemp(prefix=".bounce-", dir=REPO)
        self.addCleanup(subprocess.run, ["rm", "-rf", raw])
        harness = BounceHarness(Path(raw), provider=provider)
        harness.write_core(**core)
        harness.write_collector()
        return harness

    def test_an_unnamed_session_is_recolored(self) -> None:
        """The whole point of item 20: no name, still recolorable."""
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                harness = self.harness(provider, bounce_title="", bounce_status=1)
                harness.write_state(session_row())
                harness.run(reason="color")
                self.assertTrue(harness.signalled)

    def test_an_unnamed_recolor_keeps_the_one_shot_marker(self) -> None:
        """Or the session's status bar is stranded on its conversation ID.

        The marker is what every later remedy needs -- the automatic retry at
        reopen and the picker's apply-the-pending-title action both refuse
        without it. A restart that applied no name must not spend it.
        """
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                harness = self.harness(provider, bounce_title="", bounce_status=1)
                harness.write_state(session_row())
                harness.run(reason="color")
                self.assertTrue(harness.signalled)
                self.assertTrue(
                    harness.marker_survives,
                    "the pending-title remedies are gone without it",
                )

    def test_recolor_then_name_still_reaches_the_window(self) -> None:
        """The sequence the marker bug broke, end to end.

        Recolor an unnamed session (restart, no name applied), let the titler
        name it afterwards, then let the next open ask for the name bounce.
        With the marker spent, that second bounce could never run.
        """
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                harness = self.harness(provider, bounce_title="", bounce_status=1)
                harness.write_state(session_row())
                harness.run(reason="color")
                self.assertTrue(harness.signalled)
                self.assertTrue(harness.marker_survives)

                # The thread is named now, and the window is opened again.
                harness.kill_log.unlink()
                harness.write_core(bounce_title="Named Later")
                harness.run(reason="name")
                self.assertTrue(harness.signalled, "the name never reached the window")
                self.assertFalse(harness.marker_survives)

    def test_a_recolor_that_also_carried_a_name_spends_the_marker(self) -> None:
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                harness = self.harness(provider, bounce_title="A Real Name")
                harness.write_state(session_row())
                harness.run(reason="color")
                self.assertTrue(harness.signalled)
                self.assertFalse(harness.marker_survives)

    def test_a_name_bounce_still_refuses_without_a_name(self) -> None:
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                harness = self.harness(provider, bounce_title="", bounce_status=1)
                harness.write_state(session_row())
                harness.run(reason="name")
                self.assertFalse(harness.signalled)
                self.assertTrue(harness.marker_survives)

    def test_a_color_with_nothing_to_apply_never_signals(self) -> None:
        """`color effective` succeeded for any uuid; readiness is the real test."""
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                harness = self.harness(provider, color_ready=False)
                harness.write_state(session_row())
                harness.run(reason="color")
                self.assertFalse(harness.signalled)
                self.assertTrue(harness.marker_survives)

    def test_a_working_or_busy_session_is_never_signalled(self) -> None:
        cases = {
            "working": session_row(agent_status="working"),
            "state unavailable": session_row(agent_status="state unavailable"),
            "attached": session_row(availability="attached"),
            "has subagents": session_row(subagents=[{"pid": 5}]),
            "just printed": session_row(recent_output_age_seconds=5),
            "different process": session_row(
                identity={"uuid": UUID, "pid": 999, "process_start_ticks": 99000}
            ),
        }
        for provider in ("claude", "codex"):
            for name, row in cases.items():
                with self.subTest(provider=provider, case=name):
                    harness = self.harness(provider)
                    harness.write_state(row)
                    harness.run(reason="color")
                    self.assertFalse(
                        harness.signalled, f"{provider} was signalled while {name}"
                    )

    def test_work_that_starts_during_the_race_window_stops_the_signal(self) -> None:
        """The fresh proof exists for exactly this; it skipped the quiet test.

        First snapshot: quiet and idle. By the time the lock is released the
        window is printing again -- status can still read idle while output is
        arriving -- and the signal must not be sent.
        """
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                harness = self.harness(provider)
                harness.write_state(
                    session_row(),
                    session_row(recent_output_age_seconds=3),
                )
                harness.run(reason="color")
                self.assertFalse(harness.signalled)

    def test_a_state_change_during_the_race_window_stops_the_signal(self) -> None:
        for provider in ("claude", "codex"):
            for name, second in (
                ("started working", session_row(agent_status="working")),
                ("someone attached", session_row(availability="attached")),
                ("spawned a subagent", session_row(subagents=[{"pid": 7}])),
            ):
                with self.subTest(provider=provider, case=name):
                    harness = self.harness(provider)
                    harness.write_state(session_row(), second)
                    harness.run(reason="color")
                    self.assertFalse(harness.signalled)

    def test_a_quiet_idle_session_is_signalled(self) -> None:
        """The control: without it, every refusal above proves nothing."""
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                harness = self.harness(provider)
                harness.write_state(session_row())
                harness.run(reason="color")
                self.assertTrue(harness.signalled)

    def test_a_deferred_recolor_is_still_pending_at_the_next_open(self) -> None:
        """A busy session refuses the restart; the request must not die there.

        `sp color` asks for one safe restart and a working or attached session
        rightly refuses. Without a record of the request, the next open asked
        only for a NAME bounce -- which an unnamed session can never satisfy --
        so the window kept its own color while the picker showed the new one.
        """
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                harness = self.harness(provider, bounce_title="", bounce_status=1)
                pending = harness.state / "provider-recolor" / "drill-session"
                pending.parent.mkdir(exist_ok=True)
                pending.write_text("", encoding="utf-8")
                # Busy: refused, and the pending record survives.
                harness.write_state(session_row(agent_status="working"))
                harness.run(reason="color")
                self.assertFalse(harness.signalled)
                self.assertTrue(pending.is_file())
                # Quiet at the next open: the restart happens and clears it.
                harness.write_state(session_row())
                harness.run(reason="color")
                self.assertTrue(harness.signalled)
                self.assertFalse(pending.is_file())

    def test_the_picker_retries_a_pending_recolor_at_open(self) -> None:
        picker = (REPO / "lib" / "sh" / "sp_picker.sh").read_text(encoding="utf-8")
        opener = picker.split("picker_open() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("provider-recolor/$id", opener)
        self.assertIn("automatic color", opener)
        commands = (REPO / "lib" / "sh" / "sp_commands.sh").read_text(encoding="utf-8")
        recolor = commands.split("color_target() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('sk_mark_provider_recolor "$id"', recolor)

    def test_the_bounce_survives_its_own_recheck_collection(self) -> None:
        """The bounce must not be cancelled by the collection it causes.

        The order inside `picker_bounce_codex` is: write the marker, take a
        FRESH guard snapshot to re-prove the session is idle, then signal the
        window. That snapshot is a real collection and it runs while the old
        window is still up, so it publishes `app_server_window` -- the very
        signal that tells collection a bounce has finished and its marker can
        go. Read against a marker nobody has consumed yet, that signal is about
        the OLD window, and acting on it deletes the instruction before the
        session shell has read it.

        What follows is not a hidden row, it is a closed session: the shell
        reaches the marker, finds nothing where its instruction should be,
        takes the ordinary provider exit instead of relaunching, and the
        conversation the person was sitting in ends. The marker stays until the
        shell itself empties it.
        """
        harness = self.harness("codex")
        harness.write_state(session_row())

        harness.run(reason="name", collect_during_recheck=True)

        self.assertTrue(harness.signalled, "the window was never asked to exit")
        self.assertTrue(
            harness.pending.is_file(),
            "the bounce was cancelled by its own recheck; the shell will end "
            "the session instead of relaunching it",
        )
        self.assertEqual(
            UUID,
            harness.pending.read_text(encoding="utf-8").splitlines()[0],
            "the shell must still find the conversation it was told to resume",
        )

    def test_the_marker_is_replaced_whole_never_truncated_in_place(self) -> None:
        """An empty marker is the shell's receipt, so it must never be a phase.

        `> file` truncates before it writes, so a plain redirect passes through
        the empty state on the way to the full one. A collection landing there
        reads a receipt for a bounce nobody has performed and removes the
        marker -- the same cancellation, through a smaller door. A rename has
        no such moment, and a rename is observable: it replaces the file rather
        than rewriting it.
        """
        harness = self.harness("codex")
        harness.write_state(session_row())
        # A marker left by an earlier bounce of the same session: this is the
        # case where a redirect rewrites rather than creates, and it is the
        # only one in which the difference is observable after the fact.
        harness.pending.parent.mkdir(parents=True, exist_ok=True)
        harness.pending.write_text("11111111-1111-4111-8111-111111111111\n", encoding="utf-8")
        before = harness.pending.stat().st_ino

        harness.run(reason="name")

        self.assertTrue(harness.signalled)
        self.assertEqual(
            UUID,
            harness.pending.read_text(encoding="utf-8").splitlines()[0],
            "the marker must name the conversation this bounce is for",
        )
        lines = harness.pending.read_text(encoding="utf-8").splitlines()
        self.assertEqual(3, len(lines))
        self.assertRegex(lines[2], r"^[A-Za-z0-9]{6,32}$")
        self.assertNotEqual(
            before,
            harness.pending.stat().st_ino,
            "the marker was rewritten in place, so it was briefly empty, and a "
            "collection landing in that instant reads the shell's receipt for a "
            "bounce nobody performed and cancels it",
        )

    def test_a_bogus_reason_is_refused(self) -> None:
        harness = self.harness("claude")
        harness.write_state(session_row())
        result = harness.run(reason="sideways")
        self.assertIn("rc=2", result.stdout)
        self.assertFalse(harness.signalled)


class ColorBounceContractTests(unittest.TestCase):
    """The few things worth pinning as shape rather than behavior."""

    def function_body(self, name: str) -> str:
        source = BOUNCE.read_text(encoding="utf-8")
        return source.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]

    def test_both_bounces_take_a_reason_and_default_to_name(self) -> None:
        for name in ("picker_bounce_claude", "picker_bounce_codex"):
            with self.subTest(name):
                body = self.function_body(name)
                self.assertIn("reason=${3:-name}", body)

    def test_the_recolor_command_asks_for_a_color_bounce(self) -> None:
        commands = (REPO / "lib" / "sh" / "sp_commands.sh").read_text(encoding="utf-8")
        recolor = commands.split("color_target() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('sk_refresh_provider_title "$id" "$provider" color', recolor)
        rename = commands.split("name_target() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("color\n", rename.split("sk_refresh_provider_title", 1)[1])


class CodexLiveRecolorSeamTests(unittest.TestCase):
    def core(self):
        import importlib.util
        import sys

        if "session_inventory" in sys.modules:
            return sys.modules["session_inventory"]
        spec = importlib.util.spec_from_file_location(
            "session_inventory", REPO / "lib" / "session_inventory.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_without_the_capability_it_names_the_real_reason(self) -> None:
        core = self.core()
        with tempfile.TemporaryDirectory(prefix=".live-color-", dir=REPO) as raw:
            pushes, warnings = core.push_codex_live_color(Path(raw), UUID, "cyan")
            self.assertEqual([], pushes)
            self.assertEqual(1, len(warnings))
            self.assertIn("next starts", warnings[0])

    def test_a_declared_capability_without_a_client_never_claims_success(self) -> None:
        core = self.core()
        with tempfile.TemporaryDirectory(prefix=".live-color-", dir=REPO) as raw:
            state = Path(raw)
            (state / "app-server").mkdir()
            (state / "app-server" / "capabilities.json").write_text(
                json.dumps({"theme_set": True}), encoding="utf-8"
            )
            pushes, warnings = core.push_codex_live_color(state, UUID, "cyan")
            self.assertEqual([], pushes)
            self.assertIn("no app-server client is wired", warnings[0])

    def test_the_client_is_used_once_it_exists(self) -> None:
        core = self.core()
        with tempfile.TemporaryDirectory(prefix=".live-color-", dir=REPO) as raw:
            state = Path(raw)
            (state / "app-server").mkdir()
            (state / "app-server" / "capabilities.json").write_text(
                json.dumps({"theme_set": True}), encoding="utf-8"
            )
            seen: list[tuple[str, str]] = []

            def send(_root: Path, uuid: str, color: str):
                seen.append((uuid, color))
                return ["codex-live-color"], []

            pushes, warnings = core.push_codex_live_color(
                state, UUID, "cyan", send=send
            )
            self.assertEqual(["codex-live-color"], pushes)
            self.assertEqual([], warnings)
            self.assertEqual([(UUID, "cyan")], seen)

    def test_capabilities_default_to_nothing_and_ignore_a_symlink(self) -> None:
        core = self.core()
        with tempfile.TemporaryDirectory(prefix=".live-caps-", dir=REPO) as raw:
            state = Path(raw)
            self.assertEqual({}, core.codex_live_capabilities(state))
            (state / "app-server").mkdir()
            elsewhere = state / "elsewhere.json"
            elsewhere.write_text(json.dumps({"theme_set": True}), encoding="utf-8")
            link = state / "app-server" / "capabilities.json"
            link.symlink_to(elsewhere)
            self.assertEqual({}, core.codex_live_capabilities(state))
            os.unlink(link)
            link.write_text("{not json", encoding="utf-8")
            self.assertEqual({}, core.codex_live_capabilities(state))


class ColorReadinessTests(unittest.TestCase):
    """`color bounce-ready`: can a restart actually paint this color?"""

    def sandbox(self, base: Path) -> dict[str, str]:
        home = base / "home"
        (home / ".claude" / "sessions").mkdir(parents=True, mode=0o700)
        (home / ".claude" / "projects" / "-srv-project").mkdir(parents=True, mode=0o700)
        (home / ".codex" / "themes").mkdir(parents=True, mode=0o700)
        config = base / "session-kit.json"
        config.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        config.chmod(0o600)
        state = base / "state"
        state.mkdir(mode=0o700)
        state.chmod(0o700)
        return {
            **os.environ,
            "HOME": os.fspath(home),
            "SESSION_KIT_CONFIG": os.fspath(config),
            "SESSION_KIT_STATE_DIR": os.fspath(state),
            "SESSION_KIT_CODEX_AUTOTITLE": "0",
        }

    def ready(self, env: dict[str, str], provider: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [REPO / "lib" / "session_inventory.py", "color", "bounce-ready", provider, UUID],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_claude_needs_a_color_record_the_resumed_window_will_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".ready-claude-", dir=REPO) as raw:
            env = self.sandbox(Path(raw))
            transcript = (
                Path(env["HOME"]) / ".claude" / "projects" / "-srv-project" / f"{UUID}.jsonl"
            )
            transcript.write_text(
                json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n",
                encoding="utf-8",
            )
            refused = self.ready(env, "claude")
            self.assertEqual(1, refused.returncode, refused.stdout)
            self.assertIn("no color record", refused.stderr)

            effective = subprocess.run(
                [REPO / "lib" / "session_inventory.py", "color", "effective", "claude", UUID],
                env=env,
                capture_output=True,
                text=True,
            ).stdout.strip()
            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "agent-color",
                            "agentColor": effective,
                            "sessionId": UUID,
                        }
                    )
                    + "\n"
                )
            accepted = self.ready(env, "claude")
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            self.assertEqual(effective, accepted.stdout.strip())

    def test_codex_needs_the_theme_the_launcher_would_pass(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".ready-codex-", dir=REPO) as raw:
            env = self.sandbox(Path(raw))
            refused = self.ready(env, "codex")
            self.assertEqual(1, refused.returncode, refused.stdout)
            self.assertIn("no Codex theme", refused.stderr)

            effective = subprocess.run(
                [REPO / "lib" / "session_inventory.py", "color", "effective", "codex", UUID],
                env=env,
                capture_output=True,
                text=True,
            ).stdout.strip()
            theme = Path(env["HOME"]) / ".codex" / "themes" / f"sk-{effective}.tmTheme"
            theme.write_text("<theme/>", encoding="utf-8")
            accepted = self.ready(env, "codex")
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            self.assertEqual(effective, accepted.stdout.strip())


if __name__ == "__main__":
    unittest.main()
