# Architecture

## Design goals

Session Kit is built around four rules:

1. Provider conversation UUIDs are durable identity.
2. Display numbers, names, paths, and recency are context, not authority.
3. A mutating action must revalidate live identity immediately before it runs.
4. Existing sessions must survive Session Kit release selection.

## Components

```text
shpool list --json ─┐
Claude agent state ─┼─> session_inventory.py ─> snapshot ─> picker / sp
Codex local state  ─┤              │
Linux /proc ────────┘              ├─> recovery state
                                   ├─> terminal numbers
                                   └─> guarded mutation proofs

stable launcher -> current release -> command helper
managed Bash -> optional journal -> provider or shell
```

### Inventory core

`lib/session_inventory.py`:

- collects one shpool snapshot;
- scans a bounded process tree;
- joins Claude Code and Codex roots to exact shpool shells;
- classifies structured reply state;
- assigns boot-scoped terminal numbers;
- resolves title priority;
- maintains private recovery and alias state;
- renders a bounded, control-safe terminal view.

Strict and guard snapshots refuse partial provider identity when an operation
requires exact identity.

### Command layer

`bin/sp` provides explicit session operations. `bin/shpool_login` implements the
interactive SSH picker. `bin/shpool_status` exposes inventory and lookup views.
`bin/codex_resume_here` prints or executes an exact Codex resume command.

The shared shell library carries proof fields between lookup and action. A
fresh guard snapshot must still match the displayed daemon, shell, provider,
UUID, process start times, and shpool generation.

### Shell integration

`bashrc/shpool.bashrc`:

- starts a guided journal wrapper for new managed sessions;
- consumes one-shot launch records;
- starts Claude Code, Codex, or a shell only after the paired launch record is
  armed;
- provides the managed-session prompt marker and `bye` behavior;
- optionally opens the SSH picker.

Session Kit supports Bash only.

### Journals and recovery

New managed sessions write one append-only raw journal segment. Reattachment
uses shpool's bounded rendered buffer and does not replay the full journal.

Recovery records retain exact provider, UUID, working directory, title, and
argument information. Recovery refuses an already-active or ambiguous UUID.

### Reaper

The reaper observes disconnected empty shells and records candidates. It never
kills a session. `sp prune` performs two fresh snapshots and an exact process
tree check before a confirmed removal.

### Watchdog

The public watchdog policy is report-only. It reads the shpool user journal,
inventory, process table, and optional binary fingerprint. It does not contact
the daemon or repair sessions automatically.

### Release layout

Each committed release is copied into an immutable directory named by its full
Git commit ID. A stable launcher resolves one `current` symlink and dispatches
only known helper names.

Activation changes the pointer, integration marker, and receipt as one
recoverable transaction. It does not restart shpool.

## Identity and title model

A managed AI root is identified by provider, exact UUID, provider PID and start
time, shell PID and start time, shpool ID and generation, and daemon generation.

Title sources are display-only:

1. local alias;
2. explicit provider rename;
3. retained automatic title;
4. deterministic fallback;
5. shortened UUID fallback.

Subagents remain children of their root and do not become top-level managed
sessions.

## Platform boundary

The supported implementation depends on Linux `/proc` and systemd user
services. A future macOS core preview requires platform-specific process and
service handling plus a real-Mac lifecycle test. Tracking a plist alone does
not establish support.
