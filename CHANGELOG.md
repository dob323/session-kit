# Changelog

All notable changes to Session Kit are documented here.

## [Unreleased]

### Fixed

- The documented install command could not run. `gh release download` without a
  tag requires `--pattern` or `--archive`, so the version-free form printed a
  flag error instead of downloading anything. The README and `docs/install.md`
  now pass `--pattern 'session-kit-*'`.
- The README, `docs/install.md`, `SECURITY.md`, and `CONTRIBUTING.md` no longer
  say beta releases are published as GitHub prereleases. They are ordinary
  releases, so `releases/latest` resolves to the current beta and the install
  no longer has to name a version.
- The README claimed Session Kit does not isolate work in a git worktree. It
  does: a delegated session is given its own worktree on its own branch and
  hands it back when it closes. The front page now documents that, and the
  "does not do" list keeps only what is true.
- `tools/publish-release` checked version references under `docs/*.md`, which
  never reached `docs/maintainers` or `docs/migrations`. The maintainer release
  process had named `v0.4.0` ever since, while telling its reader to search
  nested directories by hand. The check now walks them.
- `tools/check-doc-links` now resolves a `#fragment` against the headings of
  the document it points into. A renamed heading used to leave every link to it
  scrolling nowhere, with nothing failing.
- Two shipped documents were reachable from nothing. The legacy-install
  migration is linked from `docs/install.md`, and the maintainer release
  process from `CONTRIBUTING.md`.
- The bug-report and pull-request templates offered `Dashboard` and
  `Kill confirmation`. `docs/voice.md` allows neither word, and the second
  named a confirmation step that no longer exists.

### Added

- `CODE_OF_CONDUCT.md`, the Contributor Covenant 2.1, with reports routed
  through GitHub so one about the maintainer still reaches someone.
- The README's pictures are generated from the running picker rather than
  drawn. `tools/render-readme-picker` captures the session list and the
  needs-you screen in one run of the real picker, `tools/render-readme-figures`
  lays out the two diagrams, and `tools/render-readme-social` builds the
  GitHub social card. A render is refused if any text escapes its box, if the
  command bar offers a key the README does not document, or if the two picker
  screens disagree about which sessions are waiting.
- `docs/assets/mark.svg`: the project mark, which is the picker's own prompt.

## [0.4.2] - 2026-08-18

### Added

- Short project directory names: a Claude session launched at the registered
  root of a project shortcut now stores transcripts and auto memory under the
  shortcut's alias (Claude Code 2.1.234's `CLAUDE_CODE_PROJECT_DIR_NAME`).
  The export is proved per launch — launcher version, unambiguous registry,
  valid alias, no directory conflict — and an existing munged directory is
  renamed in one atomic move only while no other session of that profile runs
  inside the root. `session-kit doctor` gained a `project-dir-names` check;
  `SESSION_KIT_PROJECT_DIR_NAME=off` disables the feature.

### Security / data safety

- A second Unix account's shpool daemon no longer disables this account's
  kit. Daemon selection now rules out any daemon owned by another account
  before it asks who holds the listener: their `/proc/<pid>/fd` is
  unreadable, which the uniqueness rule read as "cannot establish", so
  `daemon_generation` went null and every session became unprovable —
  `sp new` produced an unresolved row and `sp go`/`sp close` refused. A
  listener under `/run/user/<uid>` is mode 0700, so another account's daemon
  was never a candidate. Census rows now carry the owning uid; a row without
  one is still treated as a candidate.
- The project-directory migration closed three lose-work races found by a
  post-install review lane: the liveness scan now re-runs on the far side of
  the rename and undoes it if a raw same-profile launch appeared in the gap;
  a same-profile provider in the launcher's own ancestor chain counts as a
  live session (only shells and launch plumbing are excused); and the Claude
  version proof is bound to the exact executable the shell then runs — an
  armed export launches the proved realpath, never a re-resolved `claude`.
  An alias path occupied by a regular file or symlink now refuses instead of
  exporting an unusable name.
- Enrolment writes `autoContinueAtUsageLimit: false` even when the source
  Claude profile has no settings file at all — the default is a promise about
  every managed profile, not a transform applied only when there was
  something to copy.
- The project-dir-name helper reads the same projects registry every other
  consumer does (`SESSION_KIT_PROJECTS_FILE`, then the XDG location), and its
  doctor honours `SESSION_KIT_ACCOUNT_ROOT`. The watchdog's lock-jam notice
  no longer promises that ending the holder is universally safe; it says what
  actually happens.

### Fixed

- A self-name could deadlock the whole estate: its write-time revalidation ran
  a fresh collection while holding the name-store locks, and collecting can
  re-acquire `config.lock` through a second descriptor — the process then
  waits forever on its own lock while every collector, picker refresh and
  name attempt queues behind it (observed live for twenty minutes on
  2026-08-17). Revalidation now re-reads only the process table; the one
  snapshot is taken before any lock.
- The watchdog now detects that class of jam: a snapshot that has stopped
  refreshing while a kit state lock has queued waiters is reported once, with
  the holding pid and the safe remedy.
- `tests/run` drops itself to the lowest CPU and idle IO priority, so a full
  suite on a live box can no longer starve the picker and sessions.

### Changed

- Enrolled Claude profiles now start with `autoContinueAtUsageLimit` off, so
  a managed account never resumes spending by itself when a usage window
  resets; turn it back on per profile in Claude's `/config`.
- The test sandbox guard now drops an inherited `CLAUDE_CONFIG_DIR`,
  `CODEX_HOME`, and `CLAUDE_CODE_PROJECT_DIR_NAME` from fixture-homed
  children, so a suite that starts a real provider CLI can no longer write
  transcripts into a real account profile.

## [0.4.1] - 2026-08-17

### Changed

- The sub-agent sweep's background-shell pass was hardened after live audits:
  any live descendant now refuses closure outright (even a sleeping zero-CPU
  child), the harness-shell fingerprint is anchored (a marker inside arbitrary
  `-c` text never qualifies), the shell's own CPU counters veto closure, a
  shell owning its terminal's foreground process group is never touched, and
  delivery pins and stops the shell, re-proves every fact while it is frozen,
  then signals and resumes it. A closing signal that reached its target is
  reported delivered even when the target is reaped before the resume step.
- A Claude-derived placeholder name (`nameSource: "derived"`) is never adopted
  as a human rename: the automatic title now displays over it, and the kit's
  title push updates per-PID session records inside every enrolled account
  root, not only the ambient profile. Push lag ranks as pending, never as a
  human rename; a real human rename still wins outright.
- The title push reaches a profile's transcript even when that profile has no
  sessions registry yet; the sessions-directory narrowing had silently skipped
  fresh account roots.

### Fixed

- The public CI suite is genuinely green: thirteen baseline test failures were
  root-caused and repaired against the code's actual contracts, shellcheck is
  scoped to real shell scripts by shebang, pty-dependent tests are pinned, and
  the Python tree passes ruff and mypy clean.

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

[0.4.2]: https://github.com/dob323/session-kit/releases/tag/v0.4.2
[0.4.1]: https://github.com/dob323/session-kit/releases/tag/v0.4.1
[0.4.0]: https://github.com/dob323/session-kit/releases/tag/v0.4.0
