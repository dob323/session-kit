# Maintainer release process

This is the public release checklist, not an installation guide. A release is
one reviewed commit carried unchanged through export, artifact creation, tag,
and release assets.

## Version and support policy

The release documented by this tree is `v0.4.0` and remains beta software.
Follow Semantic Versioning and never move or reuse a tag.

Before `1.0.0`, put every incompatible command, configuration, or private-state
change in the changelog. Each beta minor line is supported for 90 days after
its first release or for 30 days after the next beta minor release, whichever
ends later. A security fix may require upgrading to the latest patch.

## Release gates

1. Scan the public tree and its reachable history for secrets and private data.
   No private account names, hostnames, UUIDs, transcripts, incident details,
   credentials, or internal services may remain.
2. Make the README, changelog, notices, security policy, voice contract, and
   behavior documentation agree with the shipped code.
3. Run Bash syntax and ShellCheck, Python syntax and type checks, Ruff, branch
   coverage, documentation links, manifest coverage, and public-export tests.
4. Test Ubuntu 22.04 and 24.04 with supported Python versions. Run native macOS
   CI on Apple Silicon and Intel, and keep CI evidence distinct from real-device
   acceptance.
5. Apply all selected shpool patches to the pinned upstream commit, run its
   workspace tests, build it, and record the patch set, toolchain, target, host,
   command, and checksum.
6. On clean disposable accounts, test preflight, install, doctor, both picker
   screens, provider exit and exact reopen, close safety, display integration,
   update, rollback, and uninstall.
7. Confirm unsupported platforms and unsafe evidence fail closed. Journals,
   notifications, automatic repair, and automatic close must remain off by
   default.
8. Confirm ordinary logs contain no prompt, response, terminal, credential, or
   journal content. Automatic cleanup must still require 72 continuous hours
   with every exact safety predicate unchanged after its timer is enabled.
9. Confirm the export contains every file in the reviewed manifest, the release
   archive is reproducible, and the checksum and provenance record name the
   candidate commit.
10. Search every shipped README and documentation directory, including nested
    directories, for version strings. Reader-facing references must name
    `v0.4.0`; the changelog may retain older release headings.

## Build a candidate

Use a clean clone at one full commit and keep that commit fixed. Run the
repository's preflight and complete test suite, documentation-link checker, and
private-data scanner. Those source-maintainer programs live under `tools/` and
are intentionally absent from an installed machine.

Build the public tree into an empty directory outside the source worktree, then
run the strict private-marker scan against the exported tree and its history.
Build the release artifact from the same candidate commit into another empty
directory. Repeat the artifact build elsewhere and compare the archive,
checksum, and provenance bytes.

The release manifest uses `schema_version` 1. The lifecycle metadata uses
`lifecycle_schema_version` 2. Verification may understand older installed
rollback targets, but a new release must emit those current values; do not
describe internal package-layout generations as public schema versions.

## Acceptance

Install, update, and rollback perform a bounded service refresh. On Linux they
reload user-unit definitions, may enable only a newly introduced timer with no
prior enablement policy, and `try-restart` an already running watchdog. On
macOS they kickstart an already loaded watchdog. They never restart shpool or
the reaper. Record the selected release, service definitions, process
generations, and daemon generation before and after each lifecycle step.

Test at least:

- a normal shell login and an on-demand `kit` launch;
- the key-driven picker and the managed-Bash TUI opt-in and fallback;
- Claude Code and Codex attention, status, titles, semantic colors, and
  no-color output;
- IDs hidden on ordinary rows and available only in detail, JSON, and explicit
  search;
- provider exit leaving exact recoverable history, followed by one exact
  reopen without a latest-directory fallback;
- moving the same session between two terminals;
- key-driven `k`, list, range, and `all` targeting, plus TUI typed marks and
  the default Close action, all under frozen proof and cached-state refusal;
- the 72-hour cleanup boundary, every reset condition, and retained provider
  history;
- update and rollback while sessions stay detached and recoverable;
- uninstall retaining private data.

For macOS, also test on a release-listed device with macOS 14 or newer, Python
3.11 or newer, Homebrew Bash 4 or newer, and official shpool 0.11.0. Exercise
native process generations, rapid PID churn, stable boot identity, inventory,
both providers, SSH disconnect and reconnect, explicit service enable and safe
disable, watchdog report mode, repair-mode refusal, and lifecycle changes with
live sessions. Release notes must name the exact OS, architecture, provider,
Python, Bash, and shpool versions used, and say which evidence came only from
CI.

## Publish

Add the date to the `0.4.0` changelog section and commit it before publishing.
The source repository's release publisher is the only supported publication
path; it is a maintainer tool and is intentionally omitted from the public
export and installed tree.

First rehearse against throwaway clones. The rehearsal must export the chosen
commit, create the public commit and tag, build the artifact from that same
commit, and prove tag, archive, and export agree byte for byte. Then run the
same publication once for real, either printing the push and GitHub-release
commands for review or using its guarded push mode, never both.

Verify the generated chain record and inspect links and downloads from a
logged-out view. The publisher must refuse a dirty tree, unreachable commit,
existing tag, version drift, scan failure, or override during guarded push. Fix
the refusal; do not fall back to manual tagging. Repository visibility and
GitHub security settings remain separate administrative changes after the
final repository and history audit.
