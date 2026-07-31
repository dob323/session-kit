# Optional shpool 0.11.0 heartbeat patch

Session Kit uses official shpool 0.11.0 by default. This directory contains an
optional one-file source patch for users who have confirmed the matching
heartbeat failure in their own daemon logs.

The patch is not installed, built, selected, or activated by Session Kit.

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

## License and upstream

The patch modifies Apache-2.0 licensed shpool source. See
[Third-party notices](../THIRD_PARTY_NOTICES) and
[Apache License 2.0](../LICENSES/Apache-2.0.txt).

Prefer an upstream release containing the accepted fix over carrying a local
patch.
