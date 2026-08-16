"""Who asked for a session, stamped at the moment it is created.

A machine starts sessions all day — drills, automations, watchdog restores —
and every one of them lands in the list a person opens to find their own work.
The picker's default view is the person's own screen (D17), and it can only be
that if creation says who asked: nothing infers origin later from a directory,
a title, or a guess.

An unstamped session belongs to the person. A tool that forgets to say it is a
machine therefore shows up in the human list and gets noticed, rather than
hiding by accident.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.support import REPO, run
from tests.test_commands import CommandFixture

sys.path.insert(0, os.fspath(REPO / "lib"))

from sessionkit_inventory import closed_sessions  # noqa: E402
from sessionkit_inventory import collector  # noqa: E402
from sessionkit_inventory import origins  # noqa: E402
from sessionkit_inventory.processes import ProcessTable  # noqa: E402

SP = REPO / "bin" / "sp"
CORE = REPO / "lib" / "session_inventory.py"


class OriginStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".origins-", dir=REPO)
        self.state = Path(self.temp.name)
        self.instance_environment = {
            "SESSION_KIT_ORIGIN_SHELL_PID": "20",
            "SESSION_KIT_ORIGIN_SHELL_START_TICKS": "200",
            "SESSION_KIT_ORIGIN_STARTED_AT_UNIX_MS": "2",
        }
        self.instance_patch = mock.patch.dict(
            os.environ, self.instance_environment
        )
        self.instance_patch.start()

    def tearDown(self) -> None:
        self.instance_patch.stop()
        self.temp.cleanup()

    def test_an_unstamped_session_belongs_to_the_person(self) -> None:
        self.assertEqual(
            origins.HUMAN,
            origins.origin_for("s1", origins.load_origins(self.state)),
        )

    def test_a_stamp_survives_a_reread_and_names_only_its_own_session(self) -> None:
        origins.record_origin(self.state, shpool_id="s1", origin=origins.MACHINE)
        stored = origins.load_origins(self.state)
        self.assertEqual(origins.MACHINE, origins.origin_for("s1", stored))
        self.assertEqual(origins.HUMAN, origins.origin_for("s2", stored))
        self.assertEqual(
            0o600, os.stat(origins.origins_path(self.state)).st_mode & 0o777
        )

    def test_the_writer_refuses_a_name_only_schema_2_stamp(self) -> None:
        with mock.patch.dict(os.environ):
            for variable in self.instance_environment:
                os.environ.pop(variable, None)
            with self.assertRaises(Exception):
                origins.record_origin(
                    self.state, shpool_id="unbound", origin=origins.MACHINE
                )
        self.assertFalse(origins.origins_path(self.state).exists())

    def test_the_writer_binds_a_machine_stamp_to_its_exact_instance(self) -> None:
        origins.record_origin(
            self.state, shpool_id="main2", origin=origins.MACHINE
        )
        stored = origins.load_origins(self.state)
        reused = {
            "shpool_id_raw": "main2",
            "started_at_unix_ms": 3,
            "shpool_shell": {"pid": 21, "process_start_ticks": 201},
            "machine_driven": False,
        }

        origins.apply_session_origins(
            {"sessions": [reused]}, state_dir=self.state
        )

        self.assertEqual(origins.HUMAN, reused["origin"])
        self.assertNotIn("origin_recorded", reused)
        self.assertEqual(
            {
                "shell_pid": 20,
                "shell_start_ticks": 200,
                "started_at_unix_ms": 2,
            },
            stored["sessions"]["main2"].get("instance"),
        )

    def test_the_writer_binds_a_human_stamp_to_its_exact_instance(self) -> None:
        origins.record_origin(self.state, shpool_id="main2", origin=origins.HUMAN)
        reused = {
            "shpool_id_raw": "main2",
            "started_at_unix_ms": 3,
            "shpool_shell": {"pid": 21, "process_start_ticks": 201},
            "machine_driven": True,
        }

        origins.apply_session_origins(
            {"sessions": [reused]}, state_dir=self.state
        )

        self.assertEqual(origins.MACHINE, reused["origin"])
        self.assertNotIn("origin_recorded", reused)

    def test_an_unknown_origin_is_refused_rather_than_stored(self) -> None:
        with self.assertRaises(Exception):
            origins.record_origin(self.state, shpool_id="s1", origin="robot")
        self.assertEqual({}, origins.load_origins(self.state)["sessions"])

    def stamp(self, shpool_id: str, origin: str, *, age_ms: int = 0) -> None:
        """Stamp a session, optionally as of some moment in the past."""
        origins.record_origin(
            self.state,
            shpool_id=shpool_id,
            origin=origin,
            now_unix_ms=int(time.time() * 1000) - age_ms,
        )

    def test_a_stamp_is_forgotten_once_its_session_is_gone(self) -> None:
        old = origins.ORIGIN_PRUNE_GRACE_MS * 2
        self.stamp("s1", origins.MACHINE, age_ms=old)
        self.stamp("s2", origins.MACHINE, age_ms=old)
        captured = origins.capture_origin_generations(self.state)
        self.assertEqual(
            1,
            origins.prune_origins(
                self.state,
                ["s1"],
                retire_generations=captured,
            ),
        )
        self.assertEqual(["s1"], sorted(origins.load_origins(self.state)["sessions"]))

    def test_a_stamp_survives_a_collection_that_landed_before_attach(self) -> None:
        """A stale manager reading cannot prune a newly captured stamp.

        Creation attaches, captures the exact shell generation, and then
        stamps it. A collector using a reading taken just before attach can
        reach pruning afterward under a different lock. The grace keeps that
        older reading from deleting the new instance-bound stamp.
        """
        self.stamp("about-to-attach", origins.HUMAN)
        captured = origins.capture_origin_generations(self.state)

        self.assertEqual(
            0,
            origins.prune_origins(
                self.state,
                ["some-other-session"],
                retire_generations=captured,
            ),
        )

        self.assertEqual(
            origins.HUMAN,
            origins.recorded_origin(
                "about-to-attach", origins.load_origins(self.state)
            ),
        )

    def test_a_newer_stamp_is_unreachable_to_a_stale_clock(self) -> None:
        self.stamp("already-seen", origins.MACHINE, age_ms=10_000)
        captured = origins.capture_origin_generations(self.state)
        self.stamp("written-after-capture", origins.HUMAN)

        moved_clock = int(time.time() * 1000) + origins.ORIGIN_PRUNE_GRACE_MS * 2
        self.assertEqual(
            1,
            origins.prune_origins(
                self.state,
                ["still-live"],
                now_unix_ms=moved_clock,
                retire_generations=captured,
            ),
        )
        self.assertEqual(
            origins.HUMAN,
            origins.recorded_origin(
                "written-after-capture", origins.load_origins(self.state)
            ),
        )

    def test_a_listing_with_no_sessions_never_empties_the_store(self) -> None:
        """One empty listing must not un-stamp the whole machine.

        A listing that names nothing is what a collection run against a
        stand-in session manager produces -- the state directory is shared,
        the session manager is not -- and what a daemon restart produces for a
        moment. Read as proof, it deletes every stamp on the box in one pass,
        and no live session is ever stamped again.

        Age is not a second opinion. The operator's own sessions run for days,
        so a rule that forgets stamps "too old to be anybody's live session"
        is a rule that deletes theirs -- it reads an age as evidence about a
        session when it is only a fact about the stamp. The old stamp below is
        a day and an hour old, which is an ordinary age for a session on this
        estate.
        """
        self.stamp("person", origins.HUMAN)
        self.stamp("agents", origins.MACHINE)
        self.stamp("person-since-yesterday", origins.HUMAN, age_ms=25 * 60 * 60 * 1000)

        self.assertEqual(0, origins.prune_origins(self.state, []))

        self.assertEqual(
            ["agents", "person", "person-since-yesterday"],
            sorted(origins.load_origins(self.state)["sessions"]),
        )

    def test_a_store_that_will_not_read_is_never_written_over(self) -> None:
        """A reading failure must not become permanent data loss.

        A prune rewrites the store wholesale. If a row in it did not read,
        the rewrite drops that row for good -- and the session behind it is
        live, so it loses its provenance and falls to the driver.
        """
        path = origins.origins_path(self.state)
        old = origins.ORIGIN_PRUNE_GRACE_MS * 2
        self.stamp("readable", origins.MACHINE, age_ms=old)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["sessions"]["unreadable"] = {"origin": "yes-please", "at_unix_ms": 1}
        path.write_text(json.dumps(document), encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        self.assertEqual(0, origins.prune_origins(self.state, []))

        self.assertEqual(before, path.read_text(encoding="utf-8"))

    def test_a_store_that_will_not_read_never_hands_a_row_to_the_driver(self) -> None:
        """"Never stamped" is a claim about the store, so it needs the store.

        An unreadable store answers "no stamp here" for every session on the
        machine, including the ones stamped as the person's. Read as silence,
        it licenses the driver -- so one unreadable file moves their own
        sessions behind the machine count, with nothing said anywhere.
        """
        self.stamp("person", origins.HUMAN)
        # The private-state reader refuses anything but a mode-0600 file, which
        # is what a stray chmod or a restore from a backup leaves behind.
        origins.origins_path(self.state).chmod(0o644)
        inventory = {
            "sessions": [
                {"shpool_id_raw": "person", "machine_driven": True},
                {"shpool_id_raw": "legacy", "machine_driven": True},
            ]
        }

        origins.apply_session_origins(inventory, state_dir=self.state)

        self.assertEqual(
            [origins.HUMAN, origins.HUMAN],
            [row["origin"] for row in inventory["sessions"]],
        )

    def test_a_row_carries_the_stamp_itself_beside_what_it_is(self) -> None:
        """Two fields, because the durable writers must read the stamp alone.

        `origin` is what the row IS -- for an unstamped session, whatever
        collection inferred a moment ago. Anything that writes provenance down
        for good (a repair that re-declares an origin, a close that fills the
        ledger) reads `origin_recorded`, which exists only where somebody
        actually said.
        """
        self.stamp("stamped", origins.MACHINE)
        inventory = {
            "sessions": [
                {
                    "shpool_id_raw": "stamped",
                    "started_at_unix_ms": 2,
                    "shpool_shell": {"pid": 20, "process_start_ticks": 200},
                },
                {"shpool_id_raw": "inferred", "machine_driven": True},
            ]
        }

        origins.apply_session_origins(inventory, state_dir=self.state)

        stamped, inferred = inventory["sessions"]
        self.assertEqual(origins.MACHINE, stamped["origin"])
        self.assertEqual(origins.MACHINE, stamped["origin_recorded"])
        self.assertEqual(origins.MACHINE, inferred["origin"])
        self.assertNotIn("origin_recorded", inferred)

    def test_a_legacy_machine_stamp_does_not_bind_a_reused_name(self) -> None:
        path = origins.origins_path(self.state)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sessions": {
                        "main2": {"origin": origins.MACHINE, "at_unix_ms": 1}
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        inventory = {
            "sessions": [
                {
                    "shpool_id_raw": "main2",
                    "started_at_unix_ms": 2,
                    "shpool_shell": {"pid": 20, "process_start_ticks": 200},
                }
            ]
        }

        origins.apply_session_origins(inventory, state_dir=self.state)

        self.assertEqual(origins.HUMAN, inventory["sessions"][0]["origin"])
        self.assertNotIn("origin_recorded", inventory["sessions"][0])

    def test_a_legacy_human_stamp_does_not_bind_a_reused_name(self) -> None:
        path = origins.origins_path(self.state)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sessions": {
                        "main2": {"origin": origins.HUMAN, "at_unix_ms": 1}
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        inventory = {
            "sessions": [
                {
                    "shpool_id_raw": "main2",
                    "machine_driven": True,
                    "started_at_unix_ms": 2,
                    "shpool_shell": {"pid": 20, "process_start_ticks": 200},
                }
            ]
        }

        origins.apply_session_origins(inventory, state_dir=self.state)

        self.assertEqual(origins.MACHINE, inventory["sessions"][0]["origin"])
        self.assertNotIn("origin_recorded", inventory["sessions"][0])

    def test_an_instance_bound_stamp_requires_the_exact_shell(self) -> None:
        path = origins.origins_path(self.state)
        path.write_text(
            json.dumps(
                {
                    "schema_version": origins.ORIGIN_SCHEMA_VERSION,
                    "sessions": {
                        "main2": {
                            "origin": origins.MACHINE,
                            "at_unix_ms": 1,
                            "record_generation": "a" * 64,
                            "instance": {
                                "shell_pid": 20,
                                "shell_start_ticks": 200,
                                "started_at_unix_ms": 2,
                            },
                        }
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        exact = {
            "shpool_id_raw": "main2",
            "started_at_unix_ms": 2,
            "shpool_shell": {"pid": 20, "process_start_ticks": 200},
        }
        reused = {
            "shpool_id_raw": "main2",
            "started_at_unix_ms": 3,
            "shpool_shell": {"pid": 21, "process_start_ticks": 201},
        }
        inventory = {"sessions": [exact, reused]}

        origins.apply_session_origins(inventory, state_dir=self.state)

        self.assertEqual(origins.MACHINE, exact["origin"])
        self.assertEqual(origins.MACHINE, exact["origin_recorded"])
        self.assertEqual(origins.HUMAN, reused["origin"])
        self.assertNotIn("origin_recorded", reused)

    def test_schema_2_name_only_stamps_classify_nothing_and_remain_prunable(
        self,
    ) -> None:
        path = origins.origins_path(self.state)
        path.write_text(
            json.dumps(
                {
                    "schema_version": origins.ORIGIN_SCHEMA_VERSION,
                    "sessions": {
                        "old-machine": {
                            "origin": origins.MACHINE,
                            "at_unix_ms": 1,
                            "record_generation": "a" * 64,
                        },
                        "old-human": {
                            "origin": origins.HUMAN,
                            "at_unix_ms": 1,
                            "record_generation": "b" * 64,
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        old_machine_name = {
            "shpool_id_raw": "old-machine",
            "started_at_unix_ms": 3,
            "shpool_shell": {"pid": 21, "process_start_ticks": 201},
        }
        old_human_name = {
            "shpool_id_raw": "old-human",
            "started_at_unix_ms": 4,
            "shpool_shell": {"pid": 22, "process_start_ticks": 202},
            "machine_driven": True,
        }

        inventory = {"sessions": [old_machine_name, old_human_name]}
        origins.apply_session_origins(inventory, state_dir=self.state)

        self.assertEqual(origins.HUMAN, old_machine_name["origin"])
        self.assertEqual(origins.MACHINE, old_human_name["origin"])
        self.assertNotIn("origin_recorded", old_machine_name)
        self.assertNotIn("origin_recorded", old_human_name)
        captured = origins.capture_origin_generations(self.state)
        self.assertEqual(
            2,
            origins.prune_origins(
                self.state,
                ["some-live-session"],
                now_unix_ms=origins.ORIGIN_PRUNE_GRACE_MS * 2,
                retire_generations=captured,
            ),
        )
        self.assertEqual({}, origins.load_origins(self.state)["sessions"])

    def test_a_bounce_ends_when_the_window_is_SEEN_not_when_a_shell_says_so(
        self,
    ) -> None:
        """The relaunching shell cannot report its own success.

        It blocks on the provider it just started, so any moment it picks to
        drop the marker is a moment before the replacement window exists --
        and in that interval the session is an App Server and a broker with no
        window, which is the machine shape. So the shell leaves the marker and
        collection ends the bounce when it SEES the window. A session whose
        replacement never appeared keeps its marker and stays visible.
        """
        markers = self.state / "provider-bounce"
        markers.mkdir(parents=True)
        # All three have been TAKEN by their session shell -- the state a
        # sighting is allowed to end. The shell renames rather than empties, so
        # the receipt is a second name.
        taken = {
            name: markers / f"{name}{origins.TAKEN_GENERATION_SEPARATOR}generation"
            for name in ("window-is-back", "still-restarting", "no-app-server")
        }
        for marker in taken.values():
            marker.write_text("", encoding="utf-8")
        captured = origins.capture_bounce_receipts(self.state)
        inventory = {
            "sessions": [
                {"shpool_id_raw": "window-is-back", "app_server_window": True},
                {"shpool_id_raw": "still-restarting", "machine_driven": True},
                {"shpool_id_raw": "no-app-server"},
            ]
        }

        origins.apply_session_origins(
            inventory,
            state_dir=self.state,
            settle_bounce_receipts=captured,
        )

        self.assertFalse(taken["window-is-back"].exists())
        self.assertTrue(taken["still-restarting"].exists())
        self.assertTrue(taken["no-app-server"].exists())
        # And the session still mid-restart is still the person's.
        self.assertEqual(
            [origins.HUMAN, origins.HUMAN, origins.HUMAN],
            [row["origin"] for row in inventory["sessions"]],
        )

    def test_a_bounce_nobody_has_performed_yet_is_never_cleared(self) -> None:
        """A pending instruction beside a visible window is the OLD window.

        The picker writes the conversation ID, re-proves the session on a fresh
        collection, and only then asks the window to exit -- so every bounce
        spends the whole interval between request and exit as a live window
        sitting next to an instruction nobody has carried out. Deleting it there
        does not end a bounce, it cancels one, and the cost is not a hidden row:
        the session shell reaches the marker, finds no instruction, takes the
        ordinary provider exit instead of relaunching, and the session the
        person was sitting in closes.

        The shell's receipt is the second NAME, and only that name is deleted --
        so the two sides never touch the same path, and no read here can race a
        picker's write there.
        """
        markers = self.state / "provider-bounce"
        markers.mkdir(parents=True)
        requested = markers / "bounce-requested"
        taken = markers / (
            f"bounce-taken{origins.TAKEN_GENERATION_SEPARATOR}generation"
        )
        requested.write_text("11111111-2222-4333-8444-555555555555\n", encoding="utf-8")
        taken.write_text("", encoding="utf-8")
        inventory = {
            "sessions": [
                {"shpool_id_raw": "bounce-requested", "app_server_window": True},
                {"shpool_id_raw": "bounce-taken", "app_server_window": True},
            ]
        }

        origins.apply_session_origins(
            inventory,
            state_dir=self.state,
            settle_bounce_receipts=origins.capture_bounce_receipts(self.state),
        )

        self.assertTrue(
            requested.is_file(),
            "a bounce nobody performed was cancelled, which closes the session",
        )
        self.assertEqual(
            "11111111-2222-4333-8444-555555555555",
            requested.read_text(encoding="utf-8").strip(),
        )
        # The taken one still goes: that is the whole point of the sighting.
        self.assertFalse(taken.exists())
        # And a session with a pending instruction still reads as bouncing, so
        # the row it belongs to is never folded while it waits.
        self.assertTrue(origins._provider_is_bouncing(self.state, "bounce-requested"))

    def test_a_taken_marker_belongs_to_its_session_and_is_not_swept(self) -> None:
        """The receipt is the same session's state under a second name.

        Swept as a stranger, a bounce in flight under a LIVE session would be
        disarmed the moment it passed the grace -- and a disarmed bounce is a
        person's window in the machine list for as long as its replacement
        takes. Once the session really is gone, both names go.
        """
        markers = self.state / "provider-bounce"
        markers.mkdir(parents=True)
        live = markers / (
            f"still-here{origins.TAKEN_GENERATION_SEPARATOR}generation"
        )
        dead = markers / f"gone{origins.TAKEN_GENERATION_SEPARATOR}generation"
        live_reservation = markers / (
            f"{origins.BOUNCE_GENERATION_RESERVATION_PREFIX}still-here"
            f"{origins.TAKEN_GENERATION_SEPARATOR}generation"
        )
        dead_reservation = markers / (
            f"{origins.BOUNCE_GENERATION_RESERVATION_PREFIX}gone"
            f"{origins.TAKEN_GENERATION_SEPARATOR}generation"
        )
        for marker in (live, dead, live_reservation, dead_reservation):
            marker.write_text("", encoding="utf-8")
        old = time.time() - (origins.ORIGIN_PRUNE_GRACE_MS / 1000) * 2
        for marker in (live, dead, live_reservation, dead_reservation):
            os.utime(marker, (old, old))

        captured = origins.capture_bounce_cleanup_generations(self.state)
        removed = origins.prune_bounce_markers(
            self.state,
            ["still-here"],
            retire_generations=captured,
        )

        self.assertEqual(2, removed)
        self.assertTrue(live.is_file(), "a bounce in flight was disarmed")
        self.assertTrue(live_reservation.is_file())
        self.assertFalse(dead.exists())
        self.assertFalse(dead_reservation.exists())

    def test_a_read_only_collection_deletes_no_marker(self) -> None:
        """`--no-write` promised not to create state, so it may not delete it.

        Every `sp` guard snapshot comes through that branch, unlocked and
        concurrent with the picker that is writing bounce instructions. Rows are
        still classified there; only the deleting is left to a collection that
        is allowed to write.
        """
        markers = self.state / "provider-bounce"
        markers.mkdir(parents=True)
        taken = markers / f"settled{origins.TAKEN_GENERATION_SEPARATOR}generation"
        taken.write_text("", encoding="utf-8")
        inventory = {
            "sessions": [{"shpool_id_raw": "settled", "app_server_window": True}]
        }

        origins.apply_session_origins(
            inventory, state_dir=self.state, clear_settled_bounces=False
        )

        self.assertTrue(taken.is_file())
        self.assertEqual(origins.HUMAN, inventory["sessions"][0]["origin"])

    def test_a_stale_sighting_cannot_retire_a_newer_receipt(self) -> None:
        """The review ordering, including its complete restart-gap reading."""
        markers = self.state / "provider-bounce"
        markers.mkdir(parents=True)
        pending = markers / "person-session"
        newer = markers / (
            f"person-session{origins.TAKEN_GENERATION_SEPARATOR}newgeneration"
        )

        # Nothing was receipted when this collector began its process reading.
        stale_generations = origins.capture_bounce_receipts(self.state)
        stale_inventory = {
            "sessions": [
                {"shpool_id_raw": "person-session", "app_server_window": True}
            ]
        }

        # The picker requests the next bounce and the provider takes it while
        # that older process reading is still in flight.
        pending.write_text(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\n",
            encoding="utf-8",
        )
        os.replace(pending, newer)
        origins.apply_session_origins(
            stale_inventory,
            state_dir=self.state,
            settle_bounce_receipts=stale_generations,
        )
        self.assertTrue(newer.is_file(), "stale evidence retired a newer receipt")

        socket = "/tmp/session-kit-stale-generation.sock"
        table = ProcessTable(
            {
                2001: {
                    "pid": 2001,
                    "ppid": 1001,
                    "cmdline": [
                        "codex",
                        "app-server",
                        "--listen",
                        f"unix://{socket}",
                    ],
                },
                2100: {
                    "pid": 2100,
                    "ppid": 1001,
                    "cmdline": ["python3", "provider_broker.py", "--socket", socket],
                },
            }
        )
        verdict = collector._codex_app_server_driver(
            2001, table[2001]["cmdline"], table, table, 120
        )
        self.assertEqual("program", verdict, "the gap control was widened away")
        gap = {
            "sessions": [
                {
                    "shpool_id_raw": "person-session",
                    "machine_driven": verdict == "program",
                }
            ]
        }
        origins.apply_session_origins(gap, state_dir=self.state)
        self.assertEqual(origins.HUMAN, gap["sessions"][0]["origin"])

        # A fresh positive sighting captures this exact generation and settles
        # it. This is generation binding, not a blanket refusal to settle.
        fresh_generations = origins.capture_bounce_receipts(self.state)
        origins.apply_session_origins(
            {
                "sessions": [
                    {"shpool_id_raw": "person-session", "app_server_window": True}
                ]
            },
            state_dir=self.state,
            settle_bounce_receipts=fresh_generations,
        )
        self.assertFalse(newer.exists())
        origins.apply_session_origins(gap, state_dir=self.state)
        self.assertEqual(origins.MACHINE, gap["sessions"][0]["origin"])

    def test_a_captured_generation_cannot_reach_its_replacement(self) -> None:
        markers = self.state / "provider-bounce"
        markers.mkdir(parents=True)
        old = markers / f"same{origins.TAKEN_GENERATION_SEPARATOR}oldgeneration"
        new = markers / f"same{origins.TAKEN_GENERATION_SEPARATOR}newgeneration"
        old.write_text("", encoding="utf-8")
        captured = origins.capture_bounce_receipts(self.state)
        old.unlink()
        new.write_text("", encoding="utf-8")

        origins.apply_session_origins(
            {"sessions": [{"shpool_id_raw": "same", "app_server_window": True}]},
            state_dir=self.state,
            settle_bounce_receipts=captured,
        )

        self.assertTrue(new.is_file())

    def test_an_already_live_shells_reusable_receipt_is_never_settled(self) -> None:
        markers = self.state / "provider-bounce"
        markers.mkdir(parents=True)
        legacy = markers / f"legacy{origins.TAKEN_SUFFIX}"
        legacy.write_text("", encoding="utf-8")

        origins.apply_session_origins(
            {
                "sessions": [
                    {"shpool_id_raw": "legacy", "app_server_window": True}
                ]
            },
            state_dir=self.state,
            settle_bounce_receipts=origins.capture_bounce_receipts(self.state),
        )

        self.assertTrue(legacy.is_file())

    def test_a_bounce_marker_outliving_its_session_is_swept(self) -> None:
        """The marker is removed by the shell that relaunches the provider.

        One still there when its session is gone means that shell never got
        there -- killed, or the box went down mid-bounce -- and nothing will
        ever consume it; left behind, it suppresses classification for that id
        forever. A marker for a LIVE session is never touched: taking one away
        mid-bounce is what puts a person's window in the machine list while it
        restarts.
        """
        markers = self.state / "provider-bounce"
        markers.mkdir(parents=True)
        stale = markers / "gone"
        live = markers / "still-here"
        fresh = markers / "just-armed"
        for index, marker in enumerate((stale, live, fresh), start=1):
            marker.write_text(
                f"uuid\npadding\ngeneration{index}\n",
                encoding="utf-8",
            )
        old = time.time() - (origins.ORIGIN_PRUNE_GRACE_MS / 1000) * 2
        os.utime(stale, (old, old))
        os.utime(live, (old, old))

        captured = origins.capture_bounce_cleanup_generations(self.state)
        self.assertEqual(
            0,
            origins.prune_bounce_markers(
                self.state,
                [],
                retire_generations=captured,
            ),
        )
        self.assertEqual(
            1,
            origins.prune_bounce_markers(
                self.state,
                ["still-here"],
                retire_generations=captured,
            ),
        )

        self.assertFalse(stale.exists())
        self.assertTrue(live.exists())
        self.assertTrue(fresh.exists())

    def test_session_gone_sweep_cannot_reach_a_newer_bounce_request(self) -> None:
        markers = self.state / "provider-bounce"
        markers.mkdir(parents=True)
        request = markers / "gone"
        request.write_text("uuid\npadding\noldgeneration\n", encoding="utf-8")
        captured = origins.capture_bounce_cleanup_generations(self.state)
        request.write_text("uuid\npadding\nnewgeneration\n", encoding="utf-8")
        old = time.time() - (origins.ORIGIN_PRUNE_GRACE_MS / 1000) * 2
        os.utime(request, (old, old))

        self.assertEqual(
            0,
            origins.prune_bounce_markers(
                self.state,
                ["still-here"],
                now_unix_ms=int(time.time() * 1000),
                retire_generations=captured,
            ),
        )
        self.assertIn("newgeneration", request.read_text(encoding="utf-8"))

    def test_every_row_carries_an_origin_before_anything_filters(self) -> None:
        origins.record_origin(self.state, shpool_id="s2", origin=origins.MACHINE)
        inventory = {
            "sessions": [
                {"shpool_id_raw": "s1"},
                {
                    "shpool_id_raw": "s2",
                    "started_at_unix_ms": 2,
                    "shpool_shell": {"pid": 20, "process_start_ticks": 200},
                },
                {"shpool_id_raw": None},
            ]
        }
        origins.apply_session_origins(inventory, state_dir=self.state)
        self.assertEqual(
            [origins.HUMAN, origins.MACHINE, origins.HUMAN],
            [row["origin"] for row in inventory["sessions"]],
        )

    def test_a_stamp_outranks_the_driver_verdict_in_both_directions(self) -> None:
        """Provenance is the answer wherever it exists.

        The driver verdict reads the present -- who holds a socket right now.
        The stamp records who CREATED the session, which is what the operator asked
        about. So a session they created stays theirs even on a refresh where no
        window happens to hold it (a provider bounce is exactly that), and a
        session an agent created folds even while a window is attached.

        The third row is a control, and it is what makes this test able to
        fail: without it, "person" reads human whether the stamp outranked a
        driver verdict or no driver verdict was ever consulted. The control is
        the same verdict on an unstamped row, so a run where the driver did
        nothing at all fails here instead of passing quietly.
        """
        origins.record_origin(self.state, shpool_id="person", origin=origins.HUMAN)
        origins.record_origin(self.state, shpool_id="agents", origin=origins.MACHINE)
        inventory = {
            "sessions": [
                {
                    "shpool_id_raw": "person",
                    "machine_driven": True,
                    "started_at_unix_ms": 2,
                    "shpool_shell": {"pid": 20, "process_start_ticks": 200},
                },
                {
                    "shpool_id_raw": "agents",
                    "started_at_unix_ms": 2,
                    "shpool_shell": {"pid": 20, "process_start_ticks": 200},
                },
                {"shpool_id_raw": "unstamped", "machine_driven": True},
            ]
        }

        origins.apply_session_origins(inventory, state_dir=self.state)

        self.assertEqual(
            [origins.HUMAN, origins.MACHINE, origins.MACHINE],
            [row["origin"] for row in inventory["sessions"]],
        )

    def test_only_a_never_stamped_session_is_decided_by_the_driver(self) -> None:
        """The legacy case the second signal exists for."""
        inventory = {
            "sessions": [
                {"shpool_id_raw": "legacy-worker", "machine_driven": True},
                {"shpool_id_raw": "legacy-window"},
            ]
        }

        origins.apply_session_origins(inventory, state_dir=self.state)

        self.assertEqual(
            [origins.MACHINE, origins.HUMAN],
            [row["origin"] for row in inventory["sessions"]],
        )

    def test_a_bouncing_provider_never_folds_an_unstamped_row(self) -> None:
        """The kit removes the window itself when it repaints a title.

        During that relaunch an unstamped session a person is sitting in has
        a server and a broker and no window -- the worker shape exactly. The
        kit knows it asked for the restart, so it does not read the verdict.
        """
        bounces = self.state / "provider-bounce"
        bounces.mkdir(parents=True)
        (bounces / "bouncing").write_text("uuid\n", encoding="utf-8")
        inventory = {
            "sessions": [
                {"shpool_id_raw": "bouncing", "machine_driven": True},
                {"shpool_id_raw": "not-bouncing", "machine_driven": True},
            ]
        }

        origins.apply_session_origins(inventory, state_dir=self.state)

        self.assertEqual(
            [origins.HUMAN, origins.MACHINE],
            [row["origin"] for row in inventory["sessions"]],
        )

    def test_the_core_records_and_lists_one_origin(self) -> None:
        env = {
            **os.environ,
            "SESSION_KIT_STATE_DIR": str(self.state),
            "HOME": str(self.state),
        }
        recorded = subprocess.run(
            [sys.executable, str(CORE), "origin", "record", "s9", "machine"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, recorded.returncode, recorded.stderr)
        self.assertEqual("machine", json.loads(recorded.stdout)["origin"])
        listed = subprocess.run(
            [sys.executable, str(CORE), "origin", "list"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertEqual(
            "machine", json.loads(listed.stdout)["sessions"]["s9"]["origin"]
        )
        self.assertEqual(
            {
                "shell_pid": 20,
                "shell_start_ticks": 200,
                "started_at_unix_ms": 2,
            },
            json.loads(listed.stdout)["sessions"]["s9"]["instance"],
        )
        refused = subprocess.run(
            [sys.executable, str(CORE), "origin", "record", "s9", "robot"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, refused.returncode)


class OriginAtCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CommandFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def stamps(self) -> list[list[str]]:
        if not self.fixture.origin_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.fixture.origin_log.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def new_session(self, *argv: str, env: dict[str, str] | None = None) -> None:
        merged = {
            **self.fixture.env(),
            "STUB_DYNAMIC_PROVIDER": "shell",
            "STUB_DYNAMIC_CWD": str(self.fixture.project),
            **(env or {}),
        }
        run([SP, "new", "shell", "fixture", *argv], env=merged)

    def test_a_new_session_is_stamped_as_a_persons_by_default(self) -> None:
        self.new_session()
        self.assertEqual([["human"]], [stamp[1:] for stamp in self.stamps()])

    def test_the_flag_stamps_a_machine_session(self) -> None:
        self.new_session("--origin", "machine")
        self.assertEqual([["machine"]], [stamp[1:] for stamp in self.stamps()])

    def test_creation_stamps_the_confirmed_shell_instance(self) -> None:
        self.new_session("--origin", "machine")
        created = json.loads(self.fixture.shpool_state.read_text(encoding="utf-8"))[
            "sessions"
        ][0]
        stamped = json.loads(
            self.fixture.origin_instance_log.read_text(encoding="utf-8")
        )

        self.assertEqual(
            {
                "shell_pid": "1001",
                "shell_start_ticks": "10001",
                "started_at_unix_ms": str(created["started_at_unix_ms"]),
            },
            stamped,
        )

    def test_the_environment_stamps_everything_a_run_starts(self) -> None:
        self.new_session(env={"SESSION_KIT_ORIGIN": "machine"})
        self.assertEqual([["machine"]], [stamp[1:] for stamp in self.stamps()])

    def test_the_flag_outranks_the_environment(self) -> None:
        self.new_session("--origin", "human", env={"SESSION_KIT_ORIGIN": "machine"})
        self.assertEqual([["human"]], [stamp[1:] for stamp in self.stamps()])

    def test_an_unknown_environment_value_is_a_persons_session(self) -> None:
        self.new_session(env={"SESSION_KIT_ORIGIN": "robot"})
        self.assertEqual([["human"]], [stamp[1:] for stamp in self.stamps()])

    def test_a_session_that_creates_a_session_stamps_a_machine(self) -> None:
        """The operator's rule: only sessions THEY created belong in their list.

        Sessions 87 and 88 were their examples -- an agent started them, and
        both came back as their own rows because the kit asked the caller to
        volunteer that it was a machine and no agent ever does. Every managed
        session exports SHPOOL_SESSION_NAME into what it runs; the login shell
        the picker runs in does not. So the caller is readable without its
        cooperation, whatever provider or verb it uses.
        """
        self.new_session(env={"SHPOOL_SESSION_NAME": "s20200102-050607-2000000"})
        self.assertEqual([["machine"]], [stamp[1:] for stamp in self.stamps()])

    def test_the_pickers_own_creations_are_still_the_persons(self) -> None:
        """The other half of the same rule, and the one that must never break.

        The picker runs in the login shell, which exports no session name.
        If this ever reads machine, every session the operator starts leaves their list.
        """
        for label, env in (
            ("no session name at all", {}),
            ("an empty session name", {"SHPOOL_SESSION_NAME": ""}),
            ("a name that is not a session ID", {"SHPOOL_SESSION_NAME": "workbench"}),
        ):
            with self.subTest(label=label):
                self.fixture.close()
                self.fixture = CommandFixture()
                self.new_session(env=env)
                self.assertEqual(
                    [["human"]], [stamp[1:] for stamp in self.stamps()]
                )

    def test_a_declaration_still_outranks_the_caller(self) -> None:
        """An operator override, and the repair paths' carried origin."""
        inside = {"SHPOOL_SESSION_NAME": "s20200102-050607-2000000"}
        self.new_session("--origin", "human", env=inside)
        self.assertEqual([["human"]], [stamp[1:] for stamp in self.stamps()])

        self.fixture.close()
        self.fixture = CommandFixture()
        self.new_session(env={**inside, "SESSION_KIT_ORIGIN": "human"})
        self.assertEqual([["human"]], [stamp[1:] for stamp in self.stamps()])

    def restore(self, provider: str, uuid: str, env: dict[str, str] | None = None):
        merged = {
            **self.fixture.env(),
            "SESSION_KIT_BACKGROUND": "1",
            "SESSION_KIT_PROVIDER_PROOF_ATTEMPTS": "1",
            "STUB_DYNAMIC_PROVIDER": provider,
            "STUB_DYNAMIC_UUID": uuid,
            "STUB_DYNAMIC_CWD": str(self.fixture.project),
            **(env or {}),
        }
        return run(
            [SP, "restore-exact", provider, uuid, str(self.fixture.project)],
            env=merged,
            check=False,
        )

    def record_close(self, provider: str, uuid: str, origin: str) -> None:
        closed_sessions.record_close(
            provider=provider,
            uuid=uuid,
            cwd=str(self.fixture.project),
            origin=origin,
            environ=self.fixture.env(),
        )

    def test_restore_brings_a_machine_session_back_as_a_machine(self) -> None:
        """The recovery sweep recreated 87-90, and lost what they were.

        It runs from the login shell, where nothing declares a machine, so
        every restored agent session came back as one of the operator's rows. A restore
        resumes a session that already had an origin, so the record of what it
        was outranks any reading of who is running the restore.
        """
        uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.record_close("codex", uuid, "machine")
        self.restore("codex", uuid)
        self.assertEqual("machine", self.stamps()[-1][1])

    def test_restore_keeps_a_persons_session_the_persons(self) -> None:
        """The safe direction, at the same door: only a recorded machine
        moves a restored row out of the list the operator reads."""
        for label, recorded in (
            ("closed as a person's", "human"),
            ("no closed record at all", None),
        ):
            with self.subTest(label=label):
                self.fixture.close()
                self.fixture = CommandFixture()
                uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                if recorded is not None:
                    self.record_close("codex", uuid, recorded)
                self.restore("codex", uuid)
                self.assertEqual("human", self.stamps()[-1][1])

    def test_a_declared_origin_still_outranks_the_closed_record(self) -> None:
        """The picker's repair paths declare one, and it must keep winning."""
        uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.record_close("codex", uuid, "machine")
        self.restore("codex", uuid, env={"SESSION_KIT_ORIGIN": "human"})
        self.assertEqual("human", self.stamps()[-1][1])

    def test_the_record_wins_in_both_directions_wherever_the_restore_runs(
        self,
    ) -> None:
        """A restore is not a creation, so the caller is not the question.

        The record says what this conversation WAS. Reading only its machine
        half left the other direction to the caller -- and the caller reading
        answers "machine" for anything running inside a managed session. So a
        conversation recorded as the operator's, restored by a sweep or by them
        typing `sp restore-exact` in one of their own windows, came back a
        machine's and left the list they were looking at. Both cells of the same
        table, run from inside a session, are asserted here.
        """
        inside = {"SHPOOL_SESSION_NAME": "s20200102-050607-2000000"}
        for label, recorded, expected in (
            ("recorded as the person's", "human", "human"),
            ("recorded as an agent's", "machine", "machine"),
        ):
            with self.subTest(label=label):
                self.fixture.close()
                self.fixture = CommandFixture()
                uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                self.record_close("codex", uuid, recorded)
                self.restore("codex", uuid, env=inside)
                self.assertEqual(expected, self.stamps()[-1][1])

    def test_an_unrecorded_conversation_is_the_persons_wherever_it_is_restored(
        self,
    ) -> None:
        """A restore creates nothing, so the caller is not its creator.

        The question "who started this?" has an answer for a NEW session and
        the caller's context answers it. For a restore the conversation
        already had an owner, and if no record survives, nobody knows who --
        which is unknown, which is the person's. Reading the caller there
        answered a question nobody asked and froze the answer as a permanent
        stamp, so a repair, a model change, or the verb typed in one of their
        own windows took a session out of their list for good. An agent that
        wants its restore marked still says so.
        """
        for label, declared, expected in (
            ("nothing declared", None, "human"),
            ("an agent declaring itself", "machine", "machine"),
        ):
            with self.subTest(label=label):
                self.fixture.close()
                self.fixture = CommandFixture()
                env = {"SHPOOL_SESSION_NAME": "s20200102-050607-2000000"}
                if declared:
                    env["SESSION_KIT_ORIGIN"] = declared
                self.restore("codex", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", env=env)
                self.assertEqual(expected, self.stamps()[-1][1])

    def test_a_ledger_that_will_not_parse_never_hides_the_persons_session(
        self,
    ) -> None:
        """Opening a file is not reading it.

        A ledger truncated mid-write, or with one line this module would skip,
        opens perfectly and yields no rows. Read as "no record", that silence
        was answered from the caller's context, so one interrupted write took
        a recorded conversation of theirs out of their list on the way back in --
        exit 0, no warning. A ledger that will not parse is unproven state,
        and unproven state leaves the row visible.
        """
        uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.record_close("codex", uuid, "human")
        ledger = closed_sessions.ledger_path(self.fixture.env())
        ledger.write_text(
            '{"provider": "codex", "uuid": "%s", "closed_at_unix' % uuid,
            encoding="utf-8",
        )

        self.assertFalse(closed_sessions.ledger_is_readable(self.fixture.env()))

        self.restore(
            "codex", uuid, env={"SHPOOL_SESSION_NAME": "s20200102-050607-2000000"}
        )
        self.assertEqual("human", self.stamps()[-1][1])

    def test_a_ledger_that_will_not_read_never_hides_the_persons_session(
        self,
    ) -> None:
        """An unreadable file is not a record, and not the absence of one.

        The ledger is the thing that says a conversation was an agent's. Read
        as "no record", its failure hands the answer to the caller -- and from
        inside a session the caller reads machine, so an unreadable file takes
        one of their conversations out of their list on the way back in.
        """
        uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.record_close("codex", uuid, "human")
        ledger = closed_sessions.ledger_path(self.fixture.env())
        ledger.chmod(0o000)
        try:
            self.restore(
                "codex",
                uuid,
                env={"SHPOOL_SESSION_NAME": "s20200102-050607-2000000"},
            )
        finally:
            ledger.chmod(0o600)

        self.assertEqual("human", self.stamps()[-1][1])

    def test_an_unknown_origin_refuses_the_creation(self) -> None:
        refused = run(
            [SP, "new", "shell", "fixture", "--origin", "robot"],
            env=self.fixture.env(),
            check=False,
        )
        self.assertEqual(2, refused.returncode)
        self.assertEqual(
            "session-kit: --origin requires human or machine\n", refused.stderr
        )
        self.assertEqual([], self.stamps())

    def test_the_stamp_names_the_session_that_was_attached(self) -> None:
        """The captured manager name and the stamped manager name agree."""
        self.new_session("--origin", "machine")
        log = self.fixture.shpool_log.read_text(encoding="utf-8")
        self.assertTrue(log.startswith("attach "), log)
        stamped = self.stamps()
        self.assertEqual(1, len(stamped))
        self.assertEqual(log.split()[1], stamped[0][0])


if __name__ == "__main__":
    unittest.main()
