from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import subprocess
import sys
import unittest

from tests.support import REPO


CORE = REPO / "lib" / "session_inventory.py"
REQUIRED_SYMBOLS = {
    "ABSENT_ALIAS_CONFIG_BACKUP",
    "CollectionError",
    "DEFAULT_MAX_PROC_NODES",
    "ROLLOUT_TAIL_BYTES",
    "_DarwinBsdInfo",
    "_display_width",
    "_empty_terminal_registry",
    "_json_bytes",
    "_parse_darwin_procargs2",
    "_parser",
    "_plan_token",
    "_process_age",
    "_provider_title_info",
    "_read_terminal_registry",
    "_release_sha",
    "_require_supported_platform",
    "_rollout_turn_state",
    "_sha256",
    "_state_paths",
    "_terminal_generation_key",
    "_validate_terminal_registry",
    "acknowledge_pending",
    "apply_legacy_recovery_manifest",
    "apply_retained_setup_attributions",
    "apply_terminal_numbers",
    "atomic_write_json",
    "audit_automatic_titles",
    "automatic_naming_enabled",
    "build_inventory",
    "canonical_aliases",
    "canonical_automatic_titles",
    "codex_open_rollouts",
    "collect_live",
    "descendants",
    "flatten_pending",
    "guard_live_inventory",
    "index_codex_processes",
    "list_pending",
    "load_config",
    "lookup",
    "main",
    "migrate_runtime_aliases",
    "mutate_canonical_alias",
    "mutate_canonical_automatic_title",
    "normalize_automatic_title",
    "os",
    "plan_legacy_recovery_manifest",
    "prove_self_name_caller",
    "prune_automatic_titles",
    "publish_legacy_migration_plan",
    "read_codex_db",
    "read_codex_session_index",
    "recent_output_times",
    "recovery_spec",
    "render_inventory",
    "rollback_legacy_recovery_manifest",
    "scan_darwin_process_table",
    "scan_process_table",
    "self_name_automatic_title",
    "shpool_id_mutation_policy",
    "shutil",
    "snapshot",
    "source_generation_key",
    "strict_live_inventory",
    "update_recovery_state",
}

SIGNATURES = {
    "snapshot": (
        "(*, write_state: 'bool' = True, "
        "config: 'dict[str, Any] | None' = None) -> 'dict[str, Any]'"
    ),
    "collect_live": (
        "(config: 'Mapping[str, Any]', *, runner: 'Runner | None' = None, "
        "proc_root: 'Path | None' = None) -> 'dict[str, Any]'"
    ),
    "codex_open_rollouts": (
        "(pid: 'int', proc_root: 'Path', codex_home: 'Path', "
        "expected_process: 'Mapping[str, Any]') -> 'list[dict[str, Any]]'"
    ),
    "render_inventory": (
        "(inventory: 'Mapping[str, Any]', rows_only: 'bool' = False) -> 'str'"
    ),
    "main": "(argv: 'Sequence[str] | None' = None) -> 'int'",
}

EXPECTED_TOP_LEVEL_COMMANDS = (
    "snapshot",
    "render",
    "waiting-count",
    "lookup",
    "recovery-command",
    "platform",
    "alias",
    "automatic-title",
    "recovery-pending",
    "recovery-manifest",
)


def load_facade(name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, CORE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def referenced_facade_symbols() -> set[str]:
    referenced: set[str] = set()
    for path in sorted((REPO / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "inventory_core"
            ):
                referenced.add(node.attr)
    return referenced


class InventoryFacadeTests(unittest.TestCase):
    def test_contract_matches_every_current_test_reference(self) -> None:
        self.assertEqual(len(REQUIRED_SYMBOLS), 65)
        self.assertEqual(REQUIRED_SYMBOLS, referenced_facade_symbols())

    def test_current_tested_symbols_remain_available(self) -> None:
        facade = load_facade("session_inventory_facade_symbols")
        missing = sorted(name for name in REQUIRED_SYMBOLS if not hasattr(facade, name))
        self.assertEqual(missing, [])

    def test_high_risk_call_signatures_are_stable(self) -> None:
        facade = load_facade("session_inventory_facade_signatures")
        actual = {
            name: str(inspect.signature(getattr(facade, name))) for name in SIGNATURES
        }
        self.assertEqual(actual, SIGNATURES)

    def test_normal_import_exposes_the_same_facade(self) -> None:
        code = (
            "import session_inventory as module\n"
            f"expected = {sorted(REQUIRED_SYMBOLS)!r}\n"
            "missing = [name for name in expected if not hasattr(module, name)]\n"
            "raise SystemExit(1 if missing else 0)\n"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.fspath(REPO / "lib")
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_direct_script_help_contract(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CORE), "--help"],
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "{" + ",".join(EXPECTED_TOP_LEVEL_COMMANDS) + "}",
            result.stdout,
        )
        self.assertEqual(result.stderr, "")

    def test_direct_script_help_works_outside_repository(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CORE), "--help"],
            cwd=REPO.parent,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "{" + ",".join(EXPECTED_TOP_LEVEL_COMMANDS) + "}",
            result.stdout,
        )
        self.assertEqual(result.stderr, "")

    def test_common_kernel_preserves_facade_identity_and_behavior(self) -> None:
        facade = load_facade("session_inventory_facade_common")
        from sessionkit_inventory import common

        self.assertIs(facade.CollectionError, common.CollectionError)
        self.assertEqual(
            common.clean_text("  A\x1b[31m  Title\x00  "),
            "A [31m Title",
        )
        uuid = "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"
        self.assertEqual(common.valid_uuid(uuid), uuid.lower())
        self.assertIsNone(common.valid_uuid("not-a-uuid"))
        self.assertEqual(
            sorted(("main10", "main", "main2"), key=common.natural_name_key),
            ["main", "main2", "main10"],
        )
        self.assertEqual(common._positive_int("8", 4, 2, 10), 8)
        self.assertEqual(common._positive_int("20", 4, 2, 10), 4)
        self.assertEqual(common._positive_float("0.5", 1.0, 0.2, 2.0), 0.5)
        for disabled in ("0", "false", "NO", " off "):
            self.assertFalse(
                common.automatic_naming_enabled({"SESSION_KIT_AUTO_NAME": disabled})
            )
        self.assertTrue(common.automatic_naming_enabled({}))

    def test_facade_path_remains_the_launcher_entry_point(self) -> None:
        for relative in ("bin/sp", "bin/shpool_status"):
            text = (REPO / relative).read_text(encoding="utf-8")
            self.assertIn("lib/session_inventory.py", text)


if __name__ == "__main__":
    unittest.main()
