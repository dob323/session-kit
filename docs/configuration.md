# Configuration

Session Kit keeps host configuration outside immutable release directories and
uses XDG locations where available.

## Default paths

| Purpose | Default |
| --- | --- |
| Inventory configuration | `${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/inventory.json` |
| Project aliases | `${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/projects.tsv` |
| Session Kit state | `${XDG_STATE_HOME:-$HOME/.local/state}/session-kit` |
| Optional terminal journals | `${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal` |
| Recovery journal archive | `${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal-recovery` |
| Paired launch records | `${XDG_STATE_HOME:-$HOME/.local/state}/shpool-start` |
| shpool configuration | `${XDG_CONFIG_HOME:-$HOME/.config}/shpool/config.toml` |
| Immutable releases | `$HOME/.local/lib/session-kit/releases` |
| Stable commands | `$HOME/.local/bin` |
| macOS launchd templates | `${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/launchd` |
| Active macOS LaunchAgents | `$HOME/Library/LaunchAgents` |

Private directories should be mode `0700`; private files should be mode `0600`.
Sensitive proof files also require the expected owner, regular-file type, and
link count. Provider transcripts are the one exception, because the provider —
not Session Kit — creates them under your umask: a Codex rollout is accepted
when this uid owns it and no group or other write bit is set. See
[Security and data](security-and-data.md).

## Account profiles

An account profile is a local alias for one Claude Code or Codex subscription
login. It is not a Session Kit cloud account. Claude profiles launch with a
profile-specific `CLAUDE_CONFIG_DIR`; Codex profiles launch with a
profile-specific `CODEX_HOME`. These roots keep each provider's native login and
local conversation data separate.

```text
sp account list
sp account enroll <claude|codex> <alias> <email>
sp account verify <claude|codex> <alias>
sp account configure-feeds <absolute-roster-json> <absolute-advice-json>
```

`enroll` creates the owner-private profile location and registers the alias and
email description. Sign in through the provider's normal interactive login.
Session Kit does not copy, link, import, print, or log credentials from another
profile. `verify` then checks that the provider's reported signed-in identity
matches the registered description. An unverified or logged-out profile is not
eligible for a recommendation or account change.

Aliases are short labels intended for the home screen. `sp account list` keeps
the registered identity and last verification time. Session detail shows the
bound alias, email, and provider-supplied plan. The account-choice screen shows
current roster health, usage, and any provider-qualified recommendation.
Provider data that is missing or cannot be verified is shown as unknown rather
than inferred.

The default Claude and Codex homes remain usable as legacy profiles, but Session
Kit does not invent an email identity for them. Enroll and verify a named profile
before relying on account-aware recommendations or switching.

Account changes are manual. Session Kit does not rotate a live thread when a
quota threshold or timer is reached. The owner-only state sentinel
`$XDG_STATE_HOME/session-kit/account-switching-off`, defaulting to
`~/.local/state/session-kit/account-switching-off`, disables enrollment, new
account selection, and switching. It does not delete profiles, change
provider-owned login state, or stop running providers.

`configure-feeds` stores two owner-controlled absolute paths in the private
Session Kit state directory. Guided New uses a fresh roster for availability
and provider-qualified advice for recommendations. Missing or stale feeds leave
the choice unselected; they never guess an account.

## Inventory settings

Start from
[the inventory example](../config/session-inventory.example.json):

```json
{
  "schema_version": 1,
  "command_timeout_seconds": 6,
  "max_proc_nodes": 16384,
  "max_proc_depth": 32,
  "aliases": {}
}
```

Session Kit manages local aliases and automatic titles. Set names through the
command interface so identity checks and locking remain active:

```text
sp name <terminal-number|shpool-id> <title>
sp name reset <terminal-number|shpool-id>
```

Manual names take priority over provider titles, retained automatic names, and
fallback descriptions.

