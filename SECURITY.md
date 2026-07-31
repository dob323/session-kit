# Security policy

## Supported versions

Before `v0.1.0`, only the current `main` branch receives security fixes.

After the first beta tag, each beta minor line is supported for 90 days after
its first release or for 30 days after the next beta minor release, whichever
ends later. Security fixes may require the latest patch release. Unsupported
platforms, including macOS, are outside this policy.

## Report privately

Do not publish vulnerability details, exploit steps, terminal output, provider
content, UUIDs, local paths, or credentials in an issue.

Use GitHub private vulnerability reporting from the repository Security tab:

<https://github.com/dob323/session-kit/security/advisories/new>

If private reporting is unavailable, open a public issue containing only a
request for a private contact channel. Do not describe the vulnerability.

Include privately:

- affected tag or full commit;
- Linux and dependency versions;
- the security boundary involved;
- minimal reproduction with secrets removed;
- whether a live session or private data can be changed or exposed;
- a safe temporary workaround, if known.

The maintainer will acknowledge a valid private report, investigate it, and
coordinate disclosure after a fix is available. Response times are a
best-effort beta service, not an emergency support guarantee.

## Boundaries

Session Kit is local-only and has no telemetry. It reads local process,
provider, shpool, configuration, and private Session Kit state.

It does not isolate hostile processes running as the same Unix user. Such a
process may read owner-accessible terminal state or edit owner-writable files.

Optional terminal journals are off by default because they may contain
credentials, source code, prompts, and terminal output. Read
[Security and local data](docs/security-and-data.md) before enabling them.
