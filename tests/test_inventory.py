from __future__ import annotations

import argparse
import ast
import copy
import contextlib
import errno
import fcntl
import hashlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import pty
import re
import select
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import time
import threading
from typing import Any
import unittest
from unittest import mock
import unicodedata

from tests.support import REPO, run


CORE_PATH = REPO / "lib" / "session_inventory.py"
CORE_SPEC = importlib.util.spec_from_file_location("session_inventory", CORE_PATH)
assert CORE_SPEC is not None and CORE_SPEC.loader is not None
inventory_core = importlib.util.module_from_spec(CORE_SPEC)
sys.modules[CORE_SPEC.name] = inventory_core
CORE_SPEC.loader.exec_module(inventory_core)
from sessionkit_inventory import lifecycle as lifecycle_state  # noqa: E402
from sessionkit_inventory.model import (  # noqa: E402
    classify_top_level_sessions,
    session_is_unavailable,
)
from sessionkit_inventory import origins as origins_state  # noqa: E402
from sessionkit_inventory import collector as collector_inventory  # noqa: E402
from sessionkit_inventory import processes as process_inventory  # noqa: E402
from sessionkit_inventory import providers_claude as claude_inventory  # noqa: E402
from sessionkit_inventory import recovery as recovery_state  # noqa: E402
from sessionkit_inventory import snapshot as snapshot_state  # noqa: E402
from sessionkit_inventory import state_io as state_io_state  # noqa: E402


# The renderer reads two things from the ambient process: whether stdout is a
# terminal (colour, `render._color_enabled`) and how wide that terminal is
# (`shutil.get_terminal_size`). Inheriting them made this module's answer
# depend on how the suite was launched -- green through a pipe, red on a
# terminal, with the width deciding which detail form a row shows and the
# terminal deciding whether escape codes sit between a row's marker and its
# text. Pin both here, once, so every test renders against the documented
# no-terminal fallbacks instead of the developer's window. A test that needs a
# specific width or colour still sets it locally, and that still wins.
_AMBIENT_PINS: list = []


def setUpModule() -> None:
    environment = mock.patch.dict(os.environ, {"SESSION_KIT_NO_COLOR": "1"})
    environment.start()
    _AMBIENT_PINS.append(environment)
    # Restored with the rest of the dict when the patch stops. Without this an
    # exported COLUMNS would answer shutil before the refusal below is reached.
    os.environ.pop("COLUMNS", None)
    # Refusing the window query is exactly the answer a pipe gives, so each
    # caller falls back to the width it declares in its own signature.
    no_terminal = mock.patch.object(
        os,
        "get_terminal_size",
        side_effect=OSError(errno.ENOTTY, "no terminal (pinned by the suite)"),
    )
    no_terminal.start()
    _AMBIENT_PINS.append(no_terminal)


def tearDownModule() -> None:
    while _AMBIENT_PINS:
        _AMBIENT_PINS.pop().stop()


