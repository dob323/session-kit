# Uninstall Session Kit

`session-kit uninstall` removes Session Kit's integration with the account: the
guarded shell-login blocks, the stable commands under `$HOME/.local/bin`, and
the install receipt. It stops no service, closes no session, and deletes no
conversation, journal, or private state. Two optional flags remove the installed
release code and the configuration root, and even those leave every retained
data directory in place.

Order matters. Disabling the services is a separate step that comes first, and
on macOS uninstall enforces it.

## Check what is running

```bash
sp list
sp health
session-kit services status
```

`sp list` shows the managed sessions a service change would affect.
`services status` reports each Session Kit job: `systemctl --user status` for
the shpool socket and the reaper timer on Linux, or one `LOADED`/`UNLOADED` line
per LaunchAgent on macOS. Close or move any session you still want before going
further. Nothing below closes one for you.

## Disable the services

```bash
session-kit services disable
```

On macOS this step is mandatory and uninstall enforces it twice over. Uninstall
refuses while any of the three Session Kit labels is loaded, and refuses again
while any of the three plist files remains in `~/Library/LaunchAgents` — even
unloaded, even after a manual `launchctl bootout`. Removing those files by hand
is the case `services disable` exists to replace: before it touches an active
plist it proves that file is byte-identical to the template this installation
generated, or that it matches the digest recorded in the private LaunchAgent
receipt. An edited or unowned plist is refused rather than deleted.

`services disable` on macOS holds the Session Kit creation lock, proves shpool
reports zero sessions, unloads the watchdog and the reaper, proves none
appeared, and only then unloads shpool. Any of those steps can refuse
and leave shpool running. The two proofs bracket the window in which a session
could appear between them, because unloading the shpool job stops the daemon and
takes the sessions it holds with it.

On Linux, `services disable` runs `systemctl --user disable --now
shpool-reaper.timer session-kit-subagent-sweep.timer shpool.socket`, which
disables and stops the socket, the hourly cleanup timer, and the five-minute
sub-agent sweep. It makes no empty-session proof and never signals shpool
directly. What happens to a running daemon depends on how that daemon was
started: the shipped `shpool.service` declares `Requires=shpool.socket`, so a
systemd-started daemon is stopped along with the socket, while a daemon started
outside the user manager is not under its control. Confirm the outcome with
`sp list` rather than assuming either one.

## Remove the integration

```bash
session-kit uninstall
```

Before deleting anything, uninstall recovers any pending lifecycle transaction,
validates the lifecycle roots, and checks every command it is about to remove
against the active release: each helper launcher must be byte-identical to that
release's `deploy/session-kit-launcher`, and `session-kit` must match that
release's own copy. A launcher that was edited, replaced, or turned into a
symlink is refused and uninstall stops. Session Kit does not delete a file it
cannot prove it wrote.

The default uninstall removes:

- Session Kit's provenance-marked `UserPromptSubmit` handler from Claude and
  Codex configuration; unrelated handlers in the same matcher group are
  retained, and a file, empty `hooks` object, or empty event list that existed
  before installation is restored byte-for-byte;
- the guarded login block from `.bashrc`, and on macOS from `.bash_profile` and
  `.zshrc` as well;
- the private integration marker;
- `kit`, `sp`, `shpool_login`, `shpool_status`, `shpool_reaper`,
  `codex_resume_here`, and `session-kit` from `$HOME/.local/bin`;
- on Linux, the copied systemd user units in `~/.config/systemd/user/`;
- the install receipt, once the managed files above are verified and gone.

It does not close sessions, delete provider conversations, remove shpool, remove
the installed release code, remove configuration, or delete private state. It
starts, stops, restarts, and signals nothing.

On Linux the unit files go with the uninstall, and the enablement is checked
first. A unit systemd still holds a link to -- `enabled`, `enabled-runtime`,
`linked`, or `linked-runtime` -- stops the uninstall with the command that
clears it, because deleting a unit file does not deactivate the unit and a
removed file behind a live enablement is worse than a file left in place. A unit
that is merely *running* does not stop it: that daemon is holding live sessions,
removing its file does not stop the process, and the way out of an installation
must not go through killing every session in it. Uninstall names any unit it
leaves running. A unit path that is a symlink is refused, not followed, and
nothing is removed until every path has passed.

## Remove the code and configuration

```bash
session-kit uninstall --purge-code --purge-config
```

`--purge-code` removes the installed release root,
`$HOME/.local/lib/session-kit`, including every retained release and the
`current` pointer. `--purge-config` removes the configuration root,
`${XDG_CONFIG_HOME:-$HOME/.config}/session-kit`, which holds `projects.tsv`,
`inventory.json`, and on macOS the generated LaunchAgent templates.

Each flag requires proof of ownership before it deletes a directory: a current
install receipt whose recorded roots name that exact path, plus a
`.session-kit-owned.json` marker inside the target naming the same path and
kind. A missing receipt, an outdated receipt schema, a mismatched marker, or a
symlinked target is refused. Neither flag removes a directory it cannot
attribute to this installation.

Pass these flags on the uninstall invocation itself. Afterwards the receipt and
the `session-kit` command are both gone, so there is no supported path back to a
proved purge.

Disable the services before purging code. The installed units execute paths
inside the root being deleted — on Linux the watchdog unit runs
`~/.local/lib/session-kit/current/bin/session_kit_watchdog` and the reaper unit
runs `~/.local/bin/shpool_reaper` — so purging while they are enabled leaves
them enabled and pointing at nothing. Do not purge code while a managed session
still depends on an installed release.

Both purge options retain state, journals, archives, launch records, logs,
backups, and provider storage.

## What stays behind

By default Session Kit keeps:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/
${XDG_STATE_HOME:-$HOME/.local/state}/session-kit/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal-recovery/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-archive/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-start/
```

Shell startup backups, lifecycle transaction backups, and on macOS the
LaunchAgent job logs all sit beneath the Session Kit state root. Between them
these locations can hold exact IDs, private paths, action receipts, service
history, logs, and terminal content.
[Security and local data](security-and-data.md) describes what each class
contains.

Some files the installer wrote sit outside all of those roots.
`~/.config/shpool/config.toml` is created only when it is absent, and it is the
account's own file from that moment on. The kit's Codex
theme files, `${CODEX_HOME:-$HOME/.codex}/themes/sk-*.tmTheme`, are installed
into the Codex home so Codex can read them; remove them by hand if you want them
gone. The Claude Code title hook and status line at
`~/.claude/hooks/nameintent_title.sh` and `~/.claude/statusline.sh` are owned by
the kit. `claude-integration.json` records their installed digests. Uninstall
removes them only while their content still matches that record, keeps both an
edited file and its ledger entry, and removes only the kit's exact settings
registrations. `claude-statusline-backups.json` records the operator's prior
`statusLine`; uninstall restores its current recorded value instead of deleting
it. Edited files and unrelated hooks stay in place. The generated per-account
quota probes and caches beneath `~/.claude/cache/session-kit-quota/` are removed
with the integration; provider credentials and conversation storage are not. See
[the Claude Code integration notes](../config/claude/README.md).

## Removing retained data

Deleting that data is a separate, manual decision, because only the owner can
judge what a journal or a recovery record is still worth. Before deleting
anything, prove that no live process writes to the target, list each exact path
and data class, and make and verify a backup if recovery matters. Reject
symlinks, unexpected owners, and broad path targets, and never sweep Claude Code
or Codex provider storage in by accident — those directories hold the
conversations themselves, not Session Kit's view of them.

Session Kit provides no broad "delete everything" command.
