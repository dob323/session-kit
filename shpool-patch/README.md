# Optional shpool 0.11.0 patches

Session Kit uses official shpool 0.11.0 by default. This directory contains
optional one-file source patches: `0001` for a heartbeat failure confirmed in
daemon logs, `0002` for input-mode loss on reattach, and `0003` for sessions
that become unkillable once their shell dies on its own. Apply them in
numeric order; `0002` touches a different file and is independent of the
other two.

The patches are not installed, built, selected, or activated by Session Kit.

## Problem

In shpool 0.11.0, the heartbeat sender treats a send timeout as a busy handler
and continues. The acknowledgement receiver can treat the same timeout as
fatal. That error can unwind the handler scope, leaving a listed session unable
to accept another client.

Typical evidence is:

```text
waiting for heartbeat ack
timed out waiting on receive operation
attaching new client stream to shell->client thread
```

Sanitize session names, account names, paths, timestamps, and terminal content
before sharing logs.

## Change

The patch changes only the acknowledgement timeout branch:

```rust
Err(crossbeam_channel::RecvTimeoutError::Timeout) => continue,
```

A disconnected channel still returns normally. A dead client is still detected
by the existing client-stream write failure.

## Limits

The repository does not claim a deterministic reproduction of the narrow race.
The patch removes one fatal timeout path based on the upstream control flow. It
does not prove that every unopenable session has this cause.

Do not apply it for quiet output, a provider pause, a normal provider exit, or a
generic attach failure without the matching acknowledgement evidence.

## Verify and build

Use upstream tag `v0.11.0`, commit
`fe2d11595ff255810523b0868159dec051e303f1`:

```bash
git clone --branch v0.11.0 --depth 1 \
  https://github.com/shell-pool/shpool.git shpool-0.11.0
cd shpool-0.11.0
test "$(git rev-parse HEAD)" = fe2d11595ff255810523b0868159dec051e303f1
git apply --check /path/to/session-kit/shpool-patch/0001-heartbeat-ack-timeout-is-not-fatal.patch
git apply /path/to/session-kit/shpool-patch/0001-heartbeat-ack-timeout-is-not-fatal.patch
cargo build --locked --release --bin shpool
sha256sum target/release/shpool
```

`patched-binary.sha256` is a reference checksum from one reviewed build. A
different Rust toolchain, target, or build environment can produce different
bytes. Record the upstream tag, patch checksum, Rust version, target triple,
build command, and resulting binary checksum for your own artifact.

The CI patch job checks that the patch applies to the pinned upstream tag and
that the patched source builds.

## Activation and rollback

Replacing the shpool binary normally requires restarting the daemon. Restarting
the daemon ends its managed terminal processes.

Before activation:

1. list active terminals and exact provider recovery identities;
2. keep the prior binary under a different filename;
3. verify the new binary checksum and build record;
4. plan recovery for every active provider conversation;
5. choose a maintenance window.

After replacement, record the running binary checksum in the private Session
Kit state if you use binary-change monitoring.

Rollback restores the prior binary and also requires a planned daemon restart.
Session Kit never performs either restart.

## Input-mode restore patch (0002)

On attach, shpool replays restorable screen contents but not the input modes
the application enabled: bracketed paste, application cursor and keypad keys,
and the mouse protocol. A freshly connected terminal — a new window
reattaching to a long-lived session — starts with those modes off while the
application inside still believes they are on.

The visible failure is pasting. Terminals with paste protection prompt before
pasting multi-line text into a session that appears to lack bracketed paste,
and the paste then arrives unbracketed, so embedded newlines submit or execute
immediately instead of arriving as one block. Arrow-key and mouse handling can
be similarly skewed until the application re-asserts its modes.

`0002-restore-input-modes-on-reattach.patch` appends the engine's tracked
input-mode state (`input_mode_formatted()`, already maintained by the
`shpool_vt100` engine) to the restore buffer in both vt100 restore paths
(`screen` and `lines` modes).

Limits: modes are replayed only when a restore buffer is emitted at all, so
the `simple` restore mode and the vterm engine are unchanged. The build,
activation, and rollback procedure is the same as for 0001, including the
daemon-restart requirement.

## Dead-shell kill patch (0003)

`shpool kill` sends SIGHUP to the session's child and treats any signal
error as fatal. If the child already exited on its own — a crash, a clean
exit the daemon's bookkeeping missed, an external kill — the signal returns
ESRCH, the kill handler aborts, and the session is never removed from the
daemon's table. The result is a phantom session that `shpool list` keeps
advertising, that cannot be attached, and that no kill can remove until the
daemon itself restarts. Log signature:

```text
ERROR ... handling new connection: killing shell proc
Caused by:
    0: sending SIGHUP to child proc
    1: ESRCH: No such process
```

`0003-kill-tolerates-an-already-dead-shell.patch` treats ESRCH as the state
kill exists to reach: the kill succeeds and the session is removed. Both the
SIGHUP and SIGKILL-escalation sites are guarded.

## License and upstream

The patch modifies Apache-2.0 licensed shpool source. See
[Third-party notices](../THIRD_PARTY_NOTICES) and
[Apache License 2.0](../LICENSES/Apache-2.0.txt).

Prefer an upstream release containing the accepted fix over carrying a local
patch.