def uuid_for(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def process(
    pid: int,
    ppid: int,
    comm: str,
    *,
    session_name: str = "",
    cwd: str = "/srv/project",
    cmdline: list[str] | None = None,
    start_ticks: int | None = None,
) -> dict:
    return {
        "pid": pid,
        "ppid": ppid,
        "comm": comm,
        "session_name": session_name,
        "cwd": cwd,
        "cmdline": cmdline if cmdline is not None else [f"/usr/bin/{comm}"],
        "start_ticks": start_ticks if start_ticks is not None else pid * 10,
    }


class SnapshotPublicationOrderTests(unittest.TestCase):
    BOOT = "11111111-2222-3333-4444-555555555555"
    GENERATION = {"pid": 10, "process_start_ticks": 100}

    def row(self, session_id: str, number: int, color: str) -> dict[str, object]:
        uuid = uuid_for(number)
        return {
            "shpool_id_raw": session_id,
            "provider": "codex",
            "identity": {
                "confidence": "exact",
                "provider": "codex",
                "uuid": uuid,
            },
            "display_color": color,
            "recovery": {
                "available": True,
                "provider": "codex",
                "uuid": uuid,
                "cwd": "/tmp",
                "argv": [],
                "command": "codex",
            },
        }

    def inventory(self, *rows: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": inventory_core.SCHEMA_VERSION,
            "generated_at": "2026-08-15T17:00:00Z",
            "source": "live",
            "stale": False,
            "warnings": [],
            "daemon_generation": dict(self.GENERATION),
            "sessions": list(rows),
            "outside_agents": [],
        }

    def assert_exact_marker(self, state: Path, document: Path) -> int:
        """Assert the on-disk artifact, without requiring a new-runtime API."""
        marker_path = state / "collection-markers" / f"{document.name}.json"
        self.assertTrue(
            marker_path.exists(),
            f"publication did not create a freshness marker for {document.name}",
        )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(
            hashlib.sha256(document.read_bytes()).hexdigest(),
            marker.get("content_sha256"),
        )
        sequence = marker.get("collection_start")
        self.assertIsInstance(sequence, int)
        self.assertGreater(sequence, 0)
        return sequence

    def ordered_write(
        self,
        paths: dict[str, Path],
        path: Path,
        payload: object,
        *,
        collection_start: int | None,
    ) -> None:
        """Exercise each runtime's actual publication behavior.

        Before ordered publication existed, the production primitive was the
        same atomic JSON replacement used here.  This fallback lets the test
        reach that old behavior and fail on the overwritten document instead
        of erroring merely because the new helper is absent.
        """
        writer = getattr(state_io_state, "write_collection_json", None)
        if writer is None:
            state_io_state.atomic_write_json(path, payload)
            return
        writer(paths, path, payload, collection_start=collection_start)

    def allocate_start(self, paths: dict[str, Path]) -> tuple[int, str | None]:
        """Reach the old plain-counter behavior when the allocator is absent."""
        allocator = getattr(state_io_state, "allocate_collection_start", None)
        if allocator is not None:
            return allocator(paths)
        counter = paths.get(
            "collection_sequence", paths["root"] / "collection-sequence.json"
        )
        try:
            prior_document = json.loads(counter.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior_document = {}
        prior = prior_document.get("last_collection_start")
        if isinstance(prior, bool) or not isinstance(prior, int) or prior <= 0:
            prior = 0
        sequence = prior + 1
        state_io_state.atomic_write_json(
            counter,
            {"schema_version": 1, "last_collection_start": sequence},
        )
        return sequence, None

    def snapshot_kwargs(
        self,
        state: Path,
        collect_live,
    ) -> dict[str, object]:
        paths = state_io_state._state_paths({"state_dir": state})

        def manifest(value):
            return recovery_state.recovery_manifest(
                value,
                schema_version=inventory_core.SCHEMA_VERSION,
                boot_id=lambda: self.BOOT,
            )

        def update_recovery(current_paths, value, *, collection_start=None):
            extra = {}
            if (
                "collection_start"
                in inspect.signature(recovery_state.update_recovery_state).parameters
            ):
                extra.update(
                    collection_start=collection_start,
                    write_collection_json=(
                        state_io_state.write_collection_json
                        if collection_start is not None
                        else None
                    ),
                )
            recovery_state.update_recovery_state(
                current_paths,
                value,
                schema_version=inventory_core.SCHEMA_VERSION,
                recovery_manifest=manifest,
                read_state_json=inventory_core._read_state_json,
                has_recovery_entries=lambda document: (
                    recovery_state._has_recovery_entries(
                        document,
                        valid_recovery_state=lambda candidate: (
                            recovery_state._valid_recovery_state(
                                candidate, schema_version=inventory_core.SCHEMA_VERSION
                            )
                        ),
                    )
                ),
                generation_key=recovery_state._generation_key,
                atomic_write_json=state_io_state.atomic_write_json,
                **extra,
            )

        def enqueue(
            current_paths,
            current,
            previous,
            *,
            boot_id,
            collection_start=None,
        ):
            extra = {}
            if (
                "collection_start"
                in inspect.signature(
                    inventory_core.enqueue_lost_conversations
                ).parameters
            ):
                extra["collection_start"] = collection_start
            return inventory_core.enqueue_lost_conversations(
                current_paths,
                current,
                previous,
                boot_id=boot_id,
                config={"state_dir": state},
                **extra,
            )

        kwargs: dict[str, object] = {
            "write_state": True,
            "config": {"state_dir": state},
            "schema_version": inventory_core.SCHEMA_VERSION,
            "load_config": lambda: {"state_dir": state},
            "state_paths": lambda _settings: paths,
            "state_lock": inventory_core.StateLock,
            "read_state_json": inventory_core._read_state_json,
            "collect_live": collect_live,
            "boot_id_factory": lambda: self.BOOT,
            "persist_last_exact": lambda *_args, **_kwargs: None,
            "apply_provider_exit_states": lambda *_args, **_kwargs: None,
            "prune_inactive_state": lambda *_args, **_kwargs: None,
            "read_terminal_registry": lambda *_args, **_kwargs: {},
            "read_terminal_retirements": lambda *_args, **_kwargs: {},
            "apply_terminal_numbers": lambda _value, *_args, **_kwargs: {},
            "terminal_retirement_payload": lambda *_args, **_kwargs: {},
            "atomic_write_json": state_io_state.atomic_write_json,
            "quarantine_orphaned_provider_untitled_markers": (
                lambda *_args, **_kwargs: []
            ),
            "update_recovery_state": update_recovery,
            "enqueue_lost_conversations": enqueue,
            "apply_session_origins": lambda *_args, **_kwargs: None,
            "capture_bounce_receipts": lambda _state: frozenset(),
            "capture_bounce_cleanup_generations": lambda _state: frozenset(),
            "capture_lifecycle_generations": lambda _state: frozenset(),
            "capture_origin_generations": lambda _state: frozenset(),
            "capture_provider_untitled_generations": lambda _state: frozenset(),
            "capture_session_color_generations": (
                inventory_core.capture_session_color_generations
            ),
            "prune_origins": lambda *_args, **_kwargs: 0,
            "publish_session_colors": inventory_core.publish_session_colors,
            "cold_inventory": lambda error: {"error": error},
        }
        parameters = inspect.signature(snapshot_state.snapshot).parameters
        if "allocate_collection_start" in parameters:
            kwargs.update(
                allocate_collection_start=state_io_state.allocate_collection_start,
                preflight_collection_documents=(
                    state_io_state.preflight_collection_documents
                ),
                write_collection_json=state_io_state.write_collection_json,
            )
        return kwargs

    def test_an_older_collection_cannot_replace_any_newer_published_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".publication-order-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            old = self.row("old", 1, "blue")
            new = self.row("new", 2, "red")
            human_new = {
                "shpool_id_raw": "human-new",
                "display_color": "green",
                "origin": "human",
                "recovery": {"available": False},
            }
            stale = self.inventory(old)
            newer = self.inventory(old, new, human_new)
            stale_started = threading.Event()
            release_stale = threading.Event()

            def collect(_settings):
                if threading.current_thread().name == "stale-collector":
                    stale_started.set()
                    self.assertTrue(release_stale.wait(5))
                    return copy.deepcopy(stale)
                return copy.deepcopy(newer)

            kwargs = self.snapshot_kwargs(state, collect)
            outcomes: dict[str, object] = {}

            def run_snapshot(label: str) -> None:
                outcomes[label] = snapshot_state.snapshot(**kwargs)

            older = threading.Thread(
                target=run_snapshot, args=("older",), name="stale-collector"
            )
            older.start()
            self.assertTrue(stale_started.wait(5))
            try:
                run_snapshot("newer")
            finally:
                release_stale.set()
            older.join(5)
            self.assertFalse(older.is_alive())

            paths = state_io_state._state_paths({"state_dir": state})
            stored_inventory = inventory_core._read_state_json(paths["inventory"])
            stored_manifest = inventory_core._read_state_json(paths["manifest"])
            pending = inventory_core._read_state_json(paths["pending"])
            self.assertEqual(
                ["human-new", "new", "old"],
                sorted(row["shpool_id_raw"] for row in stored_inventory["sessions"]),
            )
            self.assertEqual(["new", "old"], sorted(stored_manifest["sessions"]))
            self.assertFalse(
                pending
                and any(
                    entry["old_shpool_id"] == "new"
                    for entry in inventory_core.flatten_pending(pending)["entries"]
                )
            )
            self.assertEqual(
                ["human-new", "new", "old"],
                sorted(row["shpool_id_raw"] for row in outcomes["older"]["sessions"]),
            )

    def test_preupgrade_document_without_allocation_state_refuses(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".publication-upgrade-", dir=REPO
        ) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})
            incumbent = self.inventory(self.row("old", 1, "blue"))
            state_io_state.atomic_write_json(paths["inventory"], incumbent)
            incumbent_bytes = paths["inventory"].read_bytes()
            fresh = self.inventory(self.row("new", 2, "red"))
            result = snapshot_state.snapshot(
                **self.snapshot_kwargs(state, lambda _settings: copy.deepcopy(fresh))
            )

            self.assertEqual(incumbent_bytes, paths["inventory"].read_bytes())
            self.assertFalse(paths["collection_sequence"].exists())
            self.assertFalse(paths["collection_sequence_floor"].exists())
            self.assertNotIn("collection_start", result)
            self.assertEqual(
                ["new"], [row["shpool_id_raw"] for row in result["sessions"]]
            )
            self.assertTrue(
                any(
                    "both durable collection sequence records are unreadable" in warning
                    and "read-only" in warning
                    for warning in result["warnings"]
                )
            )

    def test_corrupt_document_without_allocation_state_refuses(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".publication-corrupt-", dir=REPO
        ) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})
            paths["inventory"].write_text("{broken\n", encoding="utf-8")
            paths["inventory"].chmod(0o600)
            incumbent_bytes = paths["inventory"].read_bytes()
            fresh = self.inventory(self.row("new", 2, "red"))

            result = snapshot_state.snapshot(
                **self.snapshot_kwargs(state, lambda _settings: copy.deepcopy(fresh))
            )

            self.assertEqual(incumbent_bytes, paths["inventory"].read_bytes())
            self.assertFalse(paths["collection_sequence"].exists())
            self.assertFalse(paths["collection_sequence_floor"].exists())
            self.assertNotIn("collection_start", result)
            self.assertEqual(
                ["new"],
                [row["shpool_id_raw"] for row in result["sessions"]],
            )
            self.assertTrue(
                any(
                    "both durable collection sequence records are unreadable" in warning
                    and "read-only" in warning
                    for warning in result["warnings"]
                )
            )

    def test_unknown_incoming_cannot_replace_a_marked_document(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".publication-unknown-", dir=REPO
        ) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})
            with inventory_core.StateLock(state, paths["lock"]):
                sequence, _diagnostic = self.allocate_start(paths)
                self.ordered_write(
                    paths,
                    paths["inventory"],
                    self.inventory(self.row("new", 2, "red")),
                    collection_start=sequence,
                )
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "unknown collection"
                ):
                    self.ordered_write(
                        paths,
                        paths["inventory"],
                        self.inventory(self.row("old", 1, "blue")),
                        collection_start=None,
                    )
            stored = inventory_core._read_state_json(paths["inventory"])
            self.assertEqual("new", stored["sessions"][0]["shpool_id_raw"])

    def test_counter_rebuild_stays_above_an_inflight_allocation(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".publication-floor-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})
            counter_path = paths.get(
                "collection_sequence", state / "collection-sequence.json"
            )
            huge = 10**40
            state_io_state.atomic_write_json(
                counter_path,
                {"schema_version": 1, "last_collection_start": huge},
            )

            first, _diagnostic = self.allocate_start(paths)
            counter_path.write_text("{broken\n", encoding="utf-8")
            counter_path.chmod(0o600)
            second, diagnostic = self.allocate_start(paths)

            self.assertEqual(huge + 1, first)
            self.assertEqual(huge + 2, second)
            self.assertIn("durable allocation floor", diagnostic)
            floor = state_io_state.read_private_json(
                paths["collection_sequence_floor"], max_bytes=8192
            )
            self.assertEqual(second, floor["last_collection_start"])

    def test_two_unreadable_sequence_records_refuse_publication(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".publication-refusal-", dir=REPO
        ) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})
            old = self.row("old", 1, "blue")
            human_new = self.row("human-new", 3, "green")
            human_new["origin"] = "human"

            with inventory_core.StateLock(state, paths["lock"]):
                first, _diagnostic = self.allocate_start(paths)
                self.ordered_write(
                    paths,
                    paths["inventory"],
                    self.inventory(old),
                    collection_start=first,
                )

            stale_started = threading.Event()
            release_stale = threading.Event()

            def collect(_settings):
                if threading.current_thread().name == "stale-collector":
                    stale_started.set()
                    self.assertTrue(release_stale.wait(5))
                    return copy.deepcopy(self.inventory(old))
                return copy.deepcopy(self.inventory(old, human_new))

            kwargs = self.snapshot_kwargs(state, collect)
            stale_result: dict[str, object] = {}

            def run_stale() -> None:
                stale_result.update(snapshot_state.snapshot(**kwargs))

            stale = threading.Thread(target=run_stale, name="stale-collector")
            stale.start()
            self.assertTrue(stale_started.wait(5))
            try:
                for path in (
                    paths["collection_sequence"],
                    paths["collection_sequence_floor"],
                ):
                    path.write_text("{broken\n", encoding="utf-8")
                    path.chmod(0o600)
                before = {
                    path.relative_to(state): path.read_bytes()
                    for path in state.rglob("*")
                    if path.is_file()
                }

                fresh = snapshot_state.snapshot(**kwargs)

                after = {
                    path.relative_to(state): path.read_bytes()
                    for path in state.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(before, after)
                self.assertNotIn("collection_start", fresh)
                self.assertEqual(
                    ["human-new", "old"],
                    sorted(row["shpool_id_raw"] for row in fresh["sessions"]),
                )
                self.assertTrue(
                    any(
                        "both durable collection sequence records are unreadable"
                        in warning
                        and "read-only" in warning
                        for warning in fresh["warnings"]
                    )
                )
            finally:
                release_stale.set()
                stale.join(5)
            self.assertFalse(stale.is_alive())
            self.assertEqual(2, stale_result["collection_start"]["sequence"])

            stored = inventory_core._read_state_json(paths["inventory"])
            self.assertEqual(
                ["old"],
                [row["shpool_id_raw"] for row in stored["sessions"]],
            )
            pending = inventory_core._read_state_json(paths["pending"])
            self.assertNotIn("human-new", json.dumps(pending, sort_keys=True))

    def test_absent_sequence_records_with_a_document_refuse_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".publication-lost-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})
            old = self.row("old", 1, "blue")
            human_new = self.row("human-new", 3, "green")
            human_new["origin"] = "human"

            with inventory_core.StateLock(state, paths["lock"]):
                first, _diagnostic = self.allocate_start(paths)
                self.ordered_write(
                    paths,
                    paths["inventory"],
                    self.inventory(old),
                    collection_start=first,
                )

            stale_started = threading.Event()
            release_stale = threading.Event()

            def collect(_settings):
                if threading.current_thread().name == "stale-collector":
                    stale_started.set()
                    self.assertTrue(release_stale.wait(5))
                    return copy.deepcopy(self.inventory(old))
                return copy.deepcopy(self.inventory(old, human_new))

            kwargs = self.snapshot_kwargs(state, collect)
            stale_result: dict[str, object] = {}

            def run_stale() -> None:
                stale_result.update(snapshot_state.snapshot(**kwargs))

            stale = threading.Thread(target=run_stale, name="stale-collector")
            stale.start()
            self.assertTrue(stale_started.wait(5))
            try:
                paths["collection_sequence"].unlink()
                paths["collection_sequence_floor"].unlink()
                for marker in paths["collection_markers"].iterdir():
                    marker.unlink()
                before = {
                    path.relative_to(state): path.read_bytes()
                    for path in state.rglob("*")
                    if path.is_file()
                }

                fresh = snapshot_state.snapshot(**kwargs)

                after = {
                    path.relative_to(state): path.read_bytes()
                    for path in state.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(before, after)
                self.assertNotIn("collection_start", fresh)
                self.assertEqual(
                    ["human-new", "old"],
                    sorted(row["shpool_id_raw"] for row in fresh["sessions"]),
                )
                self.assertTrue(
                    any(
                        "both durable collection sequence records are unreadable"
                        in warning
                        and "read-only" in warning
                        for warning in fresh["warnings"]
                    )
                )
            finally:
                release_stale.set()
                stale.join(5)
            self.assertFalse(stale.is_alive())
            self.assertEqual(2, stale_result["collection_start"]["sequence"])
            pending = inventory_core._read_state_json(paths["pending"])
            self.assertNotIn("human-new", json.dumps(pending, sort_keys=True))

    def test_any_document_or_marker_prevents_absent_state_bootstrap(self) -> None:
        document_keys = (
            "inventory",
            "terminal_numbers",
            "terminal_numbers_retired",
            "terminal_numbers_epoch",
            "manifest",
            "pending",
        )
        for evidence in (*document_keys, "marker"):
            with (
                self.subTest(evidence=evidence),
                tempfile.TemporaryDirectory(
                    prefix=".publication-evidence-", dir=REPO
                ) as raw,
            ):
                state = Path(raw)
                state.chmod(0o700)
                paths = state_io_state._state_paths({"state_dir": state})
                if evidence == "marker":
                    paths["collection_markers"].mkdir(mode=0o700)
                    marker = paths["collection_markers"] / "orphan.json"
                    marker.write_text("{broken\n", encoding="utf-8")
                    marker.chmod(0o600)
                else:
                    paths[evidence].write_text("{broken\n", encoding="utf-8")
                    paths[evidence].chmod(0o600)
                before = {
                    path.relative_to(state): path.read_bytes()
                    for path in state.rglob("*")
                    if path.is_file()
                }

                with self.assertRaisesRegex(
                    inventory_core.CollectionError,
                    "both durable collection sequence records are unreadable.*read-only",
                ):
                    self.allocate_start(paths)

                after = {
                    path.relative_to(state): path.read_bytes()
                    for path in state.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(before, after)

    def test_genuinely_fresh_state_bootstraps_at_one(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".publication-first-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})

            sequence, diagnostic = self.allocate_start(paths)

            self.assertEqual(1, sequence)
            self.assertIsNone(diagnostic)
            for key in ("collection_sequence", "collection_sequence_floor"):
                record = state_io_state.read_private_json(paths[key], max_bytes=8192)
                self.assertEqual(1, record["last_collection_start"])

    def test_one_readable_record_recovers_above_markers_and_inflight_floor(
        self,
    ) -> None:
        for floor, marker, expected in ((12, 20, 21), (20, 12, 21)):
            with (
                self.subTest(floor=floor, marker=marker),
                tempfile.TemporaryDirectory(
                    prefix=".publication-recovery-", dir=REPO
                ) as raw,
            ):
                state = Path(raw)
                state.chmod(0o700)
                paths = state_io_state._state_paths({"state_dir": state})
                state_io_state.atomic_write_json(
                    paths["collection_sequence_floor"],
                    {"schema_version": 1, "last_collection_start": floor},
                )
                self.ordered_write(
                    paths,
                    paths["inventory"],
                    self.inventory(self.row("old", 1, "blue")),
                    collection_start=marker,
                )

                sequence, _diagnostic = self.allocate_start(paths)

                self.assertEqual(expected, sequence)
                for key in ("collection_sequence", "collection_sequence_floor"):
                    record = state_io_state.read_private_json(
                        paths[key], max_bytes=8192
                    )
                    self.assertEqual(expected, record["last_collection_start"])

    def test_witness_precedes_document_and_closes_each_crash_boundary(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".publication-intent-", dir=REPO
        ) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})
            old_payload = self.inventory(self.row("old", 1, "blue"))
            new_payload = self.inventory(self.row("new", 2, "red"))
            first, _diagnostic = self.allocate_start(paths)
            self.ordered_write(
                paths,
                paths["inventory"],
                old_payload,
                collection_start=first,
            )
            second, _diagnostic = self.allocate_start(paths)
            marker_dir = paths.get("collection_markers", state / "collection-markers")
            marker_path = marker_dir / f"{paths['inventory'].name}.json"
            original_atomic_write = state_io_state.atomic_write_json

            def crash_before_witness(path: Path, payload: object) -> None:
                if path == marker_path:
                    raise inventory_core.CollectionError(
                        "simulated crash before witness replacement"
                    )
                original_atomic_write(path, payload)

            with mock.patch.object(
                state_io_state, "atomic_write_json", side_effect=crash_before_witness
            ):
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "before witness"
                ):
                    self.ordered_write(
                        paths,
                        paths["inventory"],
                        new_payload,
                        collection_start=second,
                    )
            self.assertEqual(first, self.assert_exact_marker(state, paths["inventory"]))

            calls: list[Path] = []

            def crash_after_witness(path: Path, payload: object) -> None:
                calls.append(path)
                if path == paths["inventory"]:
                    raise inventory_core.CollectionError(
                        "simulated crash after witness, before document"
                    )
                original_atomic_write(path, payload)

            with mock.patch.object(
                state_io_state, "atomic_write_json", side_effect=crash_after_witness
            ):
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "after witness"
                ):
                    self.ordered_write(
                        paths,
                        paths["inventory"],
                        new_payload,
                        collection_start=second,
                    )

            self.assertEqual([marker_path, paths["inventory"]], calls)
            self.assertEqual(
                "old",
                inventory_core._read_state_json(paths["inventory"])["sessions"][0][
                    "shpool_id_raw"
                ],
            )
            self.assertIsNone(
                state_io_state.read_collection_marker(paths, paths["inventory"])
            )
            refused, _diagnostics = state_io_state.preflight_collection_documents(
                paths, ("inventory",), first
            )
            self.assertEqual(["inventory.json"], refused)
            with self.assertRaisesRegex(
                inventory_core.CollectionError, "replacing newer"
            ):
                self.ordered_write(
                    paths,
                    paths["inventory"],
                    old_payload,
                    collection_start=first,
                )

            third, _diagnostic = self.allocate_start(paths)
            self.ordered_write(
                paths,
                paths["inventory"],
                new_payload,
                collection_start=third,
            )
            self.assertEqual(
                "new",
                inventory_core._read_state_json(paths["inventory"])["sessions"][0][
                    "shpool_id_raw"
                ],
            )
            self.assertEqual(third, self.assert_exact_marker(state, paths["inventory"]))

            fourth, _diagnostic = self.allocate_start(paths)

            def crash_after_document(path: Path, payload: object) -> None:
                original_atomic_write(path, payload)
                if path == paths["inventory"]:
                    raise inventory_core.CollectionError(
                        "simulated crash after document replacement"
                    )

            with mock.patch.object(
                state_io_state, "atomic_write_json", side_effect=crash_after_document
            ):
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "after document"
                ):
                    self.ordered_write(
                        paths,
                        paths["inventory"],
                        old_payload,
                        collection_start=fourth,
                    )
            self.assertEqual(
                fourth, self.assert_exact_marker(state, paths["inventory"])
            )
            refused, _diagnostics = state_io_state.preflight_collection_documents(
                paths, ("inventory",), third
            )
            self.assertEqual(["inventory.json"], refused)

    def test_pending_loss_is_reproved_immediately_before_authorities_advance(
        self,
    ) -> None:
        """A writer's returned success is not the consumer's durable evidence."""
        with tempfile.TemporaryDirectory(
            prefix=".pending-consumer-proof-", dir=REPO
        ) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})
            lost = self.row("lost", 91, "blue")
            survivor = self.row("survivor", 92, "green")
            previous = self.inventory(lost, survivor)
            current = self.inventory(survivor)
            previous_manifest = recovery_state.recovery_manifest(
                previous,
                schema_version=inventory_core.SCHEMA_VERSION,
                boot_id=lambda: self.BOOT,
            )
            prior_pending = {
                "schema_version": inventory_core.SCHEMA_VERSION,
                "generated_at": previous["generated_at"],
                "source_boot_id": self.BOOT,
                "source_daemon_generation": dict(self.GENERATION),
                "detected_boot_id": self.BOOT,
                "detected_daemon_generation": dict(self.GENERATION),
                "sessions": {},
                "queued_generations": [],
            }
            state_io_state.atomic_write_json(
                paths["collection_sequence_floor"],
                {"schema_version": 1, "last_collection_start": 1},
            )
            state_io_state.atomic_write_json(paths["inventory"], previous)
            state_io_state.atomic_write_json(paths["manifest"], previous_manifest)
            with inventory_core.StateLock(state, paths["lock"]):
                state_io_state.atomic_write_json(paths["pending"], prior_pending)
            replacement = state / ".prior-pending"
            state_io_state.atomic_write_json(replacement, prior_pending)

            real_prove = state_io_state._prove_published_descriptor
            attacked = False

            def replace_after_proof(descriptor: int, path: Path) -> None:
                nonlocal attacked
                real_prove(descriptor, path)
                if path == paths["pending"] and not attacked:
                    attacked = True
                    os.replace(replacement, path)
                    directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)

            with mock.patch.object(
                state_io_state,
                "_prove_published_descriptor",
                replace_after_proof,
            ):
                result = snapshot_state.snapshot(
                    **self.snapshot_kwargs(
                        state, lambda _settings: copy.deepcopy(current)
                    )
                )

            self.assertTrue(attacked)
            self.assertEqual(
                ["lost", "survivor"],
                [
                    row["shpool_id_raw"]
                    for row in inventory_core._read_state_json(paths["inventory"])[
                        "sessions"
                    ]
                ],
            )
            self.assertEqual(
                ["lost", "survivor"],
                sorted(inventory_core._read_state_json(paths["manifest"])["sessions"]),
            )
            self.assertTrue(
                any(
                    "pending recovery evidence changed before inventory/manifest advance"
                    in warning
                    and "found none" in warning
                    for warning in result["warnings"]
                )
            )

            snapshot_state.snapshot(
                **self.snapshot_kwargs(state, lambda _settings: copy.deepcopy(current))
            )
            queued = inventory_core.list_pending({"state_dir": state})["entries"]
            self.assertIn(uuid_for(91), {entry["uuid"] for entry in queued})

    def test_pending_publication_requires_this_thread_to_hold_the_state_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".pending-lock-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})
            lost = self.row("lost", 93, "blue")
            previous = self.inventory(lost)
            current = self.inventory()
            config = {"state_dir": state}
            real_atomic = state_io_state._atomic_write_bytes
            lock_observations: list[bool] = []

            def observe_pending_write(path: Path, payload: bytes) -> None:
                if path == paths["pending"]:
                    held = getattr(
                        state_io_state, "_held_publishing_locks", lambda: set()
                    )()
                    key = getattr(
                        state_io_state,
                        "_publishing_lock_key",
                        lambda root: os.path.abspath(os.fspath(root)),
                    )(state)
                    lock_observations.append(key in held)
                real_atomic(path, payload)

            with mock.patch.object(
                state_io_state,
                "_atomic_write_bytes",
                side_effect=observe_pending_write,
            ):
                queued = inventory_core.enqueue_lost_conversations(
                    paths,
                    current,
                    previous,
                    boot_id=self.BOOT,
                    config=config,
                )
            self.assertEqual([f"codex:{uuid_for(93)}"], queued)
            self.assertEqual([True], lock_observations)

            paths["pending"].unlink()
            with inventory_core.StateLock(state, paths["lock"]):
                inventory_core.enqueue_lost_conversations(
                    paths,
                    current,
                    previous,
                    boot_id=self.BOOT,
                    config=config,
                )

    def test_every_shipped_pending_writer_uses_the_locked_publication_seam(
        self,
    ) -> None:
        production = [
            REPO / "lib/session_inventory.py",
            *(REPO / "lib/sessionkit_inventory").rglob("*.py"),
            *(REPO / "tools").rglob("*.py"),
            REPO / "bin/reset-collection-order.py",
        ]
        calls: list[tuple[str, str, str]] = []
        aliases: list[tuple[str, str]] = []
        literals: list[tuple[str, str]] = []
        for path in production:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = {
                child: parent
                for parent in ast.walk(tree)
                for child in ast.iter_child_nodes(parent)
            }

            def owner(node: ast.AST) -> str:
                while node in parents:
                    node = parents[node]
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        return node.name
                return "<module>"

            def is_pending_subscript(node: ast.AST) -> bool:
                return (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.slice, ast.Constant)
                    and node.slice.value == "pending"
                )

            relative = path.relative_to(REPO).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and any(
                    is_pending_subscript(candidate) for candidate in ast.walk(node)
                ):
                    if isinstance(node.func, ast.Name):
                        callee = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        callee = node.func.attr
                    else:
                        callee = ast.unparse(node.func)
                    calls.append((relative, owner(node), callee))
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = node.value
                    if value is not None and is_pending_subscript(value):
                        aliases.append((relative, owner(node)))
                if (
                    isinstance(node, ast.Constant)
                    and node.value == "recovery-pending.json"
                ):
                    literals.append((relative, owner(node)))

        self.assertEqual(
            sorted(
                [
                    (
                        "lib/session_inventory.py",
                        "enqueue_lost_conversations",
                        "_state_paths",
                    ),
                    (
                        "lib/sessionkit_inventory/migration.py",
                        "apply_legacy_recovery_manifest",
                        "exists",
                    ),
                    (
                        "lib/sessionkit_inventory/migration.py",
                        "apply_legacy_recovery_manifest",
                        "is_symlink",
                    ),
                    (
                        "lib/sessionkit_inventory/migration.py",
                        "plan_legacy_recovery_manifest",
                        "exists",
                    ),
                    (
                        "lib/sessionkit_inventory/migration.py",
                        "plan_legacy_recovery_manifest",
                        "is_symlink",
                    ),
                    (
                        "lib/sessionkit_inventory/migration.py",
                        "rollback_legacy_recovery_manifest",
                        "exists",
                    ),
                    (
                        "lib/sessionkit_inventory/migration.py",
                        "rollback_legacy_recovery_manifest",
                        "is_symlink",
                    ),
                    (
                        "lib/sessionkit_inventory/recovery.py",
                        "acknowledge_pending",
                        "read_state_json",
                    ),
                    (
                        "lib/sessionkit_inventory/recovery.py",
                        "enqueue_lost_conversations",
                        "atomic_write_json",
                    ),
                    (
                        "lib/sessionkit_inventory/recovery.py",
                        "enqueue_lost_conversations",
                        "read_state_json",
                    ),
                    (
                        "lib/sessionkit_inventory/recovery.py",
                        "enqueue_lost_conversations",
                        "write_collection_json",
                    ),
                    (
                        "lib/sessionkit_inventory/recovery.py",
                        "list_pending",
                        "flatten_pending",
                    ),
                    (
                        "lib/sessionkit_inventory/recovery.py",
                        "list_pending",
                        "read_state_json",
                    ),
                    (
                        "lib/sessionkit_inventory/recovery.py",
                        "update_recovery_state",
                        "CollectionError",
                    ),
                    (
                        "lib/sessionkit_inventory/recovery.py",
                        "update_recovery_state",
                        "publish",
                    ),
                    (
                        "lib/sessionkit_inventory/recovery.py",
                        "update_recovery_state",
                        "read_state_json",
                    ),
                    (
                        "lib/sessionkit_inventory/recovery.py",
                        "update_recovery_state",
                        "read_state_json",
                    ),
                    (
                        "lib/sessionkit_inventory/snapshot.py",
                        "snapshot",
                        "CollectionError",
                    ),
                    (
                        "lib/sessionkit_inventory/snapshot.py",
                        "snapshot",
                        "read_state_json",
                    ),
                ]
            ),
            sorted(calls),
        )
        self.assertEqual([], aliases)
        self.assertEqual(
            [
                ("bin/reset-collection-order.py", "<module>"),
                ("lib/sessionkit_inventory/state_io.py", "_state_paths"),
            ],
            sorted(literals),
        )
        facade = (REPO / "lib/session_inventory.py").read_text(encoding="utf-8")
        self.assertEqual(4, facade.count("_require_pending_publication_lock(path)"))
        self.assertEqual(2, facade.count("with _state_io.publication_lock("))
        reset = (REPO / "bin/reset-collection-order.py").read_text(encoding="utf-8")
        self.assertLess(
            reset.index("fcntl.flock(descriptor, fcntl.LOCK_EX)"),
            reset.index("os.replace(source, target)"),
        )

    def test_the_new_lock_generation_fences_legacy_publishers(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".publication-fence-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})
            # Spell the legacy pathname independently of the new path map.
            # Loaded against a pre-fence runtime, this reaches the behavioral
            # inode assertion below instead of dying on a new-only dict key.
            legacy_lock = state / "inventory.lock"
            legacy_lock.touch(mode=0o600)
            waiting_fd = os.open(legacy_lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            self.addCleanup(lambda: os.close(waiting_fd))
            old_inode = os.fstat(waiting_fd).st_ino

            with inventory_core.StateLock(state, paths["lock"]):
                self.assertNotEqual(old_inode, legacy_lock.stat().st_ino)

            # An old collector that opened the former inode before the fence
            # can acquire that orphaned inode later, but its existing
            # post-flock inode check sees that the pathname now names the
            # refusal sentinel and aborts before publishing.
            fcntl.flock(waiting_fd, fcntl.LOCK_EX)
            self.assertNotEqual(
                os.fstat(waiting_fd).st_ino,
                legacy_lock.stat().st_ino,
            )
            fcntl.flock(waiting_fd, fcntl.LOCK_UN)
            self.assertEqual(0o400, stat.S_IMODE(legacy_lock.stat().st_mode))
            with self.assertRaises((inventory_core.CollectionError, OSError)):
                inventory_core.StateLock(state, legacy_lock).__enter__()


class CollectionOrderResetToolTests(unittest.TestCase):
    def run_reset(self, state: Path) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["SESSION_KIT_STATE_DIR"] = os.fspath(state)
        return subprocess.run(
            [sys.executable, os.fspath(REPO / "bin/reset-collection-order.py")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_offline_reset_archives_lost_order_state_and_preserves_other_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".publication-reset-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})
            state_io_state.atomic_write_json(paths["inventory"], {"sessions": []})
            paths["collection_markers"].mkdir(mode=0o700)
            state_io_state.atomic_write_json(
                paths["collection_markers"] / "inventory.json.json", {"broken": True}
            )
            paths["collection_sequence"].write_text("{broken\n", encoding="utf-8")
            paths["collection_sequence"].chmod(0o600)
            unrelated = state / "closed-sessions.jsonl"
            unrelated.write_text("preserved\n", encoding="utf-8")
            unrelated.chmod(0o600)

            completed = self.run_reset(state)

            self.assertEqual(0, completed.returncode, completed.stderr)
            archive = Path(completed.stdout.strip())
            self.assertEqual(state, archive.parent)
            self.assertEqual(b"preserved\n", unrelated.read_bytes())
            self.assertTrue((archive / "inventory.json").is_file())
            self.assertTrue((archive / "collection-sequence.json").is_file())
            self.assertTrue((archive / "collection-markers").is_dir())
            self.assertFalse(paths["inventory"].exists())
            self.assertFalse(paths["collection_sequence"].exists())
            self.assertFalse(paths["collection_markers"].exists())
            first, diagnostic = state_io_state.allocate_collection_start(paths)
            self.assertEqual(1, first)
            self.assertIsNone(diagnostic)

    def test_offline_reset_refuses_a_readable_allocation_record(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".publication-reset-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            paths = state_io_state._state_paths({"state_dir": state})
            state_io_state.atomic_write_json(paths["inventory"], {"sessions": []})
            state_io_state.atomic_write_json(
                paths["collection_sequence_floor"],
                {"schema_version": 1, "last_collection_start": 17},
            )
            inventory_before = paths["inventory"].read_bytes()
            floor_before = paths["collection_sequence_floor"].read_bytes()

            completed = self.run_reset(state)

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("reset is unnecessary", completed.stderr)
            self.assertEqual(inventory_before, paths["inventory"].read_bytes())
            self.assertEqual(
                floor_before, paths["collection_sequence_floor"].read_bytes()
            )
            self.assertFalse(any(state.glob("collection-order-reset-*")))


def inventory_fixture(
    count: int,
    *,
    providers: tuple[str, ...] = ("claude", "codex"),
) -> tuple[
    dict, list[dict], dict[int, dict], dict[int, list[dict]], tuple[dict, dict], dict
]:
    shpool = {"sessions": []}
    claude: list[dict] = []
    table = {10: process(10, 1, "shpool", cmdline=["/usr/bin/shpool", "daemon"])}
    codex_index: dict[int, list[dict]] = {}
    threads: dict[str, dict] = {}
    for index in range(1, count + 1):
        name = f"main{index}" if index > 1 else "main"
        root_pid = 1000 + index
        agent_pid = 2000 + index
        provider = providers[(index - 1) % len(providers)]
        exact_uuid = uuid_for(index)
        cwd = f"/srv/project-{index}"
        shpool["sessions"].append(
            {
                "name": name,
                "status": "Disconnected" if index % 2 else "Attached",
                "started_at_unix_ms": 1_700_000_000_000 + index,
            }
        )
        table[root_pid] = process(root_pid, 10, "bash", session_name=name, cwd=cwd)
        table[agent_pid] = process(
            agent_pid,
            root_pid,
            provider,
            cwd=cwd,
            cmdline=[f"/usr/bin/{provider}"],
        )
        if provider == "claude":
            claude.append(
                {
                    "pid": agent_pid,
                    "sessionId": exact_uuid,
                    "cwd": cwd,
                    "kind": "interactive",
                    "name": f"Claude task {index}",
                    "status": "busy",
                    "startedAt": 1_700_000_000_000 + index,
                }
            )
        else:
            codex_index[agent_pid] = [
                {
                    "source": "cli",
                    "id": exact_uuid,
                    "session_id": exact_uuid,
                }
            ]
            threads[exact_uuid] = {
                "id": exact_uuid,
                "title": f"Codex task {index}",
                "cwd": cwd,
            }
    config = {
        "aliases": {},
        "max_proc_nodes": 8192,
        "max_proc_depth": 32,
    }
    return shpool, claude, table, codex_index, (threads, {}), config


class InventoryScaleTests(unittest.TestCase):
    def test_claude_derived_placeholder_yields_to_alias_but_missing_source_wins(
        self,
    ) -> None:
        fixture = list(inventory_fixture(1, providers=("claude",)))
        uuid = uuid_for(1)
        key = f"claude:{uuid}"
        fixture[1][0].update({"name": "v2-5e", "nameSource": "derived"})
        fixture[5].update(
            {
                "aliases": {key: "Session Kit Closeout"},
                "automatic_titles": {key: "Session Kit Closeout"},
                "pushed_titles": {key: "Session Kit Closeout"},
            }
        )

        derived = inventory_core.build_inventory(*fixture, now=1_800_000_000)[
            "sessions"
        ][0]
        self.assertEqual("Session Kit Closeout", derived["title"])
        self.assertEqual("alias", derived["title_source"])

        fixture[1][0].pop("nameSource")
        historical = inventory_core.build_inventory(*fixture, now=1_800_000_000)[
            "sessions"
        ][0]
        self.assertEqual("v2-5e", historical["title"])
        self.assertEqual("native", historical["title_source"])

    def test_claude_agent_name_outranks_generated_native_title(self) -> None:
        fixture = list(inventory_fixture(1, providers=("claude",)))
        fixture[1][0].update(
            {
                "name": "Verify disposable session kit",
                "nameSource": "derived",
                "aiTitle": "Verify disposable session kit",
                "agentName": "Disposable Claude Proof",
            }
        )

        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)

        row = result["sessions"][0]
        self.assertEqual("Disposable Claude Proof", row["native_title"])
        self.assertEqual("Disposable Claude Proof", row["title"])
        self.assertEqual("native", row["title_source"])
        self.assertEqual("ready", row["provider_title_state"])
        self.assertEqual("not-needed", row["automatic_name_state"])

    def test_claude_stale_pre_push_name_is_pending_and_third_name_is_human(
        self,
    ) -> None:
        fixture = list(inventory_fixture(1, providers=("claude",)))
        uuid = uuid_for(1)
        key = f"claude:{uuid}"
        fixture[5].update(
            {
                "aliases": {key: "New Kit Name"},
                "automatic_titles": {key: "New Kit Name"},
                "pushed_titles": {key: "New Kit Name"},
                "pending_native_titles": {
                    key: {
                        "title": "Old Registry Name",
                        "nameSince": 100,
                        "nameSource": "",
                    }
                },
            }
        )
        fixture[1][0]["name"] = "Old Registry Name"
        fixture[1][0]["nameSince"] = 100

        pending = inventory_core.build_inventory(*fixture, now=1_800_000_000)[
            "sessions"
        ][0]
        self.assertEqual("New Kit Name", pending["title"])
        self.assertEqual("alias", pending["title_source"])
        self.assertEqual("pending", pending["provider_title_state"])

        fixture[1][0]["name"] = "A Third Human Name"
        fixture[1][0]["nameSince"] = 200
        human = inventory_core.build_inventory(*fixture, now=1_800_000_000)["sessions"][
            0
        ]
        self.assertEqual("A Third Human Name", human["title"])
        self.assertEqual("native", human["title_source"])
        self.assertEqual("ready", human["provider_title_state"])

    def test_zero_one_and_100_plus_counts_have_no_ceiling(self) -> None:
        for count in (0, 1, 9, 10, 99, 125, 250):
            with self.subTest(count=count):
                fixture = inventory_fixture(count)
                result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
                self.assertEqual(count, len(result["sessions"]))
                self.assertEqual(
                    list(range(1, count + 1)),
                    sorted(row["row"] for row in result["sessions"]),
                )
                self.assertEqual(
                    count,
                    len({row["shpool_id"] for row in result["sessions"]}),
                )
                with mock.patch.dict(
                    os.environ,
                    {"COLUMNS": "60", "SESSION_KIT_NO_COLOR": "1"},
                    clear=False,
                ):
                    rendered = inventory_core.render_inventory(result, rows_only=True)
                if count:
                    session_word = "session" if count == 1 else "sessions"
                    self.assertTrue(
                        rendered.startswith(f"  {count} {session_word}:"),
                        rendered[:100],
                    )
                else:
                    self.assertTrue(rendered.startswith("  Sessions: none."))
                if count in (0, 1, 125):
                    self.assertTrue(
                        all(
                            inventory_core._display_width(line) <= 59
                            for line in rendered.splitlines()
                        ),
                        rendered,
                    )

    def test_action_provider_activity_sort_and_rows_are_deterministic(self) -> None:
        fixture = list(inventory_fixture(12, providers=("claude",)))
        for item in fixture[0]["sessions"]:
            item["status"] = "Disconnected"
        fixture[1][9]["status"] = "waiting"
        fixture[1][9]["waitingFor"] = "reply"
        recent = {
            "main2": 2_000,
            "main3": 3_000,
            "main4": 3_000,
            "main10": 1_000,
        }
        first = inventory_core.build_inventory(
            *fixture,
            now=1_800_000_000,
            recent_output_by_shpool_id=recent,
        )
        fixture[0] = {"sessions": list(reversed(fixture[0]["sessions"]))}
        fixture[1] = list(reversed(fixture[1]))
        second = inventory_core.build_inventory(
            *fixture,
            now=1_800_000_000,
            recent_output_by_shpool_id=dict(reversed(tuple(recent.items()))),
        )
        expected = [
            "main3",
            "main4",
            "main2",
            "main10",
            "main",
            "main5",
            "main6",
            "main7",
            "main8",
            "main9",
            "main11",
            "main12",
        ]
        self.assertEqual(expected, [item["shpool_id"] for item in first["sessions"]])
        self.assertEqual(expected, [item["shpool_id"] for item in second["sessions"]])
        first_rows = {item["shpool_id"]: item["row"] for item in first["sessions"]}
        second_rows = {item["shpool_id"]: item["row"] for item in second["sessions"]}
        self.assertEqual(first_rows, second_rows)
        self.assertEqual(
            list(range(1, 13)), [item["row"] for item in first["sessions"]]
        )
        self.assertTrue(first["sessions"][3]["needs_you"])
        self.assertEqual(3_000, first["sessions"][0]["recent_output_at_unix_ms"])
        self.assertEqual(3_000, first["sessions"][1]["recent_output_at_unix_ms"])

    def test_availability_then_provider_precede_activity(self) -> None:
        fixture = list(inventory_fixture(4, providers=("codex", "claude")))
        fixture[0]["sessions"][1]["status"] = "Disconnected"
        fixture[0]["sessions"][3]["status"] = "Disconnected"
        result = inventory_core.build_inventory(
            *fixture,
            now=1_800_000_000,
            recent_output_by_shpool_id={
                "main": 4_000,
                "main2": 1_000,
                "main3": 3_000,
                "main4": 2_000,
            },
        )
        self.assertEqual(
            ["claude", "claude", "codex", "codex"],
            [item["provider"] for item in result["sessions"]],
        )
        self.assertEqual(
            ["main4", "main2", "main", "main3"],
            [item["shpool_id"] for item in result["sessions"]],
        )

    def test_recent_output_uses_exact_map_legacy_and_segmented_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".recent-output-", dir=REPO) as raw:
            base = Path(raw)
            journals = base / "journals"
            recovery = base / "recovery"
            journals.mkdir()
            recovery.mkdir()

            legacy = journals / "main.raw"
            legacy.write_text("legacy", encoding="utf-8")
            os.utime(legacy, ns=(1_000_000_000, 1_000_000_000))

            segment_dir = journals / "main2"
            segment_dir.mkdir()
            first_segment = segment_dir / "segment-000001.raw"
            latest_segment = segment_dir / "segment-000002.raw"
            ignored = segment_dir / "notes.txt"
            for path in (first_segment, latest_segment, ignored):
                path.write_text(path.name, encoding="utf-8")
            os.utime(first_segment, ns=(2_000_000_000, 2_000_000_000))
            os.utime(latest_segment, ns=(3_000_000_000, 3_000_000_000))
            os.utime(ignored, ns=(9_000_000_000, 9_000_000_000))

            active_but_mapped = journals / "main3.raw"
            active_but_mapped.write_text("active", encoding="utf-8")
            os.utime(active_but_mapped, ns=(8_000_000_000, 8_000_000_000))
            recovered = recovery / "main3-recovered.raw"
            recovered.write_text("mapped", encoding="utf-8")
            os.utime(recovered, ns=(4_000_000_000, 4_000_000_000))
            (recovery / "current-map.tsv").write_text(
                f"main3\t{recovered}\nmain4\t{recovered}\nmain4\t{active_but_mapped}\n",
                encoding="utf-8",
            )

            result = inventory_core.recent_output_times(
                ("main", "main2", "main3", "main4", "custom-session"),
                journal_dir=journals,
                recovery_dir=recovery,
            )
            self.assertEqual(
                {"main": 1_000, "main2": 3_000, "main3": 4_000},
                result,
            )


class InventoryIdentityTests(unittest.TestCase):
    def test_claude_owner_keeps_a_descendant_codex_candidate_as_a_worker(
        self,
    ) -> None:
        fixture = list(inventory_fixture(1, providers=("claude",)))
        child_pid = 3001
        child_uuid = uuid_for(2)
        fixture[2][child_pid] = process(
            child_pid,
            2001,
            "codex",
            start_ticks=30_010,
            cmdline=["/usr/bin/codex", "exec", "verify"],
        )
        fixture[3][child_pid] = [
            {
                "source": "cli",
                "id": child_uuid,
                "session_id": child_uuid,
                "_turn_state": "working",
            }
        ]
        fixture[4][0][child_uuid] = {
            "id": child_uuid,
            "title": "Codex worker",
            "cwd": "/srv/project-1",
        }

        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)

        row = result["sessions"][0]
        self.assertEqual("claude", row["provider"])
        self.assertEqual(uuid_for(1), row["identity"]["uuid"])
        self.assertEqual(
            [
                {
                    "provider": "codex",
                    "uuid": child_uuid,
                    "pid": child_pid,
                    "title": "Codex worker",
                    "status": "working",
                }
            ],
            row["subagents"],
        )
        self.assertEqual(1, row["active_subagent_count"])
        self.assertEqual([], result["outside_agents"])

    def test_codex_owner_keeps_a_descendant_claude_candidate_as_a_worker(
        self,
    ) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        child_pid = 3001
        child_uuid = uuid_for(2)
        fixture[2][child_pid] = process(
            child_pid,
            2001,
            "claude",
            start_ticks=30_010,
            cmdline=["/usr/bin/claude", "--session-id", child_uuid],
        )
        fixture[1].append(
            {
                "pid": child_pid,
                "sessionId": child_uuid,
                "cwd": "/srv/project-1",
                "kind": "interactive",
                "name": "Claude worker",
                "status": "busy",
                "startedAt": 1_700_000_000_002,
            }
        )

        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)

        row = result["sessions"][0]
        self.assertEqual("codex", row["provider"])
        self.assertEqual(uuid_for(1), row["identity"]["uuid"])
        self.assertEqual(
            [
                {
                    "provider": "claude",
                    "uuid": child_uuid,
                    "pid": child_pid,
                    "title": "Claude worker",
                    "status": "working",
                }
            ],
            row["subagents"],
        )
        self.assertEqual(1, row["active_subagent_count"])
        self.assertEqual([], result["outside_agents"])

    def test_disjoint_provider_candidates_stay_unresolved(self) -> None:
        fixture = list(inventory_fixture(1, providers=("claude",)))
        rival_pid = 3001
        rival_uuid = uuid_for(2)
        fixture[2][rival_pid] = process(
            rival_pid,
            1001,
            "codex",
            start_ticks=30_010,
            cmdline=["/usr/bin/codex"],
        )
        fixture[3][rival_pid] = [
            {"source": "cli", "id": rival_uuid, "session_id": rival_uuid}
        ]
        fixture[4][0][rival_uuid] = {
            "id": rival_uuid,
            "title": "Rival Codex",
            "cwd": "/srv/project-1",
        }

        row = inventory_core.build_inventory(*fixture, now=1_800_000_000)["sessions"][0]

        self.assertEqual("unknown", row["provider"])
        self.assertEqual("Unresolved provider session", row["title"])
        self.assertIsNone(row["identity"]["uuid"])

    def test_unreadable_link_cannot_turn_a_candidate_into_a_worker(self) -> None:
        fixture = list(inventory_fixture(1, providers=("claude",)))
        bridge_pid = 2501
        child_pid = 3001
        child_uuid = uuid_for(2)
        fixture[2][bridge_pid] = {
            **process(bridge_pid, 2001, "", start_ticks=-1, cmdline=[]),
            "argv_unreadable": True,
        }
        fixture[2][child_pid] = process(
            child_pid,
            bridge_pid,
            "codex",
            start_ticks=30_010,
            cmdline=["/usr/bin/codex", "exec", "verify"],
        )
        fixture[3][child_pid] = [
            {"source": "cli", "id": child_uuid, "session_id": child_uuid}
        ]
        fixture[4][0][child_uuid] = {
            "id": child_uuid,
            "title": "Unproven Codex worker",
            "cwd": "/srv/project-1",
        }

        row = inventory_core.build_inventory(*fixture, now=1_800_000_000)["sessions"][0]

        self.assertEqual("unknown", row["provider"])
        self.assertEqual("Unresolved provider session", row["title"])
        self.assertIsNone(row["identity"]["uuid"])

    def test_recycled_parent_generation_cannot_fake_worker_lineage(self) -> None:
        fixture = list(inventory_fixture(1, providers=("claude",)))
        child_pid = 3001
        child_uuid = uuid_for(2)
        fixture[2][2001]["start_ticks"] = 40_010
        fixture[2][child_pid] = process(
            child_pid,
            2001,
            "codex",
            start_ticks=30_010,
            cmdline=["/usr/bin/codex", "exec", "verify"],
        )
        fixture[3][child_pid] = [
            {"source": "cli", "id": child_uuid, "session_id": child_uuid}
        ]
        fixture[4][0][child_uuid] = {
            "id": child_uuid,
            "title": "Stale Codex child",
            "cwd": "/srv/project-1",
        }

        row = inventory_core.build_inventory(*fixture, now=1_800_000_000)["sessions"][0]

        self.assertEqual("unknown", row["provider"])
        self.assertEqual("Unresolved provider session", row["title"])
        self.assertIsNone(row["identity"]["uuid"])

    def test_linux_process_scan_retains_exact_provider_account_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".proc-account-", dir=REPO) as raw:
            proc_root = Path(raw)
            entry = proc_root / "123"
            entry.mkdir()
            (entry / "stat").write_text("fixture\n", encoding="utf-8")
            (entry / "cmdline").write_bytes(b"codex\0")
            (entry / "comm").write_text("codex\n", encoding="utf-8")
            (entry / "environ").write_bytes(
                b"CLAUDE_CONFIG_DIR=/profiles/claude-a\0"
                b"CODEX_HOME=/profiles/codex-a\0"
                b"SESSION_KIT_ACCOUNT_ALIAS=paid-a\0"
                b"SESSION_KIT_ACCOUNT_CAPABLE=1\0"
            )
            os.symlink(REPO, entry / "cwd")
            with mock.patch.object(
                process_inventory, "_proc_stat", return_value=(123, 10, 1000)
            ):
                scanned = process_inventory.scan_process_table(
                    proc_root,
                    64,
                    proc_stat=process_inventory._proc_stat,
                    proc_environ=process_inventory._proc_environ,
                )
        self.assertEqual("/profiles/claude-a", scanned[123]["claude_config_dir"])
        self.assertEqual("/profiles/codex-a", scanned[123]["codex_home"])
        self.assertEqual("paid-a", scanned[123]["account_alias"])
        self.assertEqual("1", scanned[123]["account_capable"])

    def test_unreadable_own_environment_is_recorded_and_named_in_diagnostics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".proc-denied-", dir=REPO) as raw:
            proc_root = Path(raw)
            entry = proc_root / "123"
            entry.mkdir()
            (entry / "stat").write_text("fixture\n", encoding="utf-8")
            (entry / "cmdline").write_bytes(b"bash\0")
            (entry / "comm").write_text("bash\n", encoding="utf-8")
            # A directory where the file belongs denies the read for every
            # uid, which is what a ptrace policy does to a live managed shell.
            (entry / "environ").mkdir()
            with mock.patch.object(
                process_inventory, "_proc_stat", return_value=(123, 10, 1000)
            ):
                scanned = process_inventory.scan_process_table(
                    proc_root,
                    64,
                    proc_stat=process_inventory._proc_stat,
                    proc_environ=process_inventory._proc_environ,
                )
        self.assertTrue(scanned[123]["environ_unreadable"])
        self.assertEqual("", scanned[123]["session_name"])
        table = {
            10: {"pid": 10, "ppid": 1, "cmdline": ["/usr/bin/shpool", "daemon"]},
            123: scanned[123],
        }
        roots, diagnostics = process_inventory.shpool_roots(
            ["s20260811-090000-3"],
            table,
            is_shpool_daemon=process_inventory._is_shpool_daemon,
        )
        self.assertEqual({}, roots)
        self.assertTrue(
            any(
                "unreadable environment" in line
                for line in diagnostics["s20260811-090000-3"]
            ),
            diagnostics,
        )

    def test_failed_uid_check_records_environment_as_unknown(self) -> None:
        with mock.patch.object(Path, "stat", side_effect=OSError("denied")):
            self.assertIsNone(process_inventory._readable_by_us(Path("/proc/123")))
        with tempfile.TemporaryDirectory(prefix=".proc-uid-unknown-", dir=REPO) as raw:
            proc_root = Path(raw)
            entry = proc_root / "123"
            entry.mkdir()
            (entry / "stat").write_text("fixture\n", encoding="utf-8")
            (entry / "cmdline").write_bytes(b"bash\0")
            (entry / "comm").write_text("bash\n", encoding="utf-8")
            with (
                mock.patch.object(
                    process_inventory, "_proc_stat", return_value=(123, 10, 1000)
                ),
                mock.patch.object(
                    process_inventory, "_readable_by_us", return_value=None
                ),
            ):
                scanned = process_inventory.scan_process_table(
                    proc_root,
                    64,
                    proc_stat=process_inventory._proc_stat,
                    proc_environ=process_inventory._proc_environ,
                )
        self.assertTrue(scanned[123]["environ_unreadable"])
        self.assertEqual("", scanned[123]["session_name"])

    def test_a_process_whose_argv_will_not_read_stays_in_the_table(self) -> None:
        """A process that exists and will not say what it is still exists.

        Dropped from the table, it left readers answering "nothing in this
        tree holds that socket" about a process they never saw -- and that
        answer is what moves a person's session behind the machine count. It
        stays, placed by its parent, saying nothing but that it is unread.
        """
        with tempfile.TemporaryDirectory(prefix=".proc-argv-", dir=REPO) as raw:
            proc_root = Path(raw)
            entry = proc_root / "123"
            entry.mkdir()
            (entry / "stat").write_text("fixture\n", encoding="utf-8")
            # A directory where the file belongs denies the read for every
            # uid, the same shape the environ test uses.
            (entry / "cmdline").mkdir()
            (entry / "comm").write_text("codex\n", encoding="utf-8")
            (entry / "environ").write_bytes(b"SHPOOL_SESSION_NAME=main\0")
            with mock.patch.object(
                process_inventory, "_proc_stat", return_value=(123, 10, 1000)
            ):
                scanned = process_inventory.scan_process_table(
                    proc_root,
                    64,
                    proc_stat=process_inventory._proc_stat,
                    proc_environ=process_inventory._proc_environ,
                )

        self.assertIn(123, scanned)
        self.assertTrue(scanned[123]["argv_unreadable"])
        self.assertEqual(10, scanned[123]["ppid"])
        # Nothing about it may be mistaken for evidence of anything.
        self.assertEqual([], scanned[123]["cmdline"])
        self.assertEqual("", scanned[123]["comm"])
        self.assertEqual("", scanned[123]["session_name"])

    def test_a_scan_that_lost_a_process_says_so(self) -> None:
        """The loss with no parent, which cannot be recorded in place.

        A pid listed a moment ago whose stat will not read has no parent, so
        it belongs to no tree and no slot can hold it. The scan itself carries
        the fact instead. A fixture that never says otherwise reads as
        complete, which is what a fixture is.
        """
        with tempfile.TemporaryDirectory(prefix=".proc-gone-", dir=REPO) as raw:
            proc_root = Path(raw)
            for name in ("123", "124"):
                entry = proc_root / name
                entry.mkdir()
                (entry / "stat").write_text("fixture\n", encoding="utf-8")
                (entry / "cmdline").write_bytes(b"bash\0")
                (entry / "comm").write_text("bash\n", encoding="utf-8")
                (entry / "environ").write_bytes(b"")
            with mock.patch.object(
                process_inventory,
                "_proc_stat",
                side_effect=[
                    OSError(errno.ESRCH, "vanished"),
                    (124, 10, 1000),
                    (124, 10, 1000),
                ],
            ):
                scanned = process_inventory.scan_process_table(
                    proc_root,
                    64,
                    proc_stat=process_inventory._proc_stat,
                    proc_environ=process_inventory._proc_environ,
                )

        self.assertEqual([124], sorted(scanned))
        self.assertFalse(process_inventory.table_is_complete(scanned))
        self.assertTrue(process_inventory.table_is_complete({124: scanned[124]}))

    def test_readable_environment_never_claims_an_unreadable_one(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".proc-plain-", dir=REPO) as raw:
            proc_root = Path(raw)
            entry = proc_root / "123"
            entry.mkdir()
            (entry / "stat").write_text("fixture\n", encoding="utf-8")
            (entry / "cmdline").write_bytes(b"bash\0")
            (entry / "comm").write_text("bash\n", encoding="utf-8")
            (entry / "environ").write_bytes(b"SHPOOL_SESSION_NAME=main\0")
            with mock.patch.object(
                process_inventory, "_proc_stat", return_value=(123, 10, 1000)
            ):
                scanned = process_inventory.scan_process_table(
                    proc_root,
                    64,
                    proc_stat=process_inventory._proc_stat,
                    proc_environ=process_inventory._proc_environ,
                )
        self.assertFalse(scanned[123]["environ_unreadable"])
        table = {
            10: {"pid": 10, "ppid": 1, "cmdline": ["/usr/bin/shpool", "daemon"]},
            123: scanned[123],
        }
        _, diagnostics = process_inventory.shpool_roots(
            ["other"],
            table,
            is_shpool_daemon=process_inventory._is_shpool_daemon,
        )
        self.assertFalse(
            any("unreadable environment" in line for line in diagnostics["other"]),
            diagnostics,
        )

    def test_darwin_process_scan_retains_exact_provider_account_environment(
        self,
    ) -> None:
        info = inventory_core._DarwinBsdInfo()
        info.pbi_pid = 123
        info.pbi_ppid = 10
        info.pbi_start_tvsec = 1_752_000_000
        info.pbi_start_tvusec = 123_456
        info.pbi_name = b"codex"
        values = [
            b"/opt/homebrew/bin/codex",
            b"",
            b"/opt/homebrew/bin/codex",
            b"CLAUDE_CONFIG_DIR=/profiles/claude-a",
            b"CODEX_HOME=/profiles/codex-a",
            b"SESSION_KIT_ACCOUNT_ALIAS=paid-a",
            b"SESSION_KIT_ACCOUNT_CAPABLE=1",
        ]
        payload = struct.pack("=i", 1) + b"\0".join(values) + b"\0"
        scanned = inventory_core.scan_darwin_process_table(
            4,
            pids=[123],
            bsd_reader=lambda _pid: info,
            args_reader=lambda _pid: payload,
        )
        self.assertEqual("/profiles/claude-a", scanned[123]["claude_config_dir"])
        self.assertEqual("/profiles/codex-a", scanned[123]["codex_home"])
        self.assertEqual("paid-a", scanned[123]["account_alias"])
        self.assertEqual("1", scanned[123]["account_capable"])

    def test_account_evidence_requires_an_exact_provider_process(self) -> None:
        fixture = list(inventory_fixture(2))
        provider_pids = {
            row["comm"]: pid
            for pid, row in fixture[2].items()
            if row["comm"] in {"claude", "codex"}
        }
        fixture[2][provider_pids["claude"]].update(
            {"account_alias": "  claude_a  ", "account_capable": "1"}
        )
        fixture[2][provider_pids["codex"]].update(
            {"account_alias": "codex-paid", "account_capable": "yes"}
        )
        exact = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        rows = {row["provider"]: row for row in exact["sessions"]}
        self.assertEqual("claude_a", rows["claude"]["account_alias"])
        self.assertTrue(rows["claude"]["account_switch_capable"])
        self.assertEqual("codex-paid", rows["codex"]["account_alias"])
        self.assertFalse(rows["codex"]["account_switch_capable"])

        unresolved_fixture = list(inventory_fixture(1, providers=("claude",)))
        unresolved_fixture[2][2001].update(
            {"account_alias": "no-leak", "account_capable": "1"}
        )
        unresolved_fixture[1] = []
        unresolved = inventory_core.build_inventory(
            *unresolved_fixture, now=1_800_000_000
        )["sessions"][0]
        self.assertEqual("unknown", unresolved["provider"])
        self.assertNotIn("account_alias", unresolved)
        self.assertFalse(unresolved["account_switch_capable"])

    def test_codex_process_index_uses_each_exact_live_profile(self) -> None:
        default_home = REPO / ".codex-default-fixture"
        custom_home = REPO / ".codex-custom-fixture"
        proc_root = REPO / ".proc-fixture"
        table = {
            20: {
                **process(20, 10, "codex"),
                "codex_thread_id": uuid_for(20),
            },
            21: {
                **process(21, 10, "codex"),
                "codex_home": os.fspath(custom_home),
                "codex_thread_id": uuid_for(21),
            },
        }
        with (
            mock.patch.object(
                inventory_core, "_runtime_platform", return_value="linux"
            ),
            mock.patch.object(
                inventory_core, "codex_open_rollouts", return_value=[]
            ) as open_rollouts,
        ):
            inventory_core.index_codex_processes(table, proc_root, default_home)
        open_rollouts.assert_has_calls(
            [
                mock.call(20, proc_root, default_home, table[20]),
                mock.call(21, proc_root, custom_home, table[21]),
            ]
        )
        with (
            mock.patch.object(
                inventory_core, "_runtime_platform", return_value="darwin"
            ),
            mock.patch.object(
                inventory_core, "codex_rollout_by_uuid", return_value=[]
            ) as rollout_by_uuid,
        ):
            inventory_core.index_codex_processes(table, proc_root, default_home)
        rollout_by_uuid.assert_has_calls(
            [
                mock.call(default_home, uuid_for(20)),
                mock.call(custom_home, uuid_for(21)),
            ]
        )

    def test_claude_enrichment_reads_the_exact_custom_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".claude-profile-", dir=REPO) as raw:
            custom_root = Path(raw)
            exact_uuid = uuid_for(25)
            transcript = custom_root / "projects" / "fixture" / f"{exact_uuid}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps(
                    {
                        "type": "ai-title",
                        "sessionId": exact_uuid,
                        "aiTitle": "Custom profile title",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            sessions = custom_root / "sessions"
            sessions.mkdir()
            (sessions / "225.json").write_text(
                json.dumps({"sessionId": exact_uuid, "nameSource": "derived"}),
                encoding="utf-8",
            )
            payload = [
                {
                    "pid": 225,
                    "sessionId": exact_uuid,
                    "_session_kit_claude_config_dir": os.fspath(custom_root),
                }
            ]
            enriched = claude_inventory._enrich_claude_payload(
                payload,
                environ=os.environ,
                home_factory=Path.home,
                palette=inventory_core.CLAUDE_SESSION_COLORS,
                transcript_signals=inventory_core.read_claude_transcript_signals,
            )
        self.assertEqual("Custom profile title", enriched[0]["aiTitle"])
        self.assertEqual("derived", enriched[0]["nameSource"])

    def test_claude_enrichment_ignores_unreadable_or_symlinked_pid_record(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".claude-profile-", dir=REPO) as raw:
            custom_root = Path(raw)
            sessions = custom_root / "sessions"
            sessions.mkdir()
            exact_uuid = uuid_for(26)
            payload = [
                {
                    "pid": 226,
                    "sessionId": exact_uuid,
                    "name": "Ordinary Native Name",
                    "_session_kit_claude_config_dir": os.fspath(custom_root),
                }
            ]
            record = sessions / "226.json"
            record.write_text("not json", encoding="utf-8")
            unreadable = claude_inventory._enrich_claude_payload(
                [dict(payload[0])],
                environ=os.environ,
                home_factory=Path.home,
                palette=inventory_core.CLAUDE_SESSION_COLORS,
                transcript_signals=inventory_core.read_claude_transcript_signals,
            )
            self.assertNotIn("nameSource", unreadable[0])

            external = custom_root / "external.json"
            external.write_text(
                json.dumps({"sessionId": exact_uuid, "nameSource": "derived"}),
                encoding="utf-8",
            )
            record.unlink()
            record.symlink_to(external)
            symlinked = claude_inventory._enrich_claude_payload(
                [dict(payload[0])],
                environ=os.environ,
                home_factory=Path.home,
                palette=inventory_core.CLAUDE_SESSION_COLORS,
                transcript_signals=inventory_core.read_claude_transcript_signals,
            )
            self.assertNotIn("nameSource", symlinked[0])

    def test_live_collection_keeps_claude_and_codex_profiles_isolated(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".profiles-", dir=REPO) as raw:
            base = Path(raw)
            account_home = base / "home"
            custom_claude = base / "claude-custom"
            default_codex = base / "codex-default"
            custom_codex = base / "codex-custom"
            account_home.mkdir()
            custom_claude.mkdir()
            default_codex.mkdir()
            custom_codex.mkdir()
            fixture = list(
                inventory_fixture(4, providers=("claude", "claude", "codex", "codex"))
            )
            claude_pids = sorted(
                pid for pid, row in fixture[2].items() if row["comm"] == "claude"
            )
            codex_pids = sorted(
                pid for pid, row in fixture[2].items() if row["comm"] == "codex"
            )
            fixture[2][claude_pids[0]].update(
                {"account_alias": "cld-main", "account_capable": "1"}
            )
            fixture[2][claude_pids[1]].update(
                {
                    "claude_config_dir": os.fspath(custom_claude),
                    "account_alias": "cld-alt",
                    "account_capable": "0",
                }
            )
            fixture[2][codex_pids[0]].update(
                {"account_alias": "cdx-main", "account_capable": "1"}
            )
            fixture[2][codex_pids[1]].update(
                {
                    "codex_home": os.fspath(custom_codex),
                    "account_alias": "cdx-alt",
                    "account_capable": "",
                }
            )
            for home, exact_uuid, title in (
                (default_codex, uuid_for(3), "Default Codex title"),
                (custom_codex, uuid_for(4), "Custom Codex title"),
            ):
                connection = sqlite3.connect(home / "state_5.sqlite")
                connection.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, cwd TEXT)"
                )
                connection.execute(
                    "INSERT INTO threads VALUES (?, ?, ?)",
                    (
                        exact_uuid,
                        title,
                        f"/srv/project-{3 if home == default_codex else 4}",
                    ),
                )
                connection.commit()
                connection.close()

            commands: list[tuple[str, ...]] = []

            def command_json(**kwargs: object) -> object:
                command = tuple(kwargs["default_command"])
                commands.append(command)
                if "shpool" in Path(command[0]).name:
                    return fixture[0]
                if command == ("claude", "agents", "--json"):
                    return fixture[1]
                if command[:1] == ("env",):
                    return fixture[1]
                self.fail(f"unexpected command: {command!r}")

            with (
                mock.patch.dict(
                    os.environ,
                    {"HOME": os.fspath(account_home), "CLAUDE_CONFIG_DIR": ""},
                    clear=False,
                ),
                mock.patch.object(
                    inventory_core, "_command_json", side_effect=command_json
                ),
                mock.patch.object(
                    inventory_core, "scan_process_table", return_value=fixture[2]
                ),
                mock.patch.object(
                    inventory_core,
                    "_payload_daemon_identity",
                    return_value={"pid": 10, "process_start_ticks": 100},
                ),
                mock.patch.object(
                    inventory_core,
                    "_codex_paths",
                    return_value=(default_codex, default_codex / "state_5.sqlite"),
                ),
                mock.patch.object(
                    inventory_core, "index_codex_processes", return_value=fixture[3]
                ),
                mock.patch.object(
                    inventory_core, "recent_output_times", return_value={}
                ),
            ):
                collected = inventory_core.collect_live(fixture[5])

        rows = {row["shpool_id"]: row for row in collected["sessions"]}
        self.assertEqual("Claude task 1", rows["main"]["native_title"])
        self.assertEqual("Claude task 2", rows["main2"]["native_title"])
        self.assertEqual("Default Codex title", rows["main3"]["native_title"])
        self.assertEqual("Custom Codex title", rows["main4"]["native_title"])
        self.assertEqual("cld-main", rows["main"]["account_alias"])
        self.assertEqual("cld-alt", rows["main2"]["account_alias"])
        self.assertEqual("cdx-main", rows["main3"]["account_alias"])
        self.assertEqual("cdx-alt", rows["main4"]["account_alias"])
        self.assertTrue(rows["main"]["account_switch_capable"])
        self.assertFalse(rows["main2"]["account_switch_capable"])
        self.assertTrue(rows["main3"]["account_switch_capable"])
        self.assertFalse(rows["main4"]["account_switch_capable"])
        self.assertIn(
            (
                "env",
                f"CLAUDE_CONFIG_DIR={custom_claude}",
                "claude",
                "agents",
                "--json",
            ),
            commands,
        )
        self.assertTrue(collected["_complete"])

    def test_default_proc_bound_covers_peak_and_remains_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".config-bound-", dir=REPO) as raw:
            missing = Path(raw) / "missing.json"
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(missing)},
                clear=False,
            ):
                default = inventory_core.load_config()
            self.assertEqual(16384, default["max_proc_nodes"])
            missing.write_text(
                json.dumps({"schema_version": 1, "max_proc_nodes": 100001}),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(missing)},
                clear=False,
            ):
                invalid = inventory_core.load_config()
            self.assertEqual(16384, invalid["max_proc_nodes"])
        partial_fixture = list(inventory_fixture(1))
        partial_fixture[5] = {"aliases": {}, "max_proc_depth": 32}
        with mock.patch.object(
            inventory_core,
            "descendants",
            wraps=inventory_core.descendants,
        ) as descend:
            inventory_core.build_inventory(*partial_fixture, now=1_800_000_000)
        self.assertEqual(
            inventory_core.DEFAULT_MAX_PROC_NODES,
            descend.call_args.kwargs["max_nodes"],
        )

        class ScanReached(RuntimeError):
            pass

        with (
            mock.patch.object(
                inventory_core,
                "_command_json",
                return_value={"sessions": []},
            ),
            mock.patch.object(
                inventory_core,
                "scan_process_table",
                side_effect=ScanReached,
            ) as scan,
        ):
            with self.assertRaises(ScanReached):
                inventory_core.collect_live({})
        self.assertEqual(
            inventory_core.DEFAULT_MAX_PROC_NODES,
            scan.call_args.args[1],
        )

    def test_live_list_is_bracketed_and_revalidated_before_build(self) -> None:
        fixture = inventory_fixture(0)
        table = {
            10: process(
                10,
                1,
                "shpool",
                cmdline=["/usr/bin/shpool", "daemon"],
            )
        }
        events: list[str] = []

        def scan(*args, **kwargs):
            events.append("scan")
            return table

        def command_json(**kwargs):
            command = tuple(kwargs["default_command"])
            if "shpool" in Path(command[0]).name:
                events.append("list")
                return fixture[0]
            events.append("provider")
            return fixture[1]

        with (
            mock.patch.object(
                inventory_core, "_command_json", side_effect=command_json
            ),
            mock.patch.object(inventory_core, "scan_process_table", side_effect=scan),
            mock.patch.object(
                inventory_core,
                "_codex_paths",
                return_value=(REPO, REPO / "missing.sqlite"),
            ),
            mock.patch.object(
                inventory_core, "read_codex_session_index", return_value={}
            ),
            mock.patch.object(inventory_core, "read_codex_db", return_value=({}, {})),
            mock.patch.object(inventory_core, "recent_output_times", return_value={}),
        ):
            inventory_core.collect_live(fixture[5])

        self.assertEqual(["scan", "list", "scan", "provider", "scan"], events)

    def test_live_payload_binding_requires_three_matching_identities(self) -> None:
        fixture = inventory_fixture(1, providers=("claude",))
        operator = {"pid": 10, "process_start_ticks": 100}
        foreign = {"pid": 20, "process_start_ticks": 200}

        def command_json(**kwargs):
            command = tuple(kwargs["default_command"])
            return fixture[0] if "shpool" in Path(command[0]).name else fixture[1]

        for label, observations, expected_provider in (
            ("stable", (operator, operator, operator), "claude"),
            ("changed-around-list", (operator, foreign, operator), "unknown"),
            ("changed-before-publish", (operator, operator, foreign), "unknown"),
            ("unknown-before-publish", (operator, operator, None), "unknown"),
        ):
            with self.subTest(case=label):
                with (
                    mock.patch.object(
                        inventory_core, "_command_json", side_effect=command_json
                    ),
                    mock.patch.object(
                        inventory_core, "scan_process_table", return_value=fixture[2]
                    ),
                    mock.patch.object(
                        inventory_core,
                        "_payload_daemon_identity",
                        side_effect=observations,
                    ) as identity,
                    mock.patch.object(
                        inventory_core,
                        "_codex_paths",
                        return_value=(REPO, REPO / "missing.sqlite"),
                    ),
                    mock.patch.object(
                        inventory_core, "read_codex_session_index", return_value={}
                    ),
                    mock.patch.object(
                        inventory_core, "read_codex_db", return_value=({}, {})
                    ),
                    mock.patch.object(
                        inventory_core, "recent_output_times", return_value={}
                    ),
                ):
                    document = inventory_core.collect_live(fixture[5])
                self.assertEqual(3, identity.call_count)
                row = document["sessions"][0]
                self.assertEqual(expected_provider, row["provider"])
                if expected_provider == "unknown":
                    self.assertIsNone(document["daemon_generation"])
                    self.assertIsNone(row["shpool_shell"])
                    self.assertIs(False, row["mutation_allowed"])
                    self.assertEqual("Unresolved provider session", row["title"])
                else:
                    self.assertEqual(operator, document["daemon_generation"])
                    self.assertIs(True, row["mutation_allowed"])

    def test_linux_scan_retains_only_unreadable_shpool_argv_candidate(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".proc-argv-", dir=REPO) as raw:
            root = Path(raw)
            for pid, comm in ((10, "shpool"), (20, "bash")):
                entry = root / str(pid)
                entry.mkdir()
                (entry / "stat").write_text("fixture\n", encoding="utf-8")
                (entry / "comm").write_text(f"{comm}\n", encoding="utf-8")
                (entry / "cmdline").mkdir()

            def proc_stat(path: Path) -> tuple[int, int, int]:
                return int(path.parent.name), 1, int(path.parent.name) * 10

            with mock.patch.object(
                vars(inventory_core)["_processes"],
                "_readable_by_us",
                return_value=False,
            ):
                table = vars(inventory_core)["_processes"].scan_process_table(
                    root,
                    10,
                    proc_stat=proc_stat,
                    proc_environ=lambda path: {},
                )
        self.assertEqual([10, 20], sorted(table))
        self.assertEqual("shpool", table[10]["comm"])
        self.assertEqual([], table[10]["cmdline"])
        self.assertIs(False, table[10]["args_available"])
        self.assertIs(True, table[10]["argv_unreadable"])
        self.assertEqual("", table[20]["comm"])
        self.assertEqual(-1, table[20]["start_ticks"])

    def test_duplicate_cwd_does_not_conflate_claude_and_codex(self) -> None:
        fixture = list(inventory_fixture(2))
        shared = "/srv/shared"
        fixture[1][0]["cwd"] = shared
        fixture[2][2001]["cwd"] = shared
        fixture[2][1001]["cwd"] = shared
        fixture[2][2002]["cwd"] = shared
        fixture[2][1002]["cwd"] = shared
        fixture[4][0][uuid_for(2)]["cwd"] = shared
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        by_provider = {row["provider"]: row for row in result["sessions"]}
        self.assertEqual(uuid_for(1), by_provider["claude"]["identity"]["uuid"])
        self.assertEqual(uuid_for(2), by_provider["codex"]["identity"]["uuid"])
        self.assertEqual(
            ["claude", "codex"], sorted(row["provider"] for row in result["sessions"])
        )

    def test_subagents_are_nested_and_not_promoted_to_sessions(self) -> None:
        fixture = list(inventory_fixture(1, providers=("claude",)))
        parent_uuid = uuid_for(1)
        fixture[2][3001] = process(
            3001,
            2001,
            "claude",
            cmdline=[
                "/usr/bin/claude",
                "--parent-session-id",
                parent_uuid,
                "--agent-name",
                "Verifier",
            ],
        )
        fixture[1].append(
            {
                "pid": 3001,
                "sessionId": uuid_for(9001),
                "cwd": "/srv/project-1",
                "kind": "subagent",
                "name": "Verifier",
                "status": "busy",
            }
        )
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        self.assertEqual(1, len(result["sessions"]))
        self.assertEqual("claude", result["sessions"][0]["provider"])
        self.assertEqual(parent_uuid, result["sessions"][0]["identity"]["uuid"])
        self.assertEqual(
            ["Verifier"], [x["title"] for x in result["sessions"][0]["subagents"]]
        )
        self.assertEqual([], result["outside_agents"])

    def test_separately_managed_claude_children_record_the_exact_parent(self) -> None:
        fixture = list(inventory_fixture(3, providers=("claude",)))
        parent_uuid = uuid_for(1)
        for index in (2, 3):
            child_pid = 2000 + index
            fixture[2][child_pid]["cmdline"] = [
                "/usr/bin/claude",
                "--session-id",
                uuid_for(index),
                "--parent-session-id",
                parent_uuid,
                "--agent-name",
                f"Builder {index}",
            ]

        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)

        parent_row = next(
            row for row in result["sessions"] if row["identity"]["uuid"] == parent_uuid
        )
        self.assertNotIn("is_subagent", parent_row)
        children = [row for row in result["sessions"] if row is not parent_row]
        self.assertEqual(2, len(children))
        for child in children:
            self.assertTrue(child["is_subagent"])
            self.assertEqual(
                {"provider": "claude", "uuid": parent_uuid},
                {
                    "provider": child["parent_session"]["provider"],
                    "uuid": child["parent_session"]["uuid"],
                },
            )

    def test_every_claude_parent_flag_shape_proves_the_process_is_a_child(self) -> None:
        parent_uuid = uuid_for(1)
        cases = {
            "truncated separate value": ["--parent-session-id", parent_uuid[:-4]],
            "truncated joined value": [f"--parent-session-id={parent_uuid[:-4]}"],
            "flag without a value": ["--parent-session-id"],
            "valid joined value": [f"--parent-session-id={parent_uuid}"],
        }
        for label, parent_args in cases.items():
            with self.subTest(label=label):
                fixture = list(inventory_fixture(2, providers=("claude",)))
                fixture[2][2002]["cmdline"] = [
                    "/usr/bin/claude",
                    "--session-id",
                    uuid_for(2),
                    *parent_args,
                ]

                result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
                by_uuid = {row["identity"]["uuid"]: row for row in result["sessions"]}
                child = by_uuid[uuid_for(2)]
                people, machines, orphans = classify_top_level_sessions(
                    result["sessions"]
                )

                self.assertTrue(child.get("is_subagent"))
                self.assertNotIn(child, people)
                self.assertNotIn(child, machines)
                if label == "valid joined value":
                    self.assertEqual(parent_uuid, child["parent_session"]["uuid"])
                    self.assertEqual([], orphans)
                    self.assertEqual(1, len(people))
                    self.assertEqual(1, people[0]["active_subagent_count"])
                else:
                    self.assertNotIn("parent_session", child)
                    self.assertEqual(
                        [uuid_for(2)], [row["identity"]["uuid"] for row in orphans]
                    )

    def test_codex_spawn_edge_and_headless_exec_classify_child_rows(self) -> None:
        fixture = list(inventory_fixture(3, providers=("codex",)))
        parent, child = uuid_for(1), uuid_for(2)
        fixture[4] = (
            fixture[4][0],
            {
                parent: [
                    {
                        "provider": "codex",
                        "uuid": child,
                        "title": "Builder",
                        "status": "working",
                    }
                ]
            },
        )
        fixture[2][2003]["cmdline"] = ["/usr/bin/codex", "exec", "build"]

        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        by_uuid = {row["identity"]["uuid"]: row for row in result["sessions"]}

        self.assertEqual(parent, by_uuid[child]["parent_session"]["uuid"])
        self.assertTrue(by_uuid[child]["is_subagent"])
        self.assertTrue(by_uuid[uuid_for(3)]["is_subagent"])
        self.assertNotIn("parent_session", by_uuid[uuid_for(3)])

    def test_codex_exec_is_headless_only_at_the_real_subcommand_position(
        self,
    ) -> None:
        cases = (
            ("profile value named exec", ["codex", "--profile", "exec"], False),
            ("direct exec", ["codex", "exec", "-s", "workspace-write"], True),
            (
                "leading sandbox and directory",
                ["codex", "-s", "workspace-write", "-C", "/tmp/x", "exec"],
                True,
            ),
            (
                "profile value before exec",
                ["codex", "--profile", "exec", "exec"],
                True,
            ),
            ("unknown option", ["codex", "--unknown-flag", "exec"], False),
            ("joined profile value", ["codex", "--profile=exec"], False),
            (
                "variadic image values",
                ["codex", "--image", "image.png", "exec"],
                False,
            ),
            (
                "malformed profile value",
                ["codex", "--profile", "--no-alt-screen", "exec"],
                False,
            ),
        )
        for label, argv, expected_headless in cases:
            with self.subTest(label=label):
                fixture = list(inventory_fixture(1, providers=("codex",)))
                fixture[2][2001]["cmdline"] = argv

                result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
                session = result["sessions"][0]
                people, machines, orphans = classify_top_level_sessions(
                    result["sessions"]
                )

                self.assertEqual(expected_headless, session.get("is_subagent", False))
                self.assertEqual([] if expected_headless else [session], people)
                self.assertEqual([], machines)
                self.assertEqual([session] if expected_headless else [], orphans)

    def test_machine_driven_codex_modes_never_render_as_a_person(self) -> None:
        """Tour finding X16: session 87 was a live counterexample.

        A Codex nobody converses with -- a headless run, a non-interactive
        review, or a server another program drives over a socket -- must fold
        behind the machine count, not sit in the list as a person's window.
        The wrapper cases close X13 (argv[0]=node hid the grammar entirely).
        app-server stays VISIBLE by argv: it is the kit's own plumbing for
        every managed Codex session, human windows included -- classifying it
        machine hid two real sessions (X17). Machine-vs-human for app-server
        needs driver evidence, a separate fix.
        """
        vendor = (
            "/home/x/.npm-global/lib/node_modules/@openai/codex/node_modules/"
            "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
        )
        cases = (
            (
                "app-server is the kit's own plumbing, never machine by argv",
                [
                    vendor,
                    "-c",
                    "check_for_update_on_startup=false",
                    "app-server",
                    "--listen",
                    "unix:///run/x/app.sock",
                ],
                False,
            ),
            (
                "wrapper + app-server stays visible too",
                [
                    "node",
                    "/home/x/.npm-global/bin/codex",
                    "-c",
                    "k=v",
                    "app-server",
                    "--listen",
                    "unix:///run/x/app.sock",
                ],
                False,
            ),
            (
                "npm wrapper + exec (X13 closed)",
                ["node", "/home/x/.npm-global/bin/codex", "exec", "build"],
                True,
            ),
            ("non-interactive review", ["codex", "review"], True),
            ("mcp server mode", ["codex", "mcp-server"], True),
            (
                "wrapper, no subcommand: a person's window",
                ["node", "/home/x/.npm-global/bin/codex"],
                False,
            ),
            (
                "wrapper + profile named exec stays a person",
                ["node", "/home/x/.npm-global/bin/codex", "--profile", "exec"],
                False,
            ),
            (
                "profile value named app-server stays a person",
                ["codex", "--profile", "app-server"],
                False,
            ),
            (
                "wrapper of some other script is not resolved",
                ["node", "/home/x/bin/codex-lookalike", "exec"],
                False,
            ),
            (
                "unproven management verb errs visible",
                ["codex", "remote-control", "enable"],
                False,
            ),
        )
        for label, argv, expected_machine in cases:
            with self.subTest(label=label):
                fixture = list(inventory_fixture(1, providers=("codex",)))
                fixture[2][2001]["cmdline"] = argv

                result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
                session = result["sessions"][0]
                people, machines, orphans = classify_top_level_sessions(
                    result["sessions"]
                )

                self.assertEqual(expected_machine, session.get("is_subagent", False))
                self.assertEqual([] if expected_machine else [session], people)
                self.assertEqual([], machines)
                self.assertEqual([session] if expected_machine else [], orphans)

    def app_server_fixture(
        self,
        *,
        window: bool = True,
        broker: bool = True,
        socket: str = "/run/x/s1/app.sock",
        listen: str | None = None,
        server_ticks: int | None = None,
        wrapper: bool = False,
    ) -> list:
        """One managed Codex session in App Server shape, wired as asked.

        The live estate builds every managed Codex session this way: the login
        shell starts the server, the coordination broker on the same socket,
        and -- for a person -- the window resumed against it. Sessions 68/80
        and the machine-driven 87 differ in exactly one of those processes.
        """
        vendor = (
            "/home/x/.npm-global/lib/node_modules/@openai/codex/node_modules/"
            "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
        )
        fixture = list(inventory_fixture(1, providers=("codex",)))
        fixture[2][2001]["cmdline"] = [
            *(["node", "/home/x/.npm-global/bin/codex"] if wrapper else [vendor]),
            "-c",
            "check_for_update_on_startup=false",
            "app-server",
            "--listen",
            f"unix://{socket}" if listen is None else listen,
        ]
        if server_ticks is not None:
            fixture[2][2001]["start_ticks"] = server_ticks
        if broker:
            fixture[2][2100] = process(
                2100,
                1001,
                "python3",
                cmdline=[
                    "python3",
                    "/srv/x/.claude/coordination/provider_broker.py",
                    "--socket",
                    socket,
                    "--server-pid",
                    "2001",
                ],
            )
        if window:
            fixture[2][2101] = process(
                2101,
                1001,
                "codex",
                cmdline=[
                    vendor,
                    "--remote",
                    f"unix://{socket}",
                    "--no-alt-screen",
                    "resume",
                    uuid_for(1),
                ],
            )
        return fixture

    def app_server_lists(self, fixture: list) -> tuple[dict, tuple]:
        """Build the row, stamp origins from an empty store, then project it."""
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        with tempfile.TemporaryDirectory(prefix=".origins-") as directory:
            origins_state.apply_session_origins(result, state_dir=Path(directory))
        return result["sessions"][0], classify_top_level_sessions(result["sessions"])

    def test_app_server_driven_by_a_program_is_never_a_persons_row(self) -> None:
        """Tour finding X16: session 87, the Matrix lead's Codex worker.

        Its argv is byte-for-byte the argv of the operator's own sessions 68 and 80 --
        every managed Codex session runs an App Server -- so the answer is in
        who holds the socket. A broker holds 87's and no window does.
        """
        row, (people, machines, orphans) = self.app_server_lists(
            self.app_server_fixture(window=False)
        )

        self.assertEqual([], people)
        self.assertEqual([row], machines)
        self.assertEqual("machine", row["origin"])
        self.assertTrue(row["machine_driven"])
        # Not a child of a session nothing can name: an unnameable child is
        # projected into the orphan class, which no list and no count shows.
        self.assertEqual([], orphans)
        self.assertNotIn("is_subagent", row)

    def test_snapshot_binds_a_window_sighting_to_older_receipts(self) -> None:
        """The orchestration captures generations before process collection."""
        with tempfile.TemporaryDirectory(
            prefix=".receipt-generation-", dir=REPO
        ) as raw:
            state = Path(raw)
            state.chmod(0o700)
            markers = state / "provider-bounce"
            markers.mkdir(mode=0o700)
            pending = markers / "main"
            receipt = markers / (
                f"main{origins_state.TAKEN_GENERATION_SEPARATOR}newgeneration"
            )
            live = inventory_core.build_inventory(
                *self.app_server_fixture(window=True), now=1_800_000_000
            )
            calls = 0

            def collect(_settings):
                nonlocal calls
                calls += 1
                if calls == 1:
                    pending.write_text(f"{uuid_for(1)}\n", encoding="utf-8")
                    os.replace(pending, receipt)
                return copy.deepcopy(live)

            config = {
                "state_dir": state,
                "aliases": {},
                "max_proc_nodes": 8192,
                "max_proc_depth": 32,
            }
            with (
                mock.patch.object(inventory_core, "collect_live", side_effect=collect),
                mock.patch.object(inventory_core, "_boot_id", return_value="boot-a"),
            ):
                stale = inventory_core.snapshot(config=config)
                self.assertTrue(receipt.is_file())
                fresh = inventory_core.snapshot(config=config)

            self.assertEqual(origins_state.HUMAN, stale["sessions"][0]["origin"])
            self.assertEqual(origins_state.HUMAN, fresh["sessions"][0]["origin"])
            self.assertFalse(receipt.exists())

    def test_stale_snapshot_preserves_a_newer_lifecycle_keep_record(self) -> None:
        """A publishing pass cannot erase a keep written after its reading."""
        threshold = lifecycle_state.EXACT_RECOVERY_RETENTION_SECONDS
        clock_cases = {
            "older-than-threshold": time.time() - threshold - 1,
            "future-after-clock-move": time.time() + threshold + 1,
        }
        for label, record_mtime in clock_cases.items():
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory(
                    prefix=".lifecycle-stale-snapshot-", dir=REPO
                ) as raw,
            ):
                state = Path(raw)
                state.chmod(0o700)
                newer_session = "s20260815-170000-19"
                boot_id = "11111111-2222-3333-4444-555555555555"
                live = inventory_core.build_inventory(
                    *inventory_fixture(1), now=1_800_000_000
                )
                config = {
                    "state_dir": state,
                    "aliases": {},
                    "max_proc_nodes": 8192,
                    "max_proc_depth": 32,
                }

                def keep_after_process_reading() -> str:
                    lifecycle_state.record_provider_exit(
                        state,
                        session_id=newer_session,
                        boot_id=boot_id,
                        shell_pid=123,
                        shell_start_ticks=456,
                        provider="codex",
                        exit_code=0,
                        input_tracking=True,
                        now_monotonic_ns=100,
                    )
                    lifecycle_state.update_state(
                        state,
                        session_id=newer_session,
                        boot_id=boot_id,
                        shell_pid=123,
                        shell_start_ticks=456,
                        event="keep",
                        keep=True,
                    )
                    path = lifecycle_state.lifecycle_path(state, newer_session)
                    assert path is not None
                    os.utime(path, (record_mtime, record_mtime))
                    return boot_id

                with (
                    mock.patch.object(
                        inventory_core, "collect_live", return_value=live
                    ),
                    mock.patch.object(
                        inventory_core,
                        "_boot_id",
                        side_effect=keep_after_process_reading,
                    ),
                ):
                    inventory_core.snapshot(config=config)

                self.assertEqual(
                    {newer_session},
                    lifecycle_state.cleanup_protected(state, [newer_session]),
                )

    def test_the_npm_wrapper_does_not_hide_the_app_server_from_the_driver(
        self,
    ) -> None:
        """X13 and X16 meet here: 87 was launched through the npm wrapper.

        With argv[0] = node, subcommand parsing never reached `app-server`,
        so the driver was never asked about. A path that merely contains the
        word codex still resolves nothing.
        """
        row, (people, machines, _) = self.app_server_lists(
            self.app_server_fixture(window=False, wrapper=True)
        )
        self.assertEqual([], people)
        self.assertEqual([row], machines)
        self.assertTrue(row["machine_driven"])

        lookalike = self.app_server_fixture(window=False, wrapper=True)
        lookalike[2][2001]["cmdline"][1] = "/home/x/bin/codex-lookalike"
        row, (people, machines, _) = self.app_server_lists(lookalike)
        self.assertNotIn("machine_driven", row)
        self.assertEqual([row], people)
        self.assertEqual([], machines)

    def test_app_server_with_a_window_stays_a_visible_person(self) -> None:
        """X17: this shape is sessions 68 and 80, and it must never move.

        A previous fix read `app-server` as machine by argv and hid both of
        the operator's real sessions for four minutes. This case passes at that commit
        too -- it has to, or the regression it guards is not being guarded.
        """
        row, (people, machines, orphans) = self.app_server_lists(
            self.app_server_fixture()
        )

        self.assertNotIn("machine_driven", row)
        self.assertEqual("human", row["origin"])
        self.assertEqual([row], people)
        self.assertEqual([], machines)
        self.assertEqual([], orphans)

    def test_unreadable_app_server_evidence_keeps_the_row_visible(self) -> None:
        """Failing visible: every reading short of proof is a person's row."""
        hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        cases = {
            "no client on the socket at all": self.app_server_fixture(
                window=False, broker=False
            ),
            "a broker on some other socket": self.app_server_fixture(
                window=False,
                socket="/run/x/s1/app.sock",
                listen="unix:///run/x/s2/app.sock",
            ),
            "no listen endpoint to compare": self.app_server_fixture(
                window=False, listen=""
            ),
            "a server still booting its window": self.app_server_fixture(
                window=False, server_ticks=int(uptime * hz)
            ),
        }
        for label, fixture in cases.items():
            with self.subTest(label=label):
                row, (people, machines, orphans) = self.app_server_lists(fixture)

                self.assertNotIn("machine_driven", row)
                self.assertEqual([row], people)
                self.assertEqual([], machines)
                self.assertEqual([], orphans)

    def test_a_window_the_reader_could_not_read_keeps_the_row_visible(self) -> None:
        """The other unreadable case: the window is THERE, and unread.

        "A program drives this" is reached by NOT finding a window, so it is
        only ever as strong as the reading of the tree. A process whose argv
        the scan could not take -- denied, or gone between the listing and the
        read -- leaves a tree that looks exactly like a tree with no window in
        it. A person is sitting in that session, and the previous reading
        filed it behind the machine count.
        """
        fixture = self.app_server_fixture()
        fixture[2][2101] = {
            **fixture[2][2101],
            "cmdline": [],
            "comm": "",
            "argv_unreadable": True,
        }

        row, (people, machines, orphans) = self.app_server_lists(fixture)

        self.assertNotIn("machine_driven", row)
        self.assertEqual([row], people)
        self.assertEqual([], machines)
        self.assertEqual([], orphans)

    def test_a_window_lost_before_its_parent_was_read_keeps_the_row_visible(
        self,
    ) -> None:
        """A hole anywhere on the machine is a hole in every tree.

        When the FIRST stat read fails there is no parent, so the lost process
        cannot be placed in any tree and no slot can hold it. It is still a
        process, and it may be the very window whose absence is about to be
        read as proof that a program drives this session. The scan is driven
        end to end here, from a /proc where the window exits between the
        listing and the first stat, so what reaches the classifier is the real
        table a real dropout produces.
        """
        with tempfile.TemporaryDirectory(prefix=".proc-drop-", dir=REPO) as raw:
            proc_root = Path(raw)
            for name in ("1001", "2001", "2100", "2101"):
                entry = proc_root / name
                entry.mkdir()
                (entry / "stat").write_text("fixture\n", encoding="utf-8")
                (entry / "comm").write_text("codex\n", encoding="utf-8")
                (entry / "environ").write_bytes(b"")
            (proc_root / "1001" / "cmdline").write_bytes(b"bash\0-i\0")
            (proc_root / "2001" / "cmdline").write_bytes(
                b"codex\0app-server\0--listen\0unix:///run/x/s1/app.sock\0"
            )
            (proc_root / "2100" / "cmdline").write_bytes(
                b"python3\0broker.py\0--socket\0/run/x/s1/app.sock\0"
            )
            # The window, on the same socket, and about to vanish.
            (proc_root / "2101" / "cmdline").write_bytes(
                b"codex\0--remote\0unix:///run/x/s1/app.sock\0"
            )
            with mock.patch.object(
                process_inventory,
                "_proc_stat",
                side_effect=[
                    (1001, 1, 900),
                    (1001, 1, 900),
                    (2001, 1001, 1000),
                    (2001, 1001, 1000),
                    (2100, 1001, 1000),
                    (2100, 1001, 1000),
                    OSError(errno.ESRCH, "the window exited mid-scan"),
                ],
            ):
                scanned = process_inventory.scan_process_table(
                    proc_root,
                    64,
                    proc_stat=process_inventory._proc_stat,
                    proc_environ=process_inventory._proc_environ,
                )

        # The tree is walked from the table exactly as collection walks it, so
        # the lost pid is not merely missing from the table -- it is missing
        # from the tree as well, and there is nothing left in the tree to
        # notice it by.
        tree = process_inventory.descendants(
            1001,
            process_inventory._children_index(scanned),
            max_nodes=64,
            max_depth=8,
        )
        self.assertNotIn(2101, tree)

        verdict = collector_inventory._codex_app_server_driver(
            2001,
            ["codex", "app-server", "--listen", "unix:///run/x/s1/app.sock"],
            tree,
            scanned,
            600,
        )

        # Not "program". The broker is there and no window is visible, but the
        # reading that would say so has a hole in it.
        self.assertEqual("unknown", verdict)

    def test_every_app_server_row_lands_in_exactly_one_list(self) -> None:
        """No row may fall out of both the lists and the counts."""
        for label, fixture in (
            ("program-driven", self.app_server_fixture(window=False)),
            ("person's window", self.app_server_fixture()),
            ("no client", self.app_server_fixture(window=False, broker=False)),
        ):
            with self.subTest(label=label):
                row, (people, machines, orphans) = self.app_server_lists(fixture)

                self.assertEqual(
                    1,
                    [row in people, row in machines, row in orphans].count(True),
                )
                self.assertEqual([], orphans)

    def test_foreign_codex_spawn_edge_shape_does_not_abort_inventory(self) -> None:
        fixture = list(inventory_fixture(2, providers=("codex",)))
        parent, child = uuid_for(1), uuid_for(2)
        fixture[4] = (fixture[4][0], {parent: [child]})
        fixture[2][2002]["cmdline"] = ["/usr/bin/codex", "exec", "build"]

        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        by_uuid = {row["identity"]["uuid"]: row for row in result["sessions"]}

        self.assertEqual([], by_uuid[parent]["subagents"])
        self.assertTrue(by_uuid[child]["is_subagent"])
        self.assertNotIn("parent_session", by_uuid[child])

    def test_provider_roots_outside_shpool_are_visible_separately(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        outside_pid = 9000
        outside_uuid = uuid_for(9000)
        fixture[2][outside_pid] = process(outside_pid, 1, "claude", cwd="/srv/outside")
        fixture[1].append(
            {
                "pid": outside_pid,
                "sessionId": outside_uuid,
                "cwd": "/srv/outside",
                "kind": "interactive",
                "name": "Outside task",
                "status": "waiting",
                "waitingFor": "user",
            }
        )
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        self.assertEqual(1, len(result["sessions"]))
        self.assertEqual(1, len(result["outside_agents"]))
        self.assertEqual(outside_uuid, result["outside_agents"][0]["identity"]["uuid"])
        self.assertTrue(result["outside_agents"][0]["needs_you"])

    def test_stale_claude_agent_without_live_process_is_not_outside(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        fixture[1].append(
            {
                "pid": 9000,
                "sessionId": uuid_for(9000),
                "cwd": "/srv/exiting",
                "kind": "interactive",
                "name": "Exiting task",
                "status": "idle",
            }
        )

        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)

        self.assertEqual([], result["outside_agents"])
        self.assertTrue(inventory_core.guard_live_inventory(result))

    def test_pid_loss_never_carries_an_old_identity_forward(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        exact = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        self.assertEqual(uuid_for(1), exact["sessions"][0]["identity"]["uuid"])
        fixture[3] = {}
        lost = inventory_core.build_inventory(*fixture, now=1_800_000_001)
        self.assertIsNone(lost["sessions"][0]["identity"]["uuid"])
        self.assertFalse(lost["sessions"][0]["recovery"]["available"])

    def test_pid_reuse_changes_exact_identity_and_start_ticks(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        first = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        new_uuid = uuid_for(777)
        fixture[2][2001]["start_ticks"] += 50_000
        fixture[3][2001] = [{"source": "cli", "id": new_uuid, "session_id": new_uuid}]
        fixture[4] = (
            {
                new_uuid: {
                    "id": new_uuid,
                    "title": "Replacement",
                    "cwd": "/srv/project-1",
                }
            },
            {},
        )
        second = inventory_core.build_inventory(*fixture, now=1_800_000_001)
        self.assertNotEqual(
            first["sessions"][0]["identity"]["process_start_ticks"],
            second["sessions"][0]["identity"]["process_start_ticks"],
        )
        self.assertEqual(new_uuid, second["sessions"][0]["identity"]["uuid"])

    def test_aliases_are_bound_to_provider_and_uuid_not_cwd_or_pid(self) -> None:
        fixture = list(inventory_fixture(2))
        fixture[5]["aliases"] = {
            f"claude:{uuid_for(1)}": "Config audit",
            f"codex:{uuid_for(2)}": "Deploy review",
        }
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        titles = {
            (row["provider"], row["identity"]["uuid"]): row["title"]
            for row in result["sessions"]
        }
        self.assertEqual("Config audit", titles[("claude", uuid_for(1))])
        self.assertEqual("Deploy review", titles[("codex", uuid_for(2))])

    def test_codex_session_index_latest_exact_name_precedes_database_title(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".codex-index-", dir=REPO) as raw:
            base = Path(raw)
            db = base / "state_5.sqlite"
            exact = uuid_for(2)
            connection = sqlite3.connect(db)
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, cwd TEXT)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?)",
                (exact, "Database title", "/srv/project-2"),
            )
            connection.commit()
            connection.close()
            index = base / "session_index.jsonl"
            index.write_text(
                "\n".join(
                    (
                        "{broken",
                        json.dumps({"id": exact, "thread_name": ""}),
                        json.dumps({"id": exact, "thread_name": "Earlier"}),
                        json.dumps({"id": exact, "thread_name": "Exact menu name"}),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            index.chmod(0o600)
            warnings: list[str] = []
            names = inventory_core.read_codex_session_index(index, warnings)
            threads, _ = inventory_core.read_codex_db(db, names)
        self.assertEqual("Exact menu name", names[exact])
        self.assertEqual("Exact menu name", threads[exact]["session_index_name"])
        self.assertEqual(1, len(warnings))
        fixture = list(inventory_fixture(2))
        fixture[5]["automatic_titles"] = {f"codex:{exact}": "Automatic Menu Name"}
        fixture[4] = (threads, {})
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        codex = next(row for row in result["sessions"] if row["provider"] == "codex")
        self.assertEqual("Exact menu name", codex["native_title"])
        self.assertEqual("native", codex["title_source"])

    def test_completed_codex_child_disappears_from_the_subagent_list(self) -> None:
        """Completion means closure (operator ruling X20, 2026-08-14): a
        finished pid-less child must not keep rendering as an open sub-agent.
        This replaces the earlier retained-but-not-active behaviour."""
        with tempfile.TemporaryDirectory(prefix=".codex-child-", dir=REPO) as raw:
            codex_home = Path(raw)
            db = codex_home / "state_5.sqlite"
            parent = uuid_for(1)
            child = uuid_for(2)
            rollout = codex_home / "sessions" / "rollout-child.jsonl"
            rollout.parent.mkdir()
            rollout.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"type": "task_started"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"type": "task_complete"},
                            }
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(db)
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, cwd TEXT, "
                "rollout_path TEXT, agent_nickname TEXT)"
            )
            connection.execute(
                "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, "
                "child_thread_id TEXT, status TEXT)"
            )
            connection.executemany(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                (
                    (parent, "Parent", "/srv/project-1", "", None),
                    (child, "Child", "/srv/project-1", str(rollout), "Verifier"),
                ),
            )
            connection.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
                (parent, child, "open"),
            )
            connection.commit()
            connection.close()

            threads, edges = inventory_core.read_codex_db(db)

        self.assertEqual("idle", edges[parent][0]["status"])
        fixture = list(inventory_fixture(1, providers=("codex",)))
        fixture[4] = (threads, edges)
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        row = result["sessions"][0]
        self.assertEqual([], row["subagents"])
        self.assertEqual(0, row["active_subagent_count"])

    def test_a_running_codex_child_still_renders_while_pid_less(self) -> None:
        """Codex children executing inside the parent process have no pid of
        their own mid-turn; "running" is their only liveness evidence and
        must keep them on screen."""
        with tempfile.TemporaryDirectory(prefix=".codex-child-", dir=REPO) as raw:
            codex_home = Path(raw)
            db = codex_home / "state_5.sqlite"
            parent = uuid_for(1)
            child = uuid_for(2)
            rollout = codex_home / "sessions" / "rollout-child.jsonl"
            rollout.parent.mkdir()
            rollout.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"type": "task_started"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(db)
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, cwd TEXT, "
                "rollout_path TEXT, agent_nickname TEXT)"
            )
            connection.execute(
                "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, "
                "child_thread_id TEXT, status TEXT)"
            )
            connection.executemany(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                (
                    (parent, "Parent", "/srv/project-1", "", None),
                    (child, "Child", "/srv/project-1", str(rollout), "Verifier"),
                ),
            )
            connection.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
                (parent, child, "open"),
            )
            connection.commit()
            connection.close()

            threads, edges = inventory_core.read_codex_db(db)

        self.assertEqual("working", edges[parent][0]["status"])
        fixture = list(inventory_fixture(1, providers=("codex",)))
        fixture[4] = (threads, edges)
        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        row = result["sessions"][0]
        self.assertEqual(["Verifier"], [x["title"] for x in row["subagents"]])
        self.assertEqual(1, row["active_subagent_count"])

    def test_codex_database_prompt_never_outranks_automatic_or_context_title(
        self,
    ) -> None:
        fixture = list(inventory_fixture(2))
        exact = uuid_for(2)
        raw_prompt = (
            "i need you to open research on the session kit project and make "
            "several important changes " * 8
        )
        fixture[4] = (
            {
                exact: {
                    "id": exact,
                    "title": raw_prompt,
                    "cwd": "/srv/example-project",
                    "session_index_name": "",
                }
            },
            {},
        )
        fixture[5]["automatic_titles"] = {f"codex:{exact}": "Session Kit Updates"}
        automatic = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        codex = next(row for row in automatic["sessions"] if row["provider"] == "codex")
        self.assertEqual("Session Kit Updates", codex["title"])
        self.assertEqual("automatic", codex["title_source"])
        self.assertEqual("ready", codex["automatic_name_state"])
        fixture[5]["automatic_titles"] = {}
        pending = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        codex = next(row for row in pending["sessions"] if row["provider"] == "codex")
        self.assertEqual("context", codex["title_source"])
        self.assertNotIn("i need you", codex["title"].casefold())
        self.assertEqual("pending", codex["automatic_name_state"])

    def test_codex_index_titles_survive_missing_or_corrupt_database(self) -> None:
        fixture = list(inventory_fixture(2))
        codex_uuid = uuid_for(2)
        for database_state in ("missing", "corrupt"):
            with self.subTest(database_state=database_state):
                with tempfile.TemporaryDirectory(
                    prefix=".codex-fallback-", dir=REPO
                ) as raw:
                    codex_home = Path(raw)
                    index = codex_home / "session_index.jsonl"
                    index.write_text(
                        json.dumps(
                            {
                                "id": codex_uuid,
                                "thread_name": "Indexed exact title",
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    index.chmod(0o600)
                    db = codex_home / "state_5.sqlite"
                    if database_state == "corrupt":
                        db.write_bytes(b"not sqlite")
                    names = inventory_core.read_codex_session_index(index)
                    if database_state == "missing":
                        threads, _ = inventory_core.read_codex_db(db, names)
                        self.assertEqual(
                            "Indexed exact title",
                            threads[codex_uuid]["session_index_name"],
                        )
                    with (
                        mock.patch.object(
                            inventory_core,
                            "_command_json",
                            side_effect=(fixture[0], fixture[1]),
                        ),
                        mock.patch.object(
                            inventory_core,
                            "scan_process_table",
                            return_value=fixture[2],
                        ),
                        mock.patch.object(
                            inventory_core,
                            "_payload_daemon_identity",
                            return_value={"pid": 10, "process_start_ticks": 100},
                        ),
                        mock.patch.object(
                            inventory_core,
                            "_codex_paths",
                            return_value=(codex_home, db),
                        ),
                        mock.patch.object(
                            inventory_core,
                            "index_codex_processes",
                            return_value=fixture[3],
                        ),
                        mock.patch.object(
                            inventory_core,
                            "recent_output_times",
                            return_value={},
                        ),
                    ):
                        collected = inventory_core.collect_live(fixture[5])
                    codex = next(
                        row
                        for row in collected["sessions"]
                        if row["provider"] == "codex"
                    )
                    self.assertEqual("Indexed exact title", codex["native_title"])
                    self.assertFalse(collected["_complete"])
                    self.assertTrue(
                        any(
                            "Codex metadata unavailable" in warning
                            for warning in collected["warnings"]
                        )
                    )

    def test_title_precedence_and_context_fallback_are_explicit(self) -> None:
        exact = uuid_for(70)
        timestamp = 1_785_220_000_000
        alias = {f"codex:{exact}": "Manual name"}
        self.assertEqual(
            ("Manual name", "alias"),
            inventory_core._provider_title_info(
                "codex", exact, "Native", alias, "/srv/example-project", timestamp
            ),
        )
        self.assertEqual(
            ("Native", "native"),
            inventory_core._provider_title_info(
                "codex", exact, "Native", {}, "/srv/example-project", timestamp
            ),
        )
        self.assertEqual(
            ("Automatic Name", "automatic"),
            inventory_core._provider_title_info(
                "codex",
                exact,
                "Raw prompt",
                {},
                "/srv/example-project",
                timestamp,
                {f"codex:{exact}": "Automatic Name"},
                provider_title_is_explicit=False,
            ),
        )
        title, source = inventory_core._provider_title_info(
            "codex", exact, "", {}, "/srv/example-project", timestamp
        )
        self.assertEqual("context", source)
        self.assertIn("Codex in example-project at ", title)
        self.assertNotIn("/srv/", title)
        self.assertNotIn(exact[:8], title)

    def test_operational_id_policy_preserves_raw_ids_and_bounds_display_only(
        self,
    ) -> None:
        safe = (
            "main",
            "main10",
            "s20260728-012345-4321",
            "s20260728-012345-4321-7",
        )
        for raw in safe:
            with self.subTest(raw=raw):
                self.assertEqual(
                    (True, None), inventory_core.shpool_id_mutation_policy(raw)
                )

        rejected = {
            "main-template": "template",
            "control": "unmanaged",
            "custom-session": "unmanaged",
            "main\ncontrol": "control",
            "é" * 65: "oversize",
        }
        for raw, reason in rejected.items():
            with self.subTest(raw=repr(raw)):
                self.assertEqual(
                    (False, reason), inventory_core.shpool_id_mutation_policy(raw)
                )
                shpool = {
                    "sessions": [
                        {
                            "name": raw,
                            "status": "Disconnected",
                            "started_at_unix_ms": 1_700_000_000_000,
                        }
                    ]
                }
                table = {
                    10: process(
                        10,
                        1,
                        "shpool",
                        cmdline=["/usr/bin/shpool", "daemon"],
                    ),
                    100: process(100, 10, "bash", session_name=raw),
                }
                result = inventory_core.build_inventory(
                    shpool,
                    [],
                    table,
                    {},
                    ({}, {}),
                    {"aliases": {}, "max_proc_nodes": 8192, "max_proc_depth": 32},
                    now=1_800_000_000,
                )
                row = result["sessions"][0]
                self.assertEqual(raw, row["shpool_id"])
                self.assertEqual(raw, row["shpool_id_raw"])
                self.assertEqual(raw, json.loads(json.dumps(row))["shpool_id_raw"])
                self.assertFalse(row["mutation_allowed"])
                self.assertEqual(reason, row["mutation_rejection_reason"])
                self.assertLessEqual(len(row["display_shpool_id"]), 32)
                self.assertFalse(
                    any(
                        unicodedata.category(character).startswith("C")
                        for character in row["display_shpool_id"]
                    )
                )

    def test_process_scan_keeps_a_changed_pid_but_publishes_nothing_about_it(
        self,
    ) -> None:
        """A pid that changed identity mid-scan is unread, not absent.

        Nothing it said may be published -- the fields were read from whatever
        it was before -- but the pid itself must stay in the table. Dropping
        it told every reader "there is no such process", and a reader asking
        whether anything in a tree holds a socket then answers "no" about a
        process it never saw, which is how a person's window becomes a
        machine row. The start tick is impossible on purpose, so no identity
        check can confirm against it.
        """
        with tempfile.TemporaryDirectory(prefix=".proc-race-", dir=REPO) as raw:
            proc_root = Path(raw)
            entry = proc_root / "123"
            entry.mkdir()
            (entry / "stat").write_text("fixture\n", encoding="utf-8")
            (entry / "cmdline").write_bytes(b"codex\0")
            (entry / "comm").write_text("codex\n", encoding="utf-8")
            (entry / "environ").write_bytes(b"")
            with mock.patch.object(
                inventory_core,
                "_proc_stat",
                side_effect=[(123, 10, 1000), (123, 10, 2000)],
            ):
                scanned = inventory_core.scan_process_table(proc_root, 64)
            self.assertEqual([123], sorted(scanned))
            self.assertTrue(scanned[123]["argv_unreadable"])
            self.assertEqual([], scanned[123]["cmdline"])
            self.assertEqual("", scanned[123]["comm"])
            self.assertEqual(-1, scanned[123]["start_ticks"])

    def test_codex_rollout_read_discards_metadata_on_pid_identity_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".codex-race-", dir=REPO) as raw:
            base = Path(raw)
            proc_root = base / "proc"
            codex_home = base / "codex-home"
            rollout = codex_home / "sessions/rollout-fixture.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "source": "cli",
                            "id": uuid_for(1),
                            "session_id": uuid_for(1),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            pid_dir = proc_root / "321"
            (pid_dir / "fd").mkdir(parents=True)
            (pid_dir / "stat").write_text("fixture\n", encoding="utf-8")
            os.symlink(rollout, pid_dir / "fd/7")
            expected_process = {"pid": 321, "ppid": 10, "start_ticks": 1000}
            stable = (321, 10, 1000)
            reused = (321, 10, 2000)
            with mock.patch.object(
                inventory_core,
                "_expected_proc_identity",
                side_effect=[stable, stable, reused],
            ):
                metadata = inventory_core.codex_open_rollouts(
                    321, proc_root, codex_home, expected_process
                )
            self.assertEqual([], metadata)

    def test_codex_metadata_is_read_through_proc_fd_not_reopened_pathname(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".codex-fd-", dir=REPO) as raw:
            base = Path(raw)
            proc_root = base / "proc"
            codex_home = base / "codex-home"
            rollout = codex_home / "sessions/rollout-direct-fd.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "source": "cli",
                            "id": uuid_for(55),
                            "session_id": uuid_for(55),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            pid_dir = proc_root / "654"
            (pid_dir / "fd").mkdir(parents=True)
            (pid_dir / "stat").write_text("fixture\n", encoding="utf-8")
            proc_descriptor = pid_dir / "fd/7"
            os.symlink(rollout, proc_descriptor)
            expected_process = {"pid": 654, "ppid": 10, "start_ticks": 1000}
            stable = (654, 10, 1000)
            real_open = os.open
            real_fstat = os.fstat
            opened_paths: list[Path] = []

            def open_spy(
                path: object, flags: int, *args: object, **kwargs: object
            ) -> int:
                candidate = Path(os.fspath(path))
                opened_paths.append(candidate)
                if candidate == rollout:
                    raise AssertionError("rollout pathname must never be reopened")
                return real_open(path, flags, *args, **kwargs)

            with (
                mock.patch.object(
                    inventory_core,
                    "_expected_proc_identity",
                    return_value=stable,
                ),
                mock.patch.object(
                    inventory_core.os,
                    "open",
                    side_effect=open_spy,
                ),
                mock.patch.object(
                    inventory_core.os,
                    "fstat",
                    wraps=real_fstat,
                ) as fstat_spy,
            ):
                metadata = inventory_core.codex_open_rollouts(
                    654, proc_root, codex_home, expected_process
                )
            self.assertEqual(uuid_for(55), metadata[0]["session_id"])
            self.assertEqual([proc_descriptor], opened_paths)
            self.assertGreaterEqual(fstat_spy.call_count, 2)

    def test_app_server_open_rollout_is_exact_managed_codex_identity(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        exact = uuid_for(1)
        app_pid = 2001
        fixture[2][app_pid]["cmdline"] = [
            "/usr/bin/codex",
            "-c",
            "check_for_update_on_startup=false",
            "app-server",
            "--listen",
            "unix:///run/user/1000/session-kit/app.sock",
        ]
        fixture[3][app_pid] = [
            {
                "source": "vscode",
                "originator": "example_coordination_broker",
                "id": exact,
                "session_id": exact,
                "_turn_state": "idle",
            }
        ]
        fixture[4][0][exact]["title"] = "How deep is deepest ocean"
        fixture[4][0][exact]["first_user_message"] = "how deep is deepest ocean"
        fixture[4][0][exact]["session_index_name"] = "How deep is deepest ocean"
        fixture[5]["colors"] = {f"codex:{exact}": "sky"}

        result = inventory_core.build_inventory(*fixture, now=1_800_000_000)

        row = result["sessions"][0]
        self.assertEqual("codex", row["provider"])
        self.assertEqual(exact, row["identity"]["uuid"])
        self.assertEqual(app_pid, row["identity"]["pid"])
        self.assertEqual(
            "native Codex PID open exact rollout",
            row["identity"]["provenance"],
        )
        self.assertEqual("How deep is deepest ocean", row["title"])
        self.assertEqual("sky", row["display_color"])
        self.assertEqual("idle", row["agent_status"])

    def test_app_server_refresh_targets_one_matching_remote_tui(self) -> None:
        socket = "unix:///run/user/1000/session-kit/app.sock"
        table = {
            100: process(100, 10, "bash"),
            200: process(
                200,
                100,
                "codex",
                cmdline=["/usr/bin/codex", "app-server", "--listen", socket],
            ),
            300: process(
                300,
                100,
                "codex",
                cmdline=[
                    "/usr/bin/codex",
                    "--remote",
                    socket,
                    "--no-alt-screen",
                ],
            ),
        }
        self.assertEqual(
            (300, 3000),
            inventory_core.codex_refresh_target(table, 100, 1000, 200, 2000),
        )

        table[301] = process(
            301,
            100,
            "codex",
            cmdline=["/usr/bin/codex", "--remote", socket, "--no-alt-screen"],
        )
        with self.assertRaisesRegex(
            inventory_core.CollectionError, "expected one remote Codex TUI"
        ):
            inventory_core.codex_refresh_target(table, 100, 1000, 200, 2000)

    def test_direct_codex_refresh_targets_provider_generation(self) -> None:
        table = {
            100: process(100, 10, "bash"),
            200: process(200, 100, "codex", cmdline=["/usr/bin/codex"]),
        }
        self.assertEqual(
            (200, 2000),
            inventory_core.codex_refresh_target(table, 100, 1000, 200, 2000),
        )

    def test_refresh_target_platform_command_has_no_single_pid_assumption(self) -> None:
        table = {
            100: process(100, 10, "bash"),
            200: process(200, 100, "codex", cmdline=["/usr/bin/codex"]),
        }
        args = argparse.Namespace(
            platform_action="codex-refresh-target",
            shell_pid=100,
            shell_generation=1000,
            provider_pid=200,
            provider_generation=2000,
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                inventory_core, "_require_supported_platform", return_value="linux"
            ),
            mock.patch.object(
                inventory_core, "platform_process_table", return_value=table
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(0, inventory_core._platform_command(args))
        self.assertEqual("200\t2000", output.getvalue().strip())

    def test_editor_rollout_requires_app_server_and_one_exact_thread(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        exact = uuid_for(1)
        app_pid = 2001
        editor_meta = {
            "source": "vscode",
            "id": exact,
            "session_id": exact,
        }
        fixture[3][app_pid] = [editor_meta]

        direct = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        self.assertEqual("unknown", direct["sessions"][0]["provider"])
        self.assertIsNone(direct["sessions"][0]["identity"]["uuid"])

        fixture[2][app_pid]["cmdline"] = [
            "/usr/bin/codex",
            "app-server",
            "--listen",
            "unix:///run/user/1000/session-kit/app.sock",
        ]
        second = uuid_for(2)
        fixture[3][app_pid].append(
            {"source": "vscode", "id": second, "session_id": second}
        )
        ambiguous = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        self.assertEqual("unknown", ambiguous["sessions"][0]["provider"])
        self.assertIsNone(ambiguous["sessions"][0]["identity"]["uuid"])


class ChurnedCensusIdentityTests(unittest.TestCase):
    """One unrelated process dying mid-scan must not unresolve any session.

    ``scan_process_table`` marks the whole table incomplete whenever any
    process exits between readdir and stat, which on a busy host is most
    scans. A session whose own subtree yielded exactly one provider claim has
    nothing to adjudicate, so that hole elsewhere on the machine is not
    evidence against it; only the multi-claim rival adjudication, which rests
    on "no other claimant exists", still demands the full census.
    """

    @staticmethod
    def churned(table: dict) -> process_inventory.ProcessTable:
        holed = process_inventory.ProcessTable(table)
        holed.complete = False
        return holed

    def test_a_sole_provider_claim_survives_a_churned_census(self) -> None:
        fixture = list(inventory_fixture(2))
        fixture[2] = self.churned(fixture[2])

        result = inventory_core.build_inventory(
            *fixture,
            now=1_800_000_000,
            daemon_binding={"pid": 10, "process_start_ticks": 100},
        )

        first, second = result["sessions"][0], result["sessions"][1]
        self.assertEqual("claude", first["provider"])
        self.assertEqual(uuid_for(1), first["identity"]["uuid"])
        self.assertEqual("codex", second["provider"])
        self.assertEqual(uuid_for(2), second["identity"]["uuid"])

    def test_aged_child_shell_and_worker_survive_a_churned_census(self) -> None:
        """Known children do not disappear because another process vanished."""

        def collect(*, churned: bool) -> list[dict]:
            fixture = list(inventory_fixture(1, providers=("claude",)))
            parent_uuid = uuid_for(1)
            fixture[2][3000] = process(3000, 2001, "bash")
            fixture[2][3001] = process(
                3001,
                2001,
                "claude",
                cmdline=[
                    "/usr/bin/claude",
                    "--parent-session-id",
                    parent_uuid,
                    "--agent-name",
                    "Verifier",
                ],
            )
            fixture[2][3002] = process(3002, 2001, "bash")
            fixture[1].append(
                {
                    "pid": 3001,
                    "sessionId": uuid_for(9001),
                    "cwd": "/srv/project-1",
                    "kind": "subagent",
                    "name": "Verifier",
                    "status": "busy",
                }
            )
            if churned:
                fixture[2] = self.churned(fixture[2])
            ages = {
                1001: 99_999,  # The managed root is not a child shell.
                2001: 200,
                3000: 2 * 3600 + 11 * 60,
                3001: 3600,
                3002: 3599,
            }
            with mock.patch.object(
                collector_inventory,
                "_process_age",
                side_effect=lambda pid, _table, _now: ages.get(pid),
            ):
                result = inventory_core.build_inventory(
                    *fixture,
                    now=1_800_000_000,
                    daemon_binding={"pid": 10, "process_start_ticks": 100},
                )
            return result["sessions"][0]["aged_children"]

        expected = [
            {"kind": "shell", "title": "bash", "age_seconds": 7860},
            {
                "kind": "worker",
                "provider": "claude",
                "title": "Verifier",
                "age_seconds": 3600,
            },
        ]
        self.assertEqual(expected, collect(churned=False))
        self.assertEqual(expected, collect(churned=True))

    def test_a_pidless_codex_edge_has_no_invented_process_age(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        fixture[4] = (
            fixture[4][0],
            {
                uuid_for(1): [
                    {
                        "provider": "codex",
                        "uuid": uuid_for(2),
                        "pid": None,
                        "title": "Verifier",
                        "status": "working",
                    }
                ]
            },
        )

        with mock.patch.object(
            collector_inventory, "_process_age", return_value=24 * 3600
        ):
            result = inventory_core.build_inventory(*fixture, now=1_800_000_000)

        row = result["sessions"][0]
        self.assertEqual(1, row["active_subagent_count"])
        self.assertEqual([], row["aged_children"])
        self.assertEqual(
            "Codex worker",
            collector_inventory._safe_worker_title(
                {
                    "provider": "codex",
                    "uuid": uuid_for(2),
                    "title": uuid_for(2)[:8],
                },
                None,
            ),
        )

    def test_lineage_owner_returns_the_sole_claim_despite_census_churn(
        self,
    ) -> None:
        table = self.churned(
            {
                100: {"pid": 100, "ppid": 1, "start_ticks": 50},
                200: {"pid": 200, "ppid": 100, "start_ticks": 60},
            }
        )
        claim = ("claude-agent", 200, uuid_for(9))
        self.assertEqual(
            (claim, []),
            collector_inventory._lineage_owner(100, [claim], table),
        )

    def test_rival_adjudication_still_requires_a_complete_census(self) -> None:
        table = self.churned(
            {
                100: {"pid": 100, "ppid": 1, "start_ticks": 50},
                200: {"pid": 200, "ppid": 100, "start_ticks": 60},
                300: {"pid": 300, "ppid": 200, "start_ticks": 70},
            }
        )
        claims = [
            ("claude-agent", 200, uuid_for(9)),
            ("codex", 300, uuid_for(8)),
        ]
        self.assertIsNone(collector_inventory._lineage_owner(100, claims, table))


class CodexTurnStateTests(unittest.TestCase):
    @staticmethod
    def _event(event_type: str, payload: dict) -> dict:
        return {"type": event_type, "payload": payload}

    def _state(self, events: list[dict]) -> str:
        with tempfile.TemporaryFile() as rollout:
            for event in events:
                rollout.write(json.dumps(event).encode("utf-8") + b"\n")
            rollout.flush()
            return inventory_core._rollout_turn_state(rollout.fileno())

    def test_pending_blocking_question_needs_reply_until_exact_output(self) -> None:
        started = self._event("event_msg", {"type": "task_started"})
        question = self._event(
            "response_item",
            {
                "type": "function_call",
                "name": "request_user_input",
                "call_id": "call-question",
                "arguments": json.dumps(
                    {"questions": [{"question": "task_complete is prose only"}]}
                ),
            },
        )
        wrong_output = self._event(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call-unrelated",
                "output": "ignored",
            },
        )
        exact_output = self._event(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call-question",
                "output": "answer text is not inspected",
            },
        )

        self.assertEqual(
            "needs your reply", self._state([started, question, wrong_output])
        )
        self.assertEqual(
            "working",
            self._state([started, question, wrong_output, exact_output]),
        )
        self.assertEqual(
            "working",
            self._state([question, exact_output]),
        )

    def test_new_structured_turn_retires_unanswered_prior_question(self) -> None:
        question = self._event(
            "response_item",
            {
                "type": "function_call",
                "name": "request_user_input",
                "call_id": "call-prior-turn",
                "arguments": json.dumps(
                    {"questions": [{"question": "Prior turn question"}]}
                ),
            },
        )
        new_turn = self._event(
            "event_msg",
            {"type": "task_started", "turn_id": "new-turn"},
        )
        self.assertEqual("working", self._state([question, new_turn]))

        completed = self._event("event_msg", {"type": "task_complete"})
        self.assertEqual("idle", self._state([question, completed]))

        aborted = self._event("event_msg", {"type": "turn_aborted"})
        self.assertEqual("idle", self._state([question, aborted]))

    def test_completed_turn_ending_in_a_prose_question_is_reply_optional(
        self,
    ) -> None:
        started = self._event("event_msg", {"type": "task_started"})
        asking = self._event(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "I checked both configs.\n\nWhich hostname should I use?",
                    }
                ],
            },
        )
        completed = self._event("event_msg", {"type": "task_complete"})
        # A finished turn whose last words ask the human something is a soft
        # wait, not idle, but never the hard "needs your reply".
        self.assertEqual("reply optional", self._state([started, asking, completed]))
        # A statement ending the turn stays idle.
        stating = self._event(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Done. Both configs match."}
                ],
            },
        )
        self.assertEqual("idle", self._state([started, stating, completed]))
        # A NEW turn clears the stale question.
        self.assertEqual("working", self._state([started, asking, completed, started]))

    def test_timed_question_is_optional_and_never_blocking(self) -> None:
        state = self._state(
            [
                self._event("event_msg", {"type": "task_started"}),
                self._event(
                    "response_item",
                    {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "call-timed",
                        "arguments": json.dumps(
                            {
                                "autoResolutionMs": 60000,
                                "questions": [{"question": "Optional context"}],
                            }
                        ),
                    },
                ),
            ]
        )
        self.assertEqual("reply optional", state)

    def test_lifecycle_events_define_working_idle_and_aborted(self) -> None:
        cases = (
            ([self._event("event_msg", {"type": "task_started"})], "working"),
            (
                [
                    self._event("event_msg", {"type": "task_started"}),
                    self._event("event_msg", {"type": "task_complete"}),
                    self._event(
                        "response_item",
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": "prose does not create a reply alert",
                        },
                    ),
                ],
                "idle",
            ),
            (
                [
                    self._event("event_msg", {"type": "task_started"}),
                    self._event("event_msg", {"type": "turn_aborted"}),
                ],
                "idle",
            ),
        )
        for events, expected in cases:
            with self.subTest(expected=expected, final=events[-1]["payload"]["type"]):
                self.assertEqual(expected, self._state(events))

    def test_malformed_or_incomplete_structured_evidence_is_unavailable(self) -> None:
        malformed_cases = (
            [
                self._event("event_msg", {"type": "task_started"}),
                self._event(
                    "response_item",
                    {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "call-bad-json",
                        "arguments": "{",
                    },
                ),
            ],
            [
                self._event(
                    "response_item",
                    {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "call-bad-timeout",
                        "arguments": json.dumps({"autoResolutionMs": True}),
                    },
                )
            ],
            [self._event("response_item", {"type": "function_call_output"})],
        )
        for events in malformed_cases:
            with self.subTest(events=events):
                self.assertEqual("state unavailable", self._state(events))

        with tempfile.TemporaryFile() as rollout:
            rollout.write(b'{"type":"event_msg","payload":{"type":"task_started"}}')
            rollout.flush()
            self.assertEqual(
                "state unavailable",
                inventory_core._rollout_turn_state(rollout.fileno()),
            )

    def test_bounded_backward_search_finds_lifecycle_beyond_normal_tail(self) -> None:
        started = self._event("event_msg", {"type": "task_started"})
        oversized = self._event(
            "event_msg",
            {"type": "token_count", "padding": "x" * inventory_core.ROLLOUT_TAIL_BYTES},
        )
        self.assertEqual(
            "working",
            self._state([started, oversized]),
        )
        self.assertEqual(
            "working",
            self._state([oversized, started]),
        )

    def test_state_propagates_to_managed_and_outside_inventory(self) -> None:
        states = (
            ("needs your reply", True),
            ("reply optional", False),
            ("working", False),
            ("idle", False),
            ("state unavailable", False),
        )
        for state, expected_needs_you in states:
            with self.subTest(state=state):
                fixture = list(inventory_fixture(1, providers=("codex",)))
                fixture[3][2001][0]["_turn_state"] = state
                managed = inventory_core.build_inventory(*fixture, now=1_800_000_000)
                self.assertEqual(state, managed["sessions"][0]["agent_status"])
                self.assertEqual(
                    expected_needs_you, managed["sessions"][0]["needs_you"]
                )

                outside = inventory_core.build_inventory(
                    {"sessions": []},
                    fixture[1],
                    fixture[2],
                    fixture[3],
                    fixture[4],
                    fixture[5],
                    now=1_800_000_000,
                )
                self.assertEqual(state, outside["outside_agents"][0]["agent_status"])
                self.assertEqual(
                    expected_needs_you,
                    outside["outside_agents"][0]["needs_you"],
                )

    def test_waiting_count_includes_only_blocking_reply_state(self) -> None:
        snapshot = {
            "sessions": [
                {"agent_status": "needs your reply", "needs_you": True},
                {"agent_status": "reply optional", "needs_you": False},
                {"agent_status": "working", "needs_you": False},
            ]
        }
        output = io.StringIO()
        with (
            mock.patch.object(inventory_core, "load_config", return_value={}),
            mock.patch.object(inventory_core, "snapshot", return_value=snapshot),
            contextlib.redirect_stdout(output),
        ):
            return_code = inventory_core.main(["waiting-count"])
        self.assertEqual(0, return_code)
        self.assertEqual("1\n", output.getvalue())


