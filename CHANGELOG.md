# Changelog

All notable changes to Session Kit are documented here.

## [0.4.0] - 2026-08-16

### The picker is one screen again

- Removed the supervisor UI, direct session messaging, and the event feed.
  Session Kit now has one picker for day-to-day work and `sp` for commands;
  system notices remain a separate guarded route.
- Unified visible language across the picker, commands, installer, doctor,
  watchdog, and documentation. Refusals state the fact and the way forward;
  cancellations say `Nothing changed.`; errors use the `session-kit:` prefix.
- Replaced the old attention wording with `needs you`, and replaced unreadable
  model, account, and state values with the single placeholder `pending`.
- Added `question` for a Claude blocking prompt that is proven open right now.
  It outranks `needs you`; Codex rows do not claim it until equally exact
  provider evidence exists.
- Added transcript-aged `idle`. A session that would otherwise say `needs you`
  becomes idle after its transcript path, size, and nanosecond modification
  time stay unchanged for the configured window. The default is 30 minutes;
  an invalid or unreadable setting disables the label instead of guessing.
- Standardized row order: ready before open elsewhere, then `question`,
  `needs you`, `working`, and `idle` inside each availability group.
- Added child shells and workers at least one hour old to `sp detail`, including
  the age of each exact live process.
- Made Enter take the likely choice on every screen. Home Enter opens the top
  row or starts a new session on an empty list; New session defaults to Claude
  Code; a session open elsewhere defaults to moving it here. `b` goes back on
  every screen where it is not typed text.
- Kept a typed session number through close and other number-based actions, so
  the result remains anchored to the selection after the list refreshes.
- Reordered footer segments by survival priority at narrow widths: Enter,
  number selection, kill, new, more, needs you, help, history, then leave.
  The Enter segment says `↵ open <number>` or `↵ new` according to the visible
  list.
- Folded sub-agent sessions under their parent instead of listing each worker
  as an ordinary session. Whole-machine totals still count them.
- Added bracketed-paste handling to picker prompts so a pasted block is one
  literal input rather than a queue of commands.
- Made Ctrl-C abandon the current picker line cleanly.
- Preserved the filter, page, jump marker, and typed selection through an
  action and refresh. A completed close reports what closed before repainting.
- Added waiting duration to rows that need attention, and made a resize repaint
  without losing the current view.
- Removed process launches from the live typing loop. Filtering now stays
  responsive even while the machine is busy.
- Added safe live-release pickup. A running picker notices a newly activated
  release, re-execs the new launcher at a refresh boundary, and restores its
  view. If the target launcher is degraded, the current picker stays running.

### Workers close themselves conservatively

- Added a dedicated five-minute sub-agent sweep. A finished provider worker is
  eligible fifteen minutes after its own transcript stops moving, so normal
  cleanup lands within about twenty minutes.
- Based worker activity on transcript size and nanosecond modification time.
  CPU is deliberately not evidence: an idle provider process can continue to
  consume CPU, while a long silent computation may not. A swept conversation
  keeps its transcript and can respawn when continued; raise
  `SESSION_KIT_SUBAGENT_IDLE_MINUTES` if a workload needs a wider window.
- Made an unreadable window disable the sweep. A rule change starts a fresh
  clock, a close decision does not survive reboot, and no pre-rule observation
  can satisfy a newer rule.
- Bound sweep candidates to the provider's worker packaging so a process
  started by the user cannot be reached accidentally.
- Applied `~/.no_shpool_reaper` to this pass too, and added a dedicated
  `subagent-sweep` doctor line showing the effective window and last pass.

### Accounts and notices

- Added guarded, opt-in account auto-switching for a conversation whose active
  account has exhausted its usable quota. It never enables an account, moves a
  conversation at most once, preserves a configurable reserve, requires the
  live provider identity to agree with the record, and never downgrades a
  model silently. Use `sp account-auto-switch --apply` to opt in.
- Marked enrolled Claude profiles as having completed provider first-run offers
  so a new managed session does not stop on an unrelated setup prompt.
- Kept the Codex identity probe's input open until `account/read` answers.
- Added `session_kit_notice` as the route for system notices that are not a
  session waiting for input. Terminal delivery requires one exact attached
  session plus recent human input; otherwise a configured away transport is
  used. With no transport, the notice is logged as unwired and the pass fails
  visibly rather than claiming delivery.
- Batched one sweep into one message naming at most five sessions, and retained
  delivery debt until a transport reports success.

### Doctor, install, update, and rollback

- Added `units-loaded`, `release-running`, `journals`, `transcripts`, and
  `subagent-sweep` doctor checks. They distinguish installed definitions from
  the code actually running and resolve recent provider transcripts through
  every supported root.
- Preserved the rollback path when a release adds a systemd unit.
- Made activation refresh the service definitions it writes. Linux reloads the
  user manager, enables only timers systemd has never seen, and try-restarts
  the running watchdog; macOS refreshes an already-loaded kit watchdog.
  The session manager is never restarted during activation.
- Precompiled and validated installed Python bytecode to reduce command startup
  time without accepting a cache built for the wrong interpreter.
- Fixed release collection on systemd-user hosts and made every skipped or
  failed pass produce a useful result.
- Preserved Claude status-line integration through install, update, rollback,
  and uninstall. The installer records provider registrations in
  `claude-integration.json`, keeps replaced settings in
  `claude-statusline-backups.json`, restores the pre-existing value on removal,
  and refuses an unrecognised local edit unless the operator explicitly uses
  `--force`.
- Added collection-floor seeding for older installations. The installer copies
  the exact proven current value once, does nothing when a valid floor already
  exists, and refuses with an exact recovery command when it cannot establish a
  safe seed.
- Made rollback validation use the target release's compatibility. When an old
  validator cannot read a modern launch record, the supported escape is the
  target release's own rollback tool, or waiting out or quarantining the record
  before retrying.
- Kept the management launcher on the newest verified release so it can recover
  an interrupted transaction even while an older runtime is selected.

### Identity, colour, and provider lifecycle

- Assigned an enrolled session's colour before its first visible provider
  frame and pushed its name without requiring a restart.
- Bound every mutable action to the exact daemon generation that supplied the
  proof. Daemon churn is never treated as absence, and termination is pinned to
  the exact shell process rather than a reused name.
- Made `r` reopen any recoverable conversation and report the precise reason
  when it cannot. A reopen re-proves the daemon generation immediately before
  launch.
- Made provider-exit handling single-shot. A clean provider exit closes the
  managed session and leaves its conversation in Closed sessions. A crash
  reopens once; a second failure stops the loop and either records a recoverable
  close or leaves the session open with the refusal reason.
- Made Ctrl-C cancel the provider-exit question and ensured a restored
  conversation follows the same exit rules as a new one.
- Released attach snapshots after use and stopped background collectors on
  every ordinary picker exit.
- Stopped recording an alert as delivered when its transport drops it.

### History

- Added incremental journal rendering through a terminal screen model, turning
  raw recordings into settled readable text while retaining checkpoints.
- Added picker history for any visible row and made search prefer rendered
  text, with a clear fallback when only the captured form is readable.
- Kept closed history reachable beyond the normal unattended size ceiling
  through the explicit large-ledger commands.
- Preserved the operator's journal choice across update and rollover.

### shpool patches

- Added patch `0005`, which preserves the managed shell's exit status through
  attach.
- Added patch `0006`, which coalesces bursts of client resize events.

[0.4.0]: https://github.com/dob323/session-kit/releases/tag/v0.4.0
