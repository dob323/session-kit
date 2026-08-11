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
installed Codex theme files, the user-level self-name instructions, Claude title-hook
coverage, active kill switches, and the private
`release-acceptance.json` record. These migration checks are warnings: they do
not alter provider state or make an otherwise healthy installation fail.

These checks report the running installation rather than the files on disk, so
they can fail an installation that installed cleanly:

| Check | Reports | Fix it with |
| --- | --- | --- |
| `shpool-version` | the installed shpool against the 0.11.0 release the kit is tested and patched against | `cargo install shpool --version 0.11.0 --locked` |
| `watchdog` | whether `session-kit-watchdog.service` is enabled and running, not only installed | `session-kit services enable` |
| `units` | the enabled and active state of each Session Kit unit; the socket-activated and timer-activated units are reported without being required | `session-kit services enable` |
| `linger` | whether logind keeps your user manager alive after logout | `loginctl enable-linger "$USER"` |
| `hook-files` | whether each registered intake hook command resolves to a runnable file | `session-kit update` |

`session-kit doctor --authority [--json] [--days N] [--session PROVIDER:UUID]`
is a separate, read-only report on source authority: how many recorded events
reach each authority tier, which sessions have a provider transcript on this
machine that their events never recorded, and which stored operator intakes
would pass or be refused today. It changes nothing and gates nothing; tier
numbers only accumulate for events captured after the evidence roots landed,
so an existing install reports its pre-existing history as tier 0
permanently — those events recorded no transcript anchor and the ledger is
append-only — while a fresh install simply reports no events at all.

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
versions, date, and all three evidence fields match the current installation.

## `kit` does not open

The dashboard is opened on demand with `kit`. Login integration adds the
commands and managed Bash integration; it does not open the dashboard
automatically.

Confirm that:

- `$HOME/.local/bin/kit` is executable and on `PATH`;
- standard input and output are terminals;
- the guarded Session Kit block exists in the relevant shell startup file;
- `session-kit doctor` reports the current release and helper launchers.

On Linux, login integration manages `.bashrc`. On macOS, it manages `.bashrc`
and `.bash_profile` for Bash and adds only the command path to `.zshrc`. It does
not replace zsh or make the Bash dashboard behavior automatic in zsh.

`session-kit disable-login` removes the managed startup-file integration
without uninstalling the commands.

## Color is hard to read

Disable it:

```bash
export SESSION_KIT_NO_COLOR=1
```

`NO_COLOR` is also supported. The text labels and symbols carry the full
meaning without color.

## Inventory is unavailable or cached

Live actions require:

- a responding shpool daemon;
- valid `shpool list --json`;
- readable Linux `/proc` or native macOS process evidence;
- Python 3.10 or newer on Linux, or Python 3.11 or newer on macOS;
- exact provider metadata for provider-specific actions.

Cached inventory is display-only. Kill, move, repair, and recovery actions stay
disabled until a fresh guard succeeds.

If a manager probe times out, do not repeatedly attach or kill. Preserve
owner-only logs and inspect the daemon first.

## macOS services are not running

Install, update, and rollback generate LaunchAgent templates but do not load
them. A signed-in desktop user session is required so the GUI launchd domain is
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

After an update or rollback, a loaded job can have a pending definition. At a
safe point with no managed sessions, apply it with:

```bash
session-kit services disable
session-kit services enable
session-kit services status
```

Disable refuses to unload shpool while sessions exist or their absence cannot
be proved.

## Reply alert is missing

Session Kit uses structured provider events, not prose. Check `sp detail` for
the session and confirm the provider version is within the tested release
matrix.

For Codex, only a live unresolved `request_user_input` without automatic
resolution needs a reply. Completed, aborted, superseded, malformed, or
optional questions do not show the alert.

## New Codex session says setup is incomplete

Codex may not create its root rollout until the first user message. Session Kit
first proves the exact process tree, then binds the UUID when it appears.

If instructed:

```text
sp verify-start <terminal-number|shpool-id>
```

Do not delete paired launch records.

## Session is open elsewhere

Selecting the row shows a move action. You can also run:

```text
sp takeover <terminal-number|shpool-id>
```

Moving displays the exact target, revalidates it, and disconnects the earlier
terminal view without an additional prompt. It does not create a second
provider process.

## Provider exited

A normal Claude Code or Codex exit should leave the managed terminal alive and
label it as provider exited. Open that row to reach the provider-exit menu,
where you can reopen the exact conversation, keep the terminal, open an
ordinary shell, or close the terminal.

If the entire row disappears, collect a sanitized `sp health`, `sp detail`, and
relevant owner-only event timestamps. Do not publish UUIDs or terminal output.

## `k` changes nothing

The kill shortcut accepts visible numbers, comma-separated lists, and ranges
from a fresh dashboard. It refuses cached state, an invalid or hidden number,
changed identity, and unsafe proof files.

Every requested display number is checked before the batch starts. Session Kit
then creates one frozen proof per item, displays the title, provider, and exact
ID as the safety notice, and revalidates it immediately before close. There is
no additional prompt. A refusal leaves that item unchanged, but it does not
undo an earlier close in the same batch. The dashboard refreshes after the
batch.

## Quiet session or repair refusal

Quiet output does not prove failure. A provider can be working without recent
terminal output.

The watchdog runs in report-only mode by default on both platforms. Automatic
watchdog repair is Linux-only; macOS explicitly refuses repair mode because the
required Linux daemon-thread evidence is unavailable. Manual `sp repair` still
requires direct evidence of an unrecoverable handoff failure and an exact
provider conversation.

For automatic decisions, an old journal line is not enough. The watchdog
requires a named handoff failure from the current daemon generation and ignores
it when a later successful attach exists. Unknown recent-output age makes the
condition report-only. A global serving-thread count mismatch warns about the
manager but is never assigned to one session and never triggers a repair.

## Automatic cleanup did not run

This is usually a safe refusal. A provider-exited terminal must remain detached
and in the same exact safe state for 72 continuous hours after the cleanup
timer is enabled. Any live provider, child process, reply, attachment, recovery
conflict, generation change, or missing evidence blocks cleanup.

Confirm that the Linux reaper timer or macOS reaper LaunchAgent is active with
`session-kit services status`. Manual inspection remains available through `sp
detail` and `sp prune`.

The guarded 72-hour close and manual prune work on Linux and macOS. Temporary
picker-file expiry and stale immutable-release garbage collection are currently
Linux-only.

## Terminal cannot be opened

If the daemon log for the named session contains the full heartbeat chain
`joining heartbeat_h`, `waiting for heartbeat ack`, and a receive timeout, the
optional `0001` patch prevents that delayed ack from destroying the session's
terminal-serving thread. Rebuilding and restarting the daemon is required;
repeatedly opening the already-wedged terminal cannot repair its lost thread.

For direct shpool handoff failure evidence:

```text
sp repair <terminal-number|shpool-id>
```

Repair ends the unreachable managed terminal and resumes the exact provider
conversation in a new one. It refuses attached, ambiguous, or UUID-less
targets.

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
