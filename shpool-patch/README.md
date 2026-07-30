# shpool patch: a busy session handler must not be treated as a dead client

## What this fixes

A session becomes permanently unopenable, and whatever runs inside it freezes
for good, while the session list still reports it as running. Observed four
times in 24 hours on 2026-07-28/29, costing about 8 hours on one Codex run and
7.5 on another. Two sessions were lost in the same second when a single
connection stalled.

The daemon log of a live incident:

```
17:03:52  heartbeat{s="…"}: close  time.idle=3.24s
17:03:52  telling shell->client to disconnect
17:03:52  failed to tell shell->client to disconnect: "SendTimeoutError(..)"
          ERROR error shuffling bytes: outer thread scope
          Caused by: 0: joining heartbeat_h
                     1: waiting for heartbeat ack
                     2: timed out waiting on receive operation
17:06:35  initial_attach_lock(shell_to_client_ctl): close time.busy=300ms
          ERROR error shuffling bytes: attaching new client stream to
                shell->client thread
```

Once that thread is gone the session has no handler: every later attach fails
after 300 ms, nothing drains its terminal, and the program inside blocks on its
next write.

## Why it happens

`libshpool/src/daemon/shell.rs`, in `spawn_heartbeat`, handles the *send* half
and the *ack* half of the same 300 ms budget inconsistently:

```rust
// send timeout — explicitly harmless:
Err(SendTimeoutError::Timeout(_)) => {
    continue;   // "If we get a timeout it doesn't necessarily mean that the
}               //  shell->client thread is unhealthy. It might just be busy
                //  doing other stuff. In particular, this comes up when the
                //  shell->client thread is generating a particularly large
                //  session restore buffer, which can take a minute."

// ack receive timeout, twelve lines later — fatal:
Err(e) => return Err(e).context("waiting for heartbeat ack"),
```

That error unwinds the whole thread scope, taking the session's `shell->client`
thread with it. So the authors' own reasoning — a busy thread must not be judged
dead — is contradicted one match arm later.

## The change

Handle the ack timeout the way the send timeout is already handled: `continue`.
That is the entire behavioural change. The compiler then reports the following
generic `Err(e)` arm as unreachable, which is the proof that
`RecvTimeoutError` has only these two variants — so after this change **no path
remains where an ack timeout can destroy a session's handler**. The dead arm is
removed.

A genuinely dead client is still detected, unchanged: the write to
`client_stream` fails and takes the existing "assuming hangup" path.

## Honest limits

This was **not** reproduced synthetically. Two attempts — a SIGSTOPped client,
and a client killed repeatedly while a large restore buffer was being built —
left both the patched and the stock binary healthy. When the client is fully
stopped the *send* times out first and is already handled safely, so the fault
needs a narrower coincidence: the handler must receive the heartbeat and then be
too busy to answer within 300 ms. That matches the observed rate of a few times
a day rather than every stall.

The case for the patch is therefore the production logs plus the code being
provably exhaustive, not a before/after demonstration. It can only remove a
fatal path; it cannot add one.

## Rebuilding

The host has no C toolchain, so build in a container. `rust:alpine` produces the
same static musl binary shape as the stock build.

```sh
git clone https://github.com/shell-pool/shpool.git ~/shpool-src
cd ~/shpool-src && git checkout v0.11.0
git apply /path/to/shpool-patch/0001-heartbeat-ack-timeout-is-not-fatal.patch
docker run --rm --cpus 4 -v ~/shpool-src:/src -w /src \
  -e CARGO_HOME=/src/.cargo-home rust:alpine \
  sh -c 'cargo build --release --bin shpool'
# expect: "Finished `release` profile", no warnings
sha256sum target/release/shpool   # compare with patched-binary.sha256
```

Installing it replaces `~/.cargo/bin/shpool` and needs the daemon restarted,
which ends every session. Conversations survive and are restored; keep the
previous binary alongside so rolling back is a single copy.

After installing, record the new fingerprint so the watchdog can tell if the
binary is ever replaced:

```sh
sha256sum ~/.cargo/bin/shpool | cut -d' ' -f1 \
  > ~/.local/state/session-kit/shpool-binary.sha256
```

## Upstream

Worth reporting: their own comment justifies the fix. The same class of fault
has been addressed repeatedly — "deadlock when shell->client thread stops"
(0.8.0), "reduce deadlock potential in shell->client" (0.8.1), "Add timeouts to
prevent session message deadlocks" (0.6.1) — so a fix in their tree is more
durable than carrying this patch.