class TerminalNumberRegistryTests(unittest.TestCase):
    @staticmethod
    def _unknown(item: dict) -> None:
        item["provider"] = "unknown"
        item["display_provider"] = "unknown"
        item["identity"] = {
            "uuid": None,
            "pid": item["shpool_shell"]["pid"],
            "process_start_ticks": item["shpool_shell"]["process_start_ticks"],
            "provenance": "process tree",
            "confidence": "unknown",
        }

    @staticmethod
    def _orphan(item: dict) -> None:
        raw_id = item["shpool_id_raw"]
        item["provider"] = "unknown"
        item["display_provider"] = "unknown"
        item["availability"] = "ready"
        item["shpool_status"] = "Disconnected"
        item["identity"] = {
            "uuid": None,
            "pid": None,
            "process_start_ticks": None,
            "provenance": "none",
            "confidence": "unknown",
        }
        item["title"] = "Unresolved provider session"
        item["display_title"] = "Unresolved provider session"
        item["native_title"] = "Unresolved provider session"
        item["title_source"] = "provider"
        item["shpool_shell"] = None
        item["recovery"] = {
            "available": False,
            "provider": None,
            "uuid": None,
            "cwd": None,
            "argv": [],
            "command": None,
        }
        item["diagnostics"] = [
            f"expected one daemon child for {raw_id!r}, found 0",
            "identity candidates: Claude=0, Codex=0",
        ]
        item["terminal_number"] = None
        item["mutation_allowed"] = True
        item["mutation_rejection_reason"] = None
        item.pop("_terminal_identity_hint", None)

    def test_shell_less_orphan_is_quarantined_without_blocking_exact_rows(
        self,
    ) -> None:
        inventory = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        exact, orphan = inventory["sessions"]
        self._orphan(orphan)
        self.assertFalse(inventory_core.strict_live_inventory(inventory))
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        self.assertEqual(1, exact["terminal_number"])
        self.assertIsNone(orphan["terminal_number"])
        self.assertIs(orphan["mutation_allowed"], False)
        self.assertEqual(
            "missing-shell-generation",
            orphan["mutation_rejection_reason"],
        )
        self.assertEqual(2, registry["next_number"])
        self.assertEqual({1}, set(registry["bindings"].values()))
        self.assertTrue(inventory_core.guard_live_inventory(inventory))
        self.assertFalse(inventory_core.strict_live_inventory(inventory))
        unchanged = inventory_core.apply_terminal_numbers(
            inventory,
            registry,
            boot_id="boot-a",
            allocate=False,
        )
        self.assertEqual(registry, unchanged)
        self.assertIsNone(orphan["terminal_number"])
        self.assertIs(orphan["mutation_allowed"], False)

        self.assertIsNone(inventory_core.lookup(inventory, "2"))
        self.assertIs(inventory_core.lookup(inventory, "1"), exact)
        self.assertIs(
            inventory_core.lookup(inventory, orphan["shpool_id_raw"]),
            orphan,
        )
        rendered = inventory_core.render_inventory(inventory)
        self.assertNotIn("Unresolved provider session", rendered)
        self.assertIn("1 session: 1 ready", rendered)

        with tempfile.TemporaryDirectory(prefix=".mixed-input-", dir=REPO) as raw:
            path = Path(raw) / "inventory.json"
            path.write_text(json.dumps(inventory), encoding="utf-8")
            loaded = getattr(inventory_core, "load_inventory_input")(path)
        self.assertEqual(1, loaded["sessions"][0]["terminal_number"])
        self.assertIsNone(loaded["sessions"][1]["terminal_number"])

    def test_recycled_numbers_respect_the_quarantine_and_continuity(self) -> None:
        quarantine = inventory_core.TERMINAL_NUMBER_QUARANTINE_SECONDS
        now = 1_800_000_000.0
        # Pass 1: terminal 1 is an ordinary number now; sessions take 1 and 2.
        inventory = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        retired: dict[int, float] = {}
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=now,
        )
        self.assertEqual(
            [1, 2], [item["terminal_number"] for item in inventory["sessions"]]
        )
        self.assertEqual({}, retired)

        # Pass 2: session 1 died, ordinary number 1 enters quarantine.
        survivor = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_100
        )
        survivor["sessions"] = [survivor["sessions"][1]]
        registry = inventory_core.apply_terminal_numbers(
            survivor,
            registry,
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=now + 100,
        )
        self.assertEqual(2, survivor["sessions"][0]["terminal_number"])
        self.assertEqual({1: now + 100}, retired)

        # Pass 3, inside the quarantine: a NEW session must not take 1.
        fresh = inventory_core.build_inventory(*inventory_fixture(3), now=1_800_000_200)
        fresh["sessions"] = fresh["sessions"][1:]
        registry = inventory_core.apply_terminal_numbers(
            fresh,
            registry,
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=now + 200,
        )
        numbers = {
            item["shpool_id_raw"]: item["terminal_number"] for item in fresh["sessions"]
        }
        self.assertEqual(2, numbers["main2"])
        self.assertEqual(3, numbers["main3"])
        self.assertIn(1, retired)

        # Pass 4: the dead conversation RECOVERS inside the window, its AI
        # binding hands its old number back and the retirement clears.
        revived = inventory_core.build_inventory(
            *inventory_fixture(3), now=1_800_000_300
        )
        registry = inventory_core.apply_terminal_numbers(
            revived,
            registry,
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=now + 300,
        )
        revived_numbers = {
            item["shpool_id_raw"]: item["terminal_number"]
            for item in revived["sessions"]
        }
        self.assertEqual(1, revived_numbers["main"])
        self.assertNotIn(1, retired)

        # Pass 5: dead again, and the quarantine EXPIRES, the number frees,
        # its bindings prune, and the next new session takes the lowest gap.
        after = inventory_core.build_inventory(*inventory_fixture(3), now=1_800_000_400)
        after["sessions"] = after["sessions"][1:]
        registry = inventory_core.apply_terminal_numbers(
            after,
            registry,
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=now + 400,
        )
        self.assertEqual({1: now + 400}, {n: retired[n] for n in retired if n == 1})
        expired_pass = inventory_core.build_inventory(
            *inventory_fixture(4), now=1_800_000_500
        )
        expired_pass["sessions"] = expired_pass["sessions"][1:]
        registry = inventory_core.apply_terminal_numbers(
            expired_pass,
            registry,
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=now + 400 + quarantine + 1,
        )
        expired_numbers = {
            item["shpool_id_raw"]: item["terminal_number"]
            for item in expired_pass["sessions"]
        }
        # main4 is new; 1 finished quarantine and is the lowest ordinary number.
        self.assertEqual(1, expired_numbers["main4"])
        self.assertNotIn(1, retired)
        self.assertNotIn(f"ai:claude:{uuid_for(1)}", registry["bindings"])
        # Legacy invariant for pinned releases stays intact.
        self.assertGreater(registry["next_number"], max(registry["bindings"].values()))

    def test_retirement_ledger_round_trip_and_boot_reset(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "terminal-numbers-retired.json"
            payload = inventory_core._terminal_retirement_payload(
                {3: 1_800_000_000.0, 1: 1_700_000_000.5}, "boot-a"
            )
            path.write_text(json.dumps(payload), encoding="utf-8")
            os.chmod(path, 0o600)
            self.assertEqual(
                {1: 1_700_000_000.5, 3: 1_800_000_000.0},
                inventory_core._read_terminal_retirements(path, "boot-a"),
            )
            # A different boot voids the ledger (numbers restart anyway).
            self.assertEqual(
                {}, inventory_core._read_terminal_retirements(path, "boot-b")
            )
            # Junk keys/values never surface.
            path.write_text(
                json.dumps(
                    {
                        "schema_version": payload["schema_version"],
                        "boot_id": "boot-a",
                        "retired": {"x": 5, "2": True, "4": -1, "5": 9.5},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                {5: 9.5},
                inventory_core._read_terminal_retirements(path, "boot-a"),
            )

    def test_a_row_that_cannot_be_numbered_never_takes_the_estate_with_it(
        self,
    ) -> None:
        """One unprovable session used to refuse the WHOLE collection.

        Numbering raised, the census fell back to cache, and the guard
        snapshot every mutation depends on stopped answering: no close, no
        switch, no repair, and no launch, because arming a launch needs a
        snapshot that is live, fresh and warning-free. The operator's whole
        machine went read-only over one row, with a message that named no
        session. Each shape below is a row whose generation cannot be proven;
        each one is now quarantined and named, and the estate keeps working.
        """
        base = inventory_core.build_inventory(*inventory_fixture(2), now=1_800_000_000)
        self._orphan(base["sessions"][1])

        def known_provider(item: dict) -> None:
            item["provider"] = "shell"

        def attached(item: dict) -> None:
            item["availability"] = "attached"
            item["shpool_status"] = "Attached"

        def partial_shell(item: dict) -> None:
            item["shpool_shell"] = {
                "pid": 123,
                "process_start_ticks": None,
            }

        def partial_identity(item: dict) -> None:
            item["identity"]["pid"] = 123

        def retained_hint(item: dict) -> None:
            item["_terminal_identity_hint"] = {
                "provider": "codex",
                "uuid": uuid_for(99),
            }

        def invalid_start(item: dict) -> None:
            item["started_at_unix_ms"] = 0

        def recoverable(item: dict) -> None:
            item["recovery"] = inventory_core.recovery_spec(
                "codex",
                uuid_for(99),
                "/srv/project",
            )

        cases = (
            ("known-provider", known_provider),
            ("attached", attached),
            ("partial-shell", partial_shell),
            ("partial-identity", partial_identity),
            ("retained-hint", retained_hint),
            ("invalid-start", invalid_start),
            ("recoverable", recoverable),
        )
        for label, mutate in cases:
            with self.subTest(case=label):
                inventory = copy.deepcopy(base)
                mutate(inventory["sessions"][1])
                inventory_core.apply_terminal_numbers(
                    inventory,
                    inventory_core._empty_terminal_registry("boot-a"),
                    boot_id="boot-a",
                    allocate=True,
                )
                unprovable = inventory["sessions"][1]
                # The row that cannot be proven is named and made
                # unactionable, not numbered, and it does not take the rest
                # of the estate down with it.
                self.assertIsNone(unprovable["terminal_number"])
                self.assertIs(False, unprovable["mutation_allowed"])
                self.assertIn(
                    unprovable["mutation_rejection_reason"],
                    ("unprovable-generation", "missing-shell-generation"),
                )
                self.assertNotIn("_terminal_identity_hint", unprovable)
                healthy = inventory["sessions"][0]
                self.assertIsInstance(healthy["terminal_number"], int)
                self.assertGreater(healthy["terminal_number"], 0)
                # And the gate every mutation passes through still opens: the
                # guard snapshot is what `sk_capture_session_generation` waits
                # for before arming a launch, and what every close, switch and
                # repair loads first.
                self.assertTrue(inventory_core.guard_live_inventory(inventory))

    def test_an_unprovable_row_still_lets_a_new_launch_arm(self) -> None:
        """The arm's own gate, in the state that used to close it.

        `sk_capture_session_generation`, the step `sp new` runs between
        creating a session and arming its launch record, refuses to arm
        unless the guard snapshot is live, unstale and warning-free, and then
        resolves the new session in it with mutation allowed. Before this fix
        one unprovable row anywhere made that snapshot unavailable, so the
        arm never happened and the session sat as a shell with no provider.
        """
        inventory = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        # One row nobody can prove: a live-looking session with no shell.
        broken = inventory["sessions"][1]
        broken["shpool_shell"] = {"pid": 123, "process_start_ticks": None}
        inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        self.assertTrue(inventory_core.guard_live_inventory(inventory))
        self.assertFalse(inventory.get("stale"))
        self.assertEqual([], list(inventory.get("warnings", [])))
        # The session an arm would resolve is numbered and actionable.
        healthy = inventory["sessions"][0]
        self.assertIsInstance(healthy["terminal_number"], int)
        self.assertIs(True, healthy["mutation_allowed"])
        # And the unprovable one is refused by name rather than by silence.
        self.assertEqual("unprovable-generation", broken["mutation_rejection_reason"])

    def test_unprovable_generation_stays_on_the_board(self) -> None:
        """Only a PROVEN missing shell quarantines; doubt stays visible.

        Two review lanes demonstrated a real, actionable human session
        vanishing from the shell and curses lists because the shared
        predicate also matched unprovable-generation. The kit's rule is the
        opposite: what it cannot judge, a person must be able to see."""
        unprovable = {
            "terminal_number": None,
            "mutation_allowed": False,
            "mutation_rejection_reason": "unprovable-generation",
        }
        proven_dead = {
            "terminal_number": None,
            "mutation_allowed": False,
            "mutation_rejection_reason": "missing-shell-generation",
        }
        self.assertFalse(session_is_unavailable(unprovable))
        self.assertTrue(session_is_unavailable(proven_dead))

    def test_orphan_quarantine_marker_is_required_by_guard_and_frozen_input(
        self,
    ) -> None:
        inventory = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        self._orphan(inventory["sessions"][1])
        inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        for label, field, value in (
            ("numbered", "terminal_number", 99),
            ("mutable", "mutation_allowed", True),
            ("wrong-reason", "mutation_rejection_reason", "unsafe-id"),
        ):
            with self.subTest(case=label):
                changed = copy.deepcopy(inventory)
                changed["sessions"][1][field] = value
                self.assertFalse(inventory_core.guard_live_inventory(changed))
                with tempfile.TemporaryDirectory(
                    prefix=".mixed-invalid-",
                    dir=REPO,
                ) as raw:
                    path = Path(raw) / "inventory.json"
                    path.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaises(inventory_core.CollectionError):
                        getattr(inventory_core, "load_inventory_input")(path)

    def test_snapshot_writes_live_exact_rows_alongside_quarantined_orphan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".orphan-snapshot-",
            dir=REPO,
        ) as raw:
            state = Path(raw)
            state.chmod(0o700)
            config = {
                "state_dir": state,
                "aliases": {},
                "max_proc_nodes": 8192,
                "max_proc_depth": 32,
            }
            live = inventory_core.build_inventory(
                *inventory_fixture(2),
                now=1_800_000_000,
            )
            self._orphan(live["sessions"][1])
            with (
                mock.patch.object(
                    inventory_core,
                    "collect_live",
                    return_value=live,
                ),
                mock.patch.object(
                    inventory_core,
                    "_boot_id",
                    return_value="boot-a",
                ),
            ):
                result = inventory_core.snapshot(config=config)
            self.assertEqual("live", result["source"])
            self.assertIs(result["stale"], False)
            self.assertEqual([], result["warnings"])
            self.assertEqual(1, result["sessions"][0]["terminal_number"])
            self.assertIsNone(result["sessions"][1]["terminal_number"])
            stored = json.loads((state / "inventory.json").read_text(encoding="utf-8"))
            self.assertEqual(result, stored)

    def test_provisional_generation_promotes_without_renumbering(self) -> None:
        inventory = inventory_core.build_inventory(
            *inventory_fixture(1), now=1_800_000_000
        )
        item = inventory["sessions"][0]
        exact_identity = copy.deepcopy(item["identity"])
        self._unknown(item)
        registry = inventory_core._empty_terminal_registry("boot-a")
        registry = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual(1, item["terminal_number"])

        inventory["daemon_generation"]["process_start_ticks"] += 99
        unchanged = inventory_core.apply_terminal_numbers(
            inventory,
            registry,
            boot_id="boot-a",
            allocate=False,
        )
        self.assertEqual(1, item["terminal_number"])
        self.assertEqual(registry, unchanged)

        item["provider"] = "claude"
        item["identity"] = exact_identity
        promoted = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual(1, item["terminal_number"])
        self.assertEqual(1, promoted["bindings"][f"ai:claude:{exact_identity['uuid']}"])

        item["shpool_id"] = item["shpool_id_raw"] = "main20"
        item["started_at_unix_ms"] += 100
        item["shpool_shell"]["pid"] += 100
        item["shpool_shell"]["process_start_ticks"] += 100
        recovered = inventory_core.apply_terminal_numbers(
            inventory, promoted, boot_id="boot-a", allocate=True
        )
        self.assertEqual(1, item["terminal_number"])
        self.assertEqual(2, recovered["next_number"])

    def test_exact_recovery_overrides_a_new_generation_provisional_number(
        self,
    ) -> None:
        inventory = inventory_core.build_inventory(
            *inventory_fixture(1), now=1_800_000_000
        )
        item = inventory["sessions"][0]
        exact_identity = copy.deepcopy(item["identity"])
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        self.assertEqual(1, item["terminal_number"])

        item["shpool_id"] = item["shpool_id_raw"] = "main20"
        item["started_at_unix_ms"] += 100
        item["shpool_shell"]["pid"] += 100
        item["shpool_shell"]["process_start_ticks"] += 100
        self._unknown(item)
        registry = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual(2, item["terminal_number"])

        item["provider"] = "claude"
        item["identity"] = exact_identity
        recovered = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual(1, item["terminal_number"])
        self.assertEqual(3, recovered["next_number"])
        generation = inventory_core._terminal_generation_key(inventory, item, "boot-a")
        self.assertEqual(1, recovered["bindings"][generation])
        self.assertEqual(
            [generation],
            [
                key
                for key, value in recovered["bindings"].items()
                if key.startswith("generation:") and value == 1
            ],
        )

    def test_other_provider_or_uuid_never_inherits_an_exact_binding(self) -> None:
        inventory = inventory_core.build_inventory(
            *inventory_fixture(1), now=1_800_000_000
        )
        item = inventory["sessions"][0]
        first_uuid = item["identity"]["uuid"]
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        self.assertEqual(1, item["terminal_number"])

        item["shpool_id"] = item["shpool_id_raw"] = "main20"
        item["started_at_unix_ms"] += 100
        item["shpool_shell"]["pid"] += 100
        item["shpool_shell"]["process_start_ticks"] += 100
        item["provider"] = "codex"
        registry = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual(first_uuid, item["identity"]["uuid"])
        self.assertEqual(2, item["terminal_number"])

        item["shpool_id"] = item["shpool_id_raw"] = "main21"
        item["started_at_unix_ms"] += 100
        item["shpool_shell"]["pid"] += 100
        item["shpool_shell"]["process_start_ticks"] += 100
        item["provider"] = "claude"
        item["identity"]["uuid"] = uuid_for(99)
        inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual(3, item["terminal_number"])

    def test_unrelated_uuid_replaces_same_generation_with_a_new_number(
        self,
    ) -> None:
        inventory = inventory_core.build_inventory(
            *inventory_fixture(1), now=1_800_000_000
        )
        item = inventory["sessions"][0]
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        item["identity"]["uuid"] = uuid_for(98)
        updated = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        generation = inventory_core._terminal_generation_key(inventory, item, "boot-a")
        self.assertEqual(2, item["terminal_number"])
        self.assertEqual(2, updated["bindings"][generation])

    def test_registry_rejects_duplicate_generations_and_active_exact_duplicates(
        self,
    ) -> None:
        corrupt = {
            "schema_version": 1,
            "boot_id": "boot-a",
            "next_number": 2,
            "bindings": {
                f"generation:{'1' * 64}": 1,
                f"generation:{'2' * 64}": 1,
            },
        }
        with self.assertRaisesRegex(
            inventory_core.CollectionError, "duplicate generation"
        ):
            inventory_core._validate_terminal_registry(corrupt, "boot-a")

        inventory = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            boot_id="boot-a",
            allocate=True,
        )
        inventory["sessions"][1]["provider"] = inventory["sessions"][0]["provider"]
        inventory["sessions"][1]["identity"] = copy.deepcopy(
            inventory["sessions"][0]["identity"]
        )
        with self.assertRaisesRegex(
            inventory_core.CollectionError, "two active sessions"
        ):
            inventory_core.apply_terminal_numbers(
                inventory, registry, boot_id="boot-a", allocate=False
            )

    def test_snapshot_writes_stable_numbers_and_no_write_leaves_new_rows_null(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".terminal-state-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            config = {
                "state_dir": state,
                "aliases": {},
                "max_proc_nodes": 8192,
                "max_proc_depth": 32,
            }
            first_live = inventory_core.build_inventory(
                *inventory_fixture(2), now=1_800_000_000
            )
            second_live = inventory_core.build_inventory(
                *inventory_fixture(2), now=1_800_000_001
            )
            second_live["sessions"].reverse()
            third_live = inventory_core.build_inventory(
                *inventory_fixture(3), now=1_800_000_002
            )
            with (
                mock.patch.object(inventory_core, "_boot_id", return_value="boot-a"),
                mock.patch.object(
                    inventory_core,
                    "collect_live",
                    side_effect=(
                        copy.deepcopy(first_live),
                        copy.deepcopy(second_live),
                        copy.deepcopy(third_live),
                    ),
                ),
            ):
                first = inventory_core.snapshot(write_state=True, config=config)
                second = inventory_core.snapshot(write_state=True, config=config)
                no_write = inventory_core.snapshot(write_state=False, config=config)
            first_map = {
                row["shpool_id_raw"]: row["terminal_number"]
                for row in first["sessions"]
            }
            second_map = {
                row["shpool_id_raw"]: row["terminal_number"]
                for row in second["sessions"]
            }
            no_write_map = {
                row["shpool_id_raw"]: row["terminal_number"]
                for row in no_write["sessions"]
            }
            self.assertEqual(first_map, second_map)
            self.assertEqual(first_map["main"], no_write_map["main"])
            self.assertEqual(first_map["main2"], no_write_map["main2"])
            self.assertIsNone(no_write_map["main3"])
            registry_path = state / "terminal-numbers.json"
            self.assertEqual(0o600, registry_path.stat().st_mode & 0o777)

    def test_registry_corruption_and_same_boot_disappearance_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".terminal-safe-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            registry = state / "terminal-numbers.json"
            epoch = state / "terminal-numbers.initialized.json"
            epoch.write_text(
                json.dumps({"schema_version": 1, "boot_id": "boot-a"}),
                encoding="utf-8",
            )
            epoch.chmod(0o600)
            with self.assertRaisesRegex(inventory_core.CollectionError, "disappeared"):
                inventory_core._read_terminal_registry(registry, "boot-a", epoch)
            registry.write_text("{broken", encoding="utf-8")
            registry.chmod(0o600)
            with self.assertRaisesRegex(inventory_core.CollectionError, "invalid JSON"):
                inventory_core._read_terminal_registry(registry, "boot-a", epoch)
            registry.unlink()
            os.symlink(epoch, registry)
            with self.assertRaises(inventory_core.CollectionError):
                inventory_core._read_terminal_registry(registry, "boot-a", epoch)

    def test_registry_receipt_requires_exact_schema_but_allows_old_boot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".terminal-epoch-", dir=REPO) as raw:
            state = Path(raw)
            state.chmod(0o700)
            registry = state / "terminal-numbers.json"
            epoch = state / "terminal-numbers.initialized.json"
            cases = (
                {"schema_version": 2, "boot_id": "boot-old"},
                {
                    "schema_version": 1,
                    "boot_id": "boot-old",
                    "extra": True,
                },
                {"schema_version": 1, "boot_id": 7},
            )
            for receipt in cases:
                with self.subTest(receipt=receipt):
                    epoch.write_text(json.dumps(receipt), encoding="utf-8")
                    epoch.chmod(0o600)
                    with self.assertRaisesRegex(
                        inventory_core.CollectionError, "invalid schema"
                    ):
                        inventory_core._read_terminal_registry(
                            registry, "boot-a", epoch
                        )
            epoch.write_text(
                json.dumps({"schema_version": 1, "boot_id": "boot-old"}),
                encoding="utf-8",
            )
            epoch.chmod(0o600)
            self.assertEqual(
                inventory_core._empty_terminal_registry("boot-a"),
                inventory_core._read_terminal_registry(registry, "boot-a", epoch),
            )

    def test_verified_new_boot_resets_monotonic_registry(self) -> None:
        registry = {
            "schema_version": 1,
            "boot_id": "boot-a",
            "next_number": 12,
            "bindings": {
                f"ai:claude:{uuid_for(1)}": 11,
            },
        }
        reset = inventory_core._validate_terminal_registry(registry, "boot-b")
        self.assertEqual(inventory_core._empty_terminal_registry("boot-b"), reset)


