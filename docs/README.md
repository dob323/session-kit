# Session Kit documentation

Everything here is written for one of four moments: getting it installed, using
it day to day, understanding what it trusts, or fixing something. Find the
moment you are in.

Back to the [project README](../README.md).

## Getting it running

| Document | What it covers |
|---|---|
| [Install Session Kit](install.md) | Every install route, the supported shpool paths, provider setup, project import, and activation. |
| [Use Session Kit](usage.md) | Opening the picker, starting sessions, and what each command does once you are running. |
| [Projects](projects.md) | A project is a directory: the shortcut you launch it by, the settings it launches with, and the sessions that belong to it. |

## Using it day to day

| Document | What it covers |
|---|---|
| [Picker navigation](picker-navigation.md) | The home screen, the cursor-driven picker, mouse behaviour, action panels, machine sessions, and closed-session restore. |
| [Configure Session Kit](configuration.md) | Every setting, including account enrolment, colour, terminal titles, and where host configuration lives. |
| [Claude Code and Codex integration](provider-integration.md) | The exact contract with each provider, and what stays under the provider's control. |

## When something is wrong

| Document | What it covers |
|---|---|
| [Troubleshooting](troubleshooting.md) | Read-only checks first, then the named problems and their remedies. |
| [Update and roll back](update-and-rollback.md) | How releases are installed and selected, and how to go back to one already on the machine. |
| [Uninstall](uninstall.md) | What `session-kit uninstall` removes, and what it deliberately leaves alone. |
| [Migrate an older installation](migrations/legacy-install.md) | A procedure, not a command, for moving a hand-managed shpool setup onto Session Kit. |

## What it trusts, and why

| Document | What it covers |
|---|---|
| [Architecture](architecture.md) | How shpool terminals are joined to provider processes without treating display text as identity. |
| [Security and local data](security-and-data.md) | No hosted service, no telemetry: what is read, what is written, and what stays owner-only. |
| [Vendor formats this kit depends on](pinned-internal-formats.md) | The provider files the kit reads and writes, which both vendors call internal and free to change. |

## For maintainers

These are kept in the open because the project's claims should be checkable.

| Document | What it covers |
|---|---|
| [Voice](voice.md) | The contract for every string the kit can show a person, so every surface speaks one way. |
| [Release process](maintainers/release-process.md) | The public release checklist: one reviewed commit carried unchanged through export, artifact, tag, and publication. |
| [Inventory modularization contract](maintainers/modularization-roadmap.md) | How the inventory facade and its implementation modules are allowed to move. |

## Elsewhere in the repository

| Document | What it covers |
|---|---|
| [CHANGELOG](../CHANGELOG.md) | Release history. |
| [Contributing](../CONTRIBUTING.md) | What is welcome, and what may be declined. |
| [Security policy](../SECURITY.md) | Supported versions, and how to report a vulnerability privately. |
| [shpool patches](../shpool-patch/) | The optional patches, their scope, and their checks. |
