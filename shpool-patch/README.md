# Optional shpool 0.11.0 patches

Session Kit uses official shpool 0.11.0 by default. This directory contains
optional source patches: `0001` for a heartbeat failure, `0002` for
input-mode loss on reattach, `0003` for sessions that become unkillable once
their shell dies on its own, `0004` for a daemon-wide deadlock in the detach
handler, `0005` for an attach client that reports the wrong exit status, and
`0006` for resize bursts that cause repeated stale-frame repaints. Apply any
set you choose in numeric order; `0002` through `0006` touch independent
regions and apply cleanly to pristine `v0.11.0` on their own.

**Evaluate `0004` first.** It is the only patch here whose reproduced failure
mode is daemon-wide rather than confined to a single session. If you then carry
more than one patch, still apply the chosen set in numeric order. See
[Detach deadlock patch (0004)](#detach-deadlock-patch-0004).

The patches are not installed, built, selected, or activated by Session Kit.

## Heartbeat ack patch (0001)

> **Evidence rule.** A generic attach failure does not establish a heartbeat
> fault. Require the matching acknowledgement wait and receive-timeout chain
> below, and rule out the bounded-detach failure addressed by `0004` first.
> Applying `0001` without that chain changes the wrong path.

In shpool 0.11.0, the heartbeat sender treats a send timeout as a busy handler
and continues. The acknowledgement receiver can treat the same timeout as
fatal. That error can unwind the handler scope, leaving a listed session unable
to accept another client.

The required chain is a daemon log containing `joining heartbeat_h`, `waiting
for heartbeat ack`, and the receive timeout, followed by attempts to open the
listed session timing out while attaching a new client stream. That sequence,
not a generic attach error by itself, is the evidence for `0001`.

Typical evidence is:

```text
waiting for heartbeat ack
timed out waiting on receive operation
attaching new client stream to shell->client thread
```

Sanitize session names, account names, paths, timestamps, and terminal content
before sharing logs.

## Change

The patch makes a delayed acknowledgement recoverable without confusing it
with the next heartbeat:

- heartbeat requests and acknowledgements carry matching request IDs;
- the acknowledgement channel has one buffer slot, so a late ack cannot block
  the shell-to-client thread;
- a timed-out request is abandoned without ending the session scope;
- a late ack for an abandoned request is discarded by ID;
- the shell-to-client thread deposits its ack without blocking, and the
  heartbeat thread drains stale acks before each request, so neither side can
  park on the other.

A disconnected channel still returns normally. A dead client is still detected
by the existing client-stream write failure.

The last point is not decoration. The buffer slot introduced above can still
hold an ack the heartbeat thread gave up waiting for, and the receiving half
lives in the long-lived control struct, so it never disconnects. A blocking
send into a full slot would therefore park the shell-to-client thread with
nobody left to drain it, the same wedge this patch exists to prevent, reached
through a different door. Dropping an ack is safe precisely because requests
carry IDs: the heartbeat thread times out and asks again under a fresh one.

## Limits

The patch includes deterministic regressions that pause the shell-to-client
thread after it writes a heartbeat, hold the ack past the 300ms timeout, and
verify continued output, detach-and-reattach, repeated late-ack ID handling,
and client loss during the delay. They do not prove that every unopenable
session has this cause.

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
git apply --check ~/.local/lib/session-kit/current/shpool-patch/0001-heartbeat-ack-timeout-is-not-fatal.patch
git apply ~/.local/lib/session-kit/current/shpool-patch/0001-heartbeat-ack-timeout-is-not-fatal.patch
cargo build --locked --release --bin shpool
sha256sum target/release/shpool
```

`patched-binary.sha256` is the reference checksum from the reviewed build with
all six patches applied in numeric order. A different Rust toolchain, target,
or build environment can produce different bytes, so the checksum is worthless
without the environment that produced it. The recorded value came from:

| | |
|---|---|
| upstream tag | `v0.11.0` == `fe2d11595ff255810523b0868159dec051e303f1` |
| patches | `0001`–`0006`, applied in numeric order |
| rustc | 1.97.1 (8bab26f4f 2026-07-14) |
| cargo | 1.97.1 (c980f4866 2026-06-30) |
| target triple | `x86_64-unknown-linux-gnu` |
| build host | AlmaLinux 10.2, glibc 2.39, rustup `stable-x86_64-unknown-linux-gnu` |
| build command | `cargo build --locked --release --bin shpool` |
| sha256 | `937ed524412b2810c45661c266cfbb495aa534b0da399ef6509aa04914556ade` |

The `0001`–`0005` checksum recorded before `0006` was
`ca8d28aa52da0b9ee45f7d7ef24aac41bccaa316a2a959ed6804db3b8a5b1e45`, built in
`docker.io/library/rust:bookworm` on the same toolchain versions. The two are
not comparable byte for byte: the patch set and the build host both differ.

Record the same set for your own artifact. A checksum recorded without its
build environment cannot later be reproduced or refuted, which is the same as
not having recorded it.

The CI patch job applies all six patches to the pinned upstream tag, runs the
workspace tests, and builds the release binary.

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
sha256sum "$(command -v shpool)" | cut -d' ' -f1 \
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
and the mouse protocol. A freshly connected terminal, a new window
reattaching to a long-lived session, starts with those modes off while the
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
error as fatal. If the child already exited on its own, a crash, a clean
exit the daemon's bookkeeping missed, an external kill, the signal returns
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

This is the one patch here whose failure mode can block the whole daemon.

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
`crossbeam_channel::bounded(0)`, **rendezvous** channels. Neither half
completes unless the session's shell→client thread is sitting in its `select!`
loop ready to receive.

A client whose socket has stopped draining, a stalled ssh window, a suspended
laptop, a terminal tab that went away without closing cleanly, leaves that
thread blocked in `write()` to the client socket instead. The detach then waits
forever while holding the global lock, and every `list`, `attach`, `detach` and
`kill` in the daemon queues behind it. The daemon still accepts connections
(its main thread stays healthy in `accept()`), which is what makes this look
like a hang rather than a crash.

**One unresponsive client window takes down every session in the daemon.**

Log signature, a `handle_detach` span that opens and never closes, followed by
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

This is not a new design, it is the pattern upstream already uses for the
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
uninvestigated, the containment is deliberate, because no single-session fault
should be able to take the daemon down regardless of its cause.

Build, activation and rollback are the same as for `0001`, including the
daemon-restart requirement. Because a restart ends every managed terminal
process, capture exact provider recovery identities first, after the restart
they come back through the one recovery list, which the login chooser's Closed
sessions screen and `sp recover` both read.

## Exit-status patch (0005)

`shpool attach` exits 1 instead of the shell's real exit status whenever the
terminal closes the client's stdin before the status frame arrives.

The attach client runs two threads over the daemon socket, `stdin->sock` and
`sock->stdout`, with a coordinator loop that watches both. The moment
**either** finishes, the coordinator stamps its fallback status of 1 into the
shared result slot so the other thread will notice and stop:

```rust
if nfinished_threads > 0 {
    let mut res = result_slot.lock().unwrap();
    if res.is_none() {
        *res = Some(PipeBytesResult::Exit(1));   // protocol.rs:405
    }
    ...
```

A finished `stdin->sock` thread only means the local input side is done: our
stdin hit EOF. It says nothing about the session, which may be about to report
the shell's status. But `sock->stdout` checks that slot at the top of its loop,
so it returns without ever reading the `ExitStatus` frame, and the client
reports 1.

The observable ordering is sufficient: stdin reaches EOF, the shared slot is
set to the fallback, the socket reader exits before consuming `ExitStatus`, and
the daemon then has no client for the real status. The broken pipe sets the connection to
`Disconnect`, so the hangup path's `if let ClientConnectionMsg::New(conn)`
no longer matches and the exit chunk is never written at all.

`0005-attach-must-not-discard-the-shells-exit-status.patch` waits the
**existing** `MAX_DETACH_WAIT_DUR` (300ms) window for the socket side before
defaulting, and only when the stdin side is the one that finished.
`common::sleep_unless` evaluates its predicate before sleeping and returns as
soon as the frame lands or the daemon closes the connection, so the ordinary
case costs one predicate call rather than the whole window.

### Evidence

Upstream's own `exits_with_same_status_as_shell` is the reproducer. Repeat it
under load on both pristine and patched trees; the pristine tree must expose
the mismatched status and the patched tree must preserve the shell status.
The full workspace suite must remain green, including
`detaches_on_null_stdin` and `client_eof_does_not_spin`, the two tests that
depend on the current early-exit behaviour.

### Limits

The wait is bounded by a constant upstream already uses for this purpose, so a
session that genuinely never reports a status still exits after 300ms rather
than hanging. The patch does not change what the daemon sends, only whether
the client is still listening when it arrives.

This is an upstream defect, not a Session Kit one, and belongs upstream. Prefer
an accepted upstream fix over carrying this patch.

## Resize coalescing patch (0006)

A fullscreen application can emit a burst of `SIGWINCH` events while a terminal
is being resized. Stock shpool forwards every event immediately, so the attached
application repaints once per intermediate size and stale frames can accumulate
in scrollback.

`0006-coalesce-client-resize-bursts.patch` waits 120 ms for the reported size
and pending signal queue to settle, then forwards one resize carrying the final
row and column count. It also remembers the last forwarded size and skips an
unchanged duplicate. The wait runs only after a resize signal and is bounded by
continued size movement; ordinary input and output are unchanged.

Build, activation, rollback, and fingerprint recording are the same as for
`0001`. When carrying it with other patches, apply it last.

## License and upstream

The patch modifies Apache-2.0 licensed shpool source. See
[Third-party notices](../THIRD_PARTY_NOTICES) and
[Apache License 2.0](../LICENSES/Apache-2.0.txt).

Prefer an upstream release containing the accepted fix over carrying a local
patch.
