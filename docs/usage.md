# Use Session Kit

## The picker

With shell integration enabled, SSH opens an ordinary shell. Type `kit` to open
the picker. Linux Bash may print a short `kit: open sessions` hint. On macOS,
zsh receives the command path but does not start the picker or replace the
shell.

```text
number          open a ready session or inspect one open elsewhere
n               create a Claude Code, Codex, or shell session
k <numbers>     close displayed sessions: k 5 · k 5, 6, 8 · k 4-7
x <numbers>     compatibility alias for k
/text           search names, providers, projects, and exact IDs
r               refresh live state and clear search
m               more: outside roots, recovery, and projects
o               show provider roots outside shpool, read-only
u               review exact recovery records
p               add, review, or drop projects (under m)
name <number>   set a local provider title
name reset #    remove the local title
fork <number>   create an independent provider conversation
next / prev     change page
?               show help
Enter           return to a regular terminal
```

Selecting a session that is already open elsewhere shows a move menu. Session
Kit never moves it without a separate, explicit choice.

Normal rows omit shpool IDs and provider UUIDs. Use `sp detail`, JSON output, or
an explicit search when you need exact identity.

### Why the screen sometimes clears while you type

An idle picker keeps itself current. It collects a snapshot in the background
every few seconds and repaints only when something visible changes, so it never
runs in front of your prompt. Terminal numbers are stable, so a repaint never
changes what a number means, and the active search and page survive one.

A repaint clears the screen. Characters you typed without pressing Enter live in
the terminal's own input queue, which this process cannot inspect: in canonical
mode the kernel reports an incomplete line as no input at all, on both Linux and
macOS. So a repaint landing mid-command erases the *echo* of what you typed
while the characters themselves stay queued and intact. Press Enter and the
command you started still runs.

Set `SESSION_KIT_PICKER_REFRESH_SECONDS=0` to switch this off, or to a number of
seconds (minimum 2) to change the pace.

## Start a session

```text
sp new claude [project-alias]
sp new codex [project-alias]
sp new shell [project-alias]
sp new <project-alias>
```

The last form uses the provider stored for that project alias.

A terminal number stays with the session for the current host boot. It is a
display convenience, not a durable provider identity.

Managed terminals use modern Bash on macOS even when the account's normal SSH
shell is zsh.

A new Claude session starts with an explicit UUID, so native macOS process
evidence can bind it immediately. A new Codex TUI has no conversation UUID until
its first message; before then the picker labels it `Codex started, no messages
yet`. Opening and closing that terminal work normally, while recovery, fork,
name, and color actions wait for Codex to publish its thread identity, because
each depends on that UUID. With the optional Codex App Server integration
active, Session Kit also accepts an exact thread rollout held open by the
managed app-server process, and refuses the association when that server has
zero or multiple candidate threads.

## Find and inspect

```text
sp list
sp detail <terminal-number|shpool-id>
sp find <text>
sp health
```

Provider roots outside shpool appear in a separate read-only view. They have no
terminal number and cannot be opened through Session Kit.

## Open or move

```text
sp go <terminal-number|shpool-id>
sp takeover <terminal-number|shpool-id>
```

`sp go` opens a detached exact managed session. `sp takeover` moves an attached
session to the current terminal after a fresh identity check; the earlier window
returns to its picker, and the provider conversation is not duplicated.

## Names

```text
sp name <terminal-number|shpool-id> <title>
sp name reset <terminal-number|shpool-id>
```

Title priority is:

1. local manual name;
2. explicit provider rename;
3. retained automatic title;
4. deterministic provider and project description;
5. shortened identity fallback;
6. shell command or `Idle shell`.

Reset removes only the local manual name.

## Session colors

Every Claude Code and Codex session carries its own color. It appears on the
picker row and in the session's own window, so a glance tells you which terminal
you are looking at. Shell sessions have no provider color.

A conversation's color is derived from its identity, so it is the same every
time you return to it. If another live session of the same provider already
holds that color, the newcomer takes the next free one instead, which is what
keeps two rows on screen from looking alike. When every color in a provider's
palette is already in use, colors begin to repeat.

The two providers draw from separate palettes, so a Claude session and a Codex
session can never share a color:

- Claude sessions use red, blue, green, yellow, purple, orange, pink, and cyan.
  That set is fixed by Claude Code itself, which accepts those eight names and
  no others.
- Codex sessions use lime, magenta, silver, sand, sky, and sea. Codex reads a
  theme file from disk and applies no allow-list, so Session Kit ships one theme
  per color.