Renaming a session by hand also settles who owns its name, for good. Session Kit
records the rename in `name_ownership` alongside the aliases, and every
automatic naming path — the Claude title hook, the Codex auto-titler, `sp
self-name`, and any queued title retry that outlives a restart — reads that
record and leaves the session alone. `sp name reset` drops the name you gave and
keeps the ownership: automatic naming does not come back and quietly rename a
session you already named.

Automatic naming claims a name the same way, once, at a thread's first prompt.
The claim is what makes the Claude hook a no-op on its second firing and stops a
later pass from re-titling a thread an earlier pass named. Pruning orphaned
automatic titles clears spent automatic claims; it never clears a rename you
made.

## Project aliases

The initial interactive installer offers to import existing folders from both
providers. Review the same bounded discovery at any time:

```text
session-kit projects discover
session-kit projects candidates
session-kit projects import
session-kit projects import --select 1,3-4
session-kit projects list
session-kit projects add web claude /absolute/path/to/web
session-kit projects add api codex /absolute/path/to/api
session-kit projects here
session-kit projects ignore /absolute/path/to/scratch
```

`candidates` answers "what would import add, and under what name" without
writing anything. `import --select` takes only the entries you name, so a
first run is never all or nothing. `here` adds the current directory under a
derived short name. `ignore` keeps a directory out of the picker for good.

The picker offers the same choices without any of these commands: press `m`
for More, then `p` for Projects, to add a directory, review the ones your
providers use that are not listed yet, or drop one.

Discovery reads only provider-owned local records. For Claude Code, those are
the project map in `~/.claude.json` and the project field in local history. For
Codex, they are project entries in `~/.codex/config.toml` and distinct working
directories in the newest local `state_*.sqlite` thread database. Missing
directories and provider-configured temporary roots are ignored. Session Kit
does not scan unrelated folders.

Every directory gets at most one shortcut, whichever providers recorded it:
the provider is already chosen before the project list appears, so a second
row for one directory would read as a duplicate. Alias collisions are resolved
with parent-folder or provider suffixes. Import is repeatable: directories
that already have a row — including an `ignore` row — and all hand-written
lines are left unchanged. Before appending any new rows, Session Kit saves an
owner-only backup under its state `backups` directory.

A row whose provider is `ignore` is a decision, not a shortcut. It is never
listed in the picker, never resolves as a launch target, and because import
gives each directory at most one row, discovery can never offer that
directory again. Adding the directory back withdraws the ignore.

The projects file is tab-separated:

```text
# alias<TAB>provider<TAB>absolute cwd
web	claude	/absolute/path/to/web
api	codex	/absolute/path/to/api
tools	shell	/absolute/path/to/tools
```

Aliases use lowercase letters, numbers, `_`, or `-`. The provider is `claude`,
`codex`, or `shell`. The directory must be absolute and exist when used.

```text
sp new web
sp new codex api
sp new shell tools
```

## shpool settings

Review [the shpool example](../config/shpool.example.toml). Session Kit expects
a bounded rendered restore buffer:

```toml
session_restore_mode = { lines = 500 }
output_spool_lines = 1000
vt100_output_spool_width = 200
prompt_prefix = ""
```

On macOS, installation also records the resolved Homebrew Bash executable in
this file. Session Kit refuses a different existing `shell` value. The launchd
shpool job receives this exact configuration path explicitly because shpool's
native macOS default is under `~/Library/Application Support`.

## Journals

Terminal journals are off by default. Opting in affects new managed sessions;
it does not add or remove a writer inside a session already running.

The `$HOME/.no_shpool_journal` sentinel forces journals off. Removing it does
not delete existing files and does not change an active terminal.

## Optional feature sentinels

```text
$HOME/.no_shpool_reaper    scheduled cleanup observer
$HOME/.no_shpool_watchdog  health reports
$HOME/.no_shpool_journal   journals for new managed sessions
```

Removing a sentinel re-enables that component only after its other
configuration checks pass.

The Bash integration itself is managed by `session-kit enable-login` and
`session-kit disable-login`. These compatibility commands add or remove the
guarded `kit` function and SSH hint; they do not enable automatic picker
startup.

