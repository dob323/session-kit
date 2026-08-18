"""A second session manager on the box must not blind the kit to its own.

On 15 August a review lane daemonised a sandboxed `shpool` of its own, holding
no sessions. The kit counted session managers by process name, found two, and
from that second on could identify none of the operator's ten live sessions:
every row read "Unresolved provider session", the home screen said
"Sessions: none", and a session they created came up as a bare shell because its
record could not be completed. Nothing was lost and nothing was closed -- the kit
refused rather than guessed -- but a stranger's process should never have been
able to cause it.

Two answers to that were wrong, and reviewers proved both:

* Resolving a name among the children of ANY daemon let a foreign daemon's child
  take over one of their rows whenever their own shell's environment would not read.
  The row then carried a foreign conversation's identity, passed strict
  validation, and a close from it would have signalled the wrong process tree.
* Inferring each daemon's socket from its command line and environment is not
  authoritative. The pinned shpool ignores `SHPOOL_SOCKET` entirely, and under
  systemd socket activation it disregards `--socket` and serves the listener it
  inherited, so either string can say the opposite of the truth and the real
  daemon can be excluded in favour of a stranger.

The kernel is asked instead: whichever daemon holds open the inode of the socket
this kit's client connects to is the one answering us.
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

from sessionkit_inventory import processes as process_inventory  # noqa: E402
from sessionkit_inventory.render import render_picker_page  # noqa: E402
import session_inventory as inventory_core  # noqa: E402

KIT_DAEMON = 10
FOREIGN_DAEMON = 20
INODE = 11065


def daemon(pid: int, *, argv: list[str] | None = None) -> dict:
    return {
        "pid": pid,
        "ppid": 1,
        "comm": "shpool",
        "cmdline": argv or ["/home/dev/.cargo/bin/shpool", "daemon"],
        "start_ticks": pid * 10,
        "session_name": "",
    }


def shell(pid: int, ppid: int, name: str, *, unreadable: bool = False) -> dict:
    return {
        "pid": pid,
        "ppid": ppid,
        "comm": "bash",
        "cmdline": ["-bash"],
        "start_ticks": pid * 10,
        "session_name": name,
        "environ_unreadable": unreadable,
    }


LISTENING = "00010000"
CONNECTED = "00000000"


@contextmanager
def kernel_says(
    holders,
    *,
    listed: bool = True,
    extra_rows=(),
    also_holds=(),
    unreadable=(),
    unreadable_links=(),
    raw_tail="",
):
    """A fixture /proc where `holders` hold the socket this kit connects to.

    `extra_rows` adds further `/proc/net/unix` lines for the SAME path, which is
    a real state: a daemon whose socket file was replaced keeps its row, and
    every connected peer repeats the pathname. `also_holds` maps a pid to an
    inode it holds, for building a stranger that holds one of those other rows.
    """
    with tempfile.TemporaryDirectory(prefix=".proc-dm-", dir=REPO) as raw:
        root = Path(raw)
        runtime = root / "runtime"
        (runtime / "shpool").mkdir(parents=True)
        socket_path = runtime / "shpool" / "shpool.socket"
        (root / "net").mkdir()
        rows = ["Num RefCount Protocol Flags Type St Inode Path"]
        for inode, flags in extra_rows:
            rows.append(
                f"0000000000000000: 00000002 00000000 {flags} 0001 01 "
                f"{inode} {socket_path}"
            )
        if listed:
            rows.append(
                f"0000000000000000: 00000002 00000000 {LISTENING} 0001 01 "
                f"{INODE} {socket_path}"
            )
        (root / "net" / "unix").write_text(
            "\n".join(rows) + "\n" + raw_tail, encoding="utf-8"
        )
        # Every candidate that can be checked needs a readable fd directory;
        # absence is the explicit UNKNOWN state, not a negative holder answer.
        for pid in (KIT_DAEMON, FOREIGN_DAEMON):
            (root / str(pid) / "fd").mkdir(parents=True)
        held = {pid: INODE for pid in holders}
        held.update(dict(also_holds))
        for pid, inode in held.items():
            fds = root / str(pid) / "fd"
            fds.mkdir(parents=True, exist_ok=True)
            os.symlink("/dev/null", fds / "1")
            os.symlink(f"socket:[{inode}]", fds / "3")
        for pid in unreadable_links:
            (root / str(pid) / "fd" / "unreadable").write_text(
                "not a symlink", encoding="utf-8"
            )
        for pid in unreadable:
            fds = root / str(pid) / "fd"
            for entry in fds.iterdir():
                entry.unlink()
            fds.rmdir()
            fds.write_text("not a directory", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_PROC_ROOT": str(root),
                "XDG_RUNTIME_DIR": str(runtime),
            },
        ):
            yield root


def roots_for(names, table):
    return process_inventory.shpool_roots(
        names,
        table,
        is_shpool_daemon=process_inventory._is_shpool_daemon,
    )


def generation_for(table):
    return process_inventory.daemon_generation(
        table,
        is_shpool_daemon=process_inventory._is_shpool_daemon,
    )


def picker_screen(document: dict) -> str:
    """Project through the real Bash view and render the real picker page."""
    with tempfile.TemporaryDirectory(prefix=".picker-dm-", dir=REPO) as raw:
        root = Path(raw)
        snapshot = root / "snapshot.json"
        view = root / "view.json"
        snapshot.write_text(json.dumps(document), encoding="utf-8")
        command = r'''
set -euo pipefail
new_temp() { NEW_TEMP="$VIEW_TARGET"; }
MODULE_DIR="$REPO_TARGET/lib/sh"
SNAPSHOT="$SNAPSHOT_TARGET"
QUERY=
PICKER_GROUP_MODE=state
PICKER_MACHINE_EXPANDED=0
SK_PROJECTS_FILE=
source "$REPO_TARGET/lib/sh/shpool_login_view.sh"
build_view
'''
        subprocess.run(
            ["bash", "-c", command],
            check=True,
            env={
                **os.environ,
                "REPO_TARGET": str(REPO),
                "SNAPSHOT_TARGET": str(snapshot),
                "VIEW_TARGET": str(view),
            },
        )
        projected = json.loads(view.read_text(encoding="utf-8"))
    return render_picker_page(
        projected,
        page=1,
        page_size=20,
        style_enabled=False,
        compact=False,
        columns=100,
        now_ms=100_000,
    )


def live_incident_table() -> tuple[dict, list[str]]:
    """The shape measured on a live machine: ten sessions, one test fixture."""
    names = [f"s20200102-0304{index:02d}-{1000000 + index}" for index in range(10)]
    table = {
        KIT_DAEMON: daemon(KIT_DAEMON),
        FOREIGN_DAEMON: daemon(
            FOREIGN_DAEMON,
            argv=[
                "/home/dev/.cargo/bin/shpool",
                "--log-file",
                "/home/dev/claude-tmp/lane/testhome/d.log",
                "--socket",
                "/home/dev/claude-tmp/lane/testhome/shpool.socket",
                "daemon",
            ],
        ),
    }
    for offset, name in enumerate(names):
        pid = 100 + offset
        table[pid] = shell(pid, KIT_DAEMON, name)
    return table, names


class TheMeasuredIncidentTest(unittest.TestCase):
    def test_a_test_fixture_beside_the_real_daemon_hides_nothing(self) -> None:
        table, names = live_incident_table()
        with kernel_says([KIT_DAEMON]):
            resolved, diagnostics = roots_for(names, table)
            self.assertEqual(len(names), len(resolved), diagnostics)
            for offset, name in enumerate(names):
                self.assertEqual(100 + offset, resolved[name])
                self.assertEqual([], diagnostics[name])
            self.assertEqual(KIT_DAEMON, generation_for(table)["pid"])

    def test_the_same_table_without_the_fixture_resolves_identically(self) -> None:
        table, names = live_incident_table()
        with kernel_says([KIT_DAEMON]):
            with_fixture, _ = roots_for(names, table)
        del table[FOREIGN_DAEMON]
        with kernel_says([KIT_DAEMON]):
            without_fixture, _ = roots_for(names, table)
        self.assertEqual(without_fixture, with_fixture)


class CommandLinesAndEnvironmentsCannotLieTest(unittest.TestCase):
    """A reviewer proved argv and environment are not authoritative.

    The pinned shpool never reads `SHPOOL_SOCKET`, and systemd socket activation
    makes it ignore `--socket` and serve the listener it inherited. So a real
    daemon can carry a foreign-looking command line and a stranger can carry
    ours. Only the kernel knows which one is answering us.
    """

    def inverted_table(self) -> tuple[dict, list[str]]:
        table, names = live_incident_table()
        # Their real daemon names a socket that is not the one it serves.
        table[KIT_DAEMON] = daemon(
            KIT_DAEMON,
            argv=["/usr/bin/shpool", "--socket", "/tmp/not-the-one.socket", "daemon"],
        )
        # The stranger names ours, and has a readable namesake child of its own.
        table[FOREIGN_DAEMON] = daemon(
            FOREIGN_DAEMON,
            argv=["/usr/bin/shpool", "--socket", "/run/user/1000/shpool/shpool.socket",
                  "daemon"],
        )
        table[3001] = shell(3001, FOREIGN_DAEMON, names[0])
        return table, names

    def test_the_daemon_holding_our_socket_wins_whatever_argv_claims(self) -> None:
        table, names = self.inverted_table()
        with kernel_says([KIT_DAEMON]):
            resolved, _ = roots_for(names, table)
            self.assertEqual(100, resolved[names[0]])
            self.assertEqual(KIT_DAEMON, generation_for(table)["pid"])

    def test_and_the_stranger_never_wins_by_naming_our_path(self) -> None:
        table, names = self.inverted_table()
        with kernel_says([KIT_DAEMON]):
            resolved, _ = roots_for(names, table)
        self.assertNotEqual(3001, resolved.get(names[0]))


class ForeignChildIsNeverAdoptedTest(unittest.TestCase):
    """The round-1 defect two reviewers found, kept closed."""

    def reviewer_table(self) -> dict:
        return {
            KIT_DAEMON: daemon(KIT_DAEMON),
            FOREIGN_DAEMON: daemon(FOREIGN_DAEMON),
            1001: shell(1001, KIT_DAEMON, "", unreadable=True),
            3001: shell(3001, FOREIGN_DAEMON, "main"),
        }

    def test_a_foreign_child_never_takes_an_unreadable_shells_name(self) -> None:
        with kernel_says([KIT_DAEMON]):
            resolved, diagnostics = roots_for(["main"], self.reviewer_table())
        self.assertEqual({}, resolved)
        self.assertTrue(
            any("unreadable environment" in line for line in diagnostics["main"]),
            diagnostics,
        )

    def test_not_even_when_the_foreign_child_is_the_only_claimant(self) -> None:
        table = self.reviewer_table()
        del table[1001]
        with kernel_says([KIT_DAEMON]):
            self.assertEqual({}, roots_for(["main"], table)[0])

    def test_a_foreign_grandchild_is_not_adopted_either(self) -> None:
        table = self.reviewer_table()
        table[3002] = shell(3002, 3001, "main")
        with kernel_says([KIT_DAEMON]):
            self.assertEqual({}, roots_for(["main"], table)[0])

    def test_our_own_child_still_wins_beside_a_foreign_namesake(self) -> None:
        table = self.reviewer_table()
        table[1001] = shell(1001, KIT_DAEMON, "main")
        with kernel_says([KIT_DAEMON]):
            self.assertEqual({"main": 1001}, roots_for(["main"], table)[0])


class WhenTheKernelCannotAnswerTest(unittest.TestCase):
    """Unanswerable falls back to the old rule, never to a guess."""

    def two_daemons(self) -> tuple[dict, list[str]]:
        table, names = live_incident_table()
        return table, names

    def test_no_holder_at_all_refuses_every_name(self) -> None:
        table, names = self.two_daemons()
        with kernel_says([]):
            resolved, diagnostics = roots_for(names, table)
        self.assertEqual({}, resolved)
        self.assertTrue(
            any("serving this kit" in line for line in diagnostics[names[0]]),
            diagnostics,
        )

    def test_two_holders_refuse_rather_than_pick_one(self) -> None:
        table, names = self.two_daemons()
        with kernel_says([KIT_DAEMON, FOREIGN_DAEMON]):
            self.assertEqual({}, roots_for(names, table)[0])
            self.assertIsNone(generation_for(table))

    def test_a_socket_missing_from_the_kernel_table_refuses(self) -> None:
        table, names = self.two_daemons()
        with kernel_says([KIT_DAEMON], listed=False):
            self.assertEqual({}, roots_for(names, table)[0])

    def test_one_daemon_needs_no_kernel_lookup_at_all(self) -> None:
        table, names = live_incident_table()
        del table[FOREIGN_DAEMON]
        with mock.patch.object(
            process_inventory, "_listening_inodes", side_effect=AssertionError
        ), mock.patch.object(
            process_inventory, "_holds_any_socket", side_effect=AssertionError
        ), mock.patch.object(
            process_inventory, "default_proc_root", side_effect=AssertionError
        ):
            resolved, _ = roots_for(names, table)
            generation = generation_for(table)
        self.assertEqual(len(names), len(resolved))
        self.assertEqual(KIT_DAEMON, generation["pid"])

    def test_no_daemon_at_all_says_so_plainly(self) -> None:
        table = {100: shell(100, 1, "main")}
        with kernel_says([]):
            resolved, diagnostics = roots_for(["main"], table)
            self.assertEqual({}, resolved)
            self.assertTrue(
                any("found 0" in line for line in diagnostics["main"]), diagnostics
            )
            self.assertIsNone(generation_for(table))


class StillRefusesWhatIsGenuinelyAmbiguousTest(unittest.TestCase):
    def test_two_children_of_our_own_daemon_sharing_a_name_are_refused(self) -> None:
        table = {
            KIT_DAEMON: daemon(KIT_DAEMON),
            100: shell(100, KIT_DAEMON, "main"),
            101: shell(101, KIT_DAEMON, "main"),
        }
        with kernel_says([KIT_DAEMON]):
            resolved, diagnostics = roots_for(["main"], table)
        self.assertEqual({}, resolved)
        self.assertTrue(
            any("found 2" in line for line in diagnostics["main"]), diagnostics
        )

    def test_a_child_of_no_daemon_is_never_adopted(self) -> None:
        table = {KIT_DAEMON: daemon(KIT_DAEMON), 100: shell(100, 999, "main")}
        with kernel_says([KIT_DAEMON]):
            self.assertEqual({}, roots_for(["main"], table)[0])

    def test_a_shell_whose_own_argv_says_socket_is_not_a_daemon(self) -> None:
        table, names = live_incident_table()
        table[900] = {
            **shell(900, KIT_DAEMON, "decoy"),
            "cmdline": ["-bash", "--socket", "/tmp/x"],
        }
        with kernel_says([KIT_DAEMON]):
            self.assertEqual(len(names), len(roots_for(names, table)[0]))


class SocketPathResolutionTest(unittest.TestCase):
    def test_the_runtime_directory_is_preferred_and_home_is_the_fallback(self) -> None:
        self.assertEqual(
            "/run/user/1000/shpool/shpool.socket",
            process_inventory._kit_socket_path(
                {"XDG_RUNTIME_DIR": "/run/user/1000", "HOME": "/home/dev"}
            ),
        )
        self.assertEqual(
            "/home/dev/.local/run/shpool/shpool.socket",
            process_inventory._kit_socket_path({"HOME": "/home/dev"}),
        )
        self.assertIsNone(process_inventory._kit_socket_path({}))

    def test_shpool_socket_is_not_consulted_because_shpool_ignores_it(self) -> None:
        """Checked against the pinned source and the installed binary."""
        self.assertEqual(
            "/run/user/1000/shpool/shpool.socket",
            process_inventory._kit_socket_path(
                {
                    "SHPOOL_SOCKET": "/tmp/shpool-does-not-read-this.socket",
                    "XDG_RUNTIME_DIR": "/run/user/1000",
                }
            ),
        )

    def test_a_trailing_slash_or_dots_resolve_to_the_same_path(self) -> None:
        self.assertEqual(
            process_inventory._kit_socket_path({"XDG_RUNTIME_DIR": "/run/user/1000"}),
            process_inventory._kit_socket_path(
                {"XDG_RUNTIME_DIR": "/run/user/1000/../1000/"}
            ),
        )


class DuplicatePathRowsTest(unittest.TestCase):
    """A pathname is not unique in /proc/net/unix.

    Three lanes found the same hole independently: reading only the first row
    for our path let a stranger's inode stand in for ours, and the stranger's
    daemon was then selected and its child attached to one of their rows.
    """

    def test_a_stale_listener_on_our_path_refuses_rather_than_guesses(self) -> None:
        table, names = live_incident_table()
        with kernel_says(
            [KIT_DAEMON],
            extra_rows=[(99001, LISTENING)],
            also_holds={FOREIGN_DAEMON: 99001},
        ):
            resolved, diagnostics = roots_for(names, table)
        self.assertEqual({}, resolved)
        self.assertTrue(
            any("serving this kit" in line for line in diagnostics[names[0]]),
            diagnostics,
        )

    def test_a_stranger_holding_a_stale_row_is_never_selected(self) -> None:
        """The dangerous direction: the stranger must not win outright."""
        table, names = live_incident_table()
        table[3001] = shell(3001, FOREIGN_DAEMON, names[0])
        with kernel_says(
            [],
            extra_rows=[(99001, LISTENING)],
            also_holds={FOREIGN_DAEMON: 99001},
        ):
            resolved, _ = roots_for(names, table)
        self.assertNotEqual(3001, resolved.get(names[0]))

    def test_a_connected_peer_row_is_not_mistaken_for_the_listener(self) -> None:
        table, names = live_incident_table()
        with kernel_says(
            [KIT_DAEMON],
            extra_rows=[(99002, CONNECTED)],
            also_holds={FOREIGN_DAEMON: 99002},
        ):
            resolved, diagnostics = roots_for(names, table)
            self.assertEqual(len(names), len(resolved), diagnostics)
            self.assertEqual(KIT_DAEMON, generation_for(table)["pid"])

    def test_two_listeners_on_our_path_refuse_even_with_one_holder(self) -> None:
        """Ambiguity in the kernel table is ambiguity, whoever holds what."""
        table, names = live_incident_table()
        with kernel_says([KIT_DAEMON], extra_rows=[(99001, LISTENING)]):
            self.assertEqual({}, roots_for(names, table)[0])


class AnotherAccountsDaemonTest(unittest.TestCase):
    """A second account's shpool is not an unknown; it is not a candidate.

    Found live on a shared host, 2026-08-17: a second Unix account started
    its own shpool daemon, and from then on this account could prove nothing.
    Its `/proc/<pid>/fd` belongs to that account, so the socket-holder check
    answered UNKNOWN, the uniqueness rule refused to name any daemon, and
    every session on the board went unprovable — `sp new` produced
    "Unresolved provider session" and `sp go`/`sp close` refused. The kernel
    had already settled it: a listener under /run/user/<uid> is 0700, so a
    daemon owned by another uid can never be the one answering us.
    """

    def foreign_uid(self) -> int:
        return os.getuid() + 1

    def test_another_accounts_daemon_is_not_a_candidate(self) -> None:
        table, names = live_incident_table()
        table[FOREIGN_DAEMON] = {
            **table[FOREIGN_DAEMON],
            "uid": self.foreign_uid(),
        }
        table[KIT_DAEMON] = {**table[KIT_DAEMON], "uid": os.getuid()}
        self.assertEqual(
            [KIT_DAEMON],
            process_inventory._kit_daemons(
                table, process_inventory._is_shpool_daemon
            ),
        )

    def test_the_board_survives_an_unreadable_foreign_daemon(self) -> None:
        """The exact live shape: their fd directory cannot be read at all."""
        table, names = live_incident_table()
        table[FOREIGN_DAEMON] = {
            **table[FOREIGN_DAEMON],
            "uid": self.foreign_uid(),
        }
        table[KIT_DAEMON] = {**table[KIT_DAEMON], "uid": os.getuid()}
        with kernel_says([KIT_DAEMON], unreadable=[FOREIGN_DAEMON]):
            resolved, diagnostics = roots_for(names, table)
            self.assertEqual(len(names), len(resolved), diagnostics)
            self.assertEqual(KIT_DAEMON, generation_for(table)["pid"])

    def test_an_unreadable_owner_stays_a_candidate(self) -> None:
        """No uid on the row is not evidence against the process."""
        table, names = live_incident_table()
        table[FOREIGN_DAEMON] = {**table[FOREIGN_DAEMON], "uid": None}
        self.assertEqual(
            [KIT_DAEMON, FOREIGN_DAEMON],
            process_inventory._kit_daemons(
                table, process_inventory._is_shpool_daemon
            ),
        )

    def test_a_foreign_unreadable_argv_namesake_is_not_a_candidate(self) -> None:
        """The other account's daemon whose command line we cannot read."""
        table, names = live_incident_table()
        del table[FOREIGN_DAEMON]
        table[KIT_DAEMON] = {**table[KIT_DAEMON], "uid": os.getuid()}
        table[FOREIGN_DAEMON] = {
            "pid": FOREIGN_DAEMON,
            "ppid": 1,
            "comm": "shpool",
            "cmdline": [],
            "argv_unreadable": True,
            "start_ticks": FOREIGN_DAEMON * 10,
            "session_name": "",
            "uid": self.foreign_uid(),
        }
        self.assertEqual(
            [KIT_DAEMON],
            process_inventory._kit_daemons(
                table, process_inventory._is_shpool_daemon
            ),
        )


