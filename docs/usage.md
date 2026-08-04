# Use Session Kit

## Session picker

When shell integration is enabled, SSH opens a regular shell. Type `kit` to
open the picker. Linux Bash may print a short `kit: open sessions` hint. On
macOS, zsh receives the command path but does not start the picker or replace
the shell:

```text
number          open a ready session or inspect one open elsewhere
n               create a Claude Code, Codex, or shell session
k <numbers>     close displayed sessions: k 5 · k 5, 6, 8 · k 4-7
x <numbers>     compatibility alias for k
/text           search names, providers, projects, and exact IDs
r               refresh live state and clear search
o               show provider roots outside shpool, read-only
u               review exact recovery records
name <number>   set a local provider title
name reset #    remove the local title
fork <number>   create an independent provider conversation
next / prev     change page
?               show help
Enter           return to a regular terminal
```

Selecting a session already open elsewhere shows a move menu. Session Kit
never moves it without a separate, explicit choice.

Normal rows omit shpool IDs and provider UUIDs. Use `sp detail`, JSON, or an
explicit search for exact identity. Colors identify provider, availability,
attention, danger, and secondary status; text labels preserve the same meaning
when color is disabled.

## Start

```text
sp new claude [project-alias]
sp new codex [project-alias]
sp new shell [project-alias]
sp new <project-alias>
```

The last form uses the provider stored for the project alias.

A terminal number stays with the session for the current host boot. It is not a
durable provider identity.

Managed terminals use modern Bash on macOS even when the account's normal SSH
shell is zsh. A new Claude session starts with an explicit UUID so native macOS
process evidence can bind it immediately. A new Codex TUI has no conversation
UUID until its first message. Before that message, the picker labels it
`Codex started, no messages yet`; exact terminal open and close remain
available, while UUID-dependent recovery, fork, name, and color actions wait
for Codex to publish its thread identity. When the optional Codex App Server
integration is active, Session Kit also accepts an exact thread rollout held
open by that managed app-server process. It refuses the association when the
server has zero or multiple candidate threads.

Codex saves a renamed thread immediately, but an already-running TUI cannot
repaint its own status bar from outside the process. Session Kit defers that
bar refresh while the exact session is attached or working and offers the
proof-bound refresh only after it is detached and idle. This maintenance state
stays out of normal dashboard rows because the saved title is already correct.
Completed Codex subagent threads remain available in detail output but are not
counted as active subagents in summary rows.

## List, inspect, and search

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
session to the current terminal after a fresh identity check.
The earlier window returns to its picker; the provider conversation is not
duplicated.

## Pending Codex bar title

A new Codex provider may start before its thread name exists. The process keeps
the initial conversation ID in its status bar until that provider is restarted,
so the dashboard marks the row `title pending`.

Opening a detached session can refresh a proven-idle provider automatically.
Session Kit never restarts an attached provider automatically. For an attached
row, open its action menu and choose the pending-title action. The action is
refused unless the exact provider generation is idle, has no subagents, has a
real stored title, and still matches a fresh proof. The shell and conversation
remain the same.

## Pending Claude prompt title

Claude stores its conversation auto-title separately from the name and color
shown in the prompt box. Session Kit fills absent native name and color records
before each human-facing inventory. It never replaces an explicit `/rename` or
`/color` choice.

Claude does not repaint a running prompt box after another process updates its
records. The dashboard therefore keeps `title pending` on that live generation.
The stored title and color appear when the exact conversation next starts,
including the normal `r` reopen path after Claude exits. New Claude sessions
bootstrap their stable color before the first visible frame.

## Provider exited

When Claude Code or Codex exits normally, the managed shpool terminal stays
alive. The dashboard labels it as provider exited.

Open the row to reach this menu:

```text
r  reopen the exact conversation
k  keep the terminal and disable automatic cleanup
s  open an ordinary shell and permanently exclude the terminal from cleanup
c  close the terminal
```

Do not use a generic latest-conversation command. If exact identity cannot be
proved, reopen is disabled. From an ordinary managed shell, `keep_session`
disables automatic cleanup, `unkeep_session` allows it to resume after all
safety conditions are met, and `bye` closes the terminal after an exact-ID
check. A directly typed Bash `exit` keeps its normal shell behavior.

## Name

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

## Kill

From the picker:

```text
k <number>
```

From a shell:

```text
sp close <terminal-number|shpool-id>
```

Before killing, Session Kit refreshes live evidence and resolves every number
to one exact shpool ID. Cached inventory, changed generations, hidden numbers,
or ambiguous identity disables the whole action. No per-session confirmation
prompt is required after that proof succeeds.

Killing ends the managed terminal. It does not delete provider-native
conversation history or optional journal files.

## Cleanup

The scheduled observer considers only disconnected provider-exited terminals.
Automatic close begins only after the cleanup timer is enabled. A candidate
then needs 72 continuous hours with every exact safety predicate unchanged.

Attachment, a live provider, child work, pending reply, recovery conflict,
identity change, missing evidence, or a new terminal generation resets or
blocks eligibility.

`sp prune` performs fresh checks for a manual cleanup.

## History and recovery

```text
sp history <terminal-number|shpool-id>
sp recover
```

History requires journals to have been explicitly enabled for that session.
Recovery uses an exact Claude Code or Codex UUID and never falls back to
recency, directory, or “latest.”

An active UUID cannot be resumed into a second writable terminal. Open the
existing terminal or create an explicit fork.

## Repair

```text
sp repair <terminal-number|shpool-id>
```

Repair is reserved for direct evidence of an unrecoverable shpool handoff
failure. The installed watchdog reports evidence and does not repair in its
default mode. Its advanced repair mode is a separate Linux-only opt-in with
terminal-state risk. The macOS watchdog remains report-only. Silence alone does
not qualify.

## Noninteractive close authorization

Noninteractive close requires an explicit opt-in and the exact shpool ID:

```bash
SESSION_KIT_NONINTERACTIVE=1 \
SESSION_KIT_CONFIRM_ID=<exact-shpool-id> \
sp close <exact-shpool-id>
```

Do not automate moves, kills, repairs, or cleanup with display numbers.
