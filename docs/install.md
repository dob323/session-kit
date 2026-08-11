# Install Session Kit

Session Kit `v0.3.0` is a public beta for Linux with systemd and macOS 14 or
newer. Install the accepted release artifact under a single-user account where
provider conversations are recoverable.

Beta releases are published as GitHub prereleases. `releases/latest` does not
resolve to a prerelease, so name the tag explicitly or browse
[all releases](https://github.com/dob323/session-kit/releases). Release assets
are named by source commit rather than by version.

```bash
mkdir session-kit-download
cd session-kit-download
gh release download v0.3.0 --repo dob323/session-kit
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

The installer puts its commands in `$HOME/.local/bin`. On Debian and Ubuntu,
`~/.profile` adds that directory to `PATH` only if it already existed when you
logged in — so if you have never had one, `session-kit` is `command not found`
until you start a new login shell.

That first `session-kit doctor` run then exits **1**, on a perfectly good
install, with these two lines:

```text
FAIL  watchdog  installed but not enabled (disabled) … enable it with: session-kit services enable
FAIL  units     shpool.socket disabled/inactive; shpool-reaper.timer disabled/inactive …
```

That is the expected result at this point, not a broken install: the installer
writes the unit files and deliberately never enables them, and doctor gates on
the services being live. It turns green after
[Activate safely](#activate-safely) below. Everything else doctor checks —
files, permissions, provider readers, kill switches — is meaningful right now.

On the first interactive install, Session Kit reads the project paths already
recorded by Claude Code and Codex, shows the existing directories it found,
and offers to import all of them. Discovery reads Claude's local project map
and history plus Codex's configured projects and stored thread directories. It
does not walk the home directory or search the rest of the filesystem.

For unattended installation, project import stays off unless it is requested:

```bash
./install.sh --non-interactive --import-projects
```

Use `--no-import-projects` to skip the interactive offer. The same discovery
can be reviewed or rerun later with `session-kit projects discover` and
`session-kit projects import`. An existing `projects.tsv` is never replaced by
an install or update.

Without the GitHub CLI, download the `.tar.gz`, `.sha256`, and
`.provenance.json` assets from the
[`v0.3.0` release](https://github.com/dob323/session-kit/releases/tag/v0.3.0),
and put them in one empty directory. The checksum file covers the archive. Use
`sha256sum --check` on Linux or
`shasum -a 256 --check` on macOS. The provenance file records the exact source
commit and public-tree digest.

Cloning `main` is the development path, not the normal installation path. If
you use it, review and test the exact commit before installation.

`./install.sh --check` is read-only. The installer copies files and user-service
definitions but does not start, stop, restart, or enable a service.

## Requirements

- shpool 0.11.0 — see [shpool](#shpool) for the two supported ways to get it,
  including a route that needs no compiler on the target machine;
- Claude Code, Codex, or both — see [Provider CLIs](#provider-clis);
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

### Provider CLIs

Session Kit manages provider conversations; it does not install or update the
providers. The preflight **fails** with `install Claude Code, Codex, or both`
until at least one provider command is on `PATH`, so install one first, from its
vendor's own instructions. Either provider alone is enough. The commands below are the vendors' documented ones — check the
linked page if a command fails, since these change.

**Claude Code** ([install guide](https://code.claude.com/docs/en/setup)):

```bash
curl -fsSL https://claude.ai/install.sh | bash   # macOS, Linux, WSL
claude --version                                 # confirm it runs
```

Anthropic also publishes `brew install --cask claude-code`, signed apt/dnf/apk
repositories, and `npm install -g @anthropic-ai/claude-code`. The native
installer keeps itself updated; the package-manager routes do not. Claude Code
requires a Pro, Max, Team, Enterprise, or Console account.

**Codex** ([install guide](https://learn.chatgpt.com/docs/codex/cli)):

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh   # macOS, Linux
codex --version                                        # confirm it runs
```

OpenAI also publishes `brew install --cask codex` and
`npm install -g @openai/codex`. Sign in on the first `codex` run in a project.

Sign in to each provider once before installing Session Kit. The picker lists
sessions it can prove; a provider that has never authenticated has none.

### Linux prerequisites

A minimal server image usually lacks several tools the preflight requires. On
Debian or Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y python3 procps diffutils findutils util-linux \
  bsdutils coreutils curl git
```

On Fedora, RHEL, or a RHEL rebuild:

```bash
sudo dnf install -y python3 procps-ng diffutils findutils util-linux \
  coreutils curl git
```

`procps` (`procps-ng` on the RPM side) supplies `ps` and `pgrep`, `diffutils`
supplies `cmp`, `bsdutils` supplies `script`, and `flock` comes from
`util-linux`. `git` is only needed for a checkout install, and is absent from
every minimal image tested. The preflight names anything still missing rather
than failing part-way through an install.

shpool is not packaged by any distribution, so it has to be built. Building it
needs a Rust toolchain **and** a C compiler and linker, which the prerequisites
above do not include:

```bash
sudo apt-get install -y build-essential   # Debian or Ubuntu
sudo dnf install -y gcc                   # Fedora or RHEL
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
cargo install shpool --version 0.11.0 --locked
```

Without one the build stops at ``error: linker `cc` not found``.

**No compiler on the target machine?** You do not need one.
[Getting a static shpool binary](../extras/build-static-binary.md) builds a
fully static musl binary inside a throwaway container and copies it out;
nothing is installed on the host, and the result runs on any x86-64 Linux
regardless of glibc version.

Read the [shpool](#shpool) section below before running either route on a
machine you intend to rely on.

#### Keep user services running after you log out

Session Kit's services belong to your user's systemd manager, which by default
stops when your last session ends. On a headless machine reached only over SSH,
that means the watchdog and the cleanup timer stop the moment you disconnect,
and managed sessions lose their supervision:

```bash
loginctl enable-linger "$USER"
loginctl show-user "$USER" --property=Linger
```

The second command must report `Linger=yes`. Enable this before
`session-kit services enable`, not after.

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

Use official shpool 0.11.0 by default. Session Kit ships four optional source
patches against that release and installs none of them automatically. Review
[the patch guide](../shpool-patch/README.md) before deciding.

Read about `0004` first. Stock 0.11.0 can deadlock on detach and make every
managed session unreachable at once — the daemon stays alive and accepting
connections while every list, attach, and detach blocks forever, and it does not
recover on its own. One stalled SSH window is enough to trigger it. The other
three patches address narrower problems.

If you apply a patch, record the resulting binary's fingerprint as the last step
of the rebuild. The patch guide gives the exact command. Skipping it leaves the
watchdog reporting a changed binary on every pass for a change you made
deliberately, and `session-kit doctor` will tell you so.

With a current Rust toolchain:

```bash
cargo install shpool --version 0.11.0 --locked
```

Without one, use the container build in
[Getting a static shpool binary](../extras/build-static-binary.md). Both routes
produce the same 0.11.0 binary; the container route leaves no toolchain behind.

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

One provider file is registered rather than only laid down: the automatic
project intake hook, added to `~/.claude/settings.json` and `~/.codex/hooks.json`
under `UserPromptSubmit`. Both files are backed up before the edit and re-read
after it. Session Kit adds one provenance-marked handler inside a compatible
matcher group; it does not claim that group. Existing handlers, including
other `UserPromptSubmit` commands in the same group, remain byte-for-byte
untouched. The registration is idempotent — installing again changes nothing.
Configuration JSON with duplicate keys, a linked path ancestor, or more than
one Session Kit-owned handler is refused instead of being normalized.

A managed `sp new codex --prompt-file ...` keeps the source file until its
private handoff is durable and the audit append succeeds. Provider acceptance
and supervisor intake are separate proofs. The prompt is consumed only after
the fsynced intake proof matches the exact conversation, turn, content digest,
and managed shell generation. Any started process without that proof moves the
prompt to `sp prompt-quarantine list` as **Needs You** and never replays it.
Use `ingest` to retry supervisor intake without contacting Codex, `resume` to
reopen the exact accepted conversation, or `discard` to retain it for 30-day
recovery.

**Codex asks you to trust that hook once.** Codex trusts a command hook by the
exact hash of the registered wrapper file, so the first Codex session after
installation prompts to review it, and until you accept, Codex skips the hook
and no Codex project is recorded automatically. The same prompt returns
whenever a release changes that file — see `docs/update-and-rollback.md`.

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
`services enable` activates the shpool socket, the cleanup timer, and the
session watchdog; `session-kit doctor` fails while any of the three is
installed but not enabled. On macOS, it
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

## Provider config directory permissions

Activation writes one hook into `~/.claude/settings.json` and
`~/.codex/hooks.json`, so it refuses a config directory another account could
write. Group-writable is allowed only when the directory is yours and its group
is your own single-member private group, which is what a private-group
distribution with a 002 umask creates; a shared group or a world-writable
directory is refused. `./install.sh --check` reports this before anything is
written and prints the repair to run, such as `chmod g-w ~/.claude`.
