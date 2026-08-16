"""The picker's error channel is a terminal for its whole life, or nobody can type.

shpool's client only puts a terminal into raw mode when stdin, stdout AND
stderr are all a terminal, and when one of them is not it declines *silently*
(libshpool/src/tty.rs, set_attach_flags). So a picker that loses fd 2 keeps
drawing and keeps taking keys, while every session it opens afterwards comes up
in line-typing mode: keystrokes echoed by the kernel as literal ^L over the
provider's first screen, nothing delivered until Enter, and no message anywhere
saying why. The only cure a person has is `stty raw -echo` typed from another
window, which is what the operator had to do at 00:46 on 2026-08-15 after
twelve minutes of it.

Two ways in, and both are covered here:

  * `picker_events_stop` closed the event subscription with a redirection
    written on a bare `exec`. Bash applies those to the SHELL, permanently, so
    the close also pointed this picker's stderr at /dev/null for good -- and
    `picker_events_stop` runs on the ordinary resubscribe path (an event stream
    the daemon dropped) and again inside `cleanup` before the self-upgrade
    execs the new release, which inherits the poisoned descriptor.

  * `bin/shpool_login` checked `-t 0` and `-t 1` at startup and never `-t 2`,
    so a picker handed a redirected error channel -- by a parent, or by the
    self-upgrade above -- drew a normal screen and said nothing.

The scan at the end keeps the shape from coming back anywhere in the tree.
"""

from __future__ import annotations

import os
from pathlib import Path
import pty
import re
import select
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parent.parent
LIVE = REPO / "lib" / "sh" / "shpool_login_live.sh"
LOGIN = REPO / "bin" / "shpool_login"


def _drain(master: int, child: int) -> str:
    captured = bytearray()
    while True:
        readable, _, _ = select.select([master], [], [], 20)
        if not readable:
            break
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        captured.extend(chunk)
    os.waitpid(child, 0)
    os.close(master)
    return captured.decode("utf-8", "replace")


def run_on_a_terminal(
    script: str,
    env_updates: dict | None = None,
    stderr_replacement: int | None = None,
) -> str:
    """Run a bash script in a window shaped like a login window.

    pty.fork gives the child the terminal as its CONTROLLING terminal, the way
    sshd does. That matters: `/dev/tty` only opens for a process that has one,
    and the repair in bin/shpool_login goes through /dev/tty. A pty passed as
    plain descriptors would make the repair look impossible when it is not.

    Returns everything the terminal received.
    """

    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # A picker started by the test runner must not inherit the runner's own
    # kit settings; SESSION_KIT_NONINTERACTIVE in particular would end the
    # real bin/shpool_login before it reaches anything this file is about.
    for inherited in list(environment):
        if inherited.startswith("SESSION_KIT_"):
            del environment[inherited]
    environment.update(env_updates or {})
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(script)
        probe = handle.name
    try:
        child, master = pty.fork()
        if child == 0:  # pragma: no cover - the child execs or dies
            try:
                if stderr_replacement is not None:
                    os.dup2(stderr_replacement, 2)
                os.execve("/bin/bash", ["/bin/bash", probe], environment)
            finally:
                os._exit(127)
        return _drain(master, child)
    finally:
        os.unlink(probe)


# Everything bin/shpool_login assigns before it sources the live module, so the
# real picker_events_stop below is the shipped one and not a copy.
PRELUDE = f"""#!/usr/bin/env bash
set -u
SCRIPT_DIR={REPO}/bin
source {REPO}/bin/session_kit_common
STATUS_CMD=$SCRIPT_DIR/shpool_status
SP_CMD=$SCRIPT_DIR/sp
TEMP_FILES=()
PICKER_EVENTS_FD=
PICKER_EVENTS_PID=
PICKER_PULSE_PID=
PICKER_EVENTS_FILE=
PICKER_EVENTS_DOWN=
PICKER_SCREEN=0
PICKER_TTY_STATE=""
PICKER_EVENTS_STATE=off
PICKER_PULSE_STATE=off
source {LIVE}
"""


