"""Clear what an interrupted run left behind, before the next run starts.

Fixtures build their sandbox with `tempfile.TemporaryDirectory(dir=REPO)` and
rely on interpreter shutdown to remove it. That covers a clean exit and a
KeyboardInterrupt; it does not cover SIGKILL, a harness timeout, or a traceback
that keeps the fixture alive. And some suites start a REAL provider CLI inside
the sandbox: two `claude` processes were found alive one day and twenty hours
after the run that spawned them, holding a conversation id with their sandbox
working directory deleted underneath them. The crontab orphan reaper missed
them because it requires `ppid == 1` and these had reparented to
`systemd --user`.

So the sweep is by sandbox, not by parent: any same-user process whose working
directory is inside a stale sandbox goes with the sandbox. Nothing younger than
the grace period is touched, so a run in flight -- including a parallel one --
is never disturbed.

Which names count as a sandbox is READ FROM THE SUITE, never listed here. The
hand-written list this file used to carry named six of the two hundred and
more the suite hands tempfile, so most of what a crashed run leaves stayed
where it was -- including the leaked processes this exists to kill, because it
decides those by sandbox too. A list somebody has to remember to extend is a
list that is wrong from the next commit onward. `sandbox_prefixes()` parses the
test sources instead, and `tests/test_sandbox_sweep.py` fails when a fixture
writes a prefix that parse cannot read.

Run for its effect, from tests/run. It prints only what it actually removed.
`--list` prints the same judgements and removes nothing.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import shutil
import signal
import string
import sys
import time

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"

# The two calls that make a DIRECTORY under a name tempfile chooses. mkstemp
# and NamedTemporaryFile are deliberately absent: they leave a file, and this
# sweep only ever removes directories.
SANDBOX_CALLS = frozenset({"TemporaryDirectory", "mkdtemp"})

# The characters tempfile draws the random part of a name from
# (tempfile._RandomNameSequence.characters). A leftover sandbox is a known
# prefix followed by a run of these and nothing else, which is what separates
# `.watchdog-g2hiuui4` from a directory somebody deliberately named
# `.watchdog-notes`. Note the absence of `-`: it keeps a short prefix from
# claiming a longer sibling, so `.reap-` cannot answer for `.reap-er-x1y2`.
RANDOM_NAME_CHARACTERS = frozenset(string.ascii_lowercase + string.digits + "_")

# Never removed, whatever the derived prefixes say, and checked before any
# prefix is consulted.
#
# `.git` is first because it is the only entry here whose loss cannot be
# undone: every commit not yet pushed lives inside it and nothing in the
# working tree can rebuild it. The other three are the repository's own dotted
# entries at the root -- source, not litter -- and the same argument applies to
# each: a mistake about them is not litter left behind, it is source removed.
# No prefix in the tree reaches any of the four today; this is what holds if
# one ever does, and tests/test_sandbox_sweep.py proves it by feeding the
# matcher a prefix that would swallow `.git`.
#
# This is NOT `tests.support.SOURCE_ROOT_DOTTED` and must never be replaced by
# it. That set answers a different question -- what a COPY of this tree must
# leave behind -- and it deliberately omits `.git`, because a copy carrying
# history is the bug it was written to stop. Two rules with opposite
# requirements for the same name; sharing one constant between them puts a
# deleting rule one edit away from losing the repository.
NEVER_REMOVE = frozenset({".git", ".github", ".gitignore", ".shellcheckrc"})

# Long enough that no live run is ever in range, short enough that a leak costs
# one run rather than a week. The override exists so this file can be tested
# against a sandbox created a moment ago.
GRACE_SECONDS = int(os.environ.get("SESSION_KIT_SWEEP_GRACE_SECONDS") or 2 * 3600)


def called_name(node: ast.Call) -> str:
    """`tempfile.mkdtemp(...)` and `mkdtemp(...)` both answer `mkdtemp`."""
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    if isinstance(function, ast.Name):
        return function.id
    return ""


def string_literal(expression: ast.expr | None) -> str | None:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value
    return None


def string_argument(node: ast.Call, keyword: str, position: int) -> str | None:
    """The literal a call passes for `keyword`, by name or at `position`."""
    for entry in node.keywords:
        if entry.arg == keyword:
            return string_literal(entry.value)
        if entry.arg is None:
            # `**kwargs` could carry the prefix; nothing here can read it.
            return None
    if position < len(node.args):
        return string_literal(node.args[position])
    return None


def parse_test_sources() -> dict[Path, ast.Module]:
    """Every test module that parses, by path.

    A file that does not parse is skipped and said out loud rather than taken
    as having no sandboxes. Skipping narrows the sweep, which leaves litter;
    the alternative reading -- treat an unparseable file as empty and carry on
    silently -- is how the previous list went stale unnoticed.
    """
    trees: dict[Path, ast.Module] = {}
    if not TESTS.is_dir():
        print(f"tests: no {TESTS}, sweeping nothing", file=sys.stderr)
        return trees
    for path in sorted(TESTS.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            trees[path] = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as error:
            print(
                f"tests: cannot read sandbox names from {path}: {error}",
                file=sys.stderr,
            )
    return trees


def sandbox_factories(trees: dict[Path, ast.Module]) -> dict[str, tuple[str, int]]:
    """Helpers that forward a caller's string straight to tempfile.

    `tests/test_worktree_isolation.sandbox(prefix)` is the shape: one function
    the whole file goes through, so the literals sit at its call sites instead
    of at the tempfile call. Maps the helper's name to the name and position of
    the parameter it forwards, so either spelling of a call site can be read.
    Names are collected across the whole tree because helpers are imported
    between modules.
    """
    factories: dict[str, tuple[str, int]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            parameters = [argument.arg for argument in node.args.args]
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                if called_name(inner) not in SANDBOX_CALLS:
                    continue
                for entry in inner.keywords:
                    if entry.arg != "prefix":
                        continue
                    forwarded = entry.value
                    if isinstance(forwarded, ast.Name) and forwarded.id in parameters:
                        factories[node.name] = (
                            forwarded.id,
                            parameters.index(forwarded.id),
                        )
    return factories


def sandbox_prefixes() -> frozenset[str]:
    """Every prefix the suite can hand tempfile, read from the test sources.

    Call sites are taken whatever their `dir=` argument says. Where a fixture
    MEANT to put its sandbox is not a property this file can evaluate -- the
    argument is an expression, and several are read from the environment -- and
    guessing at it is what a sweep must not do. What makes a removal safe is
    the name: a prefix from this set followed by tempfile's random characters
    and nothing else, at the repository root, outside `NEVER_REMOVE`. A prefix
    belonging to a sandbox that normally lands in /tmp costs nothing here,
    because no such directory appears at the root; a fixture that moves its
    sandbox INTO the checkout is covered from the same commit that moves it.
    """
    trees = parse_test_sources()
    factories = sandbox_factories(trees)
    found: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = called_name(node)
            if name in SANDBOX_CALLS:
                # tempfile takes prefix second, after suffix.
                prefix = string_argument(node, "prefix", 1)
            elif name in factories:
                keyword, position = factories[name]
                prefix = string_argument(node, keyword, position)
            else:
                continue
            if isinstance(prefix, str) and prefix:
                found.add(prefix)
    return frozenset(found)


def is_a_sandbox_name(name: str, prefixes: frozenset[str]) -> bool:
    """Could the suite have created a directory called this?

    Protected names are refused before any prefix is looked at, so widening the
    prefix set can never reach one.
    """
    if name in NEVER_REMOVE:
        return False
    for prefix in prefixes:
        if not name.startswith(prefix):
            continue
        random_part = name[len(prefix) :]
        if random_part and set(random_part) <= RANDOM_NAME_CHARACTERS:
            return True
    return False


def stale_sandboxes(prefixes: frozenset[str]) -> list[Path]:
    cutoff = time.time() - GRACE_SECONDS
    found = []
    for entry in REPO.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        if not is_a_sandbox_name(entry.name, prefixes):
            continue
        # Nothing outside the checkout, ever. iterdir yields only children of
        # REPO and the symlinks are already gone, so this cannot point
        # elsewhere; it is checked anyway because rmtree is what comes next.
        if entry.resolve().parent != REPO:
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        found.append(entry)
    return sorted(found)


def in_a_sandbox(cwd: str, prefixes: frozenset[str]) -> bool:
    """Is this working directory inside a fixture sandbox, live or deleted?

    The kernel appends " (deleted)" once the directory is gone, and gone is the
    normal case: the fixture's own cleanup removes the sandbox and the process it
    started survives with a working directory that no longer exists. That is the
    pair the audit found still running after a day and twenty hours.
    """
    if cwd.endswith(" (deleted)"):
        cwd = cwd[: -len(" (deleted)")]
    try:
        relative = Path(cwd).relative_to(REPO)
    except ValueError:
        return False
    head = relative.parts[0] if relative.parts else ""
    return is_a_sandbox_name(head, prefixes)


def process_age_seconds(pid: str) -> float | None:
    """How long this process has been running, from its own start time.

    The /proc entry's mtime is not the start time -- the kernel refreshes it --
    so the age comes from field 22 of /proc/<pid>/stat against system uptime.
    That is the only age a process whose sandbox is already gone can be judged
    by, and the judgement has to be exact: it decides what gets killed.
    """
    try:
        with open("/proc/uptime", encoding="utf-8") as handle:
            uptime = float(handle.read().split()[0])
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            fields = handle.read()
    except (OSError, ValueError, IndexError):
        return None
    # The command name is parenthesised and may contain spaces: split after it.
    try:
        rest = fields[fields.rindex(")") + 1 :].split()
        started_ticks = float(rest[19])
    except (ValueError, IndexError):
        return None
    ticks = os.sysconf("SC_CLK_TCK") or 100
    return max(0.0, uptime - started_ticks / ticks)


def leaked_processes(
    stale: list[Path], prefixes: frozenset[str]
) -> list[tuple[int, str]]:
    """Same-user pids that cannot belong to a live run, inside a sandbox.

    Two ways to be sure. A process sitting in a sandbox this sweep has already
    judged stale goes with it whatever its own age. Otherwise the sandbox is
    gone -- the fixture removed it and the process outlived it -- and the
    process must be older than the grace period before anything is killed.
    """
    uid = os.geteuid()
    condemned = [os.fspath(root) for root in stale]
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        base = f"/proc/{name}"
        try:
            if os.stat(base, follow_symlinks=False).st_uid != uid:
                continue
            cwd = os.readlink(f"{base}/cwd")
        except OSError:
            continue
        if not in_a_sandbox(cwd, prefixes):
            continue
        plain = cwd[: -len(" (deleted)")] if cwd.endswith(" (deleted)") else cwd
        inside_stale = any(
            plain == root or plain.startswith(root + os.sep) for root in condemned
        )
        if not inside_stale:
            age = process_age_seconds(name)
            if age is None or age < GRACE_SECONDS:
                continue
        try:
            with open(f"{base}/cmdline", "rb") as handle:
                command = handle.read(200).replace(b"\0", b" ").decode(
                    "utf-8", "replace"
                ).strip()
        except OSError:
            command = "?"
        out.append((int(name), command))
    return out


def main(argv: list[str] | None = None) -> int:
    listing = "--list" in (argv if argv is not None else sys.argv[1:])
    prefixes = sandbox_prefixes()
    roots = stale_sandboxes(prefixes)
    leaked = leaked_processes(roots, prefixes)
    if not roots and not leaked:
        return 0
    if listing:
        for pid, command in leaked:
            print(f"tests: would end leaked process {pid} ({command[:60]})")
        for root in roots:
            print(f"tests: would remove stale sandbox {root.name}")
        return 0
    for pid, command in leaked:
        # The process group: a provider CLI runs under `script`, and killing the
        # wrapper alone leaves the CLI holding its conversation.
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        print(f"tests: ended leaked process {pid} ({command[:60]})")
    if leaked:
        time.sleep(0.5)
        for pid, _command in leaked:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
    for root in roots:
        shutil.rmtree(root, ignore_errors=True)
        print(f"tests: removed stale sandbox {root.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
