# Security and local data

## Local-only operation

Session Kit has no hosted service, account, analytics, update beacon, or
telemetry. It reads local shpool state, process metadata, provider metadata,
configuration, and Session Kit state.

External notifications are optional and must be explicitly configured. The
public default is local reporting only.

## Trust boundary

Session Kit is designed for one cooperative Unix account. It verifies file
ownership, modes, link counts, process start times, daemon generations,
provider process ancestry, and exact conversation UUIDs before mutations.

Those checks prevent stale or ambiguous UI state from selecting the wrong
session. They do not protect against a hostile process running as the same
Unix user. Such a process can inspect owner-readable state, forge environment
values, or edit owner-writable configuration.

Do not use one Session Kit installation as a security boundary between
untrusted people.

## Data stored locally

Session Kit state may include:

- shpool IDs and boot-scoped terminal numbers;
- Claude Code and Codex conversation UUIDs;
- display titles and working directories;
- process IDs and start times;
- recovery records and activation receipts;
- report-only watchdog findings;
- exact mutation proof files;
- terminal journals.

Provider-native transcripts remain under their provider's own storage.
Session Kit does not replace or upload them.

## Terminal journals

Guided installation enables journals for new managed sessions by default.
Journals contain the raw bytes displayed by a terminal and may include:

- credentials and tokens;
- source code and private prompts;
- command output;
- personal or regulated data;
- terminal control sequences.

Store journal and state directories with mode `0700` and files with mode
`0600`. Include them in backup and retention policy decisions. Do not attach a
journal to a public issue.

The reaper does not delete, truncate, compress, or archive journals. Uninstall
must preserve them unless the user separately requests a data purge.

## Mutating actions

Open, takeover, close, repair, recovery, fork, and prune actions use a fresh
live guard. Actions refuse stale, duplicated, ambiguous, malformed, or unsafe
targets.

Takeover, close, repair, and prune can affect a running terminal. Review the
displayed target and keep a verified rollback or recovery path.

## Watchdog policy

The supported public watchdog is report-only. It may report:

- a manager probe timeout;
- direct terminal-handoff failure evidence;
- a terminal-serving thread count mismatch;
- a running shpool binary whose fingerprint changed;
- a quiet session without proof of failure.

It does not automatically contact shpool, close a session, or launch recovery.
Silence is never enough to declare an AI task dead.

## Backups

A supported installer must take a private pre-change backup and verify it
before installation or update. The backup should cover every file the
installer may replace plus enough inventory evidence to select the correct
rollback behavior.

Backups can contain private paths, UUIDs, and configuration. Do not commit them
to the repository.
