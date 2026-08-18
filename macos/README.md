# macOS support

Session Kit supports macOS 14 or newer on Apple Silicon and Intel. It needs a
normal user who is logged into the Mac desktop, because that login is what
creates the per-user `gui/$UID` launchd domain the services load into. It also
needs Homebrew Bash 4 or newer, Python 3.11 or newer, official shpool 0.11.0,
and a local Claude Code, Codex, or both.

```bash
brew install bash python rust
cargo install shpool --version 0.11.0 --locked
```

Follow the [installation guide](../docs/install.md) for the release install.
Apple ships Bash 3.2, which cannot run this code, so `install.sh` and
`session-kit` re-execute themselves under `/opt/homebrew/bin/bash` or
`/usr/local/bin/bash` when they are started by it. If neither exists they exit
`69` with the `brew` and `cargo` commands above rather than running in a shell
that would misparse them.

Installation may add the opt-in login integration to `.bashrc`,
`.bash_profile`, and `.zshrc`, but it never replaces the account's login shell.
zsh receives only the Session Kit command path, so a normal zsh SSH login stays
a normal shell and the picker opens when you type `kit`. Managed shpool sessions
run the exact modern Bash the installer validated, recorded in the
owner-controlled shpool configuration; a conflicting `shell` setting there is
refused rather than silently replaced.

## LaunchAgents

Installation generates three LaunchAgent templates in
`~/.config/session-kit/launchd/`. A first install does not load them; activate
the jobs explicitly while logged into the Mac desktop. On an update or
rollback, an already-loaded watchdog is kickstarted onto the selected release.
The shpool and reaper jobs are not restarted automatically.

```bash
session-kit services enable
session-kit services status
session-kit services disable
```

The three jobs are `com.session-kit.shpool`, which keeps the shpool daemon
available with `KeepAlive` and a ten-second throttle;
`com.session-kit.reaper`, which runs the safe-state cleanup pass hourly; and
`com.session-kit.watchdog`, which runs one report-only check every 60 seconds
under `SESSION_KIT_WATCHDOG_MODE=report`. They are user LaunchAgents, not
privileged LaunchDaemons, so they provide no service persistence before a GUI
login. Each job's stdout and stderr go to
`~/.local/state/session-kit/logs/<label>.{out,err}.log`, created mode `0600`.
launchd gives Session Kit no retention or rotation guarantee for them, so manage
their size yourself.

`services enable` refuses to load the shpool job while an unmanaged shpool
daemon is already reachable, or while a probe for one times out. A second daemon
on the same socket is the failure that loses sessions, so an ambiguous probe is
treated as a positive. It also refuses to overwrite a loaded job whose active
plist differs from the generated template, that combination means an update
left a pending definition, and quietly replacing the file would leave the loaded
job and the file on disk describing different things.

`services disable` holds the Session Kit creation lock, proves shpool reports
zero sessions, unloads the watchdog and reaper, proves the list is still empty,
and only then unloads shpool. Unloading shpool stops the daemon and takes the
managed terminals with it, which is why the proof is taken twice around the
window where a new session could appear. Before it removes any active plist it
verifies that file against the generated template or the digest in its private
receipt, and refuses a modified or unowned one.

Update and rollback regenerate the templates and kickstart an already-loaded
watchdog. They do not unload or reload shpool or the reaper. To adopt changed
definitions for those jobs, reach a point with no managed sessions and cycle
`services disable` then `services enable` deliberately. See
[Update and roll back](../docs/update-and-rollback.md) and
[Troubleshooting](../docs/troubleshooting.md#macos-services-are-not-running).

## Native identity and lifecycle

Darwin process identity comes from `PROC_PIDTBSDINFO`. The recorded generation
is the native process start timestamp in epoch microseconds. Session Kit reads
that identity before and after `KERN_PROCARGS2` and discards any PID whose
identity changed between the two reads, because a PID can be reused mid-read and
the argv it returned would then belong to a different process. Boot identity
comes from `kern.boottime`.

Codex identity uses the exact live process's `CODEX_THREAD_ID` together with one
owner-controlled local rollout whose structured UUID matches it; the rollout
supplies turn and reply state. Claude Code uses its structured agent state
joined to the same exact native process tree. Missing, duplicated, or changed
identity fails closed.

The supported macOS lifecycle covers inventory, picker, new, attach, takeover,
exact resume and fork, automatic naming, journals, provider exit and reopen, the
safe-state reaper, and manual `sp prune`.

## Current limits

- Watchdog repair mode is Linux-only because it depends on Linux daemon-thread
  evidence. The macOS watchdog reports findings and refuses repair mode before
  it changes anything.
- Two reaper maintenance passes are skipped on Darwin: expiring retired picker
  temporary files, and garbage-collecting stale immutable releases. Installed
  releases therefore accumulate under `~/.local/lib/session-kit/releases/` on
  macOS until they are removed by hand.
- Per-user service persistence requires the account's Mac desktop session to be
  logged in. A privileged or pre-login LaunchDaemon is not supported.
- Provider versions outside the combinations named in the release evidence are
  best-effort, because provider state formats and commands can change.
- GitHub CI runs native macOS 15 runners on Apple Silicon and Intel against the
  Darwin adapter and fixture-based lifecycle and export behavior. It does not
  load a user LaunchAgent or start a provider TUI, so it proves nothing about
  real shpool, provider, LaunchAgent, SSH reconnect, or reboot behavior.

## Release acceptance

CI passing on both architectures does not release a beta. A dedicated supported
Mac must first verify the real-device lifecycle below, and the release notes
must state which coverage came from CI and which from that device:

1. Native process start identity, including rapid PID churn between the two
   identity reads.
2. Stable `kern.boottime` across one boot. Fixture tests already verify that a
   changed boot identity invalidates saved process generations, so acceptance
   does not require rebooting the device.
3. List, picker, new, attach, takeover, resume, fork, naming, journals, provider
   exit, reopen, reaper, and manual prune against real shpool sessions.
4. Exact Claude Code and Codex identity and reply state for the release's pinned
   provider versions.
5. SSH disconnect and reattach while the GUI user LaunchAgent domain stays
   available.
6. Explicit service enable, status, and safe disable, including the refusal of
   an unmanaged daemon and the refusal to unload a nonempty shpool daemon.
7. Report-only watchdog behavior and repair-mode refusal.
8. Update, rollback, and uninstall without disturbing live sessions or deleting
   retained private data.
