# Update and roll back

Session Kit installs from reviewed local source. It does not fetch source, and
it does not start, stop, or restart a service on its own.

From an updated local Git checkout:

```bash
git pull --ff-only
tests/run
session-kit update --source "$PWD"
session-kit doctor
```

`session-kit update` can omit `--source` when the source recorded by the current
install receipt is still available. If that path no longer exists, give it a
reviewed local Git checkout or a supported extracted release source explicitly.

## Updating does not disturb running sessions

This is the property the whole layout exists to provide, so it is worth being
precise about why.

```text
$HOME/.local/lib/session-kit/
  current -> releases/<full-commit-id>
  releases/<full-commit-id>/
$HOME/.local/bin/
  session-kit
  kit
  sp
  shpool_login
  shpool_status
  shpool_reaper
  codex_resume_here
```

Each release directory is immutable and named for its exact source commit. The
files in `$HOME/.local/bin` are stable executable launcher copies, not symlinks
into a release. A launcher resolves `current` once and pins that physical
release for the lifetime of the command. A command already running therefore
finishes on the release it started with, and only new commands use the newly
selected one.

Lifecycle commands use the separately pinned `manager` release. A rollback can
therefore select an older runtime without reviving that runtime's obsolete
updater. On update, the current manager validates the reviewed source and hands
the transaction to that source's manager, so new transaction and provider-hook
rules apply on the first update rather than one update later.

`session-kit update` checks compatibility, installs the exact source commit,
keeps the prior release, refreshes the stable launchers, and switches the
pointer atomically. Release selection does not restart, stop, signal, attach
to, or detach from shpool. Your open terminals keep running.

Updates default journals to off; pass `--journal on` only when the local
retention decision still applies. The existing login-integration choice is
preserved unless you supply an explicit login flag.

## A changed Codex hook must be trusted again

Codex trusts a command hook by the exact hash of the file it was shown. Any
release that changes the registered
`lib/sessionkit_supervisor/provider_hooks.py` wrapper changes that hash, and
Codex then **skips the hook until you re-trust it** — silently, from the
kit's point of view. The automatic project intake for Codex sessions simply
stops until the next Codex session shows you the review prompt and you accept.

Nothing is lost while it is skipped: the intake path a project takes when it is
messaged still works, and Claude sessions are unaffected (Claude re-reads its
registered hook by path, not by hash). But a fleet that quietly stopped
recording half its projects looks exactly like a fleet with no projects, so
after an update that touches that file, open one Codex session and accept the
prompt before assuming intake is running.

## Service definitions need a separate, deliberate step

An update refreshes the installed service definitions. It does not activate
them. A long-running service therefore keeps executing the code it started
with until you restart it, even after `current` points somewhere new.

That matters whenever a release changes what a service *does* rather than only
what the commands do. The watchdog is the usual case: a new check in a new
release is not running until the watchdog restarts.

On Linux, update does not run `systemctl --user daemon-reload` and does not
start or restart a unit. Review service state and activate a definition change
separately. Read-only source and doctor probes can fall back to systemd's
local-machine transport when the direct user socket is unavailable, and they
report that state as a warning; service-control commands still require the
normal direct user socket.

On macOS, update regenerates private LaunchAgent templates, and any job already
loaded by launchd continues using its active definition. To apply a changed
definition, first reach a safe point with no managed sessions, then:

```bash
session-kit services disable
session-kit services enable
session-kit services status
```

`services disable` refuses to unload shpool while live sessions exist, or when
their absence cannot be proved. `services enable` refuses to overwrite a loaded
job whose active definition differs from the generated template.

## Roll back

`session-kit rollback` selects the prior installed release recorded by the last
install or update. For any other retained release:

```text
session-kit rollback --to <full-commit>
```

Rollback validates the retained release, updates the pointer, regenerates
platform service templates, and updates the private
integration marker and receipt. Like update, it does not restart or reload
services, so on macOS a loaded LaunchAgent continues using its prior active
definition until the same safe disable and enable cycle above.

Provider-hook registration follows the target release as part of the same
transaction. The release that begins rollback performs the hook change with
its own registration code; it never imports a helper from the rollback target.
This allows rollback to a release from before automatic hook registration. A
failed flip restores both provider files from the transaction preimage, and a
successful rollback removes only Session Kit's provenance-marked handler while
leaving every user handler in the matcher group untouched.

The recovery-capable management launcher is stable across rollback. If power
is lost after the pointer changes, it reads the owner-private pending journal
and runs the exact pre-flip manager that created it. The launcher itself is not
rewritten after journal removal, eliminating the former kill window where an
older manager could become active without durable recovery instructions. Both
journal publication and removal are followed by a directory sync so the
decision survives a machine crash.

## State written by a newer release

A release that changes the format of private state must say whether an older
release can still read it, because rollback does not rewrite state.

**Session colors, from release 0.2.0.** That release splits session colors into a Claude
palette and a Codex palette and adds six color names that earlier releases have
never seen. Rolling back to v0.1.x is safe: the older code ignores a stored
color override it does not recognise and falls back to its own palette, and its
validator drops the unknown name rather than failing. A session that showed a
Codex-only color simply returns to an older color. No state needs to be removed
before rolling back.

## shpool binary updates

Session Kit release selection and shpool binary replacement are separate
operations. Replacing shpool may require a daemon restart, which can end every
managed terminal process. Never combine that restart with a routine Session Kit
update.

Rebuilding or reinstalling shpool also discards any patch previously applied to
it, and that loss is silent. The optional patch has its own build and rollback
instructions in [the shpool patch guide](../shpool-patch/README.md).

## An interrupted update

Lifecycle commands record the exact pre-change file state before touching
exposed paths. A later Session Kit lifecycle invocation either completes a
committed transaction or restores an incomplete one.

Do not delete a transaction receipt to get past a refusal. Preserve it and
report the sanitized error: the receipt is what makes the recovery exact.
