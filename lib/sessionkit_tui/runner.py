"""The only part of the screen that starts a process.

The snapshot comes from `shpool_status --json`, exactly the document the rest
of the kit reads. Actions go to `sp`, with a proof file naming the exact
session, exactly as the picker has always called them. Nothing here decides
what should happen — it carries out what `state` already decided.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any, Callable, Sequence

from sessionkit_inventory import closed_sessions as closed_ledger

from . import voice
from .actions import PROOF, Plan
from .model import ClosedSession, Inventory, Session, parse_closed, parse_inventory

# What a picker verb answers with when it refuses, and when the attach itself
# failed after the action was allowed. `sp help exit-codes` owns both.
REFUSED = 74
ATTACH_FAILED = 75

SNAPSHOT_TIMEOUT_SECONDS = 30


def _state_dir(environ) -> Path:
    override = environ.get("SESSION_KIT_STATE_DIR")
    if override:
        return Path(override)
    base = environ.get("XDG_STATE_HOME") or os.path.join(
        environ.get("HOME", "/tmp"), ".local/state"
    )
    return Path(base) / "session-kit"


@dataclass
class Runner:
    """Where the release keeps its helpers, and how to reach them."""

    sp_cmd: str
    status_cmd: str
    state_dir: Path
    environ: dict

    @classmethod
    def from_environment(cls, script_dir: Path, environ=None) -> "Runner":
        environ = dict(os.environ if environ is None else environ)
        return cls(
            sp_cmd=environ.get("SESSION_KIT_SP_CMD") or str(script_dir / "sp"),
            status_cmd=environ.get("SESSION_KIT_STATUS_CMD")
            or str(script_dir / "shpool_status"),
            state_dir=_state_dir(environ),
            environ=environ,
        )

    # ---- reading ---------------------------------------------------------

    def snapshot(self, *, timeout: int = SNAPSHOT_TIMEOUT_SECONDS) -> Inventory | None:
        """The estate as the session manager reports it, or nothing at all.

        A refusal to answer is not an empty estate: the caller keeps the last
        list it had and the screen says it is showing one.
        """

        try:
            done = subprocess.run(
                [self.status_cmd, "--json"],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self.environ,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        try:
            return parse_inventory(json.loads(done.stdout))
        except ValueError:
            return None

    def closed_sessions(self) -> tuple[ClosedSession, ...]:
        """The ledger of deliberate closes, newest first.

        Written by the close path. Until that ledger exists the screen says
        the list is empty, which is the truth about what it can offer.
        """

        # The TUI used to have a second, unlocked reader which looked in the
        # state directory, skipped malformed rows, and turned every read error
        # into an empty screen. Use the durable ledger's one transaction and
        # let its read error stay distinguishable from a genuinely empty list.
        # The same readability rule the recovery list applies, asked of the
        # same ledger: this screen offers a Restore, so it may not offer one
        # for a conversation whose transcript this machine cannot read.
        from sessionkit_inventory.transcripts import locate_transcript

        return parse_closed(
            closed_ledger.load_closed(
                environ=self.environ,
                still_readable=lambda provider, uuid: locate_transcript(
                    provider, uuid, environ=self.environ
                )
                is not None,
            )
        )

    def projects(self) -> tuple[tuple[str, str], ...]:
        path = self.environ.get("SESSION_KIT_PROJECTS_FILE") or str(
            Path(self.environ.get("HOME", "/tmp")) / ".config/session-kit/projects.tsv"
        )
        found: list[tuple[str, str]] = []
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 3 or not parts[0] or not parts[2].startswith("/"):
                        continue
                    found.append((parts[0], parts[2]))
        except OSError:
            return ()
        seen = set()
        unique = []
        for alias, path_text in found:
            if alias in seen:
                continue
            seen.add(alias)
            unique.append((alias, path_text))
        return tuple(unique)

    def accounts(self, provider: str) -> tuple[str, ...]:
        """The accounts this provider can actually switch to.

        `sp account choices` alone decides selectability; nothing here
        re-derives it, so an account this screen offers is one that command
        already called selectable.
        """

        try:
            done = subprocess.run(
                [self.sp_cmd, "account", "choices", provider],
                capture_output=True,
                text=True,
                timeout=20,
                env=self.environ,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        if done.returncode != 0:
            return ()
        try:
            payload = json.loads(done.stdout)
        except ValueError:
            return ()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list):
            return ()
        return tuple(
            str(row.get("alias"))
            for row in choices
            if isinstance(row, dict) and row.get("eligible") is True and row.get("alias")
        )

    def models(self, provider: str) -> tuple[str, ...]:
        """The models this machine offers for a provider.

        There is no catalog inside the kit, so the list is configuration: one
        `provider<TAB>model` line per model. With no configuration the panel
        says the list is empty instead of guessing a model name.
        """

        from sessionkit_inventory.worker_model import configured_models

        return configured_models(provider, self.environ)

    # ---- proofs ----------------------------------------------------------

    def write_proof(self, session: Session, inventory: Inventory) -> str | None:
        """Name the exact session, the way every picker action always has.

        A proof carries the process identities the snapshot saw. A repaint
        that moves the row cannot move the action with it.
        """

        if not inventory.live or not session.mutation_allowed:
            return None
        row = session.raw
        identity = row.get("identity") or {}
        shell = row.get("shpool_shell") or {}
        daemon = inventory.daemon or {}
        proof = {
            "daemon_pid": daemon.get("pid"),
            "daemon_process_start_ticks": daemon.get("process_start_ticks"),
            "proof_type": "session-kit-picker-session-v1",
            "provider": row.get("provider"),
            "provider_pid": identity.get("pid"),
            "provider_process_start_ticks": identity.get("process_start_ticks"),
            "schema_version": 1,
            "shell_pid": shell.get("pid"),
            "shell_process_start_ticks": shell.get("process_start_ticks"),
            "shpool_id": row.get("shpool_id_raw"),
            "started_at_unix_ms": row.get("started_at_unix_ms"),
            "uuid": identity.get("uuid") or "",
        }
        payload = (json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix="picker-proof.", suffix=".json", dir=str(self.state_dir)
            )
        except OSError:
            return None
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            metadata = os.lstat(name)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
            ):
                raise OSError("unsafe proof")
        except OSError:
            try:
                os.unlink(name)
            except OSError:
                pass
            return None
        return name

    # ---- running ---------------------------------------------------------

    def argv_for(self, plan: Plan, proof_path: str | None) -> list[str]:
        argv = []
        for word in plan.argv:
            if word == "sp":
                argv.append(self.sp_cmd)
            elif word == PROOF:
                argv.append(proof_path or "")
            else:
                argv.append(word)
        return argv

    def execute(
        self,
        plan: Plan,
        inventory: Inventory,
        *,
        session: Session | None = None,
        suspend: Callable[[], Any] | None = None,
        resume: Callable[[], Any] | None = None,
    ) -> tuple[bool, str]:
        """Run one plan. Returns whether it worked and anything it said."""

        proof_path = None
        if plan.wants_proof:
            if session is None:
                return False, voice.NOTHING_CHANGED
            proof_path = self.write_proof(session, inventory)
            if proof_path is None:
                return False, voice.NOTHING_CHANGED
        environ = dict(self.environ)
        environ.update(plan.env)
        if plan.confirm_id:
            environ["SESSION_KIT_CONFIRM_ID"] = plan.confirm_id
        argv = self.argv_for(plan, proof_path)
        capture = not plan.needs_terminal
        try:
            if plan.needs_terminal and suspend is not None:
                suspend()
            try:
                done = subprocess.run(
                    argv,
                    env=environ,
                    capture_output=capture,
                    text=True,
                )
            finally:
                if plan.needs_terminal and resume is not None:
                    resume()
        except (OSError, subprocess.SubprocessError):
            return False, voice.action_failed("The action")
        finally:
            if proof_path:
                try:
                    os.unlink(proof_path)
                except OSError:
                    pass
        if done.returncode == 0:
            return True, ""
        # A verb the release may not carry yet answers in its own words. Those
        # are the words shown: a friendlier sentence would hide which release
        # is installed. Every other refusal falls back to the plan's line.
        note = ""
        if capture and plan.quote_stderr:
            note = _first_line(done.stderr) or _first_line(done.stdout)
        return False, note


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def run_batch(
    runner: Runner,
    inventory: Inventory,
    plans: Sequence[Plan],
    *,
    suspend: Callable[[], Any] | None = None,
    resume: Callable[[], Any] | None = None,
) -> tuple[bool, str]:
    """Every plan in one Enter. The first refusal is the sentence shown."""

    everything_worked = True
    note = ""
    for plan in plans:
        session = inventory.by_key(plan.proof_for) if plan.proof_for else None
        ok, said = runner.execute(
            plan, inventory, session=session, suspend=suspend, resume=resume
        )
        if not ok:
            everything_worked = False
            note = note or said
    return everything_worked, note
