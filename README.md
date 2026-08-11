# Session Kit

[![CI](https://github.com/dob323/session-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/dob323/session-kit/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/dob323/session-kit?include_prereleases)](https://github.com/dob323/session-kit/releases)
[![License](https://img.shields.io/github/license/dob323/session-kit)](LICENSE)

A local status and safety layer for shpool sessions running Claude Code, Codex,
or shells over SSH.

![Session Kit dashboard showing ready and open sessions grouped by provider](docs/assets/session-kit-dashboard.png)

The image is rendered from the real picker with demo-only session data by
`tools/render-readme-dashboard`; it is not a mock of a different interface.

Session Kit gives each managed terminal a stable number, useful name, color,
reply state, and exact provider identity. It keeps the terminal alive when a
provider exits and refuses actions when live identity cannot be proved.

> [!WARNING]
> Session Kit is a public beta for Linux with systemd and macOS 14 or newer.
> Start on a single-user account where Claude Code and Codex conversations can
> be recovered. Session Kit is not a boundary against another process running
> as the same Unix user.

## Highlights

- One on-demand picker for Claude Code, Codex, and shell sessions.
- First-install discovery of project folders already known to Claude Code and
  Codex, with no filesystem-wide scan.
- `needs your reply`, working, idle, quiet, provider-exited, and subagent state.
- Exact open, move, close, reopen, fork, repair, and recovery checks.
- Separate Claude Code and Codex subscription profiles, selected when a session
  starts or changed later with an explicit confirmation.
- Manual names plus guarded 2–5 word agent self-names.
- Per-provider colors, so two live sessions do not share one until the palette
  runs out, and a Claude session and a Codex session never share one at all.
- A visible `title pending` state when a Codex bar still needs a safe refresh.
- Immutable local releases with atomic update and rollback.
- Optional terminal journals, off by default.
- No Session Kit hosted account, analytics, update beacon, or telemetry.

Normal rows hide shpool IDs and provider UUIDs. Use `sp detail`, JSON output, or
an explicit search when diagnosis requires exact identity.

## Install the beta release

Download the archive, checksum, and provenance files attached to the
[`v0.3.0` release](https://github.com/dob323/session-kit/releases/tag/v0.3.0).
Beta releases are published as GitHub prereleases, so `releases/latest` does not
resolve to them; browse [all releases](https://github.com/dob323/session-kit/releases)
or name the tag explicitly, as below. Release assets are named by commit, not by
version. With the GitHub CLI:

```bash
mkdir session-kit-download
cd session-kit-download
gh release download v0.3.0 --repo dob323/session-kit
if command -v sha256sum >/dev/null; then
  sha256sum --check session-kit-*.sha256
else
  shasum -a 256 --check session-kit-*.sha256
fi
tar -xzf session-kit-*.tar.gz
cd session-kit-*/
./install.sh --check
./install.sh
session-kit doctor
session-kit services enable
```

The preflight is read-only. Installation copies an immutable release and
systemd or launchd definitions, but it does not start, stop, restart, or enable
a service. Review the definitions before `services enable`. The guided
installer can add the shell integration; journals remain off unless you
explicitly enable them. On a new interactive installation, it also shows the
existing project folders recorded by Claude Code and Codex and offers to import
them as local Session Kit shortcuts.

For requirements, manual asset download, and activation checks, read
[Install Session Kit](docs/install.md). Use `main` only for development after
reviewing and testing its exact commit.

## Use it

SSH opens a normal shell. Type `kit` when you want the session picker.

```text
kit
sp list
sp account list
sp account enroll <claude|codex> <alias> <email>
sp account verify <claude|codex> <alias>
sp new [claude|codex|shell] [project-alias] [--account <alias>]
session-kit projects discover
session-kit projects import
session-kit projects candidates
session-kit projects import --select 1,3-4
session-kit projects add <alias> <claude|codex|shell> /absolute/path
session-kit projects here
session-kit projects ignore /absolute/path
sp go <terminal-number|shpool-id>
sp takeover <terminal-number|shpool-id>
sp name <terminal-number|shpool-id> <title>
sp name reset <terminal-number|shpool-id>
sp close <terminal-number|shpool-id>
sp repair <terminal-number|shpool-id>
sp detail <terminal-number|shpool-id>
sp find <text>
sp history <terminal-number|shpool-id>
sp health
sp recover
sp prune
```

The picker accepts one visible number to open a session. `k` accepts visible
numbers, comma-separated lists, and small ranges. Every action uses a frozen
private proof and rechecks live identity immediately before changing anything.
A cached or stale dashboard is read-only.

Claude Code profiles keep their provider state in separate
`CLAUDE_CONFIG_DIR` directories. Codex profiles use separate `CODEX_HOME`
directories. Session Kit stores the alias and verified account description, not
provider tokens. It never copies credentials between profiles, and it never
changes a live thread's account automatically. See [Use Session Kit](docs/usage.md#accounts)
for enrollment, guided creation, and guarded account changes.

If a new Codex process started before its thread acquired a name, the row shows
`title pending`. A detached, proven-idle provider can refresh automatically on
open. An attached provider is never restarted automatically; its action menu
offers an explicit refresh only when the exact provider is idle and has no
subagents.

## shpool

Session Kit runs on top of [shpool](https://github.com/shell-pool/shpool) and
does not vendor or replace it. `shpool-patch/` carries local patches against
released shpool versions, each with the evidence that justified it and the
conditions under which it should not be applied. One of them, `0004`, fixes a
detach deadlock in shpool 0.11.0 that can freeze every managed session at once;
read [the patch notes](shpool-patch/README.md) before deciding what to run.

Rebuilding or reinstalling shpool replaces the binary you patched. `session-kit
doctor` records the shpool binary it validated at install time and warns when it
changes, so a silent downgrade is caught by a health check rather than by a
frozen terminal.

## Safety and privacy

Session Kit treats a provider UUID and exact process generation as identity.
Titles, numbers, directories, timestamps, and terminal output are display
context only. Missing, duplicated, changed, or partial evidence fails closed.

The default local footprint is privacy-minimal:

- journals and notifications are off;
- provider transcripts remain in provider-owned storage;
- the picker action log stores only fixed action and outcome labels;
- private state is owner-only and never uploaded by Session Kit.

Optional journals can contain prompts, credentials, source code, and command
output. Read [Security and local data](docs/security-and-data.md) before
enabling them.

The watchdog raises no alert anywhere until you configure a notifier. It detects
and logs either way, but with nothing wired up the only record is the owner-only
watchdog log. See [Watchdog alerts](docs/configuration.md#watchdog-alerts).

## Documentation

- [Install](docs/install.md)
- [Configure](docs/configuration.md)
- [Use Session Kit](docs/usage.md)
- [Projects](docs/projects.md)
- [Message your sessions](docs/messaging.md)
- [The Fleet Supervisor](docs/supervisor.md)
- [Claude Code and Codex integration](docs/provider-integration.md)
- [Security and local data](docs/security-and-data.md)
- [Troubleshoot](docs/troubleshooting.md)
- [Update and roll back](docs/update-and-rollback.md)
- [Uninstall](docs/uninstall.md)
- [Architecture](docs/architecture.md)
- [Maintainer release process](docs/maintainers/release-process.md)

## Contributing and support

Bug reports, feature requests, documentation fixes, and pull requests are
welcome. Reports about provider compatibility, lifecycle safety, privacy, and
clean installation are the most useful, especially from operating systems and
hardware the maintainer cannot test.

Session Kit is maintained by one person alongside other work. Replies are
best-effort rather than guaranteed, and a pull request may be declined when it
would weaken the identity and safety model even if the code is sound. Read
[Contributing](CONTRIBUTING.md) before opening a pull request.

Report vulnerabilities through the [Security policy](SECURITY.md), never through
a public issue.

## License

Session Kit is released under the [MIT License](LICENSE). The optional shpool
patches modify Apache-2.0 software; see [Third-party notices](THIRD_PARTY_NOTICES)
and the included [Apache License 2.0](LICENSES/Apache-2.0.txt).
