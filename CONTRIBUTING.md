# Contributing

Session Kit welcomes focused bug reports, feature requests, documentation
fixes, and pull requests.

The project ships Linux and macOS public betas, published as GitHub
prereleases. Reports about provider compatibility, lifecycle safety, privacy,
and clean installation are especially useful, and reports from operating
systems, shells, and hardware the maintainer cannot test are the most valuable
of all.

## What to expect

Session Kit is maintained by one person alongside other work. Replies are
best-effort and there is no response-time commitment; a quiet issue has not been
rejected. Security reports follow [their own process](SECURITY.md) and are
handled ahead of everything else.

A pull request may be declined even when the code is correct, if it would
weaken the identity and safety model, add a default that writes or transmits
data, or introduce behavior that cannot be proved from local evidence. Opening
an issue before a large change saves work on both sides.

## Before opening an issue

Search existing issues, then collect:

- the Session Kit tag or full commit;
- operating system version and architecture;
- Bash, Python, shpool, Claude Code, and Codex versions as relevant;
- whether the problem affects display, reply state, opening, moving, provider
  exit, close, recovery, journals, cleanup, or health checks;
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

CI tests Ubuntu 22.04 and 24.04 with Python 3.10 through 3.13. Native macOS 15
runners cover Apple Silicon and Intel with Python 3.13 and Homebrew Bash. CI
also checks Bash syntax, ShellCheck, Ruff, Python syntax, types, branch
coverage, public export, local links, reachable history, the optional shpool
patch, and release-artifact reproducibility.

The macOS CI lane proves the native process adapter and fixture-based lifecycle
behavior. It does not replace acceptance with a real shpool daemon and provider
installations on a persistent Mac account.

## Pull requests

- Keep one behavior change per pull request.
- Add regression tests for runtime changes.
- Update docs for command, configuration, support, privacy, retention, or
  lifecycle changes.
- Use `$HOME`, XDG paths, and example UUIDs instead of real local values.
- Preserve fresh identity proof and an exact-target safety display for every
  mutation.
- Explain active-session, provider-exit, cleanup, update, and rollback effects.
- Preserve both Linux `/proc` and Darwin native process-generation checks.
- Keep platform-specific service operations explicit: systemd on Linux and
  per-user launchd jobs on macOS.
- Do not add telemetry, prompt logging, terminal logging, or notifications by
  default.

A change that moves code between modules must not change behavior in the same
commit, and every symbol an existing test patches on `lib/session_inventory.py`
must stay reachable through it. See
[the modularization roadmap](docs/maintainers/modularization-roadmap.md) for the
compatibility contract and the patch-point ledger.

By contributing, you agree that your contribution is licensed under the
[MIT License](LICENSE). Changes to the shpool-derived patch remain under
[Apache License 2.0](LICENSES/Apache-2.0.txt).
