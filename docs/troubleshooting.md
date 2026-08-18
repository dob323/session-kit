# Troubleshooting

Start with read-only checks:

```text
session-kit doctor
sp health
sp list
```

Before sharing output, remove titles, UUIDs, working directories, process
details, service logs, and journal content.

`session-kit doctor` also audits provider version output, all fourteen
installed Codex theme files, the user-level self-name instructions, provider
title integration, active kill switches, internal provider formats, journal
growth, and the private `release-acceptance.json` record. It never alters
provider state. Most migration findings are warnings, but an internal format
that is known to be incompatible is a failure because provider actions would
be unsafe.

These checks report the running installation rather than the files on disk, so
they can fail an installation that installed cleanly:

| Check | Reports | Fix it with |
| --- | --- | --- |
| `shpool-version` | the installed shpool against the 0.11.0 release the kit is tested and patched against | `cargo install shpool --version 0.11.0 --locked` |
| `watchdog` | whether `session-kit-watchdog.service` is enabled and running, not only installed | `session-kit services enable` |
| `units` | the enabled and active state of each Session Kit unit; the socket-activated and timer-activated units are reported without being required | `session-kit services enable` |
| `units-loaded` | whether the service manager has loaded the definitions currently installed on disk | run `session-kit update` or `session-kit rollback` again; if the named definition is still pending, apply it at a safe service restart |
| `release-running` | whether the running watchdog uses the selected release | run `session-kit update` or `session-kit rollback` again to refresh the watchdog |
| `linger` | whether logind keeps your user manager alive after logout | `loginctl enable-linger "$USER"` |
| `tab-title` | whether Claude and Codex have the installed title integrations | rerun `session-kit update`, then inspect the named provider file before forcing an unrelated setting |
| `journals` | owner-private recording size and whether recording is on | review the reported directory; the kit measures it but does not age active recordings automatically |
| `transcripts` | whether every provider session in the last recorded inventory snapshot still resolves to a transcript on this machine, searching the provider default root, a configured `CLAUDE_CONFIG_DIR`, and every account profile the kit created | name the flagged session and read its logs; a session that has not written a turn yet resolves on its next one |

`watchdog`, `units`, and `linger` read live state only when the systemd user
manager answers on its own socket. When it is reachable only through the
local-machine transport, doctor reports them as unchecked instead of guessing.
Kill-switch findings print supported variable or sentinel names only, never
their values.

The owner-only acceptance record uses this exact schema. Provider values are
the exact recognized `--version` output, or `null` when that provider is not
installed. Evidence values are short local references or notes, not secrets.

```json
{
  "schema_version": 1,
  "release_id": "40-character-release-commit",
  "platform": "linux",
  "provider_versions": {
    "claude": "2.1.221 (Claude Code)",
    "codex": "codex-cli 0.145.0"
  },
  "accepted_on": "2026-08-04",
  "evidence": {
    "unique_colors": "local acceptance step or evidence reference",
    "thread_titles": "local acceptance step or evidence reference",
    "resume_roundtrip": "local acceptance step or evidence reference"
  }
}
```

Doctor accepts the record only when its release, platform, installed provider
versions, non-future ISO date, and all three evidence fields match the current
installation.

## `kit` does not open

The picker is opened on demand with `kit`. Login integration adds the
commands and managed Bash integration; it does not open the picker
automatically.

Confirm that:

- `$HOME/.local/bin/kit` is executable and on `PATH`;
- standard input and output are terminals;
- the guarded Session Kit block exists in the relevant shell startup file;
- `session-kit doctor` reports the current release and helper launchers.

On Linux, login integration manages `.bashrc`. On macOS, it manages `.bashrc`
and `.bash_profile` for Bash and adds only the command path to `.zshrc`. It does
not replace zsh or make the Bash picker behavior automatic in zsh.

`session-kit disable-login` removes the managed startup-file integration
without uninstalling the commands.

## Which picker `kit` opens

The installed `kit` executable opens the key-driven picker. In an interactive
Bash shell with Session Kit's managed integration loaded, `kit` is a shell
function that goes through the screen launcher. Only that Bash function honors
`SESSION_KIT_TUI=on` or a `tui-on` file in the state directory. Directly
running `~/.local/bin/kit`, using zsh, or using another shell opens the
key-driven picker.