class AutomaticTitleTests(unittest.TestCase):
    def _private_config(self, base: Path) -> tuple[Path, dict]:
        base.chmod(0o700)
        state = base / "state"
        state.mkdir(mode=0o700)
        config_path = base / "inventory.json"
        config_path.write_text(
            json.dumps({"schema_version": 1, "aliases": {}}),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        return config_path, {
            "state_dir": state,
            "max_proc_nodes": 8192,
            "max_proc_depth": 32,
        }

    def _caller_fixture(
        self, config: dict, exact: str
    ) -> tuple[dict, dict[int, dict], dict[str, str], int]:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        fixture[5].update(config)
        fixture[4] = (
            {
                exact: {
                    "id": exact,
                    "title": "raw first prompt " * 20,
                    "cwd": "/srv/project-1",
                    "session_index_name": "",
                }
            },
            {},
        )
        fixture[3] = {2001: [{"source": "cli", "id": exact, "session_id": exact}]}
        fixture[2][3001] = process(3001, 2001, "python3")
        inventory = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        environment = {
            "SHPOOL_SESSION_NAME": "main",
            "CODEX_THREAD_ID": exact,
            # Sandboxed HOME: without it the title/color pushes inside
            # self-name land in the developer's real provider stores.
            "HOME": str(config.get("state_dir", "/nonexistent-home")),
        }
        return inventory, fixture[2], environment, 3001

    def test_self_name_revalidation_never_collects_under_the_name_lock(self) -> None:
        """The 2026-08-17 estate jam, as a call-count contract.

        ``mutate_self_name`` runs ``revalidate`` inside the name-store locks.
        The old revalidate took a fresh full collection there, and collecting
        can re-acquire config.lock on a second descriptor, a process waiting
        on its own flock, which no other process can break; every collector on
        the machine queued behind it. The contract: one snapshot total, taken
        before any lock; revalidation re-reads only the process table.
        """
        from lib.sessionkit_inventory import self_name as low_level

        snapshots: list[dict] = []
        tables: list[object] = []

        def fake_snapshot(**kwargs: object) -> dict:
            snapshots.append(dict(kwargs))
            return {"sessions": []}

        def fake_table(root: object, cap: int) -> dict:
            tables.append(root)
            return {}

        def fake_mutate(
            config: dict, provider: str, uuid: str, title: str, *, revalidate=None
        ) -> dict:
            self.assertIsNotNone(revalidate)
            revalidate()  # exactly what names.py does inside its locks
            return {"aliases": {}, "automatic_titles": {}}

        result = low_level.self_name_automatic_title(
            {"max_proc_nodes": 10},
            "Session Kit Updates",
            environ={},
            default_max_proc_nodes=10,
            record_retry=lambda *a, **k: None,
            canonical_colors=lambda config: {},
            mutate_self_name=fake_mutate,
            normalize_title=lambda title: title,
            process_table_reader=fake_table,
            propagate_color=lambda *a, **k: {},
            propagate_title=lambda *a, **k: {},
            prove_caller=lambda live, table, environ, pid: {
                "provider": "codex",
                "uuid": uuid_for(91),
            },
            record_title_failure=lambda *a, **k: 0,
            session_color=lambda *a, **k: None,
            snapshot_inventory=fake_snapshot,
        )
        self.assertEqual(
            1, len(snapshots), "a revalidation re-collected under the name lock"
        )
        self.assertEqual(2, len(tables))
        self.assertEqual("ready", result["automatic_name_state"])

    def test_title_validation_kill_switch_and_provenance_precedence(self) -> None:
        self.assertEqual(
            "Session Kit Updates",
            inventory_core.normalize_automatic_title(" Session Kit Updates "),
        )
        for rejected in (
            "One",
            "one lowercase",
            "Codex Session Work",
            "Six Word Automatic Session Name Here",
            "Unsafe / Path",
            "Alphaabcdefghijkl Betaabcdefghijkl Gammaabcdefghijkl Delta Epsilon Sixth",
        ):
            with self.subTest(rejected=rejected):
                with self.assertRaises(inventory_core.CollectionError):
                    inventory_core.normalize_automatic_title(rejected)
        self.assertFalse(
            inventory_core.automatic_naming_enabled({"SESSION_KIT_AUTO_NAME": "0"})
        )
        exact = uuid_for(81)
        automatic = {f"codex:{exact}": "Session Kit Updates"}
        # A native rename outranks the alias tier, `sp name` included. The
        # kit last pushed "Session Kit Updates", so a native store reading
        # "Provider Rename" can only have been typed after that push: it is
        # the newest thing a person said about this name, and it wins.
        self.assertEqual(
            ("Provider Rename", "native"),
            inventory_core._provider_title_info(
                "codex",
                exact,
                "Provider Rename",
                {f"codex:{exact}": "Manual Override"},
                automatic_titles=automatic,
            ),
        )
        # The alias keeps its place against a native title the kit itself
        # pushed, that one is the kit's own echo, not somebody's rename.
        self.assertEqual(
            ("Manual Override", "alias"),
            inventory_core._provider_title_info(
                "codex",
                exact,
                "Session Kit Updates",
                {f"codex:{exact}": "Manual Override"},
                automatic_titles=automatic,
            ),
        )
        self.assertEqual(
            ("Provider Rename", "native"),
            inventory_core._provider_title_info(
                "codex",
                exact,
                "Provider Rename",
                {},
                automatic_titles=automatic,
            ),
        )
        with mock.patch.dict(os.environ, {"SESSION_KIT_AUTO_NAME": "0"}, clear=False):
            title, source = inventory_core._provider_title_info(
                "codex",
                exact,
                "Raw prompt",
                {},
                "/srv/project",
                1_700_000_000_000,
                automatic,
                provider_title_is_explicit=False,
            )
        self.assertEqual("context", source)
        self.assertNotEqual("Session Kit Updates", title)

    def test_atomic_write_manual_alias_race_and_mixed_release_retention(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".automatic-title-", dir=REPO) as raw:
            base = Path(raw)
            config_path, config = self._private_config(base)
            exact = uuid_for(82)
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": str(config_path)}, clear=False
            ):
                stored = inventory_core.mutate_canonical_automatic_title(
                    config, "codex", exact, "Session Kit Updates"
                )
                self.assertEqual("Session Kit Updates", stored["title"])
                # The pinned schema-v1 alias writer preserves the new keys,
                # which is the rollback/mixed-release compatibility contract.
                inventory_core.mutate_canonical_alias(
                    config, "codex", uuid_for(83), "Pinned Alias"
                )
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    "Session Kit Updates",
                    document["automatic_titles"][f"codex:{exact}"],
                )
                inventory_core.mutate_canonical_alias(
                    config, "codex", exact, "Manual Override"
                )
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "explicit local alias"
                ):
                    inventory_core.mutate_canonical_automatic_title(
                        config,
                        "codex",
                        exact,
                        "Different Automatic Name",
                        overwrite=True,
                    )
                title, source = inventory_core._provider_title_info(
                    "codex",
                    exact,
                    "Raw prompt",
                    inventory_core.canonical_aliases(config),
                    automatic_titles=inventory_core.canonical_automatic_titles(config),
                    provider_title_is_explicit=False,
                )
                self.assertEqual(("Manual Override", "alias"), (title, source))
                # An explicit reset removes only the automatic layer, even
                # while a manual alias remains authoritative.
                inventory_core.mutate_canonical_automatic_title(
                    config, "codex", exact, None
                )
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertNotIn(f"codex:{exact}", document.get("automatic_titles", {}))
                self.assertEqual(
                    "Manual Override", document["aliases"][f"codex:{exact}"]
                )
                raced = uuid_for(87)
                barrier = threading.Barrier(2)
                errors: list[BaseException] = []

                def automatic_writer() -> None:
                    barrier.wait()
                    try:
                        inventory_core.mutate_canonical_automatic_title(
                            config, "codex", raced, "Concurrent Task Name"
                        )
                    except inventory_core.CollectionError:
                        # If the manual writer wins the lock, refusing the
                        # lower-precedence automatic write is correct.
                        pass
                    except BaseException as exc:
                        errors.append(exc)

                def manual_writer() -> None:
                    barrier.wait()
                    try:
                        inventory_core.mutate_canonical_alias(
                            config, "codex", raced, "Concurrent Manual Name"
                        )
                    except BaseException as exc:
                        errors.append(exc)

                threads = [
                    threading.Thread(target=automatic_writer),
                    threading.Thread(target=manual_writer),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual([], errors)
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    "Concurrent Manual Name",
                    document["aliases"][f"codex:{raced}"],
                )
                title, source = inventory_core._provider_title_info(
                    "codex",
                    raced,
                    "Raw prompt",
                    inventory_core.canonical_aliases(config),
                    automatic_titles=inventory_core.canonical_automatic_titles(config),
                    provider_title_is_explicit=False,
                )
                self.assertEqual(("Concurrent Manual Name", "alias"), (title, source))

    def test_self_name_proves_root_generation_refuses_bad_callers_and_tracks_failures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".self-name-", dir=REPO) as raw:
            base = Path(raw)
            config_path, config = self._private_config(base)
            exact = uuid_for(84)
            inventory, table, environment, current_pid = self._caller_fixture(
                config, exact
            )
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": str(config_path)}, clear=False
            ):
                for attempt in (1, 2):
                    with self.assertRaisesRegex(
                        inventory_core.CollectionError,
                        "name failed" if attempt == 2 else "name pending",
                    ):
                        inventory_core.self_name_automatic_title(
                            config,
                            "bad lowercase",
                            inventory=inventory,
                            process_table=table,
                            environ=environment,
                            current_pid=current_pid,
                        )
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    2,
                    document["automatic_title_failures"][f"codex:{exact}"],
                )
                failed_inventory, _, _, _ = self._caller_fixture(
                    {
                        **config,
                        "automatic_title_failures": document[
                            "automatic_title_failures"
                        ],
                    },
                    exact,
                )
                self.assertEqual(
                    "failed",
                    failed_inventory["sessions"][0]["automatic_name_state"],
                )
                result = inventory_core.self_name_automatic_title(
                    config,
                    "Session Kit Updates",
                    inventory=inventory,
                    process_table=table,
                    environ=environment,
                    current_pid=current_pid,
                )
                self.assertEqual("pending", result["automatic_name_state"])
                self.assertTrue(result["provider_title_warnings"])
                retry = config["state_dir"] / "provider-title-retries.json"
                self.assertTrue(retry.is_file())
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertNotIn("automatic_title_failures", document)
                # A self-name is an explicit rename: it must land in the alias
                # tier too, or a provider's own ai-title keeps outranking it
                # in every display.
                self.assertEqual(
                    "Session Kit Updates",
                    result["aliases"][f"codex:{exact}"],
                )
                self.assertEqual(
                    "Session Kit Updates",
                    document["aliases"][f"codex:{exact}"],
                )
                repeated = inventory_core.self_name_automatic_title(
                    config,
                    "Session Kit Release",
                    inventory=inventory,
                    process_table=table,
                    environ=environment,
                    current_pid=current_pid,
                )
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual("Session Kit Release", repeated["title"])
                self.assertEqual(
                    "Session Kit Release",
                    document["aliases"][f"codex:{exact}"],
                )
                self.assertEqual(
                    "Session Kit Release",
                    document["automatic_titles"][f"codex:{exact}"],
                )

                bad_thread = dict(environment)
                bad_thread["CODEX_THREAD_ID"] = uuid_for(99)
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "managed root conversation"
                ):
                    inventory_core.self_name_automatic_title(
                        config,
                        "Another Task Name",
                        inventory=inventory,
                        process_table=table,
                        environ=bad_thread,
                        current_pid=current_pid,
                    )
                child_uuid = uuid_for(98)
                child_inventory = copy.deepcopy(inventory)
                child_inventory["sessions"][0]["subagents"] = [
                    {
                        "provider": "codex",
                        "uuid": child_uuid,
                        "status": "running",
                    }
                ]
                child_environment = dict(environment)
                child_environment["CODEX_THREAD_ID"] = child_uuid
                with self.assertRaisesRegex(inventory_core.CollectionError, "subagent"):
                    inventory_core.self_name_automatic_title(
                        config,
                        "Another Task Name",
                        inventory=child_inventory,
                        process_table=table,
                        environ=child_environment,
                        current_pid=current_pid,
                    )
                outside = dict(environment)
                outside.pop("SHPOOL_SESSION_NAME")
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "outside a managed shell"
                ):
                    inventory_core.self_name_automatic_title(
                        config,
                        "Another Task Name",
                        inventory=inventory,
                        process_table=table,
                        environ=outside,
                        current_pid=current_pid,
                    )
                setup = copy.deepcopy(inventory)
                setup["sessions"][0]["setup_incomplete"] = True
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "setup is incomplete"
                ):
                    inventory_core.self_name_automatic_title(
                        config,
                        "Another Task Name",
                        inventory=setup,
                        process_table=table,
                        environ=environment,
                        current_pid=current_pid,
                    )
                subagent_table = copy.deepcopy(table)
                subagent_table[3001]["cmdline"] = [
                    "python3",
                    "--parent-session-id",
                    exact,
                ]
                with self.assertRaisesRegex(inventory_core.CollectionError, "subagent"):
                    inventory_core.prove_self_name_caller(
                        inventory,
                        subagent_table,
                        environment,
                        current_pid,
                    )

    def test_provider_title_pending_requires_a_private_regular_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-state-", dir=REPO) as raw:
            state = Path(raw)
            marker_root = state / "provider-untitled"
            marker_root.mkdir(mode=0o700)
            marker_root.chmod(0o700)
            live = {
                "sessions": [
                    {
                        "provider": "codex",
                        "shpool_id_raw": "main2",
                    },
                    {
                        "provider": "claude",
                        "shpool_id_raw": "main3",
                    },
                ]
            }
            (marker_root / "main2").touch()
            inventory_core.apply_provider_title_states(live, {"state_dir": state})
            self.assertEqual("pending", live["sessions"][0]["provider_title_state"])
            self.assertNotIn("provider_title_state", live["sessions"][1])

            live["sessions"][0].update(
                {"availability": "attached", "agent_status": "working"}
            )
            inventory_core.apply_provider_title_states(live, {"state_dir": state})
            self.assertEqual("deferred", live["sessions"][0]["provider_title_state"])

            (marker_root / "main2").unlink()
            (marker_root / "target").touch()
            (marker_root / "main2").symlink_to(marker_root / "target")
            inventory_core.apply_provider_title_states(live, {"state_dir": state})
            self.assertEqual("ready", live["sessions"][0]["provider_title_state"])

            old_active = marker_root / "main"
            old_active.write_text("activegeneration\n", encoding="utf-8")
            old_orphan = marker_root / "gone"
            old_orphan.write_text("orphangeneration\n", encoding="utf-8")
            old = time.time() - 8 * 86400
            os.utime(old_active, (old, old))
            os.utime(old_orphan, (old, old))
            exact_live = inventory_core.build_inventory(
                *inventory_fixture(1, providers=("codex",)),
                now=1_800_000_000,
            )
            with (
                mock.patch.object(
                    inventory_core, "collect_live", return_value=exact_live
                ),
                mock.patch.object(inventory_core, "_boot_id", return_value="boot-a"),
            ):
                result = inventory_core.snapshot(
                    write_state=True,
                    config={
                        "state_dir": state,
                        "max_proc_nodes": 8192,
                        "max_proc_depth": 32,
                    },
                )
            self.assertTrue(old_active.exists())
            self.assertFalse(old_orphan.exists())
            self.assertEqual(
                "gone", result["provider_untitled_quarantine"][0]["marker"]
            )
            self.assertTrue(
                Path(result["provider_untitled_quarantine"][0]["quarantine"]).is_file()
            )
            exact_live.pop("provider_untitled_quarantine", None)

            unsafe_state = state / "unsafe-state"
            unsafe_state.mkdir(mode=0o700)
            external = state / "external-markers"
            external.mkdir(mode=0o700)
            external_marker = external / "foreign"
            external_marker.touch()
            os.utime(external_marker, (old, old))
            (unsafe_state / "provider-untitled").symlink_to(
                external, target_is_directory=True
            )
            with (
                mock.patch.object(
                    inventory_core, "collect_live", return_value=exact_live
                ),
                mock.patch.object(inventory_core, "_boot_id", return_value="boot-a"),
            ):
                unsafe_result = inventory_core.snapshot(
                    write_state=True,
                    config={
                        "state_dir": unsafe_state,
                        "max_proc_nodes": 8192,
                        "max_proc_depth": 32,
                    },
                )
            self.assertTrue(external_marker.exists())
            self.assertNotIn("provider_untitled_quarantine", unsafe_result)

    def test_untitled_quarantine_cannot_reach_a_newer_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".title-generation-", dir=REPO) as raw:
            state = Path(raw)
            marker_root = state / "provider-untitled"
            marker_root.mkdir(mode=0o700)
            marker = marker_root / "gone"
            marker.write_text("oldgeneration\n", encoding="utf-8")
            captured = inventory_core.capture_provider_untitled_generations(state)
            marker.write_text("newgeneration\n", encoding="utf-8")
            old = time.time() - 8 * 86400
            os.utime(marker, (old, old))
            inventory = {
                "source": "live",
                "stale": False,
                "sessions": [{"shpool_id_raw": "still-here"}],
            }
            config = {"state_dir": state}

            self.assertEqual(
                [],
                inventory_core._quarantine_orphaned_provider_untitled_markers(
                    config,
                    inventory,
                    retire_generations=captured,
                ),
            )
            self.assertEqual("newgeneration\n", marker.read_text(encoding="utf-8"))

            fresh = inventory_core.capture_provider_untitled_generations(state)
            moved = inventory_core._quarantine_orphaned_provider_untitled_markers(
                config,
                inventory,
                retire_generations=fresh,
            )
            self.assertEqual("gone", moved[0]["marker"])
            self.assertFalse(marker.exists())

    def test_retained_titles_audit_and_dry_run_token_gate_prune(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".automatic-prune-", dir=REPO) as raw:
            base = Path(raw)
            config_path, config = self._private_config(base)
            active = uuid_for(85)
            orphan = uuid_for(86)
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": str(config_path)}, clear=False
            ):
                inventory_core.mutate_canonical_automatic_title(
                    config, "codex", active, "Active Task Name"
                )
                inventory_core.mutate_canonical_automatic_title(
                    config, "claude", orphan, "Retained Closed Task"
                )
                live, _, _, _ = self._caller_fixture(config, active)
                audit = inventory_core.audit_automatic_titles(config, live)
                self.assertTrue(audit["dry_run"])
                self.assertEqual([f"claude:{orphan}"], audit["orphans"])
                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "stale or does not match"
                ):
                    inventory_core.prune_automatic_titles(config, live, "0" * 64)
                pruned = inventory_core.prune_automatic_titles(
                    config, live, audit["prune_token"]
                )
                self.assertEqual([f"claude:{orphan}"], pruned["pruned"])
                self.assertEqual(
                    {f"codex:{active}": "Active Task Name"},
                    inventory_core.canonical_automatic_titles(config),
                )

    def test_title_prune_recollects_before_applying_the_dry_run_token(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".automatic-prune-", dir=REPO) as raw:
            base = Path(raw)
            config_path, config = self._private_config(base)
            first = uuid_for(88)
            newly_active = uuid_for(89)
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": str(config_path)}, clear=False
            ):
                inventory_core.mutate_canonical_automatic_title(
                    config, "codex", newly_active, "Retained New Session"
                )
                old_inventory, _, _, _ = self._caller_fixture(config, first)
                current_inventory, _, _, _ = self._caller_fixture(config, newly_active)
                audit = inventory_core.audit_automatic_titles(config, old_inventory)
                calls: list[str] = []

                def revalidate() -> dict:
                    calls.append("under-lock")
                    return current_inventory

                with self.assertRaisesRegex(
                    inventory_core.CollectionError, "stale or does not match"
                ):
                    inventory_core.prune_automatic_titles(
                        config,
                        old_inventory,
                        audit["prune_token"],
                        revalidate_inventory=revalidate,
                    )

                self.assertEqual(["under-lock"], calls)
                self.assertEqual(
                    {f"codex:{newly_active}": "Retained New Session"},
                    inventory_core.canonical_automatic_titles(config),
                )

    def test_a_human_rename_refuses_every_later_automatic_rename(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".name-owner-", dir=REPO) as raw:
            base = Path(raw)
            config_path, config = self._private_config(base)
            exact = uuid_for(91)
            inventory, table, environment, current_pid = self._caller_fixture(
                config, exact
            )
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": str(config_path)}, clear=False
            ):
                inventory_core.mutate_canonical_alias(
                    config, "codex", exact, "the operator named this"
                )
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    "human",
                    document["name_ownership"][f"codex:{exact}"]["owner"],
                )
                self.assertEqual(
                    {
                        "owner": "human",
                        "at": document["name_ownership"][f"codex:{exact}"]["at"],
                    },
                    inventory_core.canonical_name_ownership(config)[f"codex:{exact}"],
                )
                for message, call in (
                    (
                        "explicit local alias",
                        lambda: inventory_core.mutate_canonical_automatic_title(
                            config, "codex", exact, "Some Automatic Name"
                        ),
                    ),
                    (
                        "a human name owns this session",
                        lambda: inventory_core.self_name_automatic_title(
                            config,
                            "Some Automatic Name",
                            inventory=inventory,
                            process_table=table,
                            environ=environment,
                            current_pid=current_pid,
                        ),
                    ),
                ):
                    with self.assertRaisesRegex(
                        inventory_core.CollectionError, message
                    ):
                        call()
                # The reset drops the alias. The override is not a display
                # tier, it outlives the name it was recorded for.
                inventory_core.mutate_canonical_alias(config, "codex", exact, None)
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual({}, document["aliases"])
                self.assertEqual(
                    "human",
                    document["name_ownership"][f"codex:{exact}"]["owner"],
                )
                for call in (
                    lambda: inventory_core.mutate_canonical_automatic_title(
                        config, "codex", exact, "Some Automatic Name", overwrite=True
                    ),
                    lambda: inventory_core.self_name_automatic_title(
                        config,
                        "Some Automatic Name",
                        inventory=inventory,
                        process_table=table,
                        environ=environment,
                        current_pid=current_pid,
                    ),
                ):
                    with self.assertRaisesRegex(
                        inventory_core.CollectionError,
                        "a human name owns this session",
                    ):
                        call()
                self.assertEqual(
                    {},
                    json.loads(config_path.read_text(encoding="utf-8")).get(
                        "automatic_titles", {}
                    ),
                )

    def test_a_self_name_keeps_naming_itself_and_prunes_its_own_claim(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".name-claim-", dir=REPO) as raw:
            base = Path(raw)
            config_path, config = self._private_config(base)
            exact = uuid_for(92)
            inventory, table, environment, current_pid = self._caller_fixture(
                config, exact
            )
            with mock.patch.dict(
                os.environ, {"SESSION_KIT_CONFIG": str(config_path)}, clear=False
            ):
                for title in ("Session Kit Updates", "Session Kit Release"):
                    inventory_core.self_name_automatic_title(
                        config,
                        title,
                        inventory=inventory,
                        process_table=table,
                        environ=environment,
                        current_pid=current_pid,
                    )
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    "automatic",
                    document["name_ownership"][f"codex:{exact}"]["owner"],
                )
                # A dead conversation's spent claim is pruned with its title;
                # a human override in the same sweep is not.
                human = uuid_for(93)
                inventory_core.mutate_canonical_alias(
                    config, "codex", human, "the operator named this"
                )
                empty = inventory_core.build_inventory(
                    *inventory_fixture(0), now=1_800_000_000
                )
                audit = inventory_core.audit_automatic_titles(config, empty)
                inventory_core.prune_automatic_titles(
                    config, empty, audit["prune_token"]
                )
                document = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertNotIn(f"codex:{exact}", document.get("name_ownership", {}))
                self.assertEqual(
                    "human",
                    document["name_ownership"][f"codex:{human}"]["owner"],
                )


