"""Run the suite across several worker processes instead of one.

The whole suite is two of the three things the release gate spends its hour on:
`tests/run` inside the exact public export, and the same suite again under
coverage. Both were one process walking 99 modules in a row, and both are
bounded by wall clock rather than by work -- most of the expensive modules sit
in a pty waiting for a picker to repaint, so a single process is idle for most
of its own runtime.

Two properties this has to keep, and they are why it is a work queue rather
than a fixed split:

  * Balance without a table to maintain. Module cost is not predictable from
    anything in the file -- the biggest module by bytes is not the slowest, and
    the slowest are the pty suites, which are small and mostly asleep. Workers
    therefore PULL the next module as they free up, so the split adapts to
    whatever the modules actually cost today. SLOW_FIRST only decides what is
    handed out early; it is a hint about balance and can never change a result.

  * One module, one process. Modules that build sandboxes inside the checkout
    run beside modules that copy the checkout, and the isolation that makes
    that safe is process isolation (see tests/sweep_sandboxes.py). Splitting
    finer than a module would also split fixtures that share class-level state.

Scheduling pressure is the risk this carries, not correctness. tests/run
records that renicing the suite took a modal pty test from three of four
passing to none of four, so the pty suites fail when they lose their slice.
Workers therefore default to a number that leaves the machine headroom rather
than to the CPU count.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]

# Handed out first so the long poles are never the last thing started: a queue
# whose slowest module starts last finishes no sooner than a serial run of the
# rest plus that module. The floor for any split is the slowest single module,
# so test_watchdog alone sets about six and a half minutes.
#
# These are measured, with the seconds each took on 2026-08-18 (4 workers, 4
# CPUs). Guessing was worse than useless: the first version of this list led
# with test_inventory, the largest module in the tree at 344 KB, which runs in
# 3.6 s -- while test_watchdog, the real long pole at over six minutes, was not
# in it at all and got handed out near the end.
#
# Refresh from the per-module table this prints at the end of every run.
# Nothing here affects which tests run or whether they pass: an unlisted slow
# module costs balance and nothing else.
SLOW_FIRST = (
    "test_watchdog",  # 388s
    "test_install",  # 171s
    "test_commands",  # 168s
    "test_login",  #  99s
    "test_account_auto_switch",  #  70s
    "test_model_availability",  #  67s
    "test_launch_log",  #  62s
    "test_tui_acceptance",  #  58s
    "test_picker_events",  #  47s
    "test_attach_tty_handoff",  #  37s
)


def modules() -> list[str]:
    """Every test module, slowest-known first, then the rest by name."""
    found = sorted(path.stem for path in (REPO / "tests").glob("test_*.py"))
    known = [name for name in SLOW_FIRST if name in found]
    return known + [name for name in found if name not in set(known)]


def default_workers() -> int:
    """Leave the machine headroom rather than saturating it.

    A worker that loses its scheduling slice mid-read fails pty tests that are
    behaving correctly, which is the same fault tests/run refuses to reintroduce
    by renicing on CI. Half the CPUs, at least two, never more than four: past
    four the suite is waiting on sleeps rather than on processors anyway.
    """
    available = os.cpu_count() or 2
    return max(2, min(4, available // 2))


def command(module: str, *, coverage: bool) -> list[str]:
    if coverage:
        # --parallel-mode gives every worker its own data file, which is what
        # makes `coverage combine` afterwards meaningful rather than a race.
        return [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--parallel-mode",
            "--branch",
            "--source=lib",
            "-m",
            "unittest",
            "-v",
            module,
        ]
    return [sys.executable, "-m", "unittest", "-v", module]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=default_workers())
    parser.add_argument("--coverage", action="store_true")
    options = parser.parse_args()

    queue = modules()
    total = len(queue)
    workers = max(1, min(options.workers, total))
    print(f"running {total} modules across {workers} worker(s)", flush=True)

    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    running: dict[subprocess.Popen[bytes], tuple[str, float]] = {}
    timings: list[tuple[float, str]] = []
    failed: list[str] = []
    started = time.monotonic()

    def start_next() -> None:
        if not queue:
            return
        module = queue.pop(0)
        process = subprocess.Popen(
            command(f"tests.{module}", coverage=options.coverage),
            cwd=REPO,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        running[process] = (module, time.monotonic())

    for _ in range(workers):
        start_next()

    while running:
        for process in list(running):
            if process.poll() is None:
                continue
            module, began = running.pop(process)
            output = process.stdout.read() if process.stdout else b""
            if process.stdout:
                process.stdout.close()
            took = time.monotonic() - began
            timings.append((took, module))
            if process.returncode != 0:
                failed.append(module)
                # The failing module's whole output, so a parallel run is no
                # harder to read than a serial one at the point it matters.
                print(f"\n===== FAILED tests.{module} =====", flush=True)
                print(output.decode("utf-8", errors="replace"), flush=True)
            else:
                print(f"  ok  {module} ({took:.1f}s)", flush=True)
            start_next()
        if running:
            time.sleep(0.05)

    elapsed = time.monotonic() - started
    print("\nslowest modules:", flush=True)
    for took, module in sorted(timings, reverse=True)[:10]:
        print(f"  {took:7.1f}s  {module}", flush=True)
    print(f"\n{total} modules in {elapsed:.1f}s across {workers} worker(s)")
    if failed:
        print(f"FAILED modules: {' '.join(sorted(failed))}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