## Advanced watchdog repair mode

The installed watchdog defaults to `report` mode. On Linux, the source also
accepts `SESSION_KIT_WATCHDOG_MODE=repair` as an explicit advanced opt-in. The
installer does not enable repair mode. macOS supports report mode only because
watchdog repair requires Linux daemon-thread evidence.

Repair mode can close a terminal that has direct evidence of an unrecoverable
shpool handoff failure and relaunch the exact provider conversation in a new
managed terminal. That can discard terminal-only state, interrupt child
processes if the proof is wrong, and depend on provider-native recovery being
available. Quiet output alone never qualifies. Review the owner-only watchdog
log and test manual `sp repair` before enabling this mode.

The watchdog accepts a handoff marker only when its journal PID and monotonic
timestamp belong to the current daemon generation, the marker is old enough,
and no later successful attach for that session cancels it. A marker with
unknown recent-output age is reported but never repaired automatically. A
process-wide mismatch between listed sessions and serving-thread count is also
reported once at manager level; it cannot identify a specific session and is
never used to repair one.

## Watchdog alerts

`SESSION_KIT_WATCHDOG_NOTIFY` is **unset by default, so the watchdog raises no
alert anywhere.** It detects and logs conditions either way, but without a
notifier the only record is the owner-only watchdog log and the picker banner —
nobody is told. Set it to an executable that accepts:

```text
--type=<id> --severity=warning --title=<subject> --body=<text>
```

`SESSION_KIT_WATCHDOG_ALERT_TYPE` sets the `--type` value and defaults to
`session-kit.watchdog`. Severity is always `warning`; an automatic repair must
never fire a critical.

Wire this up if you rely on the watchdog at all. A real 2026-08-06 daemon-wide
freeze was detected correctly and reported nowhere, because the notifier had
never been configured — the operator found out by trying to open a session.
Prefer a systemd drop-in over editing the unit, so a reinstall cannot drop it:

```ini
# ~/.config/systemd/user/session-kit-watchdog.service.d/notify.conf
[Service]
Environment=SESSION_KIT_WATCHDOG_NOTIFY=/path/to/your/notifier
```

## Color

Session Kit uses color in two unrelated ways, and only one of them is
configurable here.

**Semantic color** marks provider, availability, attention, danger, and
secondary text, each with one consistent category. The picker applies it only
when output is an interactive, supported terminal, and labels and symbols carry
the same meaning without it.

**Session color** gives each Claude Code and Codex session its own identity
color, so two rows on screen do not look alike. It is derived from the
conversation's identity, and the two providers draw from separate palettes.
There is nothing to configure: the palettes are fixed, because Claude Code
accepts only its own eight color names. See
[Session colors](usage.md#session-colors) for the behavior and
`sp color reconcile` for settling sessions that already share one.

Disable color entirely with either variable:

```bash
export NO_COLOR=1
export SESSION_KIT_NO_COLOR=1
```

## Supported environment overrides

```text
SESSION_KIT_CONFIG
SESSION_KIT_STATE_DIR
SESSION_KIT_JOURNAL_DIR
SESSION_KIT_ARCHIVE_DIR
SESSION_KIT_PROJECTS_FILE
SESSION_KIT_SHPOOL_CMD
SESSION_KIT_CLAUDE_CMD
CLAUDE_CONFIG_DIR
SESSION_KIT_CODEX_HOME
CODEX_HOME
SESSION_KIT_CODEX_DB
SESSION_KIT_BOOT_ID_FILE
SESSION_KIT_AUTO_NAME
SESSION_KIT_NO_COLOR
SESSION_KIT_NONINTERACTIVE
SESSION_KIT_PICKER_REFRESH_SECONDS
SESSION_KIT_WATCHDOG_NOTIFY
SESSION_KIT_WATCHDOG_ALERT_TYPE
SESSION_KIT_WATCHDOG_MODE
```

Other environment names in the source are internal test or release hooks and
are not a stable public interface.