class CanonicalAliasTests(unittest.TestCase):
    def test_new_alias_writer_serializes_with_pinned_config_lock_writer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".alias-lock-", dir=REPO) as raw:
            base = Path(raw)
            base.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "inventory.json"
            config_path.write_text(
                json.dumps({"schema_version": 1, "aliases": {}}),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            config = {"state_dir": state}
            old_key = f"claude:{uuid_for(31)}"
            new_uuid = uuid_for(32)
            config_lock = state / "config.lock"
            config_lock.touch(mode=0o600)
            config_lock.chmod(0o600)
            held = config_lock.open("r+")
            fcntl.flock(held, fcntl.LOCK_EX)
            started = threading.Event()
            finished = threading.Event()
            errors: list[BaseException] = []

            def new_writer() -> None:
                started.set()
                try:
                    inventory_core.mutate_canonical_alias(
                        config, "codex", new_uuid, "New writer"
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(config_path)},
                clear=False,
            ):
                thread = threading.Thread(target=new_writer, daemon=True)
                try:
                    thread.start()
                    self.assertTrue(started.wait(1))
                    self.assertFalse(finished.wait(0.1))
                    old_document = json.loads(config_path.read_text(encoding="utf-8"))
                    old_document["aliases"][old_key] = "Pinned writer"
                    inventory_core.atomic_write_json(config_path, old_document)
                finally:
                    fcntl.flock(held, fcntl.LOCK_UN)
                    held.close()
                self.assertTrue(finished.wait(2))
                thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            stored = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual("Pinned writer", stored["aliases"][old_key])
            self.assertEqual("New writer", stored["aliases"][f"codex:{new_uuid}"])

    def test_explicit_runtime_migration_preserves_runtime_wins_then_retires_overlay(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".alias-migrate-", dir=REPO) as raw:
            base = Path(raw)
            base.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "inventory.json"
            exact = uuid_for(5)
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state_dir": str(state),
                        "command_timeout_seconds": 7,
                        "aliases": {f"claude:{exact}": "Config value"},
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            runtime = state / "aliases.json"
            runtime.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"claude:{exact}": "Visible runtime value",
                            f"codex:{uuid_for(6)}": "Runtime only",
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            config_preimage = config_path.read_bytes()
            runtime_preimage = runtime.read_bytes()
            config = {"state_dir": state}
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(config_path)},
                clear=False,
            ):
                migrated = inventory_core.migrate_runtime_aliases(config)
                repeated = inventory_core.migrate_runtime_aliases(config)
            stored = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(migrated["migrated"])
            self.assertTrue(repeated["already_migrated"])
            self.assertEqual(7, stored["command_timeout_seconds"])
            self.assertEqual(
                "Visible runtime value", stored["aliases"][f"claude:{exact}"]
            )
            self.assertFalse(runtime.exists())
            archive = state / "aliases.json.migrated-v1"
            source_backup = state / "aliases.json.pre-migration-v1"
            config_backup = config_path.with_name(
                "inventory.json.pre-runtime-alias-migration-v1"
            )
            self.assertEqual(runtime_preimage, archive.read_bytes())
            self.assertEqual(runtime_preimage, source_backup.read_bytes())
            self.assertEqual(config_preimage, config_backup.read_bytes())
            self.assertEqual(0o600, archive.stat().st_mode & 0o777)
            self.assertEqual(0o600, source_backup.stat().st_mode & 0o777)
            self.assertEqual(0o600, config_backup.stat().st_mode & 0o777)

    def test_runtime_migration_resumes_after_config_publish_crash(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".alias-resume-", dir=REPO) as raw:
            base = Path(raw)
            base.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "inventory.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state_dir": str(state),
                        "aliases": {f"claude:{uuid_for(40)}": "Config"},
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            runtime = state / "aliases.json"
            runtime.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"claude:{uuid_for(40)}": "Runtime",
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            config_preimage = config_path.read_bytes()
            runtime_preimage = runtime.read_bytes()
            archive = state / "aliases.json.migrated-v1"
            real_replace = os.replace

            def fail_archive_publish(source: str, target: str) -> None:
                if Path(source) == runtime and Path(target) == archive:
                    raise OSError("injected archive publish failure")
                real_replace(source, target)

            config = {"state_dir": state}
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(config_path)},
                clear=False,
            ):
                with mock.patch.object(
                    inventory_core.os,
                    "replace",
                    side_effect=fail_archive_publish,
                ):
                    with self.assertRaisesRegex(
                        OSError, "injected archive publish failure"
                    ):
                        inventory_core.migrate_runtime_aliases(config)
                resumed = inventory_core.migrate_runtime_aliases(config)
            self.assertTrue(resumed["migrated"])
            self.assertFalse(runtime.exists())
            self.assertEqual(runtime_preimage, archive.read_bytes())
            self.assertEqual(
                runtime_preimage,
                (state / "aliases.json.pre-migration-v1").read_bytes(),
            )
            self.assertEqual(
                config_preimage,
                config_path.with_name(
                    "inventory.json.pre-runtime-alias-migration-v1"
                ).read_bytes(),
            )
            self.assertEqual(
                "Runtime",
                json.loads(config_path.read_text(encoding="utf-8"))["aliases"][
                    f"claude:{uuid_for(40)}"
                ],
            )

    def test_runtime_migration_accepts_only_byte_exact_rollback_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".alias-rollback-", dir=REPO) as raw:
            base = Path(raw)
            base.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "inventory.json"
            config_path.write_text(
                json.dumps({"schema_version": 1, "aliases": {}}),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            runtime = state / "aliases.json"
            runtime.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"codex:{uuid_for(41)}": "Rollback value",
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            config = {"state_dir": state}
            archive = state / "aliases.json.migrated-v1"
            config_backup = config_path.with_name(
                "inventory.json.pre-runtime-alias-migration-v1"
            )
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(config_path)},
                clear=False,
            ):
                inventory_core.migrate_runtime_aliases(config)
                config_path.write_bytes(config_backup.read_bytes())
                config_path.chmod(0o600)
                runtime.write_bytes(archive.read_bytes())
                runtime.chmod(0o600)
                retried = inventory_core.migrate_runtime_aliases(config)
                self.assertTrue(retried["migrated"])
                self.assertFalse(runtime.exists())
                self.assertEqual(
                    "Rollback value",
                    json.loads(config_path.read_text(encoding="utf-8"))["aliases"][
                        f"codex:{uuid_for(41)}"
                    ],
                )
                archive_preimage = archive.read_bytes()
                runtime.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "aliases": {
                                f"codex:{uuid_for(41)}": "Diverged",
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                runtime.chmod(0o600)
                with self.assertRaisesRegex(inventory_core.CollectionError, "differ"):
                    inventory_core.migrate_runtime_aliases(config)
            self.assertEqual(archive_preimage, archive.read_bytes())
            self.assertTrue(runtime.exists())

    def test_runtime_migration_repairs_an_exact_partial_config_rollback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".alias-partial-", dir=REPO) as raw:
            base = Path(raw)
            base.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "inventory.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"claude:{uuid_for(43)}": "Config value",
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            runtime = state / "aliases.json"
            runtime.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"claude:{uuid_for(43)}": "Runtime value",
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            config = {"state_dir": state}
            archive = state / "aliases.json.migrated-v1"
            config_backup = config_path.with_name(
                "inventory.json.pre-runtime-alias-migration-v1"
            )
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(config_path)},
                clear=False,
            ):
                inventory_core.migrate_runtime_aliases(config)
                archive_preimage = archive.read_bytes()
                config_path.write_bytes(config_backup.read_bytes())
                config_path.chmod(0o600)
                repaired = inventory_core.migrate_runtime_aliases(config)
            self.assertTrue(repaired["migrated"])
            self.assertFalse(repaired["already_migrated"])
            self.assertFalse(runtime.exists())
            self.assertEqual(archive_preimage, archive.read_bytes())
            self.assertEqual(
                "Runtime value",
                json.loads(config_path.read_text(encoding="utf-8"))["aliases"][
                    f"claude:{uuid_for(43)}"
                ],
            )

    def test_absent_config_backup_is_a_non_publishable_rollback_sentinel(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix=".alias-absent-", dir=REPO) as raw:
            base = Path(raw)
            base.chmod(0o700)
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "inventory.json"
            runtime = state / "aliases.json"
            runtime.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "aliases": {
                            f"claude:{uuid_for(42)}": "Only runtime",
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime.chmod(0o600)
            config = {"state_dir": state}
            archive = state / "aliases.json.migrated-v1"
            backup = config_path.with_name(
                "inventory.json.pre-runtime-alias-migration-v1"
            )
            with mock.patch.dict(
                os.environ,
                {"SESSION_KIT_CONFIG": str(config_path)},
                clear=False,
            ):
                inventory_core.migrate_runtime_aliases(config)
                self.assertEqual(
                    inventory_core.ABSENT_ALIAS_CONFIG_BACKUP,
                    backup.read_bytes(),
                )
                config_path.unlink()
                runtime.write_bytes(archive.read_bytes())
                runtime.chmod(0o600)
                retried = inventory_core.migrate_runtime_aliases(config)
                self.assertTrue(retried["migrated"])
                self.assertEqual(
                    "Only runtime",
                    json.loads(config_path.read_text(encoding="utf-8"))["aliases"][
                        f"claude:{uuid_for(42)}"
                    ],
                )
                config_path.write_bytes(inventory_core.ABSENT_ALIAS_CONFIG_BACKUP)
                config_path.chmod(0o600)
                runtime.write_bytes(archive.read_bytes())
                runtime.chmod(0o600)
                with self.assertRaisesRegex(inventory_core.CollectionError, "diverged"):
                    inventory_core.migrate_runtime_aliases(config)
            self.assertEqual(
                inventory_core.ABSENT_ALIAS_CONFIG_BACKUP,
                config_path.read_bytes(),
            )


class CensusSelfExclusionTests(unittest.TestCase):
    """The census must never count its own process chain as a session's title.

    A collection verb runs inside the managed shell it is titling; before the
    exclusion, its python3 became native_title, the Idle-shell overlay never
    applied, and provider-exit reopen could never pass its gate."""

    TABLE = {
        50: {"comm": "shpool", "ppid": 1},
        100: {"comm": "bash", "ppid": 50, "cwd": "/srv/x"},
        200: {"comm": "bash", "ppid": 100},
    }

    def table_with_self(self) -> dict:
        table = {pid: dict(row) for pid, row in self.TABLE.items()}
        table[os.getpid()] = {"comm": "python3", "ppid": 200}
        return table

    def test_the_census_chain_walks_self_and_ancestors(self) -> None:
        from lib.sessionkit_inventory.collector import _census_chain

        chain = _census_chain(self.table_with_self())
        self.assertIn(os.getpid(), chain)
        self.assertIn(200, chain)
        self.assertIn(100, chain)

    def test_the_census_never_titles_the_shell_it_runs_in(self) -> None:
        from lib.sessionkit_inventory.collector import _census_chain
        from lib.sessionkit_inventory.model import _shell_title

        table = self.table_with_self()
        chain = _census_chain(table)
        tree = [100, 200, os.getpid()]
        self.assertEqual(
            ("Idle shell", "/srv/x", 100), _shell_title(tree, 100, table, chain)
        )
        # Without the exclusion the census's own python3 wins, the exact
        # mechanism that made reopen structurally impossible.
        self.assertEqual("python3", _shell_title(tree, 100, table)[0])

    def test_a_real_program_still_titles_the_shell(self) -> None:
        from lib.sessionkit_inventory.collector import _census_chain
        from lib.sessionkit_inventory.model import _shell_title

        table = self.table_with_self()
        table[300] = {"comm": "vim", "ppid": 100, "cwd": "/srv/x"}
        chain = _census_chain(table)
        self.assertEqual(
            ("vim", "/srv/x", 300),
            _shell_title([100, 200, os.getpid(), 300], 100, table, chain),
        )

    def test_a_ppid_cycle_terminates(self) -> None:
        from lib.sessionkit_inventory.collector import _census_chain

        table = {os.getpid(): {"comm": "python3", "ppid": os.getpid()}}
        self.assertEqual(frozenset({os.getpid()}), _census_chain(table))


class InventoryInputAndRecoveryTests(unittest.TestCase):
    def test_retained_launch_modes_accept_legacy_resume_and_fork_without_aliasing_source(
        self,
    ) -> None:
        def unknown_fixture() -> dict:
            fixture = inventory_core.build_inventory(
                *inventory_fixture(1), now=1_800_000_000
            )
            item = fixture["sessions"][0]
            item["provider"] = "unknown"
            item["display_provider"] = "unknown"
            item["identity"] = {
                "uuid": None,
                "pid": item["shpool_shell"]["pid"],
                "process_start_ticks": item["shpool_shell"]["process_start_ticks"],
                "provenance": "process tree",
                "confidence": "unknown",
            }
            return fixture

        with tempfile.TemporaryDirectory(prefix=".launch-modes-", dir=REPO) as raw:
            start_dir = Path(raw)
            start_dir.chmod(0o700)
            for mode in ("resume", "fork", "unknown"):
                with self.subTest(mode=mode):
                    for path in start_dir.iterdir():
                        path.unlink()
                    fixture = unknown_fixture()
                    item = fixture["sessions"][0]
                    shell = item["shpool_shell"]
                    daemon = fixture["daemon_generation"]
                    source_uuid = uuid_for(91)
                    (start_dir / "main").write_text(
                        f"claude\t{item['cwd']}\t{source_uuid}\t{mode}\n",
                        encoding="utf-8",
                    )
                    (start_dir / "main.expected").write_text(
                        f"claude\t{item['cwd']}\tboot-a\t"
                        f"{item['started_at_unix_ms']}\t{shell['pid']}\t"
                        f"{shell['process_start_ticks']}\t{daemon['pid']}\t"
                        f"{daemon['process_start_ticks']}\t{source_uuid}\t{mode}\n",
                        encoding="utf-8",
                    )
                    (start_dir / "main").chmod(0o600)
                    (start_dir / "main.expected").chmod(0o600)
                    inventory_core.apply_retained_setup_attributions(
                        fixture, start_dir=start_dir, boot_id="boot-a"
                    )
                    if mode == "unknown":
                        self.assertEqual("unknown", item["display_provider"])
                        self.assertNotIn("_terminal_identity_hint", item)
                    else:
                        self.assertEqual("claude", item["display_provider"])
                        self.assertTrue(item["setup_incomplete"])
                        if mode == "resume":
                            self.assertEqual(
                                {
                                    "provider": "claude",
                                    "uuid": source_uuid,
                                },
                                item["_terminal_identity_hint"],
                            )
                        else:
                            self.assertNotIn("_terminal_identity_hint", item)

    def test_exact_retained_start_proofs_add_display_only_attribution(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        expected = (
            ("s20260728-022604-447706", "claude"),
            ("s20260728-022232-387175", "codex"),
        )
        for item, (shpool_id, _) in zip(fixture["sessions"], expected):
            item["shpool_id"] = shpool_id
            item["shpool_id_raw"] = shpool_id
            item["display_shpool_id"] = shpool_id
            item["provider"] = "unknown"
            item["display_provider"] = "unknown"
            item["identity"] = {
                "uuid": None,
                "pid": item["shpool_shell"]["pid"],
                "process_start_ticks": item["shpool_shell"]["process_start_ticks"],
                "provenance": "process tree",
                "confidence": "unknown",
            }
            item["title"] = "Unresolved provider session"
            item["display_title"] = item["title"]

        with tempfile.TemporaryDirectory(prefix=".retained-setup-", dir=REPO) as raw:
            start_dir = Path(raw)
            start_dir.chmod(0o700)
            for item, (_, provider) in zip(fixture["sessions"], expected):
                shpool_id = item["shpool_id_raw"]
                cwd = item["cwd"]
                shell = item["shpool_shell"]
                generation = fixture["daemon_generation"]
                start = start_dir / shpool_id
                sidecar = start_dir / f"{shpool_id}.expected"
                start.write_text(f"{provider}\t{cwd}\t\n", encoding="utf-8")
                sidecar.write_text(
                    f"{provider}\t{cwd}\tboot-a\t"
                    f"{item['started_at_unix_ms']}\t{shell['pid']}\t"
                    f"{shell['process_start_ticks']}\t{generation['pid']}\t"
                    f"{generation['process_start_ticks']}\t\n",
                    encoding="utf-8",
                )
                start.chmod(0o600)
                sidecar.chmod(0o600)

            inventory_core.apply_retained_setup_attributions(
                fixture, start_dir=start_dir, boot_id="boot-a"
            )

        for item, (_, provider) in zip(fixture["sessions"], expected):
            self.assertEqual("unknown", item["provider"])
            self.assertEqual("unknown", item["identity"]["confidence"])
            self.assertIsNone(item["identity"]["uuid"])
            self.assertEqual("Unresolved provider session", item["title"])
            self.assertEqual(provider, item["display_provider"])
            self.assertTrue(item["setup_incomplete"])
            self.assertEqual(
                f"{provider.title()} pending",
                item["display_title"],
            )

    def test_retained_setup_attribution_rejects_unsafe_and_stale_records(
        self,
    ) -> None:
        def unknown_fixture() -> dict:
            fixture = inventory_core.build_inventory(
                *inventory_fixture(1), now=1_800_000_000
            )
            item = fixture["sessions"][0]
            item["provider"] = "unknown"
            item["display_provider"] = "unknown"
            item["identity"] = {
                "uuid": None,
                "pid": item["shpool_shell"]["pid"],
                "process_start_ticks": item["shpool_shell"]["process_start_ticks"],
                "provenance": "process tree",
                "confidence": "unknown",
            }
            return fixture

        with tempfile.TemporaryDirectory(prefix=".retained-reject-", dir=REPO) as raw:
            start_dir = Path(raw)
            start_dir.chmod(0o700)

            def write_records(fixture: dict, *, boot: str = "boot-a") -> None:
                item = fixture["sessions"][0]
                generation = fixture["daemon_generation"]
                start = start_dir / "main"
                sidecar = start_dir / "main.expected"
                start.write_text(f"claude\t{item['cwd']}\t\n", encoding="utf-8")
                sidecar.write_text(
                    f"claude\t{item['cwd']}\t{boot}\t"
                    f"{item['started_at_unix_ms']}\t"
                    f"{item['shpool_shell']['pid']}\t"
                    f"{item['shpool_shell']['process_start_ticks']}\t"
                    f"{generation['pid']}\t"
                    f"{generation['process_start_ticks']}\t\n",
                    encoding="utf-8",
                )
                start.chmod(0o600)
                sidecar.chmod(0o600)

            for label in (
                "stale boot",
                "mismatched shell",
                "mismatched identity",
                "mismatched cwd",
                "mismatched start time",
                "mismatched daemon",
                "mismatched side provider",
                "mismatched uuid",
                "wrong file mode",
                "symlink",
                "wrong owner",
            ):
                with self.subTest(label=label):
                    for path in start_dir.iterdir():
                        path.unlink()
                    fixture = unknown_fixture()
                    write_records(
                        fixture,
                        boot="old-boot" if label == "stale boot" else "boot-a",
                    )
                    if label == "mismatched shell":
                        fixture["sessions"][0]["shpool_shell"]["pid"] += 1
                    elif label == "mismatched identity":
                        fixture["sessions"][0]["identity"]["confidence"] = "exact"
                    elif label == "mismatched cwd":
                        fixture["sessions"][0]["cwd"] += "-changed"
                    elif label == "mismatched start time":
                        fixture["sessions"][0]["started_at_unix_ms"] += 1
                    elif label == "mismatched daemon":
                        fixture["daemon_generation"]["process_start_ticks"] += 1
                    elif label == "mismatched side provider":
                        sidecar = start_dir / "main.expected"
                        sidecar.write_text(
                            sidecar.read_text(encoding="utf-8").replace(
                                "claude\t", "codex\t", 1
                            ),
                            encoding="utf-8",
                        )
                    elif label == "mismatched uuid":
                        sidecar = start_dir / "main.expected"
                        sidecar.write_text(
                            sidecar.read_text(encoding="utf-8").replace(
                                "\t\n",
                                f"\t{uuid_for(99)}\n",
                            ),
                            encoding="utf-8",
                        )
                    elif label == "wrong file mode":
                        (start_dir / "main.expected").chmod(0o644)
                    elif label == "symlink":
                        target = start_dir / "target"
                        target.write_text(
                            (start_dir / "main.expected").read_text(),
                            encoding="utf-8",
                        )
                        target.chmod(0o600)
                        (start_dir / "main.expected").unlink()
                        os.symlink(target, start_dir / "main.expected")
                    owner = (
                        mock.patch.object(
                            inventory_core.os,
                            "geteuid",
                            return_value=os.geteuid() + 1,
                        )
                        if label == "wrong owner"
                        else contextlib.nullcontext()
                    )
                    with owner:
                        inventory_core.apply_retained_setup_attributions(
                            fixture,
                            start_dir=start_dir,
                            boot_id="boot-a",
                        )
                    item = fixture["sessions"][0]
                    self.assertEqual("unknown", item["display_provider"])
                    self.assertFalse(item["setup_incomplete"])

    def test_guard_live_permits_unrelated_unknown_provider_row(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        self.assertTrue(inventory_core.strict_live_inventory(fixture))
        unknown = copy.deepcopy(fixture["sessions"][0])
        unknown_id = "s20260728-022232-387175"
        unknown["row"] = 3
        unknown["shpool_id"] = unknown_id
        unknown["shpool_id_raw"] = unknown_id
        unknown["display_shpool_id"] = unknown_id
        unknown["provider"] = "unknown"
        unknown["identity"] = {
            "uuid": None,
            "pid": unknown["shpool_shell"]["pid"],
            "process_start_ticks": unknown["shpool_shell"]["process_start_ticks"],
            "provenance": "process tree",
            "confidence": "unknown",
        }
        fixture["sessions"].append(unknown)

        self.assertFalse(inventory_core.strict_live_inventory(fixture))
        self.assertTrue(inventory_core.guard_live_inventory(fixture))
        known = inventory_core.lookup(fixture, "main")
        self.assertIsNotNone(known)
        self.assertEqual("claude", known["provider"])
        self.assertEqual("exact", known["identity"]["confidence"])

    def test_guard_allows_managed_outside_duplicate_but_strict_rejects_it(
        self,
    ) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        managed = fixture["sessions"][0]
        valid_outside = {
            "provider": "claude",
            "identity": {
                "uuid": uuid_for(90),
                "pid": 9000,
                "process_start_ticks": 90000,
                "provenance": "claude agents --json",
                "confidence": "exact",
            },
        }
        fixture["outside_agents"] = [valid_outside]
        self.assertTrue(inventory_core.guard_live_inventory(fixture))
        self.assertTrue(inventory_core.strict_live_inventory(fixture))

        duplicate = copy.deepcopy(fixture)
        duplicate["outside_agents"][0]["provider"] = managed["provider"]
        duplicate["outside_agents"][0]["identity"] = copy.deepcopy(managed["identity"])
        self.assertTrue(inventory_core.guard_live_inventory(duplicate))
        self.assertFalse(inventory_core.strict_live_inventory(duplicate))

        duplicate_outside = copy.deepcopy(duplicate)
        duplicate_outside["outside_agents"].append(
            copy.deepcopy(duplicate_outside["outside_agents"][0])
        )
        self.assertFalse(inventory_core.guard_live_inventory(duplicate_outside))

        mutations = (
            lambda item: item.update(provider="shell"),
            lambda item: item["identity"].update(uuid="bad"),
            lambda item: item["identity"].update(pid=0),
            lambda item: item["identity"].update(process_start_ticks=True),
            lambda item: item["identity"].update(confidence="unknown"),
        )
        for mutate in mutations:
            candidate = copy.deepcopy(fixture)
            mutate(candidate["outside_agents"][0])
            self.assertFalse(inventory_core.guard_live_inventory(candidate))
            self.assertFalse(inventory_core.strict_live_inventory(candidate))

    def test_guard_live_rejects_stale_partial_and_malformed_snapshots(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        malformed: list[tuple[str, object]] = []

        def changed(label: str, mutate: object) -> None:
            candidate = copy.deepcopy(fixture)
            mutate(candidate)
            malformed.append((label, candidate))

        changed("cache source", lambda value: value.update(source="cache"))
        changed("stale", lambda value: value.update(stale=True))
        changed("warning", lambda value: value["warnings"].append("partial"))
        changed("warnings type", lambda value: value.update(warnings=None))
        changed("schema version", lambda value: value.update(schema_version=999))
        changed("missing sessions", lambda value: value.pop("sessions"))
        changed(
            "daemon boolean pid",
            lambda value: value["daemon_generation"].update(pid=True),
        )
        changed(
            "daemon generation",
            lambda value: value["daemon_generation"].update(process_start_ticks=0),
        )
        changed(
            "shell generation",
            lambda value: value["sessions"][0]["shpool_shell"].update(pid=True),
        )
        changed(
            "known confidence",
            lambda value: value["sessions"][0]["identity"].update(confidence="unknown"),
        )
        changed(
            "known uuid",
            lambda value: value["sessions"][0]["identity"].update(uuid="bad"),
        )
        changed(
            "duplicate row",
            lambda value: value["sessions"][1].update(row=value["sessions"][0]["row"]),
        )
        changed(
            "duplicate raw id",
            lambda value: value["sessions"][1].update(
                shpool_id_raw=value["sessions"][0]["shpool_id_raw"],
                shpool_id=value["sessions"][0]["shpool_id"],
            ),
        )
        changed(
            "unknown provider",
            lambda value: value["sessions"][0].update(provider="other"),
        )
        changed("sessions type", lambda value: value.update(sessions={}))

        for label, candidate in malformed:
            with self.subTest(label=label):
                self.assertFalse(inventory_core.guard_live_inventory(candidate))
                self.assertFalse(inventory_core.strict_live_inventory(candidate))

    def test_malformed_json_and_duplicate_selectors_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".inventory-input-", dir=REPO) as raw:
            base = Path(raw)
            malformed = base / "malformed.json"
            malformed.write_text("{broken", encoding="utf-8")
            proc = run(
                ["python3", CORE_PATH, "render", "--input", malformed], check=False
            )
            self.assertNotEqual(0, proc.returncode)
            self.assertIn("cannot read inventory input", proc.stderr)

            fixture = inventory_core.build_inventory(
                *inventory_fixture(2), now=1_800_000_000
            )
            fixture["sessions"][1]["row"] = fixture["sessions"][0]["row"]
            duplicate = base / "duplicate.json"
            duplicate.write_text(json.dumps(fixture), encoding="utf-8")
            proc = run(
                ["python3", CORE_PATH, "lookup", "--input", duplicate, "1"],
                check=False,
            )
            self.assertNotEqual(0, proc.returncode)
            self.assertIn("invalid or duplicate selectors", proc.stderr)

    def test_frozen_lookup_targets_exact_row_and_shpool_id(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(10), now=1_800_000_000
        )
        expected_tenth = fixture["sessions"][9]
        by_row = inventory_core.lookup(fixture, "10")
        self.assertIsNotNone(by_row)
        self.assertEqual(expected_tenth["shpool_id"], by_row["shpool_id"])
        by_id = inventory_core.lookup(fixture, "main2")
        self.assertIsNotNone(by_id)
        self.assertEqual(
            next(
                item["row"]
                for item in fixture["sessions"]
                if item["shpool_id"] == "main2"
            ),
            by_id["row"],
        )
        self.assertIsNone(inventory_core.lookup(fixture, "missing"))

    def test_recovery_commands_require_and_preserve_exact_uuid(self) -> None:
        exact = uuid_for(42)
        claude = inventory_core.recovery_spec("claude", exact, "/srv/a b")
        codex = inventory_core.recovery_spec("codex", exact)
        self.assertEqual(["claude", "--resume", exact], claude["argv"])
        self.assertEqual(["codex", "--no-alt-screen", "resume", exact], codex["argv"])
        joined = json.dumps([claude, codex]).casefold()
        for fallback in ("continue", "latest", "newest", "--last"):
            self.assertNotIn(fallback, joined)
        with self.assertRaises(ValueError):
            inventory_core.recovery_spec("codex", "not-an-id")

    def test_codex_print_mode_never_launches_and_has_no_fallback(self) -> None:
        helper = REPO / "bin" / "codex_resume_here"
        exact = uuid_for(88)
        proc = run([helper, "--print", "--cwd", "/srv/a b", exact])
        self.assertEqual(
            f"cd -- /srv/a\\ b && codex --no-alt-screen resume {exact}\n",
            proc.stdout,
        )
        invalid = run([helper, "--print", "latest"], check=False)
        self.assertEqual(2, invalid.returncode)

    def test_strict_live_refuses_a_last_known_good_cache_without_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".strict-live-", dir=REPO) as raw:
            base = Path(raw)
            state = base / "state"
            state.mkdir()
            cached = inventory_core.build_inventory(
                *inventory_fixture(1), now=1_800_000_000
            )
            (state / "inventory.json").write_text(json.dumps(cached), encoding="utf-8")
            config = base / "inventory.json"
            config.write_text(
                json.dumps({"schema_version": 1, "state_dir": str(state)}),
                encoding="utf-8",
            )
            env = {
                "SESSION_KIT_CONFIG": str(config),
                "SESSION_KIT_SHPOOL_CMD": str(base / "missing-shpool"),
                "SESSION_KIT_STATE_DIR": str(state),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            direct = run(
                [
                    "python3",
                    CORE_PATH,
                    "snapshot",
                    "--strict-live",
                    "--no-write",
                ],
                env=env,
                check=False,
            )
            self.assertEqual(3, direct.returncode)
            self.assertEqual("", direct.stdout)
            self.assertIn("strict live snapshot unavailable", direct.stderr)
            wrapper = run(
                [REPO / "bin/shpool_status", "--strict-json"],
                env=env,
                check=False,
            )
            self.assertEqual(3, wrapper.returncode)
            self.assertEqual("", wrapper.stdout)
            guard = run(
                [REPO / "bin/shpool_status", "--guard-json"],
                env=env,
                check=False,
            )
            self.assertEqual(3, guard.returncode)
            self.assertEqual("", guard.stdout)
            self.assertIn("guard live snapshot unavailable", guard.stderr)


class InventoryRenderingTests(unittest.TestCase):
    def test_list_screen_in_a_real_80_column_terminal_hides_machine_titles(
        self,
    ) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        person, machine = fixture["sessions"]
        person.update(
            {
                "account_alias": "account-name-that-is-too-long",
                "model": "model-name-that-is-much-too-long-for-a-terminal",
            }
        )
        machine.update(
            {
                "origin": "machine",
                "title": "SECRET MACHINE SESSION TITLE",
                "display_title": "SECRET MACHINE SESSION TITLE",
            }
        )

        with tempfile.TemporaryDirectory(prefix=".list-pty-", dir=REPO) as raw:
            snapshot = Path(raw) / "inventory.json"
            snapshot.write_text(json.dumps(fixture), encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "SESSION_KIT_NO_COLOR": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            environment.pop("COLUMNS", None)

            pid, descriptor = pty.fork()
            if pid == 0:
                try:
                    os.execve(
                        REPO / "bin" / "shpool_status",
                        [
                            os.fspath(REPO / "bin" / "shpool_status"),
                            "--render-file",
                            os.fspath(snapshot),
                        ],
                        environment,
                    )
                finally:
                    os._exit(127)

            fcntl.ioctl(
                descriptor,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", 24, 80, 0, 0),
            )
            output = bytearray()
            deadline = time.monotonic() + 10
            status = None
            try:
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([descriptor], [], [], 0.05)
                    if ready:
                        try:
                            chunk = os.read(descriptor, 65536)
                        except OSError as exc:
                            if exc.errno != errno.EIO:
                                raise
                            chunk = b""
                        if chunk:
                            output.extend(chunk)
                    waited_pid, status = os.waitpid(pid, os.WNOHANG)
                    if waited_pid == pid:
                        break
                if status is None:
                    os.kill(pid, 9)
                    _, status = os.waitpid(pid, 0)
                    self.fail("list renderer did not exit within ten seconds")
            finally:
                os.close(descriptor)

            self.assertEqual(0, os.waitstatus_to_exitcode(status))
            visible = output.decode(errors="replace").replace("\r\n", "\n")
            self.assertNotIn("SECRET MACHINE SESSION TITLE", visible)
            self.assertIn("1 machine session", visible)
            self.assertIn("| CLD |", visible)
            # Both columns remain present, and the compact time gives their
            # values more room without ever deleting the time column.
            self.assertIn("account-nam…", visible)
            self.assertIn("model-nam…", visible)
            self.assertIn("| working | pending", visible)
            self.assertTrue(
                all(
                    inventory_core._display_width(line) <= 79
                    for line in visible.splitlines()
                ),
                visible,
            )

    def test_a_fresh_render_does_not_rewrite_the_inventory_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".render-read-only-", dir=REPO) as raw:
            base = Path(raw)
            state = base / "state"
            state.mkdir()
            inventory_path = state / "inventory.json"
            inventory_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "cache",
                        "stale": False,
                        "sessions": [],
                        "outside_agents": [],
                    }
                ),
                encoding="utf-8",
            )
            (state / "sentinel").write_bytes(b"listing must not touch this tree\n")
            config = base / "config.json"
            config.write_text(
                json.dumps({"schema_version": 1, "state_dir": str(state)}),
                encoding="utf-8",
            )

            proc = base / "proc"
            daemon = proc / "10"
            daemon.mkdir(parents=True)
            stat_fields = ["S", "1", *("0" for _ in range(17)), "700"]
            (daemon / "stat").write_text(
                f"10 (shpool) {' '.join(stat_fields)}\n", encoding="utf-8"
            )
            (daemon / "comm").write_text("shpool\n", encoding="utf-8")
            (daemon / "cmdline").write_bytes(b"/usr/bin/shpool\0daemon\0")
            (daemon / "environ").write_bytes(b"")
            (daemon / "cwd").symlink_to(base)
            shpool_json = base / "shpool.json"
            shpool_json.write_text('{"sessions":[]}\n', encoding="utf-8")
            claude_json = base / "claude.json"
            claude_json.write_text("[]\n", encoding="utf-8")
            boot_id = base / "boot-id"
            boot_id.write_text("fixture-boot\n", encoding="utf-8")
            fake_shpool = base / "shpool"
            fake_shpool.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_shpool.chmod(0o755)

            def tree_snapshot() -> dict[str, tuple[int, int, bytes]]:
                return {
                    str(path.relative_to(state)): (
                        path.stat().st_mode,
                        path.stat().st_mtime_ns,
                        path.read_bytes(),
                    )
                    for path in sorted(state.rglob("*"))
                    if path.is_file()
                }

            before = tree_snapshot()
            rendered = run(
                [REPO / "bin" / "sp", "list"],
                env={
                    "SESSION_KIT_CONFIG": str(config),
                    "SESSION_KIT_SHPOOL_CMD": str(fake_shpool),
                    "SESSION_KIT_SHPOOL_JSON_FILE": str(shpool_json),
                    "SESSION_KIT_CLAUDE_JSON_FILE": str(claude_json),
                    "SESSION_KIT_PROC_ROOT": str(proc),
                    "SESSION_KIT_TESTING": "1",
                    "SESSION_KIT_BOOT_ID_FILE": str(boot_id),
                    "SESSION_KIT_STATE_DIR": str(state),
                    "SESSION_KIT_AUTO_NAME": "0",
                    "SESSION_KIT_NO_COLOR": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            self.assertIn("Sessions: none.", rendered.stdout)
            self.assertNotIn("Warning:", rendered.stdout)
            self.assertEqual(before, tree_snapshot())

    def test_rows_hide_operational_ids_and_use_semantic_colors(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        fixture["sessions"][0]["needs_you"] = True
        fixture["sessions"][0]["agent_status"] = "needs your reply"
        with mock.patch.object(inventory_core, "_color_enabled", return_value=True):
            rendered = inventory_core.render_inventory(fixture)
        visible = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
        for item in fixture["sessions"]:
            self.assertNotIn(f"[{item['display_shpool_id']}]", visible)
        self.assertIn("\x1b[36mCLD", rendered)
        self.assertIn("\x1b[36mCDX", rendered)
        self.assertIn("\x1b[32m 1\x1b[0m", rendered)
        self.assertIn("\x1b[32m 2\x1b[0m", rendered)
        # The collector keeps the provider's own words; the screen renders
        # the one term for the state.
        self.assertIn("\x1b[33mneeds you", rendered)
        self.assertNotIn("needs your reply", rendered)

    def test_renderer_used_by_picker_strips_csi_osc_and_all_control_bytes(self) -> None:
        malicious = {
            "stale": False,
            "sessions": [
                {
                    "row": 1,
                    "shpool_id": "main",
                    "display_shpool_id": "main",
                    "availability": "ready",
                    "provider": "codex",
                    "title": "safe\x1b[31mred\x1b]0;owned\x07title",
                    "cwd": "/srv/\x1b[2Jproject\tline\nbreak",
                    "process_age_seconds": 1,
                    "recent_output_age_seconds": 65,
                    "agent_status": "working",
                    "needs_you": False,
                    "subagents": [],
                }
            ],
            "outside_agents": [],
        }
        with mock.patch.dict(os.environ, {"SESSION_KIT_NO_COLOR": "1"}, clear=False):
            rendered = inventory_core.render_inventory(malicious)
        self.assertIn("safe", rendered)
        self.assertIn("owned", rendered)
        self.assertIn("last active 1m ago", rendered)
        # One time on a row, not two: the process age moved to `sp detail`.
        self.assertNotIn("process age", rendered)
        self.assertNotIn("recent output", rendered)
        self.assertNotIn("project", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertFalse(
            any(
                unicodedata.category(character).startswith("C")
                for character in rendered
                if character != "\n"
            ),
            repr(rendered),
        )

    def test_non_tty_no_color_rendering_at_supported_widths(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(10), now=1_800_000_000
        )
        fixture["sessions"][0]["title"] = "Wide 漢字 title 👋 with accents e\u0301"
        for width in (60, 80, 100, 160):
            with self.subTest(width=width):
                old_columns = os.environ.get("COLUMNS")
                old_no_color = os.environ.get("SESSION_KIT_NO_COLOR")
                try:
                    os.environ["COLUMNS"] = str(width)
                    os.environ["SESSION_KIT_NO_COLOR"] = "1"
                    rendered = inventory_core.render_inventory(fixture)
                finally:
                    if old_columns is None:
                        os.environ.pop("COLUMNS", None)
                    else:
                        os.environ["COLUMNS"] = old_columns
                    if old_no_color is None:
                        os.environ.pop("SESSION_KIT_NO_COLOR", None)
                    else:
                        os.environ["SESSION_KIT_NO_COLOR"] = old_no_color
                self.assertNotIn("\x1b", rendered)
                self.assertIn("Ready", rendered)
                self.assertIn("Open elsewhere", rendered)
                self.assertIn("Claude", rendered)
                self.assertIn("Codex", rendered)
                # The retired headings must not come back on any width.
                self.assertNotIn("Available to open", rendered)
                self.assertNotIn("Already open in another window", rendered)
                self.assertNotIn("in /srv/", rendered)
                self.assertTrue(
                    all(
                        inventory_core._display_width(line) <= width - 1
                        for line in rendered.splitlines()
                    ),
                    rendered,
                )

    def test_terminal_width_is_used_when_columns_is_not_exported(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        fixture["sessions"][0]["title"] = (
            "A screenshot-sized title that must not wrap in the SSH picker"
        )
        with (
            mock.patch.dict(os.environ, {"SESSION_KIT_NO_COLOR": "1"}, clear=False),
            mock.patch.object(
                inventory_core.shutil,
                "get_terminal_size",
                return_value=os.terminal_size((72, 24)),
            ),
        ):
            os.environ.pop("COLUMNS", None)
            rendered = inventory_core.render_inventory(fixture)
        self.assertTrue(
            all(
                inventory_core._display_width(line) <= 71
                for line in rendered.splitlines()
            ),
            rendered,
        )

    def test_color_sequences_do_not_consume_the_display_cell_budget(self) -> None:
        fixture = inventory_core.build_inventory(
            *inventory_fixture(4), now=1_800_000_000
        )
        fixture["sessions"][0]["title"] = (
            "Styled wide 漢字 title that reaches the terminal boundary"
        )
        for width in (60, 80, 100, 160):
            with (
                self.subTest(width=width),
                mock.patch.dict(os.environ, {"COLUMNS": str(width)}, clear=False),
                mock.patch.object(inventory_core, "_color_enabled", return_value=True),
            ):
                rendered = inventory_core.render_inventory(fixture)
            self.assertIn("\x1b[", rendered)
            visible = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
            self.assertTrue(
                all(
                    inventory_core._display_width(line) <= width - 1
                    for line in visible.splitlines()
                ),
                visible,
            )


class ProviderLifecycleStateTests(unittest.TestCase):
    def test_provider_exit_requires_exact_committed_intake_generation(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".lifecycle-commit-", dir=REPO) as raw:
            root = Path(raw)
            root.chmod(0o700)
            marker = root / "prompt.intake_committed"
            conversation = uuid_for(91)
            generation = "aaaaaaaa-bbbb:123:456:789"
            payload = {
                "schema_version": 2,
                "status": "intake_committed",
                "provider": "codex",
                "session_id": conversation,
                "submission_key": "turn-1",
                "prompt_sha256": "a" * 64,
                "bytes": 12,
                "source_event_id": "b" * 64,
                "intake_msg_id": "c" * 8,
                "requirements_revision": 0,
                "requirements_digest": "d" * 64,
                "managed_generation": generation,
                "committed_unix_ms": 1_800_000_000_000,
            }
            marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            marker.chmod(0o600)
            environment = {
                "SESSION_KIT_LIFECYCLE_CONVERSATION_UUID": conversation,
                "SESSION_KIT_LIFECYCLE_INTAKE_COMMIT": os.fspath(marker),
                "SESSION_KIT_MANAGED_GENERATION": generation,
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                exact = inventory_core._lifecycle_committed_conversation(
                    provider="codex",
                    boot_id="aaaaaaaa-bbbb",
                    shell_pid=123,
                    shell_start=456,
                )
            self.assertEqual(conversation, exact)

            for field, changed in (
                ("provider", "claude"),
                ("session_id", uuid_for(92)),
                ("managed_generation", "aaaaaaaa-bbbb:123:999:789"),
                ("prompt_sha256", "not-a-digest"),
            ):
                broken = dict(payload)
                broken[field] = changed
                marker.write_text(json.dumps(broken) + "\n", encoding="utf-8")
                marker.chmod(0o600)
                with (
                    mock.patch.dict(os.environ, environment, clear=False),
                    self.assertRaises(inventory_core.CollectionError),
                ):
                    inventory_core._lifecycle_committed_conversation(
                        provider="codex",
                        boot_id="aaaaaaaa-bbbb",
                        shell_pid=123,
                        shell_start=456,
                    )

            marker.unlink()
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                self.assertRaisesRegex(inventory_core.CollectionError, "unavailable"),
            ):
                inventory_core._lifecycle_committed_conversation(
                    provider="codex",
                    boot_id="aaaaaaaa-bbbb",
                    shell_pid=123,
                    shell_start=456,
                )

    def test_conversation_uuid_is_persisted_in_provider_exit_state(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-conversation-", dir=REPO
        ) as raw:
            value = lifecycle_state.record_provider_exit(
                Path(raw),
                session_id="main",
                boot_id="aaaaaaaa-bbbb",
                shell_pid=123,
                shell_start_ticks=456,
                provider="codex",
                conversation_uuid=uuid_for(93),
                exit_code=0,
                input_tracking=True,
                now_monotonic_ns=999,
            )
            self.assertEqual(uuid_for(93), value["conversation_uuid"])
            self.assertEqual(4, value["schema_version"])
            self.assertRegex(value["record_generation"], r"^[0-9a-f]{64}$")
            with self.assertRaisesRegex(
                inventory_core.CollectionError, "conversation changed"
            ):
                lifecycle_state.record_provider_exit(
                    Path(raw),
                    session_id="main",
                    boot_id="aaaaaaaa-bbbb",
                    shell_pid=123,
                    shell_start_ticks=456,
                    provider="codex",
                    conversation_uuid=uuid_for(94),
                    exit_code=0,
                    input_tracking=True,
                    now_monotonic_ns=1000,
                )


class WorkerLaunchGateTests(unittest.TestCase):
    def test_inventory_row_carries_only_bound_process_model_and_launch_key(
        self,
    ) -> None:
        fixture = list(inventory_fixture(1))
        table = fixture[2]
        provider_pid = next(
            pid for pid, row in table.items() if row["comm"] == "claude"
        )
        table[provider_pid]["cmdline"] = [
            "/usr/bin/claude",
            "--model",
            "claude-opus-test",
        ]
        table[provider_pid]["requested_model"] = "claude-opus-test"
        table[provider_pid]["launch_idempotency_key"] = "worker:research:1"
        inventory = inventory_core.build_inventory(*fixture)
        row = inventory["sessions"][0]
        self.assertEqual("claude-opus-test", row["actual_model"])
        self.assertEqual("worker:research:1", row["launch_idempotency_key"])

    def test_inventory_binds_codex_cli_evidence_under_exact_managed_shell(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        table = fixture[2]
        provider_pid = next(pid for pid, row in table.items() if row["comm"] == "codex")
        shell_pid = table[provider_pid]["ppid"]
        table[provider_pid]["cmdline"] = ["/usr/bin/codex", "app-server"]
        cli_pid = max(table) + 1
        table[cli_pid] = process(cli_pid, shell_pid, "node")
        table[cli_pid]["cmdline"] = [
            "/usr/bin/node",
            "/opt/codex",
            "--model",
            "gpt-codex-test",
        ]
        table[cli_pid]["requested_model"] = "gpt-codex-test"
        table[cli_pid]["launch_idempotency_key"] = "worker:implementation:1"
        inventory = inventory_core.build_inventory(*fixture)
        row = inventory["sessions"][0]
        self.assertEqual("gpt-codex-test", row["actual_model"])
        self.assertEqual("worker:implementation:1", row["launch_idempotency_key"])

    def test_lifecycle_caller_must_descend_from_exact_managed_shell(self) -> None:
        current_pid = os.getpid()
        shell_pid = current_pid + 10_000
        table = {
            current_pid: process(current_pid, shell_pid, "python3"),
            shell_pid: process(
                shell_pid,
                1,
                "bash",
                session_name="main2",
                start_ticks=987_654,
            ),
        }
        with (
            mock.patch.object(
                inventory_core, "_require_supported_platform", return_value="linux"
            ),
            mock.patch.object(inventory_core, "scan_process_table", return_value=table),
        ):
            inventory_core._prove_lifecycle_caller("main2", shell_pid, 987_654)
            with self.assertRaisesRegex(
                inventory_core.CollectionError, "outside the exact"
            ):
                inventory_core._prove_lifecycle_caller("main2", shell_pid, 987_655)
            table[shell_pid]["session_name"] = "main3"
            with self.assertRaisesRegex(
                inventory_core.CollectionError, "outside the exact"
            ):
                inventory_core._prove_lifecycle_caller("main2", shell_pid, 987_654)

    def test_state_is_private_minimal_and_first_input_is_permanent(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".lifecycle-state-", dir=REPO) as raw:
            state_dir = Path(raw)
            session_id = "s20260730-220500-19"
            boot_id = "11111111-2222-3333-4444-555555555555"
            created = lifecycle_state.record_provider_exit(
                state_dir,
                session_id=session_id,
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                provider="codex",
                exit_code=0,
                input_tracking=True,
                now_monotonic_ns=1_800_000_000_000,
            )
            path = lifecycle_state.lifecycle_path(state_dir, session_id)
            self.assertIsNotNone(path)
            assert path is not None
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertNotIn(session_id, path.name)
            self.assertNotEqual(
                hashlib.sha256(session_id.encode()).hexdigest(),
                path.stem,
            )
            payload = path.read_text(encoding="utf-8")
            self.assertNotIn(session_id, payload)
            self.assertNotIn("/srv/", payload)
            self.assertNotIn("00000000-0000-4000", payload)
            self.assertTrue(created["input_tracking"])
            first = lifecycle_state.update_state(
                state_dir,
                session_id=session_id,
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                event="user-input",
            )
            second = lifecycle_state.update_state(
                state_dir,
                session_id=session_id,
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                event="user-input",
            )
            self.assertTrue(first["user_input_after_exit"])
            self.assertTrue(second["user_input_after_exit"])
            kept = lifecycle_state.update_state(
                state_dir,
                session_id=session_id,
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                event="keep",
                keep=True,
            )
            self.assertTrue(kept["keep"])
            with self.assertRaisesRegex(
                inventory_core.CollectionError, "generation changed"
            ):
                lifecycle_state.update_state(
                    state_dir,
                    session_id=session_id,
                    boot_id=boot_id,
                    shell_pid=999,
                    shell_start_ticks=456,
                    event="user-input",
                )
            captured = lifecycle_state.capture_lifecycle_generations(state_dir)
            self.assertEqual(
                0,
                lifecycle_state.prune_inactive_state(
                    state_dir,
                    [session_id],
                    retire_generations=captured,
                ),
            )
            rewritten = lifecycle_state.update_state(
                state_dir,
                session_id=session_id,
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                event="keep",
                keep=True,
            )
            self.assertNotEqual(
                kept["record_generation"], rewritten["record_generation"]
            )
            self.assertEqual(
                0,
                lifecycle_state.prune_inactive_state(
                    state_dir,
                    [],
                    retire_generations=captured,
                ),
            )
            self.assertTrue(path.exists())
            current = lifecycle_state.capture_lifecycle_generations(state_dir)
            self.assertEqual(
                1,
                lifecycle_state.prune_inactive_state(
                    state_dir,
                    [],
                    retire_generations=current,
                ),
            )
            self.assertFalse(path.exists())

    def test_exact_recovery_retention_is_separate_from_generation_guard(self) -> None:
        """Only exact recovery has an age-based housekeeping lifetime."""
        with tempfile.TemporaryDirectory(prefix="session-kit-lifecycle-retain.") as raw:
            state_dir = Path(raw)
            lifecycle_dir = state_dir / "lifecycle"
            lifecycle_dir.mkdir(mode=0o700, parents=True)
            self.assertIsNotNone(
                lifecycle_state._lifecycle_secret(state_dir, create=True)
            )
            key = "ab" * 32
            plain_recent = lifecycle_dir / f"{key}.json"
            exact_recent = lifecycle_dir / f"{key}.exact.json"
            old_key = "cd" * 32
            plain_old = lifecycle_dir / f"{old_key}.json"
            exact_old = lifecycle_dir / f"{old_key}.exact.json"
            for index, path in enumerate(
                (plain_recent, exact_recent, plain_old, exact_old), start=1
            ):
                path.write_text(
                    json.dumps({"record_generation": f"{index:064x}"}) + "\n",
                    encoding="utf-8",
                )
                path.chmod(0o600)
            aged = time.time() - (lifecycle_state.EXACT_RECOVERY_RETENTION_SECONDS + 1)
            for path in (plain_old, exact_old):
                os.utime(path, (aged, aged))
            captured = lifecycle_state.capture_lifecycle_generations(state_dir)
            removed = lifecycle_state.prune_inactive_state(
                state_dir,
                [],
                retire_generations=captured,
            )
            self.assertEqual(3, removed)
            self.assertFalse(plain_recent.exists())
            self.assertTrue(exact_recent.exists())
            self.assertFalse(plain_old.exists())
            self.assertFalse(exact_old.exists())

    def test_exact_exit_overlay_keeps_shell_live_and_recovery_available(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        active = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        expected_uuid = uuid_for(1)
        root_pid = 1001
        provider_pid = 2001
        del fixture[2][provider_pid]
        fixture[3] = {}
        idle = inventory_core.build_inventory(*fixture, now=1_800_000_001)
        self.assertEqual("shell", idle["sessions"][0]["provider"])
        with tempfile.TemporaryDirectory(prefix=".lifecycle-overlay-", dir=REPO) as raw:
            state_dir = Path(raw)
            lifecycle_state.persist_last_exact(
                idle,
                active,
                state_dir=state_dir,
                boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            )
            cache_after_race = copy.deepcopy(idle)
            lifecycle_state.record_provider_exit(
                state_dir,
                session_id="main",
                boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                shell_pid=root_pid,
                shell_start_ticks=fixture[2][root_pid]["start_ticks"],
                provider="codex",
                exit_code=0,
                input_tracking=True,
                now_monotonic_ns=1_800_000_000_500,
            )
            lifecycle_state.apply_provider_exit_states(
                idle,
                cache_after_race,
                state_dir=state_dir,
                boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            )
        item = idle["sessions"][0]
        self.assertEqual("shell", item["provider"])
        self.assertEqual("codex", item["display_provider"])
        self.assertEqual("provider exited", item["agent_status"])
        self.assertEqual(active["sessions"][0]["title"], item["title"])
        self.assertEqual(expected_uuid, item["recovery"]["uuid"])
        self.assertEqual(
            {"provider": "codex", "uuid": expected_uuid},
            item["_terminal_identity_hint"],
        )
        self.assertFalse(item["needs_you"])

    def test_exit_overlay_survives_the_inventorys_own_helper_process(self) -> None:
        # Reported 2026-08-11: `r` at the provider-exit menu answered "exact
        # provider-exit recovery is unavailable" for a session whose retained
        # record held `claude --resume <uuid>`. The menu runs the collector
        # from inside the managed shell, so the collector saw its own helper
        # as the session's work, called the row running, and the overlay,
        # which only annotates idle shells, never attached the recovery.
        fixture = list(inventory_fixture(1, providers=("codex",)))
        active = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        expected_uuid = uuid_for(1)
        root_pid = 1001
        del fixture[2][2001]
        fixture[3] = {}
        # Exactly what the reopen looks like on the process table: this very
        # process, running under the managed shell.
        fixture[2][os.getpid()] = process(
            os.getpid(), root_pid, "python3", cwd="/srv/project-1"
        )
        idle = inventory_core.build_inventory(*fixture, now=1_800_000_001)
        self.assertEqual("shell", idle["sessions"][0]["provider"])
        self.assertEqual("Idle shell", idle["sessions"][0]["native_title"])
        self.assertEqual("idle", idle["sessions"][0]["agent_status"])
        boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-self-helper-", dir=REPO
        ) as raw:
            state_dir = Path(raw)
            lifecycle_state.persist_last_exact(
                idle, active, state_dir=state_dir, boot_id=boot_id
            )
            lifecycle_state.record_provider_exit(
                state_dir,
                session_id="main",
                boot_id=boot_id,
                shell_pid=root_pid,
                shell_start_ticks=fixture[2][root_pid]["start_ticks"],
                provider="codex",
                exit_code=0,
                input_tracking=True,
                now_monotonic_ns=1_800_000_000_500,
            )
            lifecycle_state.apply_provider_exit_states(
                idle, None, state_dir=state_dir, boot_id=boot_id
            )
        item = idle["sessions"][0]
        self.assertEqual("codex", item["exited_provider"])
        self.assertEqual(expected_uuid, item["recovery"]["uuid"])
        self.assertTrue(item["recovery"]["available"])

    def _overlay_fixture(self, state_dir: Path, boot_id: str) -> dict[str, Any]:
        """An idle shell row plus the retained exact record `r` depends on."""
        fixture = list(inventory_fixture(1, providers=("codex",)))
        active = inventory_core.build_inventory(*fixture, now=1_800_000_000)
        del fixture[2][2001]
        fixture[3] = {}
        idle = inventory_core.build_inventory(*fixture, now=1_800_000_001)
        lifecycle_state.persist_last_exact(
            idle, active, state_dir=state_dir, boot_id=boot_id
        )
        lifecycle_state.record_provider_exit(
            state_dir,
            session_id="main",
            boot_id=boot_id,
            shell_pid=1001,
            shell_start_ticks=fixture[2][1001]["start_ticks"],
            provider="codex",
            exit_code=3,
            input_tracking=True,
            now_monotonic_ns=1_800_000_000_500,
        )
        return idle

    def _exact_records(self, state_dir: Path) -> list[Path]:
        return sorted((state_dir / "lifecycle").glob("*.exact.json"))

    def test_a_removed_exact_record_refuses_rather_than_guessing(self) -> None:
        # The retained record is the only thing that knows which conversation
        # this terminal held. Without it the row is still honestly marked as
        # provider-exited, but nothing may be offered to reopen.
        boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-exact-gone-", dir=REPO
        ) as raw:
            state_dir = Path(raw)
            idle = self._overlay_fixture(state_dir, boot_id)
            records = self._exact_records(state_dir)
            self.assertEqual(1, len(records), records)
            records[0].unlink()
            lifecycle_state.apply_provider_exit_states(
                idle, None, state_dir=state_dir, boot_id=boot_id
            )
        item = idle["sessions"][0]
        self.assertEqual("codex", item["exited_provider"])
        self.assertFalse(item["recovery"]["available"])
        self.assertIsNone(item["recovery"]["uuid"])
        # No identity hint at all, rather than a hopeful one.
        self.assertNotIn("exited_identity", item)
        self.assertNotIn("_terminal_identity_hint", item)

    def test_a_corrupt_exact_record_is_refused_not_read_past(self) -> None:
        # A record that cannot be parsed is not evidence of anything, and the
        # collection says so rather than continuing with a partial answer.
        boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-exact-corrupt-", dir=REPO
        ) as raw:
            state_dir = Path(raw)
            idle = self._overlay_fixture(state_dir, boot_id)
            records = self._exact_records(state_dir)
            self.assertEqual(1, len(records), records)
            records[0].write_text("{not json", encoding="utf-8")
            with self.assertRaises(inventory_core.CollectionError) as caught:
                lifecycle_state.apply_provider_exit_states(
                    idle, None, state_dir=state_dir, boot_id=boot_id
                )
            self.assertIn("invalid JSON", str(caught.exception))

    def test_a_truncated_exact_record_is_refused_not_read_past(self) -> None:
        # Half a record parses as JSON and still is not the record.
        boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-exact-partial-", dir=REPO
        ) as raw:
            state_dir = Path(raw)
            idle = self._overlay_fixture(state_dir, boot_id)
            records = self._exact_records(state_dir)
            document = json.loads(records[0].read_text(encoding="utf-8"))
            document.pop("recovery")
            records[0].write_text(json.dumps(document), encoding="utf-8")
            lifecycle_state.apply_provider_exit_states(
                idle, None, state_dir=state_dir, boot_id=boot_id
            )
        item = idle["sessions"][0]
        self.assertEqual("codex", item["exited_provider"])
        self.assertFalse(item["recovery"]["available"])

    def test_a_foreign_process_still_names_the_shell_as_running(self) -> None:
        # The suppression above is exactly the collector's own process and the
        # processes it started. A workload the terminal itself is running is a
        # sibling of the collector, not a descendant, and it is still reported,
        # otherwise the row would lie about a busy terminal.
        fixture = list(inventory_fixture(1, providers=("codex",)))
        root_pid = 1001
        del fixture[2][2001]
        fixture[3] = {}
        fixture[2][3001] = process(3001, root_pid, "vim", cwd="/srv/project-1")
        idle = inventory_core.build_inventory(*fixture, now=1_800_000_001)
        self.assertEqual("vim", idle["sessions"][0]["native_title"])
        self.assertEqual("running", idle["sessions"][0]["agent_status"])

    def test_the_collectors_own_subtree_hides_but_a_sibling_still_shows(
        self,
    ) -> None:
        # The realistic shape of a reopen: the collector runs under the managed
        # shell and has helpers of its own, while the operator's work sits
        # beside it. Only the collector's own tree may disappear.
        fixture = list(inventory_fixture(1, providers=("codex",)))
        root_pid = 1001
        del fixture[2][2001]
        fixture[3] = {}
        helper = os.getpid()
        fixture[2][helper] = process(helper, root_pid, "python3", cwd="/srv/project-1")
        fixture[2][3101] = process(3101, helper, "timeout", cwd="/srv/project-1")
        fixture[2][3102] = process(3102, 3101, "shpool_status", cwd="/srv/project-1")
        busy = inventory_core.build_inventory(*fixture, now=1_800_000_001)
        self.assertEqual("Idle shell", busy["sessions"][0]["native_title"])
        fixture[2][3200] = process(3200, root_pid, "vim", cwd="/srv/project-1")
        with_sibling = inventory_core.build_inventory(*fixture, now=1_800_000_002)
        self.assertEqual("vim", with_sibling["sessions"][0]["native_title"])
        self.assertEqual("running", with_sibling["sessions"][0]["agent_status"])

    def test_fast_exit_synthesizes_recovery_from_committed_conversation(self) -> None:
        fixture = list(inventory_fixture(1, providers=("codex",)))
        root_pid = 1001
        del fixture[2][2001]
        fixture[3] = {}
        idle = inventory_core.build_inventory(*fixture, now=1_800_000_001)
        expected_uuid = uuid_for(81)
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-fast-exit-", dir=REPO
        ) as raw:
            state_dir = Path(raw)
            lifecycle_state.record_provider_exit(
                state_dir,
                session_id="main",
                boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                shell_pid=root_pid,
                shell_start_ticks=fixture[2][root_pid]["start_ticks"],
                provider="codex",
                conversation_uuid=expected_uuid,
                exit_code=0,
                input_tracking=True,
                now_monotonic_ns=1_800_000_000_500,
            )
            lifecycle_state.apply_provider_exit_states(
                idle,
                None,
                state_dir=state_dir,
                boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            )
        item = idle["sessions"][0]
        self.assertEqual(expected_uuid, item["exited_identity"]["uuid"])
        self.assertIn("committed provider-exit", item["exited_identity"]["provenance"])
        self.assertEqual(expected_uuid, item["recovery"]["uuid"])
        self.assertTrue(item["recovery"]["available"])

    def test_reopen_executes_only_generation_bound_exact_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".lifecycle-reopen-", dir=REPO) as raw:
            state_dir = Path(raw)
            boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            lifecycle_state.record_provider_exit(
                state_dir,
                session_id="main2",
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                provider="codex",
                exit_code=0,
                input_tracking=True,
                now_monotonic_ns=500,
            )
            lifecycle_state.update_state(
                state_dir,
                session_id="main2",
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                event="user-input",
            )
            recovery = inventory_core.recovery_spec(
                "codex", uuid_for(2), str(state_dir)
            )
            item = {
                "provider": "shell",
                "exited_provider": "codex",
                "shpool_shell": {
                    "pid": 123,
                    "process_start_ticks": 456,
                },
                "recovery": recovery,
            }
            args = argparse.Namespace(lifecycle_action="reopen")
            completed = subprocess.CompletedProcess(recovery["argv"], 0)
            with (
                mock.patch.object(
                    inventory_core,
                    "_lifecycle_environment",
                    return_value=(state_dir, "main2", boot_id, 123, 456),
                ),
                mock.patch.object(inventory_core, "_prove_lifecycle_caller"),
                mock.patch.object(inventory_core, "_prove_unchanged_daemon_generation"),
                mock.patch.object(
                    inventory_core, "load_config", return_value={"state_dir": state_dir}
                ),
                mock.patch.object(
                    inventory_core, "snapshot", return_value={"sessions": [item]}
                ),
                mock.patch.object(
                    inventory_core, "guard_live_inventory", return_value=True
                ),
                mock.patch.object(inventory_core, "lookup", return_value=item),
                mock.patch.object(
                    inventory_core.subprocess,
                    "run",
                    return_value=completed,
                ) as launched,
            ):
                self.assertEqual(0, inventory_core._lifecycle_command(args))
            # The session's launch theme rides on every reopen so the window
            # keeps its color identity; the color is the stable identity hash
            # for this uuid.
            expected_theme = inventory_core.session_color("codex", uuid_for(2), {})
            launched.assert_called_once_with(
                [
                    "codex",
                    # And the kit's tab-title items (K3): a reopen builds its
                    # own command line, and without them Codex repaints the tab
                    # from the personal config over the name the kit just
                    # wrote, leaving that one window disagreeing with the list.
                    "-c",
                    'tui.terminal_title=["activity", "thread"]',
                    "-c",
                    f'tui.theme="sk-{expected_theme}"',
                    "-c",
                    "check_for_update_on_startup=false",
                    "--no-alt-screen",
                    "resume",
                    uuid_for(2),
                ],
                cwd=str(state_dir),
                check=False,
            )
            retained = lifecycle_state.load_state(state_dir, "main2")
            assert retained is not None
            self.assertTrue(retained["user_input_after_exit"])

    def _reopen_state(self, state_dir: Path, boot_id: str) -> None:
        lifecycle_state.record_provider_exit(
            state_dir,
            session_id="main2",
            boot_id=boot_id,
            shell_pid=123,
            shell_start_ticks=456,
            provider="codex",
            exit_code=3,
            input_tracking=True,
            now_monotonic_ns=500,
        )

    def _reopen_row(self, state_dir: Path, **overrides: Any) -> dict[str, Any]:
        item = {
            "provider": "shell",
            "exited_provider": "codex",
            "shpool_shell": {"pid": 123, "process_start_ticks": 456},
            "recovery": inventory_core.recovery_spec(
                "codex", uuid_for(2), str(state_dir)
            ),
        }
        item.update(overrides)
        return item

    def _run_reopen(
        self,
        state_dir: Path,
        boot_id: str,
        item: dict[str, Any] | None,
        *,
        completed: subprocess.CompletedProcess | None = None,
        daemon_generation: Any = "unchanged",
    ) -> tuple[int | None, str, mock.Mock]:
        """Drive the reopen verb; return its status or the refusal it raised."""
        args = argparse.Namespace(lifecycle_action="reopen")
        launched = mock.Mock(
            return_value=completed or subprocess.CompletedProcess(["codex"], 0)
        )
        generation = (
            mock.DEFAULT if daemon_generation == "unchanged" else daemon_generation
        )
        with (
            mock.patch.object(
                inventory_core,
                "_lifecycle_environment",
                return_value=(state_dir, "main2", boot_id, 123, 456),
            ),
            mock.patch.object(inventory_core, "_prove_lifecycle_caller"),
            mock.patch.object(
                inventory_core,
                "_prove_unchanged_daemon_generation",
                side_effect=(None if generation is mock.DEFAULT else generation),
            ),
            mock.patch.object(
                inventory_core, "load_config", return_value={"state_dir": state_dir}
            ),
            mock.patch.object(
                inventory_core,
                "snapshot",
                return_value={"sessions": [item] if item else []},
            ),
            mock.patch.object(
                inventory_core, "guard_live_inventory", return_value=True
            ),
            mock.patch.object(inventory_core, "lookup", return_value=item),
            mock.patch.object(inventory_core.subprocess, "run", launched),
        ):
            try:
                return inventory_core._lifecycle_command(args), "", launched
            except inventory_core.CollectionError as exc:
                return None, str(exc), launched

    def test_reopen_reports_the_reopened_providers_own_outcome(self) -> None:
        # The managed shell has to make the same clean-close / crash-menu
        # decision for a reopened conversation that it makes for a first exit,
        # and it can only make it from what this verb returns. Reporting
        # success either way redrew the menu after a clean `/exit`.
        boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        for provider_status, expected in (
            (0, 0),
            (3, inventory_core.LIFECYCLE_REOPENED_PROVIDER_CRASHED),
            (137, inventory_core.LIFECYCLE_REOPENED_PROVIDER_CRASHED),
        ):
            with self.subTest(provider_status=provider_status):
                with tempfile.TemporaryDirectory(
                    prefix=".lifecycle-reopen-status-", dir=REPO
                ) as raw:
                    state_dir = Path(raw)
                    self._reopen_state(state_dir, boot_id)
                    status, refusal, launched = self._run_reopen(
                        state_dir,
                        boot_id,
                        self._reopen_row(state_dir),
                        completed=subprocess.CompletedProcess(
                            ["codex"], provider_status
                        ),
                    )
                    self.assertEqual("", refusal)
                    self.assertEqual(expected, status)
                    launched.assert_called_once()
                    retained = lifecycle_state.load_state(state_dir, "main2")
                    assert retained is not None
                    self.assertEqual(provider_status, retained["exit_code"])

    def test_reopen_refuses_a_daemon_that_restarted_before_the_launch(
        self,
    ) -> None:
        # The snapshot is evidence with an age. A daemon restart between it and
        # the launch would reopen into a terminal the inventory no longer
        # describes, so nothing may start after that check fails.
        boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-reopen-daemon-", dir=REPO
        ) as raw:
            state_dir = Path(raw)
            self._reopen_state(state_dir, boot_id)
            status, refusal, launched = self._run_reopen(
                state_dir,
                boot_id,
                self._reopen_row(state_dir),
                daemon_generation=inventory_core.CollectionError(
                    "the shpool daemon generation changed; nothing reopened"
                ),
            )
            self.assertIsNone(status)
            self.assertIn("daemon generation changed", refusal)
            launched.assert_not_called()

    def test_daemon_revalidation_compares_the_live_generation(self) -> None:
        # The guard itself, against a process table rather than a mock.
        table = {
            10: process(10, 1, "shpool", cmdline=["/usr/bin/shpool", "daemon"]),
        }
        with mock.patch.object(
            inventory_core, "scan_process_table", return_value=table
        ):
            inventory_core._prove_unchanged_daemon_generation(
                {"daemon_generation": {"pid": 10, "process_start_ticks": 100}}
            )
            for changed in (
                {"pid": 10, "process_start_ticks": 999},
                {"pid": 11, "process_start_ticks": 100},
                None,
            ):
                with self.subTest(changed=changed):
                    with self.assertRaises(inventory_core.CollectionError) as caught:
                        inventory_core._prove_unchanged_daemon_generation(
                            {"daemon_generation": changed}
                        )
                    self.assertIn("daemon generation changed", str(caught.exception))

    def test_reopen_names_each_refusal_it_can_reach(self) -> None:
        # A refusal is the whole answer the operator gets, and the menu prints
        # it once before leaving, so each condition has to name itself.
        boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        cases = (
            (None, "no longer in the live session list"),
            (
                {
                    "provider": "shell",
                    "exited_provider": "codex",
                    "shpool_shell": {"pid": 999, "process_start_ticks": 456},
                },
                "not the one that recorded the exit",
            ),
            (
                {
                    "provider": "shell",
                    "shpool_shell": {"pid": 123, "process_start_ticks": 456},
                },
                "the recorded exit was not the provider this terminal last ran",
            ),
            (
                {
                    "provider": "shell",
                    "exited_provider": "codex",
                    "shpool_shell": {"pid": 123, "process_start_ticks": 456},
                },
                "no conversation was recorded for this terminal",
            ),
        )
        for item, expected in cases:
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory(
                    prefix=".lifecycle-reopen-refusal-", dir=REPO
                ) as raw:
                    state_dir = Path(raw)
                    self._reopen_state(state_dir, boot_id)
                    status, refusal, launched = self._run_reopen(
                        state_dir, boot_id, item
                    )
                    self.assertIsNone(status)
                    self.assertIn(expected, refusal)
                    self.assertIn("nothing reopened", refusal)
                    launched.assert_not_called()

    def test_reopen_refuses_without_lifecycle_state(self) -> None:
        # Nothing exited here as far as the private record is concerned.
        boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-reopen-missing-", dir=REPO
        ) as raw:
            state_dir = Path(raw)
            status, refusal, launched = self._run_reopen(
                state_dir, boot_id, self._reopen_row(state_dir)
            )
            self.assertIsNone(status)
            self.assertIn("lifecycle state is unavailable", refusal)
            launched.assert_not_called()

    def test_reopen_refuses_a_corrupt_lifecycle_record(self) -> None:
        # A record that cannot be trusted is not evidence that a provider
        # exited, and it must not become a launch.
        boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-reopen-corrupt-", dir=REPO
        ) as raw:
            state_dir = Path(raw)
            self._reopen_state(state_dir, boot_id)
            document = sorted(
                path
                for path in (state_dir / "lifecycle").glob("*.json")
                if path.name != "key.json" and not path.name.endswith(".exact.json")
            )[0]
            document.write_text("{not json", encoding="utf-8")
            status, refusal, launched = self._run_reopen(
                state_dir, boot_id, self._reopen_row(state_dir)
            )
            self.assertIsNone(status)
            self.assertIn("state file is invalid JSON", refusal)
            launched.assert_not_called()

    def test_reopen_refuses_a_recovery_whose_uuid_is_missing(self) -> None:
        # Exact identity is never synthesized: an absent uuid is a refusal.
        boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-reopen-nouuid-", dir=REPO
        ) as raw:
            state_dir = Path(raw)
            self._reopen_state(state_dir, boot_id)
            recovery = dict(
                inventory_core.recovery_spec("codex", uuid_for(2), str(state_dir))
            )
            recovery["uuid"] = None
            status, refusal, launched = self._run_reopen(
                state_dir, boot_id, self._reopen_row(state_dir, recovery=recovery)
            )
            self.assertIsNone(status)
            self.assertIn("the recorded conversation has no usable id", refusal)
            launched.assert_not_called()

    def test_claude_reopen_passes_hydrated_name_to_native_cli(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".lifecycle-claude-reopen-", dir=REPO
        ) as raw:
            base = Path(raw)
            state_dir = base / "state"
            home = base / "home"
            transcript_dir = home / ".claude" / "projects" / "-srv-project"
            state_dir.mkdir()
            transcript_dir.mkdir(parents=True)
            exact = uuid_for(3)
            (transcript_dir / f"{exact}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "agent-name",
                        "agentName": "Egypt Presidents",
                        "sessionId": exact,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            lifecycle_state.record_provider_exit(
                state_dir,
                session_id="main3",
                boot_id=boot_id,
                shell_pid=123,
                shell_start_ticks=456,
                provider="claude",
                exit_code=0,
                input_tracking=True,
                now_monotonic_ns=500,
            )
            recovery = inventory_core.recovery_spec("claude", exact, str(state_dir))
            item = {
                "provider": "shell",
                "exited_provider": "claude",
                "shpool_shell": {
                    "pid": 123,
                    "process_start_ticks": 456,
                },
                "recovery": recovery,
            }
            args = argparse.Namespace(lifecycle_action="reopen")
            completed = subprocess.CompletedProcess(recovery["argv"], 0)
            with (
                mock.patch.dict(os.environ, {"HOME": os.fspath(home)}),
                mock.patch.object(
                    inventory_core,
                    "_lifecycle_environment",
                    return_value=(state_dir, "main3", boot_id, 123, 456),
                ),
                mock.patch.object(inventory_core, "_prove_lifecycle_caller"),
                mock.patch.object(inventory_core, "_prove_unchanged_daemon_generation"),
                mock.patch.object(
                    inventory_core, "load_config", return_value={"state_dir": state_dir}
                ),
                mock.patch.object(
                    inventory_core, "snapshot", return_value={"sessions": [item]}
                ),
                mock.patch.object(
                    inventory_core, "guard_live_inventory", return_value=True
                ),
                mock.patch.object(inventory_core, "lookup", return_value=item),
                mock.patch.object(
                    inventory_core.subprocess,
                    "run",
                    return_value=completed,
                ) as launched,
            ):
                self.assertEqual(0, inventory_core._lifecycle_command(args))
            launched.assert_called_once_with(
                [
                    "claude",
                    "--name",
                    "Egypt Presidents",
                    "--resume",
                    exact,
                ],
                cwd=str(state_dir),
                check=False,
            )


if __name__ == "__main__":
    unittest.main()
