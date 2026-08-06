# Migrate an older installation

This page is a procedure, not a command. Session Kit ships no migration tool for
a hand-managed shpool setup, and the public beta promises no automatic migration
from an unversioned private layout. What follows is the sequence that keeps live
sessions, provider identities, and recovery state intact while you move to a
released installation.

The safest migration is not a migration. A new dedicated account gets a clean
install with nothing to convert, and the old account can be retired at leisure
once its sessions are closed. Convert in place only when you have a reviewed
plan for one exact source layout.

What makes an in-place migration dangerous is that the source state is live
while you copy it. A hand-managed setup can hold running sessions, provider
identities, shell startup code, service units, aliases, journals, and paired
launch records, and every one of those can change under a copy. Do not copy
files into a current installation while any writer is still running.

Session Kit will not overwrite what it finds. `install.sh` writes
`projects.tsv`, `inventory.json`, and `~/.config/shpool/config.toml` only when
they are absent, and on macOS it refuses a shpool `shell` setting that conflicts
with the validated modern Bash rather than rewriting it. That protects an
existing setup from a careless install, and it also means a fresh install alone
converts nothing: whatever the old layout holds is still the old layout's.

## Capture the evidence first

Before changing anything, collect a private backup of:

- the installed helper commit, or the exact file set if there is no commit;
- the Bash and shpool configuration that the migration may change;
- the relevant systemd user units or LaunchAgents;
- one frozen `shpool list --json` capture;
- one exact provider identity inventory;
- file checksums, modes, owners, and types;
- a completion marker tied to the backup manifest.

Verify the backup in an isolated directory rather than in place. It can contain
UUIDs, private paths, and terminal data, so treat it as private data and never
let it reach the public repository.

## The sequence

1. Stop every writer of the mutable source state — the automatic picker,
   cleanup, watchdog, new journals, and anything else that can touch it — and
   verify they are stopped before you copy a byte.
2. Capture a second frozen shpool list and provider inventory. Stop if the
   daemon generation or the managed-session set moved since the first capture:
   the source was not frozen, so the first capture describes a system that no
   longer exists.
3. Verify the backup.
4. Run the read-only preflight against the target source, `./install.sh
   --check`. It validates the platform, the required commands, the source
   provenance, and the lifecycle roots without writing anything, and it refuses
   a Git worktree with uncommitted or untracked files.
5. Install the target release without activating it: `./install.sh
   --non-interactive --no-import-projects` creates the immutable release,
   selects it through `current`, installs the stable launchers, and writes
   inactive service definitions. On a first install, non-interactive leaves the
   shell integration and journals off unless their flags are supplied, and no
   installation path starts a service.
6. Copy the mutable configuration and state once, from the frozen source.
   Verify the copied bytes, checksums, file types, modes, and owners before
   anything reads them as live.
7. Convert the host configuration without touching active sessions.
8. Replace the old shell integration with one guarded source block.
9. Convert names under the private configuration lock,
   `${XDG_STATE_HOME:-$HOME/.local/state}/session-kit/config.lock`, which is the
   lock the kit's own name and color writers hold.
10. Plan the recovery-state conversion — `recovery-manifest.json` and
    `recovery-pending.json` — from the frozen and current identity evidence.
11. Review each affected conversation before applying that plan.
12. Take a strict live inventory and compare generations.
13. Publish the target integration marker and start the target services — now,
    and only after every copy, conversion, and verification check has passed.
14. Verify listing and a fresh login without moving, killing, repairing, or
    recovering an active session.
15. Keep the rollback material until an independent rollback check passes.

Never copy or synchronize mutable state over a target whose Session Kit services
are already running. If a target writer started too early, stop it with the
required operational approval, freeze the source again, and repeat the copy and
verification from the beginning. A later overwrite is not equivalent evidence:
it proves the final bytes, not that nothing read the intermediate ones.

Stop on a changed daemon generation, an ambiguous provider identity, an unsafe
file, an unexpected state shape, a missing backup, or a configuration mismatch.

## Names and recovery

If the old and current title maps hold the same UUID, decide one precedence rule
before writing either. Preserve unrelated keys and keep the old bytes in the
private backup.

Recovery conversion separates plan from apply. Bind the plan to the exact old
bytes, the target commit, the daemon generation, and the current provider
identities; apply it under the inventory lock; publish a private receipt.

Launch records come in pairs. Each managed start writes `<shpool-id>` and
`<shpool-id>.expected` into the launch-record directory,
`~/.local/state/shpool-start/` by default, and rollback validation reads both: a
mismatched pair, a duplicate record, an unsafe file type, or a directory that is
not owner-only mode `0700` blocks the rollback outright.
Deleting one side of a pair to quiet a complaint removes the evidence and keeps
the block. Keep both records together and return to a release that understands
their format instead.

## Completion

Migration is complete when the selected release and the integration marker
agree, a strict inventory carries no unexplained warning, current sessions
retain their identity and attachment state, a fresh login works, the rollback
checks pass, and every private archive still verifies. Until all six hold, the
old installation is still the one you can fall back to — keep it.
