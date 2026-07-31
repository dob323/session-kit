# Maintainer release process

This is the public release checklist, not an installation guide.

## Version and support policy

The first release is `v0.1.0` with beta maturity. Follow Semantic Versioning
and never move or reuse a tag.

Before `1.0.0`, document incompatible command, configuration, or state changes
in the changelog. Until the first tag, only current `main` receives fixes. Each
beta minor line is supported for 90 days after its first release or for 30 days
after the next beta minor release, whichever ends later. Security fixes may
require upgrading to the latest patch.

## Release gates

1. The public tree and reachable Git history pass secret and private-data scans.
2. No private account names, paths, hostnames, UUIDs, transcripts, incident
   timestamps, credentials, or internal services remain.
3. License, notices, security policy, changelog, support policy, and behavior
   documentation agree.
4. Ubuntu 22.04 and 24.04 pass on Python 3.10 through 3.13.
5. Bash syntax, ShellCheck, Ruff, Python syntax, type baseline, and branch
   coverage pass.
6. Local documentation links pass.
7. The optional shpool patch applies to its pinned upstream tag and builds.
8. A clean disposable Linux account passes preflight, install, doctor, picker,
   provider exit, exact reopen, kill confirmation, update, rollback, and
   uninstall checks.
9. macOS and other unsupported platforms fail closed.
10. Journals and notifications default off.
11. Normal logs contain no prompt, response, terminal, credential, or journal
    content.
12. Automatic cleanup begins only after its timer is enabled and then requires
    72 continuous hours with every exact predicate unchanged.
13. The public export includes every runtime, test, license, and documentation
    file declared by the reviewed manifest.
14. The release archive is reproducible and its checksum and provenance record
    match the candidate commit.
15. The public repository has no earlier or conflicting tag that changes the
    declared first-release version. Private-source tags are not copied into the
    public history.

## Build a candidate

Use a fresh clone at one exact commit:

```bash
git clone https://github.com/dob323/session-kit.git session-kit-candidate
cd session-kit-candidate
git checkout <full-candidate-commit>
test -z "$(git status --short)"
./install.sh --check
tests/run
tools/check-doc-links
tools/public-scan . --git-history
```

The private source history may contain private operational context, so that
command scans it for credentials without treating known private project names
as a release failure. After the exported tree is committed in the public
repository, run the strict gate there:

```bash
tools/public-scan . --git-history --private-markers
```

Do not publish a history that fails the strict gate. Rewriting a branch does
not remove data from existing clones; investigate any credential match and
rotate the credential before publication.

Build the reviewed public tree and release artifact outside the source:

```bash
tools/build-public-tree \
  --commit <full-candidate-commit> \
  --destination ../session-kit-public
tools/build-release-artifact \
  --commit <full-candidate-commit> \
  --output-dir ../session-kit-artifacts
```

Rebuild the artifact in another empty directory and compare the archive,
checksum, and provenance bytes.

Release metadata schema 1 is the pre-package layout. Schema 2 adds the package
marker and common helpers. Schema 3 also requires lifecycle, provider, and
private-state modules. New builds emit schema 3; verification retains the
bounded older schemas for installed rollback targets.

## Acceptance

The installer must not start or restart services. Record process and daemon
generations before and after each acceptance step.

Test at least:

- fresh login and normal terminal fallback;
- Claude Code and Codex reply alerts;
- semantic color and no-color output;
- IDs hidden on normal rows and present in detail, JSON, and explicit search;
- provider exit leaving the managed terminal alive;
- exact reopen without latest-directory fallback;
- same-session move between two SSH windows;
- `k <number>` exact-ID confirmation and cached-state refusal;
- 72-hour cleanup boundary after timer enablement, reset conditions, and
  retained provider history;
- update and rollback with detached sessions;
- uninstall retaining private data.

## Publish

After every gate passes:

1. add the release date to `CHANGELOG.md`;
2. confirm `main` points to the accepted commit;
3. create one annotated tag;
4. push the branch and tag;
5. publish matching notes, archive, checksum, and provenance file;
6. verify links and downloads from a logged-out view.

Repository visibility and GitHub security settings are separate administrative
changes. Perform them only after the final repository and history audit.