class UnknownKernelEvidenceTest(unittest.TestCase):
    def assert_refuses(self, table, names) -> None:
        self.assertEqual(
            [KIT_DAEMON, FOREIGN_DAEMON],
            process_inventory._kit_daemons(
                table, process_inventory._is_shpool_daemon
            ),
        )
        self.assertEqual({}, roots_for(names, table)[0])
        self.assertIsNone(generation_for(table))

    def test_unreadable_real_fd_directory_never_promotes_visible_coholder(
        self,
    ) -> None:
        table, names = live_incident_table()
        table[3001] = shell(3001, FOREIGN_DAEMON, names[0])
        with kernel_says(
            [KIT_DAEMON, FOREIGN_DAEMON], unreadable=[KIT_DAEMON]
        ):
            self.assert_refuses(table, names)
            self.assertNotEqual(3001, roots_for(names, table)[0].get(names[0]))

    def test_unreadable_fd_entry_is_unknown_not_a_negative(self) -> None:
        table, names = live_incident_table()
        with kernel_says(
            [FOREIGN_DAEMON], unreadable_links=[KIT_DAEMON]
        ):
            self.assert_refuses(table, names)

    def test_unreadable_kernel_table_is_unknown(self) -> None:
        table, names = live_incident_table()
        with kernel_says([FOREIGN_DAEMON]) as root:
            table_path = root / "net" / "unix"
            table_path.unlink()
            table_path.mkdir()
            self.assertIsNone(
                process_inventory._listening_inodes(
                    process_inventory._kit_socket_path(os.environ), root
                )
            )
            self.assert_refuses(table, names)

    def test_partial_kernel_table_after_foreign_listener_refuses(self) -> None:
        table, names = live_incident_table()
        tail = "0000000000000000: 00000002 00000000 00010000 0001 01 22002"
        with kernel_says(
            [],
            listed=False,
            extra_rows=[(99001, LISTENING)],
            also_holds={FOREIGN_DAEMON: 99001},
            raw_tail=tail,
        ):
            self.assert_refuses(table, names)

    def test_unreadable_shpool_argv_blocks_the_single_daemon_fast_path(
        self,
    ) -> None:
        table = {
            KIT_DAEMON: {
                **daemon(KIT_DAEMON),
                "cmdline": [],
                "args_available": False,
            },
            FOREIGN_DAEMON: daemon(FOREIGN_DAEMON),
            3001: shell(3001, FOREIGN_DAEMON, "main"),
        }
        with mock.patch.object(
            process_inventory,
            "_listening_inodes",
            side_effect=AssertionError("unknown argv must refuse before selection"),
        ):
            self.assertEqual(
                [KIT_DAEMON, FOREIGN_DAEMON],
                process_inventory._kit_daemons(
                    table, process_inventory._is_shpool_daemon
                ),
            )
            self.assertEqual({}, roots_for(["main"], table)[0])
            self.assertIsNone(generation_for(table))

    def test_unreadable_non_shpool_argv_does_not_disable_ordinary_fast_path(
        self,
    ) -> None:
        table = {
            KIT_DAEMON: daemon(KIT_DAEMON),
            99: {
                "pid": 99,
                "ppid": 1,
                "comm": "bash",
                "cmdline": [],
                "args_available": False,
                "start_ticks": 990,
            },
        }
        with mock.patch.object(
            process_inventory,
            "_listening_inodes",
            side_effect=AssertionError("ordinary fast path read the kernel table"),
        ):
            self.assertEqual(
                [KIT_DAEMON],
                process_inventory._kit_daemons(
                    table, process_inventory._is_shpool_daemon
                ),
            )


