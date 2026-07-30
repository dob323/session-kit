# Maintainer release process

This document describes the public release standard. It is not an end-user
installation guide.

## Version policy

The first public release is `v0.1.0` and has beta maturity. Do not move or reuse
an existing tag. Create a new annotated tag only after every release gate
passes.

Follow Semantic Versioning. Before `1.0.0`, document incompatible configuration,
state, or command changes prominently.

## Release gates

1. The Git tree and reachable history pass credential and private-data scans.
2. No account names, private paths, real UUIDs, transcripts, or internal
   service dependencies remain.
3. `LICENSE`, `THIRD_PARTY_NOTICES`, `SECURITY.md`, the changelog, and public
   documentation match the release.
4. The required Ubuntu 22.04/24.04 and Python 3.10-3.13 CI matrix passes.
5. The hosted macOS 15 preview smoke passes and remains clearly separate from
   dedicated-Mac acceptance.
6. Fresh install passes under a disposable Linux account.
7. Update from the prior public release passes with active detached sessions.
8. Rollback restores the earlier helpers, integration marker, configuration,
   and state interpretation without restarting shpool.
9. Uninstall preserves state and journals by default.
10. Guided journals are enabled by default only after their data warning is
   shown.
11. Watchdog behavior is report-only and notifications are opt-in.
12. The macOS preview remains off unless the same candidate passes a real-Mac
    core lifecycle test.
13. The full test suite, Bash syntax checks, ShellCheck, Ruff, Python syntax,
    the branch-coverage floor, link checks, and documentation privacy checks
    pass.

## Build and install a candidate

Use a fresh clone of the exact candidate:

```bash
git clone https://github.com/dob323/session-kit.git session-kit-candidate
cd session-kit-candidate
git checkout <full-candidate-commit>
test -z "$(git status --short)"
./install.sh --check
tests/run
./install.sh
session-kit doctor
```

Confirm the installed `current` link resolves to the exact candidate commit and
that each stable helper dispatches into that immutable directory.

Release metadata schema 1 describes the pre-package layout. Schema 2 requires
both files in `lib/sessionkit_inventory/`. New builds always emit schema 2; the
verifier retains the bounded schema-1 contract so existing immutable releases
remain valid rollback targets.

## Activation

The installer must not start or restart services. Review the copied systemd
units, run `session-kit doctor`, and enable only the components required by the
acceptance scenario. Record the active-session inventory before and after; the
daemon and existing provider process generations must not change.

## Release notes

Release notes should include:

- user-visible changes;
- exact support matrix;
- configuration or state migration;
- journal and privacy effect;
- active-session effect;
- known limitations;
- rollback requirements;
- optional shpool patch status.

## Tag and publish

After the candidate commit and clean-machine artifacts pass:

1. update `CHANGELOG.md` with the release date;
2. confirm `main` points to the tested commit;
3. create an annotated `v0.1.0` tag;
4. push the branch and tag;
5. publish matching GitHub release notes;
6. verify repository links and downloadable artifacts from a logged-out view.

Changing repository visibility is a separate administrative action. Perform it
only after the final privacy and history audit.
