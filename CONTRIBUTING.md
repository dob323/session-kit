# Contributing

Session Kit accepts bug reports, feature requests, documentation fixes, and
pull requests.

The project is preparing a `v0.1.0` beta. Compatibility and safety reports are
especially useful during this stage.

## Before opening an issue

Search existing issues, then collect:

- the Session Kit commit or version;
- Linux distribution and version;
- Bash, Python, shpool, Claude Code, and Codex versions as applicable;
- whether the problem affects listing, display, opening, takeover, closing,
  recovery, journals, or the watchdog;
- sanitized output that contains no UUIDs, credentials, private paths, or
  terminal journal content.

Use the bug or feature template. Security problems belong in the private
process described by [SECURITY.md](SECURITY.md).

## Development setup

Clone the repository and run the isolated test suite:

```bash
git clone https://github.com/dob323/session-kit.git
cd session-kit
tests/run
```

The suite creates temporary fixtures and does not contact a live shpool daemon
or service manager.

For shell changes, also run:

```bash
bash -n bin/* bashrc/shpool.bashrc deploy/session-kit-launcher
shellcheck bin/* bashrc/shpool.bashrc deploy/session-kit-launcher
```

Run `python3 -m compileall -q lib tests deploy/session-kit-release` for Python
syntax checking. Python bytecode is ignored by Git.

## Pull requests

- Keep one behavior change per pull request.
- Add regression tests for runtime changes.
- Update public documentation when commands, configuration, data retention, or
  support boundaries change.
- Use account-neutral examples based on `$HOME` or XDG directories.
- Do not include provider transcripts, real UUIDs, private paths, hostnames,
  tokens, or production incident data.
- Preserve fail-closed identity checks and confirmation for mutations.
- Explain update and rollback effects when state formats change.

By contributing, you agree that your contribution is licensed under the
project's [MIT License](LICENSE).
