# Changelog

Session Kit follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Split the session palette in two, one per provider, and stopped a same-colour
  repeat from surviving inside either. Two live rows could show one colour for
  two separate reasons, and fixing one left the other. Claude and Codex drew
  from a single eight-name palette, so they collided across providers; and the
  colour comes from an identity hash, which collides on its own well before the
  names run out — measured on live state, eight Claude sessions landed on seven
  colours, two on pink with blue unused.

  Claude Code's `/color` accepts exactly `red`, `blue`, `green`, `yellow`,
  `purple`, `orange`, `pink`, and `cyan`. Twenty-two names were probed against
  Claude Code 2.1.223 with known-good and known-bad controls; every other name
  is rejected, and `gray`/`grey` resolve to `default`, which is no colour. That
  palette is therefore fixed from outside. Codex resolves its colour from an
  `sk-*.tmTheme` file this kit ships and applies no allow-list, so it now has
  six names of its own — `lime`, `magenta`, `silver`, `sand`, `sky`, `sea` —
  that Claude Code cannot use. A Claude window and a Codex window can no longer
  land on the same colour at all.

  Within a palette, a session keeps its identity-hash colour unless a live
  session of the same provider already holds it, in which case it takes the
  next free name in palette order. When every name is taken the hash colour
  applies again and the repeat is allowed, because there is no free colour to
  give; the result stays a function of identity rather than of arrival order,
  so a session that has to share shares with the same partner every time.

  Existing Codex sessions carrying a stored override that names a colour no
  longer in the Codex palette need no migration pass. The override stops
  matching the in-force palette, so it is ignored and the session re-hashes
  into the palette that now applies.

- Release payload schema 5 requires `lib/sessionkit_inventory/colors.py`, which
  is where both palettes are now declared. Schemas 1 to 4 are unchanged, so a
  rollback still validates the payload a pinned older release shipped.

### Added

- `sp color reconcile`, which settles every live same-provider colour collision
  in one pass instead of waiting for each session to relaunch into the new
  palette. It writes an override only where palette order moved a session off
  its own identity hash, pushes each moved colour so an open Claude window
  picks it up at its next start or resume, and clears stored colours that name
  a colour outside the palette now in force. Repeating it changes nothing: rows
  are settled in a fixed identity order and each prefers the colour it already
  shows, so the second pass finds every preference free and writes no file.

- Six Codex theme files, `sk-lime`, `sk-magenta`, `sk-silver`, `sk-sand`,
  `sk-sky`, and `sk-sea`. The eight Claude-named themes stay shipped so a
  rollback to 0.1.6 still finds every theme it installs.

## [0.1.6] - 2026-08-06

### Fixed

- Added optional shpool patch `0004`, which fixes a detach deadlock in shpool
  0.11.0 that can make every managed session unreachable at once. Upstream
  `handle_detach` holds the global session-table lock across an unbounded send
  and receive on two rendezvous channels, so one client whose socket has stopped
  draining parks the lock for every other session. The patch resolves under the
  lock, drops it, performs a bounded handshake, then re-locks briefly for
  bookkeeping, matching the pattern upstream already uses elsewhere in the same
  file. It applies to pristine `v0.11.0` independently of patches `0001`-`0003`.

### Changed

- Corrected the write-up for patch `0001`. It addresses heartbeat acknowledgement
  timeouts and would not have prevented the detach deadlock; the notes now say so
  and point readers at `0004` first.
- The watchdog now distinguishes an unset notifier from a broken one. With
  `SESSION_KIT_WATCHDOG_NOTIFY` unset it logged that the empty string was not
  executable, which reads like a misconfigured path rather than an absent
  configuration. Documented the variable, including that leaving it unset means
  no alert reaches anyone.

## [0.1.5] - 2026-08-05

### Changed

- Ran the full picker and provider-exit test suites on macOS CI through the
  native Darwin process adapter, replacing a Linux-only harness that read
  process generations straight from `/proc`; the one test that genuinely
  needs `/proc` now skips on Darwin instead of failing.

### Fixed

- Removed a picker repaint guard that conditioned redraws on a terminal input
  probe which always reports an empty queue in canonical mode; repaints never
  consume queued characters, so a half-typed search now survives a live menu
  repaint on Linux and macOS alike.
- Allowed read-only install and doctor probes to reach the current user's
  systemd manager through its documented local-machine transport when the
  direct private socket is unavailable, while reporting the degraded socket as
  a warning and leaving service-control commands fail-closed.
- Made Claude's persisted `agent-name` record outrank its later generated
  window label, so an exact `sp self-name` converges to ready instead of
  remaining pending after a successful native write.
