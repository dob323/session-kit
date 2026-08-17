"""One name wins everywhere, and a new session has one within a minute.

Two owners used to write a session's name and nothing reconciled them: the
kit's own name, and the title the provider window puts on its bar. A rename
through `sp name` reached the kit and the provider's store, but the running
window kept the title it read at start — so the list and the terminal
disagreed until something restarted, and nobody could say which was right.

The other half is the process rule paid for in a real session's death: an
unnamed session is clutter, and clutter gets closed. A session that names
nothing yet still carries the project it runs in, from its first refresh.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest

from tests.support import REPO, run
from tests.test_commands import CommandFixture, inventory_document, session_row

SP = REPO / "bin" / "sp"
CORE = REPO / "lib" / "session_inventory.py"
UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
KEY = f"claude:{UUID}"


def facade() -> object:
    sys.path.insert(0, os.fspath(REPO / "lib"))
    spec = importlib.util.spec_from_file_location("session_inventory_names", CORE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OneNameWinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = facade()

    def title(self, **kwargs: object) -> tuple[str, str]:
        return self.core._provider_title_info(
            "claude",
            UUID,
            kwargs.pop("native", ""),
            kwargs.pop("aliases", {}),
            "/srv/demo-app/v2",
            1_786_576_000_000,
            **kwargs,
        )

    def test_a_new_session_carries_its_project_from_the_first_refresh(self) -> None:
        name, source = self.title()
        self.assertEqual("context", source)
        self.assertIn("demo-app", name)
        # Never a bare provider word, and never a UUID.
        self.assertNotEqual("Claude", name)
        self.assertNotIn(UUID, name)

    def test_the_kit_name_outranks_the_bar_title_it_pushed(self) -> None:
        name, source = self.title(
            native="Kit Name",
            aliases={KEY: "Kit Name"},
            pushed_titles={KEY: "Kit Name"},
        )
        self.assertEqual(("Kit Name", "alias"), (name, source))

    def test_a_rename_typed_into_the_provider_after_the_push_wins(self) -> None:
        """Newest act, newest name: the person renamed in the window last."""
        name, source = self.title(
            native="Typed In The Window",
            aliases={KEY: "Kit Name"},
            pushed_titles={KEY: "Kit Name"},
        )
        self.assertEqual(("Typed In The Window", "native"), (name, source))

    def test_derived_placeholder_yields_to_automatic_alias(self) -> None:
        common = {
            "native": "v2-5e",
            "aliases": {KEY: "Session Kit Closeout"},
            "automatic_titles": {KEY: "Session Kit Closeout"},
            "pushed_titles": {KEY: "Session Kit Closeout"},
        }
        self.assertEqual(
            ("Session Kit Closeout", "alias"),
            self.title(**common, native_name_source="derived"),
        )
        self.assertEqual(("v2-5e", "native"), self.title(**common))

    def test_pre_push_registry_value_is_pending_but_third_value_wins(self) -> None:
        common = {
            "aliases": {KEY: "New Kit Name"},
            "automatic_titles": {KEY: "New Kit Name"},
            "pushed_titles": {KEY: "New Kit Name"},
            "pending_native_titles": {
                KEY: {
                    "title": "Old Registry Name",
                    "nameSince": 100,
                    "nameSource": "",
                }
            },
        }
        self.assertEqual(
            ("New Kit Name", "alias"),
            self.title(native="Old Registry Name", native_name_since=100, **common),
        )
        self.assertEqual(
            ("A Third Human Name", "native"),
            self.title(native="A Third Human Name", native_name_since=200, **common),
        )


class RenameReachesTheBarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CommandFixture()
        row = session_row("main1", provider="claude", uuid=UUID)
        row["title"] = "Old Name"
        row["native_title"] = "Old Name"
        row["agent_status"] = "idle"
        self.fixture.inventory.write_text(
            json.dumps(inventory_document(row)), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.fixture.close()

    def test_naming_a_session_marks_its_window_for_the_new_name(self) -> None:
        named = run([SP, "name", "main1", "Cache Sweep"], env=self.fixture.env())
        self.assertEqual("Named the Claude session Cache Sweep.\n", named.stdout)
        marker = self.fixture.state / "provider-untitled" / "main1"
        self.assertTrue(marker.is_file(), sorted(self.fixture.state.iterdir()))
        # Marking is not restarting: nothing was killed to rename a session.
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_clearing_a_name_leaves_the_window_alone(self) -> None:
        run([SP, "name", "reset", "main1"], env=self.fixture.env())
        self.assertFalse((self.fixture.state / "provider-untitled" / "main1").exists())

    def test_the_stored_name_is_the_one_the_kit_now_holds(self) -> None:
        run([SP, "name", "main1", "Cache Sweep"], env=self.fixture.env())
        stored = json.loads(self.fixture.config.read_text(encoding="utf-8"))
        self.assertEqual("Cache Sweep", stored["aliases"][KEY])


if __name__ == "__main__":
    unittest.main()
