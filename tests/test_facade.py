from __future__ import annotations

import ast
import importlib.util
import inspect
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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
    "_lifecycle_command",
    "_parse_darwin_procargs2",
    "_parser",
    "_alias_command",
    "_color_command",
    "_plan_token",
    "_process_age",
    "_prove_lifecycle_caller",
    "_provider_title_info",
    "_read_terminal_registry",
    "_valid_colors",
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
    "MAX_CODEX_SESSION_INDEX_BYTES",
    "migrate_runtime_aliases",
    "mutate_canonical_alias",
    "mutate_canonical_automatic_title",
    "normalize_automatic_title",
    "os",
    "plan_legacy_recovery_manifest",
    "propagate_provider_color",
    "propagate_provider_title",
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
    "session_color",
    "SESSION_COLORS",
    "shpool_id_mutation_policy",
    "shutil",
    "snapshot",
    "source_generation_key",
    "strict_live_inventory",
    "subprocess",
    "update_recovery_state",
}

SIGNATURES = {
    "_home": "() -> 'Path'",
    "_xdg_path": "(env_name: 'str', fallback: 'Path') -> 'Path'",
    "config_path": "() -> 'Path'",
    "default_state_dir": "() -> 'Path'",
    "default_journal_dir": "() -> 'Path'",
    "default_journal_recovery_dir": "() -> 'Path'",
    "default_start_dir": "() -> 'Path'",
    "load_config": "() -> 'dict[str, Any]'",
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
    "color",
    "automatic-title",
    "recovery-pending",
    "recovery-manifest",
    "lifecycle",
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
        self.assertEqual(len(REQUIRED_SYMBOLS), 76)
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

    def test_path_wrappers_forward_patched_facade_dependencies(self) -> None:
        facade = load_facade("session_inventory_facade_paths")
        cases = (
            (
                "config_path",
                "XDG_CONFIG_HOME",
                Path("/home/test/.config"),
                Path("session-kit/inventory.json"),
            ),
            (
                "default_state_dir",
                "XDG_STATE_HOME",
                Path("/home/test/.local/state"),
                Path("session-kit"),
            ),
            (
                "default_journal_dir",
                "XDG_STATE_HOME",
                Path("/home/test/.local/state"),
                Path("shpool-journal"),
            ),
            (
                "default_journal_recovery_dir",
                "XDG_STATE_HOME",
                Path("/home/test/.local/state"),
                Path("shpool-journal-recovery"),
            ),
            (
                "default_start_dir",
                "XDG_STATE_HOME",
                Path("/home/test/.local/state"),
                Path("shpool-start"),
            ),
        )
        for function_name, env_name, fallback, suffix in cases:
            with (
                self.subTest(function=function_name),
                mock.patch.dict(facade.os.environ, {}, clear=True),
                mock.patch.object(
                    facade,
                    "_home",
                    return_value=Path("/home/test"),
                ) as home,
                mock.patch.object(
                    facade,
                    "_xdg_path",
                    return_value=Path("/xdg"),
                ) as xdg_path,
            ):
                result = getattr(facade, function_name)()
                self.assertEqual(Path("/xdg") / suffix, result)
                home.assert_called_once_with()
                xdg_path.assert_called_once_with(env_name, fallback)

    def test_explicit_path_overrides_bypass_fallback_callbacks(self) -> None:
        facade = load_facade("session_inventory_facade_path_overrides")
        cases = (
            ("config_path", "SESSION_KIT_CONFIG", Path("relative/config.json")),
            ("default_state_dir", "SESSION_KIT_STATE_DIR", Path("relative/state")),
            (
                "default_journal_dir",
                "SESSION_KIT_JOURNAL_DIR",
                Path("relative/journal"),
            ),
            (
                "default_journal_recovery_dir",
                "SESSION_KIT_JOURNAL_RECOVERY_DIR",
                Path("relative/recovery"),
            ),
            ("default_start_dir", "SESSION_KIT_START_DIR", Path("relative/start")),
        )
        for function_name, env_name, expected in cases:
            with (
                self.subTest(function=function_name),
                mock.patch.dict(
                    facade.os.environ,
                    {env_name: str(expected)},
                    clear=True,
                ),
                mock.patch.object(
                    facade,
                    "_home",
                    side_effect=AssertionError("home fallback used"),
                ) as home,
                mock.patch.object(
                    facade,
                    "_xdg_path",
                    side_effect=AssertionError("XDG fallback used"),
                ) as xdg_path,
            ):
                self.assertEqual(expected, getattr(facade, function_name)())
                home.assert_not_called()
                xdg_path.assert_not_called()

    def test_home_wrapper_preserves_eager_home_factory_behavior(self) -> None:
        facade = load_facade("session_inventory_facade_home")
        with (
            mock.patch.dict(
                facade.os.environ,
                {"HOME": "/configured/home"},
                clear=True,
            ),
            mock.patch.object(
                facade.Path,
                "home",
                return_value=Path("/factory/home"),
            ) as home_factory,
        ):
            self.assertEqual(Path("/configured/home"), facade._home())
            home_factory.assert_called_once_with()
        with (
            mock.patch.dict(facade.os.environ, {}, clear=True),
            mock.patch.object(
                facade.Path,
                "home",
                return_value=Path("/factory/home"),
            ) as home_factory,
        ):
            self.assertEqual(Path("/factory/home"), facade._home())
            home_factory.assert_called_once_with()

    def test_load_config_wrapper_forwards_every_facade_dependency(self) -> None:
        facade = load_facade("session_inventory_facade_config_forwarding")
        expected = {"sentinel": True}
        with mock.patch.object(
            facade._common,
            "load_config",
            return_value=expected,
        ) as common_load:
            self.assertIs(expected, facade.load_config())
        common_load.assert_called_once_with(
            config_path=facade.config_path,
            load_json_file=facade._load_json_file,
            default_state_dir=facade.default_state_dir,
            positive_float=facade._positive_float,
            positive_int=facade._positive_int,
            valid_aliases=facade._valid_aliases,
            valid_automatic_titles=facade._valid_automatic_titles,
            valid_automatic_title_failures=facade._valid_automatic_title_failures,
            schema_version=facade.SCHEMA_VERSION,
            default_max_proc_nodes=facade.DEFAULT_MAX_PROC_NODES,
        )

    def test_common_config_kernel_uses_injected_validation(self) -> None:
        from sessionkit_inventory import common

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "inventory.json"
            path.write_text("{}", encoding="utf-8")
            load_json_file = mock.Mock(
                return_value={
                    "schema_version": 7,
                    "state_dir": "relative/state",
                    "command_timeout_seconds": "4.5",
                    "max_proc_nodes": "123",
                    "max_proc_depth": "5",
                    "aliases": {"aliases": True},
                    "automatic_titles": {"titles": True},
                    "automatic_title_failures": {"failures": True},
                }
            )
            default_state = mock.Mock(return_value=Path("/unused"))
            positive_float = mock.Mock(return_value=4.5)
            positive_int = mock.Mock(side_effect=(123, 5))
            valid_aliases = mock.Mock(return_value={"alias": "value"})
            valid_titles = mock.Mock(return_value={"title": "value"})
            valid_failures = mock.Mock(return_value={"failure": 1})
            result = common.load_config(
                config_path=lambda: path,
                load_json_file=load_json_file,
                default_state_dir=default_state,
                positive_float=positive_float,
                positive_int=positive_int,
                valid_aliases=valid_aliases,
                valid_automatic_titles=valid_titles,
                valid_automatic_title_failures=valid_failures,
                schema_version=7,
                default_max_proc_nodes=999,
            )
        self.assertEqual(
            {
                "schema_version": 7,
                "state_dir": Path("relative/state"),
                "command_timeout_seconds": 4.5,
                "max_proc_nodes": 123,
                "max_proc_depth": 5,
                "aliases": {"alias": "value"},
                "automatic_titles": {"title": "value"},
                "automatic_title_failures": {"failure": 1},
            },
            result,
        )
        load_json_file.assert_called_once_with(path)
        default_state.assert_not_called()
        positive_float.assert_called_once_with("4.5", 6.0, 0.2, 60.0)
        self.assertEqual(
            [
                mock.call("123", 999, 64, 100000),
                mock.call("5", 32, 2, 128),
            ],
            positive_int.call_args_list,
        )
        valid_aliases.assert_called_once_with({"aliases": True})
        valid_titles.assert_called_once_with({"titles": True})
        valid_failures.assert_called_once_with({"failures": True})

    def test_common_config_kernel_defaults_and_errors_are_stable(self) -> None:
        from sessionkit_inventory import common

        def load(
            path: Path,
            loader: mock.Mock,
            default_state: mock.Mock,
        ) -> dict[str, object]:
            return common.load_config(
                config_path=lambda: path,
                load_json_file=loader,
                default_state_dir=default_state,
                positive_float=common._positive_float,
                positive_int=common._positive_int,
                valid_aliases=lambda value: {},
                valid_automatic_titles=lambda value: {},
                valid_automatic_title_failures=lambda value: {},
                schema_version=1,
                default_max_proc_nodes=16384,
            )

        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            missing = base / "missing.json"
            missing_loader = mock.Mock(
                side_effect=AssertionError("missing file was loaded")
            )
            default_state = mock.Mock(return_value=Path("/default/state"))
            result = load(missing, missing_loader, default_state)
            self.assertEqual(Path("/default/state"), result["state_dir"])
            self.assertEqual(6.0, result["command_timeout_seconds"])
            self.assertEqual(16384, result["max_proc_nodes"])
            self.assertEqual(32, result["max_proc_depth"])
            missing_loader.assert_not_called()
            default_state.assert_called_once_with()

            present = base / "inventory.json"
            present.write_text("{}", encoding="utf-8")
            for error in (OSError("read failed"), ValueError("JSON failed")):
                with self.subTest(error=type(error).__name__):
                    with self.assertRaises(common.CollectionError) as raised:
                        load(
                            present,
                            mock.Mock(side_effect=error),
                            mock.Mock(return_value=Path("/default/state")),
                        )
                    self.assertEqual(
                        f"invalid config {present}: {error}",
                        str(raised.exception),
                    )
                    self.assertIs(raised.exception.__cause__, error)

            with self.assertRaisesRegex(
                common.CollectionError,
                f"invalid config {present}: top level must be an object",
            ):
                load(
                    present,
                    mock.Mock(return_value=[]),
                    mock.Mock(return_value=Path("/default/state")),
                )
            with self.assertRaisesRegex(
                common.CollectionError,
                f"unsupported config schema_version in {present}",
            ):
                load(
                    present,
                    mock.Mock(return_value={"schema_version": 2}),
                    mock.Mock(return_value=Path("/default/state")),
                )
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
