"""No test in this suite may reach the machine's real session manager.

The hole this closes
--------------------
``bashrc/shpool.bashrc`` hands a window back to the picker with
``command shpool detach``. That call is hardcoded on purpose --
``SESSION_KIT_SHPOOL_CMD`` deliberately does not reach it, because a session
shell must not be redirected by an environment variable -- so the only lever a
test has is PATH. Any fixture that sources the bashrc and drives the
provider-exit path therefore runs whatever ``shpool`` PATH resolves to, on the
default socket, carrying whatever ``SHPOOL_SESSION_NAME`` the fixture set. On
this machine that resolves to a real binary in ``~/.cargo/bin``, and a real
daemon with live sessions is listening. ``detach`` ends a client's view rather
than the session, so the damage is an operator's window thrown back to the
picker mid-work: disruptive and recoverable, not destructive. It is still the
wrong thing for a test to be able to do.

Twelve suites build a fixture HOME. A fix that depends on each of them
remembering is a fix that lasts until the thirteenth. So the guard is global
and installed once, at import of the ``tests`` package.

What the guard does
-------------------
1. Creates a private directory holding a **refusing** ``shpool`` stub. The stub
   prints the command and its arguments, records the attempt, and exits
   non-zero, so a suite that genuinely needed a session manager fails with a
   message naming the problem instead of silently touching the estate.
2. Splices that directory into ``PATH`` -- for this process and, through the
   belt below, for every child -- immediately **before** the first PATH entry
   that holds a real ``shpool`` outside the test sandbox. Placing it there
   rather than at the front matters: a fixture that installs its own stub in
   its sandbox still wins, which is what a test asserting on shpool calls
   needs.
3. Rewrites the environment of every child started through ``subprocess``,
   ``os.execve`` / ``os.execvpe``, ``os.posix_spawn`` / ``os.posix_spawnp`` and
   ``os.spawnve``, so a suite that hands its child a hand-built ``PATH``
   (several do; ``"PATH": "/usr/bin:/bin"`` and ``f"{bin}:{os.environ['PATH']}"``
   are both in the tree) cannot step outside the guard by accident.
4. Pins ``XDG_STATE_HOME`` to agree with ``HOME``. ``__sk_state_root`` is
   ``${XDG_STATE_HOME:-$HOME/.local/state}``, so an inherited value would send
   every state write the bashrc makes -- provider-bounce,
   account-switch-requests, session-color -- outside the fixture. It is unset
   on this machine today, which is luck rather than isolation. A value a test
   sets deliberately is left exactly as the test set it.

The two documented opt-outs
---------------------------
Neither of them can hand a test the real binary. That is the property that
makes them safe to have.

*A test that runs its own private daemon.* Give the binary a name that is not
``shpool``; the guard only ever intercepts the names in
:data:`GUARDED_COMMANDS`. Declare it through :func:`private_daemon_name`, which
refuses a name that would collide and makes the intent visible at the call
site.

*A test that needs no session manager at all.* ``tests/test_cli_help.py``
proves ``sp help`` answers on a machine where shpool is not installed, and a
refusing stub on PATH makes that pass for the wrong reason. :func:`without_shpool`
marks such a child, and the belt answers by removing the guard stub **and every
PATH entry holding a real binary**, so asking for absence gets absence.

There is deliberately **no** environment variable that switches the guard off.
A switch is the accidental escape this module exists to prevent, and
``PrivateDaemonOptOutTests`` asserts one never appears.

Credit: the per-suite version of this -- a PATH stub in the fixture HOME, a
call log, and a tearDown that reads every call back -- was written first for
``tests/test_lifecycle_shell.py`` on ``fix/exits-and-commands`` (b650fc2). The
recording stub and the read-back assertion here are lifted from it.
"""

from __future__ import annotations

import atexit
import inspect
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


REPO = Path(__file__).resolve().parents[1]

# Basenames a test may never resolve to a real binary. Everything about the
# guard is keyed off this tuple, so widening it later is a one-line change.
GUARDED_COMMANDS = ("shpool",)

# Distinct from anything a real tool returns, so a refusal is recognisable in a
# transcript that only kept the exit status.
REFUSAL_EXIT_CODE = 97

# A child environment carrying this asks for the guarded commands to be ABSENT
# rather than refusing. tests/test_cli_help.py needs exactly that: it proves
# `sp help` still answers on a machine with no session manager installed, and a
# refusing stub sitting on PATH makes that test pass for the wrong reason.
# Honouring the request makes the guard stricter, never weaker -- see
# guarded_environment(), which also drops every PATH entry holding a real
# binary, so "absent" can never quietly become "the real one". Ask for it
# through tests.support.without_shpool().
ABSENT_MARKER = "SESSION_KIT_TEST_SHPOOL_ABSENT"