class PickerErrorChannelSurvives(unittest.TestCase):
    """The shipped picker_events_stop, called on a real terminal."""

    def test_stopping_the_subscription_leaves_stderr_on_the_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            events = Path(scratch) / "events"
            events.write_text("", encoding="utf-8")
            output = run_on_a_terminal(
                PRELUDE
                + f"""
PICKER_EVENTS_FILE={events}
exec {{PICKER_EVENTS_FD}}< "$PICKER_EVENTS_FILE"
printf 'BEFORE stderr=%s tty=%s\\n' \\
  "$(readlink /proc/$$/fd/2)" "$([ -t 2 ] && echo yes || echo no)"
picker_events_stop
printf 'AFTER stderr=%s tty=%s\\n' \\
  "$(readlink /proc/$$/fd/2)" "$([ -t 2 ] && echo yes || echo no)"
""",
                {"SESSION_KIT_STATE_DIR": scratch},
            )
        before = re.search(r"BEFORE stderr=(\S+) tty=(\S+)", output)
        after = re.search(r"AFTER stderr=(\S+) tty=(\S+)", output)
        self.assertIsNotNone(before, output)
        self.assertIsNotNone(after, output)
        self.assertEqual(before.group(2), "yes", output)
        self.assertEqual(
            after.group(2),
            "yes",
            "picker_events_stop redirected this picker's stderr away from the "
            "terminal; every session it opens afterwards will come up unable "
            "to receive typing. Captured:\n" + output,
        )
        self.assertEqual(before.group(1), after.group(1), output)

    def test_stopping_the_subscription_still_closes_it(self) -> None:
        """The suppression may move; the close may not stop happening."""

        with tempfile.TemporaryDirectory() as scratch:
            events = Path(scratch) / "events"
            events.write_text("", encoding="utf-8")
            output = run_on_a_terminal(
                PRELUDE
                + f"""
PICKER_EVENTS_FILE={events}
exec {{PICKER_EVENTS_FD}}< "$PICKER_EVENTS_FILE"
opened=$PICKER_EVENTS_FD
picker_events_stop
if [[ -e /proc/$$/fd/$opened ]]; then
  printf 'SUBSCRIPTION still-open\\n'
else
  printf 'SUBSCRIPTION closed\\n'
fi
printf 'SLOT [%s]\\n' "$PICKER_EVENTS_FD"
""",
                {"SESSION_KIT_STATE_DIR": scratch},
            )
        self.assertIn("SUBSCRIPTION closed", output)
        self.assertIn("SLOT []", output)

    def test_a_descriptor_already_gone_stays_quiet(self) -> None:
        """The noise the suppression was written for is still suppressed.

        A slot that names a descriptor bash no longer holds must not print
        `bad file descriptor` onto a person's screen, and must not end the
        picker either.
        """

        with tempfile.TemporaryDirectory() as scratch:
            output = run_on_a_terminal(
                PRELUDE
                + """
PICKER_EVENTS_FD=61
picker_events_stop
printf 'SURVIVED rc=%s\\n' "$?"
""",
                {"SESSION_KIT_STATE_DIR": scratch},
            )
        self.assertIn("SURVIVED rc=0", output)
        self.assertNotIn("bad file descriptor", output.lower())


