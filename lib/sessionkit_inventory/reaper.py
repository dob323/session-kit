"""Exact, fail-closed planning for provider-exited terminal cleanup."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Callable, Iterable, Mapping

from .common import CollectionError, PROVIDERS, valid_uuid
from .state_io import atomic_write_private_json, read_private_json


AUTO_CLOSE_SCHEMA_VERSION = 1
DEFAULT_AUTO_CLOSE_HOURS = 72
MAX_AUTO_CLOSE_HOURS = 24 * 365
GENERATED_SESSION_RE = re.compile(r"s[0-9]{8}-[0-9]{6}-[1-9][0-9]*(?:-[1-9][0-9]*)?")
APP_SERVER_STALE_SECONDS = 3600
MAX_APP_SERVER_REMOVALS = 64
APP_SERVER_DIR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _rows(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("sessions") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise CollectionError("shpool auto-close snapshot is malformed")
    return rows


def _proc_stat(path: Path) -> tuple[int, int] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    end = text.rfind(")")
    fields = text[end + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[1]), int(fields[19])
    except ValueError:
        return None


def scan_shell_facts(
    proc_root: Path,
    daemon_pid: int,
    *,
    max_nodes: int = 16384,
) -> dict[str, dict[str, Any]]:
    """Return exact daemon-child shell generations and empty-tree proof."""
    try:
        entries = [entry for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError as exc:
        raise CollectionError("cannot enumerate process filesystem") from exc
    if len(entries) > max_nodes:
        raise CollectionError("process filesystem exceeds auto-close safety bound")
    processes: dict[int, dict[str, Any]] = {}
    children: dict[int, list[int]] = {}
    for entry in entries:
        pid = int(entry.name)
        before = _proc_stat(entry / "stat")
        if before is None:
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
            cmdline = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", "replace")
            )
            # Only the daemon's direct children are ever asked for a session name
            # (see the facts loop below), and /proc/<pid>/environ is readable only
            # by the process owner. Reading it for every pid meant a denied open
            # per foreign process for a result that was then discarded. `before[0]`
            # is the ppid, so this is exactly children[daemon_pid].
            if before[0] == daemon_pid:
                environ = (entry / "environ").read_bytes().split(b"\0")
            else:
                environ = []
        except OSError:
            continue
        after = _proc_stat(entry / "stat")
        if before != after:
            continue
        processes[pid] = {
            "ppid": before[0],
            "start_ticks": before[1],
            "comm": comm,
            "cmdline": cmdline,
            "environ": environ,
        }
        children.setdefault(before[0], []).append(pid)

    def empty_shell(shell_pid: int) -> bool:
        direct = children.get(shell_pid, [])
        if not direct:
            return True
        if len(direct) != 1:
            return False
        wrapper = processes.get(direct[0], {})
        if wrapper.get("comm") != "script" or "bash -i" not in str(
            wrapper.get("cmdline", "")
        ):
            return False
        inner = children.get(direct[0], [])
        if len(inner) != 1 or processes.get(inner[0], {}).get("comm") not in {
            "bash",
            "sh",
        }:
            return False
        return not children.get(inner[0], [])

    facts: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for pid in children.get(daemon_pid, []):
        process = processes.get(pid)
        if process is None:
            continue
        session_id = None
        for item in process["environ"]:
            if item.startswith(b"SHPOOL_SESSION_NAME="):
                session_id = item.split(b"=", 1)[1].decode("utf-8", "replace")
                break
        if not session_id:
            continue
        if session_id in facts:
            duplicates.add(session_id)
            continue
        facts[session_id] = {
            "shell_pid": pid,
            "shell_start_ticks": process["start_ticks"],
            "empty": empty_shell(pid),
        }
    for session_id in duplicates:
        facts.pop(session_id, None)
    return facts


def _inventory_by_id(inventory: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    sessions = inventory.get("sessions")
    if (
        inventory.get("source") != "live"
        or inventory.get("stale") is not False
        or inventory.get("warnings")
        or not isinstance(sessions, list)
    ):
        raise CollectionError("guard-live inventory is unavailable")
    result: dict[str, Mapping[str, Any]] = {}
    for item in sessions:
        if not isinstance(item, Mapping):
            raise CollectionError("guard-live inventory contains a malformed row")
        session_id = item.get("shpool_id_raw")
        if not isinstance(session_id, str) or session_id in result:
            raise CollectionError("guard-live inventory has duplicate session IDs")
        result[session_id] = item
    return result


def _safe_candidate(
    row: Mapping[str, Any],
    item: Mapping[str, Any] | None,
    fact: Mapping[str, Any] | None,
    *,
    now_monotonic_ns: int,
) -> tuple[Any, ...] | None:
    session_id = row.get("name")
    shell = item.get("shpool_shell") if isinstance(item, Mapping) else None
    recovery = item.get("recovery") if isinstance(item, Mapping) else None
    exited_identity = item.get("exited_identity") if isinstance(item, Mapping) else None
    provider = item.get("exited_provider") if isinstance(item, Mapping) else None
    exit_ns = (
        item.get("provider_exited_at_monotonic_ns")
        if isinstance(item, Mapping)
        else None
    )
    if not _positive_int(exit_ns):
        return None
    assert isinstance(exit_ns, int)
    uuid = valid_uuid(recovery.get("uuid")) if isinstance(recovery, Mapping) else None
    if (
        not isinstance(session_id, str)
        or not GENERATED_SESSION_RE.fullmatch(session_id)
        or str(row.get("status", "")).casefold() != "disconnected"
        or not _positive_int(row.get("started_at_unix_ms"))
        or not _positive_int(row.get("last_disconnected_at_unix_ms"))
        or not isinstance(item, Mapping)
        or item.get("shpool_id_raw") != session_id
        or item.get("started_at_unix_ms") != row.get("started_at_unix_ms")
        or item.get("shpool_status") != row.get("status")
        or item.get("availability") != "ready"
        or item.get("provider") != "shell"
        or provider not in PROVIDERS
        or item.get("agent_status") != "provider exited"
        or item.get("needs_you") is not False
        or item.get("provider_exit_input_tracking") is not True
        or item.get("user_input_after_provider_exit") is not False
        or item.get("provider_exit_keep") is not False
        or exit_ns > now_monotonic_ns
        or not isinstance(shell, Mapping)
        or not isinstance(fact, Mapping)
        or fact.get("empty") is not True
        or shell.get("pid") != fact.get("shell_pid")
        or shell.get("process_start_ticks") != fact.get("shell_start_ticks")
        or not isinstance(recovery, Mapping)
        or recovery.get("available") is not True
        or recovery.get("provider") != provider
        or not uuid
        or not isinstance(exited_identity, Mapping)
        or exited_identity.get("confidence") != "historical-exact"
        or exited_identity.get("uuid") != uuid
        or item.get("mutation_allowed") is not True
    ):
        return None
    return (
        session_id,
        row["started_at_unix_ms"],
        row["last_disconnected_at_unix_ms"],
        fact["shell_pid"],
        fact["shell_start_ticks"],
        provider,
        uuid,
        exit_ns,
    )


def _phantom_candidate(
    row: Mapping[str, Any],
    item: Mapping[str, Any] | None,
    fact: Mapping[str, Any] | None,
) -> tuple[Any, ...] | None:
    """A daemon-listed session whose shell process no longer exists at all.

    The daemon keeps advertising it (an ESRCH during a kill aborts removal on
    stock shpool), the kit quarantines it, and no picker action can touch it —
    it lies in the list until a daemon restart. There is nothing to preserve:
    no shell, no provider, no identity. Nominated only after the full
    observation window proves the state durable.
    """
    session_id = row.get("name")
    if (
        not isinstance(session_id, str)
        or not GENERATED_SESSION_RE.fullmatch(session_id)
        or str(row.get("status", "")).casefold() != "disconnected"
        or not _positive_int(row.get("started_at_unix_ms"))
        or not isinstance(item, Mapping)
        or item.get("shpool_id_raw") != session_id
        or item.get("provider") != "unknown"
        or item.get("mutation_allowed") is not False
        or item.get("mutation_rejection_reason") != "missing-shell-generation"
        or item.get("shpool_shell") is not None
        or item.get("subagents")
        or fact is not None
    ):
        return None
    identity = item.get("identity")
    if isinstance(identity, Mapping) and identity.get("pid") is not None:
        return None
    diagnostics = item.get("diagnostics")
    if not isinstance(diagnostics, list) or not any(
        isinstance(entry, str) and "found 0" in entry for entry in diagnostics
    ):
        return None
    return (
        "phantom",
        session_id,
        row["started_at_unix_ms"],
    )


def plan_auto_close(
    shpool_payload: Any,
    inventory: Mapping[str, Any],
    shell_facts: Mapping[str, Mapping[str, Any]],
    previous: Any,
    *,
    now_monotonic_ns: int,
    minimum_age_ns: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return durable observations and candidates without taking action."""
    if not _positive_int(now_monotonic_ns) or not _positive_int(minimum_age_ns):
        raise CollectionError("auto-close monotonic clock or age is invalid")
    inventory_rows = _inventory_by_id(inventory)
    previous_rows = (
        previous.get("observations")
        if isinstance(previous, Mapping)
        and previous.get("schema_version") == AUTO_CLOSE_SCHEMA_VERSION
        else {}
    )
    if not isinstance(previous_rows, Mapping):
        previous_rows = {}
    observations: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    for row in _rows(shpool_payload):
        session_id = row.get("name")
        if not isinstance(session_id, str):
            continue
        phantom = None
        proof = _safe_candidate(
            row,
            inventory_rows.get(session_id),
            shell_facts.get(session_id),
            now_monotonic_ns=now_monotonic_ns,
        )
        if proof is None:
            phantom = _phantom_candidate(
                row,
                inventory_rows.get(session_id),
                shell_facts.get(session_id),
            )
            if phantom is None:
                continue
            proof = phantom
        proof_hash = hashlib.sha256(
            json.dumps(proof, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior = previous_rows.get(session_id)
        first = (
            prior.get("first_verified_monotonic_ns")
            if isinstance(prior, Mapping)
            and prior.get("proof_hash") == proof_hash
            and _positive_int(prior.get("first_verified_monotonic_ns"))
            else now_monotonic_ns
        )
        if not isinstance(first, int):
            raise CollectionError("auto-close observation clock is invalid")
        observations[session_id] = {
            "proof_hash": proof_hash,
            "first_verified_monotonic_ns": first,
        }
        if phantom is not None:
            if now_monotonic_ns - first < minimum_age_ns:
                continue
            candidates.append(
                {
                    "class": "shell-less-phantom",
                    "shpool_id": proof[1],
                    "started_at_unix_ms": proof[2],
                    "proof_hash": proof_hash,
                    "first_verified_monotonic_ns": first,
                }
            )
            continue
        if now_monotonic_ns - max(first, proof[-1]) < minimum_age_ns:
            continue
        candidates.append(
            {
                "shpool_id": proof[0],
                "started_at_unix_ms": proof[1],
                "last_disconnected_at_unix_ms": proof[2],
                "shell_pid": proof[3],
                "shell_start_ticks": proof[4],
                "provider": proof[5],
                "provider_uuid": proof[6],
                "provider_exited_at_monotonic_ns": proof[7],
                "proof_hash": proof_hash,
                "first_verified_monotonic_ns": first,
            }
        )
    observation_doc = {
        "schema_version": AUTO_CLOSE_SCHEMA_VERSION,
        "observed_at_monotonic_ns": now_monotonic_ns,
        "observations": observations,
    }
    candidate_doc = {
        "schema_version": AUTO_CLOSE_SCHEMA_VERSION,
        "generated_at_monotonic_ns": now_monotonic_ns,
        "policy": "provider-exited-exact-72h",
        "candidates": candidates,
    }
    return observation_doc, candidate_doc


def app_server_socket_answers(socket_path: Path) -> bool:
    """Whether an App Server socket still has a listener behind it.

    Only an outright refusal proves nobody is there. An unreadable path, an
    unexpected file type, a timeout, or any other error answers "yes" so the
    caller leaves the directory alone.
    """
    import socket as socketmod

    try:
        metadata = os.lstat(socket_path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not stat.S_ISSOCK(metadata.st_mode):
        return True
    connection = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
    try:
        connection.settimeout(1.0)
        connection.connect(os.fspath(socket_path))
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return True
    finally:
        connection.close()
    return True


def sweep_stale_app_server_dirs(
    state_dir: Path,
    live_session_ids: Iterable[str],
    *,
    now_unix: float,
    stale_after_seconds: int = APP_SERVER_STALE_SECONDS,
    max_removals: int = MAX_APP_SERVER_REMOVALS,
    socket_answers: Callable[[Path], bool] = app_server_socket_answers,
) -> list[str]:
    """Remove App Server directories no session can still be using.

    A `codex app-server` socket dies with its process, but the directory
    holding it survives every reboot, so the tree grows one entry per Codex
    window forever. A directory is removed only when all three proofs hold:
    the daemon no longer lists its session, its socket refuses a connection,
    and nothing has touched the directory or its files for the stale window.
    Anything unexpected — a foreign owner, a symlink, an unreadable entry, a
    socket that answers — leaves the directory in place. Returns the removed
    session IDs.
    """
    live = set(live_session_ids)
    app_root = state_dir / "app-server"
    try:
        names = sorted(os.listdir(app_root))
    except OSError:
        return []
    cutoff = now_unix - stale_after_seconds
    owner = os.geteuid()
    removed: list[str] = []
    for name in names:
        if len(removed) >= max_removals:
            break
        if name in live or not APP_SERVER_DIR_RE.fullmatch(name):
            continue
        directory = app_root / name
        try:
            metadata = os.lstat(directory)
        except OSError:
            continue
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != owner
            or metadata.st_mtime > cutoff
        ):
            continue
        children: list[str] = []
        newest = metadata.st_mtime
        try:
            for child in os.listdir(directory):
                child_metadata = os.lstat(directory / child)
                if (
                    not (
                        stat.S_ISREG(child_metadata.st_mode)
                        or stat.S_ISSOCK(child_metadata.st_mode)
                    )
                    or child_metadata.st_uid != owner
                ):
                    raise OSError("unexpected App Server directory entry")
                newest = max(newest, child_metadata.st_mtime)
                children.append(child)
        except OSError:
            continue
        if newest > cutoff or socket_answers(directory / "app.sock"):
            continue
        try:
            for child in children:
                os.unlink(directory / child)
            os.rmdir(directory)
        except OSError:
            continue
        removed.append(name)
    return removed


def sweep_app_servers_from_environment() -> list[str]:
    """Sweep using the reaper's exact daemon list; any doubt removes nothing."""
    try:
        payload = json.loads(os.environ["SK_REAPER_SHPOOL_JSON"])
        state_dir = Path(os.environ["SK_REAPER_STATE_DIR"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return []
    try:
        rows = _rows(payload)
    except CollectionError:
        return []
    live: list[str] = []
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not name:
            # An unreadable row means the live set is unknown, and an unknown
            # live set may never authorize a removal.
            return []
        live.append(name)
    return sweep_stale_app_server_dirs(state_dir, live, now_unix=time.time())


def plan_from_environment(
    *,
    write_state: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        shpool_payload = json.loads(os.environ["SK_REAPER_SHPOOL_JSON"])
        inventory = json.loads(os.environ["SK_REAPER_INVENTORY_JSON"])
        state_dir = Path(os.environ["SK_REAPER_STATE_DIR"])
        proc_root = Path(os.environ["SK_REAPER_PROC_ROOT"])
        daemon_pid = int(os.environ["SK_REAPER_DAEMON_PID"])
        hours = int(
            os.environ.get(
                "SK_REAPER_AUTO_CLOSE_HOURS",
                str(DEFAULT_AUTO_CLOSE_HOURS),
            )
        )
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise CollectionError("auto-close environment is invalid") from exc
    if not 1 <= hours <= MAX_AUTO_CLOSE_HOURS:
        raise CollectionError("auto-close age is invalid")
    now_monotonic_ns = time.monotonic_ns()
    if os.environ.get("SESSION_KIT_TESTING") == "1":
        raw_test_clock = os.environ.get("SESSION_KIT_TEST_MONOTONIC_NS")
        if raw_test_clock is not None:
            try:
                now_monotonic_ns = int(raw_test_clock)
            except ValueError as exc:
                raise CollectionError("auto-close test clock is invalid") from exc
            if not _positive_int(now_monotonic_ns):
                raise CollectionError("auto-close test clock is invalid")
    observation_path = state_dir / "auto-close-observations.json"
    previous = read_private_json(
        observation_path,
        max_bytes=1024 * 1024,
        allow_missing=True,
    )
    observation, candidates = plan_auto_close(
        shpool_payload,
        inventory,
        scan_shell_facts(proc_root, daemon_pid),
        previous,
        now_monotonic_ns=now_monotonic_ns,
        minimum_age_ns=hours * 3600 * 1_000_000_000,
    )
    if write_state:
        atomic_write_private_json(observation_path, observation)
        atomic_write_private_json(
            state_dir / "auto-close-candidates.json",
            candidates,
        )
    return observation, candidates


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--no-write", action="store_true")
    verify = subparsers.add_parser("verify")
    verify.add_argument("candidate")
    subparsers.add_parser("sweep-app-servers")
    args = parser.parse_args(argv)
    try:
        if args.command == "sweep-app-servers":
            swept = sweep_app_servers_from_environment()
            for session_id in swept:
                print(
                    f"App Server GC removed {session_id}",
                    file=sys.stderr,
                )
            print(len(swept))
            return 0
        if args.command == "plan":
            _, candidates = plan_from_environment(write_state=not args.no_write)
            print(f"auto_candidates={len(candidates['candidates'])} actions=0")
            return 0
        candidate = read_private_json(
            Path(args.candidate),
            max_bytes=64 * 1024,
        )
        if not isinstance(candidate, Mapping):
            raise CollectionError("auto-close candidate is malformed")
        _, current = plan_from_environment(write_state=False)
        matches = [item for item in current["candidates"] if item == candidate]
        if len(matches) != 1:
            raise CollectionError("auto-close candidate changed or is no longer safe")
        print("auto-close candidate verified")
        return 0
    except (CollectionError, OSError) as exc:
        print(f"session-kit auto-close: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