- Reserved and persisted an exact live-palette color after a failed Claude
  pre-bake, before the detached session can be attached, instead of falling
  back to a collision-prone identity hash.
- Added warning-only migration audits for bounded provider versions, private
  Codex themes, naming instructions and hooks, active kill switches, and the
  private release acceptance record.
- Excluded inaccessible provider project paths while retaining readable shared
  repositories, and honored `CODEX_HOME` consistently during discovery and
  theme installation.

## [0.1.4] - 2026-08-03

### Added

- Added bounded first-install discovery and import of existing Claude Code and
  Codex project folders, plus rerunnable `session-kit projects` commands.
- Added collision-safe provider-specific aliases, owner-only backups, and
  explicit noninteractive import control.

### Fixed

- Associated a managed Codex App Server's single open remote-TUI rollout with
  its Session Kit terminal, restoring the exact thread title and launch color.
- Kept editor rollouts ineligible for ordinary Codex processes and refused
  ambiguous app servers with multiple open threads.
- Deferred Codex in-window title refreshes while the exact provider is attached
  or working instead of labeling an unsafe restart as immediately pending.
- Derived Codex child-thread activity from each exact rollout so completed
  subagents no longer inflate the active count.
- Kept the internal Codex bar-refresh marker out of normal dashboard rows when
  the saved session title is already correct.
- Targeted an App Server title refresh at its exact remote TUI instead of the
  server process, preserving the resume socket and managed thread.

## [0.1.3] - 2026-08-03

### Fixed

- Ignored stale Claude agent records whose process no longer has a live Darwin
  generation, so an exiting provider cannot block later guarded session
  actions on macOS.

## [0.1.2] - 2026-08-03

### Fixed

- Moved every runtime `mktemp` suffix before the replacement characters so
  snapshots and proof files are unique on BSD `mktemp` as well as GNU
  `mktemp`.
- Restored macOS open, takeover, close, prune, reaper, watchdog, login, and
  provider-proof paths that could otherwise reuse a literal `XXXXXX` filename
  or fail after the first call.
- Added a repository-wide regression that rejects any runtime `mktemp`
  template with characters after its final `XXXXXX`.

## [0.1.1] - 2026-08-03

### Fixed

- Preserved pipeline input through the cross-platform timeout wrapper so the
  hidden Claude bootstrap receives its native `/color` command on Linux and
  macOS.
- Reconciled Claude transcript auto-titles with the provider's `agent-name`
  record without replacing an explicit `/rename`.
- Passed the hydrated Claude name through the native `--name` option when an
  exited provider is reopened.
- Kept the dashboard title state pending until the live Claude process reports
  the same visible name; an already-running Claude TUI is never reported as
  repainted by an external storage write.

## [0.1.0] - 2026-08-03

### Added

- Unified local inventory for shpool, Claude Code, Codex, and shell sessions.
- SSH picker with boot-scoped terminal numbers and task-focused names.
- Structured `needs your reply` and optional-reply states.
- Exact provider resume, recovery, and fork operations.
- Optional local terminal journals, off by default.
- Guarded open, move, close, repair, and cleanup actions.
- Immutable installed releases with update and rollback.
- Read-only install preflight, doctor, login enable and disable, and uninstall.
- Report-only local health checks.
- Native Linux `/proc` and Darwin process-identity adapters.
- Transactional install, update, rollback, and uninstall on Linux and macOS.
- Per-user launchd definitions and explicit service lifecycle commands on
  macOS.
- Reachable-history privacy scanning, public-export completeness checks, and
  reproducible release archives with checksum and provenance files.

### Changed

- Made terminal journals opt-in and off by default.
- Kept the managed shpool terminal alive after Claude Code or Codex exits.
- Added a clear provider-exited state with exact reopen, keep, shell, and close
  choices.
- Limited automatic close to exact provider-exited terminals that satisfy every
  safety predicate continuously for 72 hours after timer enablement.
- Simplified dashboard wording and applied consistent semantic color categories.
- Removed internal IDs from normal rows while keeping them in detail, JSON, and
  explicit search views.
- Made `k <number>` the documented close shortcut with an exact-target safety
  display and no extra prompt.
- Reduced normal log data and documented private reporting requirements.
- Required macOS 14 or newer, Python 3.11 or newer, Homebrew Bash 4 or newer,
  and shpool 0.11.0 for the macOS beta.

### Known limitations

- macOS services require the same user to be logged into the Mac desktop; the
  beta does not install a privileged or headless system daemon.
- Watchdog repair requires Linux daemon-thread evidence. The macOS watchdog is
  report-only.
- Provider storage formats and command interfaces remain version-sensitive;
  versions outside the release acceptance evidence are best-effort.
