# Migrate an older installation

An older hand-managed shpool setup may contain live sessions, provider
identities, shell startup code, service units, aliases, journals, and launch
records. Do not copy files into a current installation while those inputs are
live.

The public beta does not promise an automatic migration from unversioned
private layouts. A safe migration starts with a new dedicated account or uses a
reviewed plan for one exact source layout.

## Required evidence

Before changing anything, collect a private backup of:

- the installed helper commit or exact file set;
- Bash and shpool configuration that may change;
- relevant systemd user units;
- one frozen shpool list;
- one exact provider identity inventory;
- file checksums, modes, owners, and types;
- a completion marker tied to the backup manifest.

Verify the backup in an isolated directory. It may contain UUIDs, paths, and
terminal data and must never enter the public repository.

## Safe sequence

1. Disable automatic picker, cleanup, watchdog, and new journals.
2. Verify the backup.
3. Build and verify the target immutable release.
4. Dry-run stable launcher installation.
5. Convert host configuration without changing active sessions.
6. Replace shell integration with one guarded source block.
7. Convert names under the private configuration lock.
8. Plan recovery-state conversion from frozen and current identity evidence.
9. Review each affected conversation before applying the plan.
10. Take a strict live inventory and compare generations.
11. Publish the target integration marker only after checks pass.
12. Verify listing and a new login without moving, killing, repairing, or
    recovering an active session.
13. Keep the rollback material until an independent rollback check passes.

Stop on a changed daemon generation, ambiguous provider identity, unsafe file,
unexpected state shape, missing backup, or configuration mismatch.

## Names and recovery

If old and current title maps contain the same UUID, define one precedence rule
before writing. Preserve unrelated keys and keep the old bytes in the private
backup.

Recovery conversion must separate plan from apply. Bind the plan to the exact
old bytes, target commit, daemon generation, and current provider identities.
Apply under the inventory lock and publish a private receipt.

Never let an older release interpret a newer paired launch-record format. Keep
both records together and return to a compatible release instead of deleting
one side.

## Completion

Migration is complete only when the selected release and integration marker
agree, strict inventory has no unexplained warning, current sessions retain
identity and attachment state, a fresh login works, rollback checks pass, and
all private archives remain verified.
