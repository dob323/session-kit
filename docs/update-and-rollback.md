# Update and roll back

## Current status

Update from a reviewed local clone. Session Kit never pulls source or restarts
a service on its own.

```bash
git pull --ff-only
tests/run
session-kit update --source "$PWD"
session-kit doctor
```

## Immutable releases

Session Kit's release layout is:

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
the life of the command. Commands already running continue from their pinned
release; later commands use the newly selected release.

Selecting a Session Kit release does not by itself restart, stop, signal, or
detach from shpool.

`session-kit update` reruns the compatibility check, installs the exact source
commit as a new immutable release, retains the prior release, and switches the
pointer atomically. It preserves the current journal choice and does not enable
login integration unless requested.

## Supported rollback contract

`session-kit rollback` selects the previous installed release recorded by the
last install or update. Use `session-kit rollback --to <full-commit>` for
another retained release. It updates stable launchers and the private
integration marker without restarting services.

## shpool binary updates

Session Kit release selection and shpool binary replacement are different
operations.

Replacing the shpool binary may require restarting its daemon, which ends its
managed terminal processes. Never combine that restart with a routine Session
Kit documentation or helper update. Review active sessions, provider recovery,
and the prior binary before approving it.

The official shpool 0.11.0 binary is the default. The optional patch has its own
build and rollback notes in [../shpool-patch/README.md](../shpool-patch/README.md).

## Recovery from an interrupted update

The release machinery records transaction state before changing exposed
paths. A later invocation should complete a committed transaction or restore
an incomplete one.

Do not delete a transaction journal to bypass a refusal. Preserve it and report
the exact error through a sanitized issue.
