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
link count.

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
SESSION_KIT_CODEX_HOME
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
