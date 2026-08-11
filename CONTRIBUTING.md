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
- Name roles, never people: no personal names in shipped files (see
  [No personal names in shipped files](#no-personal-names-in-shipped-files)).

A change that moves code between modules must not change behavior in the same
commit, and every symbol an existing test patches on `lib/session_inventory.py`
must stay reachable through it. See
[the modularization roadmap](docs/maintainers/modularization-roadmap.md) for the
compatibility contract and the patch-point ledger.

## No personal names in shipped files

Everything the public export ships — code, comments, docstrings, tests,
identifiers, configuration, prompts, and docs — names roles, not people. Write
"the operator" or "the maintainer"; never a person's name. This covers
identifiers as much as prose: a keyword argument or JSON key with a name in it
ships just as publicly as a comment does.

The same rule covers private account identifiers: real account aliases,
usernames, hostnames, and email addresses never ship, in fixtures least of all.
Test data uses neutral aliases and `@invalid.example` addresses. The project's
own public GitHub slug is not private and is expected to appear.

This is enforced, not advisory. `tools/public-scan --private-markers` rejects a
release tree that contains a personal-name token, and it is the gate that
`tools/build-public-tree` runs on every export. The scanner stores the blocked
words only as one-way SHA-256 digests in `PRIVATE_TOKEN_DIGESTS`, so the
scanner itself stays safe to publish, and it tests each underscore-separated
part of an identifier as well as the whole token — `operator_confirmed` is
fine, a name in that position is not.

The rule reaches published history too, and history published before the rule
existed cannot be rewritten out of clones and forks that already hold it.
`tools/public-scan-history-baseline` lists the blob object ids that were read
and accepted, and `tools/public-scan --git-history --baseline` skips those
exact objects. It skips nothing else: a credential in a listed blob still
fails, the working-tree scan ignores the baseline entirely, and an edited file
is a different object that fails again. Append to that list only after reading
the blob and only for something already published — a failure on a blob that
has not shipped means the tree still has to be fixed.

Adding a new blocked word means adding its digest, not the word:

```
python3 -c 'import hashlib; print(hashlib.sha256(b"word").hexdigest())'
```

Attribution required by an upstream licence is the one exception: an upstream
copyright holder's name stays exactly as the licence requires.

## Licensing

By contributing, you agree that your contribution is licensed under the
[MIT License](LICENSE). Changes to the shpool-derived patches remain under
[Apache License 2.0](LICENSES/Apache-2.0.txt), and changes to the vendored
Maniple code under
[its MIT licence](lib/sessionkit_supervisor/vendor/LICENSE). Third-party
attribution lives in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).
