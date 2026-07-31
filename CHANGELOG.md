# Changelog

Session Kit follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Made the public beta Linux-only and fail closed on unsupported platforms.
- Changed terminal journals to opt-in and off by default.
- Kept the managed shpool terminal alive after Claude Code or Codex exits.
- Added a clear provider-exited state with exact reopen, keep, shell, and close
  choices.
- Limited automatic close to exact provider-exited terminals that satisfy every
  safety predicate continuously for 72 hours after timer enablement.
- Simplified dashboard wording and applied consistent semantic color categories.
- Removed internal IDs from normal rows while keeping them in detail, JSON, and
  explicit search views.
- Made `k <number>` the documented kill shortcut with exact-ID confirmation.
- Reduced normal log data and documented private reporting requirements.

### Release preparation

- Added reachable-history secret scanning and local documentation link checks.
- Added public-export completeness checks.
- Added reproducible release archive, checksum, and provenance preparation.
- Added optional shpool patch apply and build checks.
- Added the Apache License 2.0 text for the shpool-derived patch.

## [0.1.0] - Unreleased beta

### Added

- Unified local inventory for shpool, Claude Code, Codex, and shell sessions.
- SSH picker with boot-scoped terminal numbers and task-focused names.
- Structured `needs your reply` and optional-reply states.
- Exact provider resume, recovery, and fork operations.
- Optional local terminal journals, off by default.
- Guarded open, move, kill, repair, and cleanup actions.
- Immutable installed releases with update and rollback.
- Read-only install preflight, doctor, login enable and disable, and uninstall.
- Report-only local health checks.

`v0.1.0` has not been tagged. Current `main` is the only supported pre-release
line.
