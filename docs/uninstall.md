# Uninstall Session Kit

Disable automatic login first and inspect active sessions:

```bash
session-kit disable-login
sp list
session-kit uninstall
```

The default uninstall removes the managed Bash block and stable helper
commands. It does not stop services, remove shpool, close sessions, delete
provider conversations, or delete private data.

`--purge-code` removes installed Session Kit release code.
`--purge-config` removes Session Kit configuration. Both retain state,
journals, archives, launch records, and provider storage.

## Retained data

By default Session Kit keeps:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/
${XDG_STATE_HOME:-$HOME/.local/state}/session-kit/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal-recovery/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-start/
```

These locations can contain exact IDs, private paths, action receipts, and
terminal content.

## Removing retained data

Data removal is a separate manual decision. Before deleting anything:

- prove no live process writes to the target;
- list each exact path and data class;
- make and verify a backup if recovery matters;
- reject symlinks, unexpected owners, and broad path targets;
- never include Claude Code or Codex provider storage by accident.

Session Kit does not provide a broad “delete everything” command.
