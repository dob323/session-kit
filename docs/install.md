# Install Session Kit

Session Kit is a Linux-only `v0.1.0` beta. Install from a reviewed Git commit
under a single-user account where provider conversations are recoverable.

```bash
git clone https://github.com/dob323/session-kit.git
cd session-kit
./install.sh --check
tests/run
./install.sh
session-kit doctor
```

`./install.sh --check` is read-only. The installer copies files and systemd
user-unit definitions but does not start, stop, restart, or enable a service.

## Requirements

- Linux with `/proc`
- systemd user services
- Bash
- Python 3.10 or newer
- shpool 0.11.0
- Claude Code, Codex, or both

The preflight also checks required command-line tools such as `flock`, `git`,
`journalctl`, `mktemp`, `pgrep`, `ps`, `script`, `sha256sum`, `stat`, and
`timeout`.

macOS, another init system, or a missing process view is unsupported and must
stop before installation. No preview switch changes that beta support policy.

## shpool

Use official shpool 0.11.0 by default. Session Kit includes an optional source
patch for one heartbeat acknowledgement timeout. It is not installed
automatically. Review [the patch guide](../shpool-patch/README.md).

With a current Rust toolchain:

```bash
cargo install shpool --version 0.11.0 --locked
```

Replacing a running shpool binary normally requires a daemon restart, which
ends its managed terminal processes. The Session Kit installer never replaces
shpool or restarts its daemon.

## What installation changes

The installer:

1. creates an immutable release at
   `$HOME/.local/lib/session-kit/releases/<commit>`;
2. selects it through the `current` link;
3. installs stable commands under `$HOME/.local/bin`;
4. creates missing private configuration and state;
5. copies systemd user units without enabling them;
6. asks before adding the guarded Bash login block;
7. backs up a Bash startup file before changing it;
8. asks whether to enable terminal journals, defaulting to off;
9. leaves health notifications off.

It does not change provider storage, start a service, restart shpool, attach to
a session, or close a session.

Noninteractive installation leaves login integration and journals off unless
their explicit flags are supplied.

## Optional components

- **SSH picker:** opens for an interactive Bash login after explicit enablement.
- **Terminal journals:** record terminal bytes for new managed sessions after
  explicit opt-in.
- **Cleanup observer:** tracks exact eligible provider-exited terminals. It can
  close only after the timer is enabled and every safety predicate has remained
  unchanged for 72 continuous hours.
- **Watchdog:** runs in report-only mode by default. The advanced repair mode is
  an explicit opt-in that can close and relaunch a proved-broken terminal.
- **Notification command:** remains disabled unless explicitly configured.

## Activate safely

After installation:

```bash
session-kit doctor
session-kit enable-login
```

Review copied user units before enabling any timer or service. Activating a
Session Kit helper does not require a shpool restart.

Use a new test session first. Confirm that disconnecting SSH leaves it running,
a provider quit leaves the managed terminal alive, exact reopen works, and
`k <number>` requires confirmation.

## Evaluate without installing

The repository test suite uses temporary fixtures and does not contact a live
shpool daemon:

```bash
tests/run
```

Do not run active-session commands merely to inspect the source.
