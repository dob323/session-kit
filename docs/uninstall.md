# Uninstall Session Kit

## Current status

Disable automatic login behavior first, then inspect active sessions:

```bash
session-kit disable-login
sp list
session-kit uninstall
```

Uninstalling Session Kit is separate from deleting local journals, recovery
records, or provider-native conversations.

The default command removes the managed Bash block and stable helper commands.
It does not stop services, remove shpool, delete releases, or delete private
data. Add `--purge-code` to remove installed Session Kit releases and
`--purge-config` to remove Session Kit configuration. Both flags retain
journals, archives, provider storage, and other session data.

## Default data policy

Uninstall keeps:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/
${XDG_STATE_HOME:-$HOME/.local/state}/session-kit/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal-recovery/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-start/
```

These locations may be needed for history, recovery, audit, or reinstallation.
They can also contain secrets and private paths.

## Data purge

Data purge must be a separate explicit operation. Before removing anything it
must:

- prove that no live writer uses the target;
- list every target path and data class;
- offer a backup;
- require confirmation;
- refuse symlinks, unexpected owners, and broad path targets;
- never remove Claude Code or Codex provider storage.

Session Kit never implements a broad data-purge command. Review and back up
retained paths before any manual removal.
