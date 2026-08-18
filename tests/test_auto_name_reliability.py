"""A Codex session carries its real name, whatever else is in the store.

The Codex titler used to look at the 200 most recent threads, ignore anything
older than seven days, stop after twenty names and twenty repairs per pass, and
return silently when its database could not be read. Claude's side is
event-driven and named everything. So a Codex thread past any one of those
edges stayed nameless on every surface -- picker, tab, and bounce -- with
nothing anywhere saying why.

The bound is now time, not count or age: a pass ends when its budget runs out
and the next one continues, so a backlog converges instead of being abandoned.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.support import REPO


def names_push():
    library = os.fspath(REPO / "lib")
    if library not in sys.path:
        sys.path.insert(0, library)
    return importlib.import_module("sessionkit_inventory.names_push")


def uuid_for(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


class CodexTitlerTests(unittest.TestCase):
    def store(self, root: Path, threads: list[tuple[str, str, str, float]]) -> Path:
        """A Codex home whose thread store holds exactly these threads."""
        codex = root / ".codex"
        codex.mkdir(parents=True, mode=0o700)
        database = codex / "state.db"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT,"
            " first_user_message TEXT, updated_at REAL, thread_source TEXT)"
        )
        connection.executemany(
            "INSERT INTO threads (id, title, first_user_message, updated_at,"
            " thread_source) VALUES (?, ?, ?, ?, 'user')",
            threads,
        )
        connection.commit()
        connection.close()
        return codex

    def run_pass(self, codex: Path, home: Path, **extra):
        module = names_push()
        titled = []

        def derive_title(first: str) -> str:
            return " ".join(str(first).split()[:7]).title()

        owners: dict[str, str] = {}

        def claim(_provider, uuid, **_keywords):
            if owners.get(uuid):
                return f"a {owners[uuid]} name already owns this session"
            owners[uuid] = "automatic"
            return ""

        def release(_provider, uuid, **_keywords):
            if owners.get(uuid) != "automatic":
                return False
            owners.pop(uuid, None)
            return True

        # `claim_automatic_name` returns "" when the claim LANDED and the
        # reason it did not otherwise; the default here is a claim that lands.
        extra.setdefault("claim_name", claim)
        extra.setdefault("release_claim", release)
        extra.setdefault("record_pushed", lambda *_a, **_k: None)
        extra.setdefault(
            "name_owner", lambda _provider, uuid, **_keywords: owners.get(uuid, "")
        )
        extra.setdefault(
            "human_named",
            lambda _environ: frozenset(
                f"codex:{uuid}" for uuid, owner in owners.items() if owner == "human"
            ),
        )
        extra.setdefault("adopt_native", lambda *_a, **_k: "")
        environ = extra.pop(
            "environ",
            {"HOME": os.fspath(home), "SESSION_KIT_CODEX_LIVE_RENAME": "0"},
        )
        live_rename = extra.pop("push_live_rename", lambda *_a, **_k: ([], []))
        return module.codex_pending_auto_titles(
            environ,
            codex_paths=lambda: (codex, codex / "state.db"),
            reconcile_pending_titles=lambda *_arguments, **_keywords: titled,
            derive_title=derive_title,
            load_config=extra.pop("load_config", dict),
            max_session_index_bytes=extra.pop("max_session_index_bytes", 1_000_000),
            push_live_rename=live_rename,
            **extra,
        )

    def indexed(self, codex: Path) -> dict[str, str]:
        index = codex / "session_index.jsonl"
        if not index.is_file():
            return {}
        found: dict[str, str] = {}
        for line in index.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            found[entry["id"]] = entry["thread_name"]
        return found

    def test_a_thread_older_than_a_week_still_gets_its_name(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".titler-old-", dir=REPO) as raw:
            home = Path(raw)
            old = time.time() - 30 * 86400
            codex = self.store(
                home, [(uuid_for(1), "", "fix the broken upload importer", old)]
            )
            result = self.run_pass(codex, home)
            self.assertEqual([uuid_for(1)], [item["uuid"] for item in result])
            self.assertIn(uuid_for(1), self.indexed(codex))

    def test_the_two_hundred_and_first_thread_gets_its_name(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".titler-many-", dir=REPO) as raw:
            home = Path(raw)
            now = time.time()
            threads = [
                (uuid_for(number), "", f"question number {number}", now - number)
                for number in range(1, 261)
            ]
            codex = self.store(home, threads)
            # A stopped clock, because this test is about the backlog and not
            # about the two-second budget: with the real clock, a loaded CI
            # runner spent the budget partway through and named 169 of 260.
            # The budget has its own tests, which pin the clock the same way.
            result = self.run_pass(
                codex, home, budget_seconds=1.0, monotonic=lambda: 0.0
            )
            self.assertEqual(260, len(result))
            self.assertIn(uuid_for(260), self.indexed(codex))

    def test_a_brand_new_thread_is_named_on_the_first_pass(self) -> None:
        """The first-minute promise: one refresh, and the name is there."""
        with tempfile.TemporaryDirectory(prefix=".titler-new-", dir=REPO) as raw:
            home = Path(raw)
            now = time.time()
            threads = [
                (uuid_for(number), "", f"older question {number}", now - 4000 - number)
                for number in range(1, 240)
            ]
            threads.append(
                (uuid_for(900), "", "name this session properly please", now - 5)
            )
            codex = self.store(home, threads)
            # A stopped clock: the promise under test is that the newest thread
            # is reached first among two hundred and forty, not that one machine
            # fits all of them inside two seconds. The budget has its own tests.
            self.run_pass(codex, home, budget_seconds=1.0, monotonic=lambda: 0.0)
            self.assertIn(uuid_for(900), self.indexed(codex))

    def test_a_pass_stops_at_its_budget_and_the_next_one_continues(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".titler-budget-", dir=REPO) as raw:
            home = Path(raw)
            now = time.time()
            threads = [
                (uuid_for(number), "", f"question number {number}", now - number)
                for number in range(1, 30)
            ]
            codex = self.store(home, threads)
            ticks = iter([0.0] + [0.0] * 30 + [99.0] * 200)
            first = self.run_pass(
                codex, home, budget_seconds=1.0, monotonic=lambda: next(ticks)
            )
            self.assertGreater(len(first), 0)
            self.assertLess(len(first), 29, "the budget has to end the pass")
            named = set(self.indexed(codex))
            second = self.run_pass(codex, home)
            # The second pass picks up exactly what the first one left.
            self.assertTrue({item["uuid"] for item in second})
            self.assertTrue(named.isdisjoint({item["uuid"] for item in second}))
            self.assertEqual(29, len(self.indexed(codex)))

    def test_the_deadline_is_passed_into_the_slowest_store_operation(self) -> None:
        """One blocking write cannot consume more than the whole pass budget."""
        with tempfile.TemporaryDirectory(prefix=".titler-deadline-", dir=REPO) as raw:
            home = Path(raw)
            codex = self.store(
                home, [(uuid_for(1), "", "fix the broken upload importer", time.time())]
            )
            received: list[float | None] = []

            # A write that ignores its deadline blocks for five seconds; one
            # that honours it returns in ten milliseconds. The gap is that wide
            # on purpose. It used to be 60ms against a 45ms ceiling, which asked
            # the machine to be fast rather than asking the code to be right --
            # a busy runner spent 45ms on the fixture alone and failed a
            # deadline that had been passed and honoured (2026-08-18,
            # ubuntu-24.04/3.11). The sleep only ever runs long when the
            # behaviour under test is actually broken.
            def slow_store(
                _root, _uuid, _title, *, timeout_seconds=None, **_keywords
            ):
                received.append(timeout_seconds)
                time.sleep(5.0 if timeout_seconds is None else timeout_seconds)
                return [], []

            started = time.monotonic()
            with mock.patch.object(
                names_push(), "_push_codex_thread_title", side_effect=slow_store
            ):
                self.run_pass(codex, home, budget_seconds=0.01)
            elapsed = time.monotonic() - started
            self.assertIsNotNone(received[0])
            self.assertLessEqual(received[0], 0.01)
            self.assertLess(elapsed, 2.0, f"10ms pass took {elapsed:.3f}s")

    def test_live_rename_receives_only_the_remaining_pass_budget(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".titler-live-budget-", dir=REPO) as raw:
            home = Path(raw)
            codex = self.store(
                home, [(uuid_for(1), "", "fix the broken upload importer", time.time())]
            )
            received: list[float | None] = []

            def slow_live(
                _state, _uuid, _title, *, timeout_seconds=None, **_keywords
            ):
                received.append(timeout_seconds)
                time.sleep(0.06 if timeout_seconds is None else timeout_seconds)
                return [], []

            self.run_pass(
                codex,
                home,
                budget_seconds=0.01,
                environ={"HOME": os.fspath(home)},
                push_live_rename=slow_live,
            )
            self.assertIsNotNone(received[0])
            self.assertLessEqual(received[0], 0.01)

    def test_each_socket_operation_uses_the_shared_remaining_deadline(self) -> None:
        class FakeSocket:
            def __init__(self, incoming: bytes = b"") -> None:
                self.incoming = incoming
                self.timeouts: list[float] = []

            def settimeout(self, value: float) -> None:
                self.timeouts.append(value)

            def sendall(self, _payload: bytes) -> None:
                return None

            def recv(self, count: int) -> bytes:
                chunk, self.incoming = self.incoming[:count], self.incoming[count:]
                return chunk

        deadline = time.monotonic() + 0.01
        sender = FakeSocket()
        names_push()._ws_send_frame(sender, b"hello", deadline=deadline)
        receiver = FakeSocket(b"\x81\x00")
        self.assertEqual(
            (1, b""),
            names_push()._ws_recv_frame(
                receiver,
                max_frame=names_push().MAX_CODEX_LIVE_RENAME_FRAME,
                deadline=deadline,
            ),
        )
        for timeout in sender.timeouts + receiver.timeouts:
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 0.01)

    def test_a_human_rename_that_wins_the_race_is_left_alone(self) -> None:
        """The claim is the decision, so a refusal writes nothing at all.

        `/rename` or `sp name` can land between the ownership read and the
        write. The claim refuses then -- and the old order claimed AFTER
        appending the index entry, so the person owned the name locally while
        every provider surface carried the automatic one.
        """
        with tempfile.TemporaryDirectory(prefix=".titler-race-", dir=REPO) as raw:
            home = Path(raw)
            codex = self.store(
                home, [(uuid_for(1), "", "fix the broken upload importer", time.time())]
            )
            result = self.run_pass(
                codex,
                home,
                claim_name=lambda *_a, **_k: "a human name already owns this session",
            )
            self.assertEqual([], result)
            self.assertEqual({}, self.indexed(codex))
            connection = sqlite3.connect(codex / "state.db")
            stored = connection.execute(
                "SELECT title FROM threads WHERE id = ?", (uuid_for(1),)
            ).fetchone()[0]
            connection.close()
            self.assertEqual("", stored, "the provider surface was overwritten")

    def test_the_titler_records_its_durable_index_push(self) -> None:
        """The fast-path evidence is written when the index append lands."""
        with tempfile.TemporaryDirectory(prefix=".titler-pushed-", dir=REPO) as raw:
            home = Path(raw)
            exact = uuid_for(1)
            codex = self.store(
                home, [(exact, "", "fix the broken upload importer", time.time())]
            )
            recorded: list[tuple[str, str, str]] = []

            result = self.run_pass(
                codex,
                home,
                record_pushed=lambda provider, uuid, title, **_keywords: recorded.append(
                    (provider, uuid, title)
                ),
            )

            self.assertEqual([exact], [item["uuid"] for item in result])
            self.assertEqual(
                [("codex", exact, "Fix The Broken Upload Importer")], recorded
            )
            self.assertEqual(recorded[0][2], self.indexed(codex)[exact])

    def test_a_human_rename_after_the_claim_wins_every_surface(self) -> None:
        """The ownership decision is re-read at the provider-write boundary."""
        with tempfile.TemporaryDirectory(prefix=".titler-postclaim-", dir=REPO) as raw:
            home = Path(raw)
            exact = uuid_for(1)
            human_title = "The operator's chosen title"
            codex = self.store(
                home, [(exact, "", "fix the broken upload importer", time.time())]
            )
            owner = ""

            def claim(*_arguments, **_keywords):
                nonlocal owner
                owner = "automatic"
                names_push()._append_codex_index_entry(
                    codex / "session_index.jsonl", exact, human_title
                )
                connection = sqlite3.connect(codex / "state.db")
                connection.execute(
                    "UPDATE threads SET title = ? WHERE id = ?", (human_title, exact)
                )
                connection.commit()
                connection.close()
                owner = "human"
                return ""

            result = self.run_pass(
                codex,
                home,
                claim_name=claim,
                name_owner=lambda *_a, **_k: owner,
            )
            self.assertEqual([], result)
            self.assertEqual(human_title, self.indexed(codex)[exact])
            connection = sqlite3.connect(codex / "state.db")
            stored = connection.execute(
                "SELECT title FROM threads WHERE id = ?", (exact,)
            ).fetchone()[0]
            connection.close()
            self.assertEqual(human_title, stored)

    def test_a_human_rename_before_sqlite_commit_rolls_back_the_auto_title(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".titler-db-race-", dir=REPO) as raw:
            home = Path(raw)
            exact = uuid_for(1)
            codex = self.store(
                home, [(exact, "", "fix the broken upload importer", time.time())]
            )
            database = codex / "state_5.sqlite"
            (codex / "state.db").rename(database)
            checks = 0

            def human_landed() -> bool:
                nonlocal checks
                checks += 1
                return False

            names_push()._push_codex_thread_title(
                codex,
                exact,
                "Automatic Title",
                timeout_seconds=1.0,
                still_automatic=human_landed,
            )
            connection = sqlite3.connect(database)
            stored = connection.execute(
                "SELECT title FROM threads WHERE id = ?", (exact,)
            ).fetchone()[0]
            connection.close()
            self.assertEqual(1, checks)
            self.assertEqual("", stored)

    def test_an_append_failure_releases_the_claim_and_the_next_pass_retries(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".titler-retry-", dir=REPO) as raw:
            home = Path(raw)
            exact = uuid_for(1)
            codex = self.store(
                home, [(exact, "", "fix the broken upload importer", time.time())]
            )
            owner = ""
            claims = 0
            releases = 0

            def claim(*_arguments, **_keywords):
                nonlocal owner, claims
                claims += 1
                if owner:
                    return "already owned"
                owner = "automatic"
                return ""

            def release(*_arguments, **_keywords):
                nonlocal owner, releases
                releases += 1
                if owner == "automatic":
                    owner = ""
                    return True
                return False

            real_append = names_push()._append_codex_index_entry
            attempts = 0

            def fail_once(*arguments):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("injected append failure")
                return real_append(*arguments)

            arguments = {
                "claim_name": claim,
                "release_claim": release,
                "name_owner": lambda *_a, **_k: owner,
            }
            with mock.patch.object(
                names_push(), "_append_codex_index_entry", side_effect=fail_once
            ):
                self.assertEqual([], self.run_pass(codex, home, **arguments))
                self.assertNotIn(exact, self.indexed(codex))
                second = self.run_pass(codex, home, **arguments)
            self.assertEqual([exact], [item["uuid"] for item in second])
            self.assertEqual((2, 2, 1), (attempts, claims, releases))
            self.assertEqual("automatic", owner)

    def test_existing_kit_rows_backfill_once_without_starving_the_tail(self) -> None:
        """Four hundred settled kit rows converge onto the fast path.

        Half predate pushed-title bookkeeping. Exact retained or derived title
        agreement backfills that history once; a second pass skips the whole
        settled head without adoption or per-row owner reads.
        """
        with tempfile.TemporaryDirectory(prefix=".titler-settled-", dir=REPO) as raw:
            home = Path(raw)
            now = time.time()
            threads = [
                (
                    uuid_for(number),
                    f"Settled {number}",
                    (
                        f"prompt {number}"
                        if number <= 300
                        else f"settled {number}"
                    ),
                    now - number,
                )
                for number in range(1, 401)
            ]
            threads.extend(
                (uuid_for(number), "", f"name tail {number}", now - 500 - number)
                for number in range(900, 940)
            )
            codex = self.store(home, threads)
            index = codex / "session_index.jsonl"
            index.write_text(
                "".join(
                    json.dumps({"id": uuid_for(number), "thread_name": f"Settled {number}"})
                    + "\n"
                    for number in range(1, 401)
                ),
                encoding="utf-8",
            )
            adoption_reads: list[str] = []
            owner_reads: list[str] = []
            pushed_writes: list[tuple[str, str, str]] = []
            ownership_reads = 0
            document = {
                "automatic_titles": {
                    f"codex:{uuid_for(number)}": f"Settled {number}"
                    for number in range(201, 301)
                },
                "pushed_titles": {
                    f"codex:{uuid_for(number)}": f"Settled {number}"
                    for number in range(1, 201)
                },
            }

            def human_named(_environ):
                nonlocal ownership_reads
                ownership_reads += 1
                return frozenset()

            def name_owner(_provider, uuid, **_keywords):
                owner_reads.append(uuid)
                # The first tail read sees no owner; the real claim between
                # reads then makes every write-boundary check automatic.
                return "automatic" if owner_reads.count(uuid) > 1 else ""

            def record_pushed(provider, uuid, title, **_keywords):
                pushed_writes.append((provider, uuid, title))
                document["pushed_titles"][f"{provider}:{uuid}"] = title

            # A stopped clock: the assertion below names all forty tail
            # threads exactly, so the pass has to finish its scan. Against the
            # real budget a slow runner truncates that list and the test reads
            # as a starved tail when nothing starved it. The budget has its own
            # tests, which pin the clock this way.
            result = self.run_pass(
                codex,
                home,
                budget_seconds=1.0,
                monotonic=lambda: 0.0,
                load_config=lambda: document,
                human_named=human_named,
                name_owner=name_owner,
                record_pushed=record_pushed,
                adopt_native=lambda _p, uuid, *_a, **_k: adoption_reads.append(uuid)
                or "",
            )
            self.assertEqual(
                [uuid_for(number) for number in range(900, 940)],
                [item["uuid"] for item in result],
            )
            self.assertEqual(1, ownership_reads)
            self.assertNotIn(uuid_for(1), adoption_reads)
            self.assertNotIn(uuid_for(1), owner_reads)
            self.assertNotIn(uuid_for(201), adoption_reads)
            self.assertNotIn(uuid_for(201), owner_reads)
            self.assertNotIn(uuid_for(900), adoption_reads)
            self.assertIn(uuid_for(900), owner_reads)
            historical_writes = [
                item for item in pushed_writes if 201 <= int(item[1][-12:]) <= 400
            ]
            self.assertEqual(
                [
                    ("codex", uuid_for(number), f"Settled {number}")
                    for number in range(201, 401)
                ],
                historical_writes,
            )

            writes_after_first_pass = len(pushed_writes)
            adoption_reads.clear()
            owner_reads.clear()
            second = self.run_pass(
                codex,
                home,
                budget_seconds=1.0,
                monotonic=lambda: 0.0,
                load_config=lambda: document,
                human_named=human_named,
                name_owner=name_owner,
                record_pushed=record_pushed,
                adopt_native=lambda _p, uuid, *_a, **_k: adoption_reads.append(uuid)
                or "",
            )
            self.assertEqual([], second)
            self.assertEqual(writes_after_first_pass, len(pushed_writes))
            self.assertFalse(
                any(1 <= int(uuid[-12:]) <= 400 for uuid in adoption_reads)
            )
            self.assertFalse(any(1 <= int(uuid[-12:]) <= 400 for uuid in owner_reads))

    def test_a_pass_with_no_budget_left_never_touches_the_store(self) -> None:
        """The query and its result set are inside the bound, not outside it.

        The old shape asked for every matching row and materialized the lot
        before the first deadline check. Here the store is unreadable: a pass
        that queried it would say so, and a pass that respects an expired
        budget says nothing because it never asked.
        """
        import contextlib
        import io

        with tempfile.TemporaryDirectory(prefix=".titler-nobudget-", dir=REPO) as raw:
            home = Path(raw)
            codex = self.store(home, [(uuid_for(1), "", "prompt", time.time())])
            (codex / "state.db").write_text("not a database", encoding="utf-8")
            ticks = iter([0.0] + [99.0] * 50)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = self.run_pass(
                    codex, home, budget_seconds=1.0, monotonic=lambda: next(ticks)
                )
            self.assertEqual([], result)
            self.assertEqual("", stderr.getvalue())

    def test_a_large_store_converges_over_a_few_passes(self) -> None:
        """Two thousand unnamed threads, and every one ends up named."""
        with tempfile.TemporaryDirectory(prefix=".titler-large-", dir=REPO) as raw:
            home = Path(raw)
            now = time.time()
            total = 2000
            codex = self.store(
                home,
                [
                    (uuid_for(number), "", f"question number {number}", now - number)
                    for number in range(1, total + 1)
                ],
            )
            passes = 0
            while passes < 12:
                passes += 1
                # A stopped clock, for the same reason the 260-thread test
                # pins one: this is about two thousand threads all reaching a
                # name, not about how many of them one machine fits inside a
                # two-second budget. Against the real clock a loaded runner
                # drained too little per pass and ran out of passes with the
                # backlog still standing -- a failure about the runner
                # (2026-08-18, ubuntu-24.04/3.11). The budget has its own
                # tests, which pin the clock exactly this way.
                named = self.run_pass(
                    codex, home, budget_seconds=1.0, monotonic=lambda: 0.0
                )
                if not named:
                    break
            self.assertEqual(total, len(self.indexed(codex)))
            self.assertLess(passes, 12, "the backlog never drained")

    def test_an_unreadable_thread_store_is_reported_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".titler-broken-", dir=REPO) as raw:
            home = Path(raw)
            codex = home / ".codex"
            codex.mkdir(parents=True, mode=0o700)
            (codex / "state.db").write_text("this is not a database", encoding="utf-8")
            import contextlib
            import io

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = self.run_pass(codex, home)
            self.assertEqual([], result)
            self.assertIn("Codex thread store unreadable", stderr.getvalue())

    def test_a_failure_is_left_where_a_person_will_meet_it(self) -> None:
        """stderr alone reached nobody: the caller sends both streams to /dev/null."""
        import contextlib
        import io

        with tempfile.TemporaryDirectory(prefix=".titler-record-", dir=REPO) as raw:
            home = Path(raw)
            state = home / ".local" / "state" / "session-kit"
            codex = self.store(home, [(uuid_for(1), "", "prompt", time.time())])
            (codex / "state.db").write_text("not a database", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_pass(codex, home)
            record = json.loads(
                (state / "codex-autotitle-error.json").read_text(encoding="utf-8")
            )
            self.assertIn("thread store unreadable", record["detail"])
            self.assertTrue(record["at"].endswith("Z"))

    def test_a_mid_scan_store_failure_keeps_its_doctor_record(self) -> None:
        """A page-two failure is not a clean pass that can clear its own alert."""
        with tempfile.TemporaryDirectory(prefix=".titler-midscan-", dir=REPO) as raw:
            home = Path(raw)
            now = time.time()
            codex = self.store(
                home,
                [
                    (uuid_for(number), "", f"question {number}", now - number)
                    for number in range(1, 202)
                ],
            )
            real_connect = sqlite3.connect
            calls = 0

            def fail_page_two(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 3:  # schema probe, page one, then page two
                    raise sqlite3.OperationalError("injected page-two failure")
                return real_connect(*args, **kwargs)

            # A stopped clock, or page two is never reached: the injected
            # failure fires on the third connect, and a runner that spends the
            # two-second budget inside page one never gets there -- so nothing
            # fails, no record is written, and the test dies reading a file that
            # was never created (2026-08-18, ubuntu-24.04/3.10). What is under
            # test is that a mid-scan failure keeps its record, not how much of
            # a two-hundred-thread store one machine covers per second.
            with mock.patch("sqlite3.connect", side_effect=fail_page_two):
                self.run_pass(codex, home, budget_seconds=1.0, monotonic=lambda: 0.0)
            record = json.loads(
                (
                    home / ".local" / "state" / "session-kit"
                    / "codex-autotitle-error.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn("injected page-two failure", record["detail"])

    def test_a_healthy_pass_retires_the_last_complaint(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".titler-clear-", dir=REPO) as raw:
            home = Path(raw)
            state = home / ".local" / "state" / "session-kit"
            state.mkdir(parents=True)
            stale = state / "codex-autotitle-error.json"
            stale.write_text(json.dumps({"detail": "old", "at": "x"}), encoding="utf-8")
            codex = self.store(home, [(uuid_for(1), "", "prompt", time.time())])
            self.run_pass(codex, home)
            self.assertFalse(stale.exists())

    def test_a_machine_without_codex_carries_no_stale_complaint(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".titler-nocodex-", dir=REPO) as raw:
            home = Path(raw)
            state = home / ".local" / "state" / "session-kit"
            state.mkdir(parents=True)
            stale = state / "codex-autotitle-error.json"
            stale.write_text(json.dumps({"detail": "old", "at": "x"}), encoding="utf-8")
            codex = home / ".codex"
            codex.mkdir(mode=0o700)
            self.run_pass(codex, home)
            self.assertFalse(stale.exists())

    def test_an_oversized_index_is_reported_not_swallowed(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory(prefix=".titler-big-", dir=REPO) as raw:
            home = Path(raw)
            codex = self.store(home, [(uuid_for(1), "", "prompt", time.time())])
            (codex / "session_index.jsonl").write_text("x" * 5000, encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = self.run_pass(codex, home, max_session_index_bytes=100)
            self.assertEqual([], result)
            self.assertIn("larger than the bounded size", stderr.getvalue())
            record = json.loads(
                (
                    home / ".local" / "state" / "session-kit" / "codex-autotitle-error.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn("larger than the bounded size", record["detail"])

    def test_the_budget_can_be_set_and_a_nonsense_value_is_ignored(self) -> None:
        module = names_push()
        self.assertEqual(2.0, module._autotitle_budget_seconds({}))
        self.assertEqual(
            5.5,
            module._autotitle_budget_seconds(
                {"SESSION_KIT_CODEX_AUTOTITLE_BUDGET_SECONDS": "5.5"}
            ),
        )
        for nonsense in ("0", "-3", "900", "soon"):
            self.assertEqual(
                2.0,
                module._autotitle_budget_seconds(
                    {"SESSION_KIT_CODEX_AUTOTITLE_BUDGET_SECONDS": nonsense}
                ),
            )


class ReconcileCopyTests(unittest.TestCase):
    """`sp color reconcile` names the providers it actually recolored."""

    def render(self, payload: dict) -> str:
        commands = (REPO / "lib" / "sh" / "sp_commands.sh").read_text(encoding="utf-8")
        body = commands.split("color_reconcile() {", 1)[1].split("\n}\n", 1)[0]
        program = body.split("python3 -c '", 1)[1].rsplit("'", 1)[0]
        return subprocess.run(
            [sys.executable, "-c", program],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def test_a_codex_only_run_never_talks_about_claude(self) -> None:
        output = self.render({"moved": {f"codex:{uuid_for(1)}": "lime"}})
        self.assertIn("Recolored 1 Codex session", output)
        self.assertIn("Codex windows show the new color from their next start.", output)
        self.assertNotIn("Claude", output)

    def test_a_mixed_run_names_both(self) -> None:
        output = self.render(
            {
                "moved": {
                    f"codex:{uuid_for(1)}": "lime",
                    f"claude:{uuid_for(2)}": "cyan",
                }
            }
        )
        self.assertIn(
            "Claude and Codex windows show the new color from their next start.",
            output,
        )

    def test_nothing_moved_says_nothing_changed(self) -> None:
        self.assertEqual("Nothing changed.\n", self.render({"moved": {}}))


if __name__ == "__main__":
    unittest.main()