class PayloadBoundInventoryTest(unittest.TestCase):
    def test_live_payload_observation_proves_the_one_daemon_socket_holder(
        self,
    ) -> None:
        table = {KIT_DAEMON: daemon(KIT_DAEMON)}
        original_inodes = process_inventory._listening_inodes
        original_holds = process_inventory._holds_any_socket
        with kernel_says([KIT_DAEMON]), mock.patch.object(
            process_inventory, "_listening_inodes", wraps=original_inodes
        ) as inodes, mock.patch.object(
            process_inventory, "_holds_any_socket", wraps=original_holds
        ) as holders:
            self.assertEqual(
                {"pid": KIT_DAEMON, "process_start_ticks": 100},
                inventory_core._payload_daemon_identity(table),
            )
        self.assertEqual(1, inodes.call_count)
        self.assertEqual(1, holders.call_count)

    def test_darwin_one_daemon_binding_keeps_its_native_fast_path(self) -> None:
        table = {KIT_DAEMON: daemon(KIT_DAEMON)}
        with mock.patch.object(
            inventory_core, "_require_supported_platform", return_value="darwin"
        ), mock.patch.object(
            process_inventory,
            "_listening_inodes",
            side_effect=AssertionError("Darwin has no proc socket table"),
        ):
            self.assertEqual(
                {"pid": KIT_DAEMON, "process_start_ticks": 100},
                inventory_core._payload_daemon_identity(table),
            )

    def test_exact_payload_binding_selects_only_that_daemon_tree(self) -> None:
        table = {
            KIT_DAEMON: daemon(KIT_DAEMON),
            FOREIGN_DAEMON: daemon(FOREIGN_DAEMON),
            1001: shell(1001, KIT_DAEMON, "main"),
            3001: shell(3001, FOREIGN_DAEMON, "main"),
        }
        document = inventory_core.build_inventory(
            {
                "sessions": [
                    {
                        "name": "main",
                        "status": "Disconnected",
                        "started_at_unix_ms": 1,
                    }
                ]
            },
            [],
            table,
            {},
            ({}, {}),
            {"aliases": {}, "max_proc_nodes": 128, "max_proc_depth": 16},
            now=1_800_000_000,
            daemon_binding={"pid": KIT_DAEMON, "process_start_ticks": 100},
        )
        row = document["sessions"][0]
        self.assertEqual(KIT_DAEMON, document["daemon_generation"]["pid"])
        self.assertEqual(1001, row["shpool_shell"]["pid"])
        self.assertEqual("shell", row["provider"])
        self.assertIs(True, row["mutation_allowed"])
        self.assertTrue(inventory_core.strict_live_inventory(document))

    def test_unbound_multi_daemon_payload_is_visible_but_unactionable(self) -> None:
        table = {
            KIT_DAEMON: daemon(KIT_DAEMON),
            FOREIGN_DAEMON: daemon(FOREIGN_DAEMON),
            1001: shell(1001, KIT_DAEMON, "main"),
            3001: shell(3001, FOREIGN_DAEMON, "main"),
            2001: {
                "pid": 2001,
                "ppid": 1001,
                "comm": "claude",
                "cmdline": ["claude"],
                "start_ticks": 20010,
                "cwd": "/operator",
            },
            4001: {
                "pid": 4001,
                "ppid": 3001,
                "comm": "claude",
                "cmdline": ["claude"],
                "start_ticks": 40010,
                "cwd": "/foreign",
            },
        }
        shpool = {
            "sessions": [
                {
                    "name": "main",
                    "status": "Disconnected",
                    "started_at_unix_ms": 1,
                }
            ]
        }
        claude = [
            {
                "pid": 2001,
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "cwd": "/operator",
                "kind": "interactive",
                "name": "Operator conversation",
                "status": "busy",
                "startedAt": 2,
            },
            {
                "pid": 4001,
                "sessionId": "22222222-2222-4222-8222-222222222222",
                "cwd": "/foreign",
                "kind": "interactive",
                "name": "Foreign conversation",
                "status": "idle",
                "startedAt": 3,
            },
        ]
        with kernel_says([FOREIGN_DAEMON]):
            document = inventory_core.build_inventory(
                shpool,
                claude,
                table,
                {},
                ({}, {}),
                {"aliases": {}, "max_proc_nodes": 128, "max_proc_depth": 16},
                now=1_800_000_000,
            )
        row = document["sessions"][0]
        self.assertEqual("main", row["shpool_id_raw"])
        self.assertEqual("unknown", row["provider"])
        self.assertEqual("Unresolved provider session", row["title"])
        self.assertIsNone(row["shpool_shell"])
        self.assertIsNone(row["identity"]["uuid"])
        self.assertIs(False, row["mutation_allowed"])
        self.assertIsNone(document["daemon_generation"])
        self.assertFalse(inventory_core.strict_live_inventory(document))
        rendered = picker_screen(document)
        self.assertIn("1 session · 1 ready · 0 open elsewhere", rendered)
        self.assertIn("Unresolved provider session", rendered)
        self.assertIn("| UNK |", rendered)
        self.assertNotIn("Sessions: none.", rendered)