class LoginChecksTheThirdDescriptor(unittest.TestCase):
    """bin/shpool_login refuses to draw over a broken error channel in silence."""

    def _login_with_stderr_on_a_pipe(self, scratch: str) -> tuple[str, bytes]:
        """Start the real picker with fd 2 on a pipe and fd 0/1 on a terminal.

        The helper paths are pointed at nothing, so the run stops two lines
        later at "the picker's helpers are missing" -- far enough to prove
        what this test is about, without a whole picker fixture.
        """

        read_end, write_end = os.pipe()
        try:
            on_terminal = run_on_a_terminal(
                f'exec "{LOGIN}"\n',
                {
                    "SESSION_KIT_STATE_DIR": scratch,
                    "SESSION_KIT_STATUS_CMD": os.path.join(scratch, "no-such-status"),
                    "SESSION_KIT_SP_CMD": os.path.join(scratch, "no-such-sp"),
                },
                stderr_replacement=write_end,
            )
        finally:
            os.close(write_end)
        on_pipe = os.read(read_end, 65536)
        os.close(read_end)
        return on_terminal, on_pipe

    def test_a_redirected_error_channel_is_named_and_put_back(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            on_terminal, on_pipe = self._login_with_stderr_on_a_pipe(scratch)
        self.assertIn(
            "error output was not the screen",
            on_terminal,
            "the picker started over a redirected error channel without "
            "saying so; a session opened from this window would have come up "
            "unable to receive typing. Terminal saw:\n" + on_terminal,
        )
        # Proof the descriptor was actually rebound rather than merely
        # described: the later refusal is written to fd 2, and it lands on the
        # terminal instead of in the pipe it started on.
        self.assertIn("helpers are missing", on_terminal, on_terminal)
        self.assertNotIn(b"helpers are missing", on_pipe)


class BareExecNeverRedirectsTheShell(unittest.TestCase):
    """A redirection written on a bare `exec` is permanent. Keep it at zero.

    `exec {FD}<&- 2>/dev/null` reads like it quietens one close. It also moves
    the shell's own stderr, forever, and that is what deafened a person's
    terminal. Nothing in this tree needs the shape; where a bare `exec` really
    must reassign fd 0/1/2 for the rest of a program, add it here with a
    reason rather than letting the pattern back in unexamined.
    """

    # A redirection is an optional source ({name} or digits) then an operator.
    # An operator with no source is fd 1 for writes and fd 0 for reads.
    REDIRECT = re.compile(r"(\{[A-Za-z_][A-Za-z0-9_]*\}|\d+)?(&>>|&>|>>|<>|>&|<&|>|<)")

    # The one place the tree means it, with the reason. bin/shpool_login puts
    # a redirected error channel back on the screen at startup precisely
    # because a permanent reassignment is what is wanted there: the picker
    # runs for hours after this line, and every session it opens has to
    # inherit a terminal on fd 2 or come up unable to receive typing.
    # Verified individually before being listed. `bin/shpool_login` puts a
    # redirected error channel back on the screen at startup precisely because a
    # permanent reassignment is what is wanted there: the picker runs for hours
    # after this line, and every session it opens has to inherit a terminal on
    # fd 2 or come up unable to receive typing.
    #
    # The three `sp_core.sh` lines arrived with the keyboard backstop, which was
    # written before this guard existed, so the merge is the first time they met.
    # Each was checked rather than assumed:
    #
    #   :114 is the body of `sk_bind_handoff_tty`, whose only caller is
    #        `attach_id()`, which ends in `exec "$SK_SHPOOL" ...`. The binding is
    #        replaced along with the process, so it cannot outlive the attach.
    #   :140 is `sk_handoff_guard_arm`, which saves the old fd 2 first and is
    #        released by `sk_handoff_unbind` on every exit path, including the
    #        EXIT, INT, TERM and HUP traps. This pair exists BECAUSE an unscoped
    #        binding on the picker door -- which does not exec -- outlived its
    #        attach once already.
    #   :148 is that release itself, putting fd 2 back where the caller had it.
    DELIBERATE = {
        ("bin/shpool_login", "exec 2>/dev/tty"),
        ("lib/sh/sp_core.sh", "exec 2>&1"),
        ("lib/sh/sp_core.sh", 'exec 2>&"$SK_HANDOFF_ERR"'),
    }

    def _standard_descriptors(self, remainder: str) -> list[str]:
        found = []
        for source, operator in self.REDIRECT.findall(remainder):
            if source.startswith("{"):
                continue
            if source == "":
                source = "0" if operator.startswith("<") else "1"
            if source in {"0", "1", "2"}:
                found.append(source + operator)
            if operator == "&>" or operator == "&>>":
                found.append(operator)
        return found

    def test_no_shell_script_moves_its_own_stdio_on_a_bare_exec(self) -> None:
        listing = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        offenders = []
        allowed = []
        matched = set()
        for name in listing:
            path = REPO / name
            if path.suffix not in {".sh", ".bash", ""}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "exec" not in text:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                code = line.split("#", 1)[0]
                match = re.search(r"(?:^|[;&|]|\bthen\b|\bdo\b)\s*exec\s+(.*)$", code)
                if match is None:
                    continue
                remainder = match.group(1).strip()
                # A command word after `exec` replaces the process; the
                # redirection goes with the new program and cannot outlive it.
                if remainder and not re.match(r"^[-{\d<>&]", remainder):
                    continue
                moved = self._standard_descriptors(remainder)
                if not moved:
                    continue
                if (name, line.strip()) in self.DELIBERATE:
                    allowed.append(f"{name}:{number}")
                    matched.add((name, line.strip()))
                    continue
                offenders.append(f"{name}:{number}: {line.strip()}  -> {moved}")
        self.assertEqual(
            [],
            offenders,
            "a bare `exec` carrying a redirect on fd 0, 1 or 2 changes the "
            "shell permanently. Either write the redirection on a group -- "
            "`{ exec {FD}<&-; } 2>/dev/null` -- or add the line to "
            "DELIBERATE with a reason:\n" + "\n".join(offenders),
        )
        # The allowance is a list of exact lines; a rename or a rewrite must
        # come back through review rather than silently stop being covered.
        # Compared as a SET, not a count: one allowed line can legitimately
        # appear more than once in a file -- `sk_bind_handoff_tty` and
        # `sk_handoff_guard_arm` both bind fd 2 with the identical statement --
        # and counting made a second honest occurrence look like a missing one.
        self.assertEqual(
            self.DELIBERATE,
            matched,
            "an allowed bare-exec redirect no longer exists as written: "
            f"expected {sorted(self.DELIBERATE)}, matched {sorted(matched)} "
            f"at {allowed}",
        )


if __name__ == "__main__":
    unittest.main()
