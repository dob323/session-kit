# Install Session Kit

Session Kit `v0.1.0` is a public beta for Linux with systemd and macOS 14 or
newer. Install the accepted release artifact under a single-user account where
provider conversations are recoverable.

```bash
mkdir session-kit-download
cd session-kit-download
gh release download v0.1.0 --repo dob323/session-kit
if command -v sha256sum >/dev/null; then
  sha256sum --check session-kit-*.sha256
else
  shasum -a 256 --check session-kit-*.sha256
fi
tar -xzf session-kit-*.tar.gz
cd session-kit-*/
./install.sh --check
./install.sh
session-kit doctor
```

Without the GitHub CLI, download the `.tar.gz`, `.sha256`, and
`.provenance.json` assets from the
[`v0.1.0` release](https://github.com/dob323/session-kit/releases/tag/v0.1.0),
and put them in one empty directory. The checksum file covers the archive. Use
`sha256sum --check` on Linux or
`shasum -a 256 --check` on macOS. The provenance file records the exact source
commit and public-tree digest.

Cloning `main` is the development path, not the normal installation path. If
you use it, review and test the exact commit before installation.

`./install.sh --check` is read-only. The installer copies files and user-service
definitions but does not start, stop, restart, or enable a service.

## Requirements

- shpool 0.11.0;
- Claude Code, Codex, or both;
- a single trusted Unix account;
- enough local access to run per-user services.

Linux additionally requires:

- Linux with readable `/proc`;
- a systemd user manager;
- Bash 4 or newer;
- Python 3.10 or newer.

macOS additionally requires:

- macOS 14 or newer on Apple Silicon or Intel;
- an active desktop login so the per-user launchd GUI domain exists;
- Homebrew Bash 4 or newer;
- Python 3.11 or newer.

The preflight checks the platform-specific tools it uses. Linux includes
`flock`, `journalctl`, and `systemctl`. macOS includes `launchctl`, `plutil`,
and `sw_vers`. A Git checkout also requires Git; a release artifact carries its
own source record.

Other init systems, macOS before 14, and a missing process view stop before
installation. There is no preview switch that bypasses these checks.

### macOS prerequisites

Install the supported toolchain if the commands are not already available:

```bash
brew install bash python rust
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/opt/homebrew/bin:$PATH"
cargo install shpool --version 0.11.0 --locked
```

Session Kit records the resolved modern Bash path in the owner-controlled
shpool configuration. A conflicting `shell` setting is refused instead of
silently replaced. The system Apple Bash is not used for managed sessions.

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
5. writes inactive systemd units or launchd templates;
6. asks before adding guarded shell integration;
7. backs up each startup file before changing it;
8. asks whether to enable terminal journals, defaulting to off;
9. leaves health notifications off.

On Linux, login integration changes `.bashrc`. On macOS, it changes `.bashrc`
and `.bash_profile` for managed Bash sessions and adds only the Session Kit
command path to `.zshrc`. A normal zsh SSH login remains a normal shell and the
picker opens only when you type `kit`.

The installer does not change provider storage, start a service, restart
shpool, attach to a session, or close a session.

Noninteractive installation leaves login integration and journals off unless
their explicit flags are supplied.

## Optional components

- **Bash integration:** adds the `kit` command and a short SSH login hint after
  explicit enablement. SSH still opens a normal shell; the picker opens only
  when you type `kit`.
- **Terminal journals:** record terminal bytes for new managed sessions after
  explicit opt-in.
- **Cleanup observer:** tracks exact eligible provider-exited terminals. It can
  close only after the timer is enabled and every safety predicate has remained
  unchanged for 72 continuous hours.
- **Watchdog:** runs in report-only mode by default. The advanced repair mode is
  an explicit Linux-only opt-in that can close and relaunch a proved-broken
  terminal. macOS supports report mode, not watchdog repair mode.
- **Notification command:** remains disabled unless explicitly configured.

## Activate safely

After installation:

```bash
session-kit doctor
session-kit enable-login
session-kit services enable
session-kit services status
```

`enable-login` is the compatibility name for enabling the Bash integration. It
does not make the picker open automatically.

Review the generated definitions before enabling them. On Linux,
`services enable` activates the shpool socket and cleanup timer. On macOS, it
loads the shpool, cleanup, and report-only watchdog LaunchAgents. macOS refuses
activation if another shpool daemon is reachable. Disabling macOS services
holds the creation lock and requires two empty-session proofs before unloading
shpool.

Activation does not require a reboot. Update and rollback do not restart a
loaded shpool daemon.

Use a new test session first. Confirm that disconnecting SSH leaves it running,
a provider quit leaves the managed terminal alive, exact reopen works, and
`k <numbers>` resolves each number to one exact session under a frozen proof.

## Evaluate without installing

The full repository test suite is the Linux development test path and uses
temporary fixtures:

```bash
tests/run
```

Do not run active-session commands merely to inspect the source.

macOS CI runs its native adapter checks plus focused installer, export, release,
privacy, and documentation tests on Apple Silicon and Intel. Real acceptance
also requires a disposable shpool session because GitHub-hosted CI does not
load a user LaunchAgent or start provider TUIs.
