"""Closing a session ends the terminal, never the conversation.

Before this ledger, a deliberate close left a tombstone that expired in seven
days and appeared on no screen at all: the recovery feed listed crashes only.
So the one thing a person is told they can do, close a session and pick the
conversation up later, was the one thing the kit could not do.

Every deliberate close now appends one durable record, and `sp recover` lists
those next to the crashes, newest first, until the conversation is restored or
its transcript is gone. A plain shell has no conversation to reopen and says so
rather than offering a restore that cannot work.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from tests.support import REPO, run
from tests.test_commands import CommandFixture

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_inventory import closed_sessions, lifecycle  # noqa: E402

SP = REPO / "bin" / "sp"
CORE = REPO / "lib" / "session_inventory.py"
ONE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
TWO = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"


def isolated_environment(base: Path, data: Path, state: Path) -> dict[str, str]:
    """Every ledger fixture is estate-proof, even when it grows a subprocess."""
    home = base / "home"
    binaries = base / "bin"
    for path in (home, binaries, state, data):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    shpool = binaries / "shpool"
    shpool.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'closed-sessions fixture refuses shpool: %s\\n' \"$*\" >&2\n"
        "exit 70\n",
        encoding="utf-8",
    )
    shpool.chmod(0o700)
    return {
        **os.environ,
        "PATH": f"{binaries}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "SESSION_KIT_STATE_DIR": str(state),
        "SESSION_KIT_DATA_DIR": str(data),
        "XDG_STATE_HOME": str(base / "xdg-state"),
        "XDG_DATA_HOME": str(base / "xdg-data"),
        "SESSION_KIT_CONFIG": str(base / "session-kit.toml"),
        "SESSION_KIT_SHPOOL_CMD": str(shpool),
        "SESSION_KIT_NONINTERACTIVE": "1",
    }


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".closed-", dir=REPO)
        self.base = Path(self.temp.name)
        self.data = self.base / "data"
        self.state = self.base / "state"
        self.environ = isolated_environment(self.base, self.data, self.state)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def record(self, **fields: object) -> dict:
        return closed_sessions.record_close(environ=self.environ, **fields)  # type: ignore[arg-type]

    def listed(self, **kwargs: object) -> list[dict]:
        return closed_sessions.load_closed(environ=self.environ, **kwargs)  # type: ignore[arg-type]

    def test_a_close_is_kept_and_read_back(self) -> None:
        self.record(
            provider="claude",
            uuid=ONE,
            title="Orphaned Record",
            cwd="/srv/project",
            now_unix_ms=1_000,
        )
        rows = self.listed()
        self.assertEqual(1, len(rows))
        self.assertEqual("Orphaned Record", rows[0]["title"])
        self.assertEqual("/srv/project", rows[0]["cwd"])
        self.assertTrue(rows[0]["restorable"])
        self.assertEqual(
            0o600,
            os.stat(closed_sessions.ledger_path(self.environ)).st_mode & 0o777,
        )

    def test_the_newest_close_of_a_conversation_is_the_one_listed(self) -> None:
        self.record(provider="claude", uuid=ONE, title="First", now_unix_ms=1_000)
        self.record(provider="claude", uuid=ONE, title="Second", now_unix_ms=2_000)
        self.record(provider="codex", uuid=TWO, title="Other", now_unix_ms=1_500)
        rows = self.listed()
        self.assertEqual(["Second", "Other"], [row["title"] for row in rows])

    def test_a_conversation_the_machine_cannot_read_is_history_only(self) -> None:
        """It is not offered as a restore, and it is not hidden either.

        Dropping the row left the reader with no account of a session they
        remembers closing, and the sentence written for this case could never
        print, because nothing could reach it.
        """
        self.record(provider="claude", uuid=ONE, title="Gone", now_unix_ms=1_000)
        self.record(provider="claude", uuid=TWO, title="Here", now_unix_ms=2_000)
        rows = self.listed(still_readable=lambda provider, uuid: uuid == TWO)
        self.assertEqual(["Here", "Gone"], [row["title"] for row in rows])
        self.assertEqual([True, False], [row["restorable"] for row in rows])

    def test_a_shell_close_is_listed_as_history_only(self) -> None:
        self.record(
            provider="shell", shpool_id="s1", title="A shell", now_unix_ms=1_000
        )
        rows = self.listed(still_readable=lambda provider, uuid: False)
        self.assertEqual(["A shell"], [row["title"] for row in rows])
        self.assertFalse(rows[0]["restorable"])

    def test_a_restored_conversation_leaves_the_list(self) -> None:
        self.record(provider="claude", uuid=ONE, now_unix_ms=1_000)
        self.record(provider="codex", uuid=TWO, now_unix_ms=2_000)
        self.assertEqual(1, closed_sessions.forget("claude", ONE, environ=self.environ))
        self.assertEqual([TWO], [row["uuid"] for row in self.listed()])
        self.assertEqual(0, closed_sessions.forget("claude", ONE, environ=self.environ))

    def test_a_rewrite_past_the_old_bound_preserves_unrelated_rows(self) -> None:
        self.record(provider="codex", uuid=TWO, title="Oldest", now_unix_ms=1)
        path = closed_sessions.ledger_path(self.environ)
        with path.open("ab") as handle:
            row = json.dumps(
                {
                    "provider": "shell",
                    "uuid": "",
                    "title": "f" * 120,
                    "cwd": "",
                    "closed_at_unix_ms": 2,
                    "origin": "human",
                    "shpool_id": "filler",
                    "account_alias": "",
                },
                sort_keys=True,
            ).encode("utf-8") + b"\n"
            while handle.tell() <= closed_sessions.MAX_LEDGER_BYTES:
                handle.write(row)
            handle.write(
                (json.dumps({
                    "provider": "claude",
                    "uuid": ONE,
                    "title": "Target",
                    "cwd": "",
                    "closed_at_unix_ms": 3,
                    "origin": "human",
                    "shpool_id": "",
                    "account_alias": "",
                }, sort_keys=True) + "\n").encode("utf-8")
            )
        self.assertEqual(
            1, closed_sessions.forget("claude", ONE, environ=self.environ)
        )
        self.assertIn(TWO.encode(), path.read_bytes())
        self.assertNotIn(ONE.encode(), path.read_bytes())

    def test_listing_reads_the_valid_prefix_beyond_the_rewrite_bound(self) -> None:
        self.record(provider="codex", uuid=TWO, title="Oldest", now_unix_ms=1)
        path = closed_sessions.ledger_path(self.environ)
        filler = (
            json.dumps(
                {
                    "provider": "shell",
                    "uuid": "",
                    "title": "Later shell history",
                    "cwd": "",
                    "closed_at_unix_ms": 2,
                    "origin": "human",
                    "shpool_id": "filler",
                    "account_alias": "",
                },
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        with path.open("ab") as handle:
            while handle.tell() <= closed_sessions.MAX_LEDGER_BYTES + 2048:
                handle.write(filler)

        rows = self.listed()
        self.assertIn(TWO, {row["uuid"] for row in rows})

    def test_default_listing_has_no_row_count_that_hides_older_history(self) -> None:
        path = closed_sessions.ledger_path(self.environ)
        with path.open("wb") as handle:
            for number in range(501):
                handle.write(
                    (
                        json.dumps(
                            {
                                "provider": "shell",
                                "uuid": "",
                                "title": f"Shell {number}",
                                "cwd": "",
                                "closed_at_unix_ms": number + 1,
                                "origin": "human",
                                "shpool_id": f"shell-{number}",
                                "account_alias": "",
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    ).encode("utf-8")
                )
        path.chmod(0o600)

        self.assertEqual(501, len(self.listed()))

    def test_a_pathological_listing_size_fails_loudly_without_a_partial_list(
        self,
    ) -> None:
        path = closed_sessions.ledger_path(self.environ)
        path.touch(mode=0o600)
        with path.open("r+b") as handle:
            handle.truncate(closed_sessions.MAX_LIST_LEDGER_BYTES + 1)

        with self.assertRaisesRegex(
            Exception, "complete-list safety limit.*no partial list was returned"
        ):
            self.listed()

    def test_a_close_cannot_append_past_the_listing_ceiling(self) -> None:
        self.record(provider="claude", uuid=ONE, now_unix_ms=1)
        path = closed_sessions.ledger_path(self.environ)
        before = path.read_bytes()
        # The existing ledger is accepted, but any normal production row is
        # larger than the one remaining byte. The success decision must use
        # the post-append size while the append lock is still held.
        with mock.patch.object(
            closed_sessions, "MAX_LIST_LEDGER_BYTES", len(before) + 1
        ):
            with self.assertRaisesRegex(
                Exception,
                r"would grow.*listing ceiling.*sp recover --allow-large-ledger",
            ):
                self.record(provider="codex", uuid=TWO, now_unix_ms=2)
        self.assertEqual(before, path.read_bytes())

    def test_success_requires_post_append_validation_and_rolls_back_damage(
        self,
    ) -> None:
        self.record(provider="claude", uuid=ONE, now_unix_ms=1)
        path = closed_sessions.ledger_path(self.environ)
        before = path.read_bytes()
        real_write = closed_sessions.os.write
        injected = False

        def corrupting_write(descriptor: int, payload: object) -> int:
            nonlocal injected
            raw = bytes(payload)
            written = real_write(descriptor, raw)
            if not injected and TWO.encode() in raw:
                injected = True
                real_write(descriptor, b"{not-json}\n")
            return written

        with mock.patch.object(closed_sessions.os, "write", corrupting_write):
            with self.assertRaisesRegex(Exception, "malformed row"):
                self.record(provider="codex", uuid=TWO, now_unix_ms=2)
        self.assertTrue(injected)
        self.assertEqual(before, path.read_bytes())

    def test_success_refuses_when_the_appended_inode_is_replaced_at_its_path(
        self,
    ) -> None:
        self.record(provider="claude", uuid=ONE, now_unix_ms=1)
        path = closed_sessions.ledger_path(self.environ)
        before = path.read_bytes()
        original_inode = path.stat().st_ino
        real_write_all = closed_sessions._write_all
        replaced = False

        def replace_after_append(
            descriptor: int, payload: bytes, action: str
        ) -> None:
            nonlocal replaced
            real_write_all(descriptor, payload, action)
            if not replaced and TWO.encode() in payload:
                replacement = path.with_name("valid-replacement.jsonl")
                replacement.write_bytes(before)
                replacement.chmod(0o600)
                os.replace(replacement, path)
                replaced = True

        with mock.patch.object(closed_sessions, "_write_all", replace_after_append):
            with self.assertRaisesRegex(
                Exception, "no longer names the file that was appended"
            ):
                self.record(provider="codex", uuid=TWO, now_unix_ms=2)
        self.assertTrue(replaced)
        self.assertNotEqual(original_inode, path.stat().st_ino)
        self.assertEqual(before, path.read_bytes())
        self.assertNotIn(TWO, {row["uuid"] for row in self.listed()})

    def test_post_append_validation_reads_the_appended_inode_before_authority(
        self,
    ) -> None:
        self.record(provider="claude", uuid=ONE, now_unix_ms=1)
        path = closed_sessions.ledger_path(self.environ)
        before = path.read_bytes()
        real_write_all = closed_sessions._write_all
        injected = False

        def corrupt_then_replace(
            descriptor: int, payload: bytes, action: str
        ) -> None:
            nonlocal injected
            real_write_all(descriptor, payload, action)
            if not injected and TWO.encode() in payload:
                real_write_all(descriptor, b"{not-json}\n", "injecting corruption")
                replacement = path.with_name("valid-replacement.jsonl")
                replacement.write_bytes(before)
                replacement.chmod(0o600)
                os.replace(replacement, path)
                injected = True

        with mock.patch.object(closed_sessions, "_write_all", corrupt_then_replace):
            with self.assertRaisesRegex(Exception, "malformed row"):
                self.record(provider="codex", uuid=TWO, now_unix_ms=2)
        self.assertTrue(injected)
        self.assertEqual(before, path.read_bytes())

    def test_the_reader_never_requests_file_sized_buffers(self) -> None:
        path = closed_sessions.ledger_path(self.environ)
        row = (
            json.dumps(
                {
                    "provider": "shell",
                    "uuid": "",
                    "title": "bounded",
                    "cwd": "",
                    "closed_at_unix_ms": 1,
                    "origin": "human",
                    "shpool_id": "one",
                    "account_alias": "",
                },
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        with path.open("wb") as handle:
            while handle.tell() < 256 * 1024:
                handle.write(row)
        path.chmod(0o600)
        requests: list[int] = []
        real_read = closed_sessions.os.read

        def measured_read(descriptor: int, count: int) -> bytes:
            requests.append(count)
            return real_read(descriptor, count)

        with mock.patch.object(closed_sessions.os, "read", measured_read):
            self.listed()
        self.assertTrue(requests)
        self.assertLessEqual(max(requests), 64 * 1024)

    def test_a_giant_single_row_marks_the_whole_ledger_damaged(self) -> None:
        path = closed_sessions.ledger_path(self.environ)
        path.write_text(
            json.dumps(
                {
                    "provider": "shell",
                    "uuid": "",
                    "title": "x" * (128 * 1024),
                    "cwd": "",
                    "closed_at_unix_ms": 1,
                    "origin": "human",
                    "shpool_id": "giant",
                    "account_alias": "",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        with self.assertRaisesRegex(Exception, "row 1 exceeds"):
            self.listed()

    def test_a_malformed_ledger_is_not_an_empty_read_or_a_rewrite_source(self) -> None:
        self.record(provider="claude", uuid=ONE, now_unix_ms=1)
        path = closed_sessions.ledger_path(self.environ)
        with path.open("ab") as handle:
            handle.write(b"{not-json}\n")
        before = path.read_bytes()
        with self.assertRaisesRegex(Exception, "malformed row"):
            self.listed()
        with self.assertRaisesRegex(Exception, "malformed row"):
            closed_sessions.forget("claude", ONE, environ=self.environ)
        self.assertEqual(before, path.read_bytes())

    def test_restore_rewrite_streams_past_the_old_four_mib_limit(self) -> None:
        self.record(provider="codex", uuid=TWO, title="preserved", now_unix_ms=1)
        path = closed_sessions.ledger_path(self.environ)
        preserved = path.read_bytes()
        filler = (
            json.dumps(
                {
                    "provider": "shell",
                    "uuid": "",
                    "title": "filler",
                    "cwd": "",
                    "closed_at_unix_ms": 2,
                    "origin": "human",
                    "shpool_id": "filler",
                    "account_alias": "",
                },
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        with path.open("ab") as handle:
            while handle.tell() <= closed_sessions.MAX_LEDGER_BYTES + 2048:
                handle.write(filler)
            handle.write(
                (
                    json.dumps(
                        {
                            "provider": "claude",
                            "uuid": ONE,
                            "title": "target",
                            "cwd": "",
                            "closed_at_unix_ms": 3,
                            "origin": "human",
                            "shpool_id": "",
                            "account_alias": "",
                        },
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
            )
        self.assertEqual(
            1, closed_sessions.forget("claude", ONE, environ=self.environ)
        )
        self.assertTrue(path.read_bytes().startswith(preserved))
        self.assertIn(TWO.encode(), path.read_bytes())
        self.assertNotIn(ONE.encode(), path.read_bytes())

    def test_restore_refuses_when_its_published_inode_is_replaced(self) -> None:
        self.record(provider="claude", uuid=ONE, now_unix_ms=1)
        self.record(provider="codex", uuid=TWO, now_unix_ms=2)
        path = closed_sessions.ledger_path(self.environ)
        before = path.read_bytes()
        real_replace = closed_sessions.os.replace
        interposed = False

        def replace_then_interpose(source: object, destination: object) -> None:
            nonlocal interposed
            real_replace(source, destination)
            if Path(destination) == path and not interposed:
                replacement = path.with_name("restore-interposed.jsonl")
                replacement.write_bytes(before)
                replacement.chmod(0o600)
                real_replace(replacement, path)
                interposed = True

        with mock.patch.object(
            closed_sessions.os, "replace", replace_then_interpose
        ):
            with self.assertRaisesRegex(
                Exception, "no longer names the file that was published"
            ):
                closed_sessions.forget("claude", ONE, environ=self.environ)
        self.assertTrue(interposed)
        self.assertEqual(before, path.read_bytes())

    def test_restore_validates_the_rewrite_through_its_written_descriptor(
        self,
    ) -> None:
        self.record(provider="claude", uuid=ONE, now_unix_ms=1)
        self.record(provider="codex", uuid=TWO, now_unix_ms=2)
        path = closed_sessions.ledger_path(self.environ)
        before = path.read_bytes()
        real_write_all = closed_sessions._write_all
        injected = False

        def corrupt_rewrite(descriptor: int, payload: bytes, action: str) -> None:
            nonlocal injected
            real_write_all(descriptor, payload, action)
            if action == "rewriting closed sessions" and not injected:
                real_write_all(descriptor, b"{not-json}\n", "injecting corruption")
                injected = True

        with mock.patch.object(closed_sessions, "_write_all", corrupt_rewrite):
            with self.assertRaisesRegex(Exception, "malformed row"):
                closed_sessions.forget("claude", ONE, environ=self.environ)
        self.assertTrue(injected)
        self.assertEqual(before, path.read_bytes())

    def test_a_restore_rewrite_serializes_with_a_new_close(self) -> None:
        self.record(provider="claude", uuid=ONE, now_unix_ms=1)
        reached_rewrite = threading.Event()
        allow_rewrite = threading.Event()
        real_tempfile = closed_sessions.tempfile
        real_mkstemp = real_tempfile.mkstemp

        def held_mkstemp(*args, **kwargs):
            reached_rewrite.set()
            self.assertTrue(allow_rewrite.wait(timeout=5))
            return real_mkstemp(*args, **kwargs)

        closed_sessions.tempfile = types.SimpleNamespace(mkstemp=held_mkstemp)
        outcomes: dict[str, object] = {}

        def restore() -> None:
            outcomes["forgotten"] = closed_sessions.forget(
                "claude", ONE, environ=self.environ
            )

        def close() -> None:
            outcomes["closed"] = self.record(
                provider="codex", uuid=TWO, now_unix_ms=2
            )

        restore_thread = threading.Thread(target=restore)
        close_thread = threading.Thread(target=close)
        try:
            restore_thread.start()
            self.assertTrue(reached_rewrite.wait(timeout=5))
            close_thread.start()
            close_thread.join(timeout=0.1)
            self.assertTrue(close_thread.is_alive(), "close bypassed the ledger transaction")
            allow_rewrite.set()
            restore_thread.join(timeout=5)
            close_thread.join(timeout=5)
        finally:
            allow_rewrite.set()
            closed_sessions.tempfile = real_tempfile
        self.assertFalse(restore_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(1, outcomes["forgotten"])
        self.assertEqual([TWO], [row["uuid"] for row in self.listed()])

    def test_two_legal_short_write_appenders_cannot_interleave(self) -> None:
        real_write = closed_sessions.os.write
        condition = threading.Condition()
        started: set[str] = set()
        turn = "writer-a"
        serialized = False
        failures: list[BaseException] = []

        def short_write(descriptor: int, payload: object) -> int:
            nonlocal turn, serialized
            name = threading.current_thread().name
            other = "writer-b" if name == "writer-a" else "writer-a"
            with condition:
                started.add(name)
                condition.notify_all()
                if len(started) < 2 and not condition.wait_for(
                    lambda: len(started) == 2, timeout=0.1
                ):
                    serialized = True
                    condition.notify_all()
                if not serialized:
                    self.assertTrue(
                        condition.wait_for(lambda: turn == name, timeout=5)
                    )
                written = real_write(descriptor, bytes(payload)[:7])
                if not serialized:
                    turn = other
                    condition.notify_all()
                return written

        def writer(provider: str, uuid: str) -> None:
            try:
                self.record(provider=provider, uuid=uuid, now_unix_ms=1)
            except BaseException as exc:
                failures.append(exc)

        closed_sessions.os.write = short_write
        workers = [
            threading.Thread(target=writer, name="writer-a", args=("claude", ONE)),
            threading.Thread(target=writer, name="writer-b", args=("codex", TWO)),
        ]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
        finally:
            closed_sessions.os.write = real_write
        self.assertEqual([], failures)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual({ONE, TWO}, {row["uuid"] for row in self.listed()})

    def test_a_record_with_no_conversation_and_no_shell_is_refused(self) -> None:
        with self.assertRaises(Exception):
            self.record(provider="claude", uuid="", title="Nameless")

    def test_a_close_learns_what_it_did_not_know_from_the_last_list(self) -> None:
        inventory = {
            "sessions": [
                {
                    "provider": "claude",
                    "identity": {"uuid": ONE},
                    "title": "Cache Sweep",
                    "cwd": "/srv/kit",
                    "origin": "machine",
                    "origin_recorded": "machine",
                    "account_alias": "work",
                }
            ]
        }
        found = closed_sessions.entry_from_inventory(
            inventory, provider="claude", uuid=ONE
        )
        self.assertEqual("Cache Sweep", found["title"])
        self.assertEqual("/srv/kit", found["cwd"])
        self.assertEqual("machine", found["origin"])

    def test_a_close_records_the_stamp_and_never_a_reading_of_the_moment(
        self,
    ) -> None:
        """This row outlives the session and is read on every future restore.

        For an unstamped session `origin` is whatever collection inferred
        about who held a socket at that instant, and a close landing while a
        provider restarts infers "a program". Written here, that instant
        becomes a permanent machine record for a conversation nobody ever
        stamped: restored later it comes back a machine's, and no refresh can
        take it back. No stamp means no claim, and no claim reads as the
        person's.
        """
        inventory = {
            "sessions": [
                {
                    "provider": "claude",
                    "identity": {"uuid": ONE},
                    "title": "Mid Bounce",
                    "cwd": "/srv/kit",
                    "origin": "machine",
                }
            ]
        }

        found = closed_sessions.entry_from_inventory(
            inventory, provider="claude", uuid=ONE
        )

        self.assertEqual("Mid Bounce", found["title"])
        # "unknown", not "human": the ledger records what was known, and
        # nothing was. Every reader treats it as the person's, which is what
        # an unproven session is -- but it is not written down as a claim.
        self.assertEqual("unknown", found["origin"])

    def test_a_ledger_that_opens_but_does_not_parse_is_not_a_ledger_with_no_rows(
        self,
    ) -> None:
        """The difference a decision turns on, which opening the file misses.

        The ledger is the only thing that says a conversation was the
        person's, so "no rows" is answered from somewhere else. A truncated
        write and an empty file both yield no rows; only one of them is a
        fact, and a reader that cannot tell them apart lets a crash mid-write
        decide whose session this is.
        """
        environ = self.environ
        path = closed_sessions.ledger_path(environ)
        path.parent.mkdir(parents=True, exist_ok=True)
        cases = {
            "no ledger yet": (None, True),
            "an empty ledger": ("", True),
            "one whole row": (
                '{"provider": "codex", "uuid": "%s", "closed_at_unix_ms": 1, '
                '"origin": "human"}\n' % ONE,
                True,
            ),
            "a row cut off mid-write": (
                '{"provider": "codex", "uuid": "%s", "closed_at_unix' % ONE,
                False,
            ),
            "a row this reader would skip": (
                '{"provider": "codex", "uuid": "not-a-uuid", "closed_at_unix_ms": 1}\n',
                False,
            ),
        }
        for label, (payload, expected) in cases.items():
            with self.subTest(label=label):
                if payload is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(payload, encoding="utf-8")
                    path.chmod(0o600)
                self.assertEqual(
                    expected, closed_sessions.ledger_is_readable(environ)
                )

    def test_an_unknown_session_teaches_the_close_nothing(self) -> None:
        found = closed_sessions.entry_from_inventory(
            {"sessions": []}, provider="claude", uuid=ONE
        )
        self.assertEqual(
            {
                "title": "",
                "title_source": "",
                "cwd": "",
                "origin": "unknown",
                "account_alias": "",
            },
            found,
        )


class LedgerThroughTheCoreTests(unittest.TestCase):
    """Every deliberate close writes the ledger, whoever performed it."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".closed-core-", dir=REPO)
        self.base = Path(self.temp.name)
        self.data = self.base / "data"
        self.state = self.base / "state"
        self.environ = isolated_environment(self.base, self.data, self.state)
        (self.state / "inventory.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sessions": [
                        {
                            "provider": "claude",
                            "shpool_id_raw": "s1",
                            "identity": {"uuid": ONE},
                            "title": "Named By The List",
                            "cwd": "/srv/project",
                            "origin": "machine",
                            "origin_recorded": "machine",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.state / "inventory.json").chmod(0o600)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def core(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CORE), *argv],
            env=self.environ,
            capture_output=True,
            text=True,
        )

    def sp(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SP), *argv],
            env=self.environ,
            capture_output=True,
            text=True,
        )

    def test_recording_a_close_intent_also_lists_the_conversation(self) -> None:
        done = self.core("close-intent", "record", "claude", ONE)
        self.assertEqual(0, done.returncode, done.stderr)
        entries = [
            json.loads(line)
            for line in (self.data / closed_sessions.LEDGER_NAME)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(1, len(entries))
        # The picker knows the row it closed; `sp close` knows it too. Neither
        # has to carry the fields, because the last list already had them.
        self.assertEqual("Named By The List", entries[0]["title"])
        self.assertEqual("/srv/project", entries[0]["cwd"])
        self.assertEqual("machine", entries[0]["origin"])

    def test_the_list_verb_answers_with_what_was_recorded(self) -> None:
        self.core(
            "closed-sessions",
            "record",
            "shell",
            "--session",
            "s1",
            "--title",
            "A shell",
        )
        listed = self.core("closed-sessions", "list")
        self.assertEqual(0, listed.returncode, listed.stderr)
        rows = json.loads(listed.stdout)["closed"]
        self.assertEqual(["A shell"], [row["title"] for row in rows])
        self.assertFalse(rows[0]["restorable"])

    def test_an_unreadable_existing_ledger_makes_list_fail(self) -> None:
        self.core("closed-sessions", "record", "shell", "--session", "s1")
        ledger = self.data / closed_sessions.LEDGER_NAME
        ledger.chmod(0)
        try:
            listed = self.core("closed-sessions", "list")
        finally:
            ledger.chmod(0o600)
        self.assertNotEqual(0, listed.returncode)
        self.assertIn("cannot open closed-sessions ledger", listed.stderr)

    def test_recover_warns_when_the_real_ledger_reader_fails(self) -> None:
        self.core("closed-sessions", "record", "shell", "--session", "s1")
        ledger = self.data / closed_sessions.LEDGER_NAME
        ledger.chmod(0)
        try:
            shown = self.sp("recover")
        finally:
            ledger.chmod(0o600)
        self.assertNotEqual(0, shown.returncode, shown.stderr)
        self.assertIn("closed-conversations list could not be read", shown.stderr)
        self.assertNotIn("Closed conversations: none.", shown.stdout)

    def test_a_large_noop_forget_validates_without_rewriting(self) -> None:
        self.core("closed-sessions", "record", "shell", "--session", "s1")
        ledger = self.data / closed_sessions.LEDGER_NAME
        with ledger.open("ab") as handle:
            handle.write(b" \n" * (closed_sessions.MAX_LEDGER_BYTES // 2 + 1))
        before = ledger.read_bytes()
        forgotten = self.core("closed-sessions", "forget", "claude", ONE)
        self.assertEqual(0, forgotten.returncode, forgotten.stderr)
        self.assertEqual(0, json.loads(forgotten.stdout)["forgotten"])
        self.assertEqual(before, ledger.read_bytes())

    def test_core_and_recover_both_reach_a_tombstoned_row_past_four_mib(
        self,
    ) -> None:
        transcript = (
            self.base
            / "home/.codex/sessions/2026/08/15"
            / f"rollout-2026-08-15T03-00-00-{ONE}.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": ONE}}) + "\n",
            encoding="utf-8",
        )
        recorded = self.core(
            "closed-sessions",
            "record",
            "codex",
            "--uuid",
            ONE,
            "--title",
            "Old Closed Conversation",
        )
        self.assertEqual(0, recorded.returncode, recorded.stderr)
        lifecycle.record_close_intent(
            self.state, provider="codex", uuid=ONE
        )
        ledger = self.data / closed_sessions.LEDGER_NAME
        with ledger.open("ab") as handle:
            number = 0
            while handle.tell() <= closed_sessions.MAX_LEDGER_BYTES + 2048:
                number += 1
                handle.write(
                    (
                        json.dumps(
                            {
                                "provider": "shell",
                                "uuid": "",
                                "title": "Later shell history",
                                "cwd": "",
                                "closed_at_unix_ms": number + 2,
                                "origin": "human",
                                "shpool_id": f"filler-{number}",
                                "account_alias": "",
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    ).encode("utf-8")
                )

        listed = self.core("closed-sessions", "list")
        shown = self.sp("recover")
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertIn(ONE, listed.stdout)
        self.assertEqual(0, shown.returncode, shown.stderr)
        self.assertIn("Old Closed Conversation", shown.stdout)
        self.assertNotIn("Argument list too long", shown.stderr)

    def test_core_and_recover_warn_instead_of_listing_a_pathological_tail(
        self,
    ) -> None:
        ledger = self.data / closed_sessions.LEDGER_NAME
        ledger.touch(mode=0o600)
        with ledger.open("r+b") as handle:
            handle.truncate(closed_sessions.MAX_LIST_LEDGER_BYTES + 1)
        size_before = ledger.stat().st_size

        listed = self.core("closed-sessions", "list")
        shown = self.sp("recover")
        self.assertNotEqual(0, listed.returncode)
        self.assertIn("complete-list safety limit", listed.stderr)
        self.assertIn("no partial list was returned", listed.stderr)
        self.assertNotEqual(0, shown.returncode)
        self.assertIn("complete-list safety limit", shown.stderr)
        self.assertIn("no partial list was returned", shown.stderr)
        self.assertIn("sp recover --allow-large-ledger", shown.stderr)
        self.assertIn("closed-conversations list could not be read", shown.stderr)
        self.assertNotIn("Closed conversations: none.", shown.stdout)
        self.assertEqual(size_before, ledger.stat().st_size)

    def test_large_ledger_recovery_override_is_a_shipped_way_back(self) -> None:
        transcript = (
            self.base
            / "home/.codex/sessions/2026/08/15"
            / f"rollout-2026-08-15T03-00-00-{ONE}.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": ONE}}) + "\n",
            encoding="utf-8",
        )
        ledger = self.data / closed_sessions.LEDGER_NAME
        ledger.write_text(
            json.dumps(
                {
                    "provider": "codex",
                    "uuid": ONE,
                    "title": "Large ledger way back",
                    "cwd": str(self.base),
                    "closed_at_unix_ms": 1,
                    "origin": "human",
                    "shpool_id": "",
                    "account_alias": "",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        ledger.chmod(0o600)
        wrapper = self.base / "small-ceiling-core"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"sys.path.insert(0, {str(CORE.parent)!r})\n"
            "from sessionkit_inventory import closed_sessions\n"
            "closed_sessions.MAX_LIST_LEDGER_BYTES = 1\n"
            "import session_inventory\n"
            "raise SystemExit(session_inventory.main())\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        env = {**self.environ, "SESSION_KIT_INVENTORY_CORE": str(wrapper)}

        refused = subprocess.run(
            [str(SP), "recover"], env=env, capture_output=True, text=True
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("sp recover --allow-large-ledger", refused.stderr)
        self.assertNotIn("Closed conversations: none.", refused.stdout)

        recovered = subprocess.run(
            [str(SP), "recover", "--allow-large-ledger"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, recovered.returncode, recovered.stderr)
        self.assertIn("Large ledger way back", recovered.stdout)
        self.assertIn(
            "sp restore --allow-large-ledger number", recovered.stdout
        )

    def test_complete_boundary_matrix_uses_one_listability_rule(self) -> None:
        ceiling = 16 * 1024
        ledger = self.data / closed_sessions.LEDGER_NAME
        for uuid in (ONE, TWO):
            transcript = (
                self.base
                / "home/.codex/sessions/2026/08/15"
                / f"rollout-2026-08-15T03-00-00-{uuid}.jsonl"
            )
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": uuid}})
                + "\n",
                encoding="utf-8",
            )
        wrapper = self.base / "matrix-core"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"sys.path.insert(0, {str(CORE.parent)!r})\n"
            "from sessionkit_inventory import closed_sessions\n"
            f"closed_sessions.MAX_LIST_LEDGER_BYTES = {ceiling}\n"
            "import session_inventory\n"
            "raise SystemExit(session_inventory.main())\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        env = {**self.environ, "SESSION_KIT_INVENTORY_CORE": str(wrapper)}

        def install(size: int) -> bytes:
            row = {
                "provider": "codex",
                "uuid": ONE,
                "title": "Boundary target",
                "cwd": str(self.base),
                "closed_at_unix_ms": 1,
                "origin": "human",
                "shpool_id": "",
                "account_alias": "",
                "padding": "",
            }
            raw = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
            row["padding"] = "x" * (size - len(raw))
            raw = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
            self.assertEqual(size, len(raw))
            ledger.write_bytes(raw)
            ledger.chmod(0o600)
            close_intents = self.state / "closed-conversations.json"
            close_intents.unlink(missing_ok=True)
            return raw

        states = {
            "ceiling_minus_one": ceiling - 1,
            "exactly_ceiling": ceiling,
            "one_over": ceiling + 1,
            "far_over": ceiling + 4096,
        }

        # Normal list and recovery accept the complete states through the
        # ceiling. Above it they refuse the whole file, and the shipped
        # deliberate override is the exact way back to every preserved row.
        for name, size in states.items():
            with self.subTest(operation="list-recover", state=name):
                install(size)
                listed = subprocess.run(
                    [str(wrapper), "closed-sessions", "list"],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                recovered = subprocess.run(
                    [str(SP), "recover"], env=env, capture_output=True, text=True
                )
                if size <= ceiling:
                    self.assertEqual(0, listed.returncode, listed.stderr)
                    self.assertEqual(0, recovered.returncode, recovered.stderr)
                    self.assertIn("Boundary target", recovered.stdout)
                else:
                    self.assertNotEqual(0, listed.returncode)
                    self.assertNotEqual(0, recovered.returncode)
                    self.assertNotIn("Closed conversations: none.", recovered.stdout)
                    self.assertIn(
                        "sp recover --allow-large-ledger", recovered.stderr
                    )
                    override = subprocess.run(
                        [str(SP), "recover", "--allow-large-ledger"],
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(0, override.returncode, override.stderr)
                    self.assertIn("Boundary target", override.stdout)

        # Every close whose row would cross (including from one byte below),
        # or which begins above, is byte-identical on refusal and writes no
        # tombstone. A comfortably-below control succeeds and stays listable.
        close_states = {
            **states,
            "append_crossing": ceiling - 100,
        }
        for name, size in close_states.items():
            with self.subTest(operation="close", state=name):
                before = install(size)
                closed = subprocess.run(
                    [str(wrapper), "close-intent", "record", "codex", TWO],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(0, closed.returncode, closed.stdout)
                self.assertFalse(json.loads(closed.stdout)["recorded"])
                self.assertIn(str(ceiling), closed.stdout)
                self.assertIn("sp recover --allow-large-ledger", closed.stdout)
                self.assertEqual(before, ledger.read_bytes())
                self.assertFalse(
                    lifecycle.closed_on_purpose(
                        self.state, provider="codex", uuid=TWO
                    )
                )
        install(ceiling - 512)
        accepted = subprocess.run(
            [str(wrapper), "close-intent", "record", "codex", TWO],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, accepted.returncode, accepted.stdout)
        self.assertTrue(json.loads(accepted.stdout)["recorded"])
        self.assertLessEqual(ledger.stat().st_size, ceiling)
        self.assertEqual(
            0,
            subprocess.run(
                [str(wrapper), "closed-sessions", "list"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode,
        )

        # Restore cleanup uses the same complete validator and original-byte
        # transaction but no obsolete file-size refusal, so all four states
        # can remove the target after its transcript has been recovered.
        for name, size in states.items():
            with self.subTest(operation="restore", state=name):
                install(size)
                forgotten = subprocess.run(
                    [str(wrapper), "closed-sessions", "forget", "codex", ONE],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, forgotten.returncode, forgotten.stderr)
                self.assertEqual(1, json.loads(forgotten.stdout)["forgotten"])
                self.assertEqual(b"", ledger.read_bytes())


class RecoverFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CommandFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def manifest(self, rows: dict) -> None:
        path = self.fixture.state / "recovery-manifest.json"
        path.write_text(json.dumps({"sessions": rows}), encoding="utf-8")
        path.chmod(0o600)

    def closed_ledger(self, *rows: dict) -> None:
        """Write the real ledger the one projection reads.

        These used to be handed to the shell through STUB_CLOSED_LIST, back
        when the CLI merged the stores itself. There is one reader now, so
        the fixture writes the store rather than the answer.
        """
        data = self.fixture.home / ".local" / "share" / "session-kit"
        data.mkdir(parents=True, exist_ok=True)
        (data / "closed-sessions.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        for row in rows:
            if row.get("provider") == "shell" or not row.get("uuid"):
                continue
            if row.get("provider") == "codex":
                transcript = (
                    self.fixture.home
                    / ".codex"
                    / "sessions"
                    / "2026"
                    / "08"
                    / "15"
                    / f"rollout-2026-08-15T03-00-00-{row['uuid']}.jsonl"
                )
            else:
                transcript = (
                    self.fixture.home
                    / ".claude"
                    / "projects"
                    / "-srv-project"
                    / f"{row['uuid']}.jsonl"
                )
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text('{"type":"user"}\n', encoding="utf-8")

    def numbers(self, **bindings: int) -> None:
        """The numbers these conversations already have."""
        path = self.fixture.state / "terminal-numbers.json"
        path.write_text(
            json.dumps({"schema_version": 1, "bindings": bindings}),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def test_an_empty_feed_says_none_once(self) -> None:
        shown = run([SP, "recover"], env=self.fixture.env())
        self.assertEqual("Closed conversations: none.\n", shown.stdout)

    def test_a_conversation_that_is_live_right_now_is_not_offered(self) -> None:
        """A crash record whose conversation is open in the inventory is a
        leftover, not lost work: restoring it would collide with the live
        session (seen live 2026-08-12 after an estate-wide restart sweep)."""
        self.manifest(
            {
                "s9": {
                    "provider": "codex",
                    "uuid": TWO,
                    "title": "Still Alive And Open",
                    "cwd": "/srv/project",
                    "crashed_at_unix_ms": 1_000,
                }
            }
        )
        inventory = self.fixture.state / "inventory.json"
        inventory.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sessions": [
                        {
                            "provider": "codex",
                            "shpool_id_raw": "s77",
                            "identity": {"uuid": TWO},
                            "title": "Still Alive And Open",
                            "cwd": "/srv/project",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        inventory.chmod(0o600)
        shown = run([SP, "recover"], env=self.fixture.env())
        self.assertEqual("Closed conversations: none.\n", shown.stdout)

    def test_closed_conversations_and_lost_ones_share_one_list(self) -> None:
        self.manifest(
            {
                "s9": {
                    "provider": "codex",
                    "uuid": TWO,
                    "title": "Lost To A Crash",
                    "cwd": "/srv/project",
                    "crashed_at_unix_ms": 1_000,
                }
            }
        )
        self.closed_ledger(
            {
                "provider": "claude",
                "uuid": ONE,
                "title": "Closed On Purpose",
                "title_source": "alias",
                "cwd": "/srv/project",
                "closed_at_unix_ms": 2_000,
                "origin": "human",
            },
            {
                "provider": "shell",
                "uuid": "",
                "title": "A Shell",
                "cwd": "/srv/project",
                "closed_at_unix_ms": 1_500,
                "origin": "human",
                "shpool_id": "s-shell",
            },
        )
        self.numbers(**{f"ai:claude:{ONE}": 11, f"ai:codex:{TWO}": 12})
        env = self.fixture.env()
        shown = run([SP, "recover"], env=env)
        lines = shown.stdout.splitlines()
        self.assertIn("Closed On Purpose", lines[0])
        # A shell never had a name; it says so instead of borrowing one.
        self.assertIn("unnamed", lines[1])
        self.assertIn("no conversation to reopen", lines[2])
        self.assertIn("Lost To A Crash", lines[3])
        self.assertIn("lost with its session", lines[3])
        self.assertRegex(lines[0], r" · \d+ days ago")
        self.assertRegex(lines[3], r" · \d+ days ago")
        self.assertEqual(
            "Bring one back with: sp restore <number>, or the selector beside"
            " a session that has none.",
            lines[-1],
        )
        # No screen prints an ID.
        self.assertNotIn(ONE, shown.stdout)
        self.assertNotIn(TWO, shown.stdout)

    def test_recovery_scrubs_terminal_controls_without_per_row_filters(self) -> None:
        env = self.fixture.env()
        self.closed_ledger(
            {
                "provider": "codex",
                "uuid": TWO,
                "title": "\x1b[31mClosed\x07 Conversation",
                "title_source": "alias",
                "cwd": "/srv/project",
                "closed_at_unix_ms": 2_000,
                "origin": "human",
            }
        )

        shown = run([SP, "recover"], env=env)
        self.assertNotIn("\x1b", shown.stdout)
        self.assertNotIn("\x07", shown.stdout)
        self.assertIn("[31mClosed Conversation", shown.stdout)

    def test_restore_brings_back_the_conversation_by_its_number(self) -> None:
        self.closed_ledger(
            {
                "provider": "claude",
                "uuid": ONE,
                "title": "Closed On Purpose",
                "title_source": "alias",
                "cwd": str(self.fixture.project),
                "closed_at_unix_ms": 2_000,
                "origin": "human",
            }
        )
        self.numbers(**{f"ai:claude:{ONE}": 42})
        env = self.fixture.env()
        env.update(
            {
                "STUB_DYNAMIC_PROVIDER": "claude",
                "STUB_DYNAMIC_UUID": ONE,
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
            }
        )
        # By the number it has everywhere else, not by a row position.
        restored = run([SP, "restore", "42"], env=env)
        self.assertEqual("Restored Closed On Purpose.\n", restored.stdout)
        self.assertTrue(self.fixture.shpool_log.read_text().startswith("attach "))

    def test_large_ledger_selector_uses_the_matching_restore_stream(self) -> None:
        env = self.fixture.env()
        self.closed_ledger(
            {
                "provider": "codex",
                "uuid": TWO,
                "title": "Large Closed On Purpose",
                "title_source": "alias",
                "cwd": str(self.fixture.project),
                "closed_at_unix_ms": 2_000,
                "origin": "human",
            }
        )
        env.update(
            {
                "STUB_DYNAMIC_PROVIDER": "codex",
                "STUB_DYNAMIC_UUID": TWO,
                "STUB_DYNAMIC_CWD": str(self.fixture.project),
                "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
            }
        )
        restored = run(
            [
                SP,
                "restore",
                "--allow-large-ledger",
                "Large Closed On Purpose",
            ],
            env=env,
        )
        self.assertEqual("Restored Large Closed On Purpose.\n", restored.stdout)
        self.assertTrue(self.fixture.shpool_log.read_text().startswith("attach "))

    def test_a_shell_row_offers_history_rather_than_a_restore(self) -> None:
        self.closed_ledger(
            {
                "provider": "shell",
                "uuid": "",
                "title": "A Shell",
                "cwd": str(self.fixture.project),
                "closed_at_unix_ms": 2_000,
                "origin": "human",
                "shpool_id": "s-shell",
            }
        )
        env = self.fixture.env()

        # The list says so in a sentence, under the row it belongs to. A shell
        # never had a conversation, so it never had a number either -- there
        # is nothing to type, and nothing that could be restored if there were.
        shown = run([SP, "recover"], env=env)
        lines = shown.stdout.splitlines()
        self.assertIn("unnamed", lines[0])
        self.assertIn("no conversation to reopen", lines[1])
        self.assertFalse(self.fixture.shpool_log.exists())

    def test_a_number_that_is_not_on_the_list_is_refused_in_one_line(self) -> None:
        refused = run([SP, "restore", "20"], env=self.fixture.env(), check=False)
        self.assertEqual(2, refused.returncode)
        self.assertEqual(
            "session-kit: there is no session 20 on that list. Type what the"
            " row shows: its number, or the selector beside a session that has"
            " none\n",
            refused.stderr,
        )


if __name__ == "__main__":
    unittest.main()
