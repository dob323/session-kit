"""The release collector runs to completion, or says why it did not.

Releases accumulate one per roll. The collector that prunes them read
`/proc/<pid>/environ` for every same-uid process and treated a refusal as
"no proof, delete nothing" — but `environ` is readable only for a dumpable
process, and `systemd --user` is same-uid and never dumpable. So the scan
aborted before the deletion loop on every systemd-user host, silently, from
the day it shipped: 152 release directories and 368 MB with no log line to
notice it by.

A process whose environment cannot be read still contributes its command line
and its working directory, which is what a release reference actually looks
like. Refusals that really do mean "no proof" now name themselves.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from tests.support import REPO

REAPER = REPO / "bin" / "shpool_reaper"
DAY = 86400.0


def release_id(index: int) -> str:
    return f"{index:040x}"


class ReleaseCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="release-gc-")
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.state = self.home / ".local" / "state" / "session-kit"
        self.state.mkdir(parents=True)
        self.root = self.home / ".local" / "lib" / "session-kit"
        self.releases = self.root / "releases"
        self.releases.mkdir(parents=True)
        self.proc = self.base / "proc"
        self.proc.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        shpool = self.bin / "shpool"
        shpool.write_text(
            '#!/usr/bin/env bash\necho \'{"sessions":[]}\'\n', encoding="utf-8"
        )
        shpool.chmod(0o755)

        # Seven releases, all past the 14-day cutoff, oldest first.
        now = time.time()
        self.ids = [release_id(index) for index in range(1, 8)]
        for offset, name in enumerate(self.ids):
            directory = self.releases / name
            (directory / "bin").mkdir(parents=True)
            age = now - (30 - offset) * DAY
            os.utime(directory, (age, age))
        (self.root / "current").symlink_to(self.releases / self.ids[-1])
        (self.state / "install.json").write_text(
            json.dumps(
                {
                    "installed_release": self.ids[-1],
                    "previous_release": self.ids[-2],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_process(
        self, pid: int, *, cmdline: bytes, environ: bytes, readable: bool = True
    ) -> None:
        root = self.proc / str(pid)
        root.mkdir()
        (root / "cmdline").write_bytes(cmdline)
        environ_path = root / "environ"
        environ_path.write_bytes(environ)
        if not readable:
            # What systemd --user looks like from here: same uid, not dumpable.
            environ_path.chmod(0o000)

    def run_reaper(self, *, proc_root: Path | None = None) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "SESSION_KIT_STATE_DIR": str(self.state),
                "SESSION_KIT_SHPOOL_CMD": str(self.bin / "shpool"),
                "SESSION_KIT_PROC_ROOT": str(proc_root or self.proc),
                "SESSION_KIT_DAEMON_PID": "10",
                "SESSION_KIT_REAPER_SENTINEL": str(self.base / "absent"),
                "SESSION_KIT_TESTING": "1",
                "SESSION_KIT_TEST_PLATFORM": "Linux",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return subprocess.run(
            [REAPER],
            cwd=REPO,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )

    def surviving(self) -> set[str]:
        return {path.name for path in self.releases.iterdir()}

    def test_a_process_with_an_unreadable_environment_does_not_stop_the_sweep(
        self,
    ) -> None:
        self.write_process(
            2254, cmdline=b"systemd\0--user\0", environ=b"HOME=/x\0", readable=False
        )
        # A standing window still running an older release keeps it.
        self.write_process(
            4242,
            cmdline=f"bash\0{self.releases / self.ids[3]}/bin/shpool_login\0".encode(),
            environ=b"",
        )
        result = self.run_reaper()
        self.assertIn("release GC removed 3 stale release(s)", result.stderr)
        self.assertNotIn("release GC declined", result.stderr)
        survivors = self.surviving()
        # current, its recorded predecessor, the newest three, and the one a
        # live process references.
        self.assertEqual(set(self.ids[3:]), survivors)

    def test_an_unreadable_process_table_declines_out_loud(self) -> None:
        result = self.run_reaper(proc_root=self.base / "no-such-proc")
        self.assertIn("release GC declined", result.stderr)
        self.assertNotIn("release GC removed", result.stderr)
        self.assertEqual(set(self.ids), self.surviving())

    def test_a_missing_install_record_declines_out_loud(self) -> None:
        (self.state / "install.json").unlink()
        self.write_process(2254, cmdline=b"bash\0", environ=b"")
        result = self.run_reaper()
        self.assertIn("release GC declined: install.json", result.stderr)
        self.assertEqual(set(self.ids), self.surviving())


if __name__ == "__main__":
    unittest.main()
