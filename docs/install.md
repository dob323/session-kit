# Install Session Kit

## Current status

Session Kit is a `v0.1.0` beta candidate. Install it from a reviewed Git clone,
not by copying individual files into a live setup.

```bash
git clone https://github.com/dob323/session-kit.git
cd session-kit
./install.sh --check
tests/run
./install.sh
session-kit doctor
```

`./install.sh --check` is read-only. Installation copies files and systemd user
unit definitions but never starts, stops, or restarts a service.

## Supported target

The supported target is a single-user Linux account with:

- systemd user services;
- Bash;
- `/proc`;
- Python 3.10 or newer;
- shpool 0.11.0;
- Claude Code, Codex, or both.

Required command-line tools include `flock`, `git`, `journalctl`, `mktemp`,
`pgrep`, `ps`, `script`, `sha256sum`, `stat`, `timeout`, and standard POSIX
file utilities.

The preflight detects the core requirements and stops before changing anything
when one is missing.

## shpool choice

Use the official shpool 0.11.0 build by default. It may be installed with Cargo
or built as described in [../extras/build-static-binary.md](../extras/build-static-binary.md).

Session Kit includes an optional source patch for a heartbeat acknowledgement
timeout. The patch is not part of the default installation. Review its evidence,
limits, build instructions, license notice, and rollback effect in
[../shpool-patch/README.md](../shpool-patch/README.md).

Installing or replacing the shpool binary can require a daemon restart. A
restart ends the terminal processes managed by that daemon, even when provider
conversations can later be resumed. The Session Kit installer must never
restart an existing daemon without explicit approval.

## What installation changes

The installer:

1. creates an immutable release under
   `${HOME}/.local/lib/session-kit/releases/<commit>`;
2. atomically selects it with the `current` link;
3. installs stable commands under `${HOME}/.local/bin`;
4. creates private XDG configuration and state files from examples only when
   those files are absent;
5. copies systemd user units on Linux without enabling them;
6. asks whether to add one guarded Bash block for the SSH picker;
7. backs up an existing Bash file before first enabling that block;
8. asks whether to keep local terminal journals, defaulting to on;
9. leaves the watchdog report-only and notifications disabled.

The installer does not install or replace shpool, alter provider storage, or
restart the shpool daemon.

## Optional components

The installer should offer these separately:

- **SSH picker:** opens automatically in an interactive login shell.
- **Guided journals:** records new managed terminal output locally. Default on
  for the guided setup.
- **Reaper reports:** finds old, disconnected, empty-shell candidates. It never
  removes them automatically.
- **Watchdog reports:** reports manager and terminal-health evidence. It never
  repairs automatically under the public policy.
- **Notification command:** disabled unless the user supplies and tests a local
  command.

## macOS preview

macOS is an experimental, opt-in core preview. Set
`SESSION_KIT_MACOS_PREVIEW=1` only in a dedicated test account and read
[../macos/README.md](../macos/README.md) first.

The preview does not include systemd services, watchdog repair, reaper
candidate generation, pruning, or feature parity. It must pass the real-Mac
acceptance gate before it is described as supported.

## Evaluate the source safely

Development tests are isolated:

```bash
git clone https://github.com/dob323/session-kit.git
cd session-kit
tests/run
```

Do not run deployment or active-session commands merely to evaluate the
repository.