For managed Bash, turn the interactive screen on or off with:

```bash
touch ~/.local/state/session-kit/tui-on    # turn the interactive screen on
rm ~/.local/state/session-kit/tui-on      # back to the key-driven picker
```

Where it is on, it still has an automatic fallback: if it ends any way other
than you leaving it deliberately, it cannot draw on this terminal, the Python
it needs is missing, or it fails mid-screen, that `kit` invocation is handed
to the key-driven picker. Login integration opens a normal shell; it does not
open either picker automatically.

Every fall back is recorded, one line each:

```bash
cat ~/.local/state/session-kit/tui-fallback.log
```

Each line is the time, the exit status the screen ended with, and the program
that ended. Nothing else changes: the same sessions, the same commands, the
same actions, driven with the older screen's letter keys.

To point the managed Bash launcher at a different program for either screen,
export the override before running `kit`:

```bash
export SESSION_KIT_PICKER_FALLBACK_CMD=~/.local/bin/shpool_login
export SESSION_KIT_TUI_CMD=~/.local/bin/shpool_login_tui
export SESSION_KIT_TUI_FALLBACK_LOG=~/somewhere/else.log
```

An empty log after using the interactive screen means no fallback was recorded
during those invocations.

## Color is hard to read

Disable it:

```bash
export SESSION_KIT_NO_COLOR=1
```

`NO_COLOR` is also supported. The text labels and symbols carry the full
meaning without color.

## Terminal titles or colors are missing

Run `session-kit doctor` and read its `tab-title` result. It distinguishes a
disabled title switch, a missing or malformed Codex title template, unsupported
Codex title items, a missing thread-title item, and Claude title integration
that is absent or disabled. Rerun the installer for missing kit-owned files;
do not overwrite an unrelated provider setting without reviewing it.

