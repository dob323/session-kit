# Contributing

Session Kit welcomes focused bug reports, feature requests, documentation
fixes, and pull requests.

The project is preparing a Linux-only `v0.1.0` beta. Reports about provider
compatibility, lifecycle safety, privacy, and clean installation are especially
useful.

## Before opening an issue

Search existing issues, then collect:

- the Session Kit tag or full commit;
- Linux distribution and version;
- Bash, Python, shpool, Claude Code, and Codex versions as relevant;
- whether the problem affects display, reply state, opening, moving, provider
  exit, kill, recovery, journals, cleanup, or health checks;
- sanitized expected and observed behavior.

Remove credentials, hostnames, account names, private paths, titles, UUIDs,
terminal output, journal content, provider prompts, and provider responses.
Report security issues through [the private process](SECURITY.md).

## Development

```bash
git clone https://github.com/dob323/session-kit.git
cd session-kit
tests/run
```

The tests use temporary fixtures and do not contact a live shpool daemon.

For shell changes:

```bash
bash -n install.sh bin/* bashrc/shpool.bashrc \
  deploy/session-kit-launcher tests/run
shellcheck install.sh bin/* bashrc/shpool.bashrc \
  deploy/session-kit-launcher tests/run
```

For Python and public-release checks:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/coverage run --branch --source=lib \
  -m unittest discover -s tests -t .
.venv/bin/coverage report --fail-under=70
tools/check-doc-links
tools/public-scan .
tools/public-scan . --git-history
```

CI tests Ubuntu 22.04 and 24.04 with Python 3.10 through 3.13. It also checks
Bash syntax, ShellCheck, Ruff, Python syntax, the type baseline, branch
coverage, public export, local links, reachable history, the optional shpool
patch, and release-artifact reproducibility.

## Pull requests

- Keep one behavior change per pull request.
- Add regression tests for runtime changes.
- Update docs for command, configuration, support, privacy, retention, or
  lifecycle changes.
- Use `$HOME`, XDG paths, and example UUIDs instead of real local values.
- Preserve fresh identity proof and confirmation for every mutation.
- Explain active-session, provider-exit, cleanup, update, and rollback effects.
- Do not weaken Linux-only fail-closed checks.
- Do not add telemetry, prompt logging, terminal logging, or notifications by
  default.

By contributing, you agree that your contribution is licensed under the
[MIT License](LICENSE). Changes to the shpool-derived patch remain under
[Apache License 2.0](LICENSES/Apache-2.0.txt).
