# Session Kit

Session Kit is a local session manager for people who use
[shpool](https://github.com/shell-pool/shpool) with Claude Code, Codex, and
ordinary shells over SSH.

It adds a readable login picker, stable terminal numbers, task-focused names,
exact conversation recovery, local terminal journals, and guarded actions for
opening, moving, closing, or repairing a session.

> [!IMPORTANT]
> Session Kit is a **v0.1.0 beta candidate**. Use a dedicated account or a host
> where you can recover every provider conversation. Run the read-only
> preflight before installing, and review the local-journal privacy warning.

## Why Session Kit exists

Long-running terminal sessions are useful, but their raw names and attachment
state can become difficult to follow. Session Kit builds one local view from
shpool, Claude Code, Codex, and the process tree:

```text
  4 sessions · 1 ready here · 3 open elsewhere

  Ready to open
    Claude
       4  Review Login Errors     | needs your reply

  Open elsewhere
    Codex
       1  Improve Session Picker  | working
       3  Document Release Process | quiet 2h 18m
    Shell
       2  Idle shell
```

Session Kit keeps the provider conversation as the identity. A directory,
display number, title, or recent timestamp is never used as recovery authority.

## Project status and support

The first public release is planned as `v0.1.0`.

| Area | Status |
| --- | --- |
| Linux with systemd user services | Supported target |
| Bash | Supported shell |
| Claude Code and Codex | Supported providers |
| Ordinary shpool shells | Supported |
| macOS core commands | Preview, opt-in, and blocked on a real-Mac test |
| Other shells and init systems | Not supported |
| Telemetry or hosted service | None |

Session Kit is local-only. It does not send usage data. An administrator may
configure an optional notification command for watchdog reports; notifications
must remain off unless explicitly configured.

See [installation requirements](docs/install.md) and the
[security and data guide](docs/security-and-data.md) before using it with
valuable sessions.

## What it provides

- A compact SSH picker grouped by availability and provider.
- `needs your reply` for unresolved structured Claude Code or Codex questions.
- Stable terminal numbers for the current host boot.
- Manual aliases and guarded automatic 2–5 word task titles.
- Exact Claude Code and Codex recovery by conversation UUID.
- Append-only local terminal journals, enabled by the guided setup by default.
- Confirmed takeovers, closes, repairs, and pruning.
- A report-only watchdog for manager and terminal-health evidence.
- Immutable release directories with atomic selection and rollback support.

## Prerequisites

The supported target requires:

- Linux with `/proc` and systemd user services;
- Bash;
- Python 3.10 or newer;
- shpool 0.11.0;
- the standard Linux tools listed in [docs/install.md](docs/install.md);
- Claude Code, Codex, or both.

The official shpool 0.11.0 release is the default. Session Kit also carries an
optional, narrowly scoped shpool patch for a heartbeat acknowledgement timeout.
Read [shpool-patch/README.md](shpool-patch/README.md) before deciding whether to
build it. The patch is not required for evaluating Session Kit.

## Install

```bash
git clone https://github.com/dob323/session-kit.git
cd session-kit
./install.sh --check
tests/run
./install.sh
```

The preflight is read-only. The installer asks before enabling the SSH picker
and local terminal journals, does not start or restart services, and keeps each
installed Git revision as an immutable release. In a non-interactive shell,
login integration stays disabled and journals default on; pass
`--enable-login`, `--disable-login`, or `--journal on|off` explicitly when
needed.

After installation:

```bash
session-kit doctor
session-kit enable-login
session-kit disable-login
session-kit update --source "$PWD"
session-kit rollback
session-kit uninstall
```

The installer copies systemd user units on Linux but prints the commands
instead of starting them. Review [docs/install.md](docs/install.md) before
enabling any service. The macOS core preview requires
`SESSION_KIT_MACOS_PREVIEW=1`, Bash 4 or newer, and the separate acceptance
limits in [macos/README.md](macos/README.md).

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

`sp go` opens a detached session. `sp takeover`, `sp close`, repair actions,
and pruning require fresh identity checks and confirmation. See
[docs/usage.md](docs/usage.md) for the full behavior.

## Configuration

Session Kit follows XDG directories:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/
${XDG_STATE_HOME:-$HOME/.local/state}/session-kit/
${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal/
```

Start with:

- [config/session-inventory.example.json](config/session-inventory.example.json)
- [config/projects.example.tsv](config/projects.example.tsv)
- [config/shpool.example.toml](config/shpool.example.toml)

Configuration files and state can contain conversation titles, working
directories, UUIDs, and terminal output. Keep directories mode `0700` and files
mode `0600`.

Read [docs/configuration.md](docs/configuration.md) and
[docs/provider-integration.md](docs/provider-integration.md).

## Safety model

Session Kit treats PID start times, shpool generations, provider UUIDs, and
private proof files as mutation guards. It fails closed when current identity
cannot be proved.

It is not a boundary against another process running as the same Unix account.
A same-account process can read terminal state and edit owner-writable
configuration. Session Kit is designed for a cooperative single-user account.

The watchdog reports quiet sessions, manager timeouts, and binary changes. Its
supported public policy is **report-only**. A user may run an explicit repair
after reviewing the evidence.

Read [docs/security-and-data.md](docs/security-and-data.md) for the complete
trust and retention model.

## Documentation

- [Install](docs/install.md)
- [Configure](docs/configuration.md)
- [Use Session Kit](docs/usage.md)
- [Connect Claude Code and Codex](docs/provider-integration.md)
- [Security and local data](docs/security-and-data.md)
- [Update and roll back](docs/update-and-rollback.md)
- [Uninstall](docs/uninstall.md)
- [Troubleshoot](docs/troubleshooting.md)
- [Architecture](docs/architecture.md)
- [Maintainer release process](docs/maintainers/release-process.md)
- [Legacy installation migration](docs/migrations/legacy-install.md)

## Contributing

Issues and pull requests are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md). Report security problems using the private
process in [SECURITY.md](SECURITY.md), not a public issue.

## License

Session Kit is released under the [MIT License](LICENSE). The optional shpool
patch is derived from Apache-2.0 software; see
[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).
