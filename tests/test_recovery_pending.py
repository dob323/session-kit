from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock

from tests.test_inventory import inventory_core, inventory_fixture, uuid_for
from tests.support import REPO


def pending_session(provider: str, uuid: str, title: str) -> dict:
    argv = ["claude", "--resume", uuid] if provider == "claude" else ["codex", "resume", uuid]
    return {
        "provider": provider,
        "uuid": uuid,
        "title": title,
        "started_at_unix_ms": 1_700_000_000_000,
        "cwd": "/srv/project",
        "argv": argv,
        "command": " ".join(argv),
    }


def pending_document() -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-07-28T00:00:00Z",
        "source_boot_id": "boot-primary",
        "source_daemon_generation": {
            "pid": 10,
            "process_start_ticks": 100,
        },
        "detected_boot_id": "boot-current",
        "detected_daemon_generation": {
            "pid": 20,
            "process_start_ticks": 200,
        },
        "sessions": {
            "old-claude": pending_session("claude", uuid_for(1), "Claude pending"),
            "old-codex": pending_session("codex", uuid_for(2), "Codex pending"),
        },
        "queued_generations": [
            {
                "source_boot_id": "boot-queued",
                "source_daemon_generation": {
                    "pid": 30,
                    "process_start_ticks": 300,
                },
                "sessions": {
                    "older-codex": pending_session(
                        "codex", uuid_for(3), "Older Codex pending"
                    )
                },
            }
        ],
    }


class PendingRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".pending-", dir=REPO)
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir()
        self.state.chmod(0o700)
        self.pending_path = self.state / "recovery-pending.json"
        self.pending_path.write_text(
            json.dumps(pending_document(), sort_keys=True), encoding="utf-8"
        )
        self.config = {
            "schema_version": 1,
            "state_dir": self.state,
            "aliases": {},
            "max_proc_nodes": 8192,
            "max_proc_depth": 32,
        }
        self.boot_file = Path(self.temp.name) / "boot-id"
        self.old_boot_env = os.environ.get("SESSION_KIT_BOOT_ID_FILE")
        os.environ["SESSION_KIT_BOOT_ID_FILE"] = str(self.boot_file)

    def tearDown(self) -> None:
        if self.old_boot_env is None:
            os.environ.pop("SESSION_KIT_BOOT_ID_FILE", None)
        else:
            os.environ["SESSION_KIT_BOOT_ID_FILE"] = self.old_boot_env
        self.temp.cleanup()

    def test_list_flattens_primary_and_queued_generations(self) -> None:
        listed = inventory_core.list_pending(self.config)
        entries = listed["entries"]
        self.assertEqual(3, len(entries))
        self.assertEqual(
            {"primary", "queued"}, {entry["queue"] for entry in entries}
        )
        self.assertTrue(all(entry["actionable"] for entry in entries))
        self.assertTrue(
            all(entry["started_at_unix_ms"] == 1_700_000_000_000 for entry in entries)
        )
        self.assertEqual(
            {"old-claude", "old-codex", "older-codex"},
            {entry["old_shpool_id"] for entry in entries},
        )

    def test_manifest_preserves_login_timestamp_for_future_recovery(self) -> None:
        live = inventory_core.build_inventory(*inventory_fixture(1), now=1_800_000_000)
        source = live["sessions"][0]
        manifest = inventory_core.recovery_manifest(live)
        stored = manifest["sessions"][source["shpool_id_raw"]]
        self.assertEqual(source["started_at_unix_ms"], stored["started_at_unix_ms"])

    def test_duplicate_exact_identity_prefers_primary_and_acknowledges_all_evidence(self) -> None:
        document = pending_document()
        duplicate = pending_session("claude", uuid_for(1), "Newer title")
        document["queued_generations"].append(
            {
                "source_boot_id": "boot-newest",
                "source_daemon_generation": {"pid": 40, "process_start_ticks": 400},
                "sessions": {"newer-claude": duplicate},
            }
        )
        self.pending_path.write_text(json.dumps(document), encoding="utf-8")
        entries = inventory_core.list_pending(self.config)["entries"]
        claude = next(entry for entry in entries if entry["uuid"] == uuid_for(1))
        self.assertEqual("primary", claude["queue"])
        self.assertEqual(1, claude["duplicate_count"])
        self.assertEqual(2, len(claude["evidence"]))
        self.assertEqual([], claude["conflict_fields"])
        self.assertTrue(claude["actionable"])
        live = inventory_core.build_inventory(*inventory_fixture(2), now=1_800_000_000)
        result = inventory_core.acknowledge_pending(
            self.config,
            claude["source_generation_key"],
            claude["old_shpool_id"],
            claude["uuid"],
            collector=lambda _: json.loads(json.dumps(live)),
        )
        self.assertNotIn(uuid_for(1), {item["uuid"] for item in result["remaining"]["entries"]})
        stored = json.loads(self.pending_path.read_text())
        self.assertNotIn("old-claude", stored["sessions"])
        self.assertEqual(1, len(stored["queued_generations"]))

    def test_duplicate_queued_identity_prefers_newest_generation(self) -> None:
        duplicate_uuid = uuid_for(44)
        document = pending_document()
        document["sessions"] = {}
        document["queued_generations"] = [
            {
                "source_boot_id": "boot-older",
                "source_daemon_generation": {"pid": 30, "process_start_ticks": 300},
                "sessions": {"older": pending_session("codex", duplicate_uuid, "Older title")},
            },
            {
                "source_boot_id": "boot-newer",
                "source_daemon_generation": {"pid": 40, "process_start_ticks": 400},
                "sessions": {"newer": pending_session("codex", duplicate_uuid, "Newer title")},
            },
        ]
        self.pending_path.write_text(json.dumps(document), encoding="utf-8")
        entries = inventory_core.list_pending(self.config)["entries"]
        self.assertEqual(1, len(entries))
        self.assertEqual("newer", entries[0]["old_shpool_id"])
        self.assertEqual("Newer title", entries[0]["title"])
        self.assertEqual(1, entries[0]["queue_index"])
        self.assertEqual(1, entries[0]["duplicate_count"])

    def test_conflicting_duplicate_metadata_is_visible_but_not_actionable(self) -> None:
        document = pending_document()
        conflicting = pending_session("claude", uuid_for(1), "Moved title")
        conflicting["cwd"] = "/srv/other-project"
        document["queued_generations"].append(
            {
                "source_boot_id": "boot-conflict",
                "source_daemon_generation": {"pid": 40, "process_start_ticks": 400},
                "sessions": {"moved-claude": conflicting},
            }
        )
        self.pending_path.write_text(json.dumps(document), encoding="utf-8")
        before = self.pending_path.read_bytes()
        target = next(
            item for item in inventory_core.list_pending(self.config)["entries"]
            if item["uuid"] == uuid_for(1)
        )
        self.assertFalse(target["actionable"])
        self.assertIn("cwd", target["conflict_fields"])
        with self.assertRaises(inventory_core.CollectionError):
            inventory_core.acknowledge_pending(
                self.config,
                target["source_generation_key"],
                target["old_shpool_id"],
                target["uuid"],
                collector=lambda _: {},
            )
        self.assertEqual(before, self.pending_path.read_bytes())

    def test_wrong_provider_or_non_live_collector_leaves_pending_bytes_unchanged(self) -> None:
        before = self.pending_path.read_bytes()
        key = inventory_core.source_generation_key(
            "boot-primary", {"pid": 10, "process_start_ticks": 100}
        )
        self.assertIsNotNone(key)
        wrong_provider = inventory_core.build_inventory(
            *inventory_fixture(1, providers=("codex",)), now=1_800_000_000
        )
        with self.assertRaises(inventory_core.CollectionError):
            inventory_core.acknowledge_pending(
                self.config,
                key,
                "old-claude",
                uuid_for(1),
                collector=lambda _: wrong_provider,
            )
        self.assertEqual(before, self.pending_path.read_bytes())

        cache = dict(wrong_provider)
        cache["source"] = "cache"
        cache["stale"] = True
        with self.assertRaises(inventory_core.CollectionError):
            inventory_core.acknowledge_pending(
                self.config,
                key,
                "old-claude",
                uuid_for(1),
                collector=lambda _: cache,
            )
        self.assertEqual(before, self.pending_path.read_bytes())

    def test_concurrent_distinct_acknowledgments_serialize_and_preserve_each_other(self) -> None:
        live = inventory_core.build_inventory(
            *inventory_fixture(2), now=1_800_000_000
        )
        self.assertTrue(inventory_core.strict_live_inventory(live))
        key = inventory_core.source_generation_key(
            "boot-primary", {"pid": 10, "process_start_ticks": 100}
        )
        self.assertIsNotNone(key)

        def collector(_: dict) -> dict:
            time.sleep(0.05)
            return json.loads(json.dumps(live))

        targets = (
            ("old-claude", uuid_for(1)),
            ("old-codex", uuid_for(2)),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    inventory_core.acknowledge_pending,
                    self.config,
                    key,
                    old_id,
                    uuid,
                    collector=collector,
                )
                for old_id, uuid in targets
            ]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(2, len(results))
        remaining = inventory_core.list_pending(self.config)["entries"]
        self.assertEqual(["older-codex"], [item["old_shpool_id"] for item in remaining])
        stored = json.loads(self.pending_path.read_text())
        self.assertEqual({}, stored["sessions"])
        self.assertEqual(1, len(stored["queued_generations"]))

    def test_daemon_change_queues_shpool_but_not_outside_root(self) -> None:
        self._write_old_manifest(boot_id="same-boot", include_shpool=True)
        self.boot_file.write_text("same-boot\n", encoding="utf-8")
        paths = inventory_core._state_paths(self.config)
        inventory_core.update_recovery_state(paths, self._new_inventory())
        pending = json.loads(self.pending_path.read_text())
        self.assertEqual(["old-main"], list(pending["sessions"]))
        self.assertEqual({}, pending["outside_agents"])

    def test_boot_change_queues_both_shpool_and_exact_outside_root(self) -> None:
        self._write_old_manifest(boot_id="old-boot", include_shpool=True)
        self.boot_file.write_text("new-boot\n", encoding="utf-8")
        paths = inventory_core._state_paths(self.config)
        inventory_core.update_recovery_state(paths, self._new_inventory())
        flattened = inventory_core.flatten_pending(
            json.loads(self.pending_path.read_text())
        )["entries"]
        scopes = {entry["old_shpool_id"]: entry["scope"] for entry in flattened}
        self.assertEqual("shpool", scopes["old-main"])
        self.assertEqual(
            "outside",
            scopes[f"outside:codex:{uuid_for(77)}"],
        )

    def test_daemon_change_with_outside_only_creates_no_pending_queue(self) -> None:
        self._write_old_manifest(boot_id="same-boot", include_shpool=False)
        self.boot_file.write_text("same-boot\n", encoding="utf-8")
        paths = inventory_core._state_paths(self.config)
        self.pending_path.unlink(missing_ok=True)
        inventory_core.update_recovery_state(paths, self._new_inventory())
        self.assertFalse(self.pending_path.exists())

    def test_legacy_plan_reconciles_nine_to_seven_without_writing_state(self) -> None:
        plan, manifest, original, _ = self._migration_plan()
        self.assertEqual(original, manifest.read_bytes())
        self.assertFalse(
            (self.state / "recovery-manifest-migration-receipt.json").exists()
        )
        dispositions = {
            row["shpool_id"]: row["disposition"]
            for row in plan["reconciliation"]
        }
        self.assertEqual(9, len(dispositions))
        self.assertEqual("ended", dispositions["main3"])
        self.assertEqual("ended", dispositions["main4"])
        self.assertEqual(
            7,
            sum(value == "carried" for value in dispositions.values()),
        )
        self.assertEqual(7, len(plan["current_roots"]))
        self.assertEqual("a" * 40, plan["release_sha"])
        self.assertEqual(plan["plan_token"], inventory_core._plan_token(plan))

    def test_legacy_plan_accepts_actual_omitted_generation_schema(self) -> None:
        first, manifest, _, live = self._migration_plan()
        legacy = json.loads(manifest.read_text())
        legacy.pop("daemon_generation")
        legacy.pop("outside_agents")
        manifest.write_text(json.dumps(legacy, sort_keys=True), encoding="utf-8")
        manifest.chmod(0o600)
        plan = inventory_core.plan_legacy_recovery_manifest(
            self.config,
            first["continuity_evidence"]["path"],
            "a" * 40,
            collector=lambda _: json.loads(json.dumps(live)),
            proc_root=self.proc_root,
        )
        self.assertEqual(9, len(plan["reconciliation"]))
        self.assertEqual(
            {"main3", "main4"},
            {
                item["shpool_id"]
                for item in plan["reconciliation"]
                if item["disposition"] == "ended"
            },
        )

    def test_legacy_apply_archives_receipts_and_exact_rollback(self) -> None:
        plan, manifest, original, live = self._migration_plan()
        plan_path = self._write_plan(plan)
        result = inventory_core.apply_legacy_recovery_manifest(
            self.config,
            plan_path,
            "a" * 40,
            collector=lambda _: json.loads(json.dumps(live)),
            proc_root=self.proc_root,
        )
        self.assertEqual("applied", result["result"])
        archive = Path(plan["archive_path"])
        receipt_path = self.state / "recovery-manifest-migration-receipt.json"
        self.assertEqual(original, archive.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(archive.stat().st_mode))
        self.assertEqual("applied", json.loads(receipt_path.read_text())["phase"])
        self.assertEqual(
            plan["target_manifest_sha256"],
            inventory_core._sha256(manifest.read_bytes()),
        )
        exact_postimage = manifest.read_bytes()
        refreshed = json.loads(json.dumps(live))
        refreshed["generated_at"] = "2027-01-16T08:00:00Z"
        inventory_core.update_recovery_state(
            inventory_core._state_paths(self.config), refreshed
        )
        self.assertEqual(exact_postimage, manifest.read_bytes())

        rolled_back = inventory_core.rollback_legacy_recovery_manifest(
            self.config,
            plan_path,
            "a" * 40,
            collector=lambda _: json.loads(json.dumps(live)),
            proc_root=self.proc_root,
        )
        self.assertEqual("rolled-back", rolled_back["result"])
        self.assertEqual(original, manifest.read_bytes())
        self.assertEqual("rolled-back", json.loads(receipt_path.read_text())["phase"])

    def test_legacy_plan_output_is_private_durable_and_non_overwriting(self) -> None:
        plan, _, _, _ = self._migration_plan()
        output = self.state / "reviewed-migration-plan.json"
        summary = inventory_core.publish_legacy_migration_plan(
            self.config, output, plan
        )
        self.assertEqual("planned", summary["result"])
        self.assertEqual(plan["plan_token"], summary["plan_token"])
        self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))
        self.assertEqual(plan, json.loads(output.read_text()))
        before = output.read_bytes()
        with self.assertRaises(inventory_core.CollectionError):
            inventory_core.publish_legacy_migration_plan(
                self.config, output, plan
            )
        self.assertEqual(before, output.read_bytes())

    def test_migration_plan_publication_refuses_state_root_and_lock_symlinks(self) -> None:
        plan, _, _, _ = self._migration_plan()
        target_root = Path(self.temp.name) / "state-target"
        target_root.mkdir(mode=0o700)
        linked_root = Path(self.temp.name) / "state-link"
        linked_root.symlink_to(target_root, target_is_directory=True)
        linked_config = {**self.config, "state_dir": linked_root}
        with self.assertRaises(inventory_core.CollectionError):
            inventory_core.publish_legacy_migration_plan(
                linked_config, linked_root / "plan.json", plan
            )
        self.assertEqual([], list(target_root.iterdir()))

        lock_target = Path(self.temp.name) / "lock-target"
        lock_target.write_text("do not touch\n", encoding="utf-8")
        lock_target.chmod(0o600)
        lock = self.state / "inventory.lock"
        lock.symlink_to(lock_target)
        before = lock_target.read_bytes()
        with self.assertRaises((inventory_core.CollectionError, OSError)):
            inventory_core.publish_legacy_migration_plan(
                self.config, self.state / "plan.json", plan
            )
        self.assertTrue(lock.is_symlink())
        self.assertEqual(before, lock_target.read_bytes())
        self.assertFalse((self.state / "plan.json").exists())

    def test_legacy_apply_resumes_crash_after_target_publication(self) -> None:
        plan, manifest, _, live = self._migration_plan()
        plan_path = self._write_plan(plan)
        real_atomic_write = inventory_core.atomic_write_json
        failed = False

        def die_before_final_receipt(path: Path, payload: object) -> None:
            nonlocal failed
            if (
                path.name == "recovery-manifest-migration-receipt.json"
                and isinstance(payload, dict)
                and payload.get("phase") == "applied"
                and not failed
            ):
                failed = True
                raise OSError("simulated crash after target publication")
            real_atomic_write(path, payload)

        with mock.patch.object(
            inventory_core, "atomic_write_json", side_effect=die_before_final_receipt
        ):
            with self.assertRaises(OSError):
                inventory_core.apply_legacy_recovery_manifest(
                    self.config,
                    plan_path,
                    "a" * 40,
                    collector=lambda _: json.loads(json.dumps(live)),
                    proc_root=self.proc_root,
                )
        self.assertEqual(
            plan["target_manifest_sha256"],
            inventory_core._sha256(manifest.read_bytes()),
        )
        changed_live = json.loads(json.dumps(live))
        changed_live["source"] = "cache"
        changed_live["stale"] = True
        resumed = inventory_core.apply_legacy_recovery_manifest(
            self.config,
            plan_path,
            "a" * 40,
            collector=lambda _: changed_live,
            proc_root=self.proc_root,
        )
        self.assertEqual("already-applied", resumed["result"])
        receipt = json.loads(
            (self.state / "recovery-manifest-migration-receipt.json").read_text()
        )
        self.assertEqual("applied", receipt["phase"])

    def test_legacy_plan_refuses_identity_changes_extras_and_bad_evidence(self) -> None:
        plan, manifest, original, live = self._migration_plan()
        evidence = Path(plan["continuity_evidence"]["path"])
        base_evidence = json.loads(evidence.read_text())
        cases: list[tuple[str, dict, dict]] = []

        reused = json.loads(json.dumps(live))
        reused["sessions"][0]["identity"]["uuid"] = uuid_for(99)
        reused["sessions"][0]["recovery"]["uuid"] = uuid_for(99)
        cases.append(("reused ID", reused, base_evidence))

        recovery_mismatch = json.loads(json.dumps(live))
        recovery_mismatch["sessions"][0]["recovery"]["uuid"] = uuid_for(99)
        cases.append(("identity recovery mismatch", recovery_mismatch, base_evidence))

        extra = json.loads(json.dumps(live))
        extra["sessions"].append(
            json.loads(json.dumps(inventory_core.build_inventory(
                *inventory_fixture(10), now=1_800_000_000
            )["sessions"][-1]))
        )
        cases.append(("current-only root", extra, base_evidence))

        bad_evidence = json.loads(json.dumps(base_evidence))
        bad_evidence["sessions"][0]["provider_uuid"] = uuid_for(99)
        cases.append(("evidence mismatch", live, bad_evidence))

        for reason, candidate_live, candidate_evidence in cases:
            with self.subTest(reason=reason):
                evidence.write_text(
                    json.dumps(candidate_evidence), encoding="utf-8"
                )
                evidence.chmod(0o600)
                with self.assertRaises(inventory_core.CollectionError):
                    inventory_core.plan_legacy_recovery_manifest(
                        self.config,
                        evidence,
                        "a" * 40,
                        collector=lambda _, value=candidate_live: json.loads(
                            json.dumps(value)
                        ),
                        proc_root=self.proc_root,
                    )
                self.assertEqual(original, manifest.read_bytes())
                evidence.write_text(json.dumps(base_evidence), encoding="utf-8")
                evidence.chmod(0o600)

    def test_legacy_apply_refuses_recomputed_plan_with_tampered_target(self) -> None:
        plan, manifest, original, live = self._migration_plan()
        plan["target_manifest"]["sessions"]["main"]["uuid"] = uuid_for(99)
        plan["target_manifest_sha256"] = inventory_core._sha256(
            inventory_core._json_bytes(plan["target_manifest"])
        )
        plan["plan_token"] = inventory_core._plan_token(plan)
        plan_path = self._write_plan(plan)
        with self.assertRaises(inventory_core.CollectionError):
            inventory_core.apply_legacy_recovery_manifest(
                self.config,
                plan_path,
                "a" * 40,
                collector=lambda _: json.loads(json.dumps(live)),
                proc_root=self.proc_root,
            )
        self.assertEqual(original, manifest.read_bytes())

    def test_legacy_plan_refuses_symlink_evidence_and_pending(self) -> None:
        plan, manifest, original, live = self._migration_plan()
        evidence = Path(plan["continuity_evidence"]["path"])
        evidence_target = evidence.with_name("evidence-target.json")
        evidence.rename(evidence_target)
        evidence.symlink_to(evidence_target)
        with self.assertRaises(inventory_core.CollectionError):
            inventory_core.plan_legacy_recovery_manifest(
                self.config,
                evidence,
                "a" * 40,
                collector=lambda _: live,
                proc_root=self.proc_root,
            )
        self.assertEqual(original, manifest.read_bytes())
        evidence.unlink()
        evidence_target.rename(evidence)
        self.pending_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(inventory_core.CollectionError):
            inventory_core.plan_legacy_recovery_manifest(
                self.config,
                evidence,
                "a" * 40,
                collector=lambda _: live,
                proc_root=self.proc_root,
            )

    def test_legacy_rollback_refuses_changed_postimage(self) -> None:
        plan, manifest, _, live = self._migration_plan()
        plan_path = self._write_plan(plan)
        inventory_core.apply_legacy_recovery_manifest(
            self.config,
            plan_path,
            "a" * 40,
            collector=lambda _: json.loads(json.dumps(live)),
            proc_root=self.proc_root,
        )
        manifest.write_text('{"changed":true}\n', encoding="utf-8")
        manifest.chmod(0o600)
        before = manifest.read_bytes()
        with self.assertRaises(inventory_core.CollectionError):
            inventory_core.rollback_legacy_recovery_manifest(
                self.config,
                plan_path,
                "a" * 40,
                collector=lambda _: live,
                proc_root=self.proc_root,
            )
        self.assertEqual(before, manifest.read_bytes())

    def test_recovery_manifest_migration_cli_is_explicit(self) -> None:
        parsed = inventory_core._parser().parse_args(
            [
                "recovery-manifest",
                "plan-legacy",
                "--continuity-evidence",
                "/tmp/evidence",
                "--release-sha",
                "a" * 40,
                "--output",
                "/tmp/plan",
            ]
        )
        self.assertEqual("recovery-manifest", parsed.command)
        self.assertEqual("plan-legacy", parsed.manifest_action)
        self.assertEqual("/tmp/plan", parsed.output)

    def test_release_sha_is_bound_to_executing_release_metadata(self) -> None:
        release_sha = "b" * 40
        release_root = Path(self.temp.name) / release_sha
        (release_root / "lib").mkdir(parents=True)
        fake_module = release_root / "lib" / "session_inventory.py"
        fake_module.write_text("# release fixture\n", encoding="utf-8")
        metadata = release_root / "RELEASE.json"
        metadata.write_text(
            json.dumps({"commit": release_sha}), encoding="utf-8"
        )
        metadata.chmod(0o444)
        with mock.patch.object(
            inventory_core, "__file__", str(fake_module)
        ), mock.patch.dict(
            os.environ,
            {"SESSION_KIT_RELEASE_DIR": str(release_root)},
        ):
            self.assertEqual(release_sha, inventory_core._release_sha(release_sha))
            metadata.chmod(0o600)
            metadata.write_text(
                json.dumps({"commit": "c" * 40}), encoding="utf-8"
            )
            metadata.chmod(0o444)
            with self.assertRaises(inventory_core.CollectionError):
                inventory_core._release_sha(release_sha)
            metadata.chmod(0o600)
            metadata.write_text(
                json.dumps({"commit": release_sha}), encoding="utf-8"
            )
            metadata.chmod(0o444)
            os.environ["SESSION_KIT_RELEASE_DIR"] = str(release_root.parent)
            with self.assertRaises(inventory_core.CollectionError):
                inventory_core._release_sha(release_sha)

    def _migration_plan(self) -> tuple[dict, Path, bytes, dict]:
        self.pending_path.unlink(missing_ok=True)
        self.boot_file.write_text("same-boot\n", encoding="utf-8")
        live = inventory_core.build_inventory(
            *inventory_fixture(9), now=1_800_000_000
        )
        live["sessions"] = [
            row
            for row in live["sessions"]
            if row["shpool_id_raw"] not in {"main3", "main4"}
        ]
        for row_number, row in enumerate(live["sessions"], start=1):
            row["row"] = row_number
        live["daemon_generation"] = {
            "pid": 20,
            "process_start_ticks": 200,
        }
        all_live = inventory_core.build_inventory(
            *inventory_fixture(9), now=1_800_000_000
        )
        identities = {
            row["shpool_id_raw"]: (row["provider"], row["identity"]["uuid"])
            for row in all_live["sessions"]
        }
        legacy = {
            "schema_version": 1,
            "generated_at": "2026-07-27T01:00:00Z",
            "boot_id": "same-boot",
            "daemon_generation": None,
            "sessions": {
                shpool_id: pending_session(provider, uuid, f"Legacy {shpool_id}")
                for shpool_id, (provider, uuid) in identities.items()
            },
            "outside_agents": {},
        }
        manifest = self.state / "recovery-manifest.json"
        manifest.write_text(json.dumps(legacy, sort_keys=True), encoding="utf-8")
        manifest.chmod(0o600)
        original = manifest.read_bytes()
        evidence = Path(self.temp.name) / "current-identities.json"
        evidence.write_text(
            json.dumps(
                {
                    "captured_at": "2026-07-27T00:00:00Z",
                    "daemon_pid": 20,
                    "sessions": [
                        {
                            "shpool_name": shpool_id,
                            "provider": provider,
                            "provider_uuid": uuid,
                        }
                        for shpool_id, (provider, uuid) in identities.items()
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        evidence.chmod(0o600)
        self.proc_root = Path(self.temp.name) / "proc"
        (self.proc_root / "20").mkdir(parents=True, exist_ok=True)
        fields = ["S", "1", *(["0"] * 17), "200"]
        (self.proc_root / "20" / "stat").write_text(
            f"20 (shpool) {' '.join(fields)}\n", encoding="utf-8"
        )
        (self.proc_root / "stat").write_text(
            "cpu 1 2 3 4\nbtime 1700000000\n", encoding="utf-8"
        )
        plan = inventory_core.plan_legacy_recovery_manifest(
            self.config,
            evidence,
            "a" * 40,
            collector=lambda _: json.loads(json.dumps(live)),
            proc_root=self.proc_root,
            now=1_800_000_001,
        )
        return plan, manifest, original, live

    def _write_plan(self, plan: dict) -> Path:
        path = Path(self.temp.name) / "migration-plan.json"
        path.write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def _write_old_manifest(self, *, boot_id: str, include_shpool: bool) -> None:
        self.pending_path.unlink(missing_ok=True)
        old = {
            "schema_version": 1,
            "generated_at": "2026-07-27T00:00:00Z",
            "boot_id": boot_id,
            "daemon_generation": {
                "pid": 10,
                "process_start_ticks": 100,
            },
            "sessions": (
                {
                    "old-main": pending_session(
                        "claude", uuid_for(1), "Old shpool root"
                    )
                }
                if include_shpool
                else {}
            ),
            "outside_agents": {
                f"outside:codex:{uuid_for(77)}": {
                    **pending_session("codex", uuid_for(77), "Old outside root"),
                    "scope": "outside",
                }
            },
        }
        (self.state / "recovery-manifest.json").write_text(
            json.dumps(old), encoding="utf-8"
        )

    def _new_inventory(self) -> dict:
        current = inventory_core.build_inventory(
            *inventory_fixture(1), now=1_800_000_000
        )
        current["daemon_generation"] = {
            "pid": 20,
            "process_start_ticks": 200,
        }
        return current


if __name__ == "__main__":
    unittest.main()
