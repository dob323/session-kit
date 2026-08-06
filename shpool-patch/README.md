# Optional shpool 0.11.0 patches

Session Kit uses official shpool 0.11.0 by default. This directory contains
optional one-file source patches: `0001` for a heartbeat failure, `0002` for
input-mode loss on reattach, `0003` for sessions that become unkillable once
their shell dies on its own, and `0004` for a daemon-wide deadlock in the
detach handler. Apply them in numeric order; `0002`, `0003` and `0004` each
touch an independent region and apply cleanly to pristine `v0.11.0` on their
own.

**Start with `0004`.** It is the only patch here backed by a reproduced
production outage, and it is the only one whose failure mode is daemon-wide
rather than confined to a single session. See
[Detach deadlock patch (0004)](#detach-deadlock-patch-0004).

The patches are not installed, built, selected, or activated by Session Kit.

## Heartbeat ack patch (0001)

> **Field correction, 2026-08-06.** A daemon-wide freeze that looked like this
> patch's territory — sessions listed but unopenable, `attaching new client
> stream to shell->client thread` in the log — was **not** caused by the
> heartbeat path. The `waiting for heartbeat ack` / `timed out waiting on
> receive operation` lines below never appeared in that daemon's log at all;
> the only heartbeat error on record was a benign `sending heartbeat ack` on a
> disconnected channel during a normal kill teardown. The real cause was the
> unbounded detach handshake described in
> [Detach deadlock patch (0004)](#detach-deadlock-patch-0004).
>
> The "do not apply without the matching acknowledgement evidence" rule below
> held up: applying `0001` on the strength of the attach-failure line alone
> would have shipped a change that fixed nothing. Check for the ack lines
> specifically, and rule out `0004` first.

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

### Record the new fingerprint, every time

The watchdog compares the running daemon against a fingerprint recorded in
private Session Kit state. Replacing the binary without updating that value
leaves the check reporting a changed binary on every pass, for a change you made
on purpose. Treat this as part of the replacement, not as optional cleanup:

```bash
install -m 600 /dev/null "${XDG_STATE_HOME:-$HOME/.local/state}/session-kit/shpool-binary.sha256"
sha256sum ~/.cargo/bin/shpool | cut -d' ' -f1 \
  > "${XDG_STATE_HOME:-$HOME/.local/state}/session-kit/shpool-binary.sha256"
```

Use the path to the binary the daemon will actually execute, which is not
necessarily the one you just built. Confirm afterwards with `session-kit
doctor`, which reports a missing or no-longer-matching fingerprint.

Skipping this is not a quiet failure with no consequence. It is a check that
complains constantly and therefore stops being read, so the genuine silent
replacement it exists to catch arrives to an audience that has learned to ignore
it.

Rollback restores the prior binary, requires the same planned daemon restart,
and needs the fingerprint recorded again for the restored binary. Session Kit
never performs either restart.

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

## Detach deadlock patch (0004)

This is the one patch here with a confirmed production outage behind it
(2026-08-06: ten sessions unreachable for nineteen minutes).

`handle_detach` in `libshpool/src/daemon/server.rs` holds the **global session
table lock** across an unbounded control-channel handshake:

```rust
let shells = self.shells.lock();                  // global lock HELD
    let shell_to_client_ctl = s.shell_to_client_ctl.lock();
    shell_to_client_ctl.client_connection
        .send(shell::ClientConnectionMsg::Disconnect)   // unbounded send
    let status = shell_to_client_ctl.client_connection_ack
        .recv()                                         // unbounded recv, no timeout
```

`client_connection` and `client_connection_ack` are both created as
`crossbeam_channel::bounded(0)` — **rendezvous** channels. Neither half
completes unless the session's shell→client thread is sitting in its `select!`
loop ready to receive.

A client whose socket has stopped draining — a stalled ssh window, a suspended
laptop, a terminal tab that went away without closing cleanly — leaves that
thread blocked in `write()` to the client socket instead. The detach then waits
forever while holding the global lock, and every `list`, `attach`, `detach` and
`kill` in the daemon queues behind it. The daemon still accepts connections
(its main thread stays healthy in `accept()`), which is what makes this look
like a hang rather than a crash.

**One unresponsive client window takes down every session in the daemon.**

Log signature — a `handle_detach` span that opens and never closes, followed by
`handle_list` spans that all stop at the same place:

```text
handle_conn{cid=N}:handle_detach:lock(shells): new
handle_conn{cid=N}:handle_detach:lock(shells):lock(shell_to_client_ctl){s="..."}: new
... no matching close, ever ...
handle_conn{cid=M}:handle_list:lock(shells): new
... no matching close, ever ...
```

Confirm with a bounded probe: `timeout 30 shpool list` exiting 124 while the
daemon process is alive and its main thread is in `accept()`.

### Change

`0004-detach-must-not-hold-the-shells-lock.patch` restructures `handle_detach`
into three phases:

1. resolve the requested names to `Arc` control handles under the shells lock,
   then **drop the lock**;
2. run the handshake with the global lock released and both halves bounded by
   `SESSION_MSG_TIMEOUT`;
3. re-take the shells lock briefly for the `last_disconnected_at` bookkeeping.

This is not a new design — it is the pattern upstream already uses for the
session-message detach in the same file (`SessionMessageRequestPayload::Detach`),
and every other user of that control channel is bounded too
(`SHELL_TO_CLIENT_CTL_TIMEOUT` at the attach and disconnect sites).
`handle_detach` was the single call site that was neither scoped nor bounded.

A session that cannot complete the handshake in time is reported as
`not_attached` instead of stalling the daemon. Both callers handle that
correctly already: the background detach behind a takeover treats it as a
non-fatal `debug!` and continues, and an explicit `shpool detach` surfaces it
as an error, which is the accurate signal that the detach did not happen.

Worst case after the patch is one unreachable session, never a daemon-wide
outage.

### Limits

The patch bounds the wait; it does not explain why a shell→client thread
stopped servicing its control channel in the first place. That remains
uninvestigated — the containment is deliberate, because no single-session fault
should be able to take the daemon down regardless of its cause.

Build, activation and rollback are the same as for `0001`, including the
daemon-restart requirement. Because a restart ends every managed terminal
process, capture exact provider recovery identities first — after the restart
they are recoverable only through the login chooser's recovery review.

## License and upstream

The patch modifies Apache-2.0 licensed shpool source. See
[Third-party notices](../THIRD_PARTY_NOTICES) and
[Apache License 2.0](../LICENSES/Apache-2.0.txt).

Prefer an upstream release containing the accepted fix over carrying a local
patch.
