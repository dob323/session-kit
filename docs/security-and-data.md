# Security and local data

## Local operation

Session Kit has no hosted service, account, analytics, update beacon, or
telemetry. It reads local shpool state, Linux process metadata, provider
metadata, configuration, and its own state.

External notifications are optional and off by default.

## Trust boundary

Session Kit is designed for one cooperative Unix account. Before a mutation it
checks file ownership and mode, process start times, daemon and terminal
generations, provider ancestry, and exact conversation UUIDs.

These checks prevent stale or ambiguous dashboard state from selecting a
different session. They do not isolate mutually hostile processes running as
the same Unix user.

## Data stored locally

Private state may include:

- shpool IDs and boot-scoped terminal numbers;
- provider conversation UUIDs;
- titles and working directories;
- process IDs, start times, and generations;
- recovery records and release receipts;
- cleanup eligibility and health events;
- short-lived action proofs;
- optional terminal journals.

The picker-action log is privacy-minimal. Each record contains only schema
version, a fixed action label, a fixed outcome label, and time. It contains no
session ID, UUID, title, path, prompt, response, terminal output, IP address, or
credential. Each append removes entries older than seven days and caps the file
at 1,000 records and 256 KiB.

Internal IDs are hidden from normal dashboard rows. They remain available in
owner-only detail and JSON output and through an explicit search because exact
identity is required for diagnosis and confirmation.

## Terminal journals

Journals are off by default. When enabled, they can contain every byte shown by
the terminal, including:

- credentials and tokens;
- source code and private prompts;
- command output;
- personal or regulated data;
- terminal control sequences.

Keep state and journal directories mode `0700` and files mode `0600`. Define a
backup and retention policy before opting in. Never attach a journal to a
public issue.

Uninstall retains existing journals unless the user performs a separate,
reviewed removal. The cleanup observer does not delete, truncate, compress, or
archive journals.

## Mutating actions

Open, move, kill, repair, recovery, fork, and cleanup actions use fresh live
evidence. A stale, duplicate, partial, malformed, or unsafe target is refused.

`k <number>` resolves the visible number to one exact shpool ID, displays the
title, provider, and exact ID, and asks for confirmation. Cached inventory
disables the action.

## Automatic cleanup

Automatic close is limited to disconnected provider-exited terminals. The
cleanup timer must be enabled, and a target must then remain in the same exact
eligible state for 72 continuous hours.

Every close rechecks the exact daemon, terminal generation, exited provider
identity, attachment state, child processes, reply state, and recovery
conflicts. Changed or missing evidence resets or blocks eligibility. Journals
and provider-native conversations are retained.

## Watchdog

The installed watchdog defaults to report-only mode. It records local evidence
such as a manager timeout, direct handoff failure, terminal thread mismatch, or
changed running binary. It does not attach, detach, move, kill, restart, or
repair in that mode.

An advanced `SESSION_KIT_WATCHDOG_MODE=repair` opt-in can close a terminal with
direct proof of an unrecoverable handoff failure and relaunch the exact provider
conversation. This can lose terminal-only state and depends on exact
provider-native recovery. Enable it only after reviewing owner-only evidence
and testing manual repair. The installer never opts in.

Quiet output alone is not failure evidence.

## Backups

Installer and update operations create and verify a private backup before
replacing host files. Backups may contain private paths, UUIDs, and
configuration. Store them as private data and never commit them.
