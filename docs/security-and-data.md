# Security and local data

## Local operation

Session Kit has no hosted service, Session Kit cloud account, analytics, update
beacon, or telemetry. It reads local shpool state, provider metadata,
configuration, and its own state. Process evidence comes from Linux `/proc` or
from native macOS process, start-time, and boot metadata.

Optional project discovery reads only project paths already stored in Claude
Code's local project map and history, and in Codex's local configuration and
conversation database. It keeps only existing directories, ignores configured
temporary roots, and does not inspect directory contents or crawl the
filesystem. Import writes only Session Kit's owner-only `projects.tsv` and its
local backup.

External notifications are optional and off by default.

## Trust boundary

Session Kit is designed for one cooperative Unix account. Before a mutation it
checks file ownership and mode, process start times, daemon and terminal
generations, provider ancestry, and exact conversation UUIDs.

Those checks exist to stop stale or ambiguous picker state from selecting a
different session than you meant. They do not isolate mutually hostile processes
running as the same Unix user, and no amount of checking would: a process with
your privileges can read owner-accessible terminal state and edit owner-writable
files directly.

One Unix account may contain several isolated provider subscription profiles.
This separation prevents accidental cross-account launches; it is not a security
boundary against another process running as that Unix user.

## Data stored locally

Private state may include:

- shpool IDs and boot-scoped session numbers;
- provider conversation UUIDs;
- titles, session color overrides, and working directories;
- process IDs, start times, and generations;
- recovery records, install receipts, and release receipts;
- imported project aliases, provider names, and working directories;
- provider account aliases, email descriptions, verification state, and
  provider-supplied plan, health, quota, or reset descriptions when available;
- service-definition receipts and launch records;
- the expected shpool binary fingerprint;
- cleanup eligibility and health events;
- short-lived action proofs;
- shell startup-file and transaction backups;
- watchdog, reaper, and macOS LaunchAgent logs;
- optional session history.

The picker-action log is privacy-minimal. Each record holds only a schema
version, a fixed action label, a fixed outcome label, and a time. It contains no
session ID, UUID, title, path, prompt, response, terminal output, IP address, or
credential. Each append drops entries older than seven days and caps the file at
1,000 records and 256 KiB.

Provider authentication remains in each provider-owned `CLAUDE_CONFIG_DIR` or
`CODEX_HOME`. Session Kit never copies or links tokens between profiles and never
stores, prints, or logs token values in its registry, picker, detail output, or
action log. Account email and status are private local metadata and should not be
shared in public diagnostics.

Internal IDs are hidden from the picker. They stay available in
owner-only detail and JSON output, and through an explicit search, because exact
identity is what makes diagnosis and action proof possible.

## Logs and session history

Watchdog, reaper, and macOS LaunchAgent stdout and stderr logs are local
operational evidence. They can contain process details, private paths, or error
text. Session Kit creates its macOS log files with mode `0600`, but launchd does
not give Session Kit a verified retention or rotation policy, so review and
manage their size and retention as private data.

Terminal journals are off by default. When enabled they can contain every byte
the terminal displayed, including:

- credentials and tokens;
- source code and private prompts;
- command output;
- personal or regulated data;
- terminal control sequences.

Keep state and journal directories mode `0700` and files mode `0600`. Decide a
backup and retention policy before opting in. Never attach a journal or a local
service log to a public issue.

Uninstall retains existing state, logs, journals, and archives unless you
perform a separate, reviewed removal. The cleanup observer does not delete,
truncate, compress, or archive journals.

## Mutating actions

Open, move, close, repair, recovery, fork, and cleanup actions use fresh live
evidence. A stale, duplicate, partial, malformed, or unsafe target is refused.

Changing a running conversation's subscription account is also a mutating
action. It requires an explicit confirmation, the exact provider UUID and
generation, no active turn, tool, hook, child, or background provider work, and
a freshly verified target account. A conversation waiting for the operator may
switch because its provider is idle. The new alias is committed only after the
exact conversation resumes successfully; a failed change restores its private
checkpoint and original binding before an exact resume is attempted on the
original profile. A local kill switch blocks account selection and account
changes without removing profile data. The switch is the owner-only
`$XDG_STATE_HOME/session-kit/account-switching-off` state sentinel, defaulting
to `~/.local/state/session-kit/account-switching-off`; its presence also blocks
enrollment and does not stop an already-running provider.

An automatic account change carries the same proofs. It substitutes the
operator's confirmation for a decision made from published account facts, a
fresh usage feed, the account's own weekly number, a target still above the
reserve, and a per-conversation count of previous automatic moves, and it
takes every one of the manual path's safety proofs unchanged, twice, around the
same action lock. It also checks that the account the live process is signed in
to is the account found dry. If the provider was launched with an explicit
model, the handoff carries the same generation-bound model record and launch
key; it never resumes that conversation on an invented default.

