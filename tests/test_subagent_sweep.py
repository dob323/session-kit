"""The sub-agent sweep: completion means closure, evidence means output.

Every test drives ``sweep()`` against a fake /proc tree and the exact Claude
transcript layout built on disk, so the evidence chain is the real one --
cmdline binding, output snapshots, and stat parsing -- with only the directory
root and the kill hand swapped out.
"""

from __future__ import annotations

import ast
import contextlib
import fcntl
import io
import json
import time
import shutil
import subprocess
import os
from pathlib import Path
import signal
import tempfile
import unittest
import unittest.mock

from tests.support import REPO

from lib.sessionkit_inventory import subagent_sweep


CLAUDE = "/home/user/.local/share/claude/versions/2.1.231"
HOUR = 3600.0


def write_process(
    proc: Path,
    pid: int,
    cmdline: list[str],
    *,
    ppid: int = 1,
    own: int = 0,
    reaped: int = 0,
    start: int = 1000,
    standalone_session: str = "",
) -> None:
    home = proc / str(pid)
    home.mkdir(parents=True, exist_ok=True)
    (home / "cmdline").write_bytes("\0".join(cmdline).encode() + b"\0")
    utime, stime = own, 0
    cutime, cstime = reaped, 0
    tail = ["S", str(ppid)] + ["0"] * 9
    tail += [str(utime), str(stime), str(cutime), str(cstime)]
    tail += ["0"] * 4 + [str(start), "0", "0"]
    (home / "stat").write_text(f"{pid} (co mm) {' '.join(tail)}\n")
    if "--agent-id" in cmdline and "--parent-session-id" in cmdline:
        transcript = (
            standalone_transcript(proc, standalone_session)
            if standalone_session
            else worker_transcript(proc, cmdline)
        )
        transcript.parent.mkdir(parents=True, exist_ok=True)
        if not transcript.exists() and not transcript.is_symlink():
            transcript.write_text("fixture output\n", encoding="utf-8")


def worker_cmdline(agent: str = "wave8@team", parent: str = "abcd-1234") -> list[str]:
    agent_name, _separator, team_name = agent.rpartition("@")
    return [
        CLAUDE,
        "--agent-id",
        agent,
        "--agent-name",
        agent_name,
        "--team-name",
        team_name,
        "--parent-session-id",
        parent,
        "--agent-type",
        "general-purpose",
    ]


def worker_transcript(proc: Path, cmdline: list[str]) -> Path:
    """The exact layout Claude binds to the two worker identity flags."""
    agent = cmdline[cmdline.index("--agent-id") + 1]
    parent = cmdline[cmdline.index("--parent-session-id") + 1]
    return (
        proc.parent
        / "home"
        / ".claude"
        / "projects"
        / "fixture"
        / parent
        / "subagents"
        / f"agent-{agent}.jsonl"
    )


def standalone_transcript(proc: Path, session_id: str) -> Path:
    return (
        proc.parent
        / "home"
        / ".claude"
        / "projects"
        / "fixture"
        / f"{session_id}.jsonl"
    )


def write_declaring_transcript(
    path: Path,
    agent_name: str,
    team_name: str,
    *,
    session_id: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "agentName": agent_name,
                "teamName": team_name,
                "sessionId": path.stem if session_id is None else session_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def write_session_record(
    proc: Path,
    pid: int,
    session_id: str,
    *,
    start: int = 1000,
    claude_root: Path | None = None,
) -> Path:
    root = claude_root or proc.parent / "home" / ".claude"
    record = root / "sessions" / f"{pid}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps(
            {"pid": pid, "procStart": str(start), "sessionId": session_id}
        ),
        encoding="utf-8",
    )
    return record


def output_state(proc: Path, cmdline: list[str]) -> dict[str, object]:
    transcript = worker_transcript(proc, cmdline)
    info = transcript.stat()
    return {
        "output_copies": [
            {
                "path": os.fspath(transcript),
                "dev": info.st_dev,
                "ino": info.st_ino,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
            }
        ],
    }


