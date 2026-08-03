# Architecture

Session Kit joins shpool terminals to Claude Code, Codex, or shell processes
without treating display text as identity.

## Core rules

1. A provider conversation UUID is durable identity.
2. A terminal number, title, directory, and timestamp are display context.
3. Every mutation must recheck live identity immediately before it runs.
4. A Session Kit update must not disturb an existing shpool session.
5. Missing or conflicting evidence fails closed.

## Data flow

```text
shpool list --json ─┐
Claude Code state  ─┼─> inventory ─> frozen snapshot ─> picker / sp
Codex local state  ─┤        │
native processes  ──┘        ├─> terminal-number state
                             ├─> recovery state
                             └─> private action proof

stable launcher -> selected immutable release -> helper
managed Bash -> optional journal -> provider -> persistent managed Bash
```

`lib/session_inventory.py` is the compatibility entry point. Focused
implementation is moving into `lib/sessionkit_inventory/` in small,
behavior-preserving steps.

The inventory:

- takes one bounded shpool snapshot;
- scans a bounded native process tree;
- joins provider roots to exact shpool shells;
- classifies structured reply and provider-exit state;
- assigns boot-scoped terminal numbers;
- selects one display title;
- writes private recovery and terminal-number state;
- renders control-safe terminal output.

Strict and guard snapshots refuse partial provider identity.

On Linux, process identity comes from `/proc`, including a boot-scoped process
start generation. On macOS, Session Kit uses `PROC_PIDTBSDINFO` for process and
start-time identity, double-reads that identity around `KERN_PROCARGS2`, and
uses `kern.boottime` as boot identity. A PID whose generation changes during a
read is discarded.

## Commands and action proofs

`bin/sp` implements explicit operations. `bin/shpool_login` provides the SSH
picker. `bin/shpool_status` renders and queries inventory.
`bin/codex_resume_here` prepares an exact Codex resume.

Before an action, the shared command layer binds and rechecks:

- shpool daemon process and start time;
- shpool terminal ID and generation;
- managed shell process and start time;
- provider process, ancestry, and start time;
- provider and exact conversation UUID;
- the frozen dashboard generation.

The proof is private, short-lived, owner-only state. If any field changes, the
action stops and the dashboard refreshes.

## Managed terminal lifecycle

`bashrc/shpool.bashrc` consumes a paired one-shot launch record and starts the
selected provider only after exact startup proof.

The provider does not replace the managed shell. When Claude Code or Codex
exits, the terminal remains alive and records an exited-provider state. The
terminal menu can reopen the exact conversation, mark the terminal to keep,
open an ordinary shell, or close the terminal.

This boundary prevents a normal provider quit from silently deleting a
recoverable shpool session.

## Journals

Journals are optional and off by default. When enabled, each new managed
session writes an append-only local segment. Reattachment uses shpool's bounded
rendered buffer rather than replaying the full journal.

Provider-native transcripts remain in provider-owned storage. Session Kit does
not copy them into logs or upload them.

## Cleanup

The scheduled observer may track a disconnected provider-exited terminal. It
cannot close the terminal until the cleanup timer is enabled and the same exact
safe state has been observed continuously for 72 hours.

Automatic close requires the same exact terminal generation, the same exited
provider identity, no attachment, no live provider or child work, no pending
reply, no recovery conflict, and unchanged evidence. Any uncertainty resets or
blocks eligibility. Manual `sp prune` uses fresh checks and an exact-target
safety display.

## Display model

The main dashboard favors names and state. Internal shpool IDs and provider
UUIDs are available through detail, JSON, and explicit search views, not normal
rows.

Semantic color categories are provider, availability, attention, danger, and
secondary text. Text labels always carry the meaning, so color is optional.

## Updates

Every installed Git commit has an immutable release directory. A stable
launcher resolves one `current` link and dispatches only approved helper names.
Linux installs systemd user definitions. macOS installs inactive per-user
LaunchAgent templates; only `session-kit services enable` copies and loads them.
Selecting another Session Kit release changes the pointer and receipt but does
not restart or reload services.

## Platform boundary

The beta supports these two platform models:

- Linux uses `/proc`, Bash, Python 3.10 or newer, and systemd user services.
- macOS 14 or newer supports Apple Silicon and Intel with Python 3.11 or newer,
  Homebrew Bash 4 or newer, official shpool 0.11.0, native Darwin process APIs,
  and per-user LaunchAgents in the logged-in GUI user's `gui/$UID` domain.

The outer macOS login shell may remain zsh, but the managed shpool shell is the
validated modern Bash executable. The LaunchAgents are not privileged or
headless daemons and are unavailable before the user logs into the Mac desktop.
The macOS watchdog is report-only; repair mode remains Linux-only because it
depends on Linux daemon-thread evidence. Other operating systems and service
models stop before installation or mutation.