The picker uses terminal color when available and remains complete in plain
text. `NO_COLOR` or `SESSION_KIT_NO_COLOR` disables color intentionally.
Ghostty supports the kit's titles and colors in its stock configuration; no
Ghostty display override is required. See [Display setup](usage.md#display-setup)
for the Claude status line, Codex status bar, terminal titles, and session
colors.

## Inventory is unavailable or cached

Live actions require:

- a responding shpool daemon;
- valid `shpool list --json`;
- readable Linux `/proc` or native macOS process evidence;
- Python 3.10 or newer on Linux, or Python 3.11 or newer on macOS;
- exact provider metadata for provider-specific actions.

Cached inventory is display-only. Close, move, repair, and recovery actions
stay disabled until a fresh guard succeeds.

If a manager probe times out, do not repeatedly open or close sessions.
Preserve owner-only logs and inspect the daemon first.

## A session record with no shell ("Unavailable")

The session manager can keep listing a session whose shell process is gone: a
close that hit an already-dead process aborts its own bookkeeping and leaves
the entry behind. Session Kit quarantines such an entry -- no session number,
never drawn in the picker, and refused by every `sp` verb, because there is no
shell to open, close, or repair. `sp help unavailable` explains the state.

They clear without intervention: the cleanup timer closes a record whose shell
has been provably absent for the full observation window (72 hours by default),
and restarting the session manager clears every stale entry at once -- which
also ends every live session, so it is not a routine step.

The optional shpool patch `0003` removes this kind of dead-shell record at the
manager layer instead of waiting for Session Kit's cleanup window.

To clear one immediately, use the session manager directly, outside Session
Kit:

```bash
shpool list
shpool kill <name>
```

Check `sp list` first: anything Session Kit still shows with a session number
is a live session, and `shpool kill` would end it for real.

## macOS services are not running

The first install generates LaunchAgent templates but leaves them unloaded. A
signed-in desktop user session is required so the GUI launchd domain is
available. Then run:

```bash
session-kit services enable
session-kit services status
```

Enable refuses to start a second daemon when an unmanaged shpool daemon already
responds or cannot be safely ruled out. If only some jobs load, `services
status` reports the partial state. Jobs loaded before the failure can remain
running. Do not keep retrying enable without inspecting the reported labels and
owner-only logs.

Update and rollback kickstart an already loaded watchdog so it uses the
selected release. They do not restart shpool or the reaper. A loaded shpool or
reaper job can therefore have a pending definition. At a safe point with no
managed sessions, apply it with:

```bash
session-kit services disable
session-kit services enable
session-kit services status
```

Disable refuses to unload shpool while sessions exist or their absence cannot
be proved.

## New Codex session says setup is incomplete

Codex may not create its root rollout until the first user message. Session Kit
first proves the exact process tree, then binds the UUID when it appears.

If instructed:

```text
sp verify-start <session-number|shpool-id>
```

Do not delete paired launch records.

## Session is open elsewhere

Selecting the row shows a move action. You can also run:

```text
sp takeover <session-number|shpool-id>
```

Moving displays the exact target, revalidates it, and disconnects the earlier
terminal view without an additional prompt. It does not create a second
provider process.

## Provider exited

A clean Claude or Codex exit closes the session and lands you at the picker.
The conversation is in Closed sessions and one Restore brings it back.

A crash is different. The conversation reopens itself once, with a one-line
notice saying so. A second crash within a minute stops the loop, so a
conversation that cannot start cannot spin. The session then closes into Closed
sessions if its conversation can be brought back, and stays open, saying why, if it cannot. There is no menu to answer.

A reopen that cannot run names what was missing, including an inexact live
inventory, a missing row, a changed shell generation, or a session the
inventory no longer records as provider exited, and hands the window back in
that same step. It
never closes the session: one of those reasons is that a provider is still
running in the terminal, and a refusal is not evidence that anything finished.

If the entire row disappears, collect a sanitized `sp health` and `sp detail`.
Do not publish UUIDs or terminal output.

## Close does nothing

Marking accepts the numbers on screen, comma-separated lists, and ranges, from
a fresh picker. Close refuses cached state, an invalid or hidden number,
changed identity, and unsafe proof files.

Every marked number is checked before the batch starts. Session Kit freezes one
proof per session and revalidates it immediately before the close. A refusal
leaves that session unchanged and says so; it does not undo a close that
already happened in the same batch. The list refreshes afterwards.

Nothing is confirmed first, because nothing has to be: every deliberate close
is written to the ledger behind the Closed sessions row, and restoring from
there brings the exact conversation back.

## Quiet session or repair refusal

Quiet output does not prove failure. A provider can be working without recent
terminal output.

The watchdog runs in report-only mode by default on both platforms. Automatic
watchdog repair is Linux-only; macOS explicitly refuses repair mode because the
required Linux daemon-thread evidence is unavailable. Manual `sp repair` is a
deliberate operator action: it still requires an exact, detached, unreachable
session with a recoverable provider conversation, but it does not require the
watchdog's recorded handoff-failure evidence.

For automatic decisions, an old journal line is not enough. The watchdog
requires a named handoff failure from the current daemon generation and ignores
it when a later successful attach exists. Unknown recent-output age makes the
condition report-only. A global serving-thread count mismatch warns about the
manager but is never assigned to one session and never triggers a repair.

## Automatic cleanup did not run

This is usually a safe refusal. A provider-exited terminal must remain detached
and in the same exact safe state for 72 continuous hours after the cleanup
timer is enabled. Any live provider, child process, attachment, recovery
conflict, generation change, or missing evidence blocks cleanup.

Confirm that the Linux reaper timer or macOS reaper LaunchAgent is active with
`session-kit services status`. Manual inspection remains available through `sp
detail` and `sp prune`.

The guarded 72-hour close, manual prune, and stale app-server-directory cleanup
work on Linux and macOS. Temporary picker-file expiry and stale
immutable-release garbage collection are currently Linux-only.

## Terminal cannot be opened

If the daemon log for the named session contains the full heartbeat chain
`joining heartbeat_h`, `waiting for heartbeat ack`, and a receive timeout, the
optional `0001` patch prevents that delayed ack from destroying the session's
terminal-serving thread. Rebuilding and restarting the daemon is required;
repeatedly opening the already-wedged terminal cannot repair its lost thread.

For a deliberate repair after you have established that the exact detached
session is unreachable:

```text
sp repair <session-number|shpool-id>
```

Repair ends the unreachable session and resumes the exact provider conversation
in a new one. It refuses attached, ambiguous, or UUID-less targets.

## The window echoes what you type and the session does not see it

You open a session and the window goes deaf. What you type appears as literal
text over the application's first screen, arrow keys show as `^[[B`, Ctrl-L
shows as `^L`, Ctrl-L and Esc do nothing, and nothing reaches the session.

**Get the window back.** From a second window, find the deaf window's terminal
and reset it:

```text
stty -F /dev/pts/<N> raw -echo
```

The next key you press goes to the session. Nothing is lost, and the session was
never in trouble: it is the terminal's input mode that is wrong, not the
session. Run `tty` inside the deaf window to read `<N>`, or
`ps -o tty= -p <pid>` from outside.

**Why it happens, and why it should not any more.**

The session's client refuses to touch the terminal at all if any one of its
three standard channels is not a terminal, silently, and it goes on shuffling
bytes as if nothing were wrong. So the window keeps the ordinary line-typing
mode it had, and a full-screen application never gets the keys.

One picker bug used to hand it exactly that: closing the live-updates
descriptor also pointed the picker's error channel at nowhere, permanently, for
the rest of that window's life. Every session opened from that window
afterwards came up deaf. That is fixed at its source.

Two further guards are in place regardless of where a bad channel comes from:

- Before handing a session over, the client is given a terminal on all three
  channels, so it can never silently decline again.
- If a session ends leaving the window unusable, your input modes are put back
  and what was found is recorded in
  `~/.local/state/session-kit/handoff-repair.log`.

**If it happens anyway**, that is new and worth capturing:

1. Note the terminal (`/dev/pts/<N>`) and the time.
2. Recover with the `stty` line above.
3. Keep `~/.local/state/session-kit/action-events.jsonl` and say when it
   happened.

The optional shpool patch `0002` also restores the terminal input mode in the
client itself. It is useful when shpool is used outside Session Kit's guarded
handoff path.

## Every session is unreachable at once

The daemon is alive and still accepting connections, but every `sp` command,
attach, and detach blocks forever. Nothing times out and it does not recover on
its own.

This is not Session Kit state, and no Session Kit command can clear it: the
commands block for the same reason everything else does. `sp repair` will not
help.

The cause is a detach deadlock in shpool 0.11.0. Upstream `handle_detach` holds
the global session-table lock across an unbounded send and receive on two
rendezvous channels, so a single client whose socket has stopped draining parks
the lock for every session. One stalled SSH window is enough.

The fix is the optional `0004` patch, which requires rebuilding the shpool
binary and restarting the daemon. Read
[the patch notes](../shpool-patch/README.md) first.

After any daemon restart, conversations are not lost. Open a new terminal, start
the picker, and press `u` to review exact conversation recovery; opening that
view changes nothing on its own. An inventory that looks empty immediately after
a restart is a stale read, not deleted work.

## Attach exits with the wrong status or resize bursts damage scrollback

These are shpool-layer issues rather than Session Kit state:

- Optional patch `0005` makes the attach client return the remote command's
  exit status instead of reporting success after a failed command.
- Optional patch `0006` coalesces a burst of terminal resize events for 120
  milliseconds, applies the final size, and skips a duplicate size. This
  avoids repeated redraws that can leave noisy scrollback.

Both require rebuilding shpool and restarting its daemon. Read
[the patch notes](../shpool-patch/README.md), and plan that restart separately
because it ends the currently managed session processes.

## Changed shpool binary report

A package or Cargo update may have replaced the binary used by the running
daemon. This is easy to miss, because a rebuilt shpool silently discards any
patch you had applied, and the failure only appears later as the deadlock
above. Do not restart the daemon as a diagnostic step. Compare the running
binary, prior binary, active sessions, and optional patch choice first.

## Report a problem

Use the issue template for sanitized behavior reports. Use the private process
in [Security policy](../SECURITY.md) for any vulnerability or suspected data
exposure.