It never enables an account. Re-verifying a target profile re-reads it under
the registry lock and refuses if it was disabled while the provider was being
asked to identify itself, so a disable made mid-handoff stands.

A successful move keeps the shell, the window and the exact conversation UUID:
the in-place handoff is the only path it uses. A move that does not prove
itself is rolled back without the picker's recreate tail, that tail closes the
session and re-creates it, which is not a conversation left working, and where
the rollback itself cannot be proven, nothing further is changed and the
refusal says so.

One owner-only file records all of it: `account-auto-hops.json` in the state
directory holds, per conversation, the count of automatic moves, one entry per
move, and the notices the operator is still owed. A move is counted when it is
reserved rather than when it completes, and a notice is cleared only by a
delivery that succeeded. It carries account aliases and timestamps, never
conversation content. The kill switch above stops automatic changes as well as
manual ones, and nothing in Session Kit creates that file.

In the key-driven picker, `k` accepts visible numbers, comma-separated lists,
ranges, or `all` for the unchanged page on screen. The interactive picker marks
the same visible numbers by typing digits, commas, and ranges, then opens an
action panel whose default is **Close**. Both surfaces validate the display
numbers before starting and create a separate frozen proof for each selected
session. The proof is revalidated under the action lock immediately before the
close, so there is no additional confirmation prompt and normal screens never
print internal IDs. A refusal leaves that item unchanged, but it does not undo
an earlier close in the same batch. Cached inventory disables the action.

## Automatic cleanup

Automatic close is limited to disconnected provider-exited terminals. The
cleanup timer must be enabled, and a target must then hold the same exact
eligible state for 72 continuous hours.

Every close rechecks the exact daemon, terminal generation, exited provider
identity, attachment state, child processes, and recovery conflicts
twice against fresh evidence. Changed or missing evidence resets or blocks
eligibility. Journals and provider-native conversations are retained.

The reaper supports the guarded 72-hour close and manual prune on Linux and
macOS. Temporary picker-file expiry and stale immutable-release garbage
collection are currently Linux-only maintenance tasks.

## Watchdog

The installed watchdog defaults to report-only mode on Linux and macOS. It
records local evidence such as a manager timeout, a direct handoff failure, a
terminal thread mismatch, or a changed running binary. In that mode it does not
open, detach, move, close, restart, or repair anything.

**It raises no alert anywhere until you configure a notifier.** The watchdog
detects and logs either way, but with `SESSION_KIT_WATCHDOG_NOTIFY` unset the
only record is the owner-only watchdog log, and nobody is told. See
[Watchdog alerts](configuration.md#watchdog-alerts).

### The shpool binary fingerprint

The watchdog compares the running daemon's executable against a recorded
fingerprint, because shpool is usually installed with Cargo and a routine
reinstall silently replaces a patched build with the stock one. Without this
check, sessions simply start failing again with nothing to explain why.

Two states make the check useless rather than merely absent, and both are quiet:

- **No fingerprint recorded.** The check is skipped entirely, so nothing breaks,
  and nothing is watched either.
- **A stale fingerprint.** After you legitimately rebuild shpool, the recorded
  value no longer matches, and the watchdog reports a changed binary every time
  it looks. A check that is always complaining is one you stop reading, which
  costs you the real event later.

Record the fingerprint again as the last step of any deliberate shpool rebuild.
The [shpool patch guide](../shpool-patch/README.md) has the exact command, and
`session-kit doctor` reports whether the recorded value is missing or no longer
matches the running daemon.

### Repair mode

Advanced `SESSION_KIT_WATCHDOG_MODE=repair` is Linux-only. macOS refuses repair
mode outright, because it lacks the Linux daemon-thread evidence that action
depends on. On Linux, repair mode can close a terminal only with direct proof of
an unrecoverable handoff failure, and can relaunch the exact provider
conversation. This can lose terminal-only state. The installer never opts in.

Quiet output alone is not failure evidence.

## Service activation

Install, update, and rollback copy service definitions and refresh safe
kit-owned processes. On Linux they reload the systemd user manager, enable a
new kit timer only when systemd has no prior enablement decision for it, and
try-restart an already-running watchdog. On macOS they kickstart an already
loaded watchdog. They do not restart the shpool daemon or socket, because that
would end managed sessions; a service that cannot be refreshed keeps its prior
code and the command prints the remedy.

On macOS, `session-kit services enable` installs and loads the generated
LaunchAgent definitions, and `session-kit services disable` refuses to unload
the shpool job unless two fresh checks prove no sessions remain.

## Backups

Install, update, rollback, project import, and login-integration operations
create private backups before replacing host files. Backups may contain shell
startup content, private paths, UUIDs, configuration, or earlier service
definitions. Store them as private data and never commit them.
