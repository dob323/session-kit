"""Provider hook registration preserves user-owned matcher content."""

from __future__ import annotations

import contextlib
import grp
import io
import json
import os
from pathlib import Path
import pwd
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import REPO

import sys

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_supervisor import provider_hooks  # noqa: E402
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


def host_group_is_private() -> bool:
    """Report whether this host gives the test account a private group."""
    try:
        account = pwd.getpwuid(os.geteuid())
        group = grp.getgrgid(os.getegid())
        accounts = pwd.getpwall()
    except (KeyError, OSError):  # pragma: no cover - directory service failure
        return False
    return bool(
        os.getegid() == account.pw_gid
        and group.gr_name == account.pw_name
        and not group.gr_mem
        and accounts
        and all(
            other.pw_gid != account.pw_gid or other.pw_name == account.pw_name
            for other in accounts
        )
    )


class ProviderHookDirectoryModeTests(unittest.TestCase):
    """A config directory may be written through only when no other account can.

    Distributions that create a private group per account and a 002 umask leave
    the provider config directory group-writable, which exposes it to nobody.
    Every other group-writable shape is a real exposure and stays refused.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".provider-modes-", dir=REPO.parent
        )
        self.root = Path(self.temporary.name)
        self.directory = self.root / "provider"
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "settings.json"
        self.gid = self.directory.stat().st_gid

    def tearDown(self) -> None:
        self.directory.chmod(0o700)
        self.temporary.cleanup()

    @contextlib.contextmanager
    def identity(
        self,
        *,
        account_name: str = "operator",
        account_gid: int | None = None,
        group_name: str | None = None,
        members: tuple[str, ...] = (),
        sharing_accounts: tuple[str, ...] = (),
        lookup_error: bool = False,
        enumerated: bool = True,
    ):
        """Present one passwd and group world to the module under test."""
        account_gid = self.gid if account_gid is None else account_gid
        account = SimpleNamespace(
            pw_name=account_name, pw_uid=os.geteuid(), pw_gid=account_gid
        )
        accounts = ([account] if enumerated else []) + [
            SimpleNamespace(pw_name=name, pw_uid=os.geteuid() + 1 + index, pw_gid=self.gid)
            for index, name in enumerate(sharing_accounts)
        ]

        def getgrgid(gid: int) -> SimpleNamespace:
            if lookup_error:
                raise KeyError(gid)
            return SimpleNamespace(
                gr_name=account_name if group_name is None else group_name,
                gr_gid=gid,
                gr_mem=list(members),
            )

        stub_pwd = SimpleNamespace(
            getpwuid=lambda uid: account, getpwall=lambda: list(accounts)
        )
        stub_grp = SimpleNamespace(getgrgid=getgrgid)
        with mock.patch.object(provider_hooks, "pwd", stub_pwd), mock.patch.object(
            provider_hooks, "grp", stub_grp
        ):
            yield

    def enable(self) -> None:
        configure(self.path, command=CLAUDE_COMMAND, enabled=True)

    def refusal(self) -> str:
        with self.assertRaises(HookConfigError) as caught:
            self.enable()
        self.assertFalse(self.path.exists())
        return str(caught.exception)

    def test_group_writable_private_group_directory_is_accepted(self) -> None:
        self.directory.chmod(0o775)
        with self.identity():
            self.enable()
        self.assertTrue(self.path.is_file())
        registered = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            CLAUDE_COMMAND,
            registered["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
        )
        self.assertEqual(0o775, self.directory.stat().st_mode & 0o777)

    @unittest.skipUnless(
        host_group_is_private(), "this host does not give the account a private group"
    )
    def test_group_writable_directory_is_accepted_with_this_host_identity(self) -> None:
        self.directory.chmod(0o775)
        self.enable()
        self.assertTrue(self.path.is_file())

    def test_group_writable_directory_with_another_member_is_refused(self) -> None:
        self.directory.chmod(0o775)
        with self.identity(members=("colleague",)):
            self.refusal()

    def test_group_writable_directory_of_a_shared_group_is_refused(self) -> None:
        self.directory.chmod(0o775)
        with self.identity(group_name="staff"):
            self.refusal()

    def test_group_writable_directory_outside_the_primary_group_is_refused(self) -> None:
        self.directory.chmod(0o775)
        with self.identity(account_gid=self.gid + 1):
            self.refusal()

    def test_group_named_for_the_account_with_a_second_member_account_is_refused(
        self,
    ) -> None:
        # The group carries the account name and lists no members, but a second
        # passwd account holds it as its primary group, so it is not private.
        self.directory.chmod(0o775)
        with self.identity(sharing_accounts=("understudy",)):
            self.refusal()

    def test_passwd_enumeration_without_the_account_refuses(self) -> None:
        # A directory service that does not list the account cannot show that
        # no second account holds the group, so the claim stays unproven.
        self.directory.chmod(0o775)
        with self.identity(enumerated=False):
            self.refusal()

    def test_group_lookup_failure_refuses(self) -> None:
        self.directory.chmod(0o775)
        with self.identity(lookup_error=True):
            self.refusal()

    def test_world_writable_directory_is_refused(self) -> None:
        self.directory.chmod(0o777)
        with self.identity():
            self.refusal()

    def test_directory_owned_by_another_account_is_refused(self) -> None:
        self.directory.chmod(0o755)
        impostor = os.geteuid() + 1
        with mock.patch.object(provider_hooks.os, "geteuid", lambda: impostor):
            self.refusal()

    def test_refusal_names_the_directory_and_the_repair(self) -> None:
        self.directory.chmod(0o775)
        with self.identity(group_name="staff"):
            group_writable = self.refusal()
        self.assertIn(os.fspath(self.directory), group_writable)
        self.assertIn(f"chmod g-w {self.directory}", group_writable)
        self.directory.chmod(0o777)
        with self.identity():
            world_writable = self.refusal()
        self.assertIn(f"chmod go-w {self.directory}", world_writable)

    def test_refusal_names_the_offending_ancestor_not_the_leaf(self) -> None:
        nested = self.directory / "nested"
        nested.mkdir(mode=0o700)
        self.directory.chmod(0o777)
        with self.identity():
            with self.assertRaises(HookConfigError) as caught:
                configure(nested / "settings.json", command=CLAUDE_COMMAND, enabled=True)
        self.assertIn(f"chmod go-w {self.directory}", str(caught.exception))

    def test_preflight_reports_the_repair_and_writes_nothing(self) -> None:
        self.directory.chmod(0o777)
        absent = self.root / "absent" / "hooks.json"
        stream = io.StringIO()
        with self.identity(), contextlib.redirect_stdout(stream):
            failed = provider_hooks.preflight(self.path, absent)
        reported = stream.getvalue().splitlines()
        self.assertEqual(1, failed)
        self.assertTrue(reported[0].startswith("fail\t"))
        self.assertIn(f"chmod go-w {self.directory}", reported[0])
        self.assertTrue(reported[1].startswith("ok\t"))
        self.assertFalse(self.path.exists())
        self.assertFalse(absent.parent.exists())

    def test_preflight_passes_a_private_group_directory(self) -> None:
        self.directory.chmod(0o775)
        stream = io.StringIO()
        with self.identity(), contextlib.redirect_stdout(stream):
            failed = provider_hooks.preflight(self.path)
        self.assertEqual(0, failed)
        self.assertTrue(stream.getvalue().startswith("ok\t"))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