class SweepHarness(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.proc = Path(scratch.name) / "proc"
        self.proc.mkdir()
        self.home = Path(scratch.name) / "home"
        self.home.mkdir()
        self.state_dir = Path(scratch.name) / "state"
        self.state_dir.mkdir()
        self.kills: list[tuple[int, int]] = []
        # Passes move forward, as they do in life: the sweep starts its clocks
        # fresh if it ever sees time run backwards.
        self.clock = 0.0

    def kill(self, pid: int, signum: int) -> None:
        self.kills.append((pid, signum))

    def run_sweep(self, now: float, environ: dict[str, str] | None = None, **kw):
        self.clock = max(self.clock, now)
        sweep_environ = {"HOME": os.fspath(self.home), **(environ or {})}
        return subagent_sweep.sweep(
            proc=self.proc,
            state_dir=self.state_dir,
            environ=sweep_environ,
            now=now,
            kill=self.kill,
            **kw,
        )

    def sweep_until(
        self,
        end: float,
        environ: dict[str, str] | None = None,
        *,
        start: float | None = None,
        **kw,
    ):
        """Run passes at a real cadence up to `end`, returning the last actions.

        Nothing is closed on evidence older than the window, so a test that
        jumps straight from t=0 to t=7h is not describing a sweep that ever
        runs -- it is describing the install-day gap the sweep now refuses to
        act on. Passes here land a third of a window apart, which is what the
        five-minute timer does against a fifteen-minute rule.
        """
        window = subagent_sweep._env_idle_hours(environ or {}) * 3600
        step = max((window or 900.0) / 3.0, 1.0)
        moment = self.clock if start is None else start
        actions = self.run_sweep(now=moment, environ=environ, **kw)
        while not actions and moment < end:
            moment = min(moment + step, end)
            actions = self.run_sweep(now=moment, environ=environ, **kw)
        # Stops at the first pass that acts: running on would escalate the
        # TERM it just found into a KILL and hide what the caller asked about.
        return actions


MINUTE = 60.0
CURRENT_SESSION = "12345678-1234-4234-8234-123456789abc"
OTHER_SESSION = "abcdef01-abcd-4abc-8abc-abcdef012345"


class TheWorkerOutputBinding(SweepHarness):
    def _find(self, **extra: str) -> list[dict[str, object]]:
        return subagent_sweep.find_subagents(
            self.proc, {"HOME": os.fspath(self.home), **extra}
        )

    def test_legacy_agent_transcript_produces_a_bound_snapshot(self) -> None:
        cmdline = worker_cmdline("legacy@team", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        write_process(self.proc, 500, cmdline)

        agents = self._find()
        snapshot, reason = subagent_sweep._output_snapshot(
            agents[0], {"HOME": os.fspath(self.home)}
        )

        self.assertEqual("", reason)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(
            [os.fspath(worker_transcript(self.proc, cmdline))],
            [copy["path"] for copy in snapshot["output_copies"]],
        )
        self.assertEqual(
            worker_transcript(self.proc, cmdline).stat().st_size,
            snapshot["output_copies"][0]["size"],
        )

    def test_standalone_session_transcript_produces_a_bound_snapshot(self) -> None:
        cmdline = worker_cmdline("named@session-12345678")
        write_process(
            self.proc,
            501,
            cmdline,
            standalone_session=CURRENT_SESSION,
        )
        write_session_record(self.proc, 501, CURRENT_SESSION)

        agents = self._find()
        identity, identity_error = subagent_sweep._worker_session_identity(
            agents[0], {"HOME": os.fspath(self.home)}
        )
        self.assertEqual((CURRENT_SESSION, ""), (identity, identity_error))
        snapshot, reason = subagent_sweep._output_snapshot(
            agents[0], {"HOME": os.fspath(self.home)}
        )

        self.assertEqual("", reason)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(
            [os.fspath(standalone_transcript(self.proc, CURRENT_SESSION))],
            [copy["path"] for copy in snapshot["output_copies"]],
        )

    def test_undecidable_standalone_identity_refuses_with_the_file_name(self) -> None:
        write_process(
            self.proc,
            502,
            worker_cmdline("missing@session-12345678"),
            standalone_session=CURRENT_SESSION,
        )
        # Larger than both probe windows with no parseable identity record
        # anywhere near either end: genuinely undecidable. A small file seen
        # end to end decides non-member instead (its own test below).
        oversized = standalone_transcript(self.proc, CURRENT_SESSION)
        oversized.parent.mkdir(parents=True, exist_ok=True)
        junk_line = b"x" * 1024 + b"\n"
        oversized.write_bytes(
            junk_line * (3 * subagent_sweep._MAX_TRANSCRIPT_PROBE_BYTES // len(junk_line))
        )
        agent = self._find()[0]

        snapshot, reason = subagent_sweep._output_snapshot(
            agent, {"HOME": os.fspath(self.home)}
        )

        self.assertIsNone(snapshot)
        self.assertEqual(
            "worker transcript membership undecidable: "
            f"{standalone_transcript(self.proc, CURRENT_SESSION)}",
            reason,
        )
        self.assertEqual([], self.run_sweep(now=0.0))
        refusal = json.loads(
            (self.state_dir / "subagent-sweep.log").read_text(encoding="utf-8")
        )
        self.assertEqual("refused-output", refusal["decision"])
        self.assertEqual(reason, refusal["reason"])

    def test_two_standalone_session_identities_refuse_as_ambiguous(self) -> None:
        write_process(
            self.proc,
            503,
            worker_cmdline("ambiguous@session-12345678"),
            standalone_session=CURRENT_SESSION,
        )
        write_session_record(self.proc, 503, CURRENT_SESSION)
        alternate = self.proc.parent / "alternate-claude"
        write_session_record(
            self.proc, 503, OTHER_SESSION, claude_root=alternate
        )
        for session_id in (CURRENT_SESSION, OTHER_SESSION):
            root = self.home / ".claude" if session_id == CURRENT_SESSION else alternate
            transcript = root / "projects" / "fixture" / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("exact candidate\n", encoding="utf-8")
        agent = self._find(CLAUDE_CONFIG_DIR=os.fspath(alternate))[0]

        snapshot, reason = subagent_sweep._output_snapshot(
            agent,
            {
                "HOME": os.fspath(self.home),
                "CLAUDE_CONFIG_DIR": os.fspath(alternate),
            },
        )

        self.assertIsNone(snapshot)
        self.assertEqual(
            "worker session identity is ambiguous across exact session records",
            reason,
        )

    def test_stale_pid_session_record_refuses_with_the_generation_field(self) -> None:
        write_process(
            self.proc,
            507,
            worker_cmdline("recycled@session-12345678"),
            standalone_session=CURRENT_SESSION,
        )
        write_session_record(self.proc, 507, CURRENT_SESSION, start=9999)

        agent = self._find()[0]
        snapshot, reason = subagent_sweep._output_snapshot(
            agent, {"HOME": os.fspath(self.home)}
        )

        self.assertIsNone(snapshot)
        self.assertEqual(
            "no exact worker session record matches the worker procStart", reason
        )

    def test_an_unreadable_exact_copy_refuses_all_readable_copies(self) -> None:
        cmdline = worker_cmdline("copies@session-12345678")
        write_process(
            self.proc,
            504,
            cmdline,
            standalone_session=CURRENT_SESSION,
        )
        write_session_record(self.proc, 504, CURRENT_SESSION)
        alternate = self.proc.parent / "alternate-claude"
        bad = alternate / "projects" / "fixture" / f"{CURRENT_SESSION}.jsonl"
        bad.parent.mkdir(parents=True)
        bad.symlink_to(bad.with_name("missing.jsonl"))
        agent = self._find()[0]

        snapshot, reason = subagent_sweep._output_snapshot(
            agent,
            {
                "HOME": os.fspath(self.home),
                "CLAUDE_CONFIG_DIR": os.fspath(alternate),
            },
        )

        self.assertIsNone(snapshot)
        self.assertIn("cannot read worker output transcript", reason)
        self.assertIn(os.fspath(bad), reason)

    def test_every_readable_exact_copy_is_published_in_path_order(self) -> None:
        cmdline = worker_cmdline("newest@session-12345678")
        write_process(
            self.proc,
            505,
            cmdline,
            standalone_session=CURRENT_SESSION,
        )
        write_session_record(self.proc, 505, CURRENT_SESSION)
        older = standalone_transcript(self.proc, CURRENT_SESSION)
        alternate = self.proc.parent / "alternate-claude"
        newer = alternate / "projects" / "fixture" / f"{CURRENT_SESSION}.jsonl"
        newer.parent.mkdir(parents=True)
        newer.write_text("newer exact copy\n", encoding="utf-8")
        os.utime(older, ns=(1_000_000_000, 1_000_000_000))
        os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
        agent = self._find()[0]

        snapshot, reason = subagent_sweep._output_snapshot(
            agent,
            {
                "HOME": os.fspath(self.home),
                "CLAUDE_CONFIG_DIR": os.fspath(alternate),
            },
        )

        self.assertEqual("", reason)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(
            sorted((os.fspath(older), os.fspath(newer))),
            [copy["path"] for copy in snapshot["output_copies"]],
        )
        by_path = {copy["path"]: copy for copy in snapshot["output_copies"]}
        self.assertEqual(1_000_000_000, by_path[os.fspath(older)]["mtime_ns"])
        self.assertEqual(2_000_000_000, by_path[os.fspath(newer)]["mtime_ns"])

    def test_process_table_churn_keeps_only_same_generation_identity(self) -> None:
        cmdline = worker_cmdline("churn@session-12345678")
        write_process(
            self.proc,
            506,
            cmdline,
            own=1,
            standalone_session=CURRENT_SESSION,
        )
        write_session_record(self.proc, 506, CURRENT_SESSION)
        stable = self._find()
        real_reader = subagent_sweep._read_cmdline
        reads = 0

        def cpu_churn(proc: Path, pid: int):
            nonlocal reads
            answer = real_reader(proc, pid)
            reads += 1
            if reads == 1:
                write_process(
                    proc,
                    pid,
                    cmdline,
                    own=999_999,
                    standalone_session=CURRENT_SESSION,
                )
            return answer

        with unittest.mock.patch.object(
            subagent_sweep, "_read_cmdline", side_effect=cpu_churn
        ):
            churned = self._find()
        self.assertEqual(stable, churned)

        reads = 0

        def generation_churn(proc: Path, pid: int):
            nonlocal reads
            answer = real_reader(proc, pid)
            reads += 1
            if reads == 1:
                write_process(
                    proc,
                    pid,
                    cmdline,
                    start=9999,
                    standalone_session=CURRENT_SESSION,
                )
            return answer

        with unittest.mock.patch.object(
            subagent_sweep, "_read_cmdline", side_effect=generation_churn
        ):
            self.assertEqual([], self._find())


class TheRegistrylessWorkerOutputBinding(SweepHarness):
    def _agent(self) -> dict[str, object]:
        return subagent_sweep.find_subagents(
            self.proc, {"HOME": os.fspath(self.home)}
        )[0]

    def test_declaring_transcript_binds_and_idle_worker_reaches_sigterm(self) -> None:
        cmdline = worker_cmdline("sweep@proof@session-933f74c7")
        write_process(
            self.proc, 520, cmdline, standalone_session=CURRENT_SESSION
        )
        transcript = standalone_transcript(self.proc, CURRENT_SESSION)
        write_declaring_transcript(
            transcript, "sweep@proof", "session-933f74c7"
        )

        self.assertEqual([], self.run_sweep(now=0.0))
        self.assertEqual([], self.run_sweep(now=5 * MINUTE))
        self.assertEqual([], self.run_sweep(now=10 * MINUTE))
        actions = self.run_sweep(now=15 * MINUTE)

        self.assertEqual(["SIGTERM"], [action["signal"] for action in actions])
        self.assertEqual([(520, signal.SIGTERM)], self.kills)

    def test_old_same_identity_copy_and_other_project_join_the_moving_set(
        self,
    ) -> None:
        cmdline = worker_cmdline("restarted@session-933f74c7")
        write_process(
            self.proc, 521, cmdline, standalone_session=CURRENT_SESSION
        )
        live = standalone_transcript(self.proc, CURRENT_SESSION)
        write_declaring_transcript(live, "restarted", "session-933f74c7")
        quiet = (
            self.home
            / ".claude"
            / "projects"
            / "previous-run"
            / f"{OTHER_SESSION}.jsonl"
        )
        write_declaring_transcript(quiet, "restarted", "session-933f74c7")

        snapshot, reason = subagent_sweep._output_snapshot(
            self._agent(), {"HOME": os.fspath(self.home)}
        )
        self.assertEqual("", reason)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(
            sorted((os.fspath(live), os.fspath(quiet))),
            [copy["path"] for copy in snapshot["output_copies"]],
        )

        self.assertEqual([], self.run_sweep(now=0.0))
        for minute in (5, 10, 15, 20):
            with live.open("a", encoding="utf-8") as handle:
                handle.write(f"live output at {minute}\n")
            self.assertEqual([], self.run_sweep(now=minute * MINUTE))
        self.assertEqual([], self.kills)

    def test_declaring_transcript_with_wrong_session_id_refuses(self) -> None:
        cmdline = worker_cmdline("mismatch@session-933f74c7")
        write_process(
            self.proc, 522, cmdline, standalone_session=CURRENT_SESSION
        )
        transcript = standalone_transcript(self.proc, CURRENT_SESSION)
        write_declaring_transcript(
            transcript,
            "mismatch",
            "session-933f74c7",
            session_id=OTHER_SESSION,
        )

        snapshot, reason = subagent_sweep._output_snapshot(
            self._agent(), {"HOME": os.fspath(self.home)}
        )

        self.assertIsNone(snapshot)
        self.assertIn("sessionId", reason)

    def test_declaring_transcript_that_becomes_unreadable_refuses(self) -> None:
        cmdline = worker_cmdline("unreadable@session-933f74c7")
        write_process(
            self.proc, 523, cmdline, standalone_session=CURRENT_SESSION
        )
        transcript = standalone_transcript(self.proc, CURRENT_SESSION)
        write_declaring_transcript(
            transcript, "unreadable", "session-933f74c7"
        )

        refusal = f"cannot read worker output transcript: {transcript}"
        with unittest.mock.patch.object(
            subagent_sweep,
            "_content_transcript_probe",
            return_value=(False, None, refusal),
        ):
            snapshot, reason = subagent_sweep._output_snapshot(
                self._agent(), {"HOME": os.fspath(self.home)}
            )

        self.assertIsNone(snapshot)
        self.assertIn("cannot read worker output transcript", reason)
        self.assertIn(os.fspath(transcript), reason)

    def test_valid_declaration_beyond_the_head_window_is_a_member(self) -> None:
        cmdline = worker_cmdline("oversized@session-933f74c7")
        write_process(
            self.proc, 524, cmdline, standalone_session=CURRENT_SESSION
        )
        transcript = standalone_transcript(self.proc, CURRENT_SESSION)
        raw_pair = b'"agentName":"oversized","teamName":"session-933f74c7"'
        prefix = raw_pair + b"x" * (
            subagent_sweep._MAX_TRANSCRIPT_PROBE_BYTES - len(raw_pair)
        )
        declaration = json.dumps(
            {
                "agentName": "oversized",
                "teamName": "session-933f74c7",
                "sessionId": CURRENT_SESSION,
            }
        ).encode()
        transcript.write_bytes(prefix + b"\n" + declaration + b"\n")

        snapshot, reason = subagent_sweep._output_snapshot(
            self._agent(), {"HOME": os.fspath(self.home)}
        )

        self.assertEqual("", reason)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(
            [os.fspath(transcript)],
            [copy["path"] for copy in snapshot["output_copies"]],
        )

    def test_exact_probe_limit_final_line_without_newline_is_a_member(self) -> None:
        transcript = standalone_transcript(self.proc, CURRENT_SESSION)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        declaration = json.dumps(
            {
                "agentName": "boundary",
                "teamName": "session-933f74c7",
                "sessionId": CURRENT_SESSION,
            }
        ).encode()
        transcript.write_bytes(
            declaration
            + b" "
            * (subagent_sweep._MAX_TRANSCRIPT_PROBE_BYTES - len(declaration))
        )

        member, reason = subagent_sweep._content_transcript_member(
            transcript, "boundary", "session-933f74c7"
        )

        self.assertTrue(member)
        self.assertEqual("", reason)

    def test_a_fully_scanned_identityless_transcript_decides_non_member(
        self,
    ) -> None:
        """An aborted session leaves a small setup-records-only transcript.

        Every live estate has one. Its complete scan proves no record carries
        the agent identity fields, so it decides non-member instead of
        refusing the whole answer as undecidable (live finding, 2026-08-16:
        a five-day-old aborted-session file froze every binding)."""
        transcript = standalone_transcript(self.proc, CURRENT_SESSION)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"type": "agent-color", "agentColor": "pink", "sessionId": CURRENT_SESSION},
            {"type": "mode", "mode": "normal", "sessionId": CURRENT_SESSION},
        ]
        transcript.write_bytes(
            b"\n".join(json.dumps(record).encode() for record in records) + b"\n"
        )

        member, reason = subagent_sweep._content_transcript_member(
            transcript, "boundary", "session-933f74c7"
        )

        self.assertFalse(member)
        self.assertEqual("", reason)

    def test_new_registry_record_without_its_transcript_refuses_old_content(
        self,
    ) -> None:
        cmdline = worker_cmdline("registry-race@session-933f74c7")
        write_process(
            self.proc, 525, cmdline, standalone_session=CURRENT_SESSION
        )
        old = standalone_transcript(self.proc, CURRENT_SESSION)
        write_declaring_transcript(old, "registry-race", "session-933f74c7")
        agent = self._agent()
        write_session_record(self.proc, 525, OTHER_SESSION)

        snapshot, reason = subagent_sweep._output_snapshot(
            agent, {"HOME": os.fspath(self.home)}
        )

        self.assertIsNone(snapshot)
        self.assertEqual(
            "worker output transcript for exact sessionId was not found", reason
        )

    def test_shape_without_agent_type_does_not_use_content_fallback(self) -> None:
        cmdline = worker_cmdline("partial@session-933f74c7")
        type_index = cmdline.index("--agent-type")
        del cmdline[type_index : type_index + 2]
        write_process(
            self.proc, 526, cmdline, standalone_session=CURRENT_SESSION
        )
        transcript = standalone_transcript(self.proc, CURRENT_SESSION)
        write_declaring_transcript(transcript, "partial", "session-933f74c7")

        snapshot, reason = subagent_sweep._output_snapshot(
            self._agent(), {"HOME": os.fspath(self.home)}
        )

        self.assertIsNone(snapshot)
        self.assertEqual(
            "worker session identity is missing exact sessions/<pid>.json record",
            reason,
        )


class C10RoundTwoStructuralRaces(SweepHarness):
    def _agent(self) -> dict[str, object]:
        return subagent_sweep.find_subagents(
            self.proc, {"HOME": os.fspath(self.home)}
        )[0]

    def _tracked_record(self) -> dict[str, object]:
        state = json.loads(
            (self.state_dir / "subagent-sweep.state.json").read_text()
        )
        self.assertEqual(1, len(state["tracked"]))
        return next(iter(state["tracked"].values()))

    def test_tail_only_live_copy_and_quiet_decoy_never_signal(self) -> None:
        cmdline = worker_cmdline("tail-live@session-933f74c7")
        write_process(
            self.proc, 530, cmdline, standalone_session=CURRENT_SESSION
        )
        live = standalone_transcript(self.proc, CURRENT_SESSION)
        declaration = json.dumps(
            {
                "type": "assistant",
                "agentName": "tail-live",
                "teamName": "session-933f74c7",
                "sessionId": CURRENT_SESSION,
            }
        ).encode()
        prefix = b"x" * (subagent_sweep._MAX_TRANSCRIPT_PROBE_BYTES + 1024)
        live.write_bytes(prefix + b"\n" + declaration + b"\n")
        self.assertGreater(live.read_bytes().index(declaration),
                           subagent_sweep._MAX_TRANSCRIPT_PROBE_BYTES)

        decoy = live.with_name(f"{OTHER_SESSION}.jsonl")
        write_declaring_transcript(
            decoy, "tail-live", "session-933f74c7"
        )
        snapshot, reason = subagent_sweep._output_snapshot(
            self._agent(), {"HOME": os.fspath(self.home)}
        )
        self.assertEqual("", reason)
        assert snapshot is not None
        self.assertEqual(
            sorted((os.fspath(live), os.fspath(decoy))),
            [copy["path"] for copy in snapshot["output_copies"]],
        )

        self.assertEqual([], self.run_sweep(now=0.0))
        for minute in (5, 10, 15, 20):
            with live.open("ab") as handle:
                handle.write(declaration + b"\n")
            self.assertEqual([], self.run_sweep(now=minute * MINUTE))
            self.assertIn(
                os.fspath(live),
                [copy["path"] for copy in self._tracked_record()["output_copies"]],
            )
        self.assertEqual([], self.kills)

    def test_same_size_same_mtime_path_swap_resets_before_term(self) -> None:
        cmdline = worker_cmdline("swap@session-933f74c7")
        write_process(
            self.proc, 531, cmdline, standalone_session=CURRENT_SESSION
        )
        transcript = standalone_transcript(self.proc, CURRENT_SESSION)
        write_declaring_transcript(transcript, "swap", "session-933f74c7")
        self.run_sweep(now=0.0)
        self.run_sweep(now=5 * MINUTE)
        self.run_sweep(now=10 * MINUTE)
        armed = self._tracked_record()["output_copies"][0]

        replacement = transcript.with_suffix(".swap")
        replacement.write_bytes(transcript.read_bytes())
        os.utime(
            replacement,
            ns=(int(armed["mtime_ns"]), int(armed["mtime_ns"])),
        )
        real_fstat = os.fstat
        stable_stats = 0

        def swap_after_probe(descriptor: int):
            nonlocal stable_stats
            info = real_fstat(descriptor)
            if info.st_ino == armed["ino"]:
                stable_stats += 1
                if stable_stats == 2:
                    os.replace(replacement, transcript)
            return info

        with unittest.mock.patch.object(
            subagent_sweep.os, "fstat", side_effect=swap_after_probe
        ):
            actions = self.run_sweep(now=15 * MINUTE)

        self.assertEqual([], actions)
        self.assertEqual([], self.kills)
        reset = self._tracked_record()
        current = transcript.stat()
        self.assertEqual(15 * MINUTE, reset["last_active"])
        self.assertEqual(current.st_ino, reset["output_copies"][0]["ino"])
        self.assertNotEqual(armed["ino"], reset["output_copies"][0]["ino"])
        self.assertEqual(armed["size"], reset["output_copies"][0]["size"])
        self.assertEqual(armed["mtime_ns"], reset["output_copies"][0]["mtime_ns"])

    def test_registry_created_in_term_guard_vetoes_and_next_pass_binds(self) -> None:
        cmdline = worker_cmdline("late-registry@session-933f74c7")
        write_process(
            self.proc, 532, cmdline, standalone_session=CURRENT_SESSION
        )
        old = standalone_transcript(self.proc, CURRENT_SESSION)
        write_declaring_transcript(old, "late-registry", "session-933f74c7")
        for minute in (0, 5, 10):
            self.assertEqual([], self.run_sweep(now=minute * MINUTE))
        registry = standalone_transcript(self.proc, OTHER_SESSION)
        real_guard = subagent_sweep._still_the_worker
        created = False

        def create_registry_in_guard(proc: Path, pid: int, start: int) -> bool:
            nonlocal created
            if not created:
                created = True
                write_session_record(self.proc, 532, OTHER_SESSION)
                write_declaring_transcript(
                    registry, "late-registry", "session-933f74c7"
                )
            return real_guard(proc, pid, start)

        with unittest.mock.patch.object(
            subagent_sweep,
            "_still_the_worker",
            side_effect=create_registry_in_guard,
        ):
            actions = self.run_sweep(now=15 * MINUTE)

        self.assertEqual([], actions)
        self.assertEqual([], self.kills)
        self.assertEqual(
            [os.fspath(registry)],
            [copy["path"] for copy in self._tracked_record()["output_copies"]],
        )
        with registry.open("a", encoding="utf-8") as handle:
            handle.write("active registry output\n")
        self.assertEqual([], self.run_sweep(now=20 * MINUTE))
        self.assertEqual(
            [os.fspath(registry)],
            [copy["path"] for copy in self._tracked_record()["output_copies"]],
        )

    def test_registry_created_in_kill_guard_vetoes_only_that_pass(self) -> None:
        cmdline = worker_cmdline("late-kill@session-933f74c7")
        write_process(
            self.proc, 533, cmdline, standalone_session=CURRENT_SESSION
        )
        old = standalone_transcript(self.proc, CURRENT_SESSION)
        write_declaring_transcript(old, "late-kill", "session-933f74c7")
        for minute in (0, 5, 10):
            self.assertEqual([], self.run_sweep(now=minute * MINUTE))
        self.assertEqual(["SIGTERM"], [
            action["signal"] for action in self.run_sweep(now=15 * MINUTE)
        ])
        registry = standalone_transcript(self.proc, OTHER_SESSION)
        real_guard = subagent_sweep._still_the_worker
        created = False

        def create_registry_in_guard(proc: Path, pid: int, start: int) -> bool:
            nonlocal created
            if not created:
                created = True
                write_session_record(self.proc, 533, OTHER_SESSION)
                write_declaring_transcript(
                    registry, "late-kill", "session-933f74c7"
                )
            return real_guard(proc, pid, start)

        with unittest.mock.patch.object(
            subagent_sweep,
            "_still_the_worker",
            side_effect=create_registry_in_guard,
        ):
            self.assertEqual([], self.run_sweep(now=20 * MINUTE))
        self.assertEqual([(533, signal.SIGTERM)], self.kills)
        tracked = self._tracked_record()
        self.assertTrue(tracked["term_sent"])
        self.assertEqual(
            [os.fspath(registry)],
            [copy["path"] for copy in tracked["output_copies"]],
        )

        actions = self.run_sweep(now=25 * MINUTE)
        self.assertEqual(["SIGKILL"], [action["signal"] for action in actions])
        self.assertEqual(
            [(533, signal.SIGTERM), (533, signal.SIGKILL)], self.kills
        )

    def test_undecidable_uuid_transcript_refuses_the_whole_answer(self) -> None:
        cmdline = worker_cmdline("junk@session-933f74c7")
        write_process(
            self.proc, 534, cmdline, standalone_session=CURRENT_SESSION
        )
        transcript = standalone_transcript(self.proc, CURRENT_SESSION)
        transcript.write_bytes(
            b"not json\n" * (subagent_sweep._MAX_TRANSCRIPT_PROBE_BYTES // 4)
        )
        expected = f"worker transcript membership undecidable: {transcript}"

        snapshot, reason = subagent_sweep._output_snapshot(
            self._agent(), {"HOME": os.fspath(self.home)}
        )
        self.assertIsNone(snapshot)
        self.assertEqual(expected, reason)
        for minute in (0, 5, 10, 15, 20):
            self.assertEqual([], self.run_sweep(now=minute * MINUTE))
        self.assertEqual([], self.kills)
        refusals = [
            json.loads(line)
            for line in (self.state_dir / "subagent-sweep.log").read_text().splitlines()
        ]
        self.assertTrue(refusals)
        self.assertTrue(all(record["reason"] == expected for record in refusals))

    def test_wrong_identity_and_root_uuid_are_confidently_excluded(self) -> None:
        cmdline = worker_cmdline("true-one@session-933f74c7")
        write_process(
            self.proc, 535, cmdline, standalone_session=CURRENT_SESSION
        )
        true_copy = standalone_transcript(self.proc, CURRENT_SESSION)
        write_declaring_transcript(true_copy, "true-one", "session-933f74c7")
        wrong = true_copy.with_name(f"{OTHER_SESSION}.jsonl")
        write_declaring_transcript(wrong, "another", "team")
        root_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        root = true_copy.with_name(f"{root_id}.jsonl")
        root.write_text(
            json.dumps(
                {"type": "assistant", "sessionId": root_id, "message": "root"}
            ) + "\n"
        )

        snapshot, reason = subagent_sweep._output_snapshot(
            self._agent(), {"HOME": os.fspath(self.home)}
        )

        self.assertEqual("", reason)
        assert snapshot is not None
        self.assertEqual(
            [os.fspath(true_copy)],
            [copy["path"] for copy in snapshot["output_copies"]],
        )

    def test_prior_three_field_copy_evidence_rearms_by_inequality(self) -> None:
        now = 10_000.0
        cmdline = worker_cmdline("migration@team")
        write_process(self.proc, 536, cmdline)
        transcript = worker_transcript(self.proc, cmdline)
        info = transcript.stat()
        boot = subagent_sweep._boot_id(self.proc)
        narrow = {
            "path": os.fspath(transcript),
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
        }
        (self.state_dir / "subagent-sweep.state.json").write_text(
            json.dumps(
                {
                    "version": subagent_sweep.STATE_VERSION,
                    "tracked": {
                        f"{boot}:536:1000": {
                            "output_copies": [narrow],
                            "last_active": now - 20 * MINUTE,
                            "term_sent": False,
                        }
                    },
                    "boot_id": boot,
                    "last_pass": now - 5 * MINUTE,
                    "window_seconds": 15 * MINUTE,
                }
            )
        )

        self.assertEqual([], self.run_sweep(now=now))
        self.assertEqual([], self.kills)
        tracked = self._tracked_record()
        self.assertEqual(now, tracked["last_active"])
        self.assertEqual(
            {"path", "dev", "ino", "size", "mtime_ns", "ctime_ns"},
            set(tracked["output_copies"][0]),
        )


class EveryTranscriptCopyIsTheIdleClock(SweepHarness):
    def test_a_future_dated_quiet_copy_cannot_hide_a_moving_live_transcript(
        self,
    ) -> None:
        cmdline = worker_cmdline("moving@session-12345678")
        write_process(
            self.proc, 510, cmdline, standalone_session=CURRENT_SESSION
        )
        write_session_record(self.proc, 510, CURRENT_SESSION)
        live = standalone_transcript(self.proc, CURRENT_SESSION)
        alternate = self.proc.parent / "future-profile"
        quiet = alternate / "projects" / "fixture" / f"{CURRENT_SESSION}.jsonl"
        quiet.parent.mkdir(parents=True)
        quiet.write_text("quiet copy\n", encoding="utf-8")
        future_ns = 2_000_000_000_000_000_000
        os.utime(quiet, ns=(future_ns, future_ns))
        environ = {"CLAUDE_CONFIG_DIR": os.fspath(alternate)}

        self.assertEqual([], self.run_sweep(now=0.0, environ=environ))
        for minute in (5, 10, 15, 20):
            with live.open("a", encoding="utf-8") as handle:
                handle.write(f"live output at {minute}\n")
            self.assertEqual([], self.run_sweep(now=minute * MINUTE, environ=environ))
        self.assertEqual([], self.kills)

    def test_a_copy_stopping_while_another_moves_keeps_resetting(self) -> None:
        cmdline = worker_cmdline(
            "legacy-moving@team", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        write_process(self.proc, 511, cmdline)
        live = worker_transcript(self.proc, cmdline)
        alternate = self.proc.parent / "legacy-profile"
        quiet = (
            alternate
            / "projects"
            / "fixture"
            / cmdline[cmdline.index("--parent-session-id") + 1]
            / "subagents"
            / live.name
        )
        quiet.parent.mkdir(parents=True)
        quiet.write_text("copy before it stops\n", encoding="utf-8")
        future_ns = 2_000_000_000_000_000_000
        os.utime(quiet, ns=(future_ns, future_ns))
        environ = {"CLAUDE_CONFIG_DIR": os.fspath(alternate)}

        self.assertEqual([], self.run_sweep(now=0.0, environ=environ))
        with quiet.open("a", encoding="utf-8") as handle:
            handle.write("copy's final output\n")
        os.utime(quiet, ns=(future_ns, future_ns))
        self.assertEqual([], self.run_sweep(now=5 * MINUTE, environ=environ))
        for minute in (10, 15, 20):
            with live.open("a", encoding="utf-8") as handle:
                handle.write(f"other copy at {minute}\n")
            self.assertEqual([], self.run_sweep(now=minute * MINUTE, environ=environ))
        self.assertEqual([], self.kills)

    def test_identical_copies_all_quiet_for_the_window_fire_idle(self) -> None:
        cmdline = worker_cmdline("quiet@session-12345678")
        write_process(
            self.proc, 512, cmdline, standalone_session=CURRENT_SESSION
        )
        write_session_record(self.proc, 512, CURRENT_SESSION)
        live = standalone_transcript(self.proc, CURRENT_SESSION)
        alternate = self.proc.parent / "quiet-profile"
        copy = alternate / "projects" / "fixture" / f"{CURRENT_SESSION}.jsonl"
        copy.parent.mkdir(parents=True)
        shutil.copyfile(live, copy)
        identical_ns = 1_900_000_000_000_000_000
        os.utime(live, ns=(identical_ns, identical_ns))
        os.utime(copy, ns=(identical_ns, identical_ns))
        environ = {"CLAUDE_CONFIG_DIR": os.fspath(alternate)}

        self.assertEqual([], self.run_sweep(now=0.0, environ=environ))
        self.assertEqual([], self.run_sweep(now=5 * MINUTE, environ=environ))
        self.assertEqual([], self.run_sweep(now=10 * MINUTE, environ=environ))
        actions = self.run_sweep(now=15 * MINUTE, environ=environ)

        self.assertEqual(["SIGTERM"], [action["signal"] for action in actions])
        self.assertEqual([(512, signal.SIGTERM)], self.kills)

    def test_appearance_disappearance_shrink_and_mtime_each_reset(self) -> None:
        cmdline = worker_cmdline(
            "set-changes@team", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        )
        write_process(self.proc, 513, cmdline)
        live = worker_transcript(self.proc, cmdline)
        alternate = self.proc.parent / "changing-profile"
        copy = (
            alternate
            / "projects"
            / "fixture"
            / cmdline[cmdline.index("--parent-session-id") + 1]
            / "subagents"
            / live.name
        )
        environ = {"CLAUDE_CONFIG_DIR": os.fspath(alternate)}

        self.assertEqual([], self.run_sweep(now=0.0, environ=environ))
        copy.parent.mkdir(parents=True)
        copy.write_text("appeared\n", encoding="utf-8")
        self.assertEqual([], self.run_sweep(now=5 * MINUTE, environ=environ))
        copy.unlink()
        self.assertEqual([], self.run_sweep(now=10 * MINUTE, environ=environ))
        live.write_text("x", encoding="utf-8")
        self.assertEqual([], self.run_sweep(now=15 * MINUTE, environ=environ))
        changed_ns = live.stat().st_mtime_ns + 1_000_000_000
        os.utime(live, ns=(changed_ns, changed_ns))
        self.assertEqual([], self.run_sweep(now=20 * MINUTE, environ=environ))
        self.assertEqual([], self.run_sweep(now=30 * MINUTE, environ=environ))
        actions = self.run_sweep(now=35 * MINUTE, environ=environ)

        self.assertEqual(["SIGTERM"], [action["signal"] for action in actions])


class TheFifteenMinuteRule(SweepHarness):
    """Operator ruling 2026-08-15: "close it 15 minutes after it's finished"."""

    def test_a_worker_idle_sixteen_minutes_is_swept(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40)
        actions = self.sweep_until(16 * MINUTE)
        self.assertEqual(["SIGTERM"], [action["signal"] for action in actions])
        self.assertEqual([(500, signal.SIGTERM)], self.kills)

    def test_a_worker_idle_fourteen_minutes_is_left_alone(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40)
        self.assertEqual(self.sweep_until(14 * MINUTE), [])
        self.assertEqual([], self.kills)

    def test_the_window_is_fifteen_minutes_by_default(self) -> None:
        self.assertEqual(15.0, subagent_sweep.DEFAULT_IDLE_MINUTES)
        self.assertAlmostEqual(0.25, subagent_sweep._env_idle_hours({}))

    def test_minutes_can_be_said_in_minutes(self) -> None:
        """A name that says hours cannot express 15 without a fraction."""
        self.assertAlmostEqual(
            0.5,
            subagent_sweep._env_idle_hours(
                {"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "30"}
            ),
        )

    def test_the_old_name_still_works_and_still_disables(self) -> None:
        """An operator who set the hours name must not be reinterpreted."""
        self.assertEqual(
            6.0,
            subagent_sweep._env_idle_hours({"SESSION_KIT_SUBAGENT_IDLE_HOURS": "6"}),
        )
        self.assertEqual(
            0.0,
            subagent_sweep._env_idle_hours({"SESSION_KIT_SUBAGENT_IDLE_HOURS": "0"}),
        )

    def test_minutes_outranks_hours_and_zero_still_disables(self) -> None:
        self.assertAlmostEqual(
            0.5,
            subagent_sweep._env_idle_hours(
                {
                    "SESSION_KIT_SUBAGENT_IDLE_MINUTES": "30",
                    "SESSION_KIT_SUBAGENT_IDLE_HOURS": "6",
                }
            ),
        )
        write_process(self.proc, 500, worker_cmdline(), own=40)
        environ = {"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "0"}
        self.assertEqual(self.run_sweep(now=0.0, environ=environ), [])
        self.assertEqual(self.run_sweep(now=99 * HOUR, environ=environ), [])
        self.assertEqual([], self.kills)

    def test_a_junk_minutes_value_disables_the_sweep(self) -> None:
        """A window nobody can read is a refusal, not the narrowest window.

        It used to fall back to fifteen minutes, so a typo in the very
        variable the docs tell them to RAISE made the closer more aggressive
        (lane A F3).
        """
        for junk in ("abc", "-5", "nan", "inf", "1h", "30 minutes", ""):
            with self.subTest(value=junk):
                self.assertEqual(
                    0.0,
                    subagent_sweep._env_idle_hours(
                        {"SESSION_KIT_SUBAGENT_IDLE_MINUTES": junk}
                    ),
                )

    def test_a_junk_value_never_revives_a_sweep_that_was_turned_off(self) -> None:
        """They turned it off with the old name; a typo must not turn it on."""
        self.assertEqual(
            0.0,
            subagent_sweep._env_idle_hours(
                {
                    "SESSION_KIT_SUBAGENT_IDLE_MINUTES": "junk",
                    "SESSION_KIT_SUBAGENT_IDLE_HOURS": "0",
                }
            ),
        )

    def test_output_movement_inside_the_window_resets_the_clock(self) -> None:
        cmdline = worker_cmdline()
        write_process(self.proc, 500, cmdline, own=40)
        self.assertEqual(self.run_sweep(now=0.0), [])
        with worker_transcript(self.proc, cmdline).open("a", encoding="utf-8") as handle:
            handle.write("new output\n")
        self.assertEqual(self.run_sweep(now=10 * MINUTE), [])
        self.assertEqual(self.run_sweep(now=20 * MINUTE), [])  # clock restarted
        self.assertEqual([], self.kills)
        actions = self.run_sweep(now=25 * MINUTE)
        self.assertEqual(["SIGTERM"], [action["signal"] for action in actions])


class OutputIsTheIdleClock(SweepHarness):
    def test_moving_cpu_with_static_output_is_swept(self) -> None:
        cmdline = worker_cmdline("ticking@team")
        write_process(self.proc, 500, cmdline, own=40)
        self.assertEqual([], self.run_sweep(now=0.0))
        for minute, ticks in ((5, 41), (10, 42)):
            write_process(self.proc, 500, cmdline, own=ticks)
            self.assertEqual([], self.run_sweep(now=minute * MINUTE))
        write_process(self.proc, 500, cmdline, own=43)

        actions = self.run_sweep(now=15 * MINUTE)

        self.assertEqual(["SIGTERM"], [action["signal"] for action in actions])

    def test_growing_output_with_zero_cpu_is_not_swept(self) -> None:
        cmdline = worker_cmdline("writing@team")
        write_process(self.proc, 500, cmdline, own=0)
        transcript = worker_transcript(self.proc, cmdline)
        self.assertEqual([], self.run_sweep(now=0.0))
        for minute in (5, 10, 15, 20):
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(f"output at {minute}\n")
            self.assertEqual([], self.run_sweep(now=minute * MINUTE))
        self.assertEqual([], self.kills)

    def test_unreadable_output_is_not_idle_and_is_logged(self) -> None:
        cmdline = worker_cmdline("unreadable@team")
        write_process(self.proc, 500, cmdline, own=0)
        transcript = worker_transcript(self.proc, cmdline)
        transcript.unlink()
        transcript.symlink_to(transcript.with_name("missing.jsonl"))

        self.assertEqual([], self.run_sweep(now=0.0))
        self.assertEqual([], self.run_sweep(now=15 * MINUTE))
        self.assertEqual([], self.run_sweep(now=30 * MINUTE))
        self.assertEqual([], self.kills)
        records = [
            json.loads(line)
            for line in (self.state_dir / "subagent-sweep.log").read_text().splitlines()
        ]
        self.assertEqual(3, len(records))
        self.assertTrue(all(record["decision"] == "refused-output" for record in records))
        self.assertTrue(all(record["agent_id"] == "unreadable@team" for record in records))


class TheZombiePath(SweepHarness):
    def test_an_idle_worker_gets_term_then_kill_across_passes(self) -> None:
        # Pinned to a six-hour window on purpose: this case is about the
        # TERM-then-KILL sequence, not about what the default happens to be.
        write_process(self.proc, 500, worker_cmdline(), own=40)
        six = {"SESSION_KIT_SUBAGENT_IDLE_HOURS": "6"}
        self.assertEqual(self.run_sweep(now=0.0, environ=six), [])  # arms the clock
        self.assertEqual(self.run_sweep(now=1 * HOUR, environ=six), [])  # too soon
        actions = self.sweep_until(7 * HOUR, six)
        self.assertEqual([a["signal"] for a in actions], ["SIGTERM"])
        self.assertEqual(self.kills, [(500, signal.SIGTERM)])
        actions = self.run_sweep(now=self.clock + 60, environ=six)
        self.assertEqual([a["signal"] for a in actions], ["SIGKILL"])
        self.assertEqual(self.kills[-1], (500, signal.SIGKILL))

    def test_a_term_handler_writing_output_cannot_dodge_the_kill(self) -> None:
        cmdline = worker_cmdline()
        write_process(self.proc, 500, cmdline, own=40)
        self.run_sweep(now=0.0)
        self.run_sweep(now=15 * MINUTE)  # TERM sent
        with worker_transcript(self.proc, cmdline).open("a", encoding="utf-8") as handle:
            handle.write("graceful shutdown\n")
        actions = self.run_sweep(now=15 * MINUTE + 60)
        self.assertEqual([a["signal"] for a in actions], ["SIGKILL"])
        self.assertTrue(actions[0]["moved_after_term"])

    def test_output_movement_before_term_resets_the_clock(self) -> None:
        cmdline = worker_cmdline()
        write_process(self.proc, 500, cmdline, own=40)
        self.run_sweep(now=0.0)
        with worker_transcript(self.proc, cmdline).open("a", encoding="utf-8") as handle:
            handle.write("still working\n")
        self.assertEqual(self.run_sweep(now=15 * MINUTE), [])
        self.assertEqual(self.kills, [])
        # ...and the reset clock counts from the movement, not from zero.
        actions = self.run_sweep(now=30 * MINUTE)
        self.assertEqual([a["signal"] for a in actions], ["SIGTERM"])

    def test_pid_reuse_never_inherits_the_term_decision(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40, start=1000)
        self.run_sweep(now=0.0)
        self.run_sweep(now=15 * MINUTE)  # TERM to the old process
        write_process(self.proc, 500, worker_cmdline(), own=0, start=9999)
        actions = self.run_sweep(now=15 * MINUTE + 60)
        self.assertEqual(actions, [])  # fresh identity, fresh clock
        self.assertEqual([s for _, s in self.kills], [signal.SIGTERM])

    def test_a_vanished_process_is_logged_already_gone(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40)
        self.run_sweep(now=0.0)

        def raising_kill(pid: int, signum: int) -> None:
            raise ProcessLookupError

        actions = subagent_sweep.sweep(
            proc=self.proc,
            state_dir=self.state_dir,
            environ={"HOME": os.fspath(self.home)},
            now=15 * MINUTE,
            kill=raising_kill,
        )
        self.assertEqual([a["signal"] for a in actions], ["already-gone"])


class TheEvidenceChain(SweepHarness):
    def test_a_worker_whose_child_burns_cpu_is_swept_on_static_output(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40)
        write_process(self.proc, 501, ["make", "-j8"], ppid=500, own=100)
        self.run_sweep(now=0.0)
        write_process(self.proc, 501, ["make", "-j8"], ppid=500, own=900)
        actions = self.run_sweep(now=15 * MINUTE)
        self.assertEqual(["SIGTERM"], [action["signal"] for action in actions])
        self.assertEqual([(500, signal.SIGTERM)], self.kills)

    def test_reaped_child_cpu_is_not_output_activity(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40, reaped=0)
        write_process(self.proc, 501, ["sh", "-c", "true"], ppid=500, own=60)
        self.run_sweep(now=0.0)
        # The child exits and its ticks land in the parent's cutime. Neither
        # fact changes the transcript, so the output clock keeps counting.
        for entry in (self.proc / "501").iterdir():
            entry.unlink()
        (self.proc / "501").rmdir()
        write_process(self.proc, 500, worker_cmdline(), own=40, reaped=60)
        actions = self.run_sweep(now=15 * MINUTE)
        self.assertEqual([a["signal"] for a in actions], ["SIGTERM"])

    def test_a_burning_grandchild_does_not_move_the_output_clock(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40)
        write_process(self.proc, 501, ["sh"], ppid=500, own=1)
        write_process(self.proc, 502, ["cc1"], ppid=501, own=100)
        self.run_sweep(now=0.0)
        write_process(self.proc, 502, ["cc1"], ppid=501, own=500)
        actions = self.run_sweep(now=15 * MINUTE)
        self.assertEqual(["SIGTERM"], [action["signal"] for action in actions])


class TheWorkerShape(SweepHarness):
    def test_a_shell_quoting_the_flag_in_its_script_is_ignored(self) -> None:
        write_process(
            self.proc,
            600,
            ["/bin/bash", "-c", "pgrep -f -- '--parent-session-id abcd'"],
            own=5,
        )
        self.assertEqual(subagent_sweep.find_subagents(self.proc), [])

    def test_a_grep_with_the_exact_flag_argv_is_ignored(self) -> None:
        write_process(
            self.proc,
            601,
            ["grep", "--parent-session-id", "abcd", "--agent-id", "x@y", "log"],
            own=5,
        )
        self.assertEqual(subagent_sweep.find_subagents(self.proc), [])

    def test_a_worker_missing_the_agent_id_flag_is_ignored(self) -> None:
        write_process(
            self.proc, 602, [CLAUDE, "--parent-session-id", "abcd"], own=5
        )
        self.assertEqual(subagent_sweep.find_subagents(self.proc), [])

    def test_the_lane_impostor_battery_never_matches(self) -> None:
        # Every impostor shape the review lanes used to break the old
        # substring match (X20-F2). Each carries both flags as exact argv
        # elements with values; the executable test alone must reject them.
        flags = ["--parent-session-id", "abcd", "--agent-id", "x@y"]
        impostors = [
            ["grep", "claude"] + flags + ["log"],
            ["pgrep", "claude"] + flags,
            ["/tmp/notclaude"] + flags,
            ["/home/user/claude-tmp/helper"] + flags,
            ["/usr/bin/python3", "/home/user/.claude/hooks/courier.py"] + flags,
            # Round 3, lane finding 4: a tool of THEIRS under a directory named
            # `claude`. The old rule accepted any executable with a `claude`
            # path component, so a review lane launched a real sleeper shaped
            # like this and watched the sweep TERM it after sixteen minutes.
            ["/home/user/fixture/claude/operator-tool.js"] + flags,
            ["/home/user/claude/my-script"] + flags,
            ["/opt/claude/bin/helper"] + flags,
            # `claude/versions/...` but the version component is not a version.
            ["/home/user/claude/versions/mytool"] + flags,
            # The same shape behind a node wrapper.
            ["node", "/x/claude/cli.js"] + flags,
        ]
        for pid, cmdline in enumerate(impostors, start=610):
            write_process(self.proc, pid, cmdline, own=5)
        self.assertEqual(subagent_sweep.find_subagents(self.proc), [])

    def test_the_equals_flag_form_is_refused(self) -> None:
        # Exact elements only: a worker never uses --flag=value, so a
        # process that does is not a worker.
        write_process(
            self.proc,
            620,
            [CLAUDE, "--parent-session-id=abcd", "--agent-id=x@y"],
            own=5,
        )
        self.assertEqual(subagent_sweep.find_subagents(self.proc), [])

    def test_the_real_worker_shape_is_found_with_its_identity(self) -> None:
        write_process(self.proc, 603, worker_cmdline("opus-lane@t", "sess-1"), own=7)
        found = subagent_sweep.find_subagents(self.proc)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["agent_id"], "opus-lane@t")
        self.assertEqual(found[0]["parent"], "sess-1")

    def test_a_node_wrapped_worker_is_still_found(self) -> None:
        """Behind a node wrapper, argv[1] faces the SAME provider test.

        This used to assert on `node /x/claude/cli.js`, which the round-3
        tightening refuses -- and rightly: an executable named `cli.js` under a
        directory named `claude` is the operator-tool shape from lane finding
        4, not a provider layout. It was never a real shape either; the actual
        npm layout is `.../@anthropic-ai/claude-code/cli.js`, whose component
        is `claude-code`, and that has never matched at any revision. Re-pointed
        at a genuine provider path, and the old string is now in the impostor
        battery where it belongs.
        """
        write_process(
            self.proc,
            604,
            ["node", "/x/share/claude/versions/2.1.233"] + worker_cmdline()[1:],
            own=7,
        )
        self.assertEqual(len(subagent_sweep.find_subagents(self.proc)), 1)


class TheSwitches(SweepHarness):
    def arm_overdue_worker(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40)
        self.run_sweep(now=0.0)

    def test_the_env_kill_switch_stops_the_sweep(self) -> None:
        self.arm_overdue_worker()
        for value in ("0", "off", "no", "false", " OFF "):
            actions = self.run_sweep(
                now=15 * MINUTE, environ={"SESSION_KIT_SUBAGENT_SWEEP": value}
            )
            self.assertEqual(actions, [], value)
        self.assertEqual(self.kills, [])

    def test_the_state_file_kill_switch_stops_the_sweep(self) -> None:
        self.arm_overdue_worker()
        (self.state_dir / "subagent-sweep-off").touch()
        self.assertEqual(self.run_sweep(now=15 * MINUTE), [])
        self.assertEqual(self.kills, [])

    def test_zero_idle_hours_disables_rather_than_kills_instantly(self) -> None:
        self.arm_overdue_worker()
        actions = self.run_sweep(
            now=15 * MINUTE, environ={"SESSION_KIT_SUBAGENT_IDLE_HOURS": "0"}
        )
        self.assertEqual(actions, [])
        self.assertEqual(self.kills, [])

    def test_garbage_idle_hours_disables_rather_than_guessing(self) -> None:
        for raw in ("abc", "-3", "inf", "nan", "", "6h"):
            self.assertEqual(
                0.0,
                subagent_sweep._env_idle_hours(
                    {"SESSION_KIT_SUBAGENT_IDLE_HOURS": raw}
                ),
                raw,
            )

    def test_a_shorter_threshold_is_honoured(self) -> None:
        window = {"SESSION_KIT_SUBAGENT_IDLE_HOURS": "1.5"}
        write_process(self.proc, 500, worker_cmdline(), own=40)
        actions = self.sweep_until(2 * HOUR, window)
        self.assertEqual([a["signal"] for a in actions], ["SIGTERM"])


class TheSignalPath(SweepHarness):
    def test_exact_process_delivery_accepts_a_shell_and_uses_pidfd(self) -> None:
        write_process(self.proc, 501, ["bash"], own=1, start=2000)
        with (
            unittest.mock.patch.object(subagent_sweep, "_HAS_PIDFD", True),
            unittest.mock.patch.object(
                subagent_sweep.os, "pidfd_open", return_value=77
            ) as opened,
            unittest.mock.patch.object(
                subagent_sweep.signal, "pidfd_send_signal"
            ) as sent,
            unittest.mock.patch.object(subagent_sweep.os, "close") as closed,
        ):
            subagent_sweep._deliver_exact_process(
                self.proc, 501, 2000, signal.SIGTERM
            )
        opened.assert_called_once_with(501)
        sent.assert_called_once_with(77, signal.SIGTERM)
        closed.assert_called_once_with(77)

    def test_exact_process_delivery_rechecks_after_pin_and_refuses_reuse(
        self,
    ) -> None:
        write_process(self.proc, 501, ["bash"], own=1, start=2000)

        def recycle_after_pin(pid: int) -> int:
            write_process(self.proc, pid, ["bash"], own=1, start=9999)
            return 77

        with (
            unittest.mock.patch.object(subagent_sweep, "_HAS_PIDFD", True),
            unittest.mock.patch.object(
                subagent_sweep.os, "pidfd_open", side_effect=recycle_after_pin
            ),
            unittest.mock.patch.object(
                subagent_sweep.signal, "pidfd_send_signal"
            ) as sent,
            unittest.mock.patch.object(subagent_sweep.os, "close"),
        ):
            with self.assertRaises(ProcessLookupError):
                subagent_sweep._deliver_exact_process(
                    self.proc, 501, 2000, signal.SIGTERM
                )
        sent.assert_not_called()

    def test_a_recycled_pid_is_refused_at_signal_time(self) -> None:
        # The scan-to-signal race (X20-F3): the scan recorded one identity,
        # the process was replaced before delivery. The pre-signal identity
        # re-read must refuse; the recycled process must never be struck.
        write_process(self.proc, 500, worker_cmdline(), own=40, start=1000)
        self.run_sweep(now=0.0)
        stale = {
            "pid": 500,
            "start_ticks": 1000,
            "agent_id": "wave8@team",
            "parent": "abcd-1234",
        }
        write_process(self.proc, 500, worker_cmdline(), own=0, start=9999)
        with unittest.mock.patch.object(
            subagent_sweep, "find_subagents", return_value=[stale]
        ):
            actions = self.run_sweep(now=15 * MINUTE)
        self.assertEqual([a["signal"] for a in actions], ["already-gone"])
        self.assertEqual(self.kills, [])

    def test_still_the_worker_rejects_new_ticks_and_new_shape(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40, start=1000)
        self.assertTrue(subagent_sweep._still_the_worker(self.proc, 500, 1000))
        self.assertFalse(subagent_sweep._still_the_worker(self.proc, 500, 2000))
        write_process(self.proc, 501, ["bash"], own=1, start=1000)
        self.assertFalse(subagent_sweep._still_the_worker(self.proc, 501, 1000))

    def test_an_undelivered_term_never_escalates_to_kill(self) -> None:
        # X20-F5: a TERM that failed to land is not a TERM. The next pass
        # must retry TERM, never fire KILL after a signal nobody received.
        write_process(self.proc, 500, worker_cmdline(), own=40)
        self.run_sweep(now=0.0)

        def refusing_kill(pid: int, signum: int) -> None:
            raise PermissionError(1, "refused")

        blocked = subagent_sweep.sweep(
            proc=self.proc,
            state_dir=self.state_dir,
            environ={"HOME": os.fspath(self.home)},
            now=15 * MINUTE,
            kill=refusing_kill,
        )
        self.assertEqual([a["signal"] for a in blocked], ["error:1"])
        retry = self.run_sweep(now=15 * MINUTE + 60)
        self.assertEqual([a["signal"] for a in retry], ["SIGTERM"])

    def test_a_second_concurrent_sweep_skips_instead_of_killing_early(self) -> None:
        # X20-F7: overlap must never collapse the TERM-to-KILL grace pass.
        # The lock is the state DIRECTORY's own descriptor -- no lock file.
        write_process(self.proc, 500, worker_cmdline(), own=40)
        self.run_sweep(now=0.0)
        import os as _os

        holder = _os.open(self.state_dir, _os.O_RDONLY)
        self.addCleanup(_os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        actions = self.run_sweep(now=15 * MINUTE)
        self.assertEqual(actions, [])
        self.assertEqual(self.kills, [])


class DryRunAndRecords(SweepHarness):
    def test_dry_run_reports_but_touches_nothing(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40)
        self.run_sweep(now=0.0)
        state_before = (self.state_dir / "subagent-sweep.state.json").read_text()
        # Touches nothing means the WHOLE directory inventory and every byte
        # in it (X20-F4 round 2 caught a lock file this assertion would have
        # caught in round 1): compare the complete listing, not chosen files.
        inventory_before = {
            entry.name: entry.read_bytes() if entry.is_file() else None
            for entry in self.state_dir.iterdir()
        }
        actions = self.run_sweep(now=15 * MINUTE, dry_run=True)
        self.assertEqual([a["signal"] for a in actions], ["SIGTERM"])
        self.assertTrue(actions[0]["dry_run"])
        self.assertEqual(self.kills, [])
        state_after = (self.state_dir / "subagent-sweep.state.json").read_text()
        self.assertEqual(state_before, state_after)
        inventory_after = {
            entry.name: entry.read_bytes() if entry.is_file() else None
            for entry in self.state_dir.iterdir()
        }
        self.assertEqual(inventory_before, inventory_after)
        # A dry run must not commit the TERM decision either: the next real
        # pass sends TERM, not KILL. It watches a fresh window first, because
        # a dry run writes nothing at all -- including the record of when a
        # pass last ran -- so the real pass after one sees a gap and starts
        # the clocks over. Fail-safe, and the point still stands: TERM.
        real = self.sweep_until(45 * MINUTE)
        self.assertEqual([a["signal"] for a in real], ["SIGTERM"])

    def test_a_cold_dry_run_creates_nothing_at_all(self) -> None:
        # The reaper may run its very first pass in dry-run mode against a
        # state directory that has never seen a sweep: even the lock must
        # not appear (X20-F4 round 2 — the flock is on the directory itself).
        write_process(self.proc, 500, worker_cmdline(), own=40)
        actions = self.run_sweep(now=0.0, dry_run=True)
        self.assertEqual(actions, [])
        self.assertEqual(sorted(p.name for p in self.state_dir.iterdir()), [])

    def test_every_action_lands_in_the_log_as_one_json_line(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40)
        self.run_sweep(now=0.0)
        self.run_sweep(now=15 * MINUTE)
        self.run_sweep(now=15 * MINUTE + 60)
        lines = (
            (self.state_dir / "subagent-sweep.log").read_text().strip().splitlines()
        )
        self.assertEqual(
            [json.loads(line)["signal"] for line in lines],
            ["SIGTERM", "SIGKILL"],
        )
        for line in lines:
            record = json.loads(line)
            self.assertEqual(record["pid"], 500)
            self.assertEqual(record["agent_id"], "wave8@team")

    def test_a_corrupt_state_file_starts_fresh_instead_of_crashing(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40)
        (self.state_dir / "subagent-sweep.state.json").write_text("{not json")
        self.assertEqual(self.run_sweep(now=0.0), [])  # fresh clock, no crash
        actions = self.run_sweep(now=15 * MINUTE)
        self.assertEqual([a["signal"] for a in actions], ["SIGTERM"])


class NothingIsClosedOnEvidenceOlderThanTheRule(SweepHarness):
    """Round 2, lane A F2/F8 and Codex 1: the clock has to be trustworthy."""

    def legacy_state(self, *, idle_minutes: list[int], now: float) -> list[int]:
        """A state file armed by the OLD six-hour pass, as install day finds it."""
        pids = [500 + index for index in range(len(idle_minutes))]
        for pid in pids:
            write_process(self.proc, pid, worker_cmdline(f"w{pid}@team"), own=40)
        (self.state_dir / "subagent-sweep.state.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "tracked": {
                        f"{pid}:1000": {
                            "cpu_ticks": 40,
                            "last_active": now - minutes * MINUTE,
                            "term_sent": False,
                        }
                        for pid, minutes in zip(pids, idle_minutes)
                    },
                }
            )
        )
        return pids

    def test_the_first_pass_after_install_closes_nothing(self) -> None:
        """The single worst thing this branch could do.

        Every worker idle longer than fifteen minutes under the SIX-hour rule
        becomes an immediate candidate the moment the new rule lands. Nothing
        may be closed on evidence gathered before the rule that judges it.
        """
        now = 10_000.0
        idle = [0, 3, 8, 17, 25, 40, 90, 210, 400, 700, 1200, 1500]
        self.legacy_state(idle_minutes=idle, now=now)

        actions = self.run_sweep(now=now)

        self.assertEqual([], actions)
        self.assertEqual([], self.kills)
        self.assertGreaterEqual(len([m for m in idle if m >= 15]), 9)

    def test_a_full_window_under_the_new_rule_still_closes(self) -> None:
        """Fresh clocks are a delay, not an amnesty."""
        now = 10_000.0
        self.legacy_state(idle_minutes=[600, 600], now=now)
        self.assertEqual([], self.run_sweep(now=now))

        actions = self.sweep_until(now + 20 * MINUTE, start=now + 5 * MINUTE)

        self.assertEqual(["SIGTERM", "SIGTERM"], [a["signal"] for a in actions])

    def test_a_gap_longer_than_the_window_starts_the_clocks_over(self) -> None:
        """A skipped pass -- the flock held by the hourly reaper -- is a gap."""
        write_process(self.proc, 500, worker_cmdline(), own=40)
        self.assertEqual([], self.run_sweep(now=0.0))

        self.assertEqual([], self.run_sweep(now=3 * HOUR))  # nothing observed between
        self.assertEqual([], self.kills)

    def test_a_clock_that_jumps_forward_closes_nothing(self) -> None:
        """An NTP correction is not fifteen minutes of idleness (F8)."""
        write_process(self.proc, 500, worker_cmdline(), own=40)
        self.assertEqual([], self.run_sweep(now=0.0))

        self.assertEqual([], self.run_sweep(now=48 * HOUR))
        self.assertEqual([], self.kills)

    def test_a_reboot_discards_the_previous_boots_decisions(self) -> None:
        """Start ticks are counted from boot, so pid:start_ticks is not\nunique across one. A persisted TERM must never land on a stranger."""
        (self.proc / "sys" / "kernel" / "random").mkdir(parents=True)
        boot = self.proc / "sys" / "kernel" / "random" / "boot_id"
        boot.write_text("11111111-1111-4111-8111-111111111111\n")
        write_process(self.proc, 500, worker_cmdline(), own=40)
        self.run_sweep(now=0.0)
        self.run_sweep(now=15 * MINUTE)  # TERM committed for THIS boot
        self.assertEqual([(500, signal.SIGTERM)], self.kills)

        boot.write_text("22222222-2222-4222-8222-222222222222\n")
        self.kills.clear()
        actions = self.run_sweep(now=15 * MINUTE + 60)

        self.assertEqual([], actions)  # not an inherited KILL
        self.assertEqual([], self.kills)


class TheSwitchesReachThePassThatCloses(SweepHarness):
    """Round 2, lane A F4: a setting they cannot apply is not a setting."""

    def test_the_window_file_reaches_a_pass_with_no_environment(self) -> None:
        """The systemd user manager inherits none of their shell environment,\nso the file beside the off switch is the switch that arrives."""
        write_process(self.proc, 500, worker_cmdline(), own=40)
        (self.state_dir / subagent_sweep.IDLE_WINDOW_FILE).write_text("120\n")

        self.assertEqual([], self.sweep_until(60 * MINUTE))
        self.assertEqual([], self.kills)

    def test_a_malformed_window_file_disables_rather_than_guesses(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40)
        (self.state_dir / subagent_sweep.IDLE_WINDOW_FILE).write_text("half an hour\n")

        self.assertEqual([], self.sweep_until(3 * HOUR))
        self.assertEqual([], self.kills)

    def test_the_environment_still_outranks_the_file(self) -> None:
        write_process(self.proc, 500, worker_cmdline(), own=40)
        (self.state_dir / subagent_sweep.IDLE_WINDOW_FILE).write_text("120\n")

        actions = self.sweep_until(
            30 * MINUTE, {"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "10"}
        )

        self.assertEqual(["SIGTERM"], [action["signal"] for action in actions])


class ThePassThatDeliversIt(unittest.TestCase):
    """A window is only as sharp as the pass that applies it.

    Idle is measured between passes, so the cadence is half the ruling. On the
    hourly reaper timer a fifteen-minute rule would have meant up to about
    seventy-five minutes -- the rule would have been a lie.
    """

    def timer(self) -> str:
        return (REPO / "systemd" / "session-kit-subagent-sweep.timer").read_text(
            encoding="utf-8"
        )

    def service(self) -> str:
        return (REPO / "systemd" / "session-kit-subagent-sweep.service").read_text(
            encoding="utf-8"
        )

    def test_the_sweep_runs_every_five_minutes(self) -> None:
        self.assertIn("OnUnitActiveSec=5min", self.timer())

    def test_the_cadence_actually_delivers_the_fifteen_minute_promise(self) -> None:
        """Worst case is the window plus one pass interval."""
        interval_minutes = 5
        worst_case = subagent_sweep.DEFAULT_IDLE_MINUTES + interval_minutes
        self.assertLessEqual(worst_case, 20)
        self.assertIn(f"OnUnitActiveSec={interval_minutes}min", self.timer())

    def test_the_five_minute_pass_sweeps_and_nothing_else(self) -> None:
        """It must not build the guard-live inventory or close a terminal.

        That work is expensive and destructive, and its own thresholds are
        measured in days -- which is why this is a separate timer rather than
        a faster shpool-reaper.timer.
        """
        self.assertIn("--sweep-subagents", self.service())
        self.assertNotIn("--auto-close", self.service())

        reaper = (REPO / "bin" / "shpool_reaper").read_text(encoding="utf-8")
        self.assertIn("--sweep-subagents)", reaper)
        # The sweep-only mode returns before anything expensive is reached.
        early_exit = reaper.index("if [[ ${MODE:-report} == sweep ]]; then")
        self.assertLess(early_exit, reaper.index("guard-live status command unavailable"))
        self.assertLess(early_exit, reaper.index("SK_REAPER_AUTO_CLOSE_HOURS"))


class TheOffSwitchStopsTheClosingPass(unittest.TestCase):
    """Round 2, lane A F1/F6: proved by RUNNING the pass, not by reading it.

    The old assertions compared `str.index()` positions in the script, so they
    passed while the operator's off switch was being skipped. This starts the
    real program with a pinned state root and a fixture /proc, and looks at
    what it did.
    """

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix=".sentinel-")
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.home = self.base / "home"
        self.state = self.base / "state"
        self.proc = self.base / "proc"
        for path in (self.home, self.state, self.proc):
            path.mkdir(mode=0o700)
        self.pid = int(
            Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8").strip()
        ) + 1
        self.assertFalse(Path("/proc", str(self.pid)).exists())
        write_process(self.proc, self.pid, worker_cmdline(), own=40)

    def reaper(self, **extra: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.update(
            {
                "HOME": os.fspath(self.home),
                "CLAUDE_CONFIG_DIR": os.fspath(self.home / ".claude"),
                "SESSION_KIT_ACCOUNT_ROOT": os.fspath(self.base / "accounts"),
                "SESSION_KIT_STATE_DIR": os.fspath(self.state),
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_PROC_ROOT": os.fspath(self.proc),
                "SESSION_KIT_NONINTERACTIVE": "1",
                # Three seconds, with passes about a second apart: a real
                # cadence in miniature. Passes must be closer together than
                # the window or the sweep starts its clocks over, which is
                # the install-day rule doing its job.
                "SESSION_KIT_SUBAGENT_IDLE_MINUTES": "0.05",
            }
        )
        environment.update(extra)
        return subprocess.run(
            [os.fspath(REPO / "bin" / "shpool_reaper"), "--sweep-subagents"],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def swept(self) -> list[dict]:
        log = self.state / "subagent-sweep.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line]

    def run_cadence(self, passes: int = 6, **extra: str) -> None:
        """Passes about a second apart until one acts, or `passes` are spent."""
        for _ in range(passes):
            self.reaper(**extra)
            if self.swept():
                return
            time.sleep(1.05)

    def test_the_pass_acts_when_the_switch_is_absent(self) -> None:
        """The control: without a switch this really does reach the worker.

        Delivery then refuses, because the fixture pid does not exist and the
        identity is re-read at signal time -- `already-gone` is the sweep
        acting. Without this control, a switch test proves nothing: a pass
        that never reached anything would pass it too.
        """
        self.run_cadence()

        acted = self.swept()
        self.assertEqual(1, len(acted), acted)
        self.assertEqual(self.pid, acted[0]["pid"])
        self.assertIn(acted[0]["signal"], {"SIGTERM", "already-gone"})

    def test_no_shpool_reaper_stops_the_sub_agent_sweep(self) -> None:
        """Their escape hatch. It covered this at base; the five-minute entry
        point walked out from behind it, and that is what round 1 shipped."""
        self.reaper()
        time.sleep(1.05)
        self.reaper()
        time.sleep(1.05)
        (self.home / ".no_shpool_reaper").touch()

        result = self.reaper()

        self.assertEqual(3, result.returncode, result.stderr)
        self.assertIn("disabled by", result.stderr)
        self.assertEqual([], self.swept())

    def test_the_sweep_switch_and_the_off_file_also_stop_it(self) -> None:
        for label, extra, marker in (
            ("SESSION_KIT_SUBAGENT_SWEEP=off", {"SESSION_KIT_SUBAGENT_SWEEP": "off"}, None),
            ("window of zero", {"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "0"}, None),
            ("malformed window", {"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "junk"}, None),
            ("subagent-sweep-off file", {}, "subagent-sweep-off"),
        ):
            with self.subTest(switch=label):
                for stale in (self.state / "subagent-sweep.log",
                              self.state / "subagent-sweep.state.json"):
                    stale.unlink(missing_ok=True)
                (self.state / "subagent-sweep-off").unlink(missing_ok=True)
                if marker:
                    (self.state / marker).touch()

                self.run_cadence(**extra)

                self.assertEqual([], self.swept(), label)

    def test_the_hourly_pass_still_sweeps_as_a_backstop(self) -> None:
        """An install that never enabled the new timer must not stop sweeping."""
        reaper = (REPO / "bin" / "shpool_reaper").read_text(encoding="utf-8")
        auto = reaper.index("if [[ $MODE == auto ]]; then")
        self.assertIn("sweep_subagents", reaper[auto:])

    def test_the_units_ship_and_are_enabled(self) -> None:
        listed = (REPO / "bin" / "session-kit").read_text(encoding="utf-8")
        self.assertIn("session-kit-subagent-sweep.timer", listed)
        self.assertIn("session-kit-subagent-sweep.service", listed)
        services = (REPO / "lib" / "sh" / "session_kit_services.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("session-kit-subagent-sweep.timer", services)


class TheWindowIsPartOfTheEvidence(SweepHarness):
    """Round 3: the guarantee must come from the RULE, not from a lucky format.

    "Nothing is closed on evidence gathered before the rule that judges it" was
    delivered by noticing that the six-hour release's state file carried no
    `last_pass` field. That is true, and it is an accident of format: it holds
    on install day and nowhere else. The same estate is destroyed by any LATER
    narrowing -- a future default, or the operator lowering the window himself
    -- because the passes are five minutes apart and the gap rule never fires.
    So the window the evidence was gathered under travels WITH the evidence.
    """

    def armed_under(self, window_minutes: float, *, idle_minutes: float, now: float,
                    record_window: bool = True) -> int:
        """A worker watched to `idle_minutes` by a healthy pass at `window_minutes`."""
        pid = 700
        cmdline = worker_cmdline("carried@team")
        write_process(self.proc, pid, cmdline, own=40)
        # The state key carries the boot (round 3, Codex 1/3), so a fixture that
        # hand-writes one has to build it the way the module does.
        boot = subagent_sweep._boot_id(self.proc)
        document = {
            "version": subagent_sweep.STATE_VERSION,
            "tracked": {
                f"{boot}:{pid}:1000": {
                    **output_state(self.proc, cmdline),
                    "last_active": now - idle_minutes * MINUTE,
                    "term_sent": False,
                }
            },
            "boot_id": subagent_sweep._boot_id(self.proc),
            # A pass five minutes ago: the cadence was healthy, no gap, no
            # reboot, no clock jump. Every other re-arm trigger is silent here
            # on purpose, so only the window change can explain the result.
            "last_pass": now - 5 * MINUTE,
        }
        if record_window:
            document["window_seconds"] = window_minutes * 60
        (self.state_dir / "subagent-sweep.state.json").write_text(json.dumps(document))
        return pid

    def test_shortening_the_window_starts_the_clocks_over(self) -> None:
        now = 10_000.0
        self.armed_under(120, idle_minutes=90, now=now)

        actions = self.run_sweep(now=now, environ={})

        self.assertEqual([], actions, "90 idle minutes were gathered while the "
                                      "rule was two hours; the new rule has to "
                                      "watch for itself")
        self.assertEqual([], self.kills)

    def test_and_then_closes_after_a_full_window_of_its_own(self) -> None:
        """Fresh clocks are a delay, not an amnesty -- the same as install day."""
        now = 10_000.0
        self.armed_under(120, idle_minutes=90, now=now)
        self.assertEqual([], self.run_sweep(now=now))

        actions = self.sweep_until(now + 20 * MINUTE, start=now + 5 * MINUTE)

        self.assertEqual(["SIGTERM"], [a["signal"] for a in actions])

    def test_widening_the_window_keeps_the_evidence(self) -> None:
        """A wider window can only ever close LESS, so its evidence still counts."""
        now = 10_000.0
        self.armed_under(15, idle_minutes=20, now=now)

        actions = self.run_sweep(
            now=now, environ={"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "30"}
        )

        self.assertEqual([], actions, "20 idle minutes is inside a 30-minute window")
        stored = json.loads(
            (self.state_dir / "subagent-sweep.state.json").read_text()
        )
        # Not re-armed: the clock it inherited is still running.
        boot = subagent_sweep._boot_id(self.proc)
        self.assertEqual(
            now - 20 * MINUTE,
            stored["tracked"][f"{boot}:700:1000"]["last_active"],
        )

    def test_a_state_file_that_never_recorded_its_window_is_not_trusted(self) -> None:
        """Unknown provenance is untrusted provenance.

        This is every state file written before this change, including the one
        the operator has on disk right now.
        """
        now = 10_000.0
        self.armed_under(15, idle_minutes=90, now=now, record_window=False)

        self.assertEqual([], self.run_sweep(now=now))
        self.assertEqual([], self.kills)

    def test_a_window_it_cannot_compare_is_a_window_it_does_not_know(self) -> None:
        """A hand-edited or corrupt state file must not buy trust.

        A NaN compares false against everything and would slip straight past the
        shorter-than check; zero and negatives are not windows at all, and a
        pass only saves state while the sweep is ON, so a stored zero cannot
        have come from this code. Every one of them takes the same answer as a
        window that was never recorded.
        """
        now = 10_000.0
        for corrupt in ("NaN", "-1", "0", "true", '"900"', "null"):
            with self.subTest(window_seconds=corrupt):
                self.setUp()
                pid = self.armed_under(120, idle_minutes=90, now=now)
                path = self.state_dir / "subagent-sweep.state.json"
                document = json.loads(path.read_text())
                document["window_seconds"] = json.loads(corrupt)
                path.write_text(json.dumps(document))

                self.assertEqual([], self.run_sweep(now=now))
                self.assertEqual([], self.kills)
                self.assertTrue(pid)

    def test_the_pass_records_the_window_it_judged_by(self) -> None:
        write_process(self.proc, 701, worker_cmdline(), own=40)

        self.run_sweep(now=1000.0, environ={"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "45"})

        stored = json.loads(
            (self.state_dir / "subagent-sweep.state.json").read_text()
        )
        self.assertEqual(45 * 60, stored["window_seconds"])


class APassThatDoesNothingSaysSo(SweepHarness):
    """Lane B F9. Silence is the symptom this whole change exists to remove.

    A skipped pass and a pass that ran and found nothing are both "no output
    and exit 0", and they are not the same fact. If the state directory lock is
    ever held for good -- a stuck process, a debugger, a hand-run pass that hung
    -- the fifteen-minute rule retires permanently and nothing anywhere says so.
    """

    def test_a_held_lock_leaves_a_trace(self) -> None:
        write_process(self.proc, 800, worker_cmdline(), own=40)
        holder = os.open(self.state_dir, os.O_RDONLY)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with unittest.mock.patch("sys.stderr", new_callable=_Capture) as err:
            actions = self.run_sweep(now=1000.0)

        self.assertEqual([], actions)
        self.assertEqual([], self.kills)
        self.assertIn("skipped", err.text())
        self.assertIn(str(self.state_dir), err.text())
        self.assertIn("nothing was closed", err.text())

    def test_a_state_directory_it_cannot_open_leaves_a_trace(self) -> None:
        missing = self.state_dir / "gone"
        with unittest.mock.patch("sys.stderr", new_callable=_Capture) as err:
            actions = subagent_sweep.sweep(
                proc=self.proc, state_dir=missing, environ={},
                now=1000.0, kill=self.kill,
            )

        self.assertEqual([], actions)
        self.assertIn("skipped", err.text())

    def test_a_completed_pass_records_when_it_ran(self) -> None:
        """The durable half of the trace: doctor reads this to find a quiet sweep."""
        write_process(self.proc, 801, worker_cmdline(), own=40)

        self.run_sweep(now=4242.0)

        stored = json.loads(
            (self.state_dir / "subagent-sweep.state.json").read_text()
        )
        self.assertEqual(4242.0, stored["last_pass"])


class _Capture:
    """A stderr stand-in that keeps what was written to it."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def text(self) -> str:
        return "".join(self.chunks)


def _unit_directives(name: str) -> tuple[list[str], dict[str, str]]:
    """(ExecStart argv, Environment map) read out of a shipped unit file."""
    text = (REPO / "systemd" / name).read_text(encoding="utf-8")
    exec_start: list[str] = []
    environment: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ExecStart="):
            exec_start = line[len("ExecStart="):].split()
        elif line.startswith("Environment="):
            key, _, value = line[len("Environment="):].partition("=")
            environment[key] = value
    return exec_start, environment


class TheUnitCommandLineIsTheThingThatRuns(unittest.TestCase):
    """Brief item 4, proved end to end instead of by reading the unit file.

    This starts the EXACT `ExecStart=` argv from the shipped
    `session-kit-subagent-sweep.service`, through the real launcher shim, in
    the environment systemd builds for a user unit: the manager's own
    environment plus the unit's `Environment=` lines, and nothing else. The
    caller's exported variables are not passed, because systemd does not pass
    them -- that is the defect being closed, not an artefact of the test.

    Boundary, stated rather than papered over: this cannot start the operator's
    systemd user manager, so the manager's contribution is MEASURED instead of
    assumed. `systemctl --user show-environment` on this box carries no
    `SESSION_KIT_*` variable at all, so the unit's `Environment=` lines are the
    entire environment this pass gets -- and the unit declares no
    `SESSION_KIT_*` line, so no variable route to the timer exists. The
    sandbox pins (HOME, state, proc root, testing flag) are the fixture, and
    they are the only additions.
    """

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        base = Path(scratch.name)
        self.home = base / "home"
        self.state_dir = base / "state"
        self.proc = base / "proc"
        for directory in (self.home / ".local" / "bin", self.state_dir, self.proc):
            directory.mkdir(parents=True)

        # A release tree the launcher shim can resolve, laid out the way an
        # install lays one out.
        self.root = base / "kitroot"
        release = self.root / "releases" / ("a" * 40)
        release.mkdir(parents=True)
        for part in ("bin", "lib"):
            shutil.copytree(REPO / part, release / part, symlinks=True)
        (self.root / "current").symlink_to(release)
        shim = self.home / ".local" / "bin" / "shpool_reaper"
        shutil.copy2(REPO / "deploy" / "session-kit-launcher", shim)
        shim.chmod(0o755)

        # A pid that cannot be alive, so the pass has something to decide about
        # and nothing it could ever signal. Verified, not assumed.
        self.pid = int(
            Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8").strip()
        ) + 1
        self.assertFalse(Path("/proc", str(self.pid)).exists())
        write_process(self.proc, self.pid, worker_cmdline("unit@team"), own=40)

    def seed_state(self, *, idle_minutes: float, now: float) -> None:
        """A healthy cadence at the default window, so one pass can decide."""
        cmdline = worker_cmdline("unit@team")
        (self.state_dir / "subagent-sweep.state.json").write_text(
            json.dumps(
                {
                    "version": subagent_sweep.STATE_VERSION,
                    "tracked": {
                        f"{subagent_sweep._boot_id(self.proc)}:{self.pid}:1000": {
                            **output_state(self.proc, cmdline),
                            "last_active": now - idle_minutes * MINUTE,
                            "term_sent": False,
                        }
                    },
                    "boot_id": subagent_sweep._boot_id(self.proc),
                    "last_pass": now - MINUTE,
                    "window_seconds": 15 * MINUTE,
                }
            )
        )

    def run_the_unit(self) -> tuple[int, str]:
        argv, unit_environment = _unit_directives(
            "session-kit-subagent-sweep.service"
        )
        self.assertTrue(argv, "the unit must declare an ExecStart")
        self.assertFalse(
            [key for key in unit_environment if key.startswith("SESSION_KIT_")],
            "the unit declares no SESSION_KIT_* variable, so there is no "
            "environment route to this pass at all",
        )
        argv = [part.replace("%h", str(self.home)) for part in argv]
        environ = {
            key: value.replace("%h", str(self.home))
            for key, value in unit_environment.items()
        }
        environ.update(
            {
                "HOME": str(self.home),
                "SESSION_KIT_ROOT": str(self.root),
                "SESSION_KIT_STATE_DIR": str(self.state_dir),
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_PROC_ROOT": str(self.proc),
            }
        )
        child = subprocess.run(
            argv, env=environ, capture_output=True, text=True, timeout=120
        )
        log = self.state_dir / "subagent-sweep.log"
        return child.returncode, log.read_text(encoding="utf-8") if log.exists() else ""

    def test_the_unit_reaches_a_finished_worker(self) -> None:
        """The control. Without it, every switch test below proves nothing."""
        self.seed_state(idle_minutes=16, now=time.time())

        status, log = self.run_the_unit()

        self.assertEqual(0, status)
        self.assertIn(str(self.pid), log)

    def test_the_window_file_reaches_the_unit_and_an_exported_variable_cannot(
        self,
    ) -> None:
        self.seed_state(idle_minutes=16, now=time.time())
        (self.state_dir / "subagent-sweep-minutes").write_text("120\n")
        # Exported in this process, exactly as they would export it in a shell.
        # It is not passed to the child, because systemd does not pass it.
        with unittest.mock.patch.dict(
            os.environ, {"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "1"}
        ):
            status, log = self.run_the_unit()

        self.assertEqual(0, status)
        self.assertEqual("", log, "a 120-minute window closes nothing at 16 minutes")

    def test_the_off_file_reaches_the_unit(self) -> None:
        self.seed_state(idle_minutes=16, now=time.time())
        (self.state_dir / "subagent-sweep-off").write_text("")

        status, log = self.run_the_unit()

        self.assertEqual(0, status)
        self.assertEqual("", log)

    def test_the_sentinel_reaches_the_unit_and_it_says_so(self) -> None:
        """Their oldest brake, through the timer's own command line."""
        self.seed_state(idle_minutes=16, now=time.time())
        (self.home / ".no_shpool_reaper").write_text("")

        status, log = self.run_the_unit()

        self.assertEqual(3, status, "3 is the documented disabled-by-sentinel status")
        self.assertEqual("", log)


class TheDoctorAndTheSweepReadTheSameWindow(unittest.TestCase):
    """`session-kit doctor` reports the window; the sweep applies it.

    Doctor cannot import the module (it runs as an embedded program with the
    kit's lib off its path), so it carries its own copy of the precedence
    rules. Two copies of a rule drift, and a doctor that reports a window the
    sweep is not using is worse than no doctor at all -- so the copies are
    pinned against one matrix here. The doctor's functions are lifted out of
    the shell heredoc and EXECUTED, not pattern-matched.
    """

    ENV_MATRIX = (
        ({"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "45"}, 45.0),
        ({"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "0"}, 0.0),
        ({"SESSION_KIT_SUBAGENT_IDLE_HOURS": "24"}, 1440.0),
        ({"SESSION_KIT_SUBAGENT_IDLE_HOURS": "0"}, 0.0),
        ({"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "junk"}, None),
        ({"SESSION_KIT_SUBAGENT_IDLE_MINUTES": ""}, None),
        ({"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "-5"}, None),
        ({"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "nan"}, None),
        ({"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "inf"}, None),
        ({"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "1h"}, None),
        ({"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "junk",
          "SESSION_KIT_SUBAGENT_IDLE_HOURS": "0"}, None),
        ({"SESSION_KIT_SUBAGENT_IDLE_HOURS": "6h"}, None),
        ({"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "30",
          "SESSION_KIT_SUBAGENT_IDLE_HOURS": "24"}, 30.0),
    )

    def doctor_readers(self, state_dir: Path) -> dict[str, object]:
        """The doctor's own window readers, lifted out and made callable."""
        shell = (REPO / "lib" / "sh" / "session_kit_doctor.sh").read_text(
            encoding="utf-8"
        )
        block = shell.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        module = ast.parse(block)
        wanted = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name
            in ("sweep_usable_duration", "sweep_window_from_file", "sweep_window_from_env")
        ]
        self.assertEqual(
            3, len(wanted), "doctor must carry exactly these three window readers"
        )
        namespace: dict[str, object] = {
            "os": os,
            "Path": Path,
            "kit_state_root": state_dir,
        }
        exec(  # noqa: S102 - the source is this repo's own doctor
            compile(ast.Module(body=wanted, type_ignores=[]), "<doctor>", "exec"),
            namespace,
        )
        return namespace

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.state_dir = Path(scratch.name)
        self.readers = self.doctor_readers(self.state_dir)
        # The sweep says why it is standing down, which is the point of it --
        # but a green suite should not print thirteen refusals.
        self.noise = io.StringIO()
        redirect = contextlib.redirect_stderr(self.noise)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_the_two_readers_agree_on_every_variable(self) -> None:
        for environ, expected_minutes in self.ENV_MATRIX:
            with self.subTest(environ=environ):
                with unittest.mock.patch.dict(os.environ, environ, clear=True):
                    doctor_minutes, _source = self.readers["sweep_window_from_env"]()
                sweep_minutes = (
                    subagent_sweep._env_idle_hours(environ, self.state_dir) * 60
                )
                # The sweep expresses "unreadable" as a window of 0, because a
                # window of 0 disables it; doctor expresses it as None so it can
                # name the reason. Both mean the sweep does not run.
                self.assertEqual(expected_minutes, doctor_minutes)
                self.assertAlmostEqual(
                    0.0 if expected_minutes is None else expected_minutes,
                    sweep_minutes,
                    places=6,
                )

    def test_the_two_readers_agree_on_the_window_file(self) -> None:
        for raw, expected in (("120\n", 120.0), ("0", 0.0), ("junk", None)):
            with self.subTest(raw=raw):
                (self.state_dir / "subagent-sweep-minutes").write_text(raw)
                with unittest.mock.patch.dict(os.environ, {}, clear=True):
                    doctor_minutes, _source = self.readers["sweep_window_from_file"]()
                self.assertEqual(expected, doctor_minutes)
                self.assertAlmostEqual(
                    0.0 if expected is None else expected,
                    subagent_sweep._env_idle_hours({}, self.state_dir) * 60,
                    places=6,
                )

    def test_doctor_reports_the_window_the_TIMER_gets_not_this_shell(self) -> None:
        """The green line that understated the closer by eight times.

        Doctor used to resolve its reported window from `os.environ`. An
        operator with `SESSION_KIT_SUBAGENT_IDLE_MINUTES=120` exported would
        read `ok ... 120-minute window` while the timer -- which inherits none
        of their shell -- was closing at fifteen. A wrong number on an `ok` line
        is worse than no line.
        """
        with unittest.mock.patch.dict(
            os.environ, {"SESSION_KIT_SUBAGENT_IDLE_MINUTES": "120"}, clear=True
        ):
            reported, source = self.readers["sweep_window_from_file"]()
            shell_only, shell_source = self.readers["sweep_window_from_env"]()

        self.assertEqual(15.0, reported)
        self.assertEqual("the built-in default", source)
        # And the variable is still surfaced, as the caveat it is.
        self.assertEqual(120.0, shell_only)
        self.assertEqual("SESSION_KIT_SUBAGENT_IDLE_MINUTES", shell_source)

    def test_one_stray_byte_cannot_blind_either_reader(self) -> None:
        """`read_text` decodes strictly, and UnicodeDecodeError is a ValueError.

        An `except OSError` does not catch it, so a single non-UTF-8 byte in
        this hand-editable file used to raise out of the doctor program -- and
        the bash caller discards the ENTIRE extended audit when that happens,
        not just this check. The same byte crashed every sweep pass.
        """
        (self.state_dir / "subagent-sweep-minutes").write_bytes(b"\xff15\n")

        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            doctor_minutes, reason = self.readers["sweep_window_from_file"]()

        self.assertIsNone(doctor_minutes)
        self.assertIn("subagent-sweep-minutes", reason)
        # And the sweep itself refuses rather than raising, which closes nothing.
        self.assertEqual(
            0.0, subagent_sweep._env_idle_hours({}, self.state_dir)
        )


class TheSweepCannotReachAProcessHeStarted(SweepHarness):
    """Round 3, lane finding 4 -- destroy class 1, reproduced then closed.

    The old rule accepted ANY executable with a path component named `claude`
    (`"claude" in executable.parts`) plus the two generic flags. A review lane
    launched a real sleeper with that argv shape and the isolated sweep sent it
    SIGTERM after sixteen minutes. A tool of the operator's, in a path
    containing `claude`, run with those flags, was closed automatically.

    Pre-existing at the base -- and in scope anyway, because this branch runs
    that closer twelve times as often, which multiplies the exposure of an
    inherited wrong-target kill.
    """

    HIS_OWN_TOOL = "/home/user/fixture/claude/operator-tool.js"

    def test_the_predicate_refuses_the_operators_tool(self) -> None:
        argv = [self.HIS_OWN_TOOL, "--agent-id", "operator-job",
                "--parent-session-id", "manual"]
        self.assertFalse(subagent_sweep._is_worker(argv))

    def test_the_sweep_never_selects_the_operators_tool(self) -> None:
        write_process(self.proc, 950, [self.HIS_OWN_TOOL, "--agent-id",
                                       "operator-job", "--parent-session-id",
                                       "manual"], own=40)
        self.assertEqual([], subagent_sweep.find_subagents(self.proc))

    def test_the_sweep_never_CLOSES_the_operators_tool(self) -> None:
        """End to end, past the window the lane watched it die at."""
        write_process(self.proc, 950, [self.HIS_OWN_TOOL, "--agent-id",
                                       "operator-job", "--parent-session-id",
                                       "manual"], own=40)

        actions = self.sweep_until(60 * MINUTE)

        self.assertEqual([], actions)
        self.assertEqual([], self.kills, "the lane watched this get SIGTERM")

    def test_and_a_real_worker_beside_it_is_still_closed(self) -> None:
        """The control. Tightening that closed everything would also pass.

        Both processes are idle past the window; exactly one is the provider.
        """
        write_process(self.proc, 950, [self.HIS_OWN_TOOL, "--agent-id",
                                       "operator-job", "--parent-session-id",
                                       "manual"], own=40)
        write_process(self.proc, 951, worker_cmdline("real@team"), own=40)

        actions = self.sweep_until(60 * MINUTE)

        self.assertEqual(["SIGTERM"], [a["signal"] for a in actions])
        self.assertEqual([(951, signal.SIGTERM)], self.kills)

    def test_every_shape_the_provider_ACTUALLY_ships_is_still_selected(self) -> None:
        """Measured on the live process table 2026-08-15, not assumed.

        All fourteen genuine workers ran as
        `/home/<user>/.local/share/claude/versions/2.1.233` -- the version
        DIRECTORY is the executable. Requiring the name `claude` would have
        refused every one of them and quietly retired the sweep, so this pins
        the real shapes against exactly that mistake.
        """
        shapes = [
            "/home/user/.local/share/claude/versions/2.1.233",   # the live one
            "/home/user/.local/share/claude/versions/2.1.231",   # older version
            "/home/user/.local/share/claude/versions/2.1.231/claude",
            "/usr/local/bin/claude",
            "claude",
        ]
        for pid, executable in enumerate(shapes, start=960):
            write_process(
                self.proc,
                pid,
                [executable, "--agent-id", f"w{pid}@t", "--parent-session-id", "p"],
                own=5,
            )

        found = subagent_sweep.find_subagents(self.proc)

        self.assertEqual(len(shapes), len(found), "a real worker shape was lost")


class ADecisionFromAnotherBootIsNeverInherited(SweepHarness):
    """Round 3, Codex 1/3: `pid:start_ticks` is not unique across a reboot.

    Start ticks are counted FROM BOOT, so the same pair names a different
    process on the other side of one. A persisted `term_sent` inherited by that
    stranger is an immediate SIGKILL of a brand-new worker -- the lane
    reproduced exactly that, delivering SIGKILL to `brand-new@team`.

    The whole-document boot check discards a previous boot's decisions. This
    pins the SECOND guard: the boot is in the state key, so an entry that did
    not come from this boot cannot even be looked up if the first check is ever
    defeated -- an unreadable boot id, a hand-edited file, a future edit to the
    re-arm chain.
    """

    def boot(self, value: str) -> None:
        random = self.proc / "sys" / "kernel" / "random"
        random.mkdir(parents=True, exist_ok=True)
        (random / "boot_id").write_text(value + "\n")

    def test_a_term_from_a_previous_boot_cannot_become_a_kill(self) -> None:
        self.boot("boot-B")
        now = 10_000.0
        write_process(self.proc, 900, worker_cmdline("brand-new@team"), own=40)
        snapshot = output_state(self.proc, worker_cmdline("brand-new@team"))
        # The document says this boot -- so the reboot check is SILENT here on
        # purpose, and only the key binding can save the new worker. The entry
        # is keyed the way the previous boot keyed it, carrying a TERM that was
        # delivered to a DIFFERENT process that happened to hold pid 900.
        (self.state_dir / "subagent-sweep.state.json").write_text(
            json.dumps(
                {
                    "version": subagent_sweep.STATE_VERSION,
                    "tracked": {
                        "900:1000": {
                            **snapshot,
                            "last_active": now - 30 * MINUTE,
                            "term_sent": True,
                        }
                    },
                    "boot_id": "boot-B",
                    "last_pass": now - MINUTE,
                    "window_seconds": 15 * MINUTE,
                }
            )
        )

        actions = self.run_sweep(now=now)

        self.assertEqual([], actions, "a stranger inherited a TERM and was KILLED")
        self.assertEqual([], self.kills)

    def test_the_key_carries_the_boot(self) -> None:
        self.boot("boot-A")
        write_process(self.proc, 901, worker_cmdline(), own=40)

        self.run_sweep(now=1000.0)

        stored = json.loads(
            (self.state_dir / "subagent-sweep.state.json").read_text()
        )
        self.assertEqual(["boot-A:901:1000"], list(stored["tracked"]))

    def test_an_actual_reboot_still_discards_everything(self) -> None:
        """The first guard, unchanged, proved beside the second."""
        self.boot("boot-A")
        write_process(self.proc, 902, worker_cmdline(), own=40)
        self.assertEqual([], self.run_sweep(now=1000.0))
        self.boot("boot-B")

        actions = self.run_sweep(now=1000.0 + 30 * MINUTE)

        self.assertEqual([], actions)
        self.assertEqual([], self.kills)


class EveryTestInThisRepositoryActuallyRuns(unittest.TestCase):
    """Lane B F6, generalised: a test that does not run is not a test.

    `tests/run` uses discovery and finds everything, so the whole suite looks
    green -- while anyone running one file directly silently loses every class
    defined after its `if __name__ == "__main__":` block. On this branch that
    was sixteen of this module's fifty-three tests, and it was exactly the
    sixteen guarding the off switch, the install-day rule, and the window
    parsing. This is the same family as the trap that failed five branches, so
    it is checked for every test module rather than for this one.
    """

    def test_no_test_class_is_defined_after_the_main_block(self) -> None:
        offenders: list[str] = []
        for path in sorted((REPO / "tests").glob("*.py")):
            module = ast.parse(path.read_text(encoding="utf-8"))
            main_line = None
            for node in module.body:
                if (
                    isinstance(node, ast.If)
                    and isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name)
                    and node.test.left.id == "__name__"
                ):
                    main_line = node.lineno
            if main_line is None:
                continue
            stranded = [
                node.name
                for node in module.body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef))
                and node.lineno > main_line
            ]
            if stranded:
                offenders.append(f"{path.name}: {', '.join(stranded)}")
        self.assertEqual(
            [],
            offenders,
            "these are skipped by `python3 <file>` and only run under "
            "discovery: " + "; ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
