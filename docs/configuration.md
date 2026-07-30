# Configuration

Session Kit uses XDG paths and keeps host-specific configuration outside the
immutable release directory.

## Default paths

| Purpose | Default |
| --- | --- |
| Inventory configuration | `${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/inventory.json` |
| Project aliases | `${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/projects.tsv` |
| Session Kit state | `${XDG_STATE_HOME:-$HOME/.local/state}/session-kit` |
| Terminal journals | `${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal` |
| Recovery journals | `${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal-recovery` |
| Launch records | `${XDG_STATE_HOME:-$HOME/.local/state}/shpool-start` |
| Immutable releases | `$HOME/.local/lib/session-kit/releases` |
| Stable commands | `$HOME/.local/bin` |

Private directories should be mode `0700`; private files should be mode `0600`.
Session Kit refuses sensitive mutation proofs with unsafe type, ownership,
mode, or link count.

## Inventory configuration

Start from [../config/session-inventory.example.json](../config/session-inventory.example.json):

```json
{
  "schema_version": 1,
  "command_timeout_seconds": 6,
  "max_proc_nodes": 16384,
  "max_proc_depth": 32,
  "aliases": {}
}
```

The `aliases` and `automatic_titles` objects are managed by Session Kit.
Manual aliases take priority over provider titles, automatic titles, and
fallback descriptions.

Use `sp name` for aliases instead of editing UUID keys by hand:

```text
sp name <terminal-number|shpool-id> <title>
sp name reset <terminal-number|shpool-id>
```

## Project aliases

The projects file is tab-separated:

```text
# alias<TAB>provider<TAB>absolute cwd
web	claude	/absolute/path/to/web
api	codex	/absolute/path/to/api
tools	shell	/absolute/path/to/tools
```

An alias contains lowercase letters, numbers, `_`, or `-`. The provider is
`claude`, `codex`, or `shell`. The directory must be absolute and must exist
when the alias is used.

With that file:

```text
sp new web
sp new codex api
sp new shell tools
```

## shpool configuration

Review [../config/shpool.example.toml](../config/shpool.example.toml). Session
Kit expects a bounded rendered restore buffer:

```toml
session_restore_mode = { lines = 500 }
output_spool_lines = 1000
vt100_output_spool_width = 200
prompt_prefix = ""
```

The terminal journal remains the full local byte record. Reattaching restores a
bounded rendered view instead of replaying the whole journal.

## Guided journal policy

The guided installer will enable journals for new managed sessions by default
after showing where data is stored. Existing journal writers are not replaced
mid-session.

Create `$HOME/.no_shpool_journal` before a new managed session starts to skip
its journal wrapper. This does not delete existing journals.

## Picker and scheduled-component switches

These files disable optional behavior:

```text
$HOME/.no_shpool           automatic SSH picker
$HOME/.no_shpool_reaper    scheduled reaper reports
$HOME/.no_shpool_watchdog  watchdog reports
$HOME/.no_shpool_journal   journals for new managed sessions
```

Removing a sentinel re-enables the corresponding component. Review the current
configuration before doing so.

## Color

Color is used only on an interactive terminal. Set either variable to disable
it:

```bash
export NO_COLOR=1
export SESSION_KIT_NO_COLOR=1
```

## Supported overrides

Common test and advanced deployment overrides include:

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

Several additional environment variables in the source are internal test or
release hooks. They are not a stable public configuration interface unless
listed here.