class ChurnedCensusTest(unittest.TestCase):
    """A process dying mid-scan must not unresolve the whole board.

    ``scan_process_table`` honestly records a hole whenever any process exits
    between readdir and stat. On a busy host some scan is nearly always
    churned, so a reader that treats "the census has a hole" as "there is no
    daemon" turns routine churn into a fully unresolved picker. The kernel's
    socket-holder answer is positive evidence about one inode and stands
    regardless of who else died; only conclusions built on absence withdraw.
    """

    @staticmethod
    def churned(table: dict) -> process_inventory.ProcessTable:
        holed = process_inventory.ProcessTable(table)
        holed.complete = False
        return holed

    def test_socket_holder_proof_survives_a_churned_census(self) -> None:
        table = self.churned({KIT_DAEMON: daemon(KIT_DAEMON)})
        with kernel_says([KIT_DAEMON]):
            self.assertEqual(
                [KIT_DAEMON],
                process_inventory._kit_daemons(
                    table,
                    process_inventory._is_shpool_daemon,
                    require_socket_holder=True,
                ),
            )

    def test_payload_identity_survives_a_churned_census(self) -> None:
        table = self.churned({KIT_DAEMON: daemon(KIT_DAEMON)})
        with kernel_says([KIT_DAEMON]):
            self.assertEqual(
                {"pid": KIT_DAEMON, "process_start_ticks": 100},
                inventory_core._payload_daemon_identity(table),
            )

    def test_census_conclusions_still_refuse_on_a_churned_census(self) -> None:
        table = self.churned({KIT_DAEMON: daemon(KIT_DAEMON)})
        with mock.patch.object(
            process_inventory,
            "_listening_inodes",
            side_effect=AssertionError(
                "census callers must refuse before the kernel"
            ),
        ):
            self.assertEqual(
                [],
                process_inventory._kit_daemons(
                    table, process_inventory._is_shpool_daemon
                ),
            )

    def test_churned_census_keeps_the_unreadable_fd_refusal(self) -> None:
        table = self.churned(
            {
                KIT_DAEMON: daemon(KIT_DAEMON),
                FOREIGN_DAEMON: daemon(FOREIGN_DAEMON),
            }
        )
        with kernel_says(
            [KIT_DAEMON, FOREIGN_DAEMON], unreadable=[KIT_DAEMON]
        ):
            self.assertEqual(
                [],
                process_inventory._kit_daemons(
                    table,
                    process_inventory._is_shpool_daemon,
                    require_socket_holder=True,
                ),
            )


if __name__ == "__main__":
    unittest.main()
