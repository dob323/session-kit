# Update and roll back

Session Kit installs from reviewed local source. It does not fetch source or
restart a service on its own.

From an updated local Git checkout:

```bash
git pull --ff-only
tests/run
session-kit update --source "$PWD"
session-kit doctor
```

`session-kit update` can omit `--source` when the source recorded by the current
install receipt is still available. If that path no longer exists, provide a
reviewed local Git checkout or supported extracted release source explicitly.

## Immutable releases

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

The files in `$HOME/.local/bin` are stable executable launcher copies, not
symlinks into a release. Each launcher resolves `current` once and pins that
physical release for the command's lifetime. Existing commands finish on their
pinned release; new commands use the selected release.

`session-kit update` checks compatibility, installs the exact source commit,
keeps the prior release, updates the launchers, and switches the pointer
atomically. Updates default journals to off; pass `--journal on` only when the
local retention decision still applies. The existing login-integration choice
is preserved unless an explicit login flag is supplied.

Release selection does not restart, stop, signal, attach to, or detach from
shpool.

## Service definitions

On Linux, update refreshes the installed user-service definitions but does not
run `systemctl --user daemon-reload` or start or restart a unit. Review service
state and activate a definition change separately. The read-only source and
doctor probes can use systemd's local-machine transport when the direct user
socket is unavailable, but they report that state as a warning. Service-control
commands continue to require the normal direct user socket.

On macOS, update regenerates private LaunchAgent templates. Any jobs already
loaded by launchd continue using their active definitions. To apply a changed
definition, first reach a safe point with no managed sessions, then run:

```bash
session-kit services disable
session-kit services enable
session-kit services status
```

The disable command refuses to unload shpool if live sessions exist or their
absence cannot be proved. `services enable` also refuses to overwrite a loaded
job whose active definition differs from the generated template.

## Rollback

`session-kit rollback` selects the prior installed release recorded by the last
install or update. Use:

```text
session-kit rollback --to <full-commit>
```

for another retained release.

Rollback validates the retained release, updates the pointer and stable
launchers, regenerates platform service templates, and updates the private
integration marker and receipt. It does not restart or reload services. On
macOS, a loaded LaunchAgent therefore continues using its prior active
definition until the same safe disable and enable cycle described above.

A state-format change must document whether the older release can read new
state before the newer release is activated.

## shpool binary updates

Session Kit release selection and shpool binary replacement are separate.
Replacing shpool may require a daemon restart and can end every managed
terminal process. Never combine that restart with a routine Session Kit update.

The optional patch has separate build and rollback instructions in
[the shpool patch guide](../shpool-patch/README.md).

## Interrupted update

Lifecycle commands record the exact pre-change file state before changing
exposed paths. A later Session Kit lifecycle invocation completes a committed
transaction or restores an incomplete one.

Do not delete a transaction receipt to bypass a refusal. Preserve it and report
the sanitized error.
