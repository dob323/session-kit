"""What the screen runs, and what it does with the answer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest

from tests.tui_support import REPO, inventory, row, write_executable

from sessionkit_inventory import closed_sessions as closed_ledger
from sessionkit_tui import actions
from sessionkit_tui.model import ClosedSession
from sessionkit_tui.runner import Runner, run_batch

LAUNCHER = REPO / "bin" / "shpool_login_launcher"


class PlanTests(unittest.TestCase):
    def _session(self, **keywords):
        return inventory(row("Alpha", number=4, **keywords)).sessions[0]

    def test_every_action_names_the_proof_bound_command(self) -> None:
        session = self._session()
        self.assertEqual(("sp", "picker-open", "{proof}"), actions.open_session(session).argv)
        self.assertEqual(("sp", "picker-history", "{proof}"), actions.history(session).argv)
        self.assertEqual(("sp", "picker-close", "{proof}"), actions.close(session).argv)
        self.assertEqual(
            ("sp", "picker-name", "{proof}", "New"), actions.rename(session, "New").argv
        )
        self.assertEqual(
            ("sp", "picker-account-switch", "{proof}", "primary"),
            actions.change_account(session, "primary").argv,
        )

    def test_open_uses_takeover_for_a_session_open_elsewhere(self) -> None:
        session = self._session(availability="attached")
        self.assertEqual("picker-takeover", actions.open_session(session).argv[1])

    def test_the_verbs_that_take_no_proof_are_selected_by_number(self) -> None:
        session = self._session()
        self.assertEqual(("sp", "color", "4", "red"), actions.color(session, "red").argv)
        self.assertEqual(
            ("sp", "change-model", "4", "opus"), actions.change_model(session, "opus").argv
        )

    def test_change_model_answers_in_the_release_s_own_words(self) -> None:
        session = self._session()
        self.assertTrue(actions.change_model(session, "opus").quote_stderr)
        self.assertFalse(actions.close(session).quote_stderr)

    def test_restore_carries_the_closed_conversation(self) -> None:
        item = ClosedSession(
            key="u1",
            title="Blueprint",
            provider="claude",
            uuid="u1",
            cwd="/srv/project",
            account_alias="primary",
            closed_at_unix_ms=1,
        )
        self.assertEqual(
            ("sp", "restore-exact", "claude", "u1", "/srv/project", "primary"),
            actions.restore(item).argv,
        )

    def test_the_palettes_are_the_ones_each_provider_accepts(self) -> None:
        self.assertEqual("red", actions.colors_for("claude")[0])
        self.assertEqual("lime", actions.colors_for("codex")[0])


class ProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="tui-proof.")
        self.state = Path(self.temp.name)
        self.runner = Runner(
            sp_cmd="/bin/true", status_cmd="/bin/true", state_dir=self.state, environ={}
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_a_proof_names_the_exact_session(self) -> None:
        record = row("Alpha", number=1)
        estate = inventory(record)
        path = self.runner.write_proof(estate.sessions[0], estate)
        self.assertIsNotNone(path)
        proof = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual("session-kit-picker-session-v1", proof["proof_type"])
        self.assertEqual(record["shpool_id_raw"], proof["shpool_id"])
        self.assertEqual(record["identity"]["uuid"], proof["uuid"])
        self.assertEqual(record["shpool_shell"]["pid"], proof["shell_pid"])
        self.assertEqual(77, proof["daemon_pid"])

    def test_a_proof_is_readable_only_by_its_owner(self) -> None:
        estate = inventory(row("Alpha", number=1))
        path = self.runner.write_proof(estate.sessions[0], estate)
        self.assertEqual(0o600, stat.S_IMODE(os.lstat(path).st_mode))

    def test_a_cached_list_writes_no_proof(self) -> None:
        estate = inventory(row("Alpha", number=1), stale=True)
        self.assertIsNone(self.runner.write_proof(estate.sessions[0], estate))


class ExecuteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="tui-run.")
        self.base = Path(self.temp.name)
        self.log = self.base / "sp.log"
        self.sp = write_executable(
            self.base / "sp",
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.log}"\n'
            f'printf "confirm=%s\\n" "${{SESSION_KIT_CONFIRM_ID:-}}" >> "{self.log}"\n'
            "exit 0\n",
        )
        self.runner = Runner(
            sp_cmd=str(self.sp),
            status_cmd="/bin/true",
            state_dir=self.base,
            environ=dict(os.environ),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_an_action_runs_the_verb_with_a_proof_and_the_confirmation_id(self) -> None:
        estate = inventory(row("Alpha", number=1))
        session = estate.sessions[0]
        ok, note = self.runner.execute(actions.close(session), estate, session=session)
        self.assertTrue(ok)
        self.assertEqual("", note)
        written = self.log.read_text(encoding="utf-8")
        self.assertIn("picker-close", written)
        self.assertIn(f"confirm={session.shpool_id}", written)

    def test_the_proof_file_is_removed_afterwards(self) -> None:
        estate = inventory(row("Alpha", number=1))
        self.runner.execute(actions.close(estate.sessions[0]), estate, session=estate.sessions[0])
        self.assertEqual([], sorted(p.name for p in self.base.glob("picker-proof.*")))

    def test_a_verb_the_release_does_not_carry_answers_in_its_own_words(self) -> None:
        refusal = "session-kit: there is no command named change-model"
        write_executable(
            self.sp,
            "#!/usr/bin/env bash\n" f'echo "{refusal}" >&2\n' "exit 2\n",
        )
        estate = inventory(row("Alpha", number=1))
        ok, note = self.runner.execute(
            actions.change_model(estate.sessions[0], "opus"), estate
        )
        self.assertFalse(ok)
        self.assertEqual(refusal, note)

    def test_a_batch_runs_every_plan_and_reports_the_first_refusal(self) -> None:
        write_executable(
            self.sp,
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.log}"\n'
            "exit 74\n",
        )
        estate = inventory(row("Alpha", number=1), row("Beta", number=2))
        plans = [actions.close(session) for session in estate.sessions]
        ok, _ = run_batch(self.runner, estate, plans)
        self.assertFalse(ok)
        self.assertEqual(2, len(self.log.read_text(encoding="utf-8").strip().splitlines()))

    def test_an_action_on_a_session_that_is_gone_changes_nothing(self) -> None:
        estate = inventory(row("Alpha", number=1))
        plan = actions.close(estate.sessions[0])
        ok, note = self.runner.execute(plan, inventory(), session=None)
        self.assertFalse(ok)
        self.assertEqual("Nothing changed.", note)


class ReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="tui-read.")
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.data = self.base / "data"
        binaries = self.base / "bin"
        for path in (self.state, self.data, binaries):
            path.mkdir(mode=0o700)
        shpool = binaries / "shpool"
        shpool.write_text("#!/usr/bin/env bash\nexit 70\n", encoding="utf-8")
        shpool.chmod(0o700)
        self.environ = {
            **os.environ,
            "PATH": f"{binaries}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.base / "home"),
            "SESSION_KIT_STATE_DIR": str(self.state),
            "SESSION_KIT_DATA_DIR": str(self.data),
            "XDG_STATE_HOME": str(self.base / "xdg-state"),
            "XDG_DATA_HOME": str(self.base / "xdg-data"),
            "SESSION_KIT_CONFIG": str(self.base / "session-kit.toml"),
            "SESSION_KIT_SHPOOL_CMD": str(shpool),
            "SESSION_KIT_NONINTERACTIVE": "1",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _runner(self, **environ):
        return Runner(
            sp_cmd="/bin/true",
            status_cmd="/bin/true",
            state_dir=self.state,
            environ={**self.environ, **environ},
        )

    def test_the_closed_ledger_is_read_newest_first(self) -> None:
        ledger = self.base / "closed-sessions.jsonl"
        ledger.write_text(
            "\n".join(
                json.dumps(record)
                for record in (
                    {
                        "provider": "claude",
                        "uuid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                        "title": "Older",
                        "cwd": "/srv/a",
                        "closed_at_unix_ms": 1,
                    },
                    {
                        "provider": "codex",
                        "uuid": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
                        "title": "Newer",
                        "cwd": "/srv/b",
                        "closed_at_unix_ms": 2,
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )
        found = self._runner(SESSION_KIT_CLOSED_LEDGER=str(ledger)).closed_sessions()
        self.assertEqual(["Newer", "Older"], [item.title for item in found])

    def test_closed_sessions_include_rows_before_the_old_four_mib_bound(self) -> None:
        ledger = self.base / "closed-sessions.jsonl"
        oldest = (
            json.dumps(
                {
                    "provider": "codex",
                    "uuid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                    "title": "Old Closed Conversation",
                    "cwd": "/srv/a",
                    "closed_at_unix_ms": 1,
                },
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        filler = (
            json.dumps(
                {
                    "provider": "shell",
                    "uuid": "",
                    "title": "Later shell history",
                    "cwd": "",
                    "closed_at_unix_ms": 2,
                    "origin": "human",
                    "shpool_id": "filler",
                    "account_alias": "",
                },
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        with ledger.open("wb") as handle:
            handle.write(oldest)
            while handle.tell() <= closed_ledger.MAX_LEDGER_BYTES + 2048:
                handle.write(filler)

        found = self._runner(SESSION_KIT_CLOSED_LEDGER=str(ledger)).closed_sessions()
        self.assertIn("Old Closed Conversation", [item.title for item in found])

    def test_an_absent_ledger_is_an_empty_list_and_not_an_error(self) -> None:
        self.assertEqual((), self._runner().closed_sessions())

    def test_a_malformed_ledger_is_not_reported_as_empty(self) -> None:
        ledger = self.data / "closed-sessions.jsonl"
        ledger.write_text("{not-json}\n", encoding="utf-8")
        with self.assertRaisesRegex(Exception, "malformed row"):
            self._runner().closed_sessions()

    def test_projects_come_from_the_projects_file(self) -> None:
        path = self.base / "projects.tsv"
        path.write_text("main\tclaude\t/srv/project\nbad\tcodex\trelative\n", encoding="utf-8")
        found = self._runner(SESSION_KIT_PROJECTS_FILE=str(path)).projects()
        self.assertEqual((("main", "/srv/project"),), found)

    def test_models_come_from_configuration_and_never_from_a_guess(self) -> None:
        self.assertEqual((), self._runner().models("claude"))
        inline = self._runner(
            SESSION_KIT_TUI_MODELS="claude-opus, claude-sonnet"
        ).models("claude")
        self.assertEqual(("claude-opus", "claude-sonnet"), inline)
        path = self.base / "models.tsv"
        path.write_text(
            "claude\tclaude-opus\ncodex\tgpt-5\n*\tclaude-shared\n",
            encoding="utf-8",
        )
        from_file = self._runner(SESSION_KIT_MODELS_FILE=str(path)).models("claude")
        self.assertEqual(("claude-opus", "claude-shared"), from_file)

    def test_configured_models_use_the_same_provider_gate_as_the_change_verb(
        self,
    ) -> None:
        from sessionkit_inventory.worker_model import (
            IntakeError,
            configured_models,
            validate_requested_model,
        )

        configured = "claude-opus-5,gpt-5-codex,opus-4.6,claude-sonnet-5"
        offered = configured_models(
            "claude", {"SESSION_KIT_TUI_MODELS": configured}
        )
        self.assertEqual(("claude-opus-5", "claude-sonnet-5"), offered)
        for model in offered:
            self.assertEqual(model, validate_requested_model("claude", model))
        for refused in ("gpt-5-codex", "opus-4.6"):
            with self.assertRaises(IntakeError):
                validate_requested_model("claude", refused)


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="tui-launch.")
        self.base = Path(self.temp.name)
        self.log = self.base / "tui-fallback.log"
        self.trace = self.base / "trace"
        self.switch = "on"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, exit_code: int, *, stderr: str = ""):
        stderr_command = f"printf '%s' {shlex.quote(stderr)} >&2\n" if stderr else ""
        tui = write_executable(
            self.base / "tui",
            (
                "#!/usr/bin/env bash\n"
                f'echo tui >> "{self.trace}"\n'
                f"{stderr_command}"
                f"exit {exit_code}\n"
            ),
        )
        fallback = write_executable(
            self.base / "old-picker",
            "#!/usr/bin/env bash\n" f'echo fallback >> "{self.trace}"\n' "exit 0\n",
        )
        environ = dict(os.environ)
        environ.update(
            SESSION_KIT_TUI=self.switch,
            SESSION_KIT_TUI_CMD=str(tui),
            SESSION_KIT_PICKER_FALLBACK_CMD=str(fallback),
            SESSION_KIT_TUI_FALLBACK_LOG=str(self.log),
            SESSION_KIT_STATE_DIR=str(self.base),
            TMPDIR=str(self.base),
        )
        return subprocess.run(
            [str(LAUNCHER)], env=environ, capture_output=True, text=True
        )

    def test_missing_python_does_not_hang_the_login_launcher(self) -> None:
        tui = write_executable(self.base / "no-python-tui", "#!/bin/bash\nexit 0\n")
        fallback = write_executable(self.base / "no-python-fallback", "#!/bin/bash\nexit 0\n")
        command_dir = self.base / "commands"
        command_dir.mkdir()
        for name in (
            "bash",
            "date",
            "dirname",
            "chmod",
            "mkdir",
            "mkfifo",
            "mktemp",
            "mv",
            "rm",
            "rmdir",
            "sleep",
            "stat",
            "tail",
            "wc",
        ):
            target = shutil.which(name)
            self.assertIsNotNone(target, name)
            (command_dir / name).symlink_to(target)
        environ = dict(os.environ)
        environ.update(
            PATH=str(command_dir),
            SESSION_KIT_TUI="on",
            SESSION_KIT_TUI_CMD=str(tui),
            SESSION_KIT_PICKER_FALLBACK_CMD=str(fallback),
            SESSION_KIT_TUI_FALLBACK_LOG=str(self.log),
            SESSION_KIT_STATE_DIR=str(self.base),
            TMPDIR=str(self.base),
        )
        done = subprocess.run(
            [str(LAUNCHER)],
            env=environ,
            capture_output=True,
            text=True,
            timeout=2,
        )
        self.assertEqual(0, done.returncode, done.stderr)

    def test_picker_child_holding_stderr_does_not_hang_clean_exit(self) -> None:
        child_pid = self.base / "child.pid"
        tui = write_executable(
            self.base / "lingering-child-tui",
            (
                "#!/usr/bin/env bash\n"
                "sleep 30 >/dev/null &\n"
                f'printf "%s\\n" "$!" > "{child_pid}"\n'
                "exit 0\n"
            ),
        )
        fallback = write_executable(self.base / "unused-fallback", "#!/bin/sh\nexit 1\n")
        environ = dict(os.environ)
        environ.update(
            SESSION_KIT_TUI="on",
            SESSION_KIT_TUI_CMD=str(tui),
            SESSION_KIT_PICKER_FALLBACK_CMD=str(fallback),
            SESSION_KIT_TUI_FALLBACK_LOG=str(self.log),
            SESSION_KIT_STATE_DIR=str(self.base),
            TMPDIR=str(self.base),
        )
        try:
            done = subprocess.run(
                [str(LAUNCHER)],
                env=environ,
                capture_output=True,
                text=True,
                timeout=2,
            )
            self.assertEqual(0, done.returncode, done.stderr)
            self.assertFalse(self.log.exists())
        finally:
            if child_pid.exists():
                try:
                    os.kill(int(child_pid.read_text(encoding="utf-8")), signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def test_a_deliberate_leave_passes_straight_through(self) -> None:
        done = self._run(0)
        self.assertEqual(0, done.returncode)
        self.assertEqual(["tui"], self.trace.read_text(encoding="utf-8").split())
        self.assertFalse(self.log.exists())

    def test_an_abnormal_exit_logs_one_line_and_hands_over(self) -> None:
        done = self._run(3)
        self.assertEqual(0, done.returncode)
        self.assertEqual(["tui", "fallback"], self.trace.read_text(encoding="utf-8").split())
        recorded = self.log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(1, len(recorded))
        self.assertIn("exit 3", recorded[0])
        self.assertIn("stderr=-", recorded[0])

    def test_an_abnormal_exit_preserves_and_records_stderr(self) -> None:
        failure = "session-kit: could not draw\nsecond line\t(detail)\n"
        done = self._run(1, stderr=failure)
        self.assertEqual(0, done.returncode)
        self.assertEqual(failure, done.stderr)
        recorded = self.log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(1, len(recorded))
        self.assertIn(
            r"stderr=session-kit: could not draw\nsecond line\t(detail)",
            recorded[0],
        )

    def test_an_abnormal_exit_logs_only_the_last_four_kib_of_stderr(self) -> None:
        failure = "discarded-prefix\n" + ("x" * 5000) + "\nuseful-tail\n"
        done = self._run(1, stderr=failure)
        self.assertEqual(0, done.returncode)
        self.assertEqual(failure, done.stderr)
        recorded = self.log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(1, len(recorded))
        self.assertNotIn("discarded-prefix", recorded[0])
        self.assertIn("useful-tail", recorded[0])
        logged_stderr = recorded[0].split("\tstderr=", 1)[1]
        self.assertLessEqual(len(logged_stderr), 4100)

    def test_stderr_capture_is_bounded_on_disk_while_tui_is_running(self) -> None:
        marker = self.base / "noisy-ready"
        tui = write_executable(
            self.base / "noisy-tui",
            (
                "#!/usr/bin/env bash\n"
                "python3 -c 'import sys; sys.stderr.write(\"x\" * (6 * 1024 * 1024))'\n"
                f'touch "{marker}"\n'
                "sleep 1\n"
                "exit 1\n"
            ),
        )
        fallback = write_executable(self.base / "bounded-fallback", "#!/bin/sh\nexit 0\n")
        environ = dict(os.environ)
        environ.update(
            SESSION_KIT_TUI="on",
            SESSION_KIT_TUI_CMD=str(tui),
            SESSION_KIT_PICKER_FALLBACK_CMD=str(fallback),
            SESSION_KIT_TUI_FALLBACK_LOG=str(self.log),
            SESSION_KIT_STATE_DIR=str(self.base),
            TMPDIR=str(self.base),
        )
        forwarded = self.base / "forwarded-stderr"
        with forwarded.open("wb") as stderr_stream:
            process = subprocess.Popen(
                [str(LAUNCHER)],
                env=environ,
                stdout=subprocess.DEVNULL,
                stderr=stderr_stream,
            )
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "noisy TUI did not reach its hold point")
            captures = [
                path
                for root in self.base.glob("tui-stderr.*")
                for path in ([root] if root.is_file() else root.rglob("*"))
                if path.is_file()
            ]
            self.assertTrue(captures or any(self.base.glob("tui-stderr.*")))
            self.assertTrue(
                all(path.stat().st_size <= 4 * 1024 * 1024 for path in captures),
                captures,
            )
            self.assertEqual(0, process.wait(timeout=5))

    def test_a_picker_that_cannot_run_also_hands_over(self) -> None:
        done = self._run(1)
        self.assertEqual(0, done.returncode)
        self.assertEqual(["tui", "fallback"], self.trace.read_text(encoding="utf-8").split())

    def test_nobody_gets_the_new_screen_without_turning_it_on(self) -> None:
        self.switch = ""
        done = self._run(0)
        self.assertEqual(0, done.returncode)
        self.assertEqual(["fallback"], self.trace.read_text(encoding="utf-8").split())
        self.assertFalse(self.log.exists())

    def test_a_tui_on_file_in_the_state_directory_turns_it_on(self) -> None:
        self.switch = ""
        (self.base / "tui-on").touch()
        done = self._run(0)
        self.assertEqual(0, done.returncode)
        self.assertEqual(["tui"], self.trace.read_text(encoding="utf-8").split())


if __name__ == "__main__":
    unittest.main()
