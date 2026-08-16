"""Codex launch identity parity for title and account-switch relaunches."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from tests.support import REPO, run


BASHRC = REPO / "bashrc/shpool.bashrc"
UUID = "11111111-2222-4333-8444-555555555555"
TXID = "a" * 32


class CodexLifecycleRelaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".codex-lifecycle-", dir=REPO
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        home_bin = self.home / ".local" / "bin"
        home_bin.mkdir(parents=True, mode=0o700)
        shpool = home_bin / "shpool"
        shpool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shpool.chmod(0o755)
        self.state_home = self.root / "state"
        self.state_home.mkdir(mode=0o700)
        self.start_dir = self.root / "start"
        self.start_dir.mkdir(mode=0o700)
        self.project = self.root / "project"
        self.project.mkdir()
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.boot = self.root / "boot-id"
        self.boot.write_text("fixture-boot\n", encoding="utf-8")
        self.provider_log = self.root / "provider.log"
        self.bounce_log = self.root / "bounce.log"
        self.relaunch_log = self.root / "relaunch.log"
        self.lifecycle_log = self.root / "lifecycle.log"
        self.inventory = self.root / "inventory.py"
        self.inventory.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "if args[:1] == ['codex-bounce-title']:\n"
            " with open(os.environ['BOUNCE_LOG'], 'a', encoding='utf-8') as stream:\n"
            "  stream.write(' '.join(args) + '\\n')\n"
            " if os.environ.get('TITLE_AVAILABLE') == '1':\n"
            "  print('A real title')\n"
            "  raise SystemExit(0)\n"
            " raise SystemExit(1)\n"
            "if args[:2] in (['color', 'effective'], ['color', 'launch-pick']):\n"
            " raise SystemExit(0)\n"
            "if args[:2] == ['lifecycle', 'provider-exited']:\n"
            " with open(os.environ['LIFECYCLE_LOG'], 'a', encoding='utf-8') as stream:\n"
            "  uuid = os.environ.get('SESSION_KIT_LIFECYCLE_CONVERSATION_UUID', '')\n"
            "  stream.write(uuid + '\\n')\n"
            " raise SystemExit(0)\n"
            "if args[:2] == ['account', 'resume-profile']:\n"
            " print(json.dumps({'provider': args[2], 'alias': args[3], "
            "'profile_dir': os.environ['ACCOUNT_PROFILE']}))\n"
            " raise SystemExit(0)\n"
            "if args[:2] in (['account', 'switch-apply'], ['account', 'switch-rollback']):\n"
            " raise SystemExit(0)\n"
            "if args[:1] == ['validate-worker-model']:\n"
            " print(args[2])\n"
            " raise SystemExit(0)\n"
            "if args[:2] == ['action-log', 'launch']:\n"
            " raise SystemExit(0)\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        self.inventory.chmod(0o755)
        codex = self.fake_bin / "codex"
        codex.write_text(
            "#!/bin/bash\n"
            "printf '<%s>' \"$@\" >> \"$PROVIDER_LOG\"\n"
            "printf '\\n' >> \"$PROVIDER_LOG\"\n"
            "if [[ ${WRITE_SWITCH_REQUEST:-0} == 1 ]]; then\n"
            "  mkdir -p -- \"$(dirname -- \"$SWITCH_REQUEST\")\"\n"
            "  chmod 700 -- \"$(dirname -- \"$SWITCH_REQUEST\")\"\n"
            "  printf 'apply\\t%s\\ttarget\\tsource\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "
            "\"$SWITCH_TXID\" \"$THREAD_UUID\" \"$SESSION_STARTED\" "
            "\"$SESSION_SHELL_PID\" \"$SESSION_SHELL_START\" \"$REQUESTED_MODEL\" > \"$SWITCH_REQUEST\"\n"
            "  chmod 600 -- \"$SWITCH_REQUEST\"\n"
            "fi\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        fake_bash = self.fake_bin / "bash"
        fake_bash.write_text(
            "#!/bin/sh\nprintf '<%s>' \"$@\" > \"$RELAUNCH_LOG\"\n",
            encoding="utf-8",
        )
        fake_bash.chmod(0o755)

    def launch(
        self,
        mode: str,
        *,
        title_available: bool = False,
        switch_request: bool = False,
        model: str = "",
    ):
        uuid = "" if mode == "new" else UUID
        start = self.start_dir / "main1"
        start.write_text(
            f"codex\t{self.project}\t{uuid}\t{mode}\n", encoding="utf-8"
        )
        start.chmod(0o600)
        account = Path(f"{start}.account")
        account.write_text("codex\tsource\n", encoding="utf-8")
        account.chmod(0o600)
        inner = (
            'shell_start=$(awk "{print \\$22}" /proc/$$/stat); '
            'daemon_start=$(awk "{print \\$22}" /proc/$PPID/stat); '
            'export SESSION_SHELL_PID=$$ SESSION_SHELL_START=$shell_start; '
            'printf "codex\\t%s\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n" '
            '"$2" "$$" "$shell_start" "$PPID" "$daemon_start" "$3" "$4" > "$5/main1.expected"; '
            'if [[ -n $6 ]]; then '
            'printf "codex\\t%s\\t%s\\tworker:fixture:1\\tfixture-boot\\t1\\t%s\\t%s\\t%s\\t%s\\n" '
            '"$2" "$6" "$$" "$shell_start" "$PPID" "$daemon_start" > "$5/main1.launch"; '
            'fi; chmod 600 "$5/main1.expected" ${6:+"$5/main1.launch"}; source "$1"'
        )
        switch_path = (
            self.state_home
            / "session-kit"
            / "account-switch-requests"
            / "main1"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "ACCOUNT_PROFILE": os.fspath(self.profile),
                "BOUNCE_LOG": os.fspath(self.bounce_log),
                "HOME": os.fspath(self.home),
                "LIFECYCLE_LOG": os.fspath(self.lifecycle_log),
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "PROVIDER_LOG": os.fspath(self.provider_log),
                "RELAUNCH_LOG": os.fspath(self.relaunch_log),
                "REQUESTED_MODEL": model,
                "SESSION_KIT_BOOT_ID_FILE": os.fspath(self.boot),
                "SESSION_KIT_INVENTORY_CORE": os.fspath(self.inventory),
                "SESSION_KIT_ARCHIVE_DIR": os.fspath(self.root / "archives"),
                "SESSION_KIT_JOURNAL_DIR": os.fspath(self.root / "journals"),
                "SESSION_KIT_JOURNAL_RECOVERY_DIR": os.fspath(self.root / "recovery"),
                "SESSION_KIT_START_DIR": os.fspath(self.start_dir),
                "SESSION_KIT_STATE_DIR": os.fspath(self.state_home / "session-kit"),
                "SHPOOL_JOURNAL": "disabled",
                "SHPOOL_SESSION_NAME": "main1",
                "SWITCH_REQUEST": os.fspath(switch_path),
                "SWITCH_TXID": TXID,
                "SESSION_STARTED": "1",
                "THREAD_UUID": UUID,
                "TITLE_AVAILABLE": "1" if title_available else "0",
                "WRITE_SWITCH_REQUEST": "1" if switch_request else "0",
                "XDG_CONFIG_HOME": os.fspath(self.root / "xdg-config"),
                "XDG_DATA_HOME": os.fspath(self.root / "xdg-data"),
                "XDG_STATE_HOME": os.fspath(self.state_home),
            }
        )
        completed = run(
            [
                "/bin/bash",
                "-c",
                '/bin/bash --noprofile --norc -ic "$1" lifecycle-inner "$2" "$3" "$4" "$5" "$6" "$7"',
                "codex-lifecycle-test",
                inner,
                BASHRC,
                self.project,
                uuid,
                mode,
                self.start_dir,
                model,
            ],
            env=environment,
        )
        return completed, start, switch_path

    def test_a_bounce_holds_its_marker_across_the_relaunch_and_bounces_once(
        self,
    ) -> None:
        """The marker is the session's cover while its window is gone.

        Nothing in this shell can see the replacement window: it blocks on the
        provider it just started, so every moment it could pick to drop the
        marker is a moment before the window exists -- and in that moment the
        session is an App Server and a broker with no window, which reads as a
        machine's. So the shell empties the marker instead of removing it.
        Emptied, this same block cannot bounce again, because the next read
        finds no conversation ID. Present, the session goes on reading as
        bouncing until collection sees the window and clears it.
        """
        marker = self.state_home / "session-kit" / "provider-bounce" / "main1"
        marker.parent.mkdir(parents=True, mode=0o700)
        marker.write_text(
            f"{UUID}\nxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\nrequestgeneration\n",
            encoding="utf-8",
        )
        marker.chmod(0o600)
        # The provider itself reports what the classifier would have seen at
        # the one instant that matters: the moment the replacement window
        # exists. Nothing else in this harness can observe that instant.
        codex = self.fake_bin / "codex"
        codex.write_text(
            "#!/bin/bash\n"
            # Either name counts. The shell renames the marker as it takes the
            # instruction, and both names read as bouncing -- what matters at
            # this instant is that SOMETHING is still covering the row.
            'if [[ -f $BOUNCE_MARKER || -f $BOUNCE_MARKER.taken ]] || '
            'compgen -G "$BOUNCE_MARKER.taken.*" >/dev/null; then\n'
            '  printf "marker=present\\n" >> "$PROVIDER_LOG"\n'
            "else\n"
            '  printf "marker=absent\\n" >> "$PROVIDER_LOG"\n'
            "fi\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        os.environ["BOUNCE_MARKER"] = os.fspath(marker)
        self.addCleanup(os.environ.pop, "BOUNCE_MARKER", None)

        completed, _, _ = self.launch("resume")

        self.assertEqual(0, completed.returncode, completed.stderr)
        seen = [
            line
            for line in self.provider_log.read_text(encoding="utf-8").splitlines()
            if line
        ]
        # Two launches: the bounce happened, and it happened once. An
        # unconsumed marker here would bounce this session forever.
        self.assertEqual(["marker=present", "marker=present"], seen)
        # And no marker of either name outlives the session's last provider
        # exit, so a bounce cannot leave classification suppressed for an id
        # that nothing will consume or settle again.
        self.assertFalse(marker.exists())
        self.assertFalse(Path(f"{marker}.taken").exists())
        self.assertEqual([], list(marker.parent.glob(f"{marker.name}.taken.*")))

    def test_a_collection_while_the_bounce_is_pending_never_ends_the_session(
        self,
    ) -> None:
        """The consequence of clearing a marker nobody has consumed.

        A bounce is requested while the window is still up, and every bounce
        takes a fresh collection before it signals -- so a collection reading
        `app_server_window` beside a full marker is the normal case, not a
        corner. If that reading removes the marker, this shell finds no
        instruction where its instruction should be, falls out of the relaunch
        loop, and takes the ordinary provider exit: the session ends.

        That is not the visible-versus-hidden trade this branch is about. A
        hidden row is one refresh from coming back; a closed session is gone.
        """
        marker = self.state_home / "session-kit" / "provider-bounce" / "main1"
        marker.parent.mkdir(parents=True, mode=0o700)
        marker.write_text(f"{UUID}\n", encoding="utf-8")
        marker.chmod(0o600)
        collector = self.root / "collect.py"
        collector.write_text(
            "import sys\n"
            f"sys.path.insert(0, {os.fspath(REPO / 'lib')!r})\n"
            "from pathlib import Path\n"
            "from sessionkit_inventory import origins\n"
            "origins.apply_session_origins(\n"
            "    {'sessions': [{'shpool_id_raw': 'main1',\n"
            "                   'app_server_window': True}]},\n"
            f"    state_dir=Path({os.fspath(self.state_home / 'session-kit')!r}),\n"
            ")\n",
            encoding="utf-8",
        )
        # The window is up for the whole of the first launch, which is exactly
        # when the requesting collection runs. It runs once, from inside it.
        codex = self.fake_bin / "codex"
        codex.write_text(
            "#!/bin/bash\n"
            'printf "launch\\n" >> "$PROVIDER_LOG"\n'
            'if [[ ! -f $COLLECT_DONE ]]; then\n'
            '  : > "$COLLECT_DONE"\n'
            '  python3 "$COLLECT_SCRIPT" || true\n'
            "fi\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        os.environ["COLLECT_SCRIPT"] = os.fspath(collector)
        os.environ["COLLECT_DONE"] = os.fspath(self.root / "collected")
        self.addCleanup(os.environ.pop, "COLLECT_SCRIPT", None)
        self.addCleanup(os.environ.pop, "COLLECT_DONE", None)

        completed, _, _ = self.launch("resume")

        self.assertEqual(0, completed.returncode, completed.stderr)
        launches = [
            line
            for line in self.provider_log.read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual(
            ["launch", "launch"],
            launches,
            "the collection cancelled the bounce, so the shell ended the "
            "session instead of relaunching its window",
        )

    def test_first_generation_codex_account_switch_relaunches_exact_thread(self) -> None:
        completed, start, request = self.launch("new", switch_request=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("could not safely relaunch", completed.stderr)
        self.assertFalse(request.exists())
        self.assertEqual(
            f"codex\t{self.project}\t{UUID}\tresume\n",
            start.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "codex\ttarget\n",
            Path(f"{start}.account").read_text(encoding="utf-8"),
        )
        self.assertEqual("<-i>", self.relaunch_log.read_text(encoding="utf-8"))

    def test_account_switch_relaunch_preserves_the_requested_model(self) -> None:
        model = "gpt-5.6-codex"
        completed, start, request = self.launch(
            "resume", switch_request=True, model=model
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertFalse(request.exists())
        launch = Path(f"{start}.launch")
        self.assertTrue(launch.is_file())
        fields = launch.read_text(encoding="utf-8").rstrip("\n").split("\t")
        self.assertEqual(10, len(fields))
        self.assertEqual(
            ["codex", str(self.project), model, "worker:fixture:1"], fields[:4]
        )

    def test_first_generation_rejects_retained_unbound_switch_request(self) -> None:
        request = (
            self.state_home
            / "session-kit"
            / "account-switch-requests"
            / "main1"
        )
        request.parent.mkdir(parents=True, mode=0o700)
        request.write_text(
            f"apply\t{TXID}\ttarget\tsource\t{UUID}\n", encoding="utf-8"
        )
        request.chmod(0o600)

        completed, start, retained = self.launch("new")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("could not safely relaunch", completed.stderr)
        self.assertTrue(retained.is_file())
        self.assertFalse(start.exists())
        self.assertFalse(self.relaunch_log.exists())

    def test_untitled_resume_leaves_the_one_shot_title_bounce_marker(self) -> None:
        completed, _, _ = self.launch("resume")

        self.assertEqual(0, completed.returncode, completed.stderr)
        marker = self.state_home / "session-kit/provider-untitled/main1"
        self.assertTrue(marker.is_file())

    def test_new_launch_marks_untitled_without_an_empty_uuid_lookup(self) -> None:
        completed, _, _ = self.launch("new")

        self.assertEqual(0, completed.returncode, completed.stderr)
        marker = self.state_home / "session-kit/provider-untitled/main1"
        self.assertTrue(marker.is_file())
        self.assertFalse(self.bounce_log.exists())

    def test_resume_title_lookup_is_read_only(self) -> None:
        completed, _, _ = self.launch("resume", title_available=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            f"codex-bounce-title --read-only {UUID}\n",
            self.bounce_log.read_text(encoding="utf-8"),
        )

    def test_untitled_fork_leaves_the_one_shot_title_bounce_marker(self) -> None:
        completed, _, _ = self.launch("fork")

        self.assertEqual(0, completed.returncode, completed.stderr)
        marker = self.state_home / "session-kit/provider-untitled/main1"
        self.assertTrue(marker.is_file())

    def test_titled_resume_does_not_request_an_unneeded_bounce(self) -> None:
        completed, _, _ = self.launch("resume", title_available=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        marker = self.state_home / "session-kit/provider-untitled/main1"
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
