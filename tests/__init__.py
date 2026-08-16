"""session-kit isolated regression tests.

Importing this package arms the sandbox guard, which is what keeps a fixture
from reaching the machine's real session manager. Read
``tests/sandbox_guard.py`` before changing anything here: the guard has to be
installed before the first test module builds a fixture environment, and
importing this package is the one point every suite passes through.

It also pins the terminal, for the same reason and at the same point. See
``pin_terminal`` below.
"""

import errno
import os

from tests import sandbox_guard

sandbox_guard.install()


def pin_terminal() -> None:
    """Answer the ambient-terminal questions here instead of inheriting them.

    ``shutil.get_terminal_size`` in the shipped renderer
    (``sessionkit_inventory/render.py``) asks the process about its window
    rather than about the data, and the width it returns picks which detail
    form a row shows. A test that renders without pinning it therefore
    measured the operator's window: the same unchanged suite was green through
    a pipe and red on a terminal, and three tests changed their answer between
    the two. That cost two reviewers and two lanes a full investigation before
    it was diagnosed, twice as an ordering fault it never was.

    Pinning belongs here rather than in ``tests/run`` alone: a reviewer runs
    ``python3 -m unittest tests.test_inventory`` far more often than the
    runner script, and both paths import this package.

    Refusing the window query is exactly the answer a pipe gives, so every
    caller falls back to the width declared in its own signature and the
    suite's rendered output is byte-identical to the piped runs CI has always
    made. The refusal is a Python-level patch, so it does not reach a child:
    a fixture driving a real pty child still measures that child's real pty.
    (The four pops below DO change what a child inherits -- that is their
    point: they remove the launcher's values, they add none.) Local overrides
    still win either way, because ``shutil.get_terminal_size`` reads COLUMNS
    before it asks the terminal, so a test that wants a width exports one.

    The colour half of the same question is deliberately NOT pinned with an
    environment variable. SESSION_KIT_NO_COLOR and NO_COLOR are *kill
    switches*: `sp doctor` reports a set one as a disabled live channel, and
    tests/test_install.py rightly asserts a clean install answers "no
    supported kill switch". A harness that exported one to make its own
    rendering deterministic would hand every child a switch the operator never
    threw -- so the inherited ones are cleared here instead, and a module that
    needs colour decided pins it for itself (see setUpModule in
    tests/test_inventory.py) or passes ``color_enabled=`` to the renderer.

    Like the sandbox guard, there is deliberately no environment variable that
    switches this off. A switch is the accidental inheritance it exists to
    prevent.
    """
    # Popped, not overwritten: an exported COLUMNS would answer shutil before
    # the refusal below is ever reached, which is the leak, not the fix.
    os.environ.pop("COLUMNS", None)
    os.environ.pop("LINES", None)
    # A kill switch the operator's shell happened to export is not this
    # suite's input either -- it reaches every child and doctor reports it.
    os.environ.pop("SESSION_KIT_NO_COLOR", None)
    os.environ.pop("NO_COLOR", None)

    def _no_terminal(*_args: object, **_kwargs: object) -> "os.terminal_size":
        raise OSError(errno.ENOTTY, "no terminal (pinned by the test suite)")

    os.get_terminal_size = _no_terminal  # type: ignore[assignment]


pin_terminal()
