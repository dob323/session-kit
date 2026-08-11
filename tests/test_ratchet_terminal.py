from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.test_inventory import inventory_fixture, inventory_core, uuid_for


class SupervisorTerminalReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="terminal-supervisor-")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state"
        self.supervisor = self.state / "supervisor"
        self.supervisor.mkdir(parents=True)

    def marker(self, name: str, thread_key: str) -> None:
        path = self.supervisor / name
        path.write_text(thread_key + "\n", encoding="utf-8")
        path.chmod(0o600)

    def inventory(self, count: int) -> dict:
        return inventory_core.build_inventory(
            *inventory_fixture(count, providers=("claude",)), now=1_800_000_000
        )

    def allocate(
        self,
        inventory: dict,
        registry: dict,
        *,
        supervisor_key: str | None = None,
        previous_supervisor_key: str | None = None,
    ) -> dict:
        return inventory_core.apply_terminal_numbers(
            inventory,
            registry,
            boot_id="boot-a",
            allocate=True,
            retired={},
            supervisor_key=supervisor_key,
            previous_supervisor_key=previous_supervisor_key,
        )

    def test_fresh_boot_reserves_one_even_when_supervisor_is_not_first(self) -> None:
        self.marker("identity", f"claude:{uuid_for(2)}")
        inventory = self.inventory(2)
        registry = self.allocate(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            supervisor_key=f"ai:claude:{uuid_for(2)}",
        )
        numbers = {
            row["identity"]["uuid"]: row["terminal_number"]
            for row in inventory["sessions"]
        }
        self.assertEqual(2, numbers[uuid_for(1)])
        self.assertEqual(1, numbers[uuid_for(2)])
        self.assertEqual(1, registry["bindings"][f"ai:claude:{uuid_for(2)}"])

    def test_mid_boot_supervisor_reclaims_one_from_legacy_ordinary_session(self) -> None:
        first = self.inventory(1)
        registry = self.allocate(
            first, inventory_core._empty_terminal_registry("boot-a")
        )
        registry["bindings"] = {
            key: 1 for key in registry["bindings"]
        }
        registry["next_number"] = 2
        self.marker("identity", f"claude:{uuid_for(2)}")
        inventory = self.inventory(2)
        registry = self.allocate(
            inventory, registry, supervisor_key=f"ai:claude:{uuid_for(2)}"
        )
        numbers = [row["terminal_number"] for row in inventory["sessions"]]
        self.assertEqual([2, 1], numbers)
        self.assertEqual(2, registry["bindings"][f"ai:claude:{uuid_for(1)}"])
        self.assertEqual(1, registry["bindings"][f"ai:claude:{uuid_for(2)}"])

    def test_one_is_reserved_even_without_a_supervisor_marker(self) -> None:
        inventory = self.inventory(3)
        registry = self.allocate(
            inventory, inventory_core._empty_terminal_registry("boot-a")
        )
        self.assertEqual(
            [2, 3, 4],
            [row["terminal_number"] for row in inventory["sessions"]],
        )
        self.assertNotIn(1, registry["bindings"].values())

    def test_existing_supervisor_migrates_from_thirteen_and_retires_it(self) -> None:
        inventory = self.inventory(1)
        empty = inventory_core._empty_terminal_registry("boot-a")
        registry = self.allocate(inventory, empty)
        registry["bindings"] = {
            key: 13 for key in registry["bindings"]
        }
        registry["bindings"][f"ai:claude:{uuid_for(99)}"] = 1
        registry["next_number"] = 14
        retired: dict[int, float] = {1: 1_799_999_000}
        supervisor_key = f"ai:claude:{uuid_for(1)}"
        registry = inventory_core.apply_terminal_numbers(
            inventory,
            registry,
            boot_id="boot-a",
            allocate=True,
            retired=retired,
            current_time=1_800_000_000,
            supervisor_key=supervisor_key,
        )
        self.assertEqual(1, inventory["sessions"][0]["terminal_number"])
        self.assertEqual(1, registry["bindings"][supervisor_key])
        self.assertNotIn(13, registry["bindings"].values())
        self.assertNotIn(1, retired)
        self.assertIn(13, retired)

    def test_refresh_transfers_only_the_closed_supervisor_number(self) -> None:
        self.marker("identity", f"claude:{uuid_for(1)}")
        old = self.inventory(1)
        registry = self.allocate(
            old, inventory_core._empty_terminal_registry("boot-a")
        )
        self.marker("previous-identity", f"claude:{uuid_for(1)}")
        self.marker("identity", f"claude:{uuid_for(2)}")
        replacement = self.inventory(2)
        replacement["sessions"] = replacement["sessions"][1:]
        registry = self.allocate(
            replacement,
            registry,
            supervisor_key=f"ai:claude:{uuid_for(2)}",
            previous_supervisor_key=f"ai:claude:{uuid_for(1)}",
        )
        self.assertEqual(1, replacement["sessions"][0]["terminal_number"])
        self.assertEqual(1, registry["bindings"][f"ai:claude:{uuid_for(2)}"])
        self.assertNotIn(f"ai:claude:{uuid_for(1)}", registry["bindings"])

    def test_absent_marker_holds_one_unbound_until_the_supervisor_claims_it(self) -> None:
        # Fresh boot, marker names last boot's supervisor (absent): slot 1 is
        # held out of new allocations but bound to nobody — the every-reboot
        # case. When the recreated supervisor appears, ordinary lowest-free
        # recycling hands it exactly 1.
        ghost = f"ai:claude:{uuid_for(99)}"
        inventory = self.inventory(3)
        registry = self.allocate(
            inventory,
            inventory_core._empty_terminal_registry("boot-a"),
            supervisor_key=ghost,
        )
        first_numbers = {
            row["identity"]["uuid"]: row["terminal_number"]
            for row in inventory["sessions"]
        }
        self.assertEqual({2, 3, 4}, set(first_numbers.values()))
        self.assertNotIn(ghost, registry["bindings"])
        self.assertNotIn(1, set(registry["bindings"].values()))

        recreated = self.inventory(4)
        recreated["sessions"][3]["identity"]["uuid"] = uuid_for(99)
        supervisor_key = f"ai:claude:{uuid_for(99)}"
        registry = self.allocate(
            recreated, registry, supervisor_key=supervisor_key
        )
        numbers = {
            row["identity"]["uuid"]: row["terminal_number"]
            for row in recreated["sessions"]
        }
        self.assertEqual(1, numbers[uuid_for(99)])
        for i in (1, 2, 3):
            self.assertEqual(first_numbers[uuid_for(i)], numbers[uuid_for(i)])
        self.assertEqual(1, registry["bindings"][supervisor_key])

    def test_absent_marker_preserves_preexisting_bindings(self) -> None:
        inventory = self.inventory(2)
        registry = self.allocate(
            inventory, inventory_core._empty_terminal_registry("boot-a")
        )
        expected_registry = copy.deepcopy(registry)
        repeated = self.inventory(2)
        actual = self.allocate(
            repeated,
            registry,
            supervisor_key=f"ai:claude:{uuid_for(99)}",
        )
        self.assertEqual([2, 3], [row["terminal_number"] for row in repeated["sessions"]])
        self.assertEqual(expected_registry, actual)

    def test_no_marker_behavior_is_byte_identical(self) -> None:
        left = self.inventory(3)
        right = copy.deepcopy(left)
        empty = inventory_core._empty_terminal_registry("boot-a")
        left_registry = self.allocate(left, copy.deepcopy(empty))
        right_registry = inventory_core.apply_terminal_numbers(
            right,
            copy.deepcopy(empty),
            boot_id="boot-a",
            allocate=True,
        )
        self.assertEqual(left, right)
        self.assertEqual(left_registry, right_registry)
        self.assertNotIn(1, left_registry["bindings"].values())

    def test_numbering_does_not_read_home_or_environment_markers(self) -> None:
        self.marker("identity", f"claude:{uuid_for(2)}")
        baseline_inventory = self.inventory(2)
        with_marker_inventory = self.inventory(2)
        empty = inventory_core._empty_terminal_registry("boot-a")
        baseline = self.allocate(baseline_inventory, copy.deepcopy(empty))
        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.state.parent),
                "XDG_STATE_HOME": str(self.state.parent),
                "SESSION_KIT_STATE_DIR": str(self.state),
            },
            clear=False,
        ):
            coupled = self.allocate(with_marker_inventory, copy.deepcopy(empty))
        self.assertEqual(baseline_inventory, with_marker_inventory)
        self.assertEqual(baseline, coupled)

    def test_registry_read_injects_marker_from_its_explicit_state_path(self) -> None:
        self.marker("identity", f"claude:{uuid_for(2)}")
        registry = inventory_core._read_terminal_registry(
            self.state / "terminal-numbers.json", "boot-a"
        )
        inventory = self.inventory(2)
        registry = inventory_core.apply_terminal_numbers(
            inventory, registry, boot_id="boot-a", allocate=True
        )
        self.assertEqual([2, 1], [row["terminal_number"] for row in inventory["sessions"]])
        self.assertEqual(1, registry["bindings"][f"ai:claude:{uuid_for(2)}"])


if __name__ == "__main__":
    unittest.main()
