from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest

from tests.support import REPO, run


BASHRC = REPO / "bashrc" / "shpool.bashrc"
CORE = REPO / "lib" / "session_inventory.py"
BOOT_ID = "11111111-2222-3333-4444-555555555555"
UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class TimeoutPipelineTests(unittest.TestCase):
    def test_timeout_preserves_pipeline_stdin(self) -> None:
        command = (
            'source "$1"; printf "native-color-input\\n" | '
            "sk_timeout 5 bash -c 'read -r value; printf \"%s\\n\" \"$value\"'"
        )
        completed = subprocess.run(
            [
                "bash",
                "-c",
                command,
                "timeout-test",
                str(REPO / "bin" / "session_kit_common"),
            ],
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("native-color-input\n", completed.stdout)


class PortableMktempTests(unittest.TestCase):
    def test_runtime_templates_end_with_replacement_characters(self) -> None:
        template_pattern = re.compile(
            r'mktemp(?:\s+-d)?\s+"([^"\n]*XXXXXX[^"\n]*)"'
        )
        runtime_files = [
            path
            for path in (REPO / "bin").iterdir()
            if path.is_file()
        ]
        runtime_files.append(REPO / "tests" / "run")
        invalid: list[str] = []
        for path in runtime_files:
            text = path.read_text(encoding="utf-8")
            for line_number, line in enumerate(text.splitlines(), start=1):
                for match in template_pattern.finditer(line):
                    if not match.group(1).endswith("XXXXXX"):
                        invalid.append(
                            f"{path.relative_to(REPO)}:{line_number}:"
                            f" {match.group(1)}"
                        )
        self.assertEqual([], invalid)


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class ProviderExitShellHarness(unittest.TestCase):
    """Fixture only: launches the bashrc against provider stubs. No tests."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix=".lifecycle-shell-", dir=REPO
        )
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.state = self.base / "state"
        self.start = self.base / "start"
        self.project = self.base / "project"
        # The bashrc prepends $HOME/.cargo/bin:$HOME/.local/bin:/opt/homebrew/bin
        # to PATH. On macOS a real provider in the Homebrew prefix would
        # therefore shadow a stub placed anywhere later, so the stub lives in
        # the fixture HOME, which that prepend puts first on both platforms.
        self.bin = self.home / ".local" / "bin"
        for path in (
            self.home,
            self.state,
            self.start,
            self.project,
        ):
            path.mkdir(mode=0o700)
        self.bin.mkdir(mode=0o700, parents=True)
        self.config = self.base / "inventory.json"
        self.config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "state_dir": str(self.state),
                    "aliases": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.config.chmod(0o600)
        self.boot = self.base / "boot-id"
        self.boot.write_text(BOOT_ID + "\n", encoding="utf-8")
        self.provider_log = self.base / "provider.log"
        self.account_environment_log = self.base / "account-environment.log"
        self.account_profile = self.base / "codex-profile"
        self.account_profile.mkdir(mode=0o700)
        self.core = CORE
        self.environment_overrides: dict[str, str] = {}
        # A resumed conversation: the shell is handed the exact UUID in its
        # launch record. `without_a_conversation()` switches to the other
        # shape, where it never learns one.
        self.launch_mode = "resume"
        self.launch_uuid = UUID
        write_executable(
            self.bin / "codex",
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$PROVIDER_LOG"\n'
            'if [[ -n ${ACCOUNT_ENV_LOG:-} ]]; then '
            'printf "%s\\t%s\\t%s\\n" "$CODEX_HOME" "$SESSION_KIT_ACCOUNT_ALIAS" '
            '"$SESSION_KIT_ACCOUNT_CAPABLE" >> "$ACCOUNT_ENV_LOG"; fi\n',
        )
        # A FAKE shpool, and it is not optional. The hand-back path runs
        # `command shpool detach` (bashrc/shpool.bashrc), which is hardcoded --
        # SESSION_KIT_SHPOOL_CMD does not reach it, deliberately, because a
        # session shell must not be redirected by an environment variable. So
        # the only lever a fixture has is PATH, and this stub takes it: the
        # bashrc prepends "$HOME/.local/bin" with the fixture HOME, which puts
        # this ahead of the real binary on both platforms.
        #
        # Without it these tests ran TWO commands against the REAL daemon on
        # its default socket: `shpool detach`, carrying
        # SHPOOL_SESSION_NAME=main2 from the fixture, and `shpool list --json`
        # from the collector behind `lifecycle reopen`. Nothing was harmed
        # only because no live session is called main2 -- luck, not isolation,
        # and the wrong thing to leave lying around on a box where fifteen
        # agents run this suite.
        #
        # It answers `list --json` with an empty but VALID payload rather than
        # refusing: the reopen guard has to fail on "this terminal is not in
        # the list" for a reason the fixture chose, not on whatever the real
        # estate happened to contain that minute.
        self.shpool_log = self.base / "shpool.log"
        write_executable(
            self.bin / "shpool",
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$*" >> "$SHPOOL_LOG"\n'
            'if [[ $1 == list ]]; then printf \'{"sessions": []}\\n\'; exit 0; fi\n'
            "exit 1\n",
        )

    def tearDown(self) -> None:
        # Every shpool call the fixture made, and it may only ever be the
        # detach. A new call reaching this stub is a new way for the suite to
        # touch a real session manager, and it should be read before it is
        # allowed.
        if self.shpool_log.exists():
            calls = sorted(
                set(self.shpool_log.read_text(encoding="utf-8").split("\n")) - {""}
            )
            self.assertEqual(
                [],
                [call for call in calls if call not in ("detach", "list --json")],
                f"the fixture called shpool unexpectedly: {calls}",
            )
        self.temp.cleanup()

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:{environment['PATH']}",
                "SHPOOL_SESSION_NAME": "main2",
                "SHPOOL_JOURNAL": "disabled",
                "SESSION_KIT_BOOT_ID_FILE": str(self.boot),
                "SESSION_KIT_CONFIG": str(self.config),
                "SESSION_KIT_INVENTORY_CORE": str(self.core),
                "SESSION_KIT_START_DIR": str(self.start),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "PROVIDER_LOG": str(self.provider_log),
                "SHPOOL_LOG": str(self.shpool_log),
                "ACCOUNT_ENV_LOG": str(self.account_environment_log),
                # `__sk_state_root` is ${XDG_STATE_HOME:-$HOME/.local/state}.
                # Inheriting a set XDG_STATE_HOME would send every state write
                # the bashrc makes -- provider-bounce, account-switch-requests,
                # session-color -- outside this fixture. Pin it.
                "XDG_STATE_HOME": str(self.home / ".local" / "state"),
                # And these two relocate `closed_sessions.data_dir()`, which
                # is the closed-sessions ledger -- the one file this whole
                # branch exists to protect. Neither is set on this box, so
                # nothing escaped; that is luck, and luck is what the shpool
                # stub above was added to stop relying on (review, 2026-08-15).
                "XDG_DATA_HOME": str(self.home / ".local" / "share"),
                "SESSION_KIT_DATA_DIR": str(
                    self.home / ".local" / "share" / "session-kit"
                ),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        environment.update(self.environment_overrides)
        return environment

    def launch(
        self, choices: str, *, account_alias: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        record = self.start / "main2"
        record.write_text(
            f"codex\t{self.project}\t{self.launch_uuid}\t{self.launch_mode}\n",
            encoding="utf-8",
        )
        if account_alias is not None:
            account = self.start / "main2.account"
            account.write_text(f"codex\t{account_alias}\n", encoding="utf-8")
            account.chmod(0o600)
        # The generation of a process is read from /proc on Linux and from the
        # native adapter on Darwin, exactly as the bashrc itself does, so this
        # harness exercises the real launch path on both platforms.
        command = r"""
bash --noprofile --norc -ic '
  # Captured out here on purpose: inside the function, $6 would refer to the
  # sixth argument of the function rather than of this script.
  inventory_core="$6"
  start_ticks() {
    if [ -r "/proc/$1/stat" ]; then
      awk "{print \$22}" "/proc/$1/stat"
    else
      python3 "$inventory_core" platform process-info "$1" | cut -f2
    fi
  }
  shell_start=$(start_ticks $$)
  parent_start=$(start_ticks $PPID)
  printf "%s\t%s\t%s\t1\t%s\t%s\t%s\t%s\t%s\t%s\n" \
    codex "$2" "$4" "$$" "$shell_start" "$PPID" "$parent_start" "$5" "$7" \
    > "$3/main2.expected"
  source "$1"
  printf "SOURCE_RETURNED\n"
' lifecycle-inner "$1" "$2" "$3" "$4" "$5" "$6" "$7"
"""
        return subprocess.run(
            [
                "bash",
                "-c",
                command,
                "lifecycle-outer",
                str(BASHRC),
                str(self.project),
                str(self.start),
                BOOT_ID,
                self.launch_uuid,
                str(self.core),
                self.launch_mode,
            ],
            cwd=REPO,
            env=self.environment(),
            input=choices,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )

    def lifecycle_document(self) -> dict:
        lifecycle_dir = self.state / "lifecycle"
        candidates = sorted(
            path
            for path in lifecycle_dir.glob("*.json")
            if path.name != "key.json" and not path.name.endswith(".exact.json")
        )
        self.assertEqual(1, len(candidates))
        self.assertEqual(0o600, stat.S_IMODE(candidates[0].stat().st_mode))
        return json.loads(candidates[0].read_text(encoding="utf-8"))

    def reopen_answers(self, status: int) -> Path:
        """Make `lifecycle reopen` answer with a fixed outcome.

        0 is a reopened conversation that ended cleanly, 76 is one that
        crashed again, and anything else is a refusal to reopen at all. The
        real verb needs a live daemon generation this harness cannot provide,
        and the shell's decision is what these tests are about.
        """
        log = self.base / "reopen.log"
        wrapper = self.base / "reopen-wrapper.py"
        wrapper.write_text(
            f"""#!/usr/bin/env python3
import os, pathlib, sys
if sys.argv[1:3] == ["lifecycle", "reopen"]:
    with pathlib.Path(os.environ["REOPEN_LOG"]).open("a") as handle:
        handle.write("reopen\\n")
    raise SystemExit({status})
os.execv(sys.executable, [sys.executable, os.environ["REAL_CORE"], *sys.argv[1:]])
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        self.core = wrapper
        self.environment_overrides = {
            "REOPEN_LOG": str(log),
            "REAL_CORE": str(CORE),
        }
        return log

    def provider_exit_record_fails(self, times: int = 99) -> Path:
        """Make `lifecycle provider-exited` fail its first `times` attempts.

        The record is what makes a row say "provider exited", and only a row
        that says it is ever considered by automatic cleanup. A failure here
        used to be permanent and silent.
        """
        log = self.base / "provider-exited.log"
        wrapper = self.base / "exit-record-wrapper.py"
        wrapper.write_text(
            f"""#!/usr/bin/env python3
import os, pathlib, sys
if sys.argv[1:3] == ["lifecycle", "provider-exited"]:
    counter = pathlib.Path(os.environ["EXIT_RECORD_LOG"])
    seen = len(counter.read_text().split()) if counter.exists() else 0
    with counter.open("a") as handle:
        handle.write("attempt\\n")
    if seen < {times}:
        raise SystemExit(1)
os.execv(sys.executable, [sys.executable, os.environ["REAL_CORE"], *sys.argv[1:]])
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        self.core = wrapper
        self.environment_overrides = {
            "EXIT_RECORD_LOG": str(log),
            "REAL_CORE": str(CORE),
        }
        return log

    def provider_transcript(self, *, uuid: str = UUID, mode: int = 0o600) -> Path:
        """The conversation's OWN record, the file a restore actually reads.

        Every close that claims a conversation comes back is claiming this
        file exists and can be read. Tests that asserted restorability while
        no transcript existed were asserting nothing -- they passed a
        `still_readable` stub instead of the real predicate, and the close
        path did the same thing in production (found in review, 2026-08-15).
        """
        day = self.home / ".codex" / "sessions" / "2026" / "08" / "15"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"rollout-2026-08-15T03-00-00-{uuid}.jsonl"
        path.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": uuid}}) + "\n",
            encoding="utf-8",
        )
        path.chmod(mode)
        return path

    def ledger_cannot_be_written(self) -> Path:
        """A closed-sessions append that fails, deterministically.

        A directory where the ledger file belongs makes every append raise
        EISDIR -- the same shape as a full disk or a lost mount, without
        needing either.
        """
        path = self.home / ".local" / "share" / "session-kit" / "closed-sessions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()
        return path

    def closed_sessions_list(self) -> list:
        """What `sp recover` and the picker actually show, via the real verb.

        Not the ledger file and not a stubbed predicate: the shipped reader,
        with the real transcript check, under this fixture's HOME and state.
        """
        completed = run(
            [CORE, "closed-sessions", "list"],
            env=self.environment(),
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)["closed"]

    def without_a_conversation(self) -> None:
        """Launch as `new`, where the shell never learns a conversation UUID.

        Codex allocates its thread ID inside the TUI, so a session started by
        `sp new` runs with an empty conversation until a first-prompt intake
        commits one. Its provider-exit record therefore names no conversation,
        and that is the state any close path has to cope with: there is
        nothing to tombstone and nothing to restore.
        """
        self.launch_mode = "new"
        self.launch_uuid = ""

    def keep_this_session(self) -> None:
        """Mark the session `keep`, the way `keep_session` does, mid-run.

        The lifecycle document does not exist until `lifecycle provider-exited`
        writes it, and `record_provider_exit` carries `keep` forward only from
        a document of the SAME shell generation. So the marker has to be set
        between the exit record and the close -- which is exactly the state a
        session is in when somebody typed `keep_session` in it earlier.

        Chains onto whatever core is already installed; call it after
        `reopen_answers`.
        """
        previous_core = self.core
        wrapper = self.base / "keep-wrapper.py"
        wrapper.write_text(
            f"""#!/usr/bin/env python3
import os, sys
sys.path.insert(0, {str(REPO / "lib")!r})
from pathlib import Path
from sessionkit_inventory import lifecycle as lifecycle_module

if sys.argv[1:3] == ["lifecycle", "closed"]:
    lifecycle_module.update_state(
        Path(os.environ["SESSION_KIT_STATE_DIR"]),
        session_id=os.environ["SESSION_KIT_LIFECYCLE_SESSION_ID"],
        boot_id=os.environ["SESSION_KIT_LIFECYCLE_BOOT_ID"],
        shell_pid=int(os.environ["SESSION_KIT_LIFECYCLE_SHELL_PID"]),
        shell_start_ticks=int(
            os.environ["SESSION_KIT_LIFECYCLE_SHELL_START_TICKS"]
        ),
        event="keep",
        keep=True,
    )
os.execv(
    sys.executable,
    [sys.executable, os.environ["KEEP_NEXT_CORE"], *sys.argv[1:]],
)
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        self.core = wrapper
        self.environment_overrides = {
            **self.environment_overrides,
            "KEEP_NEXT_CORE": str(previous_core),
        }

    def remembered_name(self, title: str, *, uuid: str = UUID) -> None:
        """The name the picker last saw for this session.

        A shell that closes itself knows only its own ID, so the closed row
        takes its name, directory and origin from the last inventory the
        collector wrote (closed_sessions.entry_from_inventory). Without that
        file the row lands nameless, which is the placeholder R3 forbids --
        so the name has to be asserted against a session that HAS one.
        """
        payload = {
            "schema_version": 1,
            "sessions": [
                {
                    "shpool_id_raw": "main2",
                    "provider": "codex",
                    "identity": {"uuid": uuid, "confidence": "exact"},
                    "title": title,
                    "cwd": str(self.project),
                    "origin": "human",
                    "account_alias": "",
                }
            ],
        }
        path = self.state / "inventory.json"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def collector_kept_the_exact_conversation(self, uuid: str = UUID) -> None:
        """Plant the handoff record `persist_last_exact` writes.

        The collector writes it at the one moment a live provider row would
        otherwise be replaced by an idle shell row, and it is bound to this
        boot, this shell PID and this shell start -- values only the running
        fixture shell knows. Writing it from a wrapper around the lifecycle
        verb is the only way to use the real generation; the wrapper takes
        those three from the same environment the shell exports, which is
        exactly what the collector would have recorded.

        Chains onto whatever core is already installed, so it composes with
        `reopen_answers`; call this one last.
        """
        previous_core = self.core
        wrapper = self.base / "retained-exact-wrapper.py"
        wrapper.write_text(
            f"""#!/usr/bin/env python3
import os, sys
sys.path.insert(0, {str(REPO / "lib")!r})
from pathlib import Path
from sessionkit_inventory import lifecycle as lifecycle_module
from sessionkit_inventory.state_io import atomic_write_private_json

if sys.argv[1:3] == ["lifecycle", "closed"]:
    state_dir = Path(os.environ["SESSION_KIT_STATE_DIR"])
    session_id = os.environ["SESSION_KIT_LIFECYCLE_SESSION_ID"]
    path = lifecycle_module._last_exact_path(
        state_dir, session_id, create_key=True
    )
    atomic_write_private_json(path, {{
        "schema_version": 1,
        "session_key": lifecycle_module.session_key(
            state_dir, session_id, create=True
        ),
        "boot_id": os.environ["SESSION_KIT_LIFECYCLE_BOOT_ID"],
        "shell_pid": int(os.environ["SESSION_KIT_LIFECYCLE_SHELL_PID"]),
        "shell_start_ticks": int(
            os.environ["SESSION_KIT_LIFECYCLE_SHELL_START_TICKS"]
        ),
        "provider": "codex",
        "uuid": {uuid!r},
        "title": "Exit Ruling Work",
        "display_title": "Exit Ruling Work",
        "title_source": "provider",
        "recovery": {{
            "available": True,
            "provider": "codex",
            "uuid": {uuid!r},
        }},
    }})
os.execv(
    sys.executable,
    [sys.executable, os.environ["RETAINED_NEXT_CORE"], *sys.argv[1:]],
)
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        self.core = wrapper
        self.environment_overrides = {
            **self.environment_overrides,
            "RETAINED_NEXT_CORE": str(previous_core),
        }

    def _state_mangling_wrapper(self, name: str, body: str) -> None:
        """Damage the lifecycle document at the moment the close reads it.

        The interesting failures are not "the file was always broken" but
        "it broke between the exit record and the close" -- the window a full
        disk or a killed writer opens. Chains onto the installed core.
        """
        previous_core = self.core
        wrapper = self.base / f"{name}-wrapper.py"
        wrapper.write_text(
            f"""#!/usr/bin/env python3
import os, sys
sys.path.insert(0, {str(REPO / "lib")!r})
from pathlib import Path
from sessionkit_inventory import lifecycle as lifecycle_module

if sys.argv[1:3] == ["lifecycle", "closed"]:
    state_dir = Path(os.environ["SESSION_KIT_STATE_DIR"])
    path = lifecycle_module.lifecycle_path(
        state_dir, os.environ["SESSION_KIT_LIFECYCLE_SESSION_ID"]
    )
    if path is not None and path.exists():
{body}
os.execv(
    sys.executable,
    [sys.executable, os.environ["MANGLE_NEXT_CORE"], *sys.argv[1:]],
)
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        self.core = wrapper
        self.environment_overrides = {
            **self.environment_overrides,
            "MANGLE_NEXT_CORE": str(previous_core),
        }

    def delete_the_lifecycle_record(self) -> None:
        self._state_mangling_wrapper("delete-record", "        path.unlink()")

    def corrupt_the_lifecycle_record(self) -> None:
        """Truncate it mid-object, the shape an interrupted write leaves."""
        self._state_mangling_wrapper(
            "corrupt-record",
            "        blob = path.read_bytes()\n"
            "        path.write_bytes(blob[: max(1, len(blob) // 2)])",
        )

    def ledger_rows(self) -> list:
        path = self.home / ".local/share/session-kit/closed-sessions.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def lifecycle_record_from_another_generation(self) -> None:
        """Leave a record that belongs to a PREVIOUS shell of this session id.

        shpool ids are reused: `main2` closes, a new `main2` opens, and the
        old lifecycle document survives until a collector pass prunes it. The
        close loaded that document by session id alone, so the fresh shell
        could tombstone the previous occupant's conversation -- `update_state`
        had guarded exactly this since it was written, and the close had not
        (found in review, 2026-08-15).
        """
        previous_core = self.core
        wrapper = self.base / "stale-generation-wrapper.py"
        wrapper.write_text(
            f"""#!/usr/bin/env python3
import os, sys
sys.path.insert(0, {str(REPO / "lib")!r})
from pathlib import Path
from sessionkit_inventory import lifecycle as lifecycle_module
from sessionkit_inventory.state_io import atomic_write_private_json

if sys.argv[1:3] == ["lifecycle", "closed"]:
    state_dir = Path(os.environ["SESSION_KIT_STATE_DIR"])
    session_id = os.environ["SESSION_KIT_LIFECYCLE_SESSION_ID"]
    document = lifecycle_module.load_state(state_dir, session_id)
    document["shell_pid"] = 424242
    path = lifecycle_module.lifecycle_path(state_dir, session_id)
    atomic_write_private_json(path, document)
os.execv(
    sys.executable,
    [sys.executable, os.environ["STALE_NEXT_CORE"], *sys.argv[1:]],
)
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        self.core = wrapper
        self.environment_overrides = {
            **self.environment_overrides,
            "STALE_NEXT_CORE": str(previous_core),
        }

    def crashing_provider(self, code: int = 3) -> None:
        """A provider that dies badly: the one path that reopens itself."""
        write_executable(
            self.bin / "codex",
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$*" >> "$PROVIDER_LOG"\n'
            f"exit {code}\n",
        )

    def close_intents(self) -> dict:
        path = self.state / "closed-conversations.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8")).get("closed", {})


class ProviderExitShellTests(ProviderExitShellHarness):
    def test_a_clean_exit_closes_the_session_without_a_menu(self) -> None:
        # /exit is the operator saying "done here": the shell ends with the
        # provider, which ends the shpool session. No marker, no menu, no
        # opt-in — tests/test_exit_closes_session.py covers the contract.
        completed = self.launch("")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("Provider exited:", completed.stdout)
        self.assertNotIn("exited with status", completed.stdout)
        # The shell closed rather than returning to the sourcing caller.
        self.assertNotIn("SOURCE_RETURNED", completed.stdout)
        state = self.lifecycle_document()
        self.assertTrue(state["user_input_after_exit"])

    def test_account_record_exports_exact_codex_profile_before_resume(self) -> None:
        wrapper = self.base / "inventory-wrapper.py"
        wrapper.write_text(
            """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:4] == ["account", "resume-profile", "codex"]:
    print(json.dumps({
        "provider": "codex",
        "alias": sys.argv[4],
        "email": "account@example.com",
        "profile_dir": os.environ["ACCOUNT_PROFILE"],
        "plan": "plus",
    }))
    raise SystemExit(0)
os.execv(sys.executable, [sys.executable, os.environ["REAL_CORE"], *sys.argv[1:]])
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        self.core = wrapper
        self.environment_overrides = {
            "ACCOUNT_PROFILE": str(self.account_profile),
            "REAL_CORE": str(CORE),
        }
        completed = self.launch("", account_alias="work")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(
            self.account_environment_log.exists(),
            completed.stdout + completed.stderr,
        )
        self.assertEqual(
            f"{self.account_profile}\twork\t1\n",
            self.account_environment_log.read_text(encoding="utf-8"),
        )
        self.assertFalse((self.start / "main2.account").exists())

    def test_crashed_provider_reopens_itself_without_asking(self) -> None:
        self.crashing_provider()
        completed = self.launch("")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited with status 3", completed.stdout)
        self.assertIn("Codex crashed. Reopened.", completed.stdout)
        self.assertNotIn("Provider exited:", completed.stdout)
        self.assertNotIn("Choice:", completed.stdout)

    def test_a_reopen_that_is_refused_lands_in_a_shell_and_says_why(
        self,
    ) -> None:
        # The reopen guard needs a live daemon generation this harness has no
        # way to provide, so the reopen is refused: the one path where the
        # window is handed back with the session still open.
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited with status 3", completed.stdout)
        self.assertIn("Codex crashed. Reopened.", completed.stdout)
        self.assertIn("nothing reopened", completed.stderr)
        self.assertIn("Shell opened", completed.stdout)
        self.assertIn("SOURCE_RETURNED", completed.stdout)
        self.assertEqual(
            (
                "-c check_for_update_on_startup=false "
                '-c tui.terminal_title=["activity", "thread"] '
                f"--no-alt-screen resume {UUID}\n"
            ),
            self.provider_log.read_text(encoding="utf-8"),
        )
        # Nothing was typed, so nothing is recorded as attended: the reaper's
        # evidence stays the truth about this session.
        self.assertEqual([], sorted((self.state / "lifecycle").glob("*.json"))[2:])
        self.assertFalse((self.start / "main2").exists())
        self.assertFalse((self.start / "main2.expected").exists())

    def test_a_crash_records_no_decision_the_operator_never_made(self) -> None:
        """The crash path asks nothing, so it records no answer.

        Every key of the old menu recorded input before it was dispatched, so
        one typo marked the terminal as attended forever -- the reaper skips
        it from then on. Reopening is the shell healing itself, not a person
        deciding something."""
        log = self.base / "lifecycle-calls.log"
        wrapper = self.base / "inventory-wrapper.py"
        wrapper.write_text(
            r"""#!/usr/bin/env python3
import os, pathlib, sys
if sys.argv[1:3] == ["lifecycle", "user-input"]:
    with pathlib.Path(os.environ["LIFECYCLE_CALL_LOG"]).open("a") as handle:
        handle.write("user-input\n")
os.execv(sys.executable, [sys.executable, os.environ["REAL_CORE"], *sys.argv[1:]])
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        self.core = wrapper
        self.environment_overrides = {
            "LIFECYCLE_CALL_LOG": str(log),
            "REAL_CORE": str(CORE),
        }
        # A crash, so the only records are the ones this menu writes: a clean
        # exit records the operator's own decision to leave before the menu is
        # ever drawn.
        write_executable(
            self.bin / "codex",
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$PROVIDER_LOG"\nexit 3\n',
        )

        completed = self.launch("exit\n")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("Unknown choice", completed.stdout)
        self.assertIn("Codex crashed. Reopened.", completed.stdout)
        self.assertFalse(log.exists(), log.read_text() if log.exists() else "")

    @unittest.skipUnless(
        Path("/proc/1/stat").exists(), "reads a foreign process generation from /proc"
    )
    def test_unrelated_process_cannot_forge_lifecycle_generation(self) -> None:
        pid_one_fields = Path("/proc/1/stat").read_text(encoding="utf-8")
        pid_one_start = int(pid_one_fields[pid_one_fields.rfind(")") + 2 :].split()[19])
        environment = self.environment()
        environment.update(
            {
                "SESSION_KIT_LIFECYCLE_SESSION_ID": "main2",
                "SESSION_KIT_LIFECYCLE_BOOT_ID": BOOT_ID,
                "SESSION_KIT_LIFECYCLE_SHELL_PID": "1",
                "SESSION_KIT_LIFECYCLE_SHELL_START_TICKS": str(pid_one_start),
                "SESSION_KIT_LIFECYCLE_PROVIDER": "codex",
                "SESSION_KIT_LIFECYCLE_EXIT_CODE": "0",
            }
        )
        forged = run(
            [CORE, "lifecycle", "provider-exited"],
            env=environment,
            check=False,
        )
        self.assertNotEqual(0, forged.returncode)
        self.assertIn("outside the exact", forged.stderr)
        lifecycle_dir = self.state / "lifecycle"
        self.assertFalse(lifecycle_dir.exists())


if __name__ == "__main__":
    unittest.main()
