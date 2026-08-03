# Uninstall Session Kit

Inspect managed sessions and service state before uninstalling:

```bash
sp list
session-kit services status
session-kit disable-login
```

On macOS, the uninstall command refuses while a Session Kit LaunchAgent is
loaded or an active managed plist remains. After every managed session is
closed, disable the jobs first:

```bash
session-kit services disable
session-kit uninstall
```

`services disable` proves there are zero shpool sessions twice before unloading
the shpool job. It also validates that the active plist files belong to this
Session Kit installation.

On Linux, disable the user socket and cleanup timer before uninstalling:

```bash
session-kit services disable
session-kit uninstall
```

The Linux command disables the Session Kit socket and reaper timer, but it does
not claim to terminate an already-running shpool daemon. Check active sessions
and service state before purging code.

## What the default uninstall removes

The default uninstall removes managed shell-login blocks and the stable
commands from `$HOME/.local/bin`, including `session-kit`, `kit`, and the helper
launchers. On macOS, active LaunchAgent files are removed by the required
`services disable` step.

The default does not close sessions, delete provider conversations, remove
shpool, remove installed release code, remove configuration, or delete private
state. It removes the install receipt after verifying the managed files.

On Linux, copied systemd user-unit files remain available for explicit review
and removal after the services have been disabled. Session Kit does not delete
them as part of the default uninstall.

## Optional code and configuration removal

`--purge-code` removes the installed Session Kit release root.
`--purge-config` removes the Session Kit configuration root. Each option
requires a valid receipt and ownership markers and refuses an unproved target.
Choose these flags on the uninstall invocation while the receipt and
`session-kit` command are still present:

```bash
session-kit uninstall --purge-code --purge-config
```

Do not use `--purge-code` while a managed session or Session Kit service still
depends on an installed release. Both purge options retain state, journals,
archives, launch records, logs, backups, and provider storage.

## Retained data

By default Session Kit keeps:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/
${XDG_STATE_HOME:-$HOME/.local/state}/session-kit/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal-recovery/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-start/
```

On macOS, Session Kit operational logs are stored beneath the Session Kit state
root. These locations can contain exact IDs, private paths, action receipts,
shell startup backups, service history, logs, and terminal content.

## Removing retained data

Data removal is a separate manual decision. Before deleting anything:

- prove no live process writes to the target;
- list each exact path and data class;
- make and verify a backup if recovery matters;
- reject symlinks, unexpected owners, and broad path targets;
- never include Claude Code or Codex provider storage by accident.

Session Kit does not provide a broad "delete everything" command.
