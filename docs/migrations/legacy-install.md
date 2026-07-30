# Migrate a legacy installation

This guide defines the safety requirements for moving an existing hand-managed
shpool setup into Session Kit. The portable guided migration is planned for
`v0.1.0`; do not substitute manual file copying.

## Why migration is different

A legacy installation may already have:

- attached and detached shpool sessions;
- provider conversations with active UUIDs;
- shell startup code;
- custom shpool configuration and user units;
- journal files or recovery records;
- aliases stored in an older location;
- one-shot launch records from an earlier Session Kit revision.

Those files may be live inputs. A migration must preserve their bytes and
identity until the target release proves it can interpret them.

## Required pre-migration evidence

The guided migrator must collect a private backup containing:

- the exact committed source or installed helper set;
- shell and shpool configuration files it may replace;
- relevant systemd user units;
- one frozen shpool list;
- one exact provider identity inventory;
- checksums, modes, owners, and file types;
- a completion marker bound to the backup manifest.

Backup verification must reconstruct the installable file set in an isolated
directory and compare it before migration begins.

## Migration sequence

1. Place picker, reaper, watchdog, and new-journal safety sentinels.
2. Verify the backup.
3. Build and verify the target immutable release.
4. Dry-run stable launcher bootstrap.
5. Back up and validate host configuration in the target format.
6. Replace shell integration with one guarded source line.
7. Migrate aliases under the private configuration locks.
8. Plan legacy recovery-manifest conversion from frozen and current identity
   evidence.
9. Review every carried and ended conversation before applying the plan.
10. Take one strict live inventory and render that frozen snapshot.
11. Publish the target integration marker only after all checks pass.
12. Verify ordinary listing and a new login without closing, moving, repairing,
    or recovering an active session.
13. Retain safety sentinels until rollback is independently reviewed.

Any changed daemon generation, ambiguous provider identity, unexpected record
shape, unsafe file type, missing archive, or configuration mismatch is a stop
condition.

## Alias migration

Legacy and canonical aliases may contain the same provider UUID. The migration
must define one precedence rule, preserve unrelated configuration keys, retain
the old bytes in a private archive, and prove an idempotent second run.

Rollback must restore both sides of the legacy overlay. Restoring only one can
lose a title or change precedence.

## Recovery-manifest migration

Plan and apply must be separate operations. The plan should bind:

- exact legacy manifest bytes;
- pre-migration identity evidence;
- current daemon generation;
- current live provider identities;
- target release commit.

Apply must take the inventory lock, recheck every input, archive the legacy
bytes before replacement, and publish a private receipt.

Rollback must refuse a changed canonical manifest, pending recovery work,
changed live identity set, missing archive, or mismatched receipt.

## Retained launch records

Never let an older release interpret a newer launch-record shape. A blocked
pair is not permission to delete or rewrite it.

Prefer returning to a compatible release and completing exact startup proof. If
urgent rollback requires quarantine, preserve checksummed byte-exact copies,
move the complete pair together under the session creation lock, and record its
original paths, metadata, reason, and current shpool identity.

## Completion

Migration is complete only when:

- the target release and integration marker agree;
- strict inventory has no unexplained warning;
- current sessions retain their identities and attachment state;
- shell integration works in a fresh login;
- rollback dry-run passes;
- backups, archives, and receipts remain private and verified.
