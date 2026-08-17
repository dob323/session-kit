"""The probe must change NOTHING, in every state it can meet.

`__sk_close_keeps_the_conversation` asks `lifecycle closed
--only-with-conversation` which of its two outcomes would happen, and keeps the
session for anything but the good one. Asking is therefore on the hot path of a
session the operator can still type into, and the whole point of the flag is
that a refusal writes nothing at all.

"Nothing at all" is asserted here the only way it can be believed: a sha256 of
every file under the state directory AND the durable data directory, taken in
the same process immediately before the verb runs and immediately after it
returns, byte-compared. A refusal that creates a file, grows the ledger, mints
a terminal number or touches a tombstone fails these tests no matter how
plausible its output looks.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from tests.test_lifecycle_shell import CORE, ProviderExitShellHarness, UUID

REPO = Path(__file__).resolve().parents[1]


TREE_WRAPPER = '''#!/usr/bin/env python3
"""Hash every watched tree around the real verb, without exec-ing away."""
import hashlib, json, os, subprocess, sys
from pathlib import Path

WATCHED = [Path(p) for p in os.environ["PROBE_WATCH"].split(os.pathsep) if p]


def snapshot():
    out = {}
    for root in WATCHED:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                out[str(path) + "/"] = "dir"
                continue
            try:
                blob = path.read_bytes()
            except OSError as exc:
                out[str(path)] = f"unreadable:{exc.errno}"
                continue
            out[str(path)] = hashlib.sha256(blob).hexdigest()
    return out


if sys.argv[1:3] == ["lifecycle", "closed"]:
    before = snapshot()
    completed = subprocess.run(
        [sys.executable, os.environ["PROBE_NEXT_CORE"], *sys.argv[1:]]
    )
    after = snapshot()
    with open(os.environ["PROBE_LOG"], "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "argv": sys.argv[1:],
                    "returncode": completed.returncode,
                    "before": before,
                    "after": after,
                }
            )
            + "\\n"
        )
    raise SystemExit(completed.returncode)
os.execv(
    sys.executable,
    [sys.executable, os.environ["PROBE_NEXT_CORE"], *sys.argv[1:]],
)
'''


class ProbeSideEffectHarness(ProviderExitShellHarness):
    def watch_the_probe(self) -> Path:
        """Record a sha256 of every watched tree either side of the verb.

        Chains onto whatever core is installed; call it last.
        """
        previous_core = self.core
        log = self.base / "probe-trees.jsonl"
        wrapper = self.base / "probe-tree-wrapper.py"
        wrapper.write_text(TREE_WRAPPER, encoding="utf-8")
        wrapper.chmod(0o700)
        self.core = wrapper
        self.environment_overrides = {
            **self.environment_overrides,
            "PROBE_NEXT_CORE": str(previous_core),
            "PROBE_LOG": str(log),
            # State AND the durable data directory: the closed-sessions ledger
            # lives under the second one on purpose, so watching only the
            # first would have missed the exact bug this flag exists for.
            "PROBE_WATCH": os.pathsep.join(
                (str(self.state), str(self.home / ".local" / "share"))
            ),
        }
        return log

    def probe_records(self, log: Path) -> list[dict]:
        self.assertTrue(log.is_file(), "the probe never ran")
        return [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def assert_probe_changed_nothing(self, log: Path) -> dict:
        records = self.probe_records(log)
        self.assertEqual(1, len(records), records)
        record = records[0]
        self.assertIn("--only-with-conversation", record["argv"])
        before, after = record["before"], record["after"]
        created = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(
            path
            for path in set(before) & set(after)
            if before[path] != after[path]
        )
        self.assertEqual([], created, f"the probe CREATED {created}")
        self.assertEqual([], removed, f"the probe REMOVED {removed}")
        self.assertEqual([], changed, f"the probe MODIFIED {changed}")
        return record


class ProbeWritesNothingTests(ProbeSideEffectHarness):
    def test_no_conversation_is_byte_identical(self) -> None:
        """The state the flag exists for."""
        self.without_a_conversation()
        self.reopen_answers(76)
        log = self.watch_the_probe()
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assert_probe_changed_nothing(log)
        self.assertIn("SOURCE_RETURNED", completed.stdout)

    def test_a_kept_session_is_byte_identical(self) -> None:
        """A refusal for a second reason must be just as empty-handed."""
        self.reopen_answers(76)
        log = self.watch_the_probe()
        self.keep_this_session()
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assert_probe_changed_nothing(log)
        self.assertIn("because you asked to keep it", completed.stdout)

    def test_an_absent_lifecycle_record_is_byte_identical(self) -> None:
        """No record at all: the verb has nothing to read and must write none."""
        self.without_a_conversation()
        self.reopen_answers(76)
        log = self.watch_the_probe()
        self.delete_the_lifecycle_record()
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assert_probe_changed_nothing(log)

    def test_a_half_written_lifecycle_record_is_byte_identical(self) -> None:
        """A truncated document is what a probe meets mid-write or after a
        full disk. It must refuse without repairing, replacing or extending
        anything -- a close path that "fixes" state it cannot read is how a
        wrong conversation gets tombstoned."""
        self.without_a_conversation()
        self.reopen_answers(76)
        log = self.watch_the_probe()
        self.corrupt_the_lifecycle_record()
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assert_probe_changed_nothing(log)


class ProbeConcurrencyTests(ProbeSideEffectHarness):
    def test_two_probes_at_once_still_write_nothing(self) -> None:
        """Two shells asking at the same moment.

        A session that crashes while another generation of the same session id
        is deciding is rare, but the answer must not depend on who won: both
        refuse, and between them they leave the trees untouched.
        """
        self.without_a_conversation()
        self.reopen_answers(76)
        log = self.watch_the_probe()
        self.crashing_provider()
        completed = self.launch("exit\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        first = self.assert_probe_changed_nothing(log)

        # Fire the verb again, concurrently, from processes that are NOT the
        # managed shell -- the lifecycle proof must refuse them outright.
        environment = self.environment()
        environment.update(
            {
                "SESSION_KIT_LIFECYCLE_SESSION_ID": "main2",
                "SESSION_KIT_LIFECYCLE_BOOT_ID": "11111111-2222-3333-4444-555555555555",
                "SESSION_KIT_LIFECYCLE_SHELL_PID": "1",
                "SESSION_KIT_LIFECYCLE_SHELL_START_TICKS": "1",
            }
        )
        running = [
            subprocess.Popen(
                [
                    "python3",
                    str(CORE),
                    "lifecycle",
                    "closed",
                    "--only-with-conversation",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        for process in running:
            _, errors = process.communicate(timeout=30)
            self.assertNotEqual(0, process.returncode, "a forged caller was served")
            self.assertIn("outside the exact", errors)
        # The trees are still exactly what the first probe left behind.
        self.assertEqual(first["after"], self._tree_now())

    def _tree_now(self) -> dict:
        out: dict[str, str] = {}
        for root in (self.state, self.home / ".local" / "share"):
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_dir():
                    out[str(path) + "/"] = "dir"
                    continue
                try:
                    out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError as exc:
                    out[str(path)] = f"unreadable:{exc.errno}"
        return out


class FlaglessVerbUnchangedTests(ProbeSideEffectHarness):
    """The other half: a flag on a shared verb must not move its old callers.

    `bye` and a clean provider exit both call `lifecycle closed` with NO flag,
    and they must keep closing exactly as they did.
    """

    def test_a_clean_exit_still_records_the_conversation(self) -> None:
        completed = self.launch("")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited. Closing this session.", completed.stdout)
        self.assertEqual([f"codex:{UUID}"], sorted(self.close_intents()))
        rows = self.ledger_rows()
        self.assertEqual(1, len(rows), rows)
        self.assertEqual("codex", rows[0]["provider"])
        self.assertEqual(UUID, rows[0]["uuid"])

    def test_the_flagless_refusal_still_lands_as_history(self) -> None:
        """The flagless refusal keeps its old job: a `shell` row, on purpose.

        This is the behaviour the flag had to leave alone. A conversation-less
        session that the PERSON ended -- a clean provider exit, no flag -- has
        nothing to tombstone, and it still belongs on the closed list as
        history rather than vanishing. Same code path, opposite decision, and
        the difference is only ever the flag.
        """
        self.without_a_conversation()
        completed = self.launch("")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Codex exited. Closing this session.", completed.stdout)
        rows = self.ledger_rows()
        self.assertEqual(1, len(rows), rows)
        self.assertEqual("shell", rows[0]["provider"])
        self.assertEqual("", rows[0]["uuid"])
        self.assertEqual([], sorted(self.close_intents()))


if __name__ == "__main__":
    import unittest

    unittest.main()
