# Troubleshooting

Start with:

```text
sp health
sp list
```

Sanitize output before sharing it. Titles, UUIDs, working directories, process
information, and journal content may be private.

## The picker does not appear

Check:

- the shell is interactive and uses Bash;
- standard input and output are terminals;
- `$HOME/.no_shpool` is absent;
- `$HOME/.local/bin/shpool_login` is executable;
- the guarded Session Kit source line is present in the active Bash startup
  file.

Noninteractive shells intentionally skip the picker.

## I want a normal shell

Press Enter in the picker. To disable automatic picker display, create:

```text
$HOME/.no_shpool
```

The `sp` commands remain available.

## No color or unreadable color

Set:

```bash
export SESSION_KIT_NO_COLOR=1
```

Session Kit also honors `NO_COLOR`. Color is automatically disabled when output
is not a terminal or the terminal type is unsuitable.

## Inventory is unavailable

The inventory requires:

- one responding shpool daemon;
- `shpool list --json`;
- readable Linux `/proc`;
- Python 3.10 or newer;
- provider metadata for exact Claude Code or Codex classification.

Provider metadata failure should produce an unavailable or unknown state rather
than selecting a conversation by directory or recency.

If the manager probe times out, do not repeatedly attach or kill sessions.
Preserve logs and investigate the daemon first.

## A new Codex session says setup is incomplete

A new Codex process may not write an open rollout until the first user message.
Session Kit proves initial startup from the exact process tree, then binds the
conversation UUID when it becomes available.

If a retained launch record remains:

```text
sp verify-start <terminal-number|shpool-id>
```

If instructed by the error, open the exact session, start an interactive Bash
inside it, then run `sp verify-start` again. Do not delete the paired launch
records.

## A session is open elsewhere

Select its number to inspect actions or use:

```text
sp takeover <terminal-number|shpool-id>
```

Takeover requires confirmation. It moves the managed terminal; it does not
start a second provider conversation.

## A session is quiet

Quiet output is not proof of failure. Codex may report a running state both
while working and while waiting.

The report-only watchdog may provide direct handoff or thread evidence. Use
`sp repair` only after reviewing that evidence.

## A terminal cannot be opened

An unrecoverable shpool terminal handler can leave provider conversation data
intact while the terminal no longer accepts an attachment.

Run:

```text
sp repair <terminal-number|shpool-id>
```

Repair ends the unreachable managed terminal and resumes the exact provider
conversation in a new session. It requires direct failure evidence and refuses
attached, ambiguous, or UUID-less targets.

## New-session launch is disabled

The active release and the private integration marker do not agree, or shell
integration has not been validated. This is a safety refusal.

Do not create a marker by hand. Return to the guided installer or updater and
complete its configuration validation and rollback checks.

## Watchdog reports a changed shpool binary

The running daemon's executable fingerprint differs from the recorded
fingerprint. A package or Cargo update may have replaced it.

Do not restart the daemon as a diagnostic step. Compare the installed and prior
binary, active sessions, and optional patch choice first.

## Reporting a problem

Use the issue template and include versions, sanitized expected and observed
behavior, and whether any state changed. Security problems belong in the
private process in [../SECURITY.md](../SECURITY.md).
