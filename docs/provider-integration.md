# Connect Claude Code and Codex

Session Kit supports Claude Code and Codex as first-class providers. Either may
be installed alone; using both gives one combined session view.

## How provider identity is found

Claude Code identity comes from `claude agents --json` joined to the exact
process tree.

Codex identity comes from a native Codex process's open root rollout with
`source=cli`, plus local Codex metadata where available. A raw database title
may be the first prompt and is not automatically treated as an explicit rename.

Working directory and recency are display context. They never establish
provider identity.

## Automatic task titles

Provider instructions may ask a new managed root conversation to assign one
short title:

```text
At the first substantive request in a new Session Kit-managed root
conversation, before spawning child agents, run:

  sp self-name "<2-5 word Task Focused Title>"

If exact identity is not ready, retry once on the next root turn. Do not run
this command from a child agent.
```

Place that instruction in the normal user-level instruction file supported by
your installed provider. Review the provider's current documentation before
editing its instruction files.

`sp self-name` accepts only 2–5 word Title Case names, checks the managed root
conversation and process generation, and verifies the stored result. Manual
aliases always win.

Automatic titles remain in local configuration until they are explicitly reset
or pruned through the audited title-maintenance interface.

Set `SESSION_KIT_AUTO_NAME=0` to disable automatic naming and display without
deleting retained titles.

## Reply states

Session Kit shows `needs your reply` only for a structured provider question
that is still unresolved.

For Codex:

- an unresolved `request_user_input` without `autoResolutionMs` needs a reply;
- a picker with `autoResolutionMs` is `reply optional`;
- an exact tool output resolves the picker;
- a new task supersedes an unanswered picker from the prior task;
- completed or aborted tasks do not retain a reply alert.

The classifier uses structured rollout lifecycle events, not prose matching.
If the required event window is missing or malformed, the state is unavailable
instead of guessed.

Claude Code status is taken from its structured agent inventory.

## Resume and fork

The only conversation resume forms are:

```text
claude --resume <exact-uuid>
codex --no-alt-screen resume <exact-uuid>
```

Independent forks use:

```text
claude --resume <exact-source-uuid> --fork-session
codex --no-alt-screen fork <exact-source-uuid>
```

Session Kit validates the source and resulting provider UUID. It refuses an
ambiguous, already-active, or changed identity.

## Compatibility

Provider local formats and command interfaces can change. The first public
release must publish the exact Claude Code and Codex versions used in its
clean-install and recovery tests. Until that matrix exists, `main` is a beta
candidate rather than a compatibility promise.
