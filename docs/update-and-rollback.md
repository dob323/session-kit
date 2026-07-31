# Update and roll back

Session Kit updates from a reviewed local clone. It does not fetch source or
restart a service on its own.

```bash
git pull --ff-only
tests/run
session-kit update --source "$PWD"
session-kit doctor
```

## Immutable releases

```text
$HOME/.local/lib/session-kit/
  launcher
  current -> releases/<full-commit-id>
  releases/<full-commit-id>/
$HOME/.local/bin/
  sp -> ../lib/session-kit/launcher
  shpool_login -> ../lib/session-kit/launcher
  shpool_status -> ../lib/session-kit/launcher
  shpool_reaper -> ../lib/session-kit/launcher
  codex_resume_here -> ../lib/session-kit/launcher
```

The stable launcher resolves `current` once and pins that physical release for
the command's lifetime. Existing commands finish on their pinned release; new
commands use the selected release.

`session-kit update` checks compatibility, installs the exact source commit,
keeps the prior release, and switches the pointer atomically. Updates default
journals to off; pass `--journal on` only when the local retention decision
still applies. The existing login choice is preserved unless a login flag is
supplied.

Release selection does not restart, stop, signal, attach to, or detach from
shpool.

## Rollback

`session-kit rollback` selects the prior installed release recorded by the last
install or update. Use:

```text
session-kit rollback --to <full-commit>
```

for another retained release.

Rollback updates stable launchers and the private integration marker without
restarting services. A state-format change must document whether the older
release can read new state before the newer release is activated.

## shpool binary updates

Session Kit release selection and shpool binary replacement are separate.
Replacing shpool may require a daemon restart and can end every managed
terminal process. Never combine that restart with a routine Session Kit update.

The optional patch has separate build and rollback instructions in
[the shpool patch guide](../shpool-patch/README.md).

## Interrupted update

Release commands write transaction state before changing exposed paths. A later
invocation completes a committed transaction or restores an incomplete one.

Do not delete a transaction receipt to bypass a refusal. Preserve it and report
the sanitized error.
