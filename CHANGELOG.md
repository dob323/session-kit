# Changelog

Session Kit follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