# Captured before anything is pinned, so the belt can tell an inherited value
# apart from one a test chose. INHERITED_PATH is the PATH this process started
# with -- the one that still reaches the machine's real binaries -- kept so a
# test can ask what the guard is standing in front of without hard-coding a
# path that would leak into the public export.
_REAL_HOME = os.environ.get("HOME") or ""
_INHERITED_STATE_HOME = os.environ.get("XDG_STATE_HOME")
# A test run launched from inside a kit-managed session inherits the session's
# own account profile in these. Any suite that starts a real provider CLI in a
# fixture HOME would then write real transcripts into a real account profile —
# 952 such stray directories were cleaned out of two profiles on 2026-08-17.
# Captured here for the same inherited-versus-chosen test the state home uses.
_INHERITED_PROVIDER_DIRS = {
    name: os.environ.get(name)
    for name in (
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "CLAUDE_CODE_PROJECT_DIR_NAME",
        # The estate identity of the SESSION the tests happen to run inside.
        # A generated SHPOOL_SESSION_NAME makes sp classify every launch as
        # machine-origin — the person-origin worktree tests fail on a live
        # box and nowhere else. A fixture that sets its own value keeps it;
        # only the inherited one is dropped.
        "SHPOOL_SESSION_NAME",
        "SHPOOL_SESSION_DIR",
        "SHPOOL_JOURNAL",
        "SESSION_KIT_ORIGIN",
        "SESSION_KIT_ACCOUNT_ALIAS",
        "SESSION_KIT_ACCOUNT_CAPABLE",
    )
    if os.environ.get(name) is not None
}
INHERITED_PATH = os.environ.get("PATH") or ""

_guard_dir: Path | None = None
_refusal_log: Path | None = None
_installed = False
_holder_cache: dict[str, bool] = {}


# --------------------------------------------------------------------------
# Where the guard lives
# --------------------------------------------------------------------------


def guard_dir() -> Path:
    """The directory holding the refusing stubs. Installed on first use."""
    install()
    assert _guard_dir is not None
    return _guard_dir


def refusal_log() -> Path:
    """Every guarded call the run refused, one ``name arg arg`` line each."""
    install()
    assert _refusal_log is not None
    return _refusal_log


def refused_calls() -> list[str]:
    """Read the refusal log back. Empty when nothing was refused."""
    log = refusal_log()
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _refusing_stub(name: str, log: Path) -> str:
    # Single-quoted heredoc-free bash: the log path is baked in absolutely so
    # the stub refuses identically however the child's environment was built.
    return (
        "#!/usr/bin/env bash\n"
        "# Refusing stub installed by tests/sandbox_guard.py. A test reached\n"
        "# for the machine's real session manager; it may not have it.\n"
        f"printf '%s %s\\n' {name!r} \"$*\" >> {str(log)!r}\n"
        "{\n"
        f"  printf 'session-kit tests: refused `%s %s`\\n' {name!r} \"$*\"\n"
        "  printf '  This test process is sandboxed: the real session manager\\n'\n"
        "  printf '  is out of reach, on purpose. A live daemon with the\\n'\n"
        "  printf '  operator sessions is listening on the default socket.\\n'\n"
        "  printf '  Install a fixture stub (tests.support"
        ".install_recording_shpool)\\n'\n"
        "  printf '  or run a private daemon under a non-%s basename\\n' "
        f"{name!r}\n"
        "  printf '  (tests.sandbox_guard.private_daemon_name).\\n'\n"
        f"}} >&2\n"
        f"exit {REFUSAL_EXIT_CODE}\n"
    )


# --------------------------------------------------------------------------
# PATH surgery
# --------------------------------------------------------------------------


def _sandbox_roots() -> tuple[str, ...]:
    roots = [os.fspath(REPO), tempfile.gettempdir()]
    exec_root = os.environ.get("SESSION_KIT_TEST_EXEC_ROOT")
    if exec_root:
        roots.append(exec_root)
    return tuple(os.path.realpath(root) for root in roots)


def _inside_the_sandbox(directory: str) -> bool:
    """Is this PATH entry somewhere a fixture builds its own stubs?

    Fixtures build their sandbox with ``tempfile.TemporaryDirectory(dir=REPO)``
    (or under ``SESSION_KIT_TEST_EXEC_ROOT``), so anything under those roots is
    the test's own and the guard must not shadow it.
    """
    try:
        real = os.path.realpath(directory)
    except (OSError, ValueError):
        return False
    return any(
        real == root or real.startswith(root + os.sep) for root in _sandbox_roots()
    )


