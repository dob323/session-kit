# Security policy

## Supported versions

Session Kit is preparing its first public beta, `v0.1.0`. Until that release is
tagged, only the current `main` branch receives security fixes. A support
window for tagged versions will be published with the first release.

## Reporting a vulnerability

Do not include vulnerability details, terminal output, conversation UUIDs,
journal contents, or local paths in a public issue.

Use GitHub's private vulnerability reporting or open a private security
advisory from the repository's **Security** tab. If private reporting is not
available, open a public issue that asks the maintainer to establish a private
contact channel, without describing the vulnerability.

Include:

- the affected commit or version;
- the operating system and relevant dependency versions;
- the security boundary that was crossed;
- minimal reproduction steps with secrets removed;
- whether the problem can modify or expose an active session;
- any safe workaround.

Please allow the maintainer time to confirm the report and prepare a fix before
public disclosure.

## Security boundaries

Session Kit is local-only and has no telemetry. It reads process metadata,
provider-owned local conversation metadata, shpool state, and its own private
state files.

Session Kit does not isolate mutually hostile processes running as the same
Unix user. A same-account process can inspect owner-readable terminal state,
forge environment values, or edit owner-writable configuration directly.

Terminal journals can contain passwords, tokens, source code, and any other
text displayed by a terminal. See
[docs/security-and-data.md](docs/security-and-data.md) before enabling Session
Kit on a shared or regulated system.
