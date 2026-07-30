# Use Session Kit

## The SSH picker

When enabled, the picker appears in an interactive login shell:

```text
number          open a ready session or inspect one open elsewhere
n               create a Claude, Codex, or managed shell session
x <number>      close the exact displayed session after confirmation
/text           search titles, providers, projects, IDs, and UUIDs
r               refresh live state and clear search
o               view provider roots outside shpool; read-only
u               review exact recovery records
name <number>   set a local Claude or Codex title
name reset #    remove the local title
fork <number>   create an independent provider conversation fork
next / prev     change page
?               show help
Enter           return to a regular terminal
```

Opening a row already attached elsewhere shows an action menu. Session Kit
does not silently take it over.

Inside a managed session, `bye` asks whether to close the shpool session.
Disconnecting SSH leaves it running.

## Start a session

```text
sp new claude [project-alias]
sp new codex [project-alias]
sp new shell [project-alias]
sp new <project-alias>
```

The final form uses the provider stored for the project alias.

New sessions receive generated shpool IDs. The displayed terminal number is
stable for that session during the current host boot, but it is not a durable
identity.

## List and inspect

```text
sp list
sp detail <terminal-number|shpool-id>
sp find <text>
sp health
```

The main groups are:

- `Ready to open`
- `Open elsewhere`
- `Claude`
- `Codex`
- `Shell`
- `Unknown`

Provider roots detected outside shpool are listed in a separate read-only view.
They receive no terminal number and cannot be opened through Session Kit.

## Open or move

```text
sp go <terminal-number|shpool-id>
sp takeover <terminal-number|shpool-id>
```

`sp go` opens a detached exact managed session. `sp takeover` moves an attached
session to the current terminal after a fresh identity check and confirmation.

## Name a conversation

```text
sp name <terminal-number|shpool-id> <title>
sp name reset <terminal-number|shpool-id>
```

Title priority is:

1. local manual alias;
2. explicit provider rename;
3. retained automatic title;
4. deterministic provider, project, and start description;
5. provider plus a shortened UUID;
6. shell command or `Idle shell`.

Reset removes only the local alias.

## Close and prune

```text
sp close <terminal-number|shpool-id>
sp prune
```

Closing requires confirmation and a fresh mutation guard. It ends the managed
terminal process. It does not delete provider-native conversation history.

The scheduled reaper only records old, disconnected, empty-shell candidates.
`sp prune` rechecks a selected candidate under the creation lock and asks for
confirmation. Journal files are retained.

## History and recovery

```text
sp history <terminal-number|shpool-id>
sp recover
```

History reads the local terminal journal. Recovery uses an exact Claude Code or
Codex UUID. It never falls back to “latest,” current directory, or recency.

An exact UUID already active elsewhere is not resumed into a second writable
terminal. Open the existing managed terminal or create an explicit fork.

## Repair

```text
sp repair <terminal-number|shpool-id>
```

Repair is for a terminal that has direct evidence of an unrecoverable shpool
handoff failure. Under the public policy, the watchdog reports evidence but
does not run repair. Review the diagnostic and invoke repair yourself.

Silence alone is not proof that a Claude Code or Codex task is stuck.

## Confirmation in automation

Mutating commands are interactive by default. Noninteractive use requires both
an explicit opt-in and the exact shpool ID:

```bash
SESSION_KIT_NONINTERACTIVE=1 \
SESSION_KIT_CONFIRM_ID=<exact-shpool-id> \
sp close <exact-shpool-id>
```

Do not automate takeovers, closes, repairs, or pruning with display numbers.
