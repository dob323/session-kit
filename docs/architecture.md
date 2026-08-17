# Architecture

Session Kit joins shpool terminals to Claude Code, Codex, or shell processes
without treating display text as identity.

## Core rules

1. Provider UUID plus exact process generation is identity.
2. Session number, title, directory, and timestamps are display context.
3. Every mutation rechecks live identity immediately before it runs.
4. Updates preserve managed sessions even while kit-owned workers refresh.
5. Missing, stale, or conflicting evidence fails closed.

## Data flow

```text
shpool list --json ─┐
Claude Code state  ─┼─> inventory ─> frozen snapshot ─> picker / sp
Codex local state  ─┤        │
native processes  ──┘        ├─> stable session numbers
                             ├─> closed and recovery state
                             └─> short-lived action proofs

stable launcher -> selected immutable release -> helper
managed Bash -> optional journal -> provider -> managed Bash
```

The collector takes bounded snapshots, joins provider conversations to exact
managed shells, publishes one private document, and refuses a strict action
view if any required identity is partial.

## Inventory package

`lib/session_inventory.py` is the installed compatibility entry point. It
retains CLI parsing, public imports, and compatibility wrappers while focused
implementation lives under `lib/sessionkit_inventory/`.

The package currently contains these focused groups; every shipped module is
accounted for here:

| Area | Modules |
| --- | --- |
| Core I/O and proof | `common`, `private_store`, `state_io`, `validation`, `processes` |
| Provider readers | `providers`, `providers_claude`, `providers_codex`, `attention`, `pulse`, `transcripts`, `transcript_text`, `session_model`, `worker_model` |
| Collection and display | `collector`, `model`, `snapshot`, `labels`, `idle_state`, `render` |
| Names and selectors | `terminal`, `names`, `names_push`, `self_name`, `colors`, `origins`, `printed_selectors` |
| Accounts and projects | `accounts`, `account_guard`, `projects`, `worktrees` |
| Lifecycle and history | `lifecycle`, `recovery`, `recovery_list`, `closed_sessions`, `reaper`, `subagent_sweep`, `history_search`, `journal_render`, `migration` |

Package modules do not import the facade. Imports perform no process scans,
locks, configuration reads, or state writes. Compatibility wrappers inject
dependencies at call time where tests and older callers rely on facade patch
points; package-local callers are patched on the module that resolves them.
The [modularization contract](maintainers/modularization-roadmap.md) records
that boundary.

## Two picker layouts, one meaning

The key-driven picker and `sp list` use
`lib/sessionkit_inventory/render.py`. The cursor-driven picker uses
`lib/sessionkit_tui/frame.py`. They are separate layout engines because one
emits complete terminal frames from Bash and the other paints a curses screen.

They share the same snapshot, labels, canonical session-order key, and guarded
`sp` actions. A wording or order change therefore belongs in the shared label
and model layer first, followed by both renderers and their comparison tests.

The installed `kit` executable resolves the key-driven picker directly. In an
interactive managed Bash that sourced `bashrc/shpool.bashrc`, the `kit` shell
function goes through `bin/shpool_login_launcher`; that launcher can select the
cursor-driven screen and fall back to the key-driven one. SSH itself opens an
ordinary shell rather than a picker.

## Action proofs

Before an action, the command layer binds and rechecks:

- session-manager process and start time;
- terminal ID and generation;
- managed shell process and start time;
- provider process, ancestry, and start time;
- provider and exact conversation UUID;
- frozen snapshot generation.

The proof is owner-only and short-lived. Any change stops the action and sends
the picker back for a fresh snapshot. Neither picker kills, moves, or restores
a session directly; it invokes the matching proof-bound `sp` verb.

## Session lifecycle

`bashrc/shpool.bashrc` consumes paired one-shot launch records and starts the
chosen provider only after exact startup proof. The provider remains a child
of the managed shell.

A clean provider exit closes the session and records the conversation in
Closed sessions. A crash reopens the exact conversation once. A second crash
within a minute stops the loop; the session closes only when recovery is
proved, otherwise it remains open and the picker states why.

Journals are optional and off by default. When enabled, each new managed
session writes an append-only local recording. Provider transcripts remain in
provider-owned storage. Session Kit uploads neither.

## Cleanup

The scheduled cleanup observer considers only detached, provider-exited
terminals. A target must keep the same exact safe state for 72 continuous
hours after the timer is enabled. Live provider work, child work, attachment,
recovery conflict, identity churn, or unreadable evidence blocks the close.

Sub-agent cleanup is a separate pass. It watches the worker transcript's size
and nanosecond modification time, not CPU use. An unreadable idle window turns
that pass off rather than selecting a shorter timeout.

## Human and machine output

Normal rows and human detail output use session numbers and titles. Internal
terminal IDs, provider UUIDs, PIDs, and generation fields remain in owner-only
state, action proofs, and explicit machine-readable lookup output. A command
may accept an exact ID without printing it back.

The snapshot schema is additive. Existing fields are not renamed, retyped, or
removed while a picker from an earlier release may still be reading them. See
[The session snapshot](../lib/sessionkit_inventory/SNAPSHOT.md).

## Installed releases and services

Each selected commit has an immutable release directory. Stable launchers
resolve `current` once and pin that physical directory for the command's
lifetime. The separately pinned management release keeps update and rollback
recovery available when an older runtime is selected.

Install, update, and rollback write platform service definitions and then
refresh the kit watchdog:

- Linux reloads the user systemd manager, starts a newly introduced timer only
  when systemd has no prior enablement decision, and try-restarts the running
  watchdog;
- macOS rewrites private templates and kickstarts an already loaded kit
  watchdog;
- neither path restarts the shpool session manager.

Explicit `session-kit services enable` and `disable` control the full service
set under their live-session safety checks.

## Platform boundary

Linux uses `/proc`, Bash, Python 3.10 or newer, and systemd user services.
macOS 14 or newer supports Apple Silicon and Intel with Python 3.11 or newer,
Homebrew Bash 4 or newer, native process APIs, and per-user LaunchAgents in the
logged-in GUI domain. The macOS watchdog is report-only; repair mode depends on
Linux daemon-thread evidence.

Both platforms use shpool 0.11.0. Optional source patches and their exact scope
are documented in the [shpool patch guide](../shpool-patch/README.md).
