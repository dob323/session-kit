"""Provider hook registration preserves user-owned matcher content."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from tests.support import REPO

import sys

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor.provider_hooks import (  # noqa: E402
    CLAUDE_COMMAND,
    CODEX_COMMAND,
    HookConfigError,
    configure,
)


TEMPLATE = REPO / "config/codex/hooks.json"


class ProviderHookRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".provider-hooks-", dir=REPO.parent
        )
        self.root = Path(self.temporary.name)
        self.path = self.root / "settings.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, value: dict) -> None:
        self.path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        self.path.chmod(0o600)

    def read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def round_trip(self, value: dict) -> dict:
        self.write(value)
        original = self.path.read_bytes()
        configure(self.path, command=CLAUDE_COMMAND, enabled=True)
        configure(self.path, command=CLAUDE_COMMAND, enabled=False)
        self.assertEqual(original, self.path.read_bytes())
        return self.read()

    def test_absent_config_is_restored_as_absent_after_disable(self) -> None:
        configure(self.path, command=CLAUDE_COMMAND, enabled=True)
        self.assertTrue(self.path.is_file())
        configure(self.path, command=CLAUDE_COMMAND, enabled=False)
        self.assertFalse(self.path.exists())

    def test_preexisting_empty_hooks_object_is_restored_exactly(self) -> None:
        original = {"hooks": {}}
        self.assertEqual(original, self.round_trip(original))

    def test_preexisting_empty_event_group_is_restored_exactly(self) -> None:
        original = {"hooks": {"UserPromptSubmit": []}}
        self.assertEqual(original, self.round_trip(original))

    def test_colocated_user_hooks_survive_enable_and_disable_unchanged(self) -> None:
        user_handlers = [
            {
                "command": "python3 ~/.claude/hooks/quota_human_session.py",
                "timeout": 9,
                "type": "command",
            },
            {
                "command": "sh ~/.claude/hooks/nameintent_title.sh",
                "statusMessage": "Naming",
                "type": "command",
            },
            {
                # An identical command without Session Kit provenance remains
                # user-owned and must not be claimed during disable.
                "command": CLAUDE_COMMAND,
                "timeout": 3,
                "type": "command",
            },
        ]
        original = {
            "hooks": {"UserPromptSubmit": [{"hooks": user_handlers}]},
            "permissions": {"allow": ["Read"]},
        }
        self.write(original)
        original_bytes = self.path.read_bytes()
        configure(self.path, command=CLAUDE_COMMAND, enabled=True)
        active = self.read()
        self.assertEqual(user_handlers, active["hooks"]["UserPromptSubmit"][0]["hooks"][:-1])
        configure(self.path, command=CLAUDE_COMMAND, enabled=False)
        self.assertEqual(original_bytes, self.path.read_bytes())
        self.assertEqual(original, self.read())

    def test_preexisting_irregular_json_bytes_are_restored_exactly(self) -> None:
        original = (
            b'{  "permissions" : {"allow":["Read"]},\n'
            b' "hooks" : { "UserPromptSubmit" : [ { "hooks" : [\n'
            b' {"type":"command", "command":"user-hook"} ] } ] } }\n'
        )
        self.path.write_bytes(original)
        self.path.chmod(0o600)
        configure(self.path, command=CLAUDE_COMMAND, enabled=True)
        configure(self.path, command=CLAUDE_COMMAND, enabled=False)
        self.assertEqual(original, self.path.read_bytes())

    def test_absent_codex_config_matches_the_shipped_template_and_is_idempotent(self) -> None:
        configure(self.path, command=CODEX_COMMAND, enabled=True)
        self.assertEqual(TEMPLATE.read_bytes(), self.path.read_bytes())
        before = self.path.read_bytes()
        changed = configure(self.path, command=CODEX_COMMAND, enabled=True)
        self.assertFalse(changed["changed"])
        self.assertEqual(before, self.path.read_bytes())

    def test_duplicate_json_keys_are_rejected_at_any_depth(self) -> None:
        for raw in (
            b'{"hooks":{},"hooks":{}}\n',
            b'{"hooks":{"UserPromptSubmit":[],"UserPromptSubmit":[]}}\n',
            b'{"hooks":{"UserPromptSubmit":[{"hooks":[],"hooks":[]}]}}\n',
        ):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.path.chmod(0o600)
                with self.assertRaises(HookConfigError):
                    configure(self.path, command=CODEX_COMMAND, enabled=True)

    def test_symlinked_ancestor_is_rejected(self) -> None:
        real = self.root / "real"
        real.mkdir(mode=0o700)
        linked = self.root / "linked"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaises((HookConfigError, OSError)):
            configure(linked / "hooks.json", command=CODEX_COMMAND, enabled=True)
        self.assertFalse((real / "hooks.json").exists())

    def test_multiple_owned_handlers_fail_closed(self) -> None:
        configure(self.path, command=CODEX_COMMAND, enabled=True)
        value = self.read()
        owned = value["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        value["hooks"]["UserPromptSubmit"][0]["hooks"].append(owned)
        self.write(value)
        with self.assertRaises(HookConfigError):
            configure(self.path, command=CODEX_COMMAND, enabled=True)


if __name__ == "__main__":
    unittest.main()
