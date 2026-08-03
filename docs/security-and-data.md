# Security and local data

## Local operation

Session Kit has no hosted service, account, analytics, update beacon, or
telemetry. It reads local shpool state, provider metadata, configuration, and
its own state. Process evidence comes from Linux `/proc` or native macOS
process, start-time, and boot metadata.

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
- recovery records, install receipts, and release receipts;
- service-definition receipts and launch records;
- cleanup eligibility and health events;
- short-lived action proofs;
- shell startup-file and transaction backups;
- watchdog, reaper, and macOS LaunchAgent logs;
- optional terminal journals.

The picker-action log is privacy-minimal. Each record contains only schema
version, a fixed action label, a fixed outcome label, and time. It contains no
session ID, UUID, title, path, prompt, response, terminal output, IP address, or
credential. Each append removes entries older than seven days and caps the
file at 1,000 records and 256 KiB.

Internal IDs are hidden from normal dashboard rows. They remain available in
owner-only detail and JSON output and through an explicit search because exact
identity is required for diagnosis and action proof.

## Logs and terminal journals

Watchdog, reaper, and macOS LaunchAgent stdout and stderr logs are local
operational evidence. They can contain process details, private paths, or error
text. Session Kit creates its macOS log files with mode `0600`, but launchd does
not provide Session Kit with a verified retention or rotation policy. Review
and manage their size and retention as private data.

Terminal journals are off by default. When enabled, they can contain every byte
shown by the terminal, including:

- credentials and tokens;
- source code and private prompts;
- command output;
- personal or regulated data;
- terminal control sequences.

Keep state and journal directories mode `0700` and files mode `0600`. Define a
backup and retention policy before opting in. Never attach a journal or local
service log to a public issue.

Uninstall retains existing state, logs, journals, and archives unless the user
performs a separate, reviewed removal. The cleanup observer does not delete,
truncate, compress, or archive journals.

## Mutating actions

Open, move, kill, repair, recovery, fork, and cleanup actions use fresh live
evidence. A stale, duplicate, partial, malformed, or unsafe target is refused.

`k` accepts visible numbers, comma-separated lists, and ranges from a fresh
dashboard. It validates every requested display number before starting, then
creates a separate frozen proof for each selected session. Each item displays
its title, provider, and exact ID as the safety notice and is revalidated under
the action lock immediately before close. There is no additional confirmation
prompt. A refusal leaves that item unchanged, but it does not undo an earlier
close in the same batch. Cached inventory disables the action.

## Automatic cleanup

Automatic close is limited to disconnected provider-exited terminals. The
cleanup timer must be enabled, and a target must then remain in the same exact
eligible state for 72 continuous hours.

Every close rechecks the exact daemon, terminal generation, exited provider
identity, attachment state, child processes, reply state, and recovery
conflicts twice with fresh evidence. Changed or missing evidence resets or
blocks eligibility. Journals and provider-native conversations are retained.

The reaper supports the guarded 72-hour close and manual prune on Linux and
macOS. Temporary picker-file expiry and stale immutable-release garbage
collection are currently Linux-only maintenance tasks.

## Watchdog

The installed watchdog defaults to report-only mode on Linux and macOS. It
records local evidence such as a manager timeout, direct handoff failure,
terminal thread mismatch, or changed running binary. It does not attach,
detach, move, kill, restart, or repair in that mode.

Advanced `SESSION_KIT_WATCHDOG_MODE=repair` is Linux-only. macOS explicitly
refuses repair mode because it lacks the Linux daemon-thread evidence required
for that automatic action. On Linux, repair mode can close a terminal only with
direct proof of an unrecoverable handoff failure and can relaunch the exact
provider conversation. This can lose terminal-only state. The installer never
opts in.

Quiet output alone is not failure evidence.

## Service activation

Install, update, and rollback copy service definitions but do not start,
restart, or reload services. On macOS, `session-kit services enable` installs
and loads the generated LaunchAgent definitions. `session-kit services disable`
refuses to unload the shpool job unless two fresh checks prove that no sessions
remain.

## Backups

Install, update, rollback, and login-integration operations create private
backups before replacing host files. Backups may contain shell startup content,
private paths, UUIDs, configuration, or earlier service definitions. Store them
as private data and never commit them.
