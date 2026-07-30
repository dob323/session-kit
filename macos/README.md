# Experimental macOS core preview

This preview is opt-in and has not passed acceptance on a dedicated Mac:

```bash
export SESSION_KIT_MACOS_PREVIEW=1
```

It is limited to Bash and the core inventory, list/picker, new, attach, resume,
automatic naming, and journal paths. Linux remains the default. A modern Bash
is required; Apple's Bash 3.2 is not compatible with the existing session-kit
scripts. Ensure Bash 4 or newer resolves first in `PATH` and is the shell
started by shpool.

This lane does not add a macOS installer, launchd setup, release promotion, or
automatic upgrade path. A dedicated-Mac acceptance operator must assemble an
isolated release manually; passing that gate is required before installation
and service automation are designed.

## Safety boundary

Darwin process identity comes from `PROC_PIDTBSDINFO`. The recorded generation
is the native process start timestamp in epoch microseconds. The collector
reads that identity before and after `KERN_PROCARGS2`; a PID whose generation
changes during the read is discarded. Session names and Codex thread IDs come
only from that exact process environment.

Codex open-rollout descriptor inspection is Linux-only. On macOS, a valid
`CODEX_THREAD_ID` can establish conversation identity, but turn state is
reported as `state unavailable`.

The Darwin creation mutex is an atomic directory. It never removes a lock that
the current command did not acquire. If a command crashes while holding the
mutex, later creation and naming operations fail closed until an operator
inspects the owner file and explicitly removes the stale directory.

These maintenance paths are unavailable and make no change on macOS:

- watchdog repair
- reaper candidate generation and verification
- `sp prune`

## Dedicated-Mac acceptance gate

Do not describe the preview as supported until all of these pass on a dedicated
Mac with the intended shpool, Claude, Codex, Python, and Bash versions:

1. Confirm `proc_bsdinfo` layout and start timestamps on both an Apple Silicon
   machine and any supported Intel target.
2. Prove PID reuse cannot satisfy a saved generation, including rapid process
   churn between both identity reads.
3. Confirm `kern.boottime` is stable during one boot and changes after reboot.
4. Exercise list and picker with ready, attached, idle-shell, Claude, Codex,
   unnamed, and outside-shpool processes.
5. Exercise `new`, attach, takeover, Claude/Codex resume, and automatic naming;
   confirm every revalidation rejects a changed PID generation.
6. Verify Codex exports `CODEX_THREAD_ID` and Claude inventory returns exact
   root identities for the pinned provider versions.
7. Verify journal bytes and reconnect behavior with the native BSD `script`
   command, including control sequences, UTF-8, and large journals.
8. Confirm shpool launches the intended Bash 4-or-newer executable rather than
   Apple's `/bin/bash` 3.2.
9. Kill a creator while it owns the Darwin mutex; confirm subsequent work
   refuses to proceed and document the inspected manual recovery.
10. Confirm watchdog, reaper, and prune refuse operation before creating state
   or invoking shpool.
11. Run the complete Linux suite again and compare Linux inventory JSON and
    command behavior with the pre-preview release.
