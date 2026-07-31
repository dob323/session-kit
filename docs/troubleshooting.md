# Troubleshooting

Start with read-only checks:

```text
sp health
sp list
```

Before sharing output, remove titles, UUIDs, working directories, process
details, and journal content.

## Picker does not open

Confirm that:

- the login shell is interactive Bash;
- standard input and output are terminals;
- `$HOME/.no_shpool` is absent;
- `$HOME/.local/bin/shpool_login` is executable;
- the guarded Session Kit block exists in the active Bash startup file.

Noninteractive shells skip the picker. Press Enter in the picker for a normal
terminal. `session-kit disable-login` removes automatic picker startup without
removing Session Kit commands.

## Color is hard to read

Disable it:

```bash
export SESSION_KIT_NO_COLOR=1
```

`NO_COLOR` is also supported. The text labels and symbols carry the full
meaning without color.

## Inventory is unavailable or cached

Live actions require:

- a responding shpool daemon;
- valid `shpool list --json`;
- readable Linux `/proc`;
- Python 3.10 or newer;
- exact provider metadata for provider-specific actions.

Cached inventory is display-only. Kill, move, repair, and recovery actions stay
disabled until a fresh guard succeeds.

If a manager probe times out, do not repeatedly attach or kill. Preserve
owner-only logs and inspect the daemon first.

## Reply alert is missing

Session Kit uses structured provider events, not prose. Check `sp detail` for
the session and confirm the provider version is within the tested release
matrix.

For Codex, only a live unresolved `request_user_input` without automatic
resolution needs a reply. Completed, aborted, superseded, malformed, or
optional questions do not show the alert.

## New Codex session says setup is incomplete

Codex may not create its root rollout until the first user message. Session Kit
first proves the exact process tree, then binds the UUID when it appears.

If instructed:

```text
sp verify-start <terminal-number|shpool-id>
```

Do not delete paired launch records.

## Session is open elsewhere

Selecting the row shows a move action. You can also run:

```text
sp takeover <terminal-number|shpool-id>
```

Moving disconnects the earlier terminal view after confirmation. It does not
create a second provider process.

## Provider exited

A normal Claude Code or Codex exit should leave the managed terminal alive and
label it as provider exited. Open that row to reach the provider-exit menu,
where you can reopen the exact conversation, keep the terminal, open an
ordinary shell, or close the terminal.

If the entire row disappears, collect a sanitized `sp health`, `sp detail`, and
relevant owner-only event timestamps. Do not publish UUIDs or terminal output.

## `k <number>` changes nothing

The kill shortcut accepts only a visible number from a fresh dashboard. It
refuses cached state, an invalid or hidden number, changed identity, and unsafe
proof files.

After resolving the target, it displays the title, provider, and exact shpool
ID and waits for confirmation. A refusal leaves the session unchanged and
refreshes the dashboard.

## Quiet session

Quiet output does not prove failure. A provider can be working without recent
terminal output.

Use `sp repair` only when the health evidence names a direct, unrecoverable
handoff failure.

## Automatic cleanup did not run

This is usually a safe refusal. A provider-exited terminal must remain detached
and in the same exact safe state for 72 continuous hours after the cleanup
timer is enabled. Any live provider, child process, reply, attachment, recovery
conflict, generation change, or missing evidence blocks cleanup.

Manual inspection remains available through `sp detail` and `sp prune`.

## Terminal cannot be opened

For direct shpool handoff failure evidence:

```text
sp repair <terminal-number|shpool-id>
```

Repair ends the unreachable managed terminal and resumes the exact provider
conversation in a new one. It refuses attached, ambiguous, or UUID-less
targets.

## Changed shpool binary report

A package or Cargo update may have replaced the binary used by the running
daemon. Do not restart the daemon as a diagnostic step. Compare the running
binary, prior binary, active sessions, and optional patch choice first.

## Report a problem

Use the issue template for sanitized behavior reports. Use the private process
in [Security policy](../SECURITY.md) for any vulnerability or suspected data
exposure.
