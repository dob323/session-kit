# Session Kit

Session Kit gives shpool users one clear view of Claude Code, Codex, and shell
sessions over SSH. It adds stable terminal numbers, useful names, reply alerts,
exact recovery, and guarded session actions.

> [!WARNING]
> Session Kit is a Linux-only `v0.1.0` beta. Use it first on an account where
> every provider conversation can be recovered. macOS and non-systemd systems
> fail closed and are not supported.

## What the dashboard shows

```text
  4 sessions · 1 ready here · 3 open elsewhere

  Ready to open
    Needs your reply · Codex
       4  Review Login Errors                    | ! needs your reply

  Open elsewhere
    Claude
       1  Improve Session Picker                 | working
    Codex
       3  Document Release Process               | quiet 2h 18m

  Open: number · New: n · Kill: k number
  Terminal: Enter · Search: /text · Help: ?
```

Colors have one meaning throughout the picker: provider headings identify the
provider, availability headings identify where a session can open, warnings
mark attention or risk, and muted text carries secondary status. Set
`NO_COLOR=1` or `SESSION_KIT_NO_COLOR=1` to disable color.

Normal rows omit internal shpool IDs and provider UUIDs. Use `sp detail`, JSON
output, or an explicit search when troubleshooting requires an exact identity.
Display numbers and titles are convenient selectors, not recovery authority.

## What it provides

- A compact SSH login picker grouped by availability and provider.
- `needs your reply` for unresolved structured Claude Code and Codex questions.
- Stable terminal numbers for the current host boot.
- Manual names and guarded automatic 2–5 word task names.
- Exact Claude Code and Codex recovery by conversation UUID.
- Confirmed open, move, kill, repair, recovery, and cleanup actions.
- A persistent managed terminal when Claude Code or Codex exits.
- Optional local terminal journals, off by default.
- Local, report-only health checks.
- Immutable installed releases with rollback.

When a provider exits, the shpool terminal stays alive. The dashboard shows that
the provider exited and offers a clear path to reopen the exact conversation or
close the terminal. Session Kit never substitutes the newest conversation or a
matching directory.

Automatic close begins only after the cleanup timer is enabled and the same
exact safe state has been observed continuously for 72 hours. Cleanup requires
the exact terminal generation, provider identity, exited state, no attachment,
no child work, no pending reply, and no changed evidence. Any missing or
changed predicate keeps the terminal.

## Supported target

The beta supports:

- Linux with `/proc` and systemd user services;
- Bash;
- Python 3.10 through 3.13;
- Ubuntu 22.04 and 24.04 in CI;
- shpool 0.11.0;
- Claude Code, Codex, and ordinary shpool shells.

macOS, other init systems, other shells, containers without the required
process view, and multi-user hostile-account isolation are not supported.
Unsupported platforms stop before installation or mutation.

Until `v0.1.0` is tagged, only the current `main` branch receives fixes. The
first beta will publish the exact provider versions used in release acceptance.
See [Security policy](SECURITY.md) for reporting and version support.

## Privacy

Session Kit is local software. It has no account, hosted service, analytics,
update beacon, or telemetry.

The default is privacy-minimal:

- terminal journals are off;
- notifications are off;
- the local picker-action log contains only fixed action and outcome labels,
  time, and schema version, with no session identity or content;
- normal dashboard rows hide internal IDs;
- provider transcripts stay in provider-owned storage.

The picker-action log is owner-only, keeps at most 1,000 records and 256 KiB,
and removes records older than seven days on each append.

Optional journals record terminal bytes and can contain prompts, credentials,
source code, and command output. Enable them only after choosing a retention
policy. Read [Security and local data](docs/security-and-data.md).

## Install

```bash
git clone https://github.com/dob323/session-kit.git
cd session-kit
./install.sh --check
tests/run
./install.sh
session-kit doctor
```

The preflight is read-only. Installation copies files and systemd user-unit
definitions but does not start, stop, restart, or enable a service. The guided
installer asks about the SSH picker and optional journals; journals default to
off. Noninteractive installation also leaves login integration and journals
off unless explicitly enabled.

Requirements and safe activation steps are in [Install Session Kit](docs/install.md).

## Everyday commands

```text
sp list
sp new [claude|codex|shell] [project-alias]
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

In the SSH picker, `k <number>` means kill the exact displayed session. It
always shows the resolved title, provider, and exact internal ID and requires
confirmation before changing anything. `x <number>` remains a compatibility
alias. A cached or stale dashboard disables actions.

See [Use Session Kit](docs/usage.md) for the complete behavior.

## Configuration and local data

Session Kit follows XDG directories:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/
${XDG_STATE_HOME:-$HOME/.local/state}/session-kit/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal/
```

Configuration and state can contain titles, working directories, process
metadata, UUIDs, and action receipts. Keep private directories mode `0700` and
private files mode `0600`.

Start with:

- [Inventory configuration](config/session-inventory.example.json)
- [Project aliases](config/projects.example.tsv)
- [shpool configuration](config/shpool.example.toml)

## Safety model

Before a mutation, Session Kit rechecks the daemon generation, shpool terminal
generation, process start times, provider ancestry, and conversation UUID. A
stale, ambiguous, partial, or unsafe identity is refused.

These checks protect against stale dashboard state. They are not a security
boundary against another process running as the same Unix user.

The installed watchdog defaults to report-only mode. It does not attach, move,
kill, restart, or repair sessions in that mode. An advanced repair mode remains
an explicit opt-in and can close and relaunch a proved-broken terminal; review
[Security and local data](docs/security-and-data.md) before considering it.

## Documentation

- [Install](docs/install.md)
- [Configure](docs/configuration.md)
- [Use Session Kit](docs/usage.md)
- [Claude Code and Codex integration](docs/provider-integration.md)
- [Security and local data](docs/security-and-data.md)
- [Troubleshoot](docs/troubleshooting.md)
- [Update and roll back](docs/update-and-rollback.md)
- [Uninstall](docs/uninstall.md)
- [Architecture](docs/architecture.md)
- [Maintainer release process](docs/maintainers/release-process.md)

## Contributing and security

Issues and pull requests are welcome. Read [Contributing](CONTRIBUTING.md).
Report vulnerabilities through the private process in
[Security policy](SECURITY.md), never through a public issue.

## License

Session Kit is released under the [MIT License](LICENSE). The optional shpool
patch modifies Apache-2.0 software; see
[Third-party notices](THIRD_PARTY_NOTICES) and the included
[Apache License 2.0](LICENSES/Apache-2.0.txt).
