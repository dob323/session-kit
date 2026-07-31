# Use Session Kit

## SSH picker

When enabled, the picker opens in an interactive Bash login:

```text
number          open a ready session or inspect one open elsewhere
n               create a Claude Code, Codex, or shell session
k <number>      kill the exact displayed session after confirmation
x <number>      compatibility alias for k
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

Selecting a session already open elsewhere shows a move menu. Session Kit never
moves it without a separate choice and confirmation.

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
session to the current terminal after a fresh identity check and confirmation.
The earlier window returns to its picker; the provider conversation is not
duplicated.

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
safety conditions are met, and `bye` closes the terminal only after exact-ID
confirmation. A directly typed Bash `exit` keeps its normal shell behavior.

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

Before killing, Session Kit refreshes live evidence, resolves the number to one
exact shpool ID, displays the title, provider, and exact ID, and requires
confirmation. Cached inventory, changed generations, or ambiguous identity
disables the action.

Killing ends the managed terminal. It does not delete provider-native
conversation history or optional journal files.

## Cleanup

The scheduled observer considers only disconnected provider-exited terminals.
Automatic close begins only after the cleanup timer is enabled. A candidate
then needs 72 continuous hours with every exact safety predicate unchanged.

Attachment, a live provider, child work, pending reply, recovery conflict,
identity change, missing evidence, or a new terminal generation resets or
blocks eligibility.

`sp prune` performs fresh checks and confirmation for a manual cleanup.

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
default mode. Its advanced repair mode is a separate explicit opt-in with
terminal-state risk. Silence alone does not qualify.

## Noninteractive confirmation

Mutations are interactive by default. Noninteractive close requires an explicit
opt-in and the exact shpool ID:

```bash
SESSION_KIT_NONINTERACTIVE=1 \
SESSION_KIT_CONFIRM_ID=<exact-shpool-id> \
sp close <exact-shpool-id>
```

Do not automate moves, kills, repairs, or cleanup with display numbers.
