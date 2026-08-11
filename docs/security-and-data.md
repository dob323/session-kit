# Security and local data

## Local operation

Session Kit has no hosted service, Session Kit cloud account, analytics, update
beacon, or telemetry. It reads local shpool state, provider metadata,
configuration, and its own state. Process evidence comes from Linux `/proc` or
from native macOS process, start-time, and boot metadata.

Optional project discovery reads only project paths already stored in Claude
Code's local project map and history, and in Codex's local configuration and
thread database. It keeps only existing directories, ignores configured
temporary roots, and does not inspect directory contents or crawl the
filesystem. Import writes only Session Kit's owner-only `projects.tsv` and its
local backup.

External notifications are optional and off by default.

## Trust boundary

Session Kit is designed for one cooperative Unix account. Before a mutation it
checks file ownership and mode, process start times, daemon and terminal
generations, provider ancestry, and exact conversation UUIDs.

Those checks exist to stop stale or ambiguous dashboard state from selecting a
different session than you meant. They do not isolate mutually hostile processes
running as the same Unix user, and no amount of checking would: a process with
your privileges can read owner-accessible terminal state and edit owner-writable
files directly.

One Unix account may contain several isolated provider subscription profiles.
This separation prevents accidental cross-account launches; it is not a security
boundary against another process running as that Unix user.

Direct source authorization follows that same boundary. The trusted prompt hook
writes owner-private event objects plus compact, rotating, hash-chained ledger
segments from the exact raw prompt bytes. A write-ahead transaction recovers a
killed writer, and sealed-segment or index disagreement fails closed. Provider
transcript matching is stronger local corroboration;
the named `hook-ledger` fallback exists only for transcript lag or absence and
is recorded as non-cryptographic. Agent messages and copied prose are notices,
not source authority. A prompt that begins as a harness envelope — a
`<task-notification`, `<cross-session-message`, `<system-reminder`, or another
machine wrapper — is machine text that reached the same hook, and is refused
both when it is recorded and when it is verified.

Kit-owned state is verified under the strict owner-private rule: this uid owns
it and it carries no group or other bit. A provider transcript is not
kit-owned, and the provider writes it under the operator's umask — Codex
creates every rollout `0644` and never changes it. A Codex rollout is therefore
verified under a no-foreign-write rule instead: this uid owns it, and no group
or other **write** bit is set. Forging authority evidence requires write
access, so a read bit the provider itself set adds no reachable attack; refusing
the mode outright only made Codex evidence unverifiable. The Claude path keeps
the strict rule, because Claude writes its transcripts `0600` and verifies
under it.

A high-risk send also needs an immutable, expiring authority request created
before approval. It binds the target, category, payload digest, scope, and
source thread. The later verified source prompt must quote that request's exact
token; changing any bound field or using an expired token refuses the send.

Supervisor delegation adds a durable preflight and launch-state ledger, but its
enforcement is cooperative for processes running as the same Unix user. Work
launched through provider-native tools or Bash can bypass the supervisor API;
such launches are unsupported and do not gain recorded source authority,
preflight approval, or verified worker identity merely by existing. Launcher
output is never identity proof; only a separate fresh inventory reconciliation
can bind the actual provider, model, identity, and launch idempotency key.

## Data stored locally

Private state may include:

- shpool IDs and boot-scoped terminal numbers;
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
- optional terminal journals.

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

Internal IDs are hidden from normal dashboard rows. They stay available in
owner-only detail and JSON output, and through an explicit search, because exact
identity is what makes diagnosis and action proof possible.

## Logs and terminal journals

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

Open, move, kill, repair, recovery, fork, and cleanup actions use fresh live
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

`k` accepts visible numbers, comma-separated lists, and ranges from a fresh
dashboard. It validates every requested display number before starting, then
creates a separate frozen proof for each selected session. Each item shows its
title, provider, and exact ID as the safety notice, and is revalidated under the
action lock immediately before close, so no additional confirmation prompt is
required. A refusal leaves that item unchanged, but it does not undo an earlier
close in the same batch. Cached inventory disables the action entirely.

## Automatic cleanup

Automatic close is limited to disconnected provider-exited terminals. The
cleanup timer must be enabled, and a target must then hold the same exact
eligible state for 72 continuous hours.

Every close rechecks the exact daemon, terminal generation, exited provider
identity, attachment state, child processes, reply state, and recovery conflicts
twice against fresh evidence. Changed or missing evidence resets or blocks
eligibility. Journals and provider-native conversations are retained.

The reaper supports the guarded 72-hour close and manual prune on Linux and
macOS. Temporary picker-file expiry and stale immutable-release garbage
collection are currently Linux-only maintenance tasks.

## Watchdog

The installed watchdog defaults to report-only mode on Linux and macOS. It
records local evidence such as a manager timeout, a direct handoff failure, a
terminal thread mismatch, or a changed running binary. In that mode it does not
attach, detach, move, kill, restart, or repair anything.

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

Install, update, and rollback copy service definitions but do not start,
restart, or reload services. A running service therefore keeps executing the
code it started with until you restart it deliberately.

On macOS, `session-kit services enable` installs and loads the generated
LaunchAgent definitions, and `session-kit services disable` refuses to unload
the shpool job unless two fresh checks prove no sessions remain.

## Backups

Install, update, rollback, project import, and login-integration operations
create private backups before replacing host files. Backups may contain shell
startup content, private paths, UUIDs, configuration, or earlier service
definitions. Store them as private data and never commit them.