This is separate from the semantic coloring of the dashboard, where color marks
provider, availability, attention, danger, and secondary status. Text labels
always carry the same meaning, so nothing depends on color being available.

## Pending titles

Two providers cannot repaint their own title from outside their process, so
Session Kit marks the row `title pending` rather than showing something untrue.

**Codex status bar.** A new Codex provider may start before its thread name
exists, and it keeps the initial conversation ID in its status bar until that
provider restarts. Opening a detached session can refresh a proven-idle provider
automatically. An attached provider is never restarted automatically: open its
action menu and choose the pending-title action, which is refused unless the
exact provider generation is idle, has no subagents, has a real stored title,
and still matches a fresh proof. The shell and the conversation stay the same.
For App Server sessions the refresh restarts only the generation-bound remote
TUI, leaving the server, Unix socket, exact thread, and managed shell running.

Codex saves a renamed thread immediately, so this maintenance state stays out of
normal dashboard rows: the stored title is already correct. Completed Codex
subagent threads remain in detail output but are not counted as active subagents
in summary rows.

**Claude prompt box.** Claude stores its conversation auto-title separately from
the name and color shown in the prompt box. Session Kit fills absent native name
and color records before each human-facing inventory, and never replaces an
explicit `/rename` or `/color` choice. Claude does not repaint a running prompt
box after another process updates its records, so the dashboard keeps `title
pending` on that live generation. The stored title and color appear when the
exact conversation next starts, including the normal `r` reopen path after
Claude exits. New Claude sessions bootstrap their stable color before the first
visible frame.

## When a provider exits

When Claude Code or Codex exits normally, the managed shpool terminal stays
alive and the dashboard labels it as provider exited. Leaving a conversation
with `/exit` closes that terminal and returns you to the picker. A provider that
exits non-zero has crashed, so it stops at a menu instead:

```text
r  reopen the exact conversation
k  keep the terminal and disable automatic cleanup
s  open an ordinary shell and permanently exclude the terminal from cleanup
c  close the terminal
```

Touch `~/.sk_keep_exit_menu` to get that menu on every exit.

Do not use a generic latest-conversation command to come back. If exact identity
cannot be proved, reopen is disabled rather than guessed.

From an ordinary managed shell, `keep_session` disables automatic cleanup,
`unkeep_session` allows it to resume once all safety conditions are met, and
`bye` closes the terminal after an exact-ID check. A directly typed Bash `exit`
keeps its normal shell behavior.

## Close a session

From the picker, `k <number>`. From a shell:

```text
sp close <terminal-number|shpool-id>
```

Before closing, Session Kit refreshes live evidence and resolves every number to
one exact shpool ID. Cached inventory, changed generations, hidden numbers, or
ambiguous identity disables the whole action rather than part of it. Once that
proof succeeds, no per-session confirmation prompt is required.

Closing ends the managed terminal. It does not delete provider-native
conversation history or optional journal files.

## Automatic cleanup

The scheduled observer considers only disconnected provider-exited terminals,
and automatic close begins only after the cleanup timer is enabled. A candidate
then needs 72 continuous hours with every exact safety predicate unchanged.

Attachment, a live provider, child work, a pending reply, a recovery conflict,
an identity change, missing evidence, or a new terminal generation resets or
blocks eligibility.

`sp prune` runs fresh checks for a manual cleanup.

## History and recovery

```text
sp history <terminal-number|shpool-id>
sp recover
```

History requires journals to have been explicitly enabled for that session.

Recovery uses an exact Claude Code or Codex UUID and never falls back to
recency, directory, or "latest". An active UUID cannot be resumed into a second
writable terminal: open the existing terminal, or create an explicit fork.

## Repair

```text
sp repair <terminal-number|shpool-id>
```

Repair is reserved for direct evidence of an unrecoverable shpool handoff
failure. Silence alone does not qualify.

The installed watchdog reports evidence and does not repair in its default mode.
Its advanced repair mode is a separate Linux-only opt-in that carries
terminal-state risk. The macOS watchdog is report-only.

If *every* session becomes unreachable at once rather than one, that is a
different problem and repair will not help. See
[Troubleshoot](troubleshooting.md).

## Automation

Noninteractive close requires an explicit opt-in and the exact shpool ID:

```bash
SESSION_KIT_NONINTERACTIVE=1 \
SESSION_KIT_CONFIRM_ID=<exact-shpool-id> \
sp close <exact-shpool-id>
```

Never automate moves, kills, repairs, or cleanup using display numbers. They are
stable within a boot, but they describe what is on screen, not which
conversation you mean.
