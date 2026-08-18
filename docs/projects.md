# Projects

A project is a directory. Everything Session Kit knows about your work,
the shortcut you launch it by, the settings it launches with, and the sessions
running in it, hangs off that one directory.

- [What a project is](#what-a-project-is)
- [The project manifest](#the-project-manifest)
- [Why a manifest has to be trusted first](#why-a-manifest-has-to-be-trusted-first)
- [Startup commands](#startup-commands)
- [Worktrees](#worktrees)
- [Reading a project from the command line](#reading-a-project-from-the-command-line)

## What a project is

A project's identity is its **canonical absolute root directory**. Two things
can point at that root:

- a row in `projects.tsv`, the host's own shortcut list, one entry per
  directory, carrying an alias and at most a *default* provider. The default is
  never a lock: which provider opens the directory is chosen when a session
  starts, so one directory stays one project however many providers you work in
  it with. See [Project aliases](configuration.md#project-aliases).
- a `session-kit.toml` committed at the root of the repository itself.

Anything with a working directory belongs to the project whose root is the
**deepest** one at or above that directory. That one rule places a live
session and the directory you are standing in, so the picker and `sp new` never
disagree about what is in a project.

A directory with neither a manifest above it nor a shortcut row above it is in
no project. Its sessions are still listed; they are simply not grouped.

A shortcut always launches its own directory. If you add a shortcut for a
subdirectory of a project, sessions there are still grouped under the project,
but the launch uses that shortcut's own settings, not the manifest further up.
A manifest governs launches for its own directory.

## The project manifest

`session-kit.toml` lives at the root of your repository and is committed with
it, so the setup travels with a clone instead of living on one machine:

```toml
# session-kit.toml
name = "demo-api"
description = "Demo API service"
provider = "codex"
account = "work"
model = "gpt-5-1-codex"
```

| Key | Meaning |
|---|---|
| `name` | Short name shown wherever the project is named. Lowercase letters, numbers, `_`, `-`. |
| `root` | Optional. The project root relative to the manifest. Must stay inside the manifest's own directory; defaults to `.`. |
| `description` | Optional one-line description. |
| `provider` | `claude`, `codex`, or `shell`. |
| `account` | An account alias already enrolled on the host. |
| `model` | The model identifier to launch with. |
| `startup` | A command to run after launch. See [Startup commands](#startup-commands). |

Session Kit reads a documented subset of TOML, single-line strings, integers,
booleans, and single-line arrays, with the same reader on
every supported Python, so a manifest cannot mean one thing on Python 3.13 and
another on 3.10. Anything outside the subset is refused with its line number
rather than half-applied. Check a manifest before committing it:

```text
session-kit projects check .
```

A manifest that fails to parse never changes a launch, and the project it
describes still resolves, the problem is reported, and the sessions running
there stay grouped where they belong.

## Why a manifest has to be trusted first

A manifest is repository content. Whoever can push to a repository can write
it, and cloning a repository is not a decision to let it choose what runs on
your machine. So a manifest's launch settings apply only for a project **on
this host's project list**:

```text
session-kit projects add demo /absolute/path/to/demo
```

Until then the manifest is read, shown, and reported, you can see exactly what
the repository proposes, but the provider, account, model, and startup command
are not applied. Adding the project is the deliberate act that turns them on. A
[worktree](#worktrees) of a listed repository inherits that decision.

## Startup commands

`startup` is a command line arriving from a repository, so it is approved once
per project, by its exact text:

- an unapproved command is shown, never run;
- approving records the command's digest for your account only;
- editing the command in the repository withdraws the approval, and it must be
  approved again;
- a non-interactive launch, a script, never approves
  anything. It starts without the startup command and says so.

Review and approve one from the command line:

```text
session-kit projects launch-plan <alias>       # what would start, and why
session-kit projects approve-startup <alias>   # approve the exact command text
```

`sp new <alias>` says which of the two states it found, `not approved here, so
it was not run`, or `changed since it was approved, so it was not run`, and
names the file the command lives in. The approval records the command's digest
for your account only; nothing about it travels with the repository.

## Worktrees

A linked git worktree contains the same committed manifest as the repository it
was cut from, so it resolves as its own root with the same settings. Session
Kit reads the worktree's `.git` pointer file to find the main repository and
groups them together: one project, several working copies, rather than several
unrelated projects that happen to share a name.

## Reading a project from the command line

Every verb prints JSON and is safe to run at any time; none of them change a
session. `approve-startup` is the one that writes: it records an approval for
your account, and nothing else.

```text
session-kit projects resolve            # the project you are standing in
session-kit projects resolve /some/path
session-kit projects list               # every project this host knows
session-kit projects launch-plan demo   # what `sp new demo` would start, and why
session-kit projects context demo       # sessions in the project
session-kit projects check .            # validate a session-kit.toml
session-kit projects approve-startup demo   # approve this project's startup command
```

`launch-plan` reports a `decisions` map naming the source of every applied
value, `flag`, `manifest`, `shortcut`, or `default`, so a session that
differs from what you typed can always be explained.

`context` answers "where did I leave this?": the live sessions in the project
and its worktrees. When a store cannot be read it is named in `unavailable`, so
an empty list never quietly means "nothing is happening".

Exit codes: `0` an answer, `1` no project covers the target, `2` bad arguments,
`3` a malformed manifest.
