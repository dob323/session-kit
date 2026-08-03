# macOS support

Session Kit supports macOS 14 or newer on Apple Silicon and Intel. The supported
account and toolchain are:

- a normal user logged into the Mac desktop, which provides the per-user
  `gui/$UID` LaunchAgent domain;
- Homebrew Bash 4 or newer;
- Python 3.11 or newer;
- official shpool 0.11.0;
- supported local Claude Code, Codex, or both.

Install the prerequisites before running Session Kit:

```bash
brew install bash python rust
cargo install shpool --version 0.11.0 --locked
```

Follow the [installation guide](../docs/install.md) for the release install.
The installer re-executes itself with Homebrew Bash when invoked by Apple's
Bash 3.2. It may add its opt-in login integration to zsh and Bash startup files,
but it does not replace the account's login shell. The managed shpool shell is
the exact validated modern Bash executable.

## LaunchAgents

Installation creates inactive LaunchAgent templates in the Session Kit config
directory. It does not copy, load, start, stop, or restart any service. Activate
the per-user jobs explicitly while logged into the Mac desktop:

```bash
session-kit services enable
session-kit services status
session-kit services disable
```

The three jobs keep shpool available, run the safe-state reaper hourly, and run
the report-only watchdog every 60 seconds. They are user LaunchAgents, not
privileged LaunchDaemons, and do not provide service persistence before a GUI
login. Enable refuses to start a second daemon when an unmanaged shpool daemon
is reachable. Disable refuses while any shpool session remains and unloads the
watchdog and reaper before shpool.

Update and rollback refresh inactive templates but never reload the active
LaunchAgents. Disable and enable services deliberately if you need to activate
a new definition, after first closing every shpool session.

## Native identity and lifecycle

Darwin process identity comes from `PROC_PIDTBSDINFO`. The recorded generation
is the native process start timestamp in epoch microseconds. Session Kit reads
that identity before and after `KERN_PROCARGS2`; a PID whose generation changes
during the read is discarded. Boot identity comes from `kern.boottime`.

Codex identity uses the exact live process's `CODEX_THREAD_ID` and one
owner-controlled local rollout whose structured UUID matches it. The rollout
provides structured turn and reply state. Claude Code uses its structured agent
state joined to the same exact native process tree. Missing, duplicated, or
changed identity fails closed.

The supported macOS lifecycle includes inventory, picker, new, attach,
takeover, exact resume and fork, automatic naming, journals, provider exit and
reopen, the safe-state reaper, and manual `sp prune`.

## Current limits

- Watchdog repair mode is Linux-only because it depends on Linux daemon-thread
  evidence. The macOS watchdog reports findings and refuses repair mode before
  making a change.
- Per-user service persistence requires the account's Mac desktop session to be
  logged in. A privileged or pre-login LaunchDaemon is not supported.
- Provider versions outside the combinations listed in the release evidence are
  best-effort because provider state formats and commands can change.
- GitHub CI exercises native macOS 15 runners on Apple Silicon and Intel, the
  Darwin adapter, and fixture-based lifecycle and export behavior. It does not
  prove real shpool, provider, LaunchAgent, SSH reconnect, or reboot behavior.

## Release acceptance

Before a beta release is published, a dedicated supported Mac must verify the
real-device lifecycle below. Native CI must pass on every supported
architecture, and the release notes must distinguish that CI coverage from the
exact real-device coverage:

1. Native process start identity, including rapid PID churn between both
   identity reads.
2. Stable `kern.boottime` during one boot. Fixture tests verify that a changed
   boot identity invalidates saved process generations; release acceptance does
   not require rebooting the device.
3. List, picker, new, attach, takeover, resume, fork, naming, journals, provider
   exit, reopen, reaper, and manual prune with real shpool sessions.
4. Exact Claude Code and Codex identity and reply state for the release's pinned
   provider versions.
5. SSH disconnect and reattach while the GUI user LaunchAgent domain remains
   available.
6. Explicit service enable, status, and safe disable, including refusal of an
   unmanaged daemon and refusal to unload a nonempty shpool daemon.
7. Report-only watchdog behavior and repair-mode refusal.
8. Update, rollback, and uninstall without disturbing live sessions or deleting
   retained private data.

CI must pass on both architectures, but it does not replace this real-Mac gate.