def _holds_a_real_guarded_command(directory: str) -> bool:
    """Does this PATH entry hold a guarded command the guard must get in front of?"""
    if not directory or _inside_the_sandbox(directory):
        return False
    cached = _holder_cache.get(directory)
    if cached is not None:
        return cached
    found = False
    for name in GUARDED_COMMANDS:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            found = True
            break
    _holder_cache[directory] = found
    return found


def guarded_path(value: str | None) -> str:
    """``value`` with the guard directory spliced in ahead of any real binary.

    Idempotent: an existing occurrence is removed and re-placed, so the belt can
    run over a PATH the belt already touched without drifting.
    """
    root = os.fspath(guard_dir())
    entries = [entry for entry in (value or "").split(os.pathsep) if entry]
    if not entries:
        # No PATH means the child searches confstr's default. Say that path out
        # loud rather than leaving the guard with nothing to sit in front of.
        default = os.confstr("CS_PATH") or "/bin:/usr/bin"
        entries = [entry for entry in default.split(os.pathsep) if entry]
    entries = [entry for entry in entries if entry != root]
    position = len(entries)
    for index, entry in enumerate(entries):
        if _holds_a_real_guarded_command(entry):
            position = index
            break
    entries.insert(position, root)
    return os.pathsep.join(entries)


def path_without_guarded_commands(value: str | None) -> str:
    """``value`` with the guard directory AND every real holder removed.

    For the test that needs a machine with no session manager at all. Dropping
    the real holders as well as the stub is what makes this safe: asking for
    absence can never hand a child the real binary instead.
    """
    root = os.fspath(guard_dir())
    entries = [entry for entry in (value or "").split(os.pathsep) if entry]
    return os.pathsep.join(
        entry
        for entry in entries
        if entry != root and not _holds_a_real_guarded_command(entry)
    )


# --------------------------------------------------------------------------
# The belt: every child's environment
# --------------------------------------------------------------------------


def guarded_environment(env):
    """A copy of ``env`` a child cannot use to reach the real session manager."""
    if env is None:
        return None
    guarded = dict(env)
    if guarded.get(ABSENT_MARKER) == "1":
        guarded["PATH"] = path_without_guarded_commands(guarded.get("PATH"))
    else:
        guarded["PATH"] = guarded_path(guarded.get("PATH"))

    home = guarded.get("HOME")
    if home and home != _REAL_HOME:
        state_home = guarded.get("XDG_STATE_HOME")
        inherited = state_home is not None and state_home == _INHERITED_STATE_HOME
        if state_home is None or inherited:
            # Absent resolves to exactly this already, so writing it changes
            # nothing and stops an inherited value from ever mattering. A value
            # the test chose is left alone.
            guarded["XDG_STATE_HOME"] = os.path.join(home, ".local", "state")
        # A provider directory the test chose is honoured; one this process
        # merely inherited is dropped, so a real provider CLI started in a
        # fixture HOME falls back to that HOME instead of a real profile.
        for name, inherited_value in _INHERITED_PROVIDER_DIRS.items():
            if guarded.get(name) == inherited_value:
                guarded.pop(name, None)
    return guarded


def _patch_subprocess() -> None:
    original = subprocess.Popen.__init__
    if getattr(original, "_session_kit_guarded", False):
        return
    signature = inspect.signature(original)

    def __init__(self, *args, **kwargs):  # noqa: N807 - patching a dunder
        try:
            bound = signature.bind(self, *args, **kwargs)
        except TypeError:
            # Let the real constructor raise the argument error itself.
            return original(self, *args, **kwargs)
        if bound.arguments.get("env") is not None:
            bound.arguments["env"] = guarded_environment(bound.arguments["env"])
        return original(*bound.args, **bound.kwargs)

    __init__._session_kit_guarded = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = __init__  # type: ignore[method-assign]


def _patch_os_exec() -> None:
    """``os.exec*e`` and friends bypass subprocess entirely.

    Nine suites fork and call ``os.execve`` with a hand-built environment. The
    fork shares this interpreter's memory, so patching here reaches the child.
    """

    def wrap(name: str, index: int) -> None:
        original = getattr(os, name, None)
        if original is None or getattr(original, "_session_kit_guarded", False):
            return

        def wrapper(*args, **kwargs):
            if "env" in kwargs:
                kwargs["env"] = guarded_environment(kwargs["env"])
            elif len(args) > index:
                arguments = list(args)
                arguments[index] = guarded_environment(arguments[index])
                args = tuple(arguments)
            return original(*args, **kwargs)

        wrapper._session_kit_guarded = True  # type: ignore[attr-defined]
        wrapper.__name__ = name
        wrapper.__doc__ = original.__doc__
        setattr(os, name, wrapper)

    # (function, position of the environment argument)
    wrap("execve", 2)
    wrap("execvpe", 2)
    wrap("spawnve", 3)
    wrap("posix_spawn", 2)
    wrap("posix_spawnp", 2)


# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------


def install() -> Path:
    """Arm the guard. Safe to call repeatedly; the first call does the work."""
    global _guard_dir, _refusal_log, _installed
    if _installed:
        assert _guard_dir is not None
        return _guard_dir

    # Some supported hosts mount /tmp noexec, and the stub has to execute, so
    # the guard lives on the repository's filesystem like the fixtures do. The
    # dotted prefix keeps it out of a commit (.gitignore covers `/.*/`) and
    # tests/sweep_sandboxes.py clears one an interrupted run left behind.
    _guard_dir = Path(tempfile.mkdtemp(prefix=".sandbox-guard-", dir=REPO))
    _refusal_log = _guard_dir / "refused-calls.log"
    for name in GUARDED_COMMANDS:
        _write_executable(_guard_dir / name, _refusing_stub(name, _refusal_log))
    atexit.register(shutil.rmtree, _guard_dir, True)
    _installed = True

    os.environ["PATH"] = guarded_path(os.environ.get("PATH"))
    # Drop an inherited value rather than carry it into every os.environ.copy().
    if _INHERITED_STATE_HOME is not None:
        os.environ.pop("XDG_STATE_HOME", None)
    for name in _INHERITED_PROVIDER_DIRS:
        os.environ.pop(name, None)

    _patch_subprocess()
    _patch_os_exec()
    return _guard_dir


# --------------------------------------------------------------------------
# Helpers for fixtures
# --------------------------------------------------------------------------

# The recording stub, lifted from tests/test_lifecycle_shell.py on
# fix/exits-and-commands (b650fc2). It answers `list --json` with an empty but
# VALID payload rather than refusing, so a reopen guard fails on "this terminal
# is not in the list" for a reason the fixture chose rather than on whatever
# the real estate happened to contain that minute.
RECORDING_STUB = (
    "#!/usr/bin/env bash\n"
    '# Recording shpool stub. Writes every call to "$SHPOOL_LOG".\n'
    'printf "%s\\n" "$*" >> "$SHPOOL_LOG"\n'
    "if [[ $1 == list ]]; then printf '{\"sessions\": []}\\n'; exit 0; fi\n"
    "exit 1\n"
)


def install_recording_shpool(bin_dir: Path, log: Path) -> Path:
    """Put a RECORDING ``shpool`` in ``bin_dir`` and return its path.

    The global guard already stops a fixture reaching the real binary. This is
    for the fixture that needs to *assert* on the calls, or that needs ``list
    --json`` to answer rather than refuse. Pass ``SHPOOL_LOG=<log>`` in the
    child environment; ``bin_dir`` must be inside the fixture sandbox, which is
    where the bashrc's ``$HOME/.local/bin`` prepend puts it first.
    """
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stub = bin_dir / "shpool"
    _write_executable(stub, RECORDING_STUB)
    log.parent.mkdir(parents=True, exist_ok=True)
    return stub


def recorded_shpool_calls(log: Path) -> list[str]:
    """Every distinct ``shpool`` call the fixture made, sorted."""
    if not log.exists():
        return []
    return sorted(set(log.read_text(encoding="utf-8").split("\n")) - {""})


def unexpected_shpool_calls(log: Path, allowed: tuple[str, ...]) -> list[str]:
    """Calls outside ``allowed``. A new one is a new way to reach a daemon."""
    return [call for call in recorded_shpool_calls(log) if call not in allowed]


def without_shpool(env: dict[str, str]) -> dict[str, str]:
    """``env`` for a child that must find NO session manager at all.

    ``tests/test_cli_help.py`` proves ``sp help`` answers on a machine where
    shpool is not installed. A refusing stub on PATH would make that test pass
    for the wrong reason, so this asks the belt for absence instead -- and the
    belt delivers absence by removing the real holders too, so the request can
    never turn into access.
    """
    return {**env, ABSENT_MARKER: "1"}


def private_daemon_name(name: str) -> str:
    """Declare a private daemon binary a test is allowed to really run.

    The one legitimate escape from the guard, and the only one: a test that
    starts its own daemon under a basename the guard does not intercept.
    ``/home/.../exits`` did this correctly with ``sk-exitproof-pool``. Calling
    this makes the intent explicit at the call site and refuses a name that
    would collide with the guarded set.
    """
    if name in GUARDED_COMMANDS:
        raise AssertionError(
            f"{name!r} is guarded: a test may not run the real binary under "
            f"that name. Give the private daemon its own basename (the exits "
            f"branch used 'sk-exitproof-pool') so the guard never has to "
            f"decide whether this particular call was safe."
        )
    return name
