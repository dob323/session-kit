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

## Color

The picker uses color only when output is an interactive, supported terminal.
Provider, availability, attention, danger, and secondary text each have one
consistent category. Labels and symbols preserve the meaning without color.

Disable color with either variable:

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
```

Other environment names in the source are internal test or release hooks and
are not a stable public interface.
